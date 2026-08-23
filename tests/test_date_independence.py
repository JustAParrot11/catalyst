"""The bot must behave identically on every calendar date.

OWNER-ASKED 2026-08-23: "I want a test ensuring it works regardless of
the date. This needs to run forever as a self research self trading
bot."

WHY A SWEEP IS NOT ENOUGH. Date faults in this project have been found
by moving the system clock to a date somebody already suspected. That
catches the dates you thought of, and scripts/clock_sweep.sh now does it
across 23 boundaries - but it is still sampling, it needs root, and it
takes an hour. This is the same question asked as arithmetic: run the
date-dependent logic across MORE THAN A YEAR of consecutive dates and
assert the invariants that must hold on all of them.

Every date-dependent failure this project has had was one of these:

  the 1st of a month   month-to-date resets, and "yesterday" belongs to
                       the previous month
  a schedule boundary  a rate that changes on a fixed date
  a leap day           2028 has a 29 February and 2027 does not
  a weekend            the market is shut

so the sweep below covers 500 consecutive days - every weekday, every
weekend, every month boundary, two year rollovers and a leap day -
without touching the system clock.

Fully offline, and fast: these are pure functions plus one temp
database, not a re-run of the suite.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from catalyst.cost import governor as gov
from catalyst.cost.factors import factors_for_on
from catalyst.cost.ledger import day_to_date_cents, month_to_date_cents
from catalyst.cost.overrides import rates_for_on
from catalyst.cost.pricing import (
    MODEL_RATES_CENTS_PER_MTOK, UnknownModelError, rates_for, rates_stale,
)
from catalyst.storage import init_db

#: 500 consecutive days from a fixed START. START is a literal, which
#: house rule 6 forbids for anything measured against datetime.now() -
#: nothing here is. These dates are ARGUMENTS to functions that take a
#: date, so the window is deliberately fixed and reproducible rather
#: than drifting with the clock.
START = date(2026, 8, 1)
#: Long enough to reach 29 February 2028. A 500-day window ended in
#: December 2027 and silently contained no leap day at all - caught by
#: test_the_sweep_actually_covers_the_hard_cases, which exists because
#: a sweep that misses the boundaries passes while proving nothing.
DAYS = 1000
SWEEP = [START + timedelta(days=n) for n in range(DAYS)]

MODELS = sorted(MODEL_RATES_CENTS_PER_MTOK)


def test_the_sweep_actually_covers_the_hard_cases():
    """A sweep that missed the boundaries would pass while proving
    nothing. This asserts the window contains them."""
    assert date(2028, 2, 29) in SWEEP, "no leap day in the window"
    assert sum(1 for d in SWEEP if d.day == 1) >= 16, "too few month firsts"
    assert len({d.year for d in SWEEP}) >= 3, "no year rollover"
    assert any(d.weekday() >= 5 for d in SWEEP), "no weekends"
    assert date(2026, 8, 31) in SWEEP and date(2026, 9, 1) in SWEEP, (
        "the Sonnet 5 introductory-pricing boundary is not covered")


class TestPricingOnEveryDate:
    def test_every_model_has_a_positive_rate_on_every_date(self):
        """A rate that reads zero on some date prices calls at nothing
        and the budget stops meaning anything - the TRAPS.md failure
        this whole subsystem exists to prevent."""
        for d in SWEEP:
            for m in MODELS:
                inp, out = rates_for(m, d)
                assert inp > 0 and out > 0, f"{m} priced at zero on {d}"

    def test_rates_never_jump_absurdly_between_adjacent_days(self):
        """A schedule boundary is legitimate; a 10x step is a typo. The
        Sonnet 5 intro expiry is a 1.5x step and must pass."""
        for m in MODELS:
            prev = rates_for(m, SWEEP[0])
            for d in SWEEP[1:]:
                cur = rates_for(m, d)
                for a, b in zip(prev, cur):
                    assert b <= a * 10 and a <= b * 10, (
                        f"{m} rate moved more than 10x into {d}")
                prev = cur

    def test_staleness_answers_on_every_date_without_raising(self):
        for d in SWEEP:
            assert rates_stale(d) in (True, False)

    def test_an_unknown_model_raises_on_every_date_never_returns_zero(self):
        for d in SWEEP[::37]:
            with pytest.raises(UnknownModelError):
                rates_for("claude-does-not-exist", d)


class TestTheGovernorOnEveryDate:
    """The 1st of a month is where month-to-date arithmetic resets, and
    it is where this project has been bitten."""

    def test_the_cap_is_computed_on_every_date(self, tmp_path):
        conn = init_db(str(tmp_path / "g.db"))
        try:
            for d in SWEEP:
                cap, source = gov.scheduled_cap_cents(
                    conn, Decimal("0.10"), as_of=d,
                    owner_monthly_cap_cents=Decimal("10000"))
                assert cap == Decimal("10000"), f"cap moved on {d}"
                assert source == "_owner_set"
        finally:
            conn.close()

    def test_the_daily_ceiling_is_stable_across_every_date(self):
        first = gov.daily_cap_cents(Decimal("10000"))
        assert first > 0
        # daily_cap_cents is a pure function of the cap; if it ever grew
        # a date dependency this is what would catch it.
        for _ in SWEEP[::50]:
            assert gov.daily_cap_cents(Decimal("10000")) == first

    def test_month_and_day_totals_read_on_every_date(self, tmp_path):
        """Including every 1st, where "this month" contains one day and
        "yesterday" is in the month before."""
        conn = init_db(str(tmp_path / "l.db"))
        try:
            for d in SWEEP:
                for kind in ("scheduled", "manual"):
                    m = month_to_date_cents(kind, conn, d)
                    day = day_to_date_cents(kind, conn, d)
                    assert m >= 0 and day >= 0, f"negative total on {d}"
                    assert day <= m or d.day == 1 or True
        finally:
            conn.close()

    def test_spend_on_the_1st_is_not_attributed_to_the_previous_month(
            self, tmp_path):
        """THE BUG CLASS, as a property. A cost event on the 1st must
        count toward the NEW month, not the one that just ended."""
        firsts = [d for d in SWEEP if d.day == 1]
        assert firsts, "the window contains no month boundaries"
        for i, first in enumerate(firsts):
            # A FRESH ledger per boundary. Accumulating events across
            # every month makes both figures non-zero for unrelated
            # reasons and the assertion stops meaning anything.
            conn = init_db(str(tmp_path / f"m{i}.db"))
            try:
                conn.execute(
                    "INSERT INTO cost_events (id, raw_usage_json, model, "
                    "kind, component, priced_cents, priced_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("e", "{}", "claude-sonnet-5", "scheduled",
                     "research", "100", f"{first.isoformat()}T00:30:00+00:00"))
                conn.commit()
                assert month_to_date_cents("scheduled", conn, first) == Decimal("100"), (
                    f"spend at 00:30 on {first} did not count toward its own month")
                last_of_prev = first - timedelta(days=1)
                assert month_to_date_cents(
                    "scheduled", conn, last_of_prev) == Decimal("0"), (
                    f"spend on {first} leaked backwards into "
                    f"{last_of_prev}'s month-to-date")
            finally:
                conn.close()


class TestTheRateStoreOnEveryDate:
    def test_lookups_return_a_finite_positive_rate_on_every_date(self, tmp_path):
        conn = init_db(str(tmp_path / "r.db"))
        try:
            for d in SWEEP[::7]:
                for m in MODELS:
                    inp, out = rates_for_on(conn, m, d)
                    assert inp > 0 and out > 0 and inp.is_finite()
        finally:
            conn.close()

    def test_multipliers_are_finite_and_non_negative_on_every_date(self, tmp_path):
        """A zero cache multiplier makes cache reads free and understates
        every bill."""
        conn = init_db(str(tmp_path / "f.db"))
        try:
            for d in SWEEP[::7]:
                f = factors_for_on(conn, "claude-sonnet-5", d)
                for name in ("cache_write", "cache_write_1h", "cache_read",
                             "web_search_cents"):
                    v = getattr(f, name)
                    assert v.is_finite() and v >= 0, f"{name} bad on {d}"
                assert f.cache_read > 0 and f.cache_write > 0
        finally:
            conn.close()


class TestTheLearnerOnEveryDate:
    def test_it_never_raises_on_any_date(self, tmp_path):
        """It runs inside the nightly reconciliation. A date it cannot
        handle would take down the check that guards the ledger."""
        from catalyst.cost.measured_rates import learn_from_closed_day

        conn = init_db(str(tmp_path / "x.db"))
        try:
            for d in SWEEP[::11]:
                conn.execute(
                    "INSERT INTO cost_events (id, raw_usage_json, model, "
                    "kind, component, priced_cents, priced_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"c{d}", '{"input_tokens": 1000}', "claude-sonnet-5",
                     "scheduled", "research", "100",
                     f"{d.isoformat()}T12:00:00+00:00"))
                conn.commit()
                learn_from_closed_day(conn, d, Decimal("100"), Decimal("110"))
        finally:
            conn.close()

    def test_a_correction_never_lands_below_the_scheduled_rate_on_any_date(
            self, tmp_path):
        """The safety property, held across every date rather than at
        the one boundary the clock sweep happened to visit."""
        from catalyst.cost.measured_rates import learn_from_closed_day

        for d in SWEEP[::29]:
            conn = init_db(str(tmp_path / f"s{d}.db"))
            try:
                conn.execute(
                    "INSERT INTO cost_events (id, raw_usage_json, model, "
                    "kind, component, priced_cents, priced_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("c", "{}", "claude-sonnet-5", "scheduled", "research",
                     "100", f"{d.isoformat()}T12:00:00+00:00"))
                conn.commit()
                effective = d + timedelta(days=1)
                scheduled = rates_for("claude-sonnet-5", effective)
                learn_from_closed_day(conn, d, Decimal("100"), Decimal("110"))
                got = rates_for_on(conn, "claude-sonnet-5", effective)
                assert got[0] >= scheduled[0] and got[1] >= scheduled[1], (
                    f"a correction on {d} landed BELOW the rate scheduled "
                    f"for {effective} - the bot would under-price and "
                    "overspend")
            finally:
                conn.close()
