"""Live EDGAR Form 4 feed — the forward-looking sibling of
``scripts/fetch_insider_data.py``.

The bake-off's Candidate C (insider-cluster open-market buying) was graded
on the SEC's quarterly *Insider Transactions data sets*. Those are
published ~45-90 days after quarter end, so they cannot drive a live
pipeline: on 2026-08-10 the newest set is ``2026q1``. This module is the
live equivalent — same fields, same semantics, sourced from filings the
day they are disseminated.

Endpoint chosen: the **EDGAR daily index**, plus one fetch of each Form 4
submission's full text file.

    https://www.sec.gov/Archives/edgar/daily-index/{Y}/QTR{n}/form.{YYYYMMDD}.idx
    https://www.sec.gov/Archives/edgar/data/{cik}/{accession}.txt

Why not ``efts.sec.gov`` full-text search (all figures measured
2026-08-10, see the module's verification notes below):

* EFTS cannot *enumerate*. A blank query returns HTTP 200 carrying
  ``{"error": "Blank search not valid..."}`` (DATA-SOURCES.md section 2.8),
  so every EFTS call is a text match, not a population. ``q="purchase"
  &forms=4`` over 2026-08-05..06 returned ``hits.total.value = 116`` —
  a keyword subset of the day's filings, and transaction code ``P`` is
  not expressible as a text query at all. Building a strategy on "the
  Form 4s that happen to contain a word" silently drops signal.
* The daily index *is* the dissemination feed. One request returned
  every Form 4 for 2026-08-06: 1,161 index rows → **562 unique
  accessions** (each accession is listed once per CIK involved — issuer
  and each reporting owner — so de-duplicating by accession halves the
  work).
* Cost per day: 1 index request + 562 submission requests ≈ 563
  requests ≈ 113s at this module's default 5 req/s, ~5 MB (mean
  submission size 9,498 bytes over a 120-filing sample). Free, keyless.
* EFTS is still the better tool for *finding* a filing by text; it is the
  wrong tool for a daily sweep.

Verified against the live API on 2026-08-10 (sample: the 2026-08-06
daily index and 200 submissions from it):

* ``form.20260806.idx`` → 200, 1,334,559 bytes, 6,864 lines, 1,161 Form 4
  rows (+15 ``4/A``), fixed-width ``Form Type | Company Name | CIK |
  Date Filed | File Name``.
* A submission text file carries the SGML header *and* the
  ``<ownershipDocument>`` XML inline, so one request gets both. There is
  exactly one ``<ownershipDocument>`` per submission (120/120 sampled).
  No XML namespace.
* ``<ACCEPTANCE-DATETIME>20260806202933`` in the header is the
  point-in-time truth (ARCHITECTURE / DATA-SOURCES.md section 2.2): a
  filing accepted after the close is not tradable until the next open.
  ``FILED AS OF DATE`` alone hands a backtest a free session.
* Transaction codes over a 80-filing sample: S 83, A 43, C 24, D 21,
  M 19, **P 12**, J 12, F 11, G 1. Over a separate 120-filing sample,
  9/120 submissions contained a ``P``. So expect **roughly 40-60 code-P
  submissions a day** before any size or liquidity filtering — this is
  a measured rate on one day, not a long-run average.
* ``<aff10b5One>`` — the 10b5-1 plan flag the strategy excludes on —
  appears with **four different spellings**: ``0`` (84), ``false`` (20),
  ``1`` (15), ``true`` (1) in that 120-filing sample. Booleans in
  ``<reportingOwnerRelationship>`` mix both styles *within a single
  filing* (accession 0001231919-26-000838 has one owner using
  ``true``/``false`` and another using ``1``/``0``). Anything that tests
  ``== "1"`` silently reads every ``true`` filing as "not a plan trade".
* The element only exists from ~2023 (STRATEGY-BAKEOFF.md section 3 C),
  and plan trades are also disclosed in free-text footnotes, so this
  module reports both signals — ``element`` and ``footnote_mention`` —
  and refuses to collapse them into one boolean here. Excluding a trade
  is discovery's decision, not the feed's.

Two traps this module handles that the docs get wrong:

1. **A missing daily index returns 403, not 404.** DATA-SOURCES.md
   section 2.4 says "a 404 here is normal". Measured on 2026-08-10:
   ``form.20260808.idx`` (a Saturday) and ``form.20260810.idx`` (today,
   before the evening publish) both return **HTTP 403** with an S3 body
   ``<Error><Code>AccessDenied</Code>...``. A feed that treats every 403
   as an IP block screams every weekend; a feed that treats every 403 as
   "no file" would swallow a real block. This module distinguishes them
   by body: ``AccessDenied``/``NoSuchKey`` means the file is not there
   (recorded as a missing date, with the raw body kept), anything else
   403 is fatal and raises.
2. **Rate limit is 10 req/s across *all* SEC APIs** and an overrun blocks
   the IP for every other SEC feed in this process (TRAPS.md). The
   limiter here is real code with an injectable clock, defaults to 5
   req/s to leave headroom for other SEC callers, and refuses to be
   configured above 10.

Failure contract — note the deliberate divergence from
``data/sources/__init__.py``'s "a dead feed returns []". A network
failure and a quiet market are the same empty list, and telling them
apart is repeatedly the whole diagnosis (BUILD-BRIEF.md). So:

* Anything that means "we could not talk to EDGAR" raises ``FeedError``
  carrying the URL, status and the verbatim upstream body. The caller
  (orchestrator) writes it to ``storage.raw_events_errors`` and carries
  on with the other feeds — the fail-soft boundary moves up one level,
  it does not disappear.
* An empty window that really is empty returns ``[]`` *and* a
  ``FetchResult`` naming every date that was missing with the raw 403
  body beside it (house rule 3: every zero gets its raw response).
"""

from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Protocol, Sequence

from catalyst.data import RawEvent

SOURCE = "edgar_form4"

ARCHIVES_BASE = "https://www.sec.gov/Archives/"
DAILY_INDEX_URL = (
    ARCHIVES_BASE + "edgar/daily-index/{year}/QTR{qtr}/form.{stamp}.idx"
)

#: SEC's published ceiling across *every* SEC API (TRAPS.md). Never exceed.
SEC_MAX_REQUESTS_PER_SEC = 10.0
#: Default pace. Deliberately below the ceiling: other SEC feeds in this
#: process share the same IP budget, and an overrun blocks all of them.
DEFAULT_REQUESTS_PER_SEC = 5.0

DEFAULT_CONTACT_EMAIL = "billysawyer0@gmail.com"

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
#: S3 says this when the object does not exist; EDGAR's archive is S3
#: fronted, so an absent daily-index file arrives as 403 + AccessDenied.
_ABSENT_MARKERS = ("AccessDenied", "NoSuchKey")

_TEN_B5_1_RE = re.compile(r"10\s*b5[\s\-‑–]*1", re.IGNORECASE)

#: HOW LONG AN ABSENT DAILY INDEX IS BELIEVED TO STAY ABSENT.
#:
#: OWNER'S BUNDLE, 2026-08-24: 252 requests to sec.gov in one day, all
#: 403, for three dates - Saturday the 22nd (93 times), Sunday the 23rd
#: (93 times) and the 24th before its evening publish (66 times). Every
#: one was correctly understood as "no file yet" and none was an error;
#: the cost is that the bot spent 252 requests of a rate limit TRAPS.md
#: warns is shared across all SEC APIs, where an overrun blocks the IP
#: for every SEC feed in the process.
#:
#: A cool-off, never a permanent skip: today's index really does appear
#: in the evening, so a date must always be asked again eventually. Half
#: an hour turns 93 requests a day per date into about 48, and the
#: longest a newly published index can go unnoticed is one cool-off.
ABSENT_RECHECK_SECONDS = 1800.0

#: date -> monotonic time it last answered "not there". Module level for
#: the same reason sec_pacer() is: the pacing budget belongs to the
#: process, not to one call.
_absent_since: dict[date, float] = {}

#: A PUBLISHED DAILY INDEX FOR A PAST DAY NEVER CHANGES AGAIN.
#:
#: OWNER'S BUNDLE, 2026-08-24, found by grouping the log's 4,903 HTTP
#: lines by endpoint: 306 successful fetches of the daily index across
#: FOUR dates - about 76 downloads each in a day. The submissions behind
#: them are cached in `edgar_filings` and were replayed for free; the
#: index in front of them was re-downloaded every fifteen minutes. At
#: the measured 1.3 MB per file that is roughly 400 MB a day pulled from
#: sec.gov for bytes that cannot have changed.
#:
#: TODAY IS NEVER CACHED. The daily index publishes in the evening and
#: filings keep arriving until it does, so only a date strictly before
#: the caller's own clock is treated as final. Process memory only: a
#: restart re-fetches, so nothing here can go permanently stale.
#:
#: Keyed by (date, forms) because the parse is form-filtered, and bounded
#: so a long-running process cannot grow without limit.
INDEX_CACHE_MAX_DAYS = 32
_index_cache: "dict[tuple[date, tuple[str, ...]], list[IndexRow]]" = {}


def clear_absent_index_memo() -> None:
    """Forget which dates answered absent, and every cached index. For
    tests, and for anything that wants the next pass to ask again
    regardless."""
    _absent_since.clear()
    _index_cache.clear()


def _remember_index(day: date, forms: tuple, rows: list) -> None:
    _index_cache[(day, forms)] = rows
    while len(_index_cache) > INDEX_CACHE_MAX_DAYS:
        # Oldest DATE first, not oldest insertion: the window walks
        # forward, so the earliest day is the one least likely to be
        # asked for again.
        del _index_cache[min(_index_cache, key=lambda k: k[0])]


class HttpResponse(Protocol):
    """The slice of ``httpx.Response`` this module uses."""

    status_code: int
    text: str


#: ``http_get(url, headers) -> HttpResponse``. Injected so tests never
#: touch a socket.
HttpGet = Callable[[str, dict], HttpResponse]


class RateLimitBlocked(RuntimeError):
    """SEC.gov has rate-limited this IP. A GLOBAL condition, not a
    per-request failure.

    Raised on the block page, and deliberately NOT a FeedError subclass:
    every per-filing loop in this module catches FeedError and continues
    to the next filing, which is exactly the wrong response here. The
    SEC's own notice says so:

        "Continuing to exceed the SEC's maximum allowable request rate
         during the time-out period will extend the duration of the
         time-out period."

    So a block must abort the whole pass and start a cooldown, not carry
    on through 2,800 more filings extending the ban with each one.
    Owner-reported 2026-08-11, live, after exactly that.
    """

    def __init__(self, message: str, raw_text: str = ""):
        super().__init__(message)
        self.raw_text = raw_text


#: The SEC's stated timeout, plus margin. Requests during the block
#: extend it, so the margin is not politeness - it is the difference
#: between a ten-minute outage and an open-ended one.
BLOCK_COOLDOWN_SECONDS = 15 * 60

#: Markers in SEC.gov's own block pages.
_BLOCK_MARKERS = (
    "Request Rate Threshold Exceeded",
    "Exceeded the SEC",
    "Undeclared Automated Tool",
    "your request rate has exceeded",
)


def looks_rate_limited(status: int, body: str) -> bool:
    """Is this the SEC saying "you are blocked"?

    Matched on the BODY rather than the status, because the block page
    has arrived as 403 and as 200 depending on which edge served it.
    """
    text = (body or "")
    return any(m.lower() in text.lower() for m in _BLOCK_MARKERS)


class FeedError(RuntimeError):
    """A source could not be read. Carries the raw upstream evidence.

    Never raised for "there was nothing to fetch" — only for "we could
    not fetch". ``raw_text`` is the verbatim upstream body (or the
    exception text for a transport failure) so the row written to
    ``raw_events_errors`` can be read without re-running anything.
    """

    def __init__(
        self,
        message: str,
        *,
        source: str = SOURCE,
        url: str | None = None,
        status_code: int | None = None,
        raw_text: str = "",
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.source = source
        self.url = url
        self.status_code = status_code
        self.raw_text = raw_text
        self.attempts = attempts

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return (
            f"[{self.source}] {self.message} "
            f"(url={self.url} status={self.status_code} attempts={self.attempts}) "
            f"raw={self.raw_text[:500]!r}"
        )


class Form4ParseError(ValueError):
    """One submission could not be parsed. Not fatal to the run."""


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


class RateLimiter:
    """Minimum-interval limiter. Clock and sleep are injectable.

    Not a token bucket on purpose: a bucket permits a burst, and a burst
    at 10 req/s is exactly what gets the IP blocked. This spaces every
    request by ``1 / rate`` with no burst allowance.
    """

    def __init__(
        self,
        rate_per_sec: float = DEFAULT_REQUESTS_PER_SEC,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        if rate_per_sec > SEC_MAX_REQUESTS_PER_SEC:
            raise ValueError(
                f"rate_per_sec={rate_per_sec} exceeds the SEC ceiling of "
                f"{SEC_MAX_REQUESTS_PER_SEC}/s; an overrun temporarily blocks "
                "this IP for every SEC API at once (TRAPS.md)"
            )
        self.rate_per_sec = float(rate_per_sec)
        self.interval = 1.0 / float(rate_per_sec)
        self._monotonic = monotonic
        self._sleep = sleep
        self._next_at: float | None = None
        self._lock = threading.Lock()
        self._blocked_until: float | None = None
        self.acquisitions = 0
        self.blocks = 0
        self.waits: list[float] = []

    def note_block(self) -> None:
        """SEC.gov said we are rate limited. Shut the whole feed down for
        the cooldown - EVERY caller, not just the one that was told."""
        with self._lock:
            self.blocks += 1
            self._blocked_until = self._monotonic() + BLOCK_COOLDOWN_SECONDS

    def blocked_for(self) -> float:
        """Seconds remaining on a cooldown, 0 if clear."""
        with self._lock:
            if self._blocked_until is None:
                return 0.0
            left = self._blocked_until - self._monotonic()
            if left <= 0:
                self._blocked_until = None
                return 0.0
            return left

    def acquire(self) -> None:
        # REFUSE OUTRIGHT DURING A COOLDOWN. Not sleep - refuse. Sleeping
        # would let the pass carry on afterwards and re-enter the same
        # sustained burst that caused the block; and a request made
        # during the timeout extends it.
        # Locked, because the SEC ceiling is per IP and this process runs
        # the dashboard's probes on request threads while the trading
        # cycle is fetching. Two threads reading _next_at between the
        # read and the write is exactly how a paced client emits a burst.
        # The cooldown check shares the lock so acquire() takes it once.
        with self._lock:
            if self._blocked_until is not None:
                left = self._blocked_until - self._monotonic()
                if left > 0:
                    # REFUSE, not sleep. A request during the timeout
                    # extends it, and sleeping would let the same
                    # sustained burst resume the moment it expired.
                    raise RateLimitBlocked(
                        f"sec.gov blocked this IP; {left / 60:.1f} minute(s) "
                        "of cooldown left. No SEC request is made until it "
                        "expires - requesting during the timeout extends it.")
                self._blocked_until = None
            now = self._monotonic()
            if self._next_at is None:
                self._next_at = now
            wait = self._next_at - now
            self._next_at = max(self._next_at, now + wait) + self.interval
        if wait > 0:
            self._sleep(wait)
            self.waits.append(wait)
        else:
            self.waits.append(0.0)
        self.acquisitions += 1


#: ONE pacer for the whole process. The SEC's limit is 10 requests per
#: second per IP ACROSS ALL ITS APIs, so a per-call limiter is not a
#: limit at all: two of them at 5/s each sit exactly on the ceiling, and
#: three are over it. Every sec.gov call in this codebase goes through
#: this one object - the feed, and the dashboard's reachability probe.
#: Measured 2026-08-11: one weekday's daily index holds 562 unique Form 4
#: accessions, so a five-day window is ~2,800 requests. That is well
#: inside the rate, and it is exactly the volume that makes sharing the
#: pacer matter rather than being a formality.
_SEC_PACER: "RateLimiter | None" = None
_SEC_PACER_LOCK = threading.Lock()


def reset_sec_pacer() -> None:
    """Drop the process-wide pacer. FOR TESTS ONLY - a block recorded by
    one test would otherwise refuse every SEC call in the ones after
    it, which is a test-ordering bug that looks like a code bug."""
    global _SEC_PACER
    with _SEC_PACER_LOCK:
        _SEC_PACER = None


def sec_pacer() -> "RateLimiter":
    global _SEC_PACER
    with _SEC_PACER_LOCK:
        if _SEC_PACER is None:
            _SEC_PACER = RateLimiter(DEFAULT_REQUESTS_PER_SEC)
        return _SEC_PACER


# --------------------------------------------------------------------------
# Parsed shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexRow:
    """One row of the daily index, plus the line it came from."""

    form_type: str
    company_name: str
    cik: str
    date_filed: str          # YYYYMMDD, verbatim from the index
    path: str                # "edgar/data/34782/0001217074-26-000006.txt"
    accession: str           # "0001217074-26-000006"
    raw_line: str

    @property
    def url(self) -> str:
        return ARCHIVES_BASE + self.path


@dataclass(frozen=True)
class Owner:
    cik: str
    name: str
    is_director: bool | None
    is_officer: bool | None
    is_ten_percent_owner: bool | None
    is_other: bool | None
    officer_title: str
    other_text: str

    @property
    def role(self) -> str:
        """Human-readable role, for the trade narrative on the dashboard."""
        parts: list[str] = []
        if self.is_officer:
            parts.append(f"officer:{self.officer_title}" if self.officer_title else "officer")
        if self.is_director:
            parts.append("director")
        if self.is_ten_percent_owner:
            parts.append("10% owner")
        if self.is_other:
            parts.append(f"other:{self.other_text}" if self.other_text else "other")
        return ", ".join(parts) if parts else "unknown"

    def to_payload(self) -> dict:
        return {
            "cik": self.cik,
            "name": self.name,
            "is_director": self.is_director,
            "is_officer": self.is_officer,
            "is_ten_percent_owner": self.is_ten_percent_owner,
            "is_other": self.is_other,
            "officer_title": self.officer_title,
            "other_text": self.other_text,
            "role": self.role,
        }


@dataclass(frozen=True)
class Transaction:
    table: str                      # "non_derivative" | "derivative"
    security_title: str
    transaction_date: str           # ISO, verbatim from the filing
    code: str                       # "P" open-market purchase, "S" sale, ...
    acquired_disposed: str          # "A" | "D"
    shares: Decimal | None
    price_per_share: Decimal | None
    shares_owned_following: Decimal | None
    direct_or_indirect: str

    @property
    def value_usd(self) -> Decimal | None:
        if self.shares is None or self.price_per_share is None:
            return None
        return self.shares * self.price_per_share

    def to_payload(self) -> dict:
        return {
            "table": self.table,
            "security_title": self.security_title,
            "transaction_date": self.transaction_date,
            "code": self.code,
            "acquired_disposed": self.acquired_disposed,
            "shares": _dec_str(self.shares),
            "price_per_share": _dec_str(self.price_per_share),
            "shares_owned_following": _dec_str(self.shares_owned_following),
            "direct_or_indirect": self.direct_or_indirect,
            "value_usd": _dec_str(self.value_usd),
        }


@dataclass(frozen=True)
class ParsedForm4:
    accession: str
    document_type: str              # "4" | "4/A"
    acceptance_datetime: str        # ISO, ET, naive — see module docstring
    filed_date: str                 # ISO
    period_of_report: str           # ISO
    issuer_cik: str
    issuer_name: str
    ticker: str
    owners: tuple[Owner, ...]
    transactions: tuple[Transaction, ...]
    aff10b5one_element: bool | None  # None == element absent (pre-~2023)
    footnote_mentions_10b5_1: bool
    footnotes: tuple[str, ...]
    remarks: str

    @property
    def plan_flagged(self) -> bool:
        """Evidence that this is a 10b5-1 plan trade. Evidence, not a
        verdict: the strategy excludes plan trades, but *deciding* to
        exclude is discovery's job, not the feed's."""
        return bool(self.aff10b5one_element) or self.footnote_mentions_10b5_1

    def to_payload(self) -> dict:
        return {
            "accession": self.accession,
            "document_type": self.document_type,
            "acceptance_datetime": self.acceptance_datetime,
            "filed_date": self.filed_date,
            "period_of_report": self.period_of_report,
            "issuer_cik": self.issuer_cik,
            "issuer_name": self.issuer_name,
            "ticker": self.ticker,
            "owners": [o.to_payload() for o in self.owners],
            "transactions": [t.to_payload() for t in self.transactions],
            "ten_b5_1": {
                "element": self.aff10b5one_element,
                "footnote_mention": self.footnote_mentions_10b5_1,
                "plan_flagged": self.plan_flagged,
            },
            "footnotes": list(self.footnotes),
            "remarks": self.remarks,
        }


@dataclass(frozen=True)
class FilingError:
    """One submission that could not be fetched or parsed. Recorded, not
    raised — one bad filing must not lose the other 561."""

    accession: str
    url: str
    error: str
    status_code: int | None
    raw_text: str


@dataclass
class FetchResult:
    """Everything ``fetch_events`` knows, including why it is empty."""

    events: list[RawEvent] = field(default_factory=list)
    missing_index_dates: list[dict] = field(default_factory=list)
    filing_errors: list[FilingError] = field(default_factory=list)
    index_rows_seen: int = 0
    unique_accessions: int = 0
    requests_made: int = 0
    truncated_at: int | None = None
    #: Filings replayed from local storage rather than re-downloaded.
    #: The gap between this and requests_made IS the rate-limit fix.
    from_cache: int = 0
    #: Daily INDEXES replayed rather than re-downloaded. The same idea
    #: one level up, and the level that was missing: the owner's
    #: 2026-08-24 log shows 306 fetches of four immutable index files,
    #: about 1.3 MB each, while the submissions behind them were being
    #: replayed for free.
    index_days_from_cache: int = 0
    #: Set when sec.gov blocked this IP mid-pass. The pass stops there
    #: on purpose: further requests extend the timeout.
    rate_limited: str = ""

    def why_empty(self) -> str:
        """One line a human can read off the dashboard when there is
        nothing here (house rule 3)."""
        if self.events:
            return ""
        bits = []
        if self.missing_index_dates:
            dates = ", ".join(d["date"] for d in self.missing_index_dates)
            bits.append(
                f"no daily index published for {dates} "
                f"(weekend/holiday, or before the evening publish); "
                f"raw upstream body: {self.missing_index_dates[0]['raw_text'][:200]!r}"
            )
        if self.filing_errors:
            bits.append(
                f"{len(self.filing_errors)} filing(s) failed, first: "
                f"{self.filing_errors[0].error} raw="
                f"{self.filing_errors[0].raw_text[:200]!r}"
            )
        if not bits:
            bits.append(
                f"{self.index_rows_seen} index rows seen, "
                f"{self.unique_accessions} unique Form 4 accessions, "
                "none survived the window filter"
            )
        return "; ".join(bits)


# --------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------


def _dec_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _to_bool(raw: str | None) -> bool | None:
    """EDGAR writes booleans as 0/1 *and* false/true, sometimes both in
    the same filing. Measured 2026-08-10; see module docstring."""
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in ("1", "true", "y", "yes"):
        return True
    if text in ("0", "false", "n", "no"):
        return False
    return None


def _to_decimal(raw: str | None) -> Decimal | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


def _value_of(parent: ET.Element | None, tag: str) -> str:
    """Form 4 wraps most leaves in ``<tag><value>x</value></tag>``, but
    some appear bare. Handle both, and ignore footnoteId attributes."""
    if parent is None:
        return ""
    node = parent.find(tag)
    if node is None:
        return ""
    value = node.find("value")
    if value is not None:
        return (value.text or "").strip()
    return (node.text or "").strip()


def _header_field(text: str, label: str) -> str:
    match = re.search(
        rf"^\s*{re.escape(label)}:\s*(.+?)\s*$", text, re.MULTILINE
    )
    return match.group(1).strip() if match else ""


def _yyyymmdd_to_iso(stamp: str) -> str:
    stamp = stamp.strip()
    if len(stamp) != 8 or not stamp.isdigit():
        return stamp
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"


def _acceptance_to_iso(stamp: str) -> str:
    """``20260806202933`` -> ``2026-08-06T20:29:33``. Eastern, naive —
    EDGAR does not stamp a zone, and inventing one here would be a lie
    that later arithmetic would trust."""
    stamp = stamp.strip()
    if len(stamp) != 14 or not stamp.isdigit():
        return _yyyymmdd_to_iso(stamp)
    return (
        f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
        f"T{stamp[8:10]}:{stamp[10:12]}:{stamp[12:14]}"
    )


def _response_text(response: Any) -> str:
    try:
        text = response.text
    except Exception as exc:  # pragma: no cover - defensive
        return f"<unreadable response body: {type(exc).__name__}: {exc}>"
    if isinstance(text, bytes):  # pragma: no cover - defensive
        return text.decode("utf-8", "replace")
    return text or ""


def _looks_absent(status: int, body: str) -> bool:
    """Is this 4xx "the file is not there" or "you are blocked"?

    Absent: 404, or 403 with the S3 ``AccessDenied``/``NoSuchKey`` body.
    Blocked: anything else — SEC's own block page says "Request Rate
    Threshold Exceeded" / "Undeclared Automated Tool" and must be loud.
    """
    if status == 404:
        return True
    if status == 403:
        return any(marker in body for marker in _ABSENT_MARKERS)
    return False


# --------------------------------------------------------------------------
# Public parsers (pure — no network, no clock)
# --------------------------------------------------------------------------


def parse_daily_index(text: str, *, forms: Sequence[str] = ("4",)) -> list[IndexRow]:
    """Rows of ``form.YYYYMMDD.idx`` whose form type is in ``forms``.

    The file is fixed width with a variable-width company name, and some
    form types contain spaces ("SCHEDULE 13G"), so the columns are taken
    from the right: the last three whitespace-separated tokens are CIK,
    date filed and file name; the form type is the first token.
    """
    wanted = {f.strip().upper() for f in forms}
    rows: list[IndexRow] = []
    for line in text.splitlines():
        if not line.strip() or line.startswith(" "):
            continue
        head = line.split(None, 1)[0]
        if head.upper() not in wanted:
            continue
        try:
            left, cik, date_filed, path = line.rsplit(None, 3)
        except ValueError:
            continue
        if not path.startswith("edgar/"):
            continue
        company = left[len(head):].strip()
        accession = path.rsplit("/", 1)[-1]
        if accession.endswith(".txt"):
            accession = accession[: -len(".txt")]
        rows.append(
            IndexRow(
                form_type=head,
                company_name=company,
                cik=cik,
                date_filed=date_filed,
                path=path,
                accession=accession,
                raw_line=line,
            )
        )
    return rows


def _extract_ownership_xml(submission_text: str) -> str:
    start = submission_text.find("<ownershipDocument")
    if start == -1:
        raise Form4ParseError("no <ownershipDocument> element in submission")
    end = submission_text.find("</ownershipDocument>", start)
    if end == -1:
        raise Form4ParseError("unterminated <ownershipDocument> element")
    return submission_text[start: end + len("</ownershipDocument>")]


def parse_submission(submission_text: str) -> ParsedForm4:
    """Parse one EDGAR full-submission text file into typed fields.

    Pure function: give it the bytes, get the fields. The caller keeps
    the verbatim text beside the result — this never becomes the only
    copy of the evidence.
    """
    xml_text = _extract_ownership_xml(submission_text)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise Form4ParseError(f"ownershipDocument is not well-formed XML: {exc}") from exc

    issuer = root.find("issuer")
    owners: list[Owner] = []
    for owner_node in root.findall("reportingOwner"):
        ident = owner_node.find("reportingOwnerId")
        rel = owner_node.find("reportingOwnerRelationship")
        owners.append(
            Owner(
                cik=_text(ident.find("rptOwnerCik")) if ident is not None else "",
                name=_text(ident.find("rptOwnerName")) if ident is not None else "",
                is_director=_to_bool(_text(rel.find("isDirector")) if rel is not None else None),
                is_officer=_to_bool(_text(rel.find("isOfficer")) if rel is not None else None),
                is_ten_percent_owner=_to_bool(
                    _text(rel.find("isTenPercentOwner")) if rel is not None else None
                ),
                is_other=_to_bool(_text(rel.find("isOther")) if rel is not None else None),
                officer_title=_text(rel.find("officerTitle")) if rel is not None else "",
                other_text=_text(rel.find("otherText")) if rel is not None else "",
            )
        )

    transactions: list[Transaction] = []
    for table_tag, table_name, txn_tag in (
        ("nonDerivativeTable", "non_derivative", "nonDerivativeTransaction"),
        ("derivativeTable", "derivative", "derivativeTransaction"),
    ):
        table = root.find(table_tag)
        if table is None:
            continue
        for txn in table.findall(txn_tag):
            coding = txn.find("transactionCoding")
            amounts = txn.find("transactionAmounts")
            post = txn.find("postTransactionAmounts")
            nature = txn.find("ownershipNature")
            transactions.append(
                Transaction(
                    table=table_name,
                    security_title=_value_of(txn, "securityTitle"),
                    transaction_date=_value_of(txn, "transactionDate"),
                    code=_text(coding.find("transactionCode")) if coding is not None else "",
                    acquired_disposed=_value_of(amounts, "transactionAcquiredDisposedCode"),
                    shares=_to_decimal(_value_of(amounts, "transactionShares")),
                    price_per_share=_to_decimal(
                        _value_of(amounts, "transactionPricePerShare")
                    ),
                    shares_owned_following=_to_decimal(
                        _value_of(post, "sharesOwnedFollowingTransaction")
                    ),
                    direct_or_indirect=_value_of(nature, "directOrIndirectOwnership"),
                )
            )

    footnotes = tuple(
        " ".join((node.text or "").split())
        for node in root.iter("footnote")
    )
    aff_node = root.find("aff10b5One")
    aff = _to_bool(_text(aff_node)) if aff_node is not None else None

    return ParsedForm4(
        accession=_header_field(submission_text, "ACCESSION NUMBER"),
        document_type=_text(root.find("documentType")) or _header_field(
            submission_text, "CONFORMED SUBMISSION TYPE"
        ),
        acceptance_datetime=_acceptance_to_iso(
            _first_match(submission_text, r"<ACCEPTANCE-DATETIME>(\d+)")
        ),
        filed_date=_yyyymmdd_to_iso(_header_field(submission_text, "FILED AS OF DATE")),
        period_of_report=_text(root.find("periodOfReport"))
        or _yyyymmdd_to_iso(_header_field(submission_text, "CONFORMED PERIOD OF REPORT")),
        issuer_cik=_text(issuer.find("issuerCik")) if issuer is not None else "",
        issuer_name=_text(issuer.find("issuerName")) if issuer is not None else "",
        ticker=(_text(issuer.find("issuerTradingSymbol")) if issuer is not None else "").upper(),
        owners=tuple(owners),
        transactions=tuple(transactions),
        aff10b5one_element=aff,
        footnote_mentions_10b5_1=any(_TEN_B5_1_RE.search(f) for f in footnotes),
        footnotes=footnotes,
        remarks=_text(root.find("remarks")),
    )


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def user_agent(contact_email: str | None = None) -> str:
    """SEC requires a contactable User-Agent on every request. Without
    one the response is a 403 block page, not data."""
    email = (
        contact_email
        or os.environ.get("CATALYST_CONTACT_EMAIL")
        or DEFAULT_CONTACT_EMAIL
    )
    return f"Catalyst Trading Bot ({email})"


def _default_http_get(url: str, headers: dict) -> HttpResponse:
    import httpx

    return httpx.get(url, headers=headers, timeout=30.0, follow_redirects=True)


def _request(
    url: str,
    *,
    http_get: HttpGet,
    limiter: RateLimiter,
    sleep: Callable[[float], None],
    headers: dict,
    max_attempts: int = 4,
    backoff_base: float = 0.5,
    tolerate: Iterable[int] = (),
) -> HttpResponse:
    """One GET, rate limited, 5xx/429 retried with backoff, 4xx never.

    Retrying a 4xx spends the rate-limit budget on a request that is
    wrong by construction — and the budget is shared with every other
    SEC feed in this process (TRAPS.md).
    """
    tolerated = set(tolerate)
    for attempt in range(max_attempts):
        limiter.acquire()
        try:
            response = http_get(url, headers)
        except FeedError:
            raise
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < max_attempts:
                sleep(backoff_base * (2 ** attempt))
                continue
            raise FeedError(
                f"transport failure after {max_attempts} attempts",
                url=url,
                status_code=None,
                raw_text=detail,
                attempts=attempt + 1,
            ) from exc

        status = int(response.status_code)
        body_peek = ""
        if status != 200:
            body_peek = _response_text(response)
        elif "text/html" in str(
                (getattr(response, "headers", None) or {}).get(
                    "Content-Type", "")).lower():
            # A 200 carrying HTML where an .idx or a submission text file
            # was expected is the block page served by an edge that did
            # not set a 4xx. Reading it as data would parse to zero rows
            # and look like a quiet day.
            body_peek = _response_text(response)
        if body_peek and looks_rate_limited(status, body_peek):
            sec_pacer().note_block()
            raise RateLimitBlocked(
                f"SEC.gov rate-limited this IP (HTTP {status}). Every further "
                "request during the timeout EXTENDS it, so this pass stops "
                f"here and nothing touches sec.gov for "
                f"{BLOCK_COOLDOWN_SECONDS // 60} minutes.",
                raw_text=body_peek[:2000])
        if status == 200:
            return response
        body = body_peek or _response_text(response)
        if status in tolerated:
            return response
        if status in RETRYABLE_STATUSES or 500 <= status < 600:
            if attempt + 1 < max_attempts:
                sleep(backoff_base * (2 ** attempt))
                continue
            raise FeedError(
                f"HTTP {status} after {max_attempts} attempts",
                url=url,
                status_code=status,
                raw_text=body,
                attempts=attempt + 1,
            )
        raise FeedError(
            f"HTTP {status} — not retried, a 4xx means the request is wrong",
            url=url,
            status_code=status,
            raw_text=body,
            attempts=attempt + 1,
        )
    raise AssertionError("unreachable")  # pragma: no cover


# --------------------------------------------------------------------------
# The feed
# --------------------------------------------------------------------------


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _daily_index_url(day: date) -> str:
    return DAILY_INDEX_URL.format(
        year=day.year,
        qtr=(day.month - 1) // 3 + 1,
        stamp=day.strftime("%Y%m%d"),
    )


def fetch_form4(
    since: datetime | date,
    until: datetime | date,
    http_get: HttpGet | None = None,
    *,
    contact_email: str | None = None,
    rate_per_sec: float = DEFAULT_REQUESTS_PER_SEC,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] | None = None,
    include_amendments: bool = False,
    max_filings: int | None = None,
    already_have: Callable[[str], dict | None] | None = None,
    on_fetched: Callable[[str, dict], None] | None = None,
) -> FetchResult:
    """Fetch Form 4 filings disseminated between ``since`` and ``until``.

    Day granularity: the daily index is published per day, so the window
    is inclusive of both end dates and intra-day times are ignored.
    ``acceptance_datetime`` is parsed onto every event so the caller can
    apply the point-in-time rule (a filing accepted after the close is
    not tradable until the next open) itself.

    ``max_filings`` bounds the request count; hitting it sets
    ``FetchResult.truncated_at`` rather than silently dropping filings.

    ``already_have(accession)`` RETURNS A STORED PARSED FILING, OR None.
    This is the difference between a feed and a denial-of-service on
    sec.gov. Without it every pass re-downloads every filing in the
    window: measured 2,815 requests for five days, 9.4 minutes of
    continuous traffic inside a 15-minute cycle - a 63% duty cycle, and
    the reason the SEC rate-limited this bot on 2026-08-11. With it a
    steady-state pass fetches the daily indexes plus only genuinely new
    filings, which is a few dozen a day.

    The stored filing is REPLAYED into the result rather than skipped,
    so the returned events are still the complete window. Skipping would
    silently break insider-cluster detection, which needs every purchase
    in the window to count distinct owners.

    ``on_fetched(accession, parsed)`` is called for each filing actually
    downloaded, so the caller can store it for next time.
    """
    getter = http_get or _default_http_get
    clock = now or (lambda: datetime.now(timezone.utc))
    # The PROCESS-WIDE pacer unless the caller injected a clock or a
    # sleep - tests do that to run instantly, and they must not be paced
    # by, or leave state on, the shared object.
    if monotonic is time.monotonic and sleep is time.sleep \
            and rate_per_sec == DEFAULT_REQUESTS_PER_SEC:
        limiter = sec_pacer()
    else:
        limiter = RateLimiter(rate_per_sec, monotonic=monotonic, sleep=sleep)
    headers = {
        "User-Agent": user_agent(contact_email),
        "Accept-Encoding": "gzip, deflate",
    }
    forms = ("4", "4/A") if include_amendments else ("4",)

    start, end = _as_date(since), _as_date(until)
    if end < start:
        raise ValueError(f"until ({end}) is before since ({start})")

    result = FetchResult()
    rows_by_accession: dict[str, IndexRow] = {}

    day = start
    while day <= end:
        url = _daily_index_url(day)
        # ALREADY HAVE THIS DAY'S INDEX? A published index for a past
        # day is immutable, so re-downloading it buys nothing and costs
        # 1.3 MB plus a request from a rate limit shared with every
        # other SEC feed in this process.
        cached = _index_cache.get((day, forms))
        if cached is not None:
            result.index_rows_seen += len(cached)
            result.index_days_from_cache += 1
            for row in cached:
                rows_by_accession.setdefault(row.accession, row)
            day += timedelta(days=1)
            continue
        # ASKED RECENTLY AND TOLD NO? Do not ask again yet. The date is
        # still recorded as missing, with why, so a zero is never left
        # unexplained (house rule 3) - it just costs no request.
        last_absent = _absent_since.get(day)
        if (last_absent is not None
                and monotonic() - last_absent < ABSENT_RECHECK_SECONDS):
            result.missing_index_dates.append({
                "date": day.isoformat(),
                "url": url,
                "status_code": None,
                "raw_text": (
                    "not requested: this date answered 'no such file' "
                    f"{monotonic() - last_absent:.0f}s ago and is asked again "
                    f"at most every {ABSENT_RECHECK_SECONDS:.0f}s. Weekends, "
                    "holidays and today-before-the-evening-publish all look "
                    "like this; the SEC rate limit is shared across every SEC "
                    "feed in this process, so re-asking every cycle spends it "
                    "for nothing."),
            })
            day += timedelta(days=1)
            continue
        response = _request(
            url,
            http_get=getter,
            limiter=limiter,
            sleep=sleep,
            headers=headers,
            tolerate=(403, 404),
        )
        result.requests_made += 1
        status = int(response.status_code)
        if status != 200:
            body = _response_text(response)
            if _looks_absent(status, body):
                # Normal: weekends, holidays, and today before the
                # evening publish. Recorded with the raw body so a zero
                # is never left unexplained.
                result.missing_index_dates.append(
                    {
                        "date": day.isoformat(),
                        "url": url,
                        "status_code": status,
                        "raw_text": body,
                    }
                )
                _absent_since[day] = monotonic()
                day += timedelta(days=1)
                continue
            raise FeedError(
                f"daily index refused with HTTP {status} and a body that is not "
                "an absent-file marker — this looks like an IP block, not a "
                "missing file",
                url=url,
                status_code=status,
                raw_text=body,
            )
        # It published. Stop remembering it as absent, so a later pass
        # over the same window is never told to skip a real index.
        _absent_since.pop(day, None)
        index_text = _response_text(response)
        rows = parse_daily_index(index_text, forms=forms)
        # ONLY A PAST DAY IS FINAL. Today's index is still being written
        # to until the evening publish, so caching it would freeze the
        # day at whatever was filed by the first pass.
        if day < clock().date():
            _remember_index(day, forms, rows)
        result.index_rows_seen += len(rows)
        for row in rows:
            # Each accession is listed once per CIK involved (issuer and
            # each reporting owner). De-dup or pay double.
            rows_by_accession.setdefault(row.accession, row)
        day += timedelta(days=1)

    result.unique_accessions = len(rows_by_accession)
    ordered = list(rows_by_accession.values())
    if max_filings is not None and len(ordered) > max_filings:
        result.truncated_at = max_filings
        ordered = ordered[:max_filings]

    attempted = 0
    for row in ordered:
        attempted += 1
        # ALREADY HAVE IT? Replay it and spend no request. See the
        # docstring - this is what keeps a pass from re-downloading the
        # whole window every fifteen minutes.
        if already_have is not None:
            try:
                cached = already_have(row.accession)
            except Exception:  # noqa: BLE001 - a cache miss is never fatal
                cached = None
            if cached:
                result.from_cache += 1
                result.events.append(RawEvent(
                    source=SOURCE, source_id=row.accession,
                    fetched_at=clock(),
                    payload_raw=dict(cached, replayed_from_cache=True)))
                continue
        try:
            response = _request(
                row.url,
                http_get=getter,
                limiter=limiter,
                sleep=sleep,
                headers=headers,
            )
            result.requests_made += 1
            body = _response_text(response)
            parsed = parse_submission(body)
        except FeedError as exc:
            result.requests_made += exc.attempts
            result.filing_errors.append(
                FilingError(
                    accession=row.accession,
                    url=row.url,
                    error=exc.message,
                    status_code=exc.status_code,
                    raw_text=exc.raw_text,
                )
            )
            continue
        except Form4ParseError as exc:
            result.filing_errors.append(
                FilingError(
                    accession=row.accession,
                    url=row.url,
                    error=f"parse failure: {exc}",
                    status_code=200,
                    raw_text=body[:4000],
                )
            )
            continue

        payload = {
            "source_url": row.url,
            "accession": row.accession,
            "daily_index_line": row.raw_line,
            "submission_text": body,          # verbatim, never trimmed
            "parsed": parsed.to_payload(),    # beside it, never instead
        }
        # Store the FINISHED payload, not the parsed dataclass: it is
        # what the event carries, it is JSON-serialisable, and replaying
        # it reproduces the event exactly.
        if on_fetched is not None:
            try:
                on_fetched(row.accession, payload)
            except Exception:  # noqa: BLE001 - storing is best effort
                pass
        result.events.append(
            RawEvent(
                source=SOURCE,
                source_id=row.accession,
                fetched_at=clock(),
                payload_raw=payload,
            )
        )

    if attempted and not result.events:
        first = result.filing_errors[0] if result.filing_errors else None
        raise FeedError(
            f"all {attempted} Form 4 submission fetches failed — an empty "
            "result here would be indistinguishable from a quiet market",
            url=first.url if first else None,
            status_code=first.status_code if first else None,
            raw_text=first.raw_text if first else "",
        )
    return result


def fetch_events(
    since: datetime | date,
    until: datetime | date,
    http_get: HttpGet | None = None,
    **kwargs: Any,
) -> list[RawEvent]:
    """The ``data/sources/<source>.py`` contract (ARCHITECTURE.md 3.2).

    Raises ``FeedError`` on a transport/HTTP failure rather than
    returning ``[]`` — see the module docstring's failure contract. Use
    ``fetch_form4`` when you also want the missing-date and per-filing
    error detail for ``raw_events_errors`` and the dashboard.
    """
    return fetch_form4(since, until, http_get, **kwargs).events
