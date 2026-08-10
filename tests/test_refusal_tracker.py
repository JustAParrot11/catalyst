"""Refusal tracker tests - the feedback loop that turns "is the floor
too strict?" into a number. Offline via MockTransport."""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst.execution.broker import Broker
from catalyst.risk.refusal_tracker import (
    SCORING_HORIZON_DAYS, conviction_floor_evidence, score_due_refusals,
)

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=SCORING_HORIZON_DAYS + 1)
RECENT = NOW - timedelta(days=2)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.executescript(open("catalyst/storage/schema.sql").read())
    yield conn
    conn.close()


def seed_refusal(db, *, rid, ticker="TEST", price="50", refused_at=OLD,
                 reason="below_conviction_floor"):
    db.execute("INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
               (f"cand-{rid}", ticker, "insider_cluster", "2026-08-20",
                "estimated", "[]", refused_at.isoformat(), "tech", "[]"))
    db.execute(
        "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"dec-{rid}", f"cand-{rid}", "skip", None, None, None, None, None,
         f'["{reason}"]', "{}", refused_at.isoformat()))
    db.execute(
        "INSERT INTO refusals (decision_id, candidate_id, price_at_refusal, "
        "refused_at) VALUES (?,?,?,?)",
        (f"dec-{rid}", f"cand-{rid}", price, refused_at.isoformat()))
    db.commit()


def quote_broker(bid="54.95", ask="55.05"):
    def handler(request):
        return httpx.Response(200, json={"quote": {"bp": bid, "ap": ask}})

    return Broker("k", "s", transport=httpx.MockTransport(handler),
                  backoff_s=0)


class TestScoring:
    def test_due_refusal_scored_at_nbbo_mid(self, db):
        seed_refusal(db, rid=1, price="50")
        assert score_due_refusals(quote_broker(), db, NOW) == 1
        row = db.execute("SELECT outcome_price, outcome_return, scored_at "
                         "FROM refusals").fetchone()
        assert Decimal(row[0]) == Decimal("55")
        assert Decimal(row[1]) == Decimal("0.1")   # +10%: refusing cost us
        assert row[2] == NOW.isoformat()

    def test_recent_refusal_not_scored_yet(self, db):
        seed_refusal(db, rid=1, refused_at=RECENT)
        assert score_due_refusals(quote_broker(), db, NOW) == 0
        assert db.execute("SELECT scored_at FROM refusals").fetchone()[0] is None

    def test_broker_failure_leaves_unscored_never_fabricates(self, db):
        seed_refusal(db, rid=1)

        def handler(request):
            return httpx.Response(500, json={})

        b = Broker("k", "s", transport=httpx.MockTransport(handler),
                   backoff_s=0)
        assert score_due_refusals(b, db, NOW) == 0
        row = db.execute("SELECT scored_at, outcome_price FROM refusals"
                         ).fetchone()
        assert row == (None, None)

    def test_already_scored_not_rescored(self, db):
        seed_refusal(db, rid=1)
        score_due_refusals(quote_broker(), db, NOW)
        # different quote now; outcome must not move
        assert score_due_refusals(quote_broker("99", "101"), db, NOW) == 0
        assert Decimal(db.execute("SELECT outcome_price FROM refusals"
                                  ).fetchone()[0]) == Decimal("55")


class TestEvidence:
    def test_profitable_refusals_push_floor_down(self, db):
        # 5 refusals all +10%: the floor refused winners -> lower it
        for i in range(5):
            seed_refusal(db, rid=i, refused_at=OLD - timedelta(days=i))
        score_due_refusals(quote_broker(), db, NOW)
        ev = conviction_floor_evidence(db, NOW)
        assert ev is not None
        assert ev.parameter == "conviction_floor"
        assert len(ev.trade_ids) == 5
        assert ev.effect_size < 0                  # lower the value
        assert ev.window_start < ev.window_end

    def test_zero_variance_uniform_returns_low_significance_guard(self, db):
        # identical returns -> se=0 -> t=0 path must not divide by zero
        seed_refusal(db, rid=1)
        score_due_refusals(quote_broker(), db, NOW)
        ev = conviction_floor_evidence(db, NOW)
        assert ev.significance == Decimal("0.50")

    def test_other_skip_reasons_excluded(self, db):
        seed_refusal(db, rid=1, reason="spread_gate")
        score_due_refusals(quote_broker(), db, NOW)
        assert conviction_floor_evidence(db, NOW) is None

    def test_unscored_refusals_excluded(self, db):
        seed_refusal(db, rid=1, refused_at=RECENT)
        assert conviction_floor_evidence(db, NOW) is None

    def test_evidence_flows_into_adaptive_refusal(self, db):
        """End-to-end: 5 scored refusals is NOT enough sample for
        conviction_floor (min 30) - propose must refuse. The loop is
        wired, and the guard holds."""
        from catalyst.risk import adaptive_params as ap
        for i in range(5):
            seed_refusal(db, rid=i, refused_at=OLD - timedelta(days=i))
        score_due_refusals(quote_broker(), db, NOW)
        ev = conviction_floor_evidence(db, NOW)
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"), ev)
        assert not p.applicable
        assert p.reason.startswith("insufficient_sample: 5 of 30")
