"""Hard bounds - human-authored, human-reviewed, NO runtime writer.

These exist to prevent ruin. The system may only ever PROPOSE changing
them (adaptive_params.propose_adjustment returns a proposal for a human
to read); it cannot apply that proposal itself. No function in
adaptive_params.py imports this module as a writable target - there is
no setter to call (ARCHITECTURE.md section 6.1-6.2).

Changing a value below requires a human-authored PR that a human merges,
with risk-reviewer sign-off (section 8 ownership table).

The literal values are placeholders pending backtest evidence
(ARCHITECTURE.md section 12: "human decides, not this document").
Deliberately conservative until then.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class HardBounds:
    max_loss_per_position_pct: Decimal   # of account equity
    max_total_exposure_pct: Decimal
    max_open_positions: int
    daily_loss_kill_pct: Decimal
    drawdown_kill_pct: Decimal
    max_correlated_cluster_pct: Decimal


HARD_BOUNDS = HardBounds(
    max_loss_per_position_pct=Decimal("0.02"),
    max_total_exposure_pct=Decimal("0.90"),
    max_open_positions=5,
    daily_loss_kill_pct=Decimal("0.04"),
    drawdown_kill_pct=Decimal("0.12"),
    max_correlated_cluster_pct=Decimal("0.35"),
)
