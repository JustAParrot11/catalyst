"""Discovery: RawEvent[] -> Candidate[]. Owner: strategy-analyst.

Which events count as candidates is a strategy decision graded on the
backtest, not a fixed interpretation of a source's schema
(ARCHITECTURE.md section 9.2).
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal


@dataclass(frozen=True)
class Candidate:
    id: str                          # ULID, assigned at discovery time
    ticker: str
    catalyst_type: str               # "earnings_drift", "gap_information", ...
    catalyst_date: date              # best estimate of resolution date
    catalyst_date_confidence: Literal["confirmed", "estimated"]
    source_event_ids: tuple[str, ...]  # RawEvent.source_id chain, for audit
    discovered_at: datetime
    sector: str
    correlation_tags: tuple[str, ...]
