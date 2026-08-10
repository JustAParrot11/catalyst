"""Adaptive parameters - the only writer of adaptive state.

Every rule here is from ARCHITECTURE.md section 6.3: closed scored
outcomes only, minimum sample, asymmetric speed (tighten 3x faster than
loosen), bounded step, logged with evidence, reversible. apply() checks
proposals JOINTLY against the full live snapshot of every other
parameter, never marginally, and refuses evidence windows that overlap
the window behind this parameter's previous adjustment.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from catalyst.risk.hard_bounds import HardBounds

ADAPTIVE_PARAMETERS = [
    "conviction_floor",
    "adverse_gap_assumption",   # per catalyst_type
    "stop_width",               # per catalyst_type
    "holding_period_estimate",  # per catalyst_type
    "search_budget_allocation", # per catalyst_type
    "governor_profit_share",    # cost governor's cap-growth fraction
]

# Placeholder floors pending power analysis (ARCHITECTURE.md section 6.1;
# STRATEGY-PROPOSALS.md section 3.2 argues these are too SMALL, so they
# may only be revised upward without new evidence).
MIN_SAMPLE_SIZE = {
    "conviction_floor": 30,
    "adverse_gap_assumption": 20,
    "stop_width": 20,
    "holding_period_estimate": 15,
    "search_budget_allocation": 40,
    "governor_profit_share": 20,
}

# Tightening moves 3x faster than loosening for the same evidence
# strength. Hard-coded: the system cannot adjust how fast it adjusts.
TIGHTEN_LOOSEN_RATIO = Decimal("3")

MAX_STEP = {
    "conviction_floor": Decimal("0.03"),
    "adverse_gap_assumption": Decimal("0.02"),
    "stop_width": Decimal("0.02"),
    "holding_period_estimate": Decimal("2"),   # days
    "search_budget_allocation": Decimal("0.10"),
    "governor_profit_share": Decimal("0.02"),
}


@dataclass(frozen=True)
class EvidenceSample:
    parameter: str
    trade_ids: tuple[str, ...]      # closed_trades / scored refusals only
    window_start: datetime
    window_end: datetime
    effect_size: Decimal
    significance: Decimal
    evidence_strength: Decimal      # derived ONLY from effect_size +
                                    # significance + sample count


@dataclass(frozen=True)
class AdjustmentProposal:
    parameter: str
    direction: Literal["tighten", "loosen"]
    old_value: Decimal
    proposed_value: Decimal
    evidence: EvidenceSample | None
    applicable: bool
    reason: str | None


def propose_adjustment(parameter: str, evidence: EvidenceSample) -> AdjustmentProposal:
    raise NotImplementedError("stage 5: implemented with the winning strategy's parameters")


def apply(proposal: AdjustmentProposal, hard_bounds: HardBounds, current_snapshot: dict):
    raise NotImplementedError("stage 5")
