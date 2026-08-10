"""Raw usage capture and cent-accurate pricing.

price() is the ONLY function permitted to convert a usage object into a
dollar figure, and it reads every UsageComponents field - there is no
code path that prices from input_tokens/output_tokens alone
(ARCHITECTURE.md section 3.2; TRAPS.md cache-token trap).

reconcile_day() queries the Cost API for exactly ONE closed day at a
time, parses amounts as decimal-string cents, compares per
(date, kind, component) - never pooled - and pauses scheduled
authorization on an unacknowledged discrepancy.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from catalyst.cost import CostEvent
from catalyst.research.schema import UsageComponents


def price(usage: UsageComponents) -> Decimal:
    raise NotImplementedError("stage 3")


def record(event: CostEvent) -> None:
    raise NotImplementedError("stage 3")


def reconcile_day(target_date: date, kind: Literal["scheduled", "manual"], component: str):
    raise NotImplementedError("stage 3")
