"""Raising the budget must actually raise what the bot does.

OWNER-REPORTED, with a diagnostic bundle behind it. On 2026-08-17 the
funnel recorded 60 research rows for the day:

    51  not_attempted: deferred_max_research_per_cycle
     5  not_attempted: market_closed
     3  not_attempted: no_market_quote
     1  actually called the API

Fifty-one candidates were never looked at, while the owner's monthly cap
sat at $100 and the month-to-date spend was $11.03. Their words: "my new
monthly limit is 100, ensure the bot doesnt still try stick to lower
standards and hinder its effectiveness."

TWO NUMBERS WERE SIZED FOR A TWENTY-POUND MONTH and stayed put when the
budget moved:

    DAILY_CAP_CENTS = 500        a flat $5/day rate ceiling
    MAX_RESEARCH_PER_CYCLE = 3   documented in its own comment as a
                                 BELT, with the governor as the real cap

A belt that does not move when the budget quadruples is just the old
budget wearing the new one's name. Both now derive from the cap in
force, which is the same answer this project already gave for the
monthly cap itself: one source of truth, never a second constant to
remember.

THE SAFETY ARGUMENT, which is what most of this file tests. Raising
these does not authorise a single cent. Every call still passes the
governor, which refuses on the monthly cap, the daily rate ceiling, an
unacknowledged discrepancy or unpriced rows. These numbers only decide
how many candidates are CONSIDERED before those gates. The tests below
pin that: the derived values never fall below the originals, and the
governor still refuses when the money is gone however wide the belt.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from catalyst.cost.governor import (
    BUDGET_MONTH_DAYS, DAILY_CAP_CENTS, daily_cap_cents,
)
from catalyst.orchestrator.cycle import (
    MAX_RESEARCH_PER_CYCLE, MAX_RESEARCH_PER_CYCLE_CEILING, research_per_cycle,
)

OWNERS_CAP = Decimal("10000")          # $100/month, as set 2026-08-17


class TestTheRateCeilingFollowsTheBudget:
    def test_the_owners_new_cap_loosens_it(self):
        assert daily_cap_cents(OWNERS_CAP) == Decimal("1000")   # $10/day

    @pytest.mark.parametrize("monthly", [None, 0, -100, "nonsense",
                                         float("nan"), float("inf")])
    def test_anything_unusable_falls_back_to_the_original(self, monthly):
        """A missing, zero, negative or unparseable cap must not become
        an unbounded rate. It reads as "no information", never as "no
        limit"."""
        assert daily_cap_cents(monthly) == DAILY_CAP_CENTS

    @pytest.mark.parametrize("monthly", ["1", "100", "500", "2000", "5000"])
    def test_a_SMALL_budget_never_tightens_below_the_agreed_floor(
            self, monthly):
        """The owner set $5/day on 2026-08-14. Deriving must only ever
        loosen from there, or lowering a monthly cap would silently
        strangle the bot below what was already agreed."""
        assert daily_cap_cents(Decimal(monthly)) >= DAILY_CAP_CENTS

    def test_it_is_three_days_of_even_spending(self):
        """The stated purpose of a rate ceiling is that a runaway stops
        within a day rather than a month. Three days' worth keeps that
        true at any budget while leaving room for lumpy arrivals."""
        big = Decimal("60000")            # $600/month
        expected = (big / BUDGET_MONTH_DAYS) * 3
        assert abs(daily_cap_cents(big) - expected) <= 1

    def test_it_lands_on_a_WHOLE_cent(self):
        """Decimal division is exact-but-repeating: 10000/30*3 is
        999.9999999999999999999999999, not 1000. A ceiling a hundredth
        of a cent under the intended figure is how an unexplainable
        off-by-one refusal gets born."""
        for cents in ("10000", "8000", "3300", "7"):
            got = daily_cap_cents(Decimal(cents))
            assert got == got.to_integral_value(), got

    def test_it_rises_monotonically_with_the_cap(self):
        caps = [Decimal(c) for c in ("500", "2000", "8000", "10000", "50000")]
        seen = [daily_cap_cents(c) for c in caps]
        assert seen == sorted(seen)


class TestTheResearchBeltFollowsTheBudget:
    def test_the_owners_cap_widens_it(self):
        assert research_per_cycle(OWNERS_CAP) > MAX_RESEARCH_PER_CYCLE

    @pytest.mark.parametrize("monthly", [None, 0, -1, "nonsense",
                                         float("nan"), float("inf")])
    def test_anything_unusable_keeps_the_original(self, monthly):
        assert research_per_cycle(monthly) == MAX_RESEARCH_PER_CYCLE

    @pytest.mark.parametrize("monthly", ["100", "500", "2000", "5000"])
    def test_a_small_budget_is_NOT_widened(self, monthly):
        """The bug in the first attempt at this. Deriving from the rate
        ceiling - which has a $5 floor - handed a $5 MONTHLY budget ten
        candidates a cycle, i.e. proposed spending the whole month in
        one cycle. The governor would have refused, but the belt whose
        job is to prevent that shape should not be the thing proposing
        it."""
        assert research_per_cycle(Decimal(monthly)) == MAX_RESEARCH_PER_CYCLE

    def test_it_is_capped_however_large_the_budget(self):
        """A cycle is 900 seconds. Spending a day's research in the
        first cycle after the open is the failure the daily ceiling
        exists to prevent, and the belt must not reintroduce it."""
        assert research_per_cycle(Decimal("1000000")) == \
            MAX_RESEARCH_PER_CYCLE_CEILING

    def test_it_never_returns_less_than_the_original(self):
        for cents in ("1", "50", "500", "10000", "999999"):
            assert research_per_cycle(Decimal(cents)) >= MAX_RESEARCH_PER_CYCLE


class TestWideningTheBeltAuthorisesNoSpend:
    """The load-bearing half. These numbers decide how many candidates
    are CONSIDERED; the governor decides what is paid for."""

    def _governor(self, tmp_path, spent_cents, monthly_cap):
        import sqlite3
        import uuid

        from catalyst.cost.governor import CostEstimate, authorize
        from catalyst.storage import init_db

        conn = init_db(str(tmp_path / "g.db"))
        now = datetime.now(timezone.utc)
        if spent_cents:
            conn.execute(
                "INSERT INTO cost_events (id,raw_usage_json,model,kind,"
                "component,priced_cents,priced_at,api_call_id) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "{}", "claude-sonnet-5", "scheduled",
                 "research", str(spent_cents), now.isoformat(), "a1"))
            conn.commit()
        est = CostEstimate(estimated_cents=Decimal("50"),
                           basis="measured from the owner's 2026-08-17 bundle",
                           kind="scheduled", component="research")
        return conn, est, authorize

    def test_the_monthly_cap_still_refuses_when_the_money_is_gone(
            self, tmp_path):
        conn, est, authorize = self._governor(tmp_path, "9990", OWNERS_CAP)
        try:
            d = authorize(est, conn, Decimal("0"),
                          owner_monthly_cap_cents=OWNERS_CAP)
        finally:
            conn.close()
        assert not d.authorized, (
            "the budget is spent and the governor allowed another call")
        assert "cap_exceeded" in d.reason

    def test_the_derived_daily_ceiling_still_bites(self, tmp_path):
        """$100/month derives a $10/day ceiling. Spend $9.99 today and
        the next call must be refused, even though the MONTH has plenty
        left - that is the whole point of a rate ceiling."""
        conn, est, authorize = self._governor(tmp_path, "999", OWNERS_CAP)
        try:
            d = authorize(est, conn, Decimal("0"),
                          owner_monthly_cap_cents=OWNERS_CAP)
        finally:
            conn.close()
        assert not d.authorized
        assert d.reason == "daily_cap_exceeded"
        assert d.cap_cents == Decimal("1000"), (
            f"the refusal was judged against {d.cap_cents}, not the derived "
            "daily ceiling - the audit row would name the wrong bound")

    def test_a_call_inside_both_bounds_is_allowed(self, tmp_path):
        conn, est, authorize = self._governor(tmp_path, "100", OWNERS_CAP)
        try:
            d = authorize(est, conn, Decimal("0"),
                          owner_monthly_cap_cents=OWNERS_CAP)
        finally:
            conn.close()
        assert d.authorized, d.reason


class TestTheCycleUsesTheDerivedNumber:
    def test_the_default_scales_but_an_explicit_argument_wins(self):
        """A caller that named a number meant it. Only the DEFAULT is
        replaced, or a test pinning max_research=1 would silently get
        twelve."""
        import inspect

        from catalyst.orchestrator import cycle

        src = inspect.getsource(cycle.run_cycle)
        assert "if max_research == MAX_RESEARCH_PER_CYCLE:" in src
        assert "research_per_cycle(owner_monthly_cap_cents)" in src

    def test_the_worst_case_call_cost_is_used_not_the_average(self):
        """Deriving a count from the AVERAGE cost over-reaches on a day
        of expensive calls. The measured worst case was 45c; the
        constant must sit at or above it."""
        from catalyst.orchestrator.cycle import TYPICAL_RESEARCH_CALL_CENTS

        assert TYPICAL_RESEARCH_CALL_CENTS >= 45
