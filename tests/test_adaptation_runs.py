"""The learning loop has to actually run, and has to be able to loosen.

TWO DEFECTS, one of omission and one of arithmetic.

THE OMISSION. `propose_adjustment`, `apply`, `maybe_auto_revert` and
`conviction_floor_evidence` were all built, reviewed and tested, and
NOTHING IN THE LIVE PATH CALLED ANY OF THEM. The refusal tracker
recorded refusals, scored them, aggregated them into an EvidenceSample -
and the sample was then dropped on the floor. Every threshold in the
system was frozen at its shipped estimate forever, while the dashboard
showed an adaptive system, because all the parts existed.

THE ARITHMETIC. `conviction_floor` could adapt up to 0.95. But
evaluate.py adds PRICED_IN_CONVICTION_PREMIUM (0.15) on top for any
candidate judged already priced in, and conviction is bounded at 1.0.
So at a floor of 0.95 the bar for a priced-in candidate is 1.10: not
"very hard", but IMPOSSIBLE, permanently, for every such candidate,
with nothing anywhere saying so.

OWNER-ASKED, 2026-08-14: "i dont want it to learn and make a hard limit
that stops all future trades. that data may of lost a trade that one
time but may win another trade in the future."

The whole point of the refusal tracker is that a too-strict threshold
is recoverable: refuse a candidate, watch what it does, and if refused
candidates keep being profitable, LOWER the bar. That only works if the
loop runs and if the bar it moves can be reached in the first place.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.risk import adaptation
from catalyst.risk.adaptive_params import (
    CONVICTION_FLOOR_CEILING, DEFAULT_PARAMS, MIN_SAMPLE_SIZE, PARAM_RANGE,
    current_values,
)
from catalyst.risk.evaluate import PRICED_IN_CONVICTION_PREMIUM

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "a.db")
    root = Path(__file__).resolve().parents[1]
    conn.executescript((root / "catalyst/storage/schema.sql").read_text())
    return conn


def seed_scored_refusal(conn, n, *, outcome_return, days_ago=30):
    """n candidates refused for below_conviction_floor, each scored with
    the return it went on to make.

    Returns are SPREAD around the mean, not identical. Real outcomes
    vary, and the significance test is a mean/standard-error one: a
    fixture where every refusal returned exactly the same number has
    zero variance, which that statistic reads as no confidence at all
    rather than as perfect consistency. Identical values would be
    testing a case that cannot occur.
    """
    spread = [Decimal("-0.02"), Decimal("0"), Decimal("0.01"),
              Decimal("0.02"), Decimal("-0.01")]
    for i in range(n):
        cid, did = f"cand-{i}", f"dec-{i}"
        refused_at = (NOW - timedelta(days=days_ago + i)).isoformat()
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     (cid, f"T{i}", "insider_cluster", "2026-07-01",
                      "estimated", "[]", refused_at, "tech", "[]"))
        conn.execute(
            "INSERT INTO risk_decisions (id, candidate_id, action, "
            "skip_reasons, adaptive_params_snapshot, decided_at) "
            "VALUES (?,?,?,?,?,?)",
            (did, cid, "skip", '["below_conviction_floor"]', "{}", refused_at))
        conn.execute(
            "INSERT INTO refusals (decision_id, candidate_id, "
            "price_at_refusal, refused_at, outcome_return, scored_at) "
            "VALUES (?,?,?,?,?,?)",
            (did, cid, "10.00", refused_at,
             str(outcome_return + spread[i % len(spread)]),
             (NOW - timedelta(days=1)).isoformat()))
    conn.commit()


class TestTheLoopActuallyRuns:
    def test_a_pass_completes_on_an_empty_database(self, db):
        """The ordinary day. Nothing to learn from, nothing moves, and
        it must not raise - this runs inside the trading loop."""
        report = adaptation.run_adaptation_pass(db, NOW)
        assert report.errors == []
        assert not report.changed_anything
        assert ("conviction_floor", "no_evidence") in report.considered

    def test_profitable_refusals_LOWER_the_conviction_floor(self, db):
        """The brief's headline loop, end to end: candidates were
        refused, they went on to make money, so the bar that refused
        them comes down."""
        seed_scored_refusal(db, MIN_SAMPLE_SIZE["conviction_floor"],
                            outcome_return=Decimal("0.08"))
        before = current_values(db)["conviction_floor"]
        report = adaptation.run_adaptation_pass(db, NOW)
        after = current_values(db)["conviction_floor"]

        assert report.applied, (
            f"the floor did not move. refused: {report.refused}, "
            f"errors: {report.errors}")
        assert after < before, (
            f"refused candidates were systematically profitable and the "
            f"floor went {before} -> {after}, the wrong way")

    def test_it_is_written_to_the_audit_log_with_its_evidence(self, db):
        seed_scored_refusal(db, MIN_SAMPLE_SIZE["conviction_floor"],
                            outcome_return=Decimal("0.08"))
        adaptation.run_adaptation_pass(db, NOW)
        row = db.execute(
            "SELECT parameter, old_value, new_value, evidence_summary "
            "FROM adaptive_param_log ORDER BY changed_at DESC "
            "LIMIT 1").fetchone()
        assert row is not None, "a parameter moved with no audit trail"
        assert row[0] == "conviction_floor"
        assert row[3] and row[3] != "{}", "no evidence recorded beside it"

    def test_a_tiny_sample_moves_nothing(self, db):
        """Adapting on four outcomes is fitting noise, which is the thing
        the loop exists to remove."""
        seed_scored_refusal(db, 3, outcome_return=Decimal("0.08"))
        report = adaptation.run_adaptation_pass(db, NOW)
        assert not report.applied
        assert any("insufficient_sample" in str(r) for _, r in report.refused)

    def test_one_parameter_failing_does_not_stop_the_others(self, db):
        def explode(conn, now=None, window_start=None):
            raise RuntimeError("evidence source is broken")

        original = dict(adaptation.EVIDENCE_SOURCES)
        try:
            adaptation.EVIDENCE_SOURCES["broken_param"] = explode
            report = adaptation.run_adaptation_pass(db, NOW)
        finally:
            adaptation.EVIDENCE_SOURCES.clear()
            adaptation.EVIDENCE_SOURCES.update(original)
        assert any("broken_param" in e for e in report.errors)
        assert ("conviction_floor", "no_evidence") in report.considered

    def test_it_never_raises_into_the_trading_loop(self, db):
        """Adaptation improves FUTURE decisions. A failure here must not
        stop the bot trading today."""
        db.execute("DROP TABLE refusals")
        db.commit()
        report = adaptation.run_adaptation_pass(db, NOW)   # must not raise
        assert report.errors, "a broken database produced no recorded error"


class TestTheFloorCanAlwaysBeReached:
    """The arithmetic half. A bar nothing can clear is not a high bar."""

    def test_the_priced_in_bar_stays_below_one(self):
        bar = CONVICTION_FLOOR_CEILING + PRICED_IN_CONVICTION_PREMIUM
        assert bar <= Decimal("1.0"), (
            f"a priced-in candidate would face a bar of {bar}, and "
            f"conviction cannot exceed 1.0 - every such candidate is "
            f"refused forever by arithmetic alone")

    def test_it_keeps_REAL_headroom_not_just_a_hair(self):
        """A bar of exactly 1.0 needs perfect certainty, which no honest
        research view expresses. Leave room for a real yes."""
        bar = CONVICTION_FLOOR_CEILING + PRICED_IN_CONVICTION_PREMIUM
        assert bar <= Decimal("0.90"), (
            f"the priced-in bar is {bar}; nothing realistic clears it")

    def test_the_range_uses_that_ceiling(self):
        assert PARAM_RANGE["conviction_floor"][1] == CONVICTION_FLOOR_CEILING

    def test_the_ceiling_is_above_the_shipped_default(self):
        """It must still be able to tighten at all."""
        assert CONVICTION_FLOOR_CEILING > DEFAULT_PARAMS["conviction_floor"]

    def test_the_floor_can_never_seal_itself_shut(self, db):
        """The property the owner actually asked for, as a test: however
        the evidence reads, the floor cannot reach a level that refuses
        everything permanently."""
        lo, hi = PARAM_RANGE["conviction_floor"]
        assert hi + PRICED_IN_CONVICTION_PREMIUM < Decimal("1.0")
        assert lo > Decimal("0"), "and it cannot fall to accepting anything"


class TestTheDashboardShowsTheFloorTHISDecisionFaced:
    """The conviction gauge draws a marker at the floor. It was hardcoded
    at 0.60, which was harmless only while the floor could never move -
    and it could never move because the adaptation loop was never wired
    up. Now that it runs, a fixed marker would put the threshold line in
    the wrong place on the one chart whose whole job is showing whether
    the model cleared it.

    Found by running it, not by reading it: the first version called
    `jload(x)` without its required `default` argument, and the resulting
    TypeError was swallowed by the function's own `except` - so it
    silently returned the default every time while looking correct.
    """

    def test_it_reads_the_value_in_force_at_the_time(self):
        import json as _json

        from catalyst.dashboard.panels import _decision_floor

        floor, source = _decision_floor(
            {"adaptive_params_snapshot":
                _json.dumps({"conviction_floor": "0.72"})})
        assert floor == 0.72, (
            "the gauge is not reading the decision's own snapshot - it "
            "would draw the threshold marker in the wrong place")
        assert "in force" in source

    @pytest.mark.parametrize("snapshot", [None, "", "not json", "[]", "{}"])
    def test_an_unreadable_snapshot_falls_back_and_SAYS_SO(self, snapshot):
        from catalyst.dashboard.panels import _decision_floor

        floor, source = _decision_floor(
            {"adaptive_params_snapshot": snapshot})
        assert floor == float(DEFAULT_PARAMS["conviction_floor"])
        assert "default" in source, (
            "a fallback figure is presented without saying it is one")

    def test_the_marker_moves_with_the_floor(self):
        import json as _json

        from catalyst.dashboard.panels import _conviction_gauge

        low = _conviction_gauge(0.71, "p", {"adaptive_params_snapshot":
                                            _json.dumps({"conviction_floor": "0.50"})})
        high = _conviction_gauge(0.71, "p", {"adaptive_params_snapshot":
                                             _json.dumps({"conviction_floor": "0.72"})})
        assert "left:50%" in low and "left:72%" in high, (
            "the threshold marker is not tracking the actual floor")
