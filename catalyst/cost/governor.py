"""Pre-call spend authorization. Expected profit never authorizes spend.

Scheduled cap: base $5/month hard (BUILD-BRIEF.md), rising only by a
fraction of NET realized monthly profit (floored at zero for the month
as a whole, not per trade - ARCHITECTURE.md section 9.13).

Manual cap: separate, human-set, non-adaptive. "Tracked separately"
does not mean unbounded (section 7.2) - the judgement-mode backtest
routes through this branch.
"""

from decimal import Decimal

from catalyst.cost import CostEstimate, GovernorDecision

BASE_CAP_CENTS = Decimal("500")                   # $5/month, hard (BUILD-BRIEF.md)
MANUAL_SPEND_CAP_CENTS_PER_MONTH = Decimal("2000")  # $20/month for testing/backtests;
                                                     # human-set, never adaptive


def authorize(estimate: CostEstimate) -> GovernorDecision:
    raise NotImplementedError("stage 3")
