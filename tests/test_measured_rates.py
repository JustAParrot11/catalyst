"""The rate table learns from the bill, and only ever in the safe direction.

The asymmetry is the whole point and gets the most tests here. A rate
that reads too HIGH makes the bot spend less than the owner allowed; a
rate that reads too LOW lets it spend more. So evidence may raise a rate
by itself and may never lower one - the same two-tier rule the rest of
the system uses for its limits.

No calendar dates anywhere (house rule 6): reconcile_day measures
against datetime.now() and refuses a day that has not closed, so every
date here is relative to the real clock.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.cost.measured_rates import (
    DEADBAND, MAX_STEP, MIN_DAY_CENTS, latest_observation,
    learn_from_closed_day,
)
from catalyst.cost.overrides import rates_for_on
from catalyst.storage import init_db

MODEL = "claude-sonnet-5"
YESTERDAY = datetime.now(timezone.utc).date() - timedelta(days=1)


@pytest.fixture
def db(tmp_path):
    """A database whose rate is FLAT across the window under test.

    The built-in table is a SCHEDULE - Sonnet 5's introductory pricing
    ends 2026-08-31 - so on 1 September the rate in force on the
    measured day and on the day a correction takes effect are different
    numbers, through no fault of anything being tested. Left alone,
    every test here silently becomes a test of the Sonnet price
    calendar, and seven of them failed on exactly one date of the year.

    Pinning a baseline makes these tests about the LOGIC on every date.
    The schedule-boundary behaviour is tested deliberately instead, in
    the two tests that set their own scheduled rise.
    """
    from catalyst.cost.overrides import set_override
    from catalyst.cost.pricing import rates_for

    conn = init_db(str(tmp_path / "rates.db"))
    flat_in, flat_out = rates_for(MODEL, YESTERDAY)
    set_override(conn, MODEL, YESTERDAY - timedelta(days=1),
                 flat_in, flat_out, set_by="test-baseline")
    yield conn
    conn.close()


def measured_overrides(db):
    """Overrides written by the LEARNER, ignoring the fixture baseline."""
    return db.execute(
        "SELECT note FROM pricing_overrides "
        "WHERE set_by LIKE 'measured%'").fetchall()


def in_force(db, day):
    """The rate the ledger would use on `day` right now.

    ALWAYS COMPARED ON THE SAME DATE. The rate table is a SCHEDULE, not
    a constant - Sonnet 5's introductory pricing ends 2026-08-31 - so
    reading "before" on one day and "after" on the next compares two
    different questions and fails on the boundary for a reason that has
    nothing to do with what is being tested (house rule 6; this file's
    own first draft did exactly that and the clock sweep caught it).
    """
    return rates_for_on(db, MODEL, day)


def seed_day(conn, cents, day=YESTERDAY, model=MODEL, n=1):
    """n priced events on `day` totalling `cents`."""
    each = Decimal(str(cents)) / n
    for i in range(n):
        conn.execute(
            "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
            (f"ce-{model}-{i}-{day}", "{}", model, "scheduled", "research",
             str(each), f"{day.isoformat()}T12:00:00+00:00", None))
    conn.commit()


class TestItOnlyEverTightens:
    """The safety property. If these pass and nothing else does, the
    system is still safe; if this class fails, money is at stake."""

    def test_a_bill_higher_than_we_priced_raises_the_rate(self, db):
        """We under-priced, so the real rate is higher than the table
        says. Raising it makes the bot spend LESS - safe, so automatic."""
        seed_day(db, "100")
        effective = YESTERDAY + timedelta(days=1)
        old_in, old_out = in_force(db, effective)

        m = learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("110"))

        assert m is not None and m.applied is True
        assert m.ratio == Decimal("1.1")
        new_in, new_out = in_force(db, effective)
        assert new_in > old_in and new_out > old_out
        assert "LOW" in m.reason and "spend less" in m.reason

    def test_a_bill_lower_than_we_priced_changes_nothing(self, db):
        """THE ONE THAT MATTERS. We over-priced. Correcting the table
        down would let the bot spend MORE, so it is reported and the
        rate is left exactly where it was."""
        seed_day(db, "100")
        days = [YESTERDAY + timedelta(days=n) for n in (1, 30, 365)]
        before = [in_force(db, d) for d in days]

        m = learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("70"))

        assert m is not None
        assert m.applied is False, "a rate must never be LOWERED automatically"
        assert [in_force(db, d) for d in days] == before
        assert "HIGH" in m.reason and "a human decides" in m.reason

    def test_a_correction_can_never_land_below_a_SCHEDULED_rise(self, db):
        """THE HOLE THE CLOCK SWEEP FOUND, as a test.

        The rate table is a schedule, not a constant. A correction
        measured on day D used to be computed from D's rate and written
        effective D+1 - so when a price rise was already scheduled for
        D+1, the override REPLACED it with a lower number and the bot
        under-priced itself into spending more. Exactly the direction
        this module exists to make impossible.

        Built from an override rather than the real Sonnet 5 intro
        expiry, so it tests the invariant on every date rather than on
        one day a year (house rule 6).
        """
        from catalyst.cost.overrides import set_override

        effective = YESTERDAY + timedelta(days=1)
        priced_at, _ = rates_for_on(db, MODEL, YESTERDAY)
        scheduled_in = priced_at * 2          # a big rise, already booked
        set_override(db, MODEL, effective, scheduled_in, priced_at * 10,
                     set_by="a-human", allow_large_change=True)
        seed_day(db, "100")

        # A small correction: 5% - far less than the scheduled doubling.
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("105"))

        assert in_force(db, effective)[0] >= scheduled_in, (
            "the correction overwrote a scheduled price rise with a LOWER "
            "rate - the bot would now under-price and overspend")

    def test_a_scheduled_rise_that_already_covers_it_writes_nothing(self, db):
        """If the schedule has already absorbed the whole discrepancy -
        which is what a day priced at the old rate looks like on the eve
        of a known price change - there is nothing to correct."""
        from catalyst.cost.overrides import set_override

        effective = YESTERDAY + timedelta(days=1)
        priced_at, priced_out = rates_for_on(db, MODEL, YESTERDAY)
        set_override(db, MODEL, effective, priced_at * 2, priced_out * 2,
                     set_by="a-human", allow_large_change=True)
        seed_day(db, "100")
        overrides_before = db.execute(
            "SELECT COUNT(*) FROM pricing_overrides").fetchone()[0]

        m = learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("110"))

        assert m is not None and m.applied is False
        assert "covers it in full" in m.reason or "covers in full" in m.reason
        assert db.execute("SELECT COUNT(*) FROM pricing_overrides"
                          ).fetchone()[0] == overrides_before

    def test_a_learned_rate_cannot_outlive_a_LATER_scheduled_change(self, db):
        """THE SECOND HOLE, and the one that was live on the VPS.

        rates_for_on() resolves "the newest override effective on or
        before that day wins", so an override does not correct the
        schedule - it REPLACES it from its date onward, forever. A rate
        learned on 25 August would still be winning on 1 September, and
        Sonnet 5's introductory pricing would never end as far as the
        ledger knew: ~220 against a real 300, under-priced by 27%.

        The first fix only guarded a change scheduled on the exact day
        the override took effect. This is a change scheduled LATER.
        """
        from catalyst.cost.pricing import SONNET5_INTRO_ENDS, rates_for

        # A day inside the introductory window, wherever "now" is.
        measured = SONNET5_INTRO_ENDS - timedelta(days=7)
        effective = measured + timedelta(days=1)
        after = SONNET5_INTRO_ENDS + timedelta(days=1)
        assert rates_for(MODEL, after) != rates_for(MODEL, measured), (
            "this test is asserting nothing unless the schedule really "
            "changes across the window it uses")

        seed_day(db, "100", day=measured)
        learn_from_closed_day(db, measured, Decimal("100"), Decimal("110"))

        assert in_force(db, effective)[0] > rates_for(MODEL, measured)[0], (
            "the correction did not apply inside the intro window")
        assert in_force(db, after) >= rates_for(MODEL, after), (
            "the learned rate swallowed the scheduled price rise - the bot "
            "would price below the real rate and overspend")

    def test_the_restored_schedule_says_why_it_is_there(self, db):
        """A second override appearing from nowhere is a mystery in the
        price history unless it explains itself."""
        from catalyst.cost.pricing import SONNET5_INTRO_ENDS

        measured = SONNET5_INTRO_ENDS - timedelta(days=7)
        seed_day(db, "100", day=measured)
        learn_from_closed_day(db, measured, Decimal("100"), Decimal("110"))

        rows = db.execute(
            "SELECT note FROM pricing_overrides "
            "WHERE set_by = 'scheduled rate restored'").fetchall()
        assert rows, "the scheduled change was not re-stated at all"
        assert "cannot outlive" in rows[0][0]

    def test_no_override_row_is_written_when_it_would_loosen(self, db):
        """Not merely 'the rate is unchanged' - nothing was recorded that
        a later lookup could pick up."""
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("50"))
        assert measured_overrides(db) == []


class TestItRefusesToLearnFromNoise:
    def test_a_day_too_small_to_price_from_is_left_alone(self, db):
        """On a few cents a single cent of rounding reads as a large
        pricing error. MIN_DAY_CENTS is where that stops."""
        small = MIN_DAY_CENTS - Decimal("1")
        seed_day(db, str(small))
        m = learn_from_closed_day(db, YESTERDAY, small, small * 2)
        assert m is not None and m.applied is False
        assert "too small" in m.reason

    def test_agreement_inside_the_deadband_moves_nothing(self, db):
        seed_day(db, "100")
        effective = YESTERDAY + timedelta(days=1)
        before = in_force(db, effective)
        m = learn_from_closed_day(db, YESTERDAY, Decimal("100"),
                                  Decimal("100") * (1 + DEADBAND / 2))
        assert m is not None and m.applied is False
        assert "agreed within" in m.reason
        assert in_force(db, effective) == before

    def test_two_models_on_one_day_is_not_a_rate(self, db):
        """The Cost API returns the day's money without a per-model
        split, so a two-model day gives a BLEND. Attributing it to either
        model's rate would be arithmetic dressed up as measurement."""
        seed_day(db, "100", model=MODEL)
        seed_day(db, "100", model="claude-haiku-4-5")
        assert learn_from_closed_day(
            db, YESTERDAY, Decimal("200"), Decimal("400")) is None
        assert measured_overrides(db) == []

    def test_a_day_with_no_spend_teaches_nothing(self, db):
        assert learn_from_closed_day(
            db, YESTERDAY, Decimal("0"), Decimal("0")) is None

    def test_an_empty_bill_is_not_evidence_the_rate_is_zero(self, db):
        """A zero from the API is the reconciliation's problem to shout
        about, not a reason to reprice anything."""
        seed_day(db, "100")
        assert learn_from_closed_day(
            db, YESTERDAY, Decimal("100"), Decimal("0")) is None


class TestTheStepIsBounded:
    def test_an_enormous_discrepancy_still_moves_only_one_step(self, db):
        """BUILD-BRIEF: 'no parameter moves more than a small fraction
        per adjustment, however emphatic the evidence.' A billing
        correction or a credit must not be able to jump the table."""
        seed_day(db, "100")
        priced_at, _ = rates_for_on(db, MODEL, YESTERDAY)
        effective = YESTERDAY + timedelta(days=1)
        scheduled, _ = in_force(db, effective)

        m = learn_from_closed_day(db, YESTERDAY, Decimal("100"),
                                  Decimal("400"))     # 4x

        assert m is not None and m.applied is True
        new_in, _ = in_force(db, effective)
        ceiling = max((priced_at * (1 + MAX_STEP)).quantize(Decimal("1")),
                      scheduled)
        assert new_in <= ceiling
        assert "capped" in m.reason

    def test_repeated_days_converge_rather_than_jump(self, db):
        """A real rate change arrives over a few days, each one on the
        record, instead of in one unexplained leap."""
        old_in, _ = rates_for_on(db, MODEL, YESTERDAY)
        seen = []
        for i in (3, 2, 1):
            day = datetime.now(timezone.utc).date() - timedelta(days=i)
            seed_day(db, "100", day=day)
            learn_from_closed_day(db, day, Decimal("100"), Decimal("400"))
            seen.append(rates_for_on(db, MODEL, day + timedelta(days=1))[0])
        assert seen == sorted(seen), "each step moves the same direction"
        assert seen[-1] > old_in * 1000 / 1000
        assert len(set(seen)) > 1, "it kept converging, not stuck"


class TestItIsWrittenDownEvenWhenNothingHappens:
    def test_a_quiet_agreement_is_still_recorded(self, db):
        """'Checked against the real bill and agreed' is the evidence
        that replaces the 90-day calendar guess. It only exists if the
        boring days are written down too."""
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("100"))
        obs = latest_observation(db)
        assert obs is not None
        assert obs["applied"] is False
        assert obs["target_date"] == YESTERDAY.isoformat()
        assert obs["billed_total_cents"] == "100"

    def test_the_applied_change_carries_its_evidence(self, db):
        """BUILD-BRIEF: every adjustment logged with the evidence that
        caused it - the old value, the new one, and what it rested on."""
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("115"))
        rows = measured_overrides(db)
        assert len(rows) == 1, rows
        note = rows[0][0]
        assert "115" in note and "100" in note
        obs = latest_observation(db)
        assert obs["applied"] is True and obs["reason"] == note


class TestItCannotBreakTheThingItRunsInside:
    def test_a_broken_table_does_not_raise(self, db):
        """It runs inside reconcile_day. A fault here must never take
        down the check that the ledger is honest."""
        seed_day(db, "100")
        db.execute("DROP TABLE measured_rate_observations")
        db.commit()
        assert learn_from_closed_day(
            db, YESTERDAY, Decimal("100"), Decimal("200")) is None

    @pytest.mark.parametrize("local,billed", [
        ("nonsense", "100"), ("100", "nonsense"),
        (Decimal("NaN"), Decimal("100")), (Decimal("Infinity"), Decimal("1")),
        (Decimal("-5"), Decimal("100")),
    ])
    def test_unreadable_figures_leave_the_table_alone(self, db, local, billed):
        seed_day(db, "100")
        effective = YESTERDAY + timedelta(days=1)
        before = in_force(db, effective)
        learn_from_closed_day(db, YESTERDAY, local, billed)
        assert in_force(db, effective) == before


class TestItIsActuallyWiredIntoTheReconciliation:
    """A learner nothing calls is a file, not a feature."""

    def _reconcile(self, db, billed):
        from catalyst.cost.tracker import CostApiPage, reconcile_day

        return reconcile_day(
            YESTERDAY, db,
            lambda d: CostApiPage(records=[{"amount": billed}], has_more=False,
                                  raw_response={"data": [{"amount": billed}]}))

    def test_reconciling_a_closed_day_learns_the_rate(self, db):
        seed_day(db, "100")
        old_in, _ = rates_for_on(db, MODEL, YESTERDAY)
        self._reconcile(db, "112")
        new_in, _ = rates_for_on(db, MODEL, YESTERDAY + timedelta(days=1))
        assert new_in > old_in, "reconcile_day never called the learner"
        obs = latest_observation(db)
        assert obs is not None, "reconcile_day never called the learner"
        assert obs["applied"] is True

    def test_a_paused_day_still_learns(self, db):
        """A large discrepancy is exactly when a rate is wrong. Fixing it
        is what stops the same pause recurring tomorrow - the pause still
        stands for the human either way."""
        seed_day(db, "100")
        result = self._reconcile(db, "400")
        assert result.action_taken == "scheduled_paused"
        obs = latest_observation(db)
        assert obs is not None, "a paused day skipped the learner"
        assert obs["applied"] is True

    def test_learning_never_changes_the_reconciliation_verdict(self, db):
        """The ledger check is the thing that guards the money. Learning
        a rate rides alongside it and must not soften it."""
        seed_day(db, "100")
        result = self._reconcile(db, "400")
        assert result.action_taken == "scheduled_paused"
        assert result.local_total_cents == Decimal("100")
        assert result.cost_api_total_cents == Decimal("400")
        assert result.discrepancy_cents == Decimal("300")


class TestThePageSaysWhetherTheRateWasMeasured:
    """'Every number says where it came from' (BUILD-BRIEF). A priced
    figure whose rate was checked against the real bill and one whose
    rate is a hand-typed guess are different claims, and the page has to
    tell them apart."""

    def _cost_page(self, db_path):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        return panels.cost_panel(Db(db_path), p="cost")

    def test_a_checked_rate_says_so_with_both_numbers(self, db, tmp_path):
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("100"))
        html = self._cost_page(db.execute(
            "PRAGMA database_list").fetchone()[2])
        assert "Checked against the real bill" in html
        assert YESTERDAY.isoformat() in html
        assert "100c locally" in html and "100c billed" in html
        assert "they agreed" in html

    def test_a_raised_rate_says_it_was_raised(self, db):
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("120"))
        html = self._cost_page(db.execute(
            "PRAGMA database_list").fetchone()[2])
        assert "the table was raised to match" in html

    def test_never_checked_is_said_plainly_not_left_blank(self, db):
        """A zero is never left unexplained (house rule 3). 'No measured
        rate' and 'the check is broken' must not look identical."""
        html = self._cost_page(db.execute(
            "PRAGMA database_list").fetchone()[2])
        assert "has not yet been checked against a real bill" in html

    def test_a_database_without_the_table_still_renders_the_page(self, db):
        """An install upgraded from before this table existed loses one
        line, never the page that reports whether the bot can run."""
        db.execute("DROP TABLE measured_rate_observations")
        db.commit()
        html = self._cost_page(db.execute(
            "PRAGMA database_list").fetchone()[2])
        assert "has not yet been checked" in html


class TestTheLearnedRateActuallyReachesTheLedger:
    def test_the_next_day_prices_at_the_corrected_rate(self, db):
        """End to end: the point of learning a rate is that the next
        call is priced by it. If this passes, the governor is spending
        against a measured number rather than an asserted one."""
        seed_day(db, "100")
        effective = YESTERDAY + timedelta(days=1)
        asserted = in_force(db, effective)          # the table's own answer

        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("120"))

        measured = in_force(db, effective)          # same day, after learning
        assert measured[0] > asserted[0], (
            "the ledger is still pricing from the hand-written table")

    def test_history_keeps_the_rate_that_was_in_force_when_it_was_priced(self, db):
        """The correction applies FORWARD. Repricing yesterday at a rate
        discovered today would rewrite a bill that was already settled."""
        seed_day(db, "100")
        before = rates_for_on(db, MODEL, YESTERDAY)
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("120"))
        assert rates_for_on(db, MODEL, YESTERDAY) == before
