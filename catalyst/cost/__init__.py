"""Cost tracking and governor. Owner: cost-auditor audits, this module
measures. Cent-accurate; the number that decides viability."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from catalyst.research.schema import UsageComponents


@dataclass(frozen=True)
class CostEstimate:
    estimated_cents: Decimal
    basis: str
    kind: Literal["scheduled", "manual"]
    component: str


@dataclass(frozen=True)
class CostEvent:
    id: str
    usage: UsageComponents
    kind: Literal["scheduled", "manual"]
    component: str
    priced_cents: Decimal
    priced_at: datetime
    api_call_id: str | None


@dataclass(frozen=True)
class GovernorDecision:
    authorized: bool
    kind: Literal["scheduled", "manual"]
    estimate: CostEstimate
    cap_cents: Decimal
    period_to_date_cents: Decimal    # for this kind only - never pooled
    shortfall_cents: Decimal | None  # populated only when authorized=False
    reason: str | None


@dataclass(frozen=True)
class CostContext:
    kind: Literal["scheduled", "manual"]
    component: str
