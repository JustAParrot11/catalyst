"""Risk engine - deterministic only. HUMAN REVIEW REQUIRED on all files.

The only module where a number becomes a position size, a stop, or a
kill decision (ARCHITECTURE.md section 2.1).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class LimitApplication:
    rule_name: str
    bound_value: Decimal
    requested_value: Decimal
    bound_type: Literal["hard", "adaptive"]
    binding: bool


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
