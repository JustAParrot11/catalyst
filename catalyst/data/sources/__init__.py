"""One adapter module per source, all with the same shape:

    def fetch_events(since: datetime, until: datetime,
                     http_get=None) -> list[RawEvent]

Failure contract, revised at stage 5 (supersedes the original
"return [] and never raise" wording; ARCHITECTURE.md section 3.2):
a dead feed RAISES the module's FeedError carrying the raw upstream
response/error text. It never returns [] for a failure - an empty list
means "the source answered and there was nothing", and conflating the
two is exactly the silent-zero the build brief forbids. The orchestrator
(cycle.run_cycle) is the fail-soft boundary: it catches FeedError,
records the raw text in storage.raw_events_errors, and reports the
funnel stage as feed_unreachable rather than empty.
"""
