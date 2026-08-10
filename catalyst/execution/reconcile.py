"""Fill reconciliation against broker state. HUMAN REVIEW REQUIRED.

broker_reported_price is recorded beside any modeled price, never
instead of it (TRAPS.md: paper fills pay no spread).
"""

from catalyst.execution import Fill


def reconcile() -> list[Fill]:
    raise NotImplementedError("stage 5")
