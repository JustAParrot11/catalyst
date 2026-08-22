"""The pricing multipliers, measured instead of assumed.

The REFUSALS carry the weight here. The Cost API's token_type vocabulary
is not verifiable from this project, and the one recorded response from
the owner's account had every breakdown field null. So the value of this
code is not that it splits a bill correctly - it is that it refuses to
split one it has misread, and falls back to the blended correction that
already works.

A wrong guess must cost a fallback. It must never cost a mispricing.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.cost.components import (
    BilledComponents, ComponentSplitRefused, classify,
)
from catalyst.cost.factors import (
    COMPONENT_SUM_TOLERANCE, DEFAULT_FACTORS, Factors, factors_for_on,
    set_measured_factors,
)
from catalyst.storage import init_db

MODEL = "claude-sonnet-5"
TOL = COMPONENT_SUM_TOLERANCE


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "factors.db"))
    yield conn
    conn.close()


def rec(token_type, amount, **kw):
    return dict({"token_type": token_type, "amount": str(amount)}, **kw)


class TestItRefusesABillItCannotRead:
    """Each of these is a way the guess about somebody else's API could
    be wrong. None of them may produce a price."""

    def test_a_line_it_cannot_place_fails_the_whole_split(self):
        """NOT skipped. A line treated as zero understates the bill -
        the TRAPS.md renamed-field trap wearing a different hat."""
        with pytest.raises(ComponentSplitRefused, match="cannot place"):
            classify([rec("uncached_input_tokens", 60),
                      rec("output_tokens", 30),
                      rec("quantum_flux_tokens", 10)], Decimal("100"), TOL)

    def test_lines_that_do_not_add_up_are_not_trusted(self):
        """The arithmetic check. If the vocabulary is different from what
        is matched, the parts will not reconstruct the whole - and that
        is the signal, without needing to know what was missed."""
        with pytest.raises(ComponentSplitRefused, match="out, over the"):
            classify([rec("uncached_input_tokens", 20),
                      rec("output_tokens", 20)], Decimal("100"), TOL)

    def test_an_unlabelled_line_fails_it(self):
        """This is the shape actually recorded from the owner's account:
        an amount with every descriptive field null."""
        with pytest.raises(ComponentSplitRefused, match="cannot place"):
            classify([{"amount": "100", "token_type": None,
                       "description": None, "cost_type": None}],
                     Decimal("100"), TOL)

    def test_an_unreadable_amount_fails_it(self):
        with pytest.raises(ComponentSplitRefused, match="unreadable amount"):
            classify([rec("output_tokens", "not-a-number")],
                     Decimal("100"), TOL)

    def test_no_records_fails_it(self):
        with pytest.raises(ComponentSplitRefused, match="no records"):
            classify([], Decimal("100"), TOL)

    def test_a_non_positive_total_fails_it(self):
        with pytest.raises(ComponentSplitRefused, match="not positive"):
            classify([rec("output_tokens", 1)], Decimal("0"), TOL)

    def test_the_refusal_says_why(self):
        """It reaches the dashboard. 'No breakdown at all' and 'the
        breakdown did not add up' are different facts - the second means
        this code is reading the API wrongly."""
        try:
            classify([rec("mystery_tokens", 100)], Decimal("100"), TOL)
        except ComponentSplitRefused as exc:
            assert "mystery_tokens" in exc.why
        else:
            pytest.fail("expected a refusal")


class TestItSplitsABillItCanRead:
    def test_every_component_lands_in_its_own_bucket(self):
        got = classify([
            rec("uncached_input_tokens", 40),
            rec("output_tokens", 30),
            rec("cache_creation_input_tokens", 15),
            rec("cache_read_input_tokens", 10),
            rec("web_search_requests", 5),
        ], Decimal("100"), TOL)
        assert got.uncached_input == 40
        assert got.output == 30
        assert got.cache_write == 15
        assert got.cache_read == 10
        assert got.web_search == 5
        assert got.total == 100

    def test_a_cache_line_is_never_read_as_plain_input(self):
        """'cache_read_input_tokens' contains the word input. Order of
        matching is the whole defence against it landing there."""
        got = classify([rec("uncached_input_tokens", 50),
                        rec("cache_read_input_tokens", 50)],
                       Decimal("100"), TOL)
        assert got.cache_read == 50 and got.uncached_input == 50

    def test_the_one_hour_cache_is_separated_from_the_five_minute_one(self):
        """They bill at different multipliers - 2x against 1.25x - so
        merging them prices both wrongly."""
        got = classify([rec("cache_creation_1h_input_tokens", 60),
                        rec("cache_creation_5m_input_tokens", 40)],
                       Decimal("100"), TOL)
        assert got.cache_write_1h == 60 and got.cache_write == 40

    def test_it_reads_the_description_when_token_type_is_empty(self):
        """Which field carries the meaning is the part that cannot be
        checked from here, so all of them are read."""
        got = classify([{"amount": "100", "token_type": None,
                         "description": "Output tokens"}], Decimal("100"), TOL)
        assert got.output == 100

    def test_small_rounding_is_tolerated(self):
        got = classify([rec("output_tokens", "99.999")], Decimal("100"), TOL)
        assert got.total == Decimal("99.999")


class TestTheMultipliersFallBackRatherThanBreak:
    def test_nothing_measured_prices_exactly_as_before(self, db):
        """An install that never measures anything must be bit-identical
        to the code before any of this existed."""
        assert factors_for_on(db, MODEL, date(2026, 8, 22)) == DEFAULT_FACTORS

    def test_a_database_without_the_table_still_prices(self, db):
        db.execute("DROP TABLE measured_factors")
        db.commit()
        assert factors_for_on(db, MODEL, date(2026, 8, 22)) == DEFAULT_FACTORS

    def test_an_unreadable_row_falls_back_rather_than_pricing_at_zero(self, db):
        """A zero multiplier would make cache reads FREE and understate
        every bill - exactly the TRAPS.md failure class."""
        db.execute(
            "INSERT INTO measured_factors VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("x", MODEL, "2026-01-01", "nonsense", "2.0", "0.1", "1",
             "test", "2026-01-01T00:00:00Z", None))
        db.commit()
        assert factors_for_on(db, MODEL, date(2026, 8, 22)) == DEFAULT_FACTORS

    def test_a_negative_multiplier_is_refused_at_the_door(self):
        with pytest.raises(ValueError, match="non-negative"):
            Factors(cache_read=Decimal("-1"))

    def test_a_measured_row_is_used_when_it_is_readable(self, db):
        set_measured_factors(db, MODEL, date(2026, 8, 1),
                             Factors(cache_read=Decimal("0.2")),
                             set_by="test")
        assert factors_for_on(db, MODEL, date(2026, 8, 22)).cache_read == Decimal("0.2")

    def test_it_applies_forward_not_backward(self, db):
        set_measured_factors(db, MODEL, date(2026, 8, 10),
                             Factors(cache_read=Decimal("0.2")),
                             set_by="test")
        assert factors_for_on(db, MODEL, date(2026, 8, 9)) == DEFAULT_FACTORS


class TestPriceUsesTheMeasuredMultipliers:
    def test_a_measured_cache_multiplier_changes_the_price(self, db):
        """End to end: if this passes, the multipliers are no longer
        assumptions as far as the ledger is concerned."""
        from catalyst.cost.factors import factors_for_on as ffo
        from catalyst.cost.tracker import make_usage_components, price

        usage = make_usage_components({
            "input_tokens": 1000, "output_tokens": 1000,
            "cache_read_input_tokens": 1_000_000})
        day = date(2026, 8, 22)
        before = price(usage, MODEL, on_date=day)

        set_measured_factors(db, MODEL, day, Factors(cache_read=Decimal("0.5")),
                             set_by="test")
        after = price(usage, MODEL, on_date=day, factors=ffo(db, MODEL, day))

        assert after > before, "price() ignored the measured multiplier"

    def test_no_factors_argument_is_identical_to_the_old_behaviour(self):
        """The safety net under the whole change: every existing caller
        that passes nothing must price exactly as it always did."""
        from catalyst.cost.pricing import (
            CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER,
        )
        from catalyst.cost.tracker import make_usage_components, price

        usage = make_usage_components({
            "input_tokens": 5000, "output_tokens": 2000,
            "cache_creation_input_tokens": 3000,
            "cache_read_input_tokens": 90000,
            "server_tool_use": {"web_search_requests": 3}})
        day = date(2026, 8, 22)
        assert price(usage, MODEL, on_date=day) == price(
            usage, MODEL, on_date=day,
            factors=Factors(cache_write=CACHE_WRITE_MULTIPLIER,
                            cache_read=CACHE_READ_MULTIPLIER))


class TestTheDerivationOnlyTightensOnOneReading:
    """Same asymmetry as the rates, for the same reason: a multiplier
    that comes out lower LOOSENS the budget."""

    def _seed(self, db, day, tokens, cents):
        db.execute(
            "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
            (f"e{day}", __import__("json").dumps(tokens), MODEL, "scheduled",
             "research", str(cents), f"{day.isoformat()}T12:00:00+00:00", None))
        db.commit()

    def _records(self, input_c, output_c, read_c):
        return [rec("uncached_input_tokens", input_c),
                rec("output_tokens", output_c),
                rec("cache_read_input_tokens", read_c)]

    def test_a_higher_measured_multiplier_applies_at_once(self, db):
        from catalyst.cost.measured_rates import learn_factors_from_closed_day

        day = datetime.now(timezone.utc).date() - timedelta(days=1)
        self._seed(db, day, {"input_tokens": 1_000_000,
                             "output_tokens": 1_000_000,
                             "cache_read_input_tokens": 1_000_000}, "100")
        # input 200c/Mtok implied; cache read billed at 100c/Mtok = 0.5x,
        # far above the assumed 0.1x.
        msg = learn_factors_from_closed_day(
            db, day, MODEL, Decimal("500"),
            self._records(200, 200, 100))

        after = factors_for_on(db, MODEL, day + timedelta(days=1))
        assert after.cache_read > DEFAULT_FACTORS.cache_read, msg
        assert "cache_read" in msg

    def test_a_lower_measured_multiplier_is_held_on_one_reading(self, db):
        from catalyst.cost.measured_rates import learn_factors_from_closed_day

        day = datetime.now(timezone.utc).date() - timedelta(days=1)
        self._seed(db, day, {"input_tokens": 1_000_000,
                             "output_tokens": 1_000_000,
                             "cache_read_input_tokens": 1_000_000}, "100")
        # cache read billed at 2c/Mtok against a 200c input = 0.01x,
        # BELOW the assumed 0.1x - cheaper, so it loosens.
        msg = learn_factors_from_closed_day(
            db, day, MODEL, Decimal("402"),
            self._records(200, 200, 2))

        after = factors_for_on(db, MODEL, day + timedelta(days=1))
        assert after.cache_read == DEFAULT_FACTORS.cache_read, msg
        assert "held" in msg.lower()

    def test_a_bill_it_cannot_split_leaves_every_multiplier_alone(self, db):
        from catalyst.cost.measured_rates import learn_factors_from_closed_day

        day = datetime.now(timezone.utc).date() - timedelta(days=1)
        self._seed(db, day, {"input_tokens": 1_000_000}, "100")
        msg = learn_factors_from_closed_day(
            db, day, MODEL, Decimal("100"), [rec("mystery_tokens", 100)])

        assert factors_for_on(db, MODEL, day + timedelta(days=1)) == DEFAULT_FACTORS
        assert "not measured" in msg and "mystery_tokens" in msg

    def test_too_little_volume_measures_nothing(self, db):
        from catalyst.cost.measured_rates import learn_factors_from_closed_day

        day = datetime.now(timezone.utc).date() - timedelta(days=1)
        self._seed(db, day, {"input_tokens": 100, "cache_read_input_tokens": 5},
                   "1")
        msg = learn_factors_from_closed_day(
            db, day, MODEL, Decimal("100"),
            [rec("uncached_input_tokens", 99), rec("cache_read_input_tokens", 1)])
        assert factors_for_on(db, MODEL, day + timedelta(days=1)) == DEFAULT_FACTORS
        assert "enough volume" in msg or "not measured" in msg
