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

#: Extra conviction a candidate must carry when the model says the move
#: is already priced in. AN ESTIMATE, and labelled one: nothing has
#: measured what the priced-in call is worth, which is exactly why it is
#: no longer allowed to veto outright.
#:
#: 0.15 on a 0.60 floor puts the bar at 0.75. Against the owner's live
#: conviction distribution (min 0.08, median 0.20, max 0.85, n=30) that
#: admits only the strongest priced-in candidates rather than none of
#: them. It is deliberately the smallest change that reopens the gate.
#:
#: This is the number to move once the refusal tracker can score
#: priced_in refusals: if they go on to earn nothing, raise it back
#: toward a veto; if they earn as much as the rest, take it to zero.
PRICED_IN_CONVICTION_PREMIUM = Decimal("0.15")


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
    # PRICED-IN RAISES THE BAR; IT NO LONGER CLOSES THE DOOR.
    #
    # It used to be an absolute veto placed AHEAD of conviction, so a
    # 0.95-conviction candidate was discarded without conviction ever
    # being read. On the owner's live day it accounted for 26 of 30
    # views and 9 of 12 declines, and the strategy analyst's measurement
    # is that refusing on an unmeasured signal is the most destructive
    # thing that can be attached to this strategy: accepting every
    # signal beat the index by 16.6pp, refusing three quarters of them
    # lost by 59.5pp, and refusing all of them - the live rate - by
    # 68.7pp. A filter needs roughly 60/40 discrimination just to break
    # even against not filtering at all, and this one has never been
    # measured because it is not an adaptive parameter and the refusal
    # tracker aggregates only below_conviction_floor.
    #
    # So it becomes a PREMIUM on conviction. The model's judgement still
    # counts against the trade, deterministic code still decides, the
    # hard bounds are untouched, and a candidate the model is genuinely
    # confident about can now be taken. Every one that clears the raised
    # bar is recorded as having cleared it, so the refusal tracker can
    # eventually say whether the premium should be higher or zero.
    #
    # Owner's instruction, 2026-08-14: "I want an agentic trading bot
    # that can make confident trades doing its own research" and
    # "balance it out so it can also make money confidently". This is a
    # risk-gate change made on that instruction.
    conviction_floor = Decimal(str(params["conviction_floor"]))
    effective_floor = conviction_floor
    if view.priced_in:
        effective_floor = conviction_floor + PRICED_IN_CONVICTION_PREMIUM

    conviction = Decimal(str(view.conviction))
    passed_gate = not skip_reasons and conviction >= effective_floor
    if not skip_reasons and not passed_gate:
        # NAME WHICH BAR IT MISSED. "below_conviction_floor" on a
        # candidate held to a higher floor is true and misleading, and
        # the two need different responses - one is the floor being too
        # high, the other is the premium being too high.
        skip_reasons.append(
            "priced_in_below_raised_floor" if view.priced_in
            else "below_conviction_floor")

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
