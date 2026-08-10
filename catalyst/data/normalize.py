"""Raw source payloads -> RawEvent. No interpretation happens here."""

from datetime import datetime

from catalyst.data import RawEvent


def normalize(source: str, source_id: str, payload: dict, fetched_at: datetime) -> RawEvent:
    return RawEvent(
        source=source,
        source_id=source_id,
        fetched_at=fetched_at,
        payload_raw=payload,
    )
