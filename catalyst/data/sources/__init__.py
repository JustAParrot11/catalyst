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

#: Separates the recorded diagnosis from the verbatim upstream body in
#: `raw_events_errors.error_text`. One text column carries both, so
#: nothing is added to a table and every existing row still reads (a row
#: without this marker is all body, which is what older rows are).
RAW_RESPONSE_MARKER = "--- the exact response from the server ---"


def error_text(exc) -> str:
    """What goes in ``raw_events_errors.error_text``: the DIAGNOSIS
    first, the verbatim upstream body after it.

    OWNER-REPORTED 2026-09-12: the dashboard's "Feeds that could not be
    read" panel explained a Form 4 failure with a Google WebFont
    JavaScript snippet. Half of that was the panel rendering markup where
    prose belongs. **The other half is here.** The row stored
    ``exc.raw_text`` ALONE:

        stored    '<!DOCTYPE html><html>... 4KB of sec.gov page ...'
        discarded message="HTTP 503 after 4 attempts" status_code=503
                  url=".../form.20260911.idx" attempts=4

    So the status code, the URL and the attempt count were thrown away at
    the point of writing, and no amount of dashboard work could recover
    them - the database simply did not have them. ``FeedError.__str__``
    formats exactly that summary and nothing had ever called it.

    HOUSE RULE 3 IS UNCHANGED: the raw response is still recorded
    verbatim, in full. It moves BELOW a sentence instead of being the
    sentence.

    Takes any exception, because the orchestrator's boundary catches
    ``Exception`` - a transport error from httpx has no ``.message`` and
    must still record something readable.
    """
    name = type(exc).__name__
    # A FeedError's str() embeds the raw body, which is the thing we are
    # separating out, so .message is preferred where it exists. A plain
    # exception's str() IS its message.
    message = " ".join(str(getattr(exc, "message", "") or "").split())
    if not message:
        message = " ".join(str(exc).split())
    if not message:
        # An exception with no text at all still has a type, and the type
        # is the diagnosis. Never return an empty string: "" is what the
        # panel reads as "no detail was returned", which would be a lie.
        message = "(the exception carried no message)"
    facts = []
    for label, attr in (("status", "status_code"), ("attempts", "attempts"),
                        ("url", "url")):
        value = getattr(exc, attr, None)
        if value not in (None, ""):
            facts.append(f"{label}={value}")
    head = f"{name}: {message}"
    if facts:
        head += " (" + " ".join(facts) + ")"
    raw = str(getattr(exc, "raw_text", "") or "")
    if not raw:
        return head
    return f"{head}\n\n{RAW_RESPONSE_MARKER}\n{raw}"


def fault_key(text) -> str:
    """What makes two recorded failures THE SAME failure.

    The dashboard listed every row separately with a hard-coded count of
    ``1``, so the owner's screen showed the same Form 4 failure twice
    with "1" beside each (reported 2026-09-12). "Once" and "forty times"
    are different facts and the count column existed to carry the
    difference; it was decorative.

    The key is the exception type and its message, WITHOUT the per-attempt
    facts - the URL carries the day's date, so keying on the whole line
    would put each day's identical outage in its own group and count 1
    forever. For a row written before the diagnosis was recorded there is
    no first line, so the whole body is the key: template-identical pages
    group, and anything else stays separate, which is the safe direction.
    """
    body = str(text or "")
    head = body.split(RAW_RESPONSE_MARKER, 1)[0].strip()
    if not head or head == body.strip():
        # No diagnosis recorded (an older row, or a body with no marker).
        return " ".join(body.split())[:500]
    # Drop the trailing "(status=... attempts=... url=...)" facts.
    return " ".join(head.rsplit(" (", 1)[0].split())[:500]
