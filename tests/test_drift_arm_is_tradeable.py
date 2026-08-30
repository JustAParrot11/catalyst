"""A candidate arm without a risk-table row cannot trade, only spend.

FOUND WHILE WIRING THE DRIFT ARM IN, before it ever ran live.
`strategies/earnings_drift.py` sets catalyst_type="earnings_drift" and
`risk/adaptive_params.py` had no such key - so evaluate.py would have
appended `unknown_catalyst_type` and skipped every one of its
candidates, AFTER paying for the research.

That is not a new failure. The note at the top of that table records it
costing 64% of one live day's candidates:

    discovery can produce 18 kinds and the risk engine had parameters
    for `insider_cluster` alone, so 23 of 36 candidates - 64% - died on
    `unknown_catalyst_type` in evaluate.py before conviction was read.
    Some had already been paid for.

Wiring an arm in without its row would have repeated that exactly, for
a brand-new arm, on the owner's money.

These tests hold the general rule rather than this instance (house rule
7): every catalyst_type a WIRED strategy can emit must have a row in
every per-type risk parameter. The next arm someone connects gets caught
by the same check.

Scoped to WIRED arms deliberately. `etf_rotation` sits on disk unwired
and must stay that way - it graded at -499.87% excess over SPY - so
giving it a row would be arranging for a strategy the backtest rejected
to become tradeable.

Fully offline.
"""

from decimal import Decimal

import pytest

from catalyst.risk.adaptive_params import (
    DEFAULT_PARAMS, GRADED_CATALYST_TYPES, _CATALYST_SHAPES,
)

PER_TYPE = ("adverse_gap_assumption", "stop_width",
            "holding_period_estimate", "search_budget_allocation")


def wired_strategy_modules() -> set:
    """Which strategy modules the live pipeline actually imports.

    Not every module on disk is connected. `etf_rotation` is on disk
    and deliberately NOT wired: it graded at -499.87% excess over SPY
    (bake-off arm E), so it must not trade, and giving it a risk-table
    row would be arranging for it to.

    Read from the source of the modules that build candidates, so an arm
    connected later is covered without anyone remembering this file.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "catalyst"
    wired = set()
    for area in ("orchestrator", "discovery", "data"):
        for path in (root / area).rglob("*.py"):
            wired |= set(re.findall(
                r"catalyst\.strategies\.([a-z_]+)", path.read_text()))
    return wired


def emitted_catalyst_types() -> set:
    """Every catalyst_type a WIRED strategy can produce.

    Read from the source rather than listed here, so an arm connected
    later is covered without anyone updating this file.
    """
    import inspect
    import re

    found = set()
    for module in sorted(wired_strategy_modules()):
        try:
            mod = __import__(f"catalyst.strategies.{module}",
                             fromlist=[module])
            found |= set(re.findall(
                r'catalyst_type\s*=\s*"([a-z_]+)"', inspect.getsource(mod)))
        except Exception:  # noqa: BLE001 - an unreadable module is not a type
            continue
    return found


class TestEveryEmittedTypeCanBeSized:
    def test_the_drift_arm_has_a_row(self):
        """The instance that prompted this."""
        assert "earnings_drift" in _CATALYST_SHAPES

    @pytest.mark.parametrize("param", PER_TYPE)
    def test_earnings_drift_is_in_every_per_type_parameter(self, param):
        assert "earnings_drift" in DEFAULT_PARAMS[param], (
            f"{param} has no earnings_drift row, so evaluate.py skips "
            "every drift candidate as unknown_catalyst_type - after the "
            "research has been paid for")

    def test_no_strategy_can_emit_a_type_the_risk_engine_cannot_size(self):
        """THE RULE, not the instance. Any arm wired in later fails here
        rather than in production."""
        emitted = emitted_catalyst_types()
        assert emitted, "the scan found no catalyst types at all"
        for param in PER_TYPE:
            missing = sorted(emitted - set(DEFAULT_PARAMS[param]))
            assert not missing, (
                f"{param} cannot size {missing} - those candidates would be "
                "researched, paid for, and then skipped")


class TestTheSearchBudgetStillBalances:
    def test_the_shares_sum_to_exactly_one(self):
        """A joint invariant refuses a table that does not. Adding a row
        without taking the share from somewhere breaks it."""
        total = sum(Decimal(v[3]) for v in _CATALYST_SHAPES.values())
        assert total == Decimal("1.00"), f"shares total {total}"

    def test_the_share_came_from_named_rows(self):
        """It was taken from earnings_result (same mechanism, ungraded)
        and strategic_review (the widest tails in the table), not
        skimmed off everything at random."""
        assert _CATALYST_SHAPES["earnings_result"][3] == "0.02"
        assert _CATALYST_SHAPES["strategic_review"][3] == "0.03"
        assert _CATALYST_SHAPES["earnings_drift"][3] == "0.06"


class TestTheRowSaysWhatIsMeasuredAndWhatIsNot:
    def test_the_hold_is_the_graded_one(self):
        """HOLD_DAYS was fixed before grading; the bake-off measured it.
        A different number here would make the live arm a different
        strategy from the one that was graded."""
        from catalyst.strategies.earnings_drift import HOLD_DAYS

        assert DEFAULT_PARAMS["holding_period_estimate"]["earnings_drift"] \
            == Decimal(str(HOLD_DAYS))

    def test_it_is_listed_as_graded(self):
        assert "earnings_drift" in GRADED_CATALYST_TYPES

    def test_the_reason_does_not_claim_the_gap_is_measured(self):
        """Overclaiming is the defect this table exists to prevent -
        'the previous build's defect was not wrong numbers, it was wrong
        numbers that looked measured'."""
        why = _CATALYST_SHAPES["earnings_drift"][4]
        assert "hold is measured" in why
        assert "gap and stop copy" in why

    def test_the_gap_matches_the_mechanism_it_copies(self):
        drift = _CATALYST_SHAPES["earnings_drift"]
        result = _CATALYST_SHAPES["earnings_result"]
        assert drift[0] == result[0] and drift[1] == result[1], (
            "the reason says the gap and stop copy earnings_result; if "
            "they no longer do, the reason is now false")


class TestTheCheckCanFail:
    """House rule 4, against the exact shape that shipped."""

    def test_a_type_with_no_row_is_detected(self):
        emitted = emitted_catalyst_types() | {"a_brand_new_arm"}
        missing = sorted(emitted - set(DEFAULT_PARAMS["adverse_gap_assumption"]))
        assert missing == ["a_brand_new_arm"], (
            "the scan cannot see a type that has no row, so it would not "
            "have caught earnings_drift either")
