"""The governor's pre-call estimate must not be optimistic.

boundary.py's own comment states the rule: "an optimistic estimate is a
hole in the cap". The estimate was a flat 15c, calibrated when
BASE_SEARCHES = 3 was the only search budget there was.

CONJUNCTION_SEARCHES = 10 broke it. Priced against the live rate table:

    searches  input     today (intro)   after 2026-08-31
     3        24k        9.85c   ok      13.27c   ok
    10        24k       16.85c   OVER    20.27c   OVER
    10        60k       24.05c   OVER    31.07c   OVER
    10       120k       36.05c   OVER    49.07c   OVER

Two things move underneath a flat constant, and both are known BEFORE
the call: how many searches this candidate earned (at exactly 1c each,
TRAPS.md) and what the model costs on the day. Sonnet 5's introductory
rate ends 2026-08-31, which raises every one of those figures by ~50%
on a date already in the calendar.

The governor compares the estimate against actual month-to-date spend
rather than reserving it, so being pessimistic costs only at the cap
boundary. Being optimistic costs the cap itself.
"""

from datetime import date
from decimal import Decimal

from catalyst.cost.pricing import WEB_SEARCH_CENTS_PER_QUERY, rates_for
from catalyst.research import boundary, prompts

INTRO = date(2026, 8, 13)
AFTER_INTRO = date(2026, 9, 1)


def _actual_cents(input_tokens, output_tokens, searches, on_date):
    """What tracker.py would price this turn at - the same arithmetic
    the bill is computed with, not a second opinion about it."""
    in_rate, out_rate = rates_for(boundary.RESEARCH_MODEL, on_date)
    return (Decimal(input_tokens) * in_rate / Decimal(1_000_000)
            + Decimal(output_tokens) * out_rate / Decimal(1_000_000)
            + Decimal(searches) * WEB_SEARCH_CENTS_PER_QUERY)


class TestTheEstimateCoversWhatTheCallActuallyCosts:
    def test_the_conjunction_path_is_covered(self):
        """THE HOLE. A 10-search turn costs more than the flat 15c the
        governor used to authorise against."""
        estimate = boundary.exploration_turn_estimate_cents(
            prompts.CONJUNCTION_SEARCHES, on_date=INTRO)
        actual = _actual_cents(60_000, 2048, prompts.CONJUNCTION_SEARCHES,
                               INTRO)
        assert estimate >= actual, (
            f"estimate {estimate}c does not cover a measured-shape "
            f"conjunction turn at {actual}c - the cap can be overshot")

    def test_the_estimate_rises_when_the_intro_pricing_ENDS(self):
        """2026-08-31 is a known date, not a surprise. An estimate that
        does not move on it is optimistic by ~50% from 1 September."""
        before = boundary.exploration_turn_estimate_cents(
            prompts.CONJUNCTION_SEARCHES, on_date=INTRO)
        after = boundary.exploration_turn_estimate_cents(
            prompts.CONJUNCTION_SEARCHES, on_date=AFTER_INTRO)
        assert after > before, (
            f"same estimate ({before}c) either side of the Sonnet 5 intro "
            "price ending - the rate table moved and the estimate did not")

    def test_more_searches_cost_more(self):
        """Web search is 1c a query on top of tokens, and the search
        results are themselves the input tokens. A budget that buys more
        searching must estimate higher."""
        base = boundary.exploration_turn_estimate_cents(
            prompts.BASE_SEARCHES, on_date=INTRO)
        conj = boundary.exploration_turn_estimate_cents(
            prompts.CONJUNCTION_SEARCHES, on_date=INTRO)
        earned = prompts.CONJUNCTION_SEARCHES - prompts.BASE_SEARCHES
        assert conj - base >= earned * WEB_SEARCH_CENTS_PER_QUERY, (
            f"{earned} extra searches cost at least {earned}c in search "
            f"charges alone, but the estimate rose only {conj - base}c")

    def test_the_base_path_is_covered_too(self):
        est = boundary.exploration_turn_estimate_cents(
            prompts.BASE_SEARCHES, on_date=AFTER_INTRO)
        actual = _actual_cents(24_000, 2048, prompts.BASE_SEARCHES,
                               AFTER_INTRO)
        assert est >= actual, f"{est}c does not cover {actual}c"

    def test_the_estimate_is_not_absurdly_pessimistic(self):
        """Pessimism is cheap but not free: the governor refuses a call
        whose estimate would breach the cap, so a wild overestimate
        stops research early for no reason. Bound it at 3x the measured
        shape."""
        est = boundary.exploration_turn_estimate_cents(
            prompts.CONJUNCTION_SEARCHES, on_date=INTRO)
        actual = _actual_cents(60_000, 2048, prompts.CONJUNCTION_SEARCHES,
                               INTRO)
        assert est <= actual * 3, (
            f"estimate {est}c is more than 3x the measured shape "
            f"({actual}c); that refuses affordable research")


class TestTheEstimateIsWhatTheGovernorActuallySees:
    def test_investigate_authorises_against_the_search_aware_estimate(
            self, tmp_path):
        """The function existing is not the same as it being used. This
        reads the estimate recorded on the governor's own decision log
        for a real conjunction investigation."""
        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "g.db"))
        conn.executescript(open("catalyst/storage/schema.sql").read())
        conn.commit()

        from tests.test_search_budget_across_turns import (
            _candidate, _two_source_signals)

        def transport(payload):
            return {
                "id": "m", "model": boundary.RESEARCH_MODEL,
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": "t",
                             "name": "submit_research_view",
                             "input": {"direction": "no_trade",
                                       "conviction": 0.1, "thesis": "t",
                                       "invalidation": "i",
                                       "expected_holding_days": 5,
                                       "priced_in": True,
                                       "priced_in_reasoning": "r"}}],
                "usage": {"input_tokens": 100, "output_tokens": 10},
            }

        boundary.investigate(
            _candidate(),
            boundary.CostContext(conn=conn, governor_profit_share=Decimal("0"),
                                 cycle_id="cyc-est", kind="scheduled"),
            transport, signals=_two_source_signals())

        rows = conn.execute(
            "SELECT estimate_cents FROM cost_governor_events "
            "WHERE cycle_id = 'cyc-est'").fetchall()
        conn.close()
        assert rows, "no governor decision was logged for the research call"
        estimated = max(Decimal(str(r[0])) for r in rows)
        assert estimated > 15, (
            f"the governor authorised against {estimated}c - the flat 15c "
            "constant is still in the live path for a conjunction")
