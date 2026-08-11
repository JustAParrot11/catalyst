"""EDGAR full-text search as a general discovery engine.

    https://efts.sec.gov/LATEST/search-index?q=<phrase>&forms=8-K&startdt=&enddt=

Until now the bot had exactly ONE feed - Form 4 insider filings - so
every candidate it has ever considered was an insider cluster. This
module is the second, and it is a different KIND of source: rather than
one filing type, it searches the text of every filing, which makes it a
way to ask "who said X recently?" about any phrase at all.

WHY THIS ONE AND NOT AN API PER CATALYST TYPE. Measured live
2026-08-11 over a 21-day window, one request each:

    "conference call to discuss"          538 filings   earnings date announced
    "fourth quarter" "financial results"  593           earnings
    "special meeting of stockholders"     843           merger vote
    "Phase 3" "primary endpoint"          271           clinical readout
    "topline results"                     236           clinical readout
    "PDUFA"                               133           FDA decision date
    "definitive merger agreement"          40           merger
    "raises full year guidance"            38           guidance

Every catalyst type the brief names, from one keyless endpoint, at one
request per query. Compare the alternative: a web search costs $0.01 per
query forever, and would return a page of journalism rather than the
filing itself.

IT CLOSES THE PDUFA GAP. TRAPS.md records that openFDA is retrospective
and "PDUFA dates are not published by the FDA at all - companies
disclose them". They disclose them HERE: 133 filings mentioning PDUFA in
three weeks, each carrying its own ticker.

WHAT THE RESPONSE GIVES, which matters more than the hit count:

  display_names  "Harmony Biosciences Holdings, Inc.  (HRMY)  (CIK ...)"
                 -> the TICKER, which is what makes a hit tradeable at
                    all. The Federal Register API, by contrast, prints
                    "Pediatric Advisory Committee" and never names a
                    company, so a hit there needs resolving before it is
                    worth anything.
  items          8-K item codes: "2.02" results of operations, "7.01"
                 Reg FD, "8.01" other events.
  sics           the SIC industry code - free sector data, which the
                 correlation engine otherwise has to guess at.
  file_date      when it was filed. NOT the catalyst date: the filing
                 ANNOUNCES a future date in its text, so extracting that
                 needs the document (see fetch_document).

TRAPS OBSERVED LIVE, ALL VERIFIED 2026-08-11:

  * NO User-Agent -> 403 block page, not data. Same rule as the rest of
    EDGAR, and the failure looks like an outage rather than a mistake.
  * This endpoint is on the SAME 10 requests/second per-IP budget as
    every other SEC API, so it goes through the shared sec_pacer() -
    never its own limiter.
  * A page is 100 hits, `from` pages through them, and `from` beyond
    10,000 is a hard 400 from Elasticsearch rather than an empty page.
    A caller that pages blindly hits it.
  * There are no rate-limit headers to read, so the pacer is the only
    thing standing between this and a temporary IP block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

from catalyst.data import RawEvent
from catalyst.data.sources.edgar_form4 import (
    FeedError,
    HttpGet,
    RateLimiter,
    _request,
    _response_text,
    sec_pacer,
    user_agent,
)

SOURCE = "edgar_fts"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
#: Elasticsearch refuses `from` past this with a 400, not an empty page.
MAX_RESULT_WINDOW = 10_000
PAGE_SIZE = 100


@dataclass(frozen=True)
class Query:
    """One search, and what a hit on it MEANS.

    `catalyst_type` is what discovery keys risk assumptions off, so a
    query whose meaning is ambiguous should not be in this table at all.
    """

    key: str
    phrase: str
    catalyst_type: str
    forms: str = ""          # "" = every form type
    #: Roughly how many filings this returned over 21 days when the
    #: table was measured. Present so a query that has silently stopped
    #: matching is visible as a number rather than as an absence.
    measured_21d: int = 0


#: Every query is a request per cycle and, far more importantly, a claim
#: that a hit means something tradeable. Adding one is a strategy
#: decision, not a plumbing one.
#:
#: SECTOR BALANCE IS DESIGNED IN, NOT HOPED FOR. The first version of
#: this table had three queries - PDUFA, topline results, Phase 3 - that
#: can ONLY match pharma, and four that were neutral. The measured
#: result was 10 of 12 live candidates in SIC 2834, and the owner's
#: fair complaint: "I want no industry bias i want it to find any
#: opportunity regardless from microsoft to a farming company to a
#: chemical company anything".
#:
#: So every query below was MEASURED live on 2026-08-11 over a 21-day
#: window, and the pharma share of its first 100 hits is recorded beside
#: it. Two candidates were REJECTED on that evidence and are listed at
#: the foot of this table so nobody re-adds them.
QUERIES: tuple[Query, ...] = (
    Query("earnings_call_scheduled", '"conference call to discuss"',
          "earnings", forms="8-K", measured_21d=538),
    Query("pdufa", '"PDUFA"', "fda_decision", measured_21d=133),
    Query("topline_expected", '"topline results"',
          "clinical_readout", measured_21d=236),
    Query("phase3_endpoint", '"Phase 3" "primary endpoint"',
          "clinical_readout", measured_21d=271),
    Query("merger_vote", '"special meeting of stockholders"',
          "merger_vote", measured_21d=843),
    Query("merger_agreement", '"definitive merger agreement"',
          "merger", forms="8-K", measured_21d=40),
    Query("guidance_raise", '"raises full year guidance"',
          "guidance", measured_21d=38),

    # --- SECTOR-NEUTRAL. Measured pharma share of first 100 hits in
    # brackets; every one of these is under a fifth.
    Query("strategic_review", '"strategic alternatives"',
          "strategic_review", measured_21d=501),          # 13% pharma
    Query("buyback", '"share repurchase program"',
          "buyback", forms="8-K", measured_21d=465),      # 9%
    Query("credit_amendment", '"credit agreement" "amendment"',
          "financing", forms="8-K", measured_21d=383),    # 8%
    Query("asset_purchase", '"asset purchase agreement"',
          "asset_deal", forms="8-K", measured_21d=101),   # 18%
    Query("restructuring", '"restructuring plan"',
          "restructuring", forms="8-K", measured_21d=84), # 12%
    Query("ceo_change", '"appointed chief executive"',
          "leadership_change", measured_21d=59),          # 19%
    Query("contract_award", '"awarded a contract"',
          "contract_award", measured_21d=24),             # 8%

    # --- DELIBERATELY sector-specific, for sectors the rest of this
    # table would otherwise never reach. These correct the balance
    # rather than skew it.
    Query("production_guidance", '"production guidance"',
          "guidance", measured_21d=107),   # mining/energy: 91 of 100
    Query("same_store_sales", '"same-store sales"',
          "earnings_result", measured_21d=78),   # retail: 43 of 100
)

#: MEASURED AND REJECTED 2026-08-11. Recorded so the evidence survives
#: and nobody re-adds them on intuition:
#:   '"letter of intent"'    -> 0 hits in 21 days. A dead query costs a
#:                              request every cycle and returns nothing.
#:   '"regulatory approval"' -> 1,781 hits but 93% pharma, which is a
#:                              worse concentration than the three
#:                              pharma-only queries already here.
#:   '"supply agreement"'    -> 35% pharma; the neutral queries above
#:                              cover the same manufacturing ground
#:                              without the skew.
REJECTED_QUERIES = (
    ('"letter of intent"', "0 hits in a 21-day window"),
    ('"regulatory approval"', "93% pharma, worse than what it would join"),
    ('"supply agreement"', "35% pharma, and redundant with the neutral set"),
)

#: "COMPANY NAME  (TICK)  (CIK 0001234567)". Several tickers can share
#: one entry ("AGM, AGM-A, AGM-PD, ..."), which is a preferred-share
#: family rather than seven companies - the FIRST is the common stock
#: and the only one worth trading.
_DISPLAY_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<tickers>[^()]*)\)\s*\(CIK\s*(?P<cik>\d+)\)")


def parse_display_name(text: str) -> dict:
    """Split EDGAR's display_names entry into name, ticker and CIK.

    Returns ticker="" when the entry carries no ticker at all - many
    filers have none (funds, trusts, individuals), and those are not
    tradeable. An unparseable entry keeps the raw text rather than
    vanishing, because a filer we cannot read is a finding.
    """
    raw = str(text or "").strip()
    m = _DISPLAY_RE.match(raw)
    if not m:
        return {"name": raw, "ticker": "", "cik": "", "raw": raw}
    tickers = [t.strip().upper() for t in m.group("tickers").split(",") if t.strip()]
    return {
        "name": m.group("name").strip(),
        # The common stock, not the preferred-share family behind it.
        "ticker": tickers[0] if tickers else "",
        "all_tickers": tickers,
        "cik": m.group("cik").lstrip("0") or "0",
        "raw": raw,
    }


@dataclass
class SearchResult:
    events: list = field(default_factory=list)
    requests_made: int = 0
    #: Per query: how many hits EDGAR said there were, and how many we
    #: took. A query that returns nothing is recorded rather than
    #: dropped - "no data" and "the query is broken" look identical
    #: otherwise (house rule 3).
    per_query: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def _default_http_get(url: str, headers: dict, params: dict):
    import httpx

    return httpx.get(url, headers=headers, params=params, timeout=30.0)


def search_one(
    query: Query,
    since: date,
    until: date,
    *,
    http_get: Callable | None = None,
    limiter: RateLimiter | None = None,
    max_hits: int = PAGE_SIZE,
) -> tuple[list, int, dict]:
    """(hits, requests_made, summary) for ONE query.

    `max_hits` bounds the paging. It is a request budget, not a filter:
    when it truncates, the summary says so, because a silently truncated
    discovery pass looks exactly like a quiet market.
    """
    pacer = limiter or sec_pacer()
    getter = http_get or _default_http_get
    headers = {"User-Agent": user_agent(),
               "Accept-Encoding": "gzip, deflate"}
    hits: list = []
    made = 0
    total: int | None = None
    relation = ""
    start = 0
    while len(hits) < max_hits:
        if start >= MAX_RESULT_WINDOW:
            break        # a further page is a hard 400, not an empty page
        params = {
            "q": query.phrase,
            "startdt": since.isoformat(),
            "enddt": until.isoformat(),
            "from": start,
        }
        if query.forms:
            params["forms"] = query.forms

        def call(url, hdrs, _params=params):
            return getter(url, hdrs, _params)

        response = _request(SEARCH_URL, http_get=call, limiter=pacer,
                            sleep=__import__("time").sleep, headers=headers)
        made += 1
        body = _parse_json(response)
        block = ((body.get("hits") or {}).get("total") or {})
        if total is None:
            total = int(block.get("value") or 0)
            relation = str(block.get("relation") or "")
        page = (body.get("hits") or {}).get("hits") or []
        if not page:
            break
        hits.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    hits = hits[:max_hits]
    summary = {
        "query": query.key,
        "phrase": query.phrase,
        "forms": query.forms or "(all)",
        "reported_total": total or 0,
        "total_relation": relation,
        "taken": len(hits),
        "truncated": bool(total and len(hits) < total),
        "requests_made": made,
        "window": [since.isoformat(), until.isoformat()],
    }
    return hits, made, summary


def _parse_json(response) -> dict:
    text = _response_text(response)
    try:
        import json

        body = json.loads(text)
    except ValueError as exc:
        raise FeedError(
            "full-text search returned a body that is not JSON - a 403 "
            "block page looks exactly like this, and the usual cause is "
            "a missing User-Agent",
            url=SEARCH_URL, status_code=getattr(response, "status_code", None),
            raw_text=text[:2000],
        ) from exc
    if not isinstance(body, dict) or "hits" not in body:
        raise FeedError(
            "full-text search body has no hits block - refusing to read "
            "an unrecognised shape as zero results",
            url=SEARCH_URL, status_code=getattr(response, "status_code", None),
            raw_text=text[:2000])
    return body


def hit_to_event(hit: dict, query: Query, fetched_at: datetime) -> RawEvent | None:
    """One search hit -> one RawEvent, or None when it is not tradeable.

    A filer with no ticker is dropped HERE rather than downstream: it is
    an absence of tradeability, not an error, and carrying it further
    would put untradeable rows in every count on the funnel.
    """
    source = hit.get("_source") or {}
    names = source.get("display_names") or []
    who = parse_display_name(names[0] if names else "")
    if not who.get("ticker"):
        return None
    adsh = str(source.get("adsh") or "").strip()
    if not adsh:
        return None
    return RawEvent(
        source=SOURCE,
        # The SAME filing can match several queries, and each match is a
        # different claim about it, so the query is part of the identity.
        source_id=f"{adsh}:{query.key}",
        fetched_at=fetched_at,
        payload_raw={
            "accession": adsh,
            "ticker": who["ticker"],
            "all_tickers": who.get("all_tickers") or [],
            "company": who["name"],
            "cik": who["cik"],
            "filed_date": source.get("file_date") or "",
            "form": source.get("root_forms") or [source.get("form") or ""],
            "file_type": source.get("file_type") or "",
            "items": source.get("items") or [],
            "sic": (source.get("sics") or [""])[0],
            "period_ending": source.get("period_ending") or "",
            # WHAT MATCHED, kept verbatim. Without it a candidate cannot
            # explain itself, and the dashboard's whole promise is that
            # every trade can be reconstructed afterwards.
            "matched_query": query.key,
            "matched_phrase": query.phrase,
            "catalyst_type": query.catalyst_type,
        },
    )


def fetch_events(
    since: datetime | date,
    until: datetime | date,
    http_get: Callable | None = None,
    *,
    queries: Iterable[Query] = QUERIES,
    max_hits_per_query: int = PAGE_SIZE,
    limiter: RateLimiter | None = None,
    now: Callable[[], datetime] | None = None,
) -> SearchResult:
    """Run the query table over a window and return RawEvents.

    ONE QUERY FAILING DOES NOT LOSE THE REST. A phrase that starts
    returning 400s, or a transient block on one call, would otherwise
    take the whole discovery pass with it. Failures land in
    `errors` with their raw upstream text and the pass continues -
    the funnel then shows a feed fault rather than a quiet day.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    fetched_at = clock()
    start = since.date() if isinstance(since, datetime) else since
    end = until.date() if isinstance(until, datetime) else until
    if end < start:
        raise ValueError(f"until ({end}) is before since ({start})")

    result = SearchResult()
    seen: set[str] = set()
    for query in queries:
        try:
            hits, made, summary = search_one(
                query, start, end, http_get=http_get, limiter=limiter,
                max_hits=max_hits_per_query)
        except FeedError as exc:
            result.errors.append({
                "query": query.key,
                "phrase": query.phrase,
                "error": str(exc),
                "raw_text": (getattr(exc, "raw_text", "") or "")[:2000],
                "status_code": getattr(exc, "status_code", None),
            })
            result.per_query.append({
                "query": query.key, "phrase": query.phrase,
                "reported_total": 0, "taken": 0, "failed": True,
            })
            continue
        result.requests_made += made
        kept = 0
        for hit in hits:
            event = hit_to_event(hit, query, fetched_at)
            if event is None:
                continue
            if event.source_id in seen:
                continue
            seen.add(event.source_id)
            result.events.append(event)
            kept += 1
        summary["tradeable"] = kept
        summary["dropped_no_ticker"] = len(hits) - kept
        result.per_query.append(summary)
    return result


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

#: How far back a discovery pass looks. A filing announcing a date three
#: weeks out is still live news; one from three months ago has either
#: happened or been repriced. TRAPS.md: judge freshness by TYPE, not age
#: alone - this is the type-appropriate window for "a company just said
#: something about a future date".
DEFAULT_LOOKBACK_DAYS = 21


def default_window(now: datetime | None = None) -> tuple[date, date]:
    today = (now or datetime.now(timezone.utc)).date()
    return today - timedelta(days=DEFAULT_LOOKBACK_DAYS), today
