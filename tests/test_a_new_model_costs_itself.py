"""A model released tomorrow is selectable today, and prices itself.

OWNER-ASKED 2026-09-12: *"i want nothing manual, i want it to auto add
the models and also where can i actually change the dropdown to try a
different model, e.g. i wanted to switch to opus 5, the pricing is ok as
it calls per day doesnt it so it will just mark a higher price"*.

THE OWNER'S OWN ASSUMPTION WAS ALMOST RIGHT, AND THE GAP IS WHY THIS
FILE EXISTS. The daily reconciliation does correct a rate from the real
bill - but it corrects by RATIO: what Anthropic charged for a closed day
divided by what the ledger priced that day locally. With no local rate
at all there is nothing to divide. `price()` raised, `record_usage`
wrote an UNPRICED row, and `governor.authorize` then refuses ALL spend
while an unpriced row exists - so the bot halted before any bill could
teach it anything. The mechanism can correct a number; it cannot
bootstrap from none.

So the chain this file tests, end to end:

    a model nobody has billed us for
      -> priced from cold_start_rates(), deliberately HIGH
      -> the row lands PRICED, so the governor keeps authorising
      -> the closed day's real bill measures what it actually cost
      -> set_override writes the measured rate for THAT model
      -> the next call prices at the measured rate

Every link had to change. The seed did not exist; `set_override` refused
a model absent from the table, so the measured rate was computed and
thrown away; and the position review used a SEPARATE hard-coded model,
so switching research alone billed two models on one day and
`measured_rates._sole_model` refused to learn anything at all.

Fully offline.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from catalyst.cost.pricing import (
    COLD_START_MULTIPLE,
    MODEL_RATES_CENTS_PER_MTOK,
    UnknownModelError,
    cold_start_rates,
    has_published_rate,
    rates_for,
)

NEW = "claude-opus-9"          # deliberately not in any table
SOME_DAY = date(2026, 9, 12)


class TestTheSeedErrsInTheSafeDirection:
    def test_an_unknown_model_is_priced_above_everything_known(self):
        seed_in, seed_out = cold_start_rates()
        for model, (inp, outp) in MODEL_RATES_CENTS_PER_MTOK.items():
            assert seed_in > inp, f"seed is not above {model}"
            assert seed_out > outp, f"seed is not above {model}"

    def test_over_pricing_is_the_safe_direction_and_is_the_one_chosen(self):
        """Stated as an assertion because it is the whole design: an
        estimate that reads HIGH makes the bot do less inside the same
        cap, which costs opportunity. One that reads LOW authorises
        calls the budget cannot afford. The owner's only stated hard
        requirement is 'a hard stop to stop bot using all the budget'."""
        assert COLD_START_MULTIPLE > 1

    def test_the_seed_is_derived_from_the_table_not_typed(self):
        """Adding a dearer model must raise the seed with no second
        number for anyone to remember."""
        before = cold_start_rates()
        MODEL_RATES_CENTS_PER_MTOK["claude-test-dear"] = (
            Decimal("9000"), Decimal("45000"))
        try:
            after = cold_start_rates()
        finally:
            del MODEL_RATES_CENTS_PER_MTOK["claude-test-dear"]
        assert after[0] > before[0] and after[1] > before[1]
        assert after == (Decimal("9000") * COLD_START_MULTIPLE,
                         Decimal("45000") * COLD_START_MULTIPLE)

    def test_the_seed_is_close_enough_for_the_bill_to_correct_it(self):
        """THE CONSTANT RELATIONSHIP THAT MAKES THE CHAIN WORK, and the
        two numbers live in different files.

        `measured_rates.SANITY_MULTIPLE` refuses a measured rate more
        than 4x from the one in force, treating it as a credit or a
        misread bill. A seed further out than that would be rejected as
        impossible on every clean day and never corrected, leaving the
        bot throttling itself against a guess forever."""
        from catalyst.cost.measured_rates import SANITY_MULTIPLE

        assert COLD_START_MULTIPLE < SANITY_MULTIPLE, (
            f"a {COLD_START_MULTIPLE}x seed cannot be corrected by a bill "
            f"the reconciliation only believes within {SANITY_MULTIPLE}x")

    def test_a_call_naming_no_model_still_refuses(self):
        """The guard that survived. There is no rate for 'unnamed', and
        pooling that spend would make a two-model day read as one to
        _sole_model."""
        for bad in ("", "   ", None, 5, object()):
            with pytest.raises(UnknownModelError):
                rates_for(bad, SOME_DAY)

    def test_where_the_price_came_from_is_reportable(self):
        assert has_published_rate("claude-sonnet-5") is True
        assert has_published_rate(NEW) is False


class TestTheGovernorKeepsRunningOnANewModel:
    def test_a_new_models_call_lands_priced_so_spending_continues(self, tmp_db):
        """The failure this replaces: one unpriced row blocks EVERY
        later authorize(), scheduled and manual alike."""
        from decimal import Decimal as D

        from catalyst.cost import CostEstimate
        from catalyst.cost.governor import DEFAULT_GOVERNOR_PROFIT_SHARE, authorize
        from catalyst.cost.tracker import has_unpriced_rows, record_usage

        record_usage({"input_tokens": 1000, "output_tokens": 100},
                     NEW, "scheduled", "research", tmp_db)
        assert not has_unpriced_rows(tmp_db)
        decision = authorize(
            CostEstimate(estimated_cents=D("1"), basis="t",
                         kind="scheduled", component="research"),
            tmp_db, DEFAULT_GOVERNOR_PROFIT_SHARE)
        assert decision.authorized, decision.reason


class TestTheBillReplacesTheGuess:
    def test_a_measured_rate_can_be_written_for_a_model_not_in_the_table(
            self, tmp_db):
        """set_override used to refuse this, which quietly broke the
        self-correction for exactly the models that need it: the
        measured rate was computed and discarded, and the cold-start
        guess stayed in force forever."""
        from catalyst.cost.overrides import rates_for_on, set_override

        set_override(tmp_db, NEW, SOME_DAY, Decimal("500"), Decimal("2500"),
                     set_by="test", allow_large_change=True)
        assert rates_for_on(tmp_db, NEW, SOME_DAY) == (Decimal("500"),
                                                       Decimal("2500"))

    def test_the_closed_days_bill_corrects_the_seed_end_to_end(self, tmp_db):
        """The whole chain, with the Admin API's figure standing in for
        the real one. The seed prices the day; the bill says the day
        cost less; the rate becomes what the bill divides to; the next
        call prices at the measured rate."""
        from catalyst.cost.measured_rates import learn_from_closed_day
        from catalyst.cost.overrides import rates_for_on
        from catalyst.cost.tracker import record_usage

        yesterday = date.today() - timedelta(days=1)
        # Enough tokens that the day clears MIN_DAY_CENTS.
        event = record_usage({"input_tokens": 20_000_000, "output_tokens": 0},
                             NEW, "scheduled", "research", tmp_db)
        tmp_db.execute("UPDATE cost_events SET priced_at = ?",
                       (yesterday.isoformat() + "T12:00:00+00:00",))
        tmp_db.commit()
        local = Decimal(event.priced_cents)
        assert local > 0

        # ANTHROPIC CHARGED HALF WHAT WE GUESSED - the expected shape,
        # because the seed is deliberately double the dearest known
        # rate.
        billed = (local / 2).quantize(Decimal("0.00001"))
        measured = learn_from_closed_day(tmp_db, yesterday, local, billed)
        assert measured is not None and measured.applied, (
            measured.reason if measured else "nothing learned at all")
        assert measured.model == NEW

        corrected = rates_for_on(tmp_db, NEW, yesterday + timedelta(days=1))
        assert corrected[0] < cold_start_rates()[0]
        assert corrected[0] > 0

    def test_learning_needs_one_model_a_day_so_the_review_shares_the_choice(
            self, tmp_db):
        """WHY THE POSITION REVIEW FOLLOWS THE RESEARCH SELECTION.

        `_sole_model` returns None when two models billed on a day,
        because the ratio would be a blend of two rates. REVIEW_MODEL
        was a separate constant: switching research to a new model
        would bill two a day, learning would refuse, and the new
        model's deliberately-high seed would never be corrected."""
        from catalyst.cost.measured_rates import _sole_model
        from catalyst.cost.tracker import record_usage

        day = date.today() - timedelta(days=2)
        for model in (NEW, "claude-sonnet-5"):
            record_usage({"input_tokens": 1000, "output_tokens": 10},
                         model, "scheduled", "research", tmp_db)
        tmp_db.execute("UPDATE cost_events SET priced_at = ?",
                       (day.isoformat() + "T12:00:00+00:00",))
        tmp_db.commit()
        assert _sole_model(tmp_db, day) is None, (
            "two models on one day must refuse to yield a rate")


class TestTheReviewUsesTheModelTheOwnerPicked:
    def test_review_position_accepts_a_model(self):
        import inspect

        from catalyst.research.position_review import review_position

        assert "model" in inspect.signature(review_position).parameters

    def test_the_payload_carries_the_given_model(self):
        from catalyst.research.position_review import _review_turn_payload

        assert _review_turn_payload("p", 1, model=NEW)["model"] == NEW

    def test_the_payload_falls_back_rather_than_sending_none(self):
        from catalyst.research.position_review import (
            DEFAULT_REVIEW_MODEL, _review_turn_payload,
        )

        assert _review_turn_payload("p", 1)["model"] == DEFAULT_REVIEW_MODEL

    def test_the_cycle_passes_the_research_model_into_the_review(self):
        """THE CALL SITE, not the function. A helper nobody calls has
        passed its own tests three times in this project
        (docs/WHAT-WE-TRIED.md section 6)."""
        import inspect

        from catalyst.orchestrator import cycle

        src = inspect.getsource(cycle._review_open_positions)
        assert "model=research_model" in src, (
            "_review_open_positions does not hand the chosen model to "
            "review_position, so reviews would bill a second model")
        outer = inspect.getsource(cycle.run_cycle)
        assert "research_model=research_model" in outer, (
            "run_cycle does not pass the chosen model to the review sweep")


class TestCorrectingARateByHandStillHasARule:
    """The manual price form cannot use "is it in pricing.py's table"
    any more - that is exactly the check this change removed. The rule
    it uses instead: a published rate, or a model the ledger has really
    billed. Both are checkable offline."""

    def test_a_non_anthropic_model_is_refused(self, tmp_db):
        from catalyst.dashboard.server import _price_correctable

        assert _price_correctable(tmp_db, "gpt-9") is False
        assert _price_correctable(tmp_db, "") is False

    def test_a_published_model_is_correctable(self, tmp_db):
        from catalyst.dashboard.server import _price_correctable

        assert _price_correctable(tmp_db, "claude-sonnet-5") is True

    def test_a_new_model_becomes_correctable_once_it_has_billed(self, tmp_db):
        """The rule has to admit a model released after this code, or
        the owner could never correct its rate at all."""
        from catalyst.cost.tracker import record_usage
        from catalyst.dashboard.server import _price_correctable

        assert _price_correctable(tmp_db, NEW) is False
        record_usage({"input_tokens": 10, "output_tokens": 1},
                     NEW, "scheduled", "research", tmp_db)
        assert _price_correctable(tmp_db, NEW) is True


class TestWhatTheAdversarialReadFound:
    """Three defects in the 2026-09-12 change, found by reading it
    adversarially rather than by a test failing (house rule 5's written
    read). Each is a test now."""

    def test_a_cheap_new_model_can_escape_its_own_cold_start_guess(
            self, tmp_db):
        """THE WORST OF THE THREE, because it made "nothing manual"
        false. The seed is 2x the DEAREST known rate; a Haiku-class
        release is a tenth of it; `SANITY_MULTIPLE` refuses a 10x
        correction as implausible - so the guess stayed in force forever
        and the bot throttled at ten times the true price, with a
        hand-typed rate the only way out.

        Measured before the fix: applied=False, rate still 1000/5000."""
        from catalyst.cost.measured_rates import learn_from_closed_day
        from catalyst.cost.overrides import rates_for_on
        from catalyst.cost.tracker import record_usage

        day = date.today() - timedelta(days=1)
        cheap = "claude-haiku-9"
        event = record_usage({"input_tokens": 20_000_000, "output_tokens": 0},
                             cheap, "scheduled", "research", tmp_db)
        tmp_db.execute("UPDATE cost_events SET priced_at = ?",
                       (day.isoformat() + "T12:00:00+00:00",))
        tmp_db.commit()
        local = Decimal(event.priced_cents)

        m = learn_from_closed_day(tmp_db, day, local, local / 10)
        assert m is not None and m.applied, (
            "a first measurement was refused, so the cold-start guess is "
            f"permanent: {m.reason if m else 'nothing recorded'}")
        assert rates_for_on(tmp_db, cheap, day + timedelta(days=1)) == (
            Decimal("100"), Decimal("500"))
        assert "FIRST MEASUREMENT" in m.reason

    def test_the_sanity_bound_returns_the_moment_a_rate_is_measured(
            self, tmp_db):
        """The other half, and the one that keeps the bound meaningful.
        Skipping SANITY_MULTIPLE is only defensible against a GUESS.
        Once a rate has been measured, an absurd ratio is a credit or a
        misread bill again and must be refused."""
        from catalyst.cost.measured_rates import learn_from_closed_day
        from catalyst.cost.tracker import record_usage

        day = date.today() - timedelta(days=1)
        cheap = "claude-haiku-9"
        event = record_usage({"input_tokens": 20_000_000, "output_tokens": 0},
                             cheap, "scheduled", "research", tmp_db)
        tmp_db.execute("UPDATE cost_events SET priced_at = ?",
                       (day.isoformat() + "T12:00:00+00:00",))
        tmp_db.commit()
        local = Decimal(event.priced_cents)
        assert learn_from_closed_day(tmp_db, day, local, local / 10).applied

        # Beyond 4x, and still above MIN_DAY_CENTS - otherwise it is
        # refused as "day too small" and proves nothing about the bound.
        absurd = learn_from_closed_day(tmp_db, day, local, local / 10)
        assert absurd is not None and not absurd.applied, (
            "an absurd ratio was applied against a MEASURED rate, so the "
            "sanity bound no longer protects anything")
        assert "beyond" in absurd.reason, absurd.reason

    def test_a_published_model_is_never_treated_as_a_first_measurement(
            self, tmp_db):
        from catalyst.cost.measured_rates import _is_cold_start_guess

        assert _is_cold_start_guess(tmp_db, "claude-sonnet-5") is False
        assert _is_cold_start_guess(tmp_db, NEW) is True

    def test_a_rate_that_would_round_to_zero_is_refused_and_recorded(
            self, tmp_db):
        """Reachable only since the bound stopped applying to a first
        measurement. A rate of zero prices every later call at nothing,
        and the old code returned None - leaving the seed in force with
        nothing on the record saying why."""
        from catalyst.cost.measured_rates import learn_from_closed_day
        from catalyst.cost.tracker import record_usage

        day = date.today() - timedelta(days=1)
        # BIG ENOUGH THAT THE ARITHMETIC CAN REACH ZERO. 1000c/MTok x 100
        # MTok = 100,000c priced locally, against a 30c bill: the ratio is
        # 0.0003, so the input rate rounds to 0. Any smaller day is
        # refused earlier by MIN_DAY_CENTS on the billed side, which is
        # why this branch is close to unreachable in production - it is
        # defence against arithmetic, not against an expected input.
        event = record_usage({"input_tokens": 100_000_000, "output_tokens": 0},
                             NEW, "scheduled", "research", tmp_db)
        tmp_db.execute("UPDATE cost_events SET priced_at = ?",
                       (day.isoformat() + "T12:00:00+00:00",))
        tmp_db.commit()
        local = Decimal(event.priced_cents)
        assert local == Decimal("100000.00"), local

        m = learn_from_closed_day(tmp_db, day, local, Decimal("30"))
        assert m is not None, "refused silently, with nothing on the record"
        assert not m.applied
        assert "zero" in m.reason
        rows = tmp_db.execute(
            "SELECT COUNT(*) FROM measured_rate_observations").fetchone()[0]
        assert rows == 1, "the refusal was not written down"

    def test_an_empty_pricing_table_raises_the_exception_callers_catch(self):
        """`max()` on an empty table raises a BARE ValueError, and
        UnknownModelError is the only exception the recording path
        catches - so it escaped `record_usage`, abandoned the cycle, and
        lost the record of spend that had already happened."""
        import catalyst.cost.pricing as pricing

        saved = dict(pricing.MODEL_RATES_CENTS_PER_MTOK)
        pricing.MODEL_RATES_CENTS_PER_MTOK.clear()
        try:
            with pytest.raises(UnknownModelError):
                rates_for("claude-anything", SOME_DAY)
        finally:
            pricing.MODEL_RATES_CENTS_PER_MTOK.update(saved)
