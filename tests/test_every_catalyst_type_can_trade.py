"""A catalyst type discovery can produce must be one the risk engine can size.

OWNER-ASKED: "i want high confidence we are successful and have a broad
range of investment areas not just one."

It had one. MEASURED from the owner's live bundle for 2026-08-14:

    catalyst types among 36 candidates : 11
    types the risk engine had parameters for : 1  (insider_cluster)
    candidates that could ever trade  : 13 of 36
    structurally excluded             : 23  (64%)

Ten kinds of event were discovered, sometimes researched at cost, and
then discarded by evaluate.py on `unknown_catalyst_type` before
conviction was ever read. Nothing said so: the funnel showed candidates
arriving and leaving and it looked like ordinary attrition.

It was not a strategy decision. It was a table nobody had filled in, and
the only reason it stayed invisible is that no test compared the two
lists. This is that test.

WHAT IT GUARDS, and why each half matters:

  - EVERY type conjunctions.py can return has risk parameters. A new
    catalyst type added to discovery without a row here is silently
    untradeable, which is exactly the failure this file exists to stop.
  - The parameters are INTERNALLY VALID: inside their declared ranges,
    search shares totalling no more than 1.0, and passing the same joint
    hard-bound check apply() runs. A table that opens the gate and then
    fails the bound check has moved the problem, not fixed it.
  - Which rows are GRADED and which are ESTIMATES stays separable. The
    previous build's defect was not wrong numbers; it was wrong numbers
    that looked measured.
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.risk import adaptive_params as ap
from catalyst.risk.hard_bounds import HARD_BOUNDS


def _types_discovery_can_produce() -> set:
    """Read them out of conjunctions.py, not out of the parameter table.

    Sourcing both sides from the same place is how the original gap
    survived: the risk table was self-consistent and simply did not
    mention most of what discovery emits.
    """
    src = (Path(__file__).resolve().parents[1]
           / "catalyst/discovery/conjunctions.py").read_text()
    block = re.search(r"for kind in \((.*?)\):", src, re.S)
    assert block, "the catalyst-type list in conjunctions.py moved"
    kinds = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    assert len(kinds) > 5, f"only found {kinds} - the regex rotted"
    return kinds


def _snapshot() -> dict:
    return {k: (dict(v) if isinstance(v, dict) else v)
            for k, v in ap.DEFAULT_PARAMS.items()}


class TestNothingIsDiscoveredThatCannotBeTraded:
    @pytest.mark.parametrize("kind", sorted(_types_discovery_can_produce()))
    def test_the_risk_engine_can_size_it(self, kind):
        """THE REPORT. Without a row here, evaluate.py appends
        `unknown_catalyst_type` and the candidate dies before conviction
        is read - after the research was paid for."""
        for param in ("adverse_gap_assumption", "stop_width",
                      "holding_period_estimate", "search_budget_allocation"):
            assert kind in ap.DEFAULT_PARAMS[param], (
                f"discovery can produce {kind!r} and the risk engine has "
                f"no {param} for it, so every one of them is discarded "
                "as unknown_catalyst_type")

    def test_it_really_reaches_a_decision_rather_than_an_unknown_type(self):
        """End to end through evaluate(), not just a dict lookup - the
        skip reason is what the funnel shows the owner.

        Uses the SHIPPED table, not test_risk's fixture. The first
        version of this read the fixture and reported analyst_action as
        still broken when it was fine, which is the same class of
        mistake as the defect under test: checking a copy rather than
        the thing that runs.
        """
        from tests.test_risk import candidate, market, portfolio, view

        params = dict(_snapshot())
        params["holding_period_basis"] = "test"
        for kind in sorted(_types_discovery_can_produce()):
            d = evaluate_kind(kind, params, candidate, view, portfolio, market)
            assert "unknown_catalyst_type" not in d.skip_reasons, (
                f"{kind} still dies before conviction is read")


def evaluate_kind(kind, PARAMS, candidate, view, portfolio, market):
    from dataclasses import replace

    from catalyst.risk.evaluate import evaluate

    return evaluate(replace(candidate(), catalyst_type=kind),
                    view(), portfolio(), PARAMS, market())


class TestTheTableIsInternallyValid:
    """Opening the gate and then failing the bound check would move the
    problem rather than fix it."""

    def test_every_value_is_inside_its_declared_range(self):
        bad = []
        for param in ("adverse_gap_assumption", "stop_width",
                      "holding_period_estimate", "search_budget_allocation"):
            lo, hi = ap.PARAM_RANGE[param]
            for kind, value in ap.DEFAULT_PARAMS[param].items():
                if not (lo <= Decimal(str(value)) <= hi):
                    bad.append((param, kind, value, lo, hi))
        assert not bad, bad

    def test_the_search_shares_total_no_more_than_one(self):
        """They are a SHARE of one budget across types, not a per-type
        multiplier. My first attempt at this table totalled 9.6 and was
        correctly refused by the joint invariant."""
        total = sum(Decimal(str(v)) for v in
                    ap.DEFAULT_PARAMS["search_budget_allocation"].values())
        assert total <= Decimal("1"), f"shares total {total}"

    def test_it_passes_the_same_joint_check_apply_runs(self):
        assert ap._joint_check(
            _snapshot(), "adverse_gap_assumption.earnings",
            HARD_BOUNDS) is None

    def test_no_position_can_breach_the_per_position_bound(self):
        """The bound that decides whether one bad morning matters. Worst
        case is max(gap, stop) and sizing solves against it; this checks
        the arithmetic lands under the bound for every type, including
        the 60% binaries."""
        from decimal import ROUND_DOWN

        slot = Decimal("1") / Decimal(HARD_BOUNDS.max_open_positions)
        for kind, gap in ap.DEFAULT_PARAMS["adverse_gap_assumption"].items():
            stop = ap.DEFAULT_PARAMS["stop_width"][kind]
            worst = max(Decimal(str(gap)), Decimal(str(stop)))
            # ROUNDED DOWN, exactly as _joint_check does. Writing this
            # without the rounding reproduced the very artifact the
            # production fix exists for: 0.02/0.14 repeats, and
            # multiplying it back overshoots 0.02 by 1E-29. A test that
            # models the arithmetic differently from the code is testing
            # its own arithmetic.
            frac = min(
                (HARD_BOUNDS.max_loss_per_position_pct / worst).quantize(
                    Decimal("0.00000001"), rounding=ROUND_DOWN),
                slot)
            assert frac * worst <= HARD_BOUNDS.max_loss_per_position_pct, kind


class TestAnEstimateIsNeverPresentedAsEvidence:
    def test_only_the_graded_type_is_marked_graded(self):
        """One row rests on a backtest. Seventeen rest on reasoning about
        the shape of the event, and the page must say which is which."""
        assert ap.GRADED_CATALYST_TYPES == frozenset({"insider_cluster"})

    def test_every_type_carries_its_reasoning(self):
        for kind in ap.DEFAULT_PARAMS["adverse_gap_assumption"]:
            why = ap.catalyst_shape_reason(kind)
            assert len(why) > 20, f"{kind} has no stated reasoning"
            assert "no parameters recorded" not in why

    def test_an_unknown_type_says_so_rather_than_inventing_a_reason(self):
        assert "no parameters recorded" in ap.catalyst_shape_reason("wat")

    def test_the_true_binaries_are_sized_TINY_not_excluded(self):
        """The brief's own evidence: the previous build measured ~60%
        adverse gaps on FDA and clinical binaries and concluded edge and
        un-sizeable risk were the same property. A 60% gap sizes the
        position at about 3.3% of the account - deliberately small
        rather than zero, because a small position in a real edge
        accumulates a sample and a zero position never does."""
        for kind in ("fda_decision", "clinical_readout"):
            gap = ap.DEFAULT_PARAMS["adverse_gap_assumption"][kind]
            assert gap >= Decimal("0.5"), (
                f"{kind} assumes a {gap} gap - the measured figure was "
                "~0.60, and assuming less sizes the position too large")
            frac = HARD_BOUNDS.max_loss_per_position_pct / gap
            assert frac < Decimal("0.05"), (
                "a true binary should take well under 5% of the account")

    def test_the_dashboard_shows_which_is_which(self):
        from catalyst.dashboard.panels import catalyst_coverage_block

        html_out = catalyst_coverage_block("alerts")
        assert "graded" in html_out and "estimate" in html_out
        assert "insider cluster" in html_out
        assert "fda decision" in html_out
        # ...and it says what a MISSING type would mean.
        assert "unknown_catalyst_type" in html_out
