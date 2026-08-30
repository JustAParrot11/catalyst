"""Execution: orders, stops, reconciliation, exits. MONEY-CRITICAL.

Talks to Alpaca. Never sizes - that is risk/'s job alone.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class OrderResult:
    decision_id: str
    broker_order_id: str | None      # None if rejected before an ID was assigned
    status: str
    submitted_at: datetime
    raw_response: dict = field(hash=False)  # broker's verbatim response, ALWAYS -
                                            # including on rejection (house rule 3)


@dataclass(frozen=True)
class Fill:
    order_id: str
    price: Decimal
    qty: Decimal
    filled_at: datetime
    broker_reported_price: Decimal   # kept distinct from any modeled price


@dataclass(frozen=True)
class StopReplacementResult:
    position_id: str
    old_stop_order_id: str | None
    new_stop_order_id: str | None    # never populated unless old stop's
                                     # cancellation was CONFIRMED
    status: Literal["replaced", "failed_cancel_unconfirmed"]
    raw_response: dict = field(hash=False)


@dataclass(frozen=True)
class StopConfirmation:
    position_id: str
    live_stop_order_ids: tuple[str, ...]  # queried fresh from the broker
    status: Literal["ok", "unprotected", "duplicate_stops"]
