"""The single gate every candidate passes through. HUMAN REVIEW REQUIRED.

Reads exactly one field of the ResearchView for anything that reaches
sizing: conviction, compared against the adaptive conviction floor to
produce passed_gate (a bool). direction and priced_in gate trade/no-
trade as booleans too - but NOTHING from the view reaches sizing's
arithmetic; sizing.size() has no parameter that can receive it
(ARCHITECTURE section 4.3).
"""

from datetime import timedelta
from decimal import Decimal

from catalyst.discovery import Candidate
from catalyst.research.schema import ResearchView
from catalyst.risk import MarketSnapshot, PortfolioState, RiskDecision
from catalyst.risk.hard_bounds import HARD_BOUNDS
from catalyst.risk.sizing import size

# Cash account, confirmed live: shorting_enabled=false. A short view is
# an automatic skip recorded with its own reason so the refusal tracker
# measures what the constraint costs (STRATEGY-PROPOSALS section 2.1).
_SHORT_SKIP = "short_unavailable_cash_account"


def evaluate(
    candidate: Candidate,
    view: ResearchView,
    portfolio: PortfolioState,
    params: dict,
    market: MarketSnapshot,
    cluster_key: str = "",
) -> RiskDecision:
    skip_reasons: list[str] = []

    # A catalyst_type with no configured parameters must SKIP, not
    # KeyError inside sizing and kill the cycle (stress escalation 9).
    for param_name in ("adverse_gap_assumption", "stop_width",
                       "holding_period_estimate"):
        if candidate.catalyst_type not in params[param_name]:
            skip_reasons.append("unknown_catalyst_type")
            break

    if view.direction == "no_trade":
        skip_reasons.append("model_no_trade")
    elif view.direction == "short":
        skip_reasons.append(_SHORT_SKIP)
    if view.priced_in:
        skip_reasons.append("model_judged_priced_in")

    conviction_floor = Decimal(str(params["conviction_floor"]))
    passed_gate = (
        not skip_reasons
        and Decimal(str(view.conviction)) >= conviction_floor
    )
    if not skip_reasons and not passed_gate:
        skip_reasons.append("below_conviction_floor")

    sized = size(
        passed_gate=passed_gate,
        catalyst_type=candidate.catalyst_type,
        portfolio=portfolio,
        params=params,
        hard_bounds=HARD_BOUNDS,
        market=market,
        cluster_key=cluster_key,
    )

    holding = params["holding_period_estimate"].get(candidate.catalyst_type)
    planned_exit = (portfolio.as_of.date() + timedelta(days=int(holding))
                    if holding is not None else None)

    if sized.action == "skip":
        skip_reasons.extend(r for r in sized.skip_reasons if r != "gate_not_passed")

    return RiskDecision(
        candidate_id=candidate.id,
        action=sized.action,
        side="long" if sized.action == "trade" else None,
        notional_usd=sized.notional_usd,
        qty=sized.qty,
        stop_price=sized.stop_price,
        planned_exit_date=planned_exit if sized.action == "trade" else None,
        limits_applied=sized.limits_applied,
        skip_reasons=tuple(skip_reasons),
        adaptive_params_snapshot=dict(params),
    )
