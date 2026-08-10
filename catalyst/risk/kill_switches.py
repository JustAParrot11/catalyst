"""Kill switches - checked once per cycle BEFORE any candidate is
evaluated. HUMAN REVIEW REQUIRED.

FAILS CLOSED (ARCHITECTURE sections 3.2 / 9.14): if portfolio state
cannot be trusted - broker read failed, or the snapshot is stale -
check() trips rather than proceeding. Losing exactly when something is
already wrong is the failure mode this closes off.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from catalyst.risk import KillSwitchState, PortfolioState
from catalyst.risk.hard_bounds import HardBounds

MAX_SNAPSHOT_AGE = timedelta(minutes=10)
MAX_CONSECUTIVE_LOSSES = 5   # human-set circuit breaker, never adaptive


def check(portfolio: PortfolioState | None, hard_bounds: HardBounds,
          now: datetime | None = None) -> KillSwitchState:
    """`now` is injectable so staleness is judged against the caller's
    clock (the orchestrator passes its cycle time); the wall-clock
    default remains the fail-closed path for direct callers."""
    if portfolio is None or not portfolio.reliable:
        return KillSwitchState(tripped=True, reason="portfolio_state_unreliable")

    age = (now or datetime.now(timezone.utc)) - portfolio.as_of
    if age > MAX_SNAPSHOT_AGE:
        return KillSwitchState(tripped=True, reason="portfolio_state_stale")

    if portfolio.equity_usd <= Decimal("0"):
        return KillSwitchState(tripped=True, reason="equity_nonpositive")

    daily_loss_limit = portfolio.equity_usd * hard_bounds.daily_loss_kill_pct
    if portfolio.day_pnl_usd < -daily_loss_limit:
        return KillSwitchState(tripped=True, reason="daily_loss_kill")

    if portfolio.peak_equity_usd > 0:
        drawdown = (portfolio.peak_equity_usd - portfolio.equity_usd) / portfolio.peak_equity_usd
        if drawdown > hard_bounds.drawdown_kill_pct:
            return KillSwitchState(tripped=True, reason="drawdown_kill")

    if portfolio.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        return KillSwitchState(tripped=True, reason="consecutive_losses_kill")

    return KillSwitchState(tripped=False, reason=None)
