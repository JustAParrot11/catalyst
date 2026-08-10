"""Data layer: fetching, normalizing, rate-limiting per source.

Owner: data-engineer. Returns RawEvent, source-agnostic. Decides nothing
about tradeability - that is discovery's job (ARCHITECTURE.md section 2.1).
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class RawEvent:
    """One upstream record, verbatim. Evidence, not a claim."""

    source: str          # "federal_register", "edgar", ...
    source_id: str       # source's own identifier, for de-dup
    fetched_at: datetime
    payload_raw: dict = field(hash=False)
