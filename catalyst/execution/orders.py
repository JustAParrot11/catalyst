"""Order construction and stop management. HUMAN REVIEW REQUIRED.

replace_stop() is cancel-then-confirm-then-place: the new stop is never
placed unless the old one's cancellation is confirmed - two live stops
on one position is the failure this sequencing exists to prevent.
confirm_stops_resting() runs once per session at the open and queries
the broker directly (not positions.stop_order_id, which can be stale).
Both per ARCHITECTURE.md section 3.2.
"""

from decimal import Decimal

from catalyst.execution import OrderResult, StopConfirmation, StopReplacementResult
from catalyst.risk import RiskDecision


def place(decision: RiskDecision) -> OrderResult:
    raise NotImplementedError("stage 5")


def replace_stop(position, new_stop_price: Decimal) -> StopReplacementResult:
    raise NotImplementedError("stage 5")


def confirm_stops_resting(positions: list) -> list[StopConfirmation]:
    raise NotImplementedError("stage 5")
