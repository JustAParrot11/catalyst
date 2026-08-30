"""Position sizing - the ONLY function permitted to construct a
notional_usd or qty value. MONEY-CRITICAL.

The signature is the enforcement (ARCHITECTURE.md section 4.1 third
layer): the sole trace of the model's output that can reach this
function is passed_gate, a bool. There is no parameter shaped to
receive a ResearchView, so no future edit can read conviction,
expected_holding_days or priced_in into the arithmetic.

Sizing arithmetic (ARCHITECTURE section 4.4):
  worst_case_pct = max(adverse_gap_assumption[catalyst_type], stop_width)
  - never nominal stop distance alone: stops do not trigger outside
  regular hours and fractional DAY stops expire at the close (TRAPS.md),
  so the overnight gap is the real worst case.
  risk_budget = equity * max_loss_per_position_pct  (hard bound)
  notional = risk_budget / worst_case_pct, then clamped by every limit
  below, each recorded as a LimitApplication with a binding flag.
"""

from decimal import ROUND_DOWN, Decimal

from catalyst.risk import stock_gap  # noqa: F401 - see size()
from catalyst.risk import LimitApplication, MarketSnapshot, PortfolioState, SizingResult
from catalyst.risk.hard_bounds import HardBounds

_BP = Decimal("10000")


def size(
    passed_gate: bool,
    catalyst_type: str,
    portfolio: PortfolioState,
    params: dict,
    hard_bounds: HardBounds,
    market: MarketSnapshot,
    cluster_key: str = "",
    bars_dir: str | None = None,
) -> SizingResult:
    skip_reasons: list[str] = []
    limits: list[LimitApplication] = []

    if not passed_gate:
        return SizingResult(action="skip", notional_usd=None, qty=None,
                            stop_price=None, limits_applied=(),
                            skip_reasons=("gate_not_passed",))

    # Hard spread gate (stage-4 market-structure verdict, a hard bound):
    # the worst decile of the measured universe individually breaches the
    # pre-registered kill condition; entering there pays the edge away.
    if market.half_spread_bp > hard_bounds.max_entry_half_spread_bp:
        limits.append(LimitApplication(
            rule_name="max_entry_half_spread_bp",
            bound_value=hard_bounds.max_entry_half_spread_bp,
            requested_value=market.half_spread_bp,
            bound_type="hard", binding=True))
        return SizingResult(action="skip", notional_usd=None, qty=None,
                            stop_price=None, limits_applied=tuple(limits),
                            skip_reasons=("spread_gate",))

    # Position-count hard bound.
    if len(portfolio.open_positions) >= hard_bounds.max_open_positions:
        limits.append(LimitApplication(
            rule_name="max_open_positions",
            bound_value=Decimal(hard_bounds.max_open_positions),
            requested_value=Decimal(len(portfolio.open_positions) + 1),
            bound_type="hard", binding=True))
        return SizingResult(action="skip", notional_usd=None, qty=None,
                            stop_price=None, limits_applied=tuple(limits),
                            skip_reasons=("no_free_slot",))

    stop_width = Decimal(str(params["stop_width"][catalyst_type]))
    adverse_gap = Decimal(str(params["adverse_gap_assumption"][catalyst_type]))

    # SIZE AGAINST THIS STOCK, NOT JUST ITS CATALYST'S CATEGORY.
    #
    # Measured over 8.4m overnight gaps: the median ticker's worst day in
    # a decade is 18.6%, the most volatile decile's is 35.3%. A single
    # category number sizes a $40bn pharma exactly like a $50m microcap,
    # and cuts the large one to under a third of what its own history
    # justifies - silently.
    #
    # Per-stock evidence can only ever TIGHTEN: both helpers take the
    # category value as a ceiling and fall back to it on any doubt, so
    # this can make a position smaller than the category says but never
    # larger. Hard bounds below are untouched and still apply.
    if bars_dir:
        adverse_gap, gap_why = stock_gap.effective_gap(
            catalyst_type, adverse_gap, bars_dir, market.ticker)
        stop_width, stop_why = stock_gap.effective_stop(
            catalyst_type, stop_width, bars_dir, market.ticker)
        limits.append(LimitApplication(
            rule_name="per_stock_adverse_gap", bound_value=adverse_gap,
            requested_value=Decimal(str(
                params["adverse_gap_assumption"][catalyst_type])),
            bound_type="adaptive", binding=True, note=gap_why))
        limits.append(LimitApplication(
            rule_name="per_stock_stop_width", bound_value=stop_width,
            requested_value=Decimal(str(params["stop_width"][catalyst_type])),
            bound_type="adaptive", binding=True, note=stop_why))

    worst_case_pct = max(adverse_gap, stop_width)

    risk_budget = portfolio.equity_usd * hard_bounds.max_loss_per_position_pct
    raw_notional = risk_budget / worst_case_pct
    limits.append(LimitApplication(
        rule_name="max_loss_per_position",
        bound_value=hard_bounds.max_loss_per_position_pct,
        requested_value=worst_case_pct,
        bound_type="hard", binding=False))

    notional = raw_notional

    # Equal-weight slot ceiling (mirrors the backtest's accounting).
    slot_ceiling = portfolio.equity_usd / Decimal(hard_bounds.max_open_positions)
    if notional > slot_ceiling:
        limits.append(LimitApplication(
            rule_name="equal_weight_slot", bound_value=slot_ceiling,
            requested_value=notional, bound_type="adaptive", binding=True))
        notional = slot_ceiling

    # Total-exposure hard bound.
    deployed = sum((p.notional_usd for p in portfolio.open_positions), Decimal("0"))
    exposure_room = portfolio.equity_usd * hard_bounds.max_total_exposure_pct - deployed
    if notional > exposure_room:
        limits.append(LimitApplication(
            rule_name="max_total_exposure", bound_value=exposure_room,
            requested_value=notional, bound_type="hard", binding=True))
        notional = exposure_room

    # Correlated-cluster hard bound: same cluster_key positions are one bet.
    cluster_deployed = sum(
        (p.notional_usd for p in portfolio.open_positions
         if cluster_key and p.cluster_key == cluster_key), Decimal("0"))
    cluster_room = (portfolio.equity_usd * hard_bounds.max_correlated_cluster_pct
                    - cluster_deployed)
    if cluster_key and notional > cluster_room:
        limits.append(LimitApplication(
            rule_name="max_correlated_cluster", bound_value=cluster_room,
            requested_value=notional, bound_type="hard", binding=True))
        notional = cluster_room

    # Settled cash (cash account, T+1 - TRAPS/bake-off constraint).
    if notional > portfolio.settled_cash_usd:
        limits.append(LimitApplication(
            rule_name="settled_cash", bound_value=portfolio.settled_cash_usd,
            requested_value=notional, bound_type="hard", binding=True))
        notional = portfolio.settled_cash_usd

    if notional <= Decimal("1"):
        return SizingResult(action="skip", notional_usd=None, qty=None,
                            stop_price=None, limits_applied=tuple(limits),
                            skip_reasons=("notional_below_minimum",))

    qty = (notional / market.last_close).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    stop_price = (market.last_close * (Decimal("1") - stop_width)).quantize(Decimal("0.01"))

    return SizingResult(
        action="trade",
        notional_usd=notional.quantize(Decimal("0.01")),
        qty=qty,
        stop_price=stop_price,
        limits_applied=tuple(limits),
        skip_reasons=(),
    )
