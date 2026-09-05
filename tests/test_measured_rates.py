"""The rate table is whatever the bill divides to, in either direction.

OWNER-SET 2026-09-05: "stop locally calculating the new price full stop
trust the admin API".

WHAT THIS FILE USED TO ASSERT, and why it changed. The old policy was
asymmetric: a bill HIGHER than we priced raised the rate at up to +25%
on one day's evidence; a bill LOWER left it alone until three days
agreed, and then moved at most -10%. The reasoning was the two-tier
rule the rest of the system runs on - the system may tighten its own
limits and may never loosen them.

That rule is right for ADAPTIVE PARAMETERS, which are inferred from
noisy outcomes. A price is not inferred; it is Anthropic's charge for a
day divided by Anthropic's token counts for the same day. Declining to
believe it downward did not make the ledger safer, it kept it knowingly
wrong - and it did: pricing.py carried a forecast that Sonnet 5's
introductory rate ended 2026-08-31, so on 1 September every call priced
50% higher on a typed-in date, and the correction was rationed to 10%
per three agreeing days.

So these tests now hold the opposite property, and the guards that
survive it: a day too small to measure from, a reading inside the
deadband, a two-model day, and a factor so large it cannot be a price.

No calendar dates anywhere (house rule 6): reconcile_day measures
against datetime.now() and refuses a day that has not closed, so every
date here is relative to the real clock.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.cost.measured_rates import (
    DEADBAND, MIN_DAY_CENTS, SANITY_MULTIPLE, latest_observation,
    learn_from_closed_day,
)
from catalyst.cost.overrides import rates_for_on
from catalyst.storage import init_db

MODEL = "claude-sonnet-5"
YESTERDAY = datetime.now(timezone.utc).date() - timedelta(days=1)


@pytest.fixture
def db(tmp_path):
    """A database with an explicit baseline rate.

    The built-in table USED to be a schedule - Sonnet 5's introductory
    pricing "ending" 2026-08-31 - so the rate in force on the measured
    day and on the day a correction took effect were different numbers
    on one date of the year, and seven tests here failed on exactly
    that date. The schedule is gone (pricing.py is a cold start now, not
    a forecast), and the baseline is kept because a test that states its
    starting rate is readable and one that inherits it is not.
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

    ALWAYS COMPARED ON THE SAME DATE. Overrides are date-effective, so
    reading "before" on one day and "after" on the next compares two
    different questions (house rule 6; this file's own first draft did
    exactly that and the clock sweep caught it).
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


class TestTheBillIsAppliedInFull:
    """The property that replaced the asymmetry. If these pass, the
    ledger prices at what Anthropic actually charged."""

    def test_a_bill_higher_than_we_priced_raises_the_rate(self, db):
        """We under-priced, so the real rate is higher than the table
        says."""
        seed_day(db, "100")
        effective = YESTERDAY + timedelta(days=1)
        old_in, old_out = in_force(db, effective)

        m = learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("110"))

        assert m is not None and m.applied is True
        assert m.ratio == Decimal("1.1")
        new_in, new_out = in_force(db, effective)
        assert new_in > old_in and new_out > old_out
        assert "LOW" in m.reason

    def test_ONE_bill_lower_than_we_priced_lowers_the_rate(self, db):
        """THE CHANGE, as a test. This used to require three agreeing
        days and was the reason a 50% over-price could persist for
        weeks."""
        seed_day(db, "100")
        effective = YESTERDAY + timedelta(days=1)
        old_in, old_out = in_force(db, effective)

        m = learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("70"))

        assert m is not None and m.applied is True, (
            "one clean reading did not move the rate - the bill is still "
            "being argued with rather than believed")
        new_in, new_out = in_force(db, effective)
        assert new_in < old_in and new_out < old_out
        assert "HIGH" in m.reason

    def test_the_new_rate_is_what_the_bill_divides_to(self, db):
        """Not a step toward it. The rate that reproduces the bill is the
        rate the day was priced at, scaled by billed/local."""
        seed_day(db, "100")
        effective = YESTERDAY + timedelta(days=1)
        priced_in, priced_out = rates_for_on(db, MODEL, YESTERDAY)

        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("70"))

        got_in, got_out = in_force(db, effective)
        assert got_in == (priced_in * Decimal("0.7")).quantize(Decimal("1"))
        assert got_out == (priced_out * Decimal("0.7")).quantize(Decimal("1"))

    def test_a_rise_lands_in_full_too(self, db):
        """Symmetry both ways: +40% used to be capped at +25%."""
        seed_day(db, "100")
        effective = YESTERDAY + timedelta(days=1)
        priced_in, _ = rates_for_on(db, MODEL, YESTERDAY)

        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("140"))

        assert in_force(db, effective)[0] == (
            priced_in * Decimal("1.4")).quantize(Decimal("1"))

    def test_the_sonnet_5_case_that_prompted_this(self, db):
        """1 September priced 50% high on a typed-in date. One clean
        day's bill now puts it back where the money says it is, instead
        of walking 10% per three agreeing days."""
        seed_day(db, "300")
        effective = YESTERDAY + timedelta(days=1)
        priced_in, _ = rates_for_on(db, MODEL, YESTERDAY)

        # Billed two thirds of what we priced: the intro rate never ended.
        m = learn_from_closed_day(db, YESTERDAY, Decimal("300"), Decimal("200"))

        assert m is not None and m.applied is True
        got = in_force(db, effective)[0]
        assert got == (priced_in * Decimal("200") / Decimal("300")
                       ).quantize(Decimal("1"))

    def test_an_override_row_IS_written_when_the_rate_falls(self, db):
        """The old policy deliberately wrote nothing on this path."""
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("50"))
        assert measured_overrides(db), (
            "a downward correction left no record a later lookup can use")

    def test_history_is_never_repriced_by_a_correction(self, db):
        """The override is effective from the day AFTER the measured
        day, so a row keeps the rate that was in force when it was
        priced. Unchanged by any of this."""
        before = in_force(db, YESTERDAY)
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("70"))
        assert in_force(db, YESTERDAY) == before


class TestAFactorTooLargeToBeAPriceIsRefused:
    """The guard that replaced the step caps."""

    def test_an_absurd_overstatement_is_not_applied(self, db):
        seed_day(db, "100")
        effective = YESTERDAY + timedelta(days=1)
        before = in_force(db, effective)

        m = learn_from_closed_day(
            db, YESTERDAY, Decimal("100"),
            Decimal("100") * (SANITY_MULTIPLE + 1))

        assert m is not None and m.applied is False
        assert in_force(db, effective) == before
        assert "NOT applied" in m.reason

    def test_an_absurd_understatement_is_not_applied_either(self, db):
        """A credit or a refund reads as the bill collapsing. That is
        the direction that would let the bot overspend, so it is exactly
        the one the clamp has to catch."""
        seed_day(db, "1000")
        effective = YESTERDAY + timedelta(days=1)
        before = in_force(db, effective)

        m = learn_from_closed_day(
            db, YESTERDAY, Decimal("1000"),
            Decimal("1000") / (SANITY_MULTIPLE + 1))

        assert m is not None and m.applied is False
        assert in_force(db, effective) == before

    def test_a_refused_reading_is_still_recorded(self, db):
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("10000"))
        obs = latest_observation(db)
        assert obs and "NOT applied" in obs["reason"]

    def test_a_large_but_believable_move_still_applies(self, db):
        """The clamp must not become the step cap it replaced. 3x is
        inside it."""
        seed_day(db, "100")
        m = learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("300"))
        assert m is not None and m.applied is True


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


class TestItConvergesOnTheBillRatherThanDrifting:
    def test_a_second_day_that_agrees_changes_nothing_further(self, db):
        """Once the rate matches the bill, the next day's reading falls
        inside the deadband and nothing moves. The old policy had to
        keep stepping toward the answer; this one arrives."""
        first = datetime.now(timezone.utc).date() - timedelta(days=2)
        second = first + timedelta(days=1)
        seed_day(db, "100", day=first)
        learn_from_closed_day(db, first, Decimal("100"), Decimal("140"))
        landed = in_force(db, second)

        # The next day, priced at the corrected rate, bills as expected.
        seed_day(db, "140", day=second)
        m = learn_from_closed_day(db, second, Decimal("140"), Decimal("140"))

        assert m is not None and m.applied is False
        assert "agreed within" in m.reason
        assert in_force(db, second + timedelta(days=1)) == landed

    def test_it_does_not_ratchet_on_repeated_identical_days(self, db):
        """Three identical readings must not compound into 3x. Each day
        is priced at the rate the previous one set, so once it is right
        the ratio is 1."""
        rate_after = []
        for i, (local, billed) in enumerate(
                ((Decimal("100"), Decimal("200")),
                 (Decimal("200"), Decimal("200")),
                 (Decimal("200"), Decimal("200")))):
            day = datetime.now(timezone.utc).date() - timedelta(days=3 - i)
            seed_day(db, str(local), day=day)
            learn_from_closed_day(db, day, local, billed)
            rate_after.append(in_force(db, day + timedelta(days=1))[0])
        assert rate_after[1] == rate_after[0] == rate_after[2], (
            f"the rate kept moving after it was correct: {rate_after}")


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
        stands for the human either way.

        The pausing direction is the bot pricing MORE than the whole
        account was billed; the reverse is the normal state of a shared
        account and no longer pauses."""
        seed_day(db, "400")
        result = self._reconcile(db, "100")
        assert result.action_taken == "discrepancy_noted"
        obs = latest_observation(db)
        assert obs is not None, "a noted discrepancy skipped the learner"
        # An OBSERVATION proves the learner ran, and since 2026-09-05 a
        # 4x factor is refused as not-a-price (SANITY_MULTIPLE) - so it
        # is on the record and NOT applied, which is the right answer
        # for a bill a quarter of what was priced.
        assert obs["target_date"] == YESTERDAY.isoformat()
        assert Decimal(str(obs["ratio"])) < 1

    def test_learning_never_changes_the_reconciliation_verdict(self, db):
        """The ledger check is the thing that guards the money. Learning
        a rate rides alongside it and must not soften it."""
        seed_day(db, "400")
        result = self._reconcile(db, "100")
        assert result.action_taken == "discrepancy_noted"
        assert result.local_total_cents == Decimal("400")
        assert result.cost_api_total_cents == Decimal("100")
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
        assert "the table was left alone" in html

    def test_a_raised_rate_says_it_was_RAISED(self, db):
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("120"))
        html = self._cost_page(db.execute(
            "PRAGMA database_list").fetchone()[2])
        assert "the table was RAISED to match" in html

    def test_a_lowered_rate_says_it_was_LOWERED(self, db):
        """The page used to say "raised" for every applied correction,
        because a correction could only ever go one way. It can go both
        ways now, and calling a price cut a rise is the kind of wrong
        label that costs an hour of reading the wrong table."""
        seed_day(db, "100")
        learn_from_closed_day(db, YESTERDAY, Decimal("100"), Decimal("70"))
        html = self._cost_page(db.execute(
            "PRAGMA database_list").fetchone()[2])
        assert "the table was LOWERED to match" in html
        assert "RAISED" not in html

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
