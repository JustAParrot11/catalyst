"""Risk engine - deterministic only. HUMAN REVIEW REQUIRED on all files.

The only module where a number becomes a position size, a stop, or a
kill decision (ARCHITECTURE.md section 2.1).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class LimitApplication:
    rule_name: str
    bound_value: Decimal
    requested_value: Decimal
    bound_type: Literal["hard", "adaptive"]
    binding: bool
    #: Why this bound landed where it did, in a sentence. "Why is this
    #: position that size" is the question the decision page exists to
    #: answer, and a rule name plus two numbers cannot answer it once
    #: the bound is derived from the stock's own history rather than
    #: from a constant. Optional, so every existing construction and
    #: every stored row stays valid.
    note: str = ""


@dataclass(frozen=True)
class SizingResult:
    action: Literal["trade", "skip"]
    notional_usd: Decimal | None
    qty: Decimal | None
    stop_price: Decimal | None
    limits_applied: tuple[LimitApplication, ...]
    skip_reasons: tuple[str, ...]


@dataclass(frozen=True)
class RiskDecision:
    candidate_id: str
    action: Literal["trade", "skip"]
    side: Literal["long", "short"] | None
    notional_usd: Decimal | None
    qty: Decimal | None
    stop_price: Decimal | None
    planned_exit_date: date | None
    limits_applied: tuple[LimitApplication, ...]
    skip_reasons: tuple[str, ...]
    adaptive_params_snapshot: dict


@dataclass(frozen=True)
class KillSwitchState:
    tripped: bool
    reason: str | None


@dataclass(frozen=True)
class OpenPosition:
    position_id: str
    ticker: str
    notional_usd: Decimal
    cluster_key: str
    opened_at_date: date
    planned_exit_date: date


@dataclass(frozen=True)
class PortfolioState:
    """Built ONLY from a confirmed broker read plus local position rows.
    reliable=False (or a stale as_of) makes kill_switches fail closed."""

    equity_usd: Decimal
    settled_cash_usd: Decimal
    open_positions: tuple[OpenPosition, ...]
    day_pnl_usd: Decimal
    peak_equity_usd: Decimal          # for drawdown
    consecutive_losses: int
    as_of: datetime
    reliable: bool                    # False = broker read failed/stale


@dataclass(frozen=True)
class MarketSnapshot:
    """Per-candidate market context, independent of anything Claude said."""

    ticker: str
    last_close: Decimal
    half_spread_bp: Decimal           # measured, from live NBBO at decision time
    median_daily_dollar_volume: Decimal
    #: What the tape has already done, from the cached daily bars. The
    #: evidence for "is this already priced in", which the model was
    #: previously asked to judge with only today's close in front of it.
    #: Optional so every existing construction stays valid, and None
    #: means NOT MEASURED rather than nothing happened.
    price_action: object = None
    #: What the cached history says about this live quote. Every traded
    #: number descends from one Alpaca reading; this is the only thing
    #: that ever disagrees with it.
    quote_check: object = None

