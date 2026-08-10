"""One adapter module per source, all with the same shape:

    def fetch_events(since: datetime, until: datetime) -> list[RawEvent]

Fail-soft contract (ARCHITECTURE.md section 3.2): a dead feed returns []
and records why in storage.raw_events_errors. It never raises into the
caller and never stops a run.
"""
