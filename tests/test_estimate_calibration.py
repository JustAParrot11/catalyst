"""INPUT_TOKENS_PER_SEARCH must be measured, not asserted.

CLAUDE.md: "Thresholds are measured, not asserted ... Conviction floors,
gap assumptions and stop widths start as estimates and must adapt on
closed, scored outcomes."

12,000 tokens per search was derived from ONE live call: 24k input
carrying 2 searches against a ~2k prompt. A sample of one, rounded up
and then relied on to keep a spend cap honest. The raw usage object of
every turn is already stored verbatim (TRAPS.md), so the real figure is
sitting in the database waiting to be read.

THE DIRECTION MATTERS AND IT IS NOT SYMMETRIC.

Raising the estimate is always safe: authorize() compares it against
ACTUAL month-to-date spend rather than reserving it, so pessimism costs
only at the cap boundary. Lowering it re-opens the exact hole this
branch just closed - and a quiet fortnight would be all the "evidence"
an auto-lowering estimate needed.

So calibration only ever RAISES. Lowering the seed is a human decision,
and the observed figure is surfaced so the human can make it on data.
That is BUILD-BRIEF's asymmetric-speed rule ("tighten quickly on
evidence of harm; loosen slowly on evidence of over-caution") taken to
its limit for a parameter guarding a spend cap.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from catalyst.research import boundary


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "cal.db"))
    c.executescript(open("catalyst/storage/schema.sql").read())
    c.commit()
    return c


def _record(conn, input_tokens, searches, n=1, component="research"):
    """n turns that each sent `input_tokens` and billed `searches`."""
    now = datetime.now(timezone.utc).isoformat()
    for i in range(n):
        usage = {"input_tokens": input_tokens, "output_tokens": 500,
                 "server_tool_use": {"web_search_requests": searches}}
        conn.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at, api_call_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"ce-{component}-{input_tokens}-{searches}-{i}",
             json.dumps(usage), "claude-sonnet-5", "scheduled", component,
             "10", now, f"call-{i}"))
    conn.commit()


class TestTheFigureIsReadFromRecordedUsage:
    def test_too_small_a_sample_does_not_move_it(self, conn):
        """BUILD-BRIEF: 'A minimum sample before anything moves ...
        Adapting on four trades is fitting noise.'"""
        _record(conn, input_tokens=200_000, searches=1, n=2)
        assert boundary.input_tokens_per_search(conn) == \
            boundary.INPUT_TOKENS_PER_SEARCH

    def test_evidence_that_searches_cost_MORE_raises_it(self, conn):
        """THE POINT. Real turns showing 30k of input per search must
        not keep being estimated at 12k."""
        _record(conn, input_tokens=30_000, searches=1,
                n=boundary.MIN_CALIBRATION_SAMPLE)
        calibrated = boundary.input_tokens_per_search(conn)
        assert calibrated > boundary.INPUT_TOKENS_PER_SEARCH
        assert calibrated >= 25_000, calibrated

    def test_evidence_that_searches_cost_LESS_does_NOT_lower_it(self, conn):
        """An estimate that lowers itself re-opens the hole in the cap,
        and a quiet fortnight is all the evidence it would need. Lowering
        the seed is a human decision."""
        _record(conn, input_tokens=3_000, searches=1,
                n=boundary.MIN_CALIBRATION_SAMPLE * 4)
        assert boundary.input_tokens_per_search(conn) == \
            boundary.INPUT_TOKENS_PER_SEARCH

    def test_turns_that_did_not_search_are_ignored(self, conn):
        """A turn with zero searches says nothing about the cost of a
        search, and dividing by it says nothing at all."""
        _record(conn, input_tokens=500_000, searches=0,
                n=boundary.MIN_CALIBRATION_SAMPLE * 2)
        assert boundary.input_tokens_per_search(conn) == \
            boundary.INPUT_TOKENS_PER_SEARCH

    def test_it_uses_a_HIGH_percentile_not_the_mean(self, conn):
        """A mean is dragged down by cheap turns, and the estimate exists
        to cover the expensive ones.

        The numbers are chosen so MEAN AND PERCENTILE DISAGREE, which the
        first version of this test failed to do - 8 cheap turns and 4
        expensive ones put both answers above the assertion, so swapping
        the percentile for a mean passed it. A test that cannot fail is
        not a test (house rule 4).

          8 turns at 14k -> 12k of search tokens each
          4 turns at 60k -> 58k each
          mean = 27,333        p75 = 58,000
        """
        _record(conn, input_tokens=14_000, searches=1,
                n=boundary.MIN_CALIBRATION_SAMPLE)
        _record(conn, input_tokens=60_000, searches=1,
                n=max(3, boundary.MIN_CALIBRATION_SAMPLE // 2))
        calibrated = boundary.input_tokens_per_search(conn)
        assert calibrated >= 50_000, (
            f"{calibrated} is near the MEAN (~27,333), not the 75th "
            "percentile (~58,000) - the expensive turns were averaged "
            "away, and they are the ones the estimate exists to cover")

    def test_a_multi_search_turn_is_divided_by_its_searches(self, conn):
        """36k of input across 3 searches is 12k a search, not 36k."""
        _record(conn, input_tokens=36_000, searches=3,
                n=boundary.MIN_CALIBRATION_SAMPLE)
        # 36k less the ~2k prompt, over 3 searches, is ~11.3k - BELOW the
        # 12k seed, so the never-lower rule holds it at the seed.
        assert boundary.input_tokens_per_search(conn) == \
            boundary.INPUT_TOKENS_PER_SEARCH

    def test_unreadable_usage_never_raises(self, conn):
        """Calibration is context, never a gate. A malformed row must
        cost the estimate its evidence, never cost the call."""
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at, api_call_id) "
            "VALUES ('bad','not json at all','claude-sonnet-5','scheduled',"
            "'research','1',?,'c')", (now,))
        conn.commit()
        assert boundary.input_tokens_per_search(conn) == \
            boundary.INPUT_TOKENS_PER_SEARCH

    def test_no_connection_falls_back_to_the_seed(self, conn):
        assert boundary.input_tokens_per_search(None) == \
            boundary.INPUT_TOKENS_PER_SEARCH


class TestTheEstimateActuallyUsesIt:
    def test_the_exploration_estimate_rises_with_measured_evidence(self, conn):
        before = boundary.exploration_turn_estimate_cents(3)
        _record(conn, input_tokens=40_000, searches=1,
                n=boundary.MIN_CALIBRATION_SAMPLE)
        after = boundary.exploration_turn_estimate_cents(3, conn=conn)
        assert after > before, (
            f"{before}c -> {after}c: measured turns showed searches cost "
            "far more than the seed and the estimate did not move")

    def test_the_extraction_estimate_rises_too(self, conn):
        """Extraction re-reads the same context, so the same evidence
        applies to it."""
        before = boundary.extraction_turn_estimate_cents(3)
        _record(conn, input_tokens=40_000, searches=1,
                n=boundary.MIN_CALIBRATION_SAMPLE)
        after = boundary.extraction_turn_estimate_cents(3, conn=conn)
        assert after > before

    def test_the_governor_SEES_the_calibrated_estimate(self, conn):
        """The function existing is not the same as the live path using
        it. This reads the governor's own decision log."""
        from decimal import Decimal

        from tests.test_search_budget_across_turns import (
            _candidate, _two_source_signals)

        _record(conn, input_tokens=60_000, searches=1,
                n=boundary.MIN_CALIBRATION_SAMPLE)
        uncalibrated = boundary.exploration_turn_estimate_cents(
            len({s.source for s in _two_source_signals()}) and 10)

        def transport(payload):
            return {"id": "m", "model": boundary.RESEARCH_MODEL,
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "t",
                                 "name": "submit_research_view",
                                 "input": {"direction": "no_trade",
                                           "conviction": 0.1, "thesis": "t",
                                           "invalidation": "i",
                                           "expected_holding_days": 5,
                                           "priced_in": True,
                                           "priced_in_reasoning": "r"}}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}

        boundary.investigate(
            _candidate(),
            boundary.CostContext(conn=conn, governor_profit_share=Decimal("0"),
                                 cycle_id="cyc-cal", kind="scheduled"),
            transport, signals=_two_source_signals())

        rows = conn.execute(
            "SELECT estimate_cents FROM cost_governor_events "
            "WHERE cycle_id = 'cyc-cal'").fetchall()
        assert rows
        authorised = max(Decimal(str(r[0])) for r in rows)
        assert authorised > uncalibrated, (
            f"governor authorised {authorised}c against an uncalibrated "
            f"{uncalibrated}c - the live path is not reading the measured "
            "figure")
