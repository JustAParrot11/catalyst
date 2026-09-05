"""Claude learned nothing between calls. Now it is shown its record.

CLAUDE.md, "What is not proven": "Claude learns nothing between calls.
No past outcome reaches the research prompt. It judges every candidate
as if it were the first. Closing that loop is the highest-value next
change."

OWNER-ASKED 2026-09-05: "optimize how agentic and self sufficient it
is". The outcomes already exist - closed_trades and scored refusals -
and research/record.py renders them into a bounded section of the
research prompt. Informational, like the market data: it sizes nothing
and no code reads it back. The adaptive parameters still move only on
closed outcomes with sample minimums; this lets the model see the same
evidence they see.

Fully offline; no calendar dates (house rule 6).
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.research.record import (
    MAX_REFUSALS, MAX_TRADES, RECORD_HEADER, recent_record, render_record,
)
from catalyst.storage import init_db

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "r.db"))
    yield conn
    conn.close()


def candidate(conn, cid, ticker, ctype="insider_cluster"):
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (cid, ticker, ctype, NOW.date().isoformat(), "confirmed",
                  "[]", NOW.isoformat(), "unknown", "[]"))


def view(conn, cid, direction, conviction, priced_in=False):
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 (cid, direction, conviction, "thesis", "inval", 12,
                  int(priced_in), "why"))


def closed_trade(conn, cid, ticker, entry, exit_, reason="hard_exit_date",
                 held=12, expected=12, conviction=0.58, ctype="insider_cluster"):
    candidate(conn, cid, ticker, ctype)
    view(conn, cid, "long", conviction)
    oid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    conn.execute(
        "INSERT INTO orders (id, decision_id, broker_order_id, side, qty, "
        "order_type, time_in_force, submitted_at, status, raw_response) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (oid, cid, "b", "buy", "10", "market", "day", NOW.isoformat(),
         "filled", "{}"))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 (pid, ticker, json.dumps([oid]), None, NOW.isoformat(),
                  NOW.date().isoformat(), "closed"))
    conn.execute(
        "INSERT INTO closed_trades (position_id, account_mode, entry_price, "
        "exit_price, exit_reason, realized_pnl_cents, expected_holding_days, "
        "actual_holding_days, closed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, "paper", str(entry), str(exit_), reason,
         int((exit_ - entry) * 1000), expected, held, NOW.isoformat()))
    conn.commit()


def scored_refusal(conn, cid, ticker, direction, conviction, ret,
                   priced_in=False, why="below_conviction_floor",
                   days_ago=20, ctype="insider_cluster"):
    candidate(conn, cid, ticker, ctype)
    view(conn, cid, direction, conviction, priced_in)
    did = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (did, cid, "skip", None, None, None, None, None,
         json.dumps([why]), "{}", NOW.isoformat()))
    refused = NOW - timedelta(days=days_ago)
    conn.execute(
        "INSERT INTO refusals (decision_id, candidate_id, price_at_refusal, "
        "refused_at, scored_at, outcome_price, outcome_return) "
        "VALUES (?,?,?,?,?,?,?)",
        (did, cid, "100", refused.isoformat(), NOW.isoformat(),
         str(100 * (1 + ret)), str(ret)))
    conn.commit()


class TestNothingToSayIsNoSection:
    def test_an_empty_database_renders_none(self, db):
        assert recent_record(db) is None

    def test_an_unscored_refusal_is_not_history_yet(self, db):
        candidate(db, "c1", "AAA")
        view(db, "c1", "long", 0.55)
        did = str(uuid.uuid4())
        db.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (did, "c1", "skip", None, None, None, None, None,
                    '["below_conviction_floor"]', "{}", NOW.isoformat()))
        db.execute("INSERT INTO refusals (decision_id, candidate_id, "
                   "price_at_refusal, refused_at) VALUES (?,?,?,?)",
                   (did, "c1", "100", NOW.isoformat()))
        db.commit()
        assert recent_record(db) is None

    def test_a_broken_database_never_raises(self, tmp_path):
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "empty.db"))   # no tables
        assert recent_record(conn) is None


class TestTheRecordSaysWhatHappened:
    def test_a_closed_loss_is_shown_with_what_the_model_said(self, db):
        closed_trade(db, "c1", "EMBC", 5.05, 4.62, conviction=0.60)
        text = recent_record(db)
        assert text.startswith(RECORD_HEADER)
        assert "EMBC" in text and "long at conviction 0.60" in text
        assert "-8.5%" in text and "hard_exit_date" in text

    def test_a_declined_name_that_rose_is_named_as_refused_without_skill(self, db):
        scored_refusal(db, "c2", "NVDA", "long", 0.56, 0.12)
        text = recent_record(db)
        assert "NVDA" in text and "you said long at 0.56" in text
        assert "+12.0%" in text
        assert "refused without skill" in text

    def test_a_priced_in_refusal_is_flagged_as_such(self, db):
        scored_refusal(db, "c3", "ZNB", "no_trade", 0.72, 0.09, priced_in=True)
        text = recent_record(db)
        assert "[called priced in]" in text
        assert "+9.0%" in text

    def test_both_sections_appear_together(self, db):
        closed_trade(db, "c1", "EMBC", 5.05, 4.62)
        scored_refusal(db, "c2", "NVDA", "long", 0.56, 0.12)
        text = recent_record(db)
        assert "Closed trades" in text and "Declined candidates" in text


class TestItIsBounded:
    def test_at_most_max_trades_and_max_refusals(self, db):
        for i in range(MAX_TRADES + 5):
            closed_trade(db, f"t{i}", f"T{i}", 10, 11)
        for i in range(MAX_REFUSALS + 5):
            scored_refusal(db, f"r{i}", f"R{i}", "long", 0.55, 0.01)
        text = recent_record(db)
        assert f"Closed trades, newest first ({MAX_TRADES})" in text
        assert f"({MAX_REFUSALS}) - what the stock did" in text

    def test_the_section_is_a_few_hundred_tokens_not_the_prompt(self, db):
        for i in range(MAX_TRADES):
            closed_trade(db, f"t{i}", f"T{i}", 10, 11)
        for i in range(MAX_REFUSALS):
            scored_refusal(db, f"r{i}", f"R{i}", "long", 0.55, 0.01)
        assert len(recent_record(db)) < 3000


class TestItReachesThePrompt:
    def test_investigate_renders_the_record_into_the_prompt(self):
        """Not a rendering helper nobody calls (the adaptation loop was
        exactly that for a week)."""
        import inspect

        from catalyst.research import boundary

        src = inspect.getsource(boundary.investigate)
        assert "recent_record(conn)" in src
        assert "record=" in src

    def test_the_prompt_places_it_and_marks_it_informational(self, db):
        from datetime import date

        from catalyst.discovery import Candidate
        from catalyst.research import prompts

        closed_trade(db, "c1", "EMBC", 5.05, 4.62)
        c = Candidate(id="x", ticker="AAA", catalyst_type="insider_cluster",
                      catalyst_date=date.today(), catalyst_date_confidence="confirmed",
                      source_event_ids=("s",), discovered_at=NOW, sector="u",
                      correlation_tags=("type:insider_cluster",))
        text = prompts.render_research_prompt(c, record=recent_record(db))
        assert RECORD_HEADER in text
        assert "sizes nothing and decides nothing" in text
        assert text.index(RECORD_HEADER) < text.index("ANSWER THESE")

    def test_no_record_leaves_the_prompt_exactly_as_before(self):
        from datetime import date

        from catalyst.discovery import Candidate
        from catalyst.research import prompts

        c = Candidate(id="x", ticker="AAA", catalyst_type="insider_cluster",
                      catalyst_date=date.today(), catalyst_date_confidence="confirmed",
                      source_event_ids=("s",), discovered_at=NOW, sector="u",
                      correlation_tags=("type:insider_cluster",))
        assert prompts.render_research_prompt(c) == \
            prompts.render_research_prompt(c, record=None)
        assert RECORD_HEADER not in prompts.render_research_prompt(c)


class TestItNeverSizesAnything:
    def test_the_section_carries_no_instruction_words(self, db):
        """Informational text only. The words the boundary refuses
        must not appear in something the system writes to the model."""
        closed_trade(db, "c1", "EMBC", 5.05, 4.62)
        scored_refusal(db, "c2", "NVDA", "long", 0.56, 0.12)
        text = recent_record(db).lower()
        for banned in ("buy ", "sell ", "shares", "position size", "stop at"):
            assert banned not in text, banned


class TestTheCheckCanFail:
    def test_render_record_with_data_is_not_none(self):
        assert render_record([], []) is None
        assert render_record([{"ticker": "X", "catalyst_type": "t",
                               "direction": "long", "conviction": 0.5,
                               "ret": 0.1, "held": 1, "expected": 1,
                               "closed_at": "d", "exit_reason": "r"}], []) \
            is not None
