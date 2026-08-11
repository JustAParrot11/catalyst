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
from catalyst.risk import (
    LimitApplication, MarketSnapshot, PortfolioState, RiskDecision,
)
from catalyst.risk.hard_bounds import HARD_BOUNDS
from catalyst.risk.sizing import size

# Cash account, confirmed live: shorting_enabled=false. A short view is
# an automatic skip recorded with its own reason so the refusal tracker
# measures what the constraint costs (STRATEGY-PROPOSALS section 2.1).
_SHORT_SKIP = "short_unavailable_cash_account"


def _hold_days(view: ResearchView, params: dict,
               catalyst_type: str) -> tuple[int | None, str]:
    """(days, why) for this position's hard exit date.

    THE MODEL PROPOSES THE DATE; CODE DISPOSES OF IT. The owner asked
    for Claude to choose the holding period rather than have one
    imposed - it has read the thesis and knows when the catalyst
    resolves, which a per-catalyst-type average cannot. So
    expected_holding_days is now used, where before it was recorded and
    ignored.

    But it is CLAMPED, and the clamp is the whole point:

      * never beyond HARD_BOUNDS.max_hold_days. A persuasive thesis
        asking for six months does not get six months. This is the one
        direction where the model's own confidence is most dangerous,
        because a losing position always has a story attached.
      * never below one day, so a zero or a nonsense value cannot
        produce an exit date in the past and a position that closes the
        instant it opens.

    A view with no usable number falls back to the adaptive per-catalyst
    estimate, which is what this always used. Returning the REASON
    alongside the number matters: the dashboard has to be able to say
    whether the date came from the model or from the fallback, and the
    brief requires every trade to be reconstructable.
    """
    estimate = params.get("holding_period_estimate", {}).get(catalyst_type)
    fallback = int(estimate) if estimate is not None else None
    cap = int(HARD_BOUNDS.max_hold_days)

    proposed = getattr(view, "expected_holding_days", None)
    if not isinstance(proposed, int) or isinstance(proposed, bool) or proposed < 1:
        if fallback is None:
            return None, "no_holding_estimate_for_this_catalyst_type"
        return min(fallback, cap), (
            f"model gave no usable holding period; used the measured "
            f"{catalyst_type} estimate of {fallback} day(s)")
    if proposed > cap:
        return cap, (
            f"model asked for {proposed} days; CLAMPED to the {cap}-day "
            "hard bound, which the system cannot raise")
    return proposed, f"model's own estimate of {proposed} day(s)"


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
    # KeyError inside sizing and kill the cycle (stress ESCALATION-6).
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

    holding, holding_basis = _hold_days(view, params, candidate.catalyst_type)
    planned_exit = (portfolio.as_of.date() + timedelta(days=holding)
                    if holding is not None else None)

    if sized.action == "skip":
        skip_reasons.extend(r for r in sized.skip_reasons if r != "gate_not_passed")

    # THE HOLD IS A LIMIT LIKE ANY OTHER, so it is recorded as one. That
    # puts it in limit_applications, which means the decision trace and
    # the funnel already show when the model asked for a longer hold
    # than it was allowed - "where the code overruled the model must be
    # visible and explained" (BUILD-BRIEF), without a schema change.
    limits = list(sized.limits_applied)
    if sized.action == "trade" and holding is not None:
        asked = getattr(view, "expected_holding_days", None)
        asked = asked if isinstance(asked, int) and not isinstance(asked, bool) \
            else holding
        limits.append(LimitApplication(
            rule_name="max_hold_days",
            bound_value=Decimal(str(HARD_BOUNDS.max_hold_days)),
            requested_value=Decimal(str(asked)),
            bound_type="hard",
            binding=asked > int(HARD_BOUNDS.max_hold_days),
        ))

    return RiskDecision(
        candidate_id=candidate.id,
        action=sized.action,
        side="long" if sized.action == "trade" else None,
        notional_usd=sized.notional_usd,
        qty=sized.qty,
        stop_price=sized.stop_price,
        planned_exit_date=planned_exit if sized.action == "trade" else None,
        limits_applied=tuple(limits),
        skip_reasons=tuple(skip_reasons),
        adaptive_params_snapshot=dict(
            params, holding_period_basis=holding_basis),
    )
