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
    max_entry_half_spread_bp: Decimal   # stage-4 market-structure verdict:
                                         # skip entries above this measured
                                         # half-spread; the worst decile of
                                         # C's universe individually breaches
                                         # the pre-registered kill gate
    max_hold_days: int                   # see the note below


HARD_BOUNDS = HardBounds(
    max_loss_per_position_pct=Decimal("0.02"),
    max_total_exposure_pct=Decimal("0.90"),
    max_open_positions=5,
    daily_loss_kill_pct=Decimal("0.04"),
    drawdown_kill_pct=Decimal("0.12"),
    max_correlated_cluster_pct=Decimal("0.35"),
    max_entry_half_spread_bp=Decimal("20"),
    # "HOLD DAYS TO WEEKS, NEVER MONTHS" IS A REQUIREMENT, SO IT NEEDS A
    # BOUND RATHER THAN A STARTING VALUE.
    #
    # Until now that rule was enforced only by holding_period_estimate
    # defaulting to 12 days. But that parameter is ADAPTIVE: it moves up
    # to 2 days per adjustment on measured evidence, and nothing here
    # capped it. Enough adjustments in one direction and a "days to
    # weeks" strategy quietly becomes a multi-month one - which is
    # exactly the two-tier failure the brief describes, where a lucky
    # run loosens a limit that then meets an unlucky one.
    #
    # 31 days, because the owner's own words are "weeks or at most a
    # month". A position still open at the bound is closed regardless of
    # what anything believes about it. Raising this is a human decision
    # with risk-reviewer sign-off, like every other bound in this file.
    max_hold_days=31,
)
