"""Alpaca news - the third feed, and the first that is not a filing.

    https://data.alpaca.markets/v1beta1/news

ALREADY PAID FOR. It comes with the market-data subscription the bot
already needs, so breadth here costs nothing per item. That matters:
the build brief prices breadth from web search at $10 per 1,000 queries
forever, and this is the same kind of coverage for no marginal cost.

WHY IT CHANGES WHAT THE BOT CAN SEE. The other two feeds are filings:
they tell you what a company has FORMALLY said. News tells you what is
being said ABOUT it - analyst actions, earnings beats and misses,
offerings, downgrades - and it arrives days before the filing that
eventually records the same fact, if one ever does.

DISCOVERY, NOT JUST ENRICHMENT. Verified live 2026-08-11:

    symbols omitted entirely  -> 50 items, 48 distinct symbols, more pages
    symbols=""                ->  0 items

TRAPS.md records the second: "An empty symbol list on the news API is
treated as a filter, not as 'everything'". The first is the new part,
and it is the interesting one - OMITTING the parameter returns the
firehose, so this feed can surface tickers nothing else was watching
rather than only annotating tickers already found. Both modes are
supported here and the difference is enforced by a test, because
getting it backwards silently returns nothing at all.

CLASSIFICATION IS DETERMINISTIC AND DELIBERATELY SHALLOW. Headlines are
matched against patterns to produce a catalyst_type and a direction
hint. No model call: this runs over every item in the firehose, and a
model pass per headline is exactly the cost blowout the brief warns
about. The patterns are also the only thing here that could be accused
of judging, so they are conservative and every one is checkable by eye.

The direction hint is a HINT. It never reaches sizing, and code never
trades on it - it exists so a conjunction can say "the news and the
filings disagree", which is more interesting than either alone. An
at-the-market offering announced beside an earnings beat is the case
that motivates it: a naive read sees good news, and the offering is
dilution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from catalyst.data import RawEvent

SOURCE = "alpaca_news"
NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
#: Alpaca's own per-request ceiling for this endpoint.
MAX_PAGE_LIMIT = 50
#: Bound on firehose paging per fetch. Alpaca allows 200 requests/minute
#: per key; the bot makes tens of calls a cycle elsewhere, so this stays
#: far inside it while still covering a normal news day.
MAX_PAGES = 20


@dataclass(frozen=True)
class Pattern:
    key: str
    catalyst_type: str
    #: -1 bad for the equity, +1 good, 0 notable but not directional.
    #: A HINT for conjunction only. It never reaches sizing.
    hint: int
    regex: object


def _p(key, catalyst, hint, pattern):
    return Pattern(key, catalyst, hint, re.compile(pattern, re.I))


#: Order matters: the FIRST match wins, so the more specific and more
#: consequential patterns come first. Dilution before earnings, because
#: "Files Prospectus for At-The-Market Offering" and "Q2 EPS Beats" turn
#: up on the same company on the same day, and the offering is the one
#: that moves the share count.
PATTERNS: tuple[Pattern, ...] = (
    _p("offering", "dilution", -1,
       r"\b(at-the-market|ATM offering|public offering|registered direct"
       r"|files? prospectus|shelf registration|convertible notes offering"
       r"|private placement)\b"),
    _p("going_concern", "distress", -1,
       r"\b(going concern|chapter 11|bankrupt|delisting|deficiency letter"
       r"|reverse split)\b"),
    _p("trial_fail", "clinical_readout", -1,
       r"\b(fail(?:s|ed)? to meet|did not meet|missed? (?:the )?primary"
       r"|discontinu\w+ (?:the )?(?:trial|study|program)|halts? trial"
       r"|clinical hold)\b"),
    _p("trial_win", "clinical_readout", +1,
       r"\b(met (?:the )?primary|positive (?:topline|results|data)"
       r"|statistically significant)\b"),
    _p("fda_approval", "fda_decision", +1,
       r"\b(FDA approv(?:e|es|ed)|approval of|receiv(?:es|ed) FDA"
       r"|grant(?:s|ed)? (?:full |accelerated )?approval"
       r"|breakthrough therapy|priority review)\b"),
    _p("fda_reject", "fda_decision", -1,
       r"\b(complete response letter|CRL\b|FDA reject|refuse to file)\b"),
    _p("merger", "merger", +1,
       r"\b(to be acquired|acquisition of|merger agreement|to acquire"
       r"|takeover bid|tender offer)\b"),
    _p("guidance_up", "guidance", +1,
       r"\b(rais(?:es|ed|ing) (?:FY|full[- ]year|20\d\d)? ?guidance"
       r"|boost(?:s|ed)? (?:its )?outlook|rais(?:es|ed) outlook)\b"),
    _p("guidance_down", "guidance", -1,
       r"\b(cut(?:s|ting)? guidance|lower(?:s|ed|ing)? (?:its )?(?:guidance|outlook)"
       r"|withdraw(?:s|n)? guidance|warns? on)\b"),
    # EPS ONLY, AND THE VERB MUST FOLLOW IT. Benzinga's format is
    # "Q2 Adj. EPS $(0.57) Misses $(0.51) Estimate, Sales $X Beats $Y" -
    # one headline carrying both verbs. A loose "beats? .*estimate"
    # matched the SALES clause and stamped an EPS miss as good news,
    # which showed up live on SCYX, AMPY, PAL and CJT. Anchoring on
    # "EPS ... <verb>" and handling the mixed case in classify() is what
    # fixes it; a sentiment read that calls a miss a beat is worse than
    # no sentiment read.
    _p("earnings_beat", "earnings_result", +1,
       r"\bEPS\b[^,;]{0,40}?\bbeats?\b"),
    _p("earnings_miss", "earnings_result", -1,
       r"\bEPS\b[^,;]{0,40}?\bmisses?\b"),
    # PAST TENSE IS THE COMMON FORM in headlines - "Acme Downgraded To
    # Neutral", "Upgraded By Baird" - and "downgrades?" matches neither.
    # This silently classified every past-tense analyst story as plain
    # "news", which is the quietest possible failure: no error, no zero,
    # just a whole category missing from the links.
    _p("analyst_up", "analyst_action", +1,
       r"\b(upgrad(?:e|es|ed|ing)|rais(?:es|ed|ing) price target"
       r"|initiat(?:es|ed) .*\bbuy\b|maintains? buy|reiterates? buy)\b"),
    _p("analyst_down", "analyst_action", -1,
       r"\b(downgrad(?:e|es|ed|ing)|cut(?:s|ting)? price target"
       r"|lower(?:s|ed|ing)? price target)\b"),
    _p("insider_news", "insider_cluster", +1,
       r"\b(insider buying|director buys?|CEO buys?)\b"),
    _p("leadership", "leadership_change", 0,
       r"\b(appoints?|names? (?:new )?(?:CEO|CFO|chief)|steps? down"
       r"|resigns?|departure of)\b"),
    _p("earnings_date", "earnings", 0,
       r"\b(to report .*results|schedules? .*(?:conference call|earnings)"
       r"|to host .*call)\b"),
)


def classify(headline: str, summary: str = "") -> tuple[str, int, str]:
    """(catalyst_type, direction_hint, pattern_key) for one story.

    Matches the HEADLINE first and only falls back to the summary. A
    summary often recaps unrelated background - "shares fell after the
    company, which last year raised guidance, ..." - and matching that
    would attribute the wrong event to the story.
    """
    for text in (headline or "", summary or ""):
        if not text.strip():
            continue
        hits = [p for p in PATTERNS if p.regex.search(text)]
        if not hits:
            continue
        # A HEADLINE CARRYING BOTH DIRECTIONS IS NOT DIRECTIONAL. "EPS
        # Misses, Sales Beats" is genuinely mixed, and picking whichever
        # pattern happens to sit higher in the table would turn the
        # table's ORDER into a market view. Same catalyst type, hint 0,
        # and the pattern key says it was mixed so the dashboard can
        # show why.
        signs = {p.hint for p in hits if p.hint}
        if len(signs) > 1 and len({p.catalyst_type for p in hits}) == 1:
            return hits[0].catalyst_type, 0, f"{hits[0].key}+mixed"
        return hits[0].catalyst_type, hits[0].hint, hits[0].key
    return "news", 0, ""


@dataclass
class NewsResult:
    events: list = field(default_factory=list)
    requests_made: int = 0
    items_seen: int = 0
    distinct_symbols: int = 0
    truncated: bool = False
    raw_sample: dict | None = None      # house rule 3: a zero shows its payload
    error: str = ""


def _default_http_get(url: str, headers: dict, params: dict):
    import httpx

    return httpx.get(url, headers=headers, params=params, timeout=30.0)


#: A story listing this many tickers is a roundup ("Earnings Volatility
#: Watch: Applied Materials and 10 Other Stocks"), not news about any of
#: them. Attributing it to all eleven would let one wire story
#: manufacture eleven conjunctions.
MAX_SYMBOLS_PER_STORY = 4


def fetch_events(
    since: datetime | date,
    until: datetime | date | None = None,
    *,
    alpaca_key: str,
    alpaca_secret: str,
    symbols: list | None = None,
    http_get: Callable | None = None,
    max_pages: int = MAX_PAGES,
    now: Callable[[], datetime] | None = None,
) -> NewsResult:
    """News as RawEvents.

    symbols=None  -> the FIREHOSE. Discovery: whatever is being written
                     about, including tickers no other feed surfaced.
    symbols=[...] -> enrichment for candidates already found.

    Never pass an empty list: Alpaca treats it as a filter matching
    nothing, so it returns zero items and looks exactly like a quiet
    news day (TRAPS.md). That is refused here rather than sent.
    """
    if symbols is not None and not symbols:
        raise ValueError(
            "symbols=[] is a filter matching NOTHING on this endpoint, not "
            "'everything' (TRAPS.md). Pass None for the firehose.")
    getter = http_get or _default_http_get
    clock = now or (lambda: datetime.now(timezone.utc))
    fetched_at = clock()
    start = since.date() if isinstance(since, datetime) else since
    end = (until.date() if isinstance(until, datetime) else until) or None

    headers = {"APCA-API-KEY-ID": alpaca_key,
               "APCA-API-SECRET-KEY": alpaca_secret}
    result = NewsResult()
    seen_ids: set = set()
    all_symbols: set = set()
    token = None
    for _page in range(max_pages):
        params = {"start": start.isoformat(), "limit": MAX_PAGE_LIMIT,
                  "sort": "desc", "include_content": "false"}
        if end:
            params["end"] = end.isoformat()
        if symbols:
            params["symbols"] = ",".join(symbols)
        if token:
            params["page_token"] = token
        response = getter(NEWS_URL, headers, params)
        result.requests_made += 1
        status = int(getattr(response, "status_code", 0))
        if status != 200:
            result.error = (f"HTTP {status}: "
                            f"{str(getattr(response, 'text', ''))[:500]}")
            return result
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            result.error = f"news body is not JSON: {type(exc).__name__}"
            result.raw_sample = {"text": str(getattr(response, "text", ""))[:1000]}
            return result
        items = (body or {}).get("news") or []
        if result.raw_sample is None:
            # Kept whatever happens, so an empty result can print its own
            # upstream payload instead of being an unexplained zero.
            result.raw_sample = {"first_page_item_count": len(items),
                                 "has_next": bool((body or {}).get("next_page_token")),
                                 "params": {k: v for k, v in params.items()
                                            if k not in ("page_token",)}}
        for item in items:
            item_id = str(item.get("id") or "")
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            result.items_seen += 1
            # NON-US LISTINGS ARE NOT TRADEABLE HERE. The feed carries
            # exchange-prefixed foreign tickers ("TSX:CJT"), and this is
            # a US-equities cash account - carrying them would put
            # candidates in the funnel that can never become an order.
            # A SYMBOL HAS TO BE A STRING BEFORE IT IS A SYMBOL.
            #
            # OWNER'S BUNDLE, 2026-08-26: 24 requests a day to
            # /v2/stocks/NONE/quotes/latest, all 404. A null inside a
            # story's `symbols` array survived the guard below, because
            # str(None) is "None" - truthy, no colon - and .upper() made
            # it the ticker NONE. It then flowed into a candidate, and
            # the quote gate spent a request a cycle proving it does not
            # exist.
            #
            # Stringifying BEFORE validating is what did it: the check
            # has to be about what the value is, not about what it looks
            # like once str() has had a go at it.
            tickers = [s.strip().upper()
                       for s in (item.get("symbols") or [])
                       if isinstance(s, str) and s.strip() and ":" not in s]
            if not tickers or len(tickers) > MAX_SYMBOLS_PER_STORY:
                continue
            headline = str(item.get("headline") or "")
            summary = str(item.get("summary") or "")
            catalyst, hint, key = classify(headline, summary)
            all_symbols.update(tickers)
            for ticker in tickers:
                result.events.append(RawEvent(
                    source=SOURCE,
                    source_id=f"{item_id}:{ticker}",
                    fetched_at=fetched_at,
                    payload_raw={
                        "ticker": ticker,
                        "all_symbols": tickers,
                        "headline": headline,
                        "summary": summary[:1000],
                        "publisher": str(item.get("source") or ""),
                        "author": str(item.get("author") or ""),
                        "url": str(item.get("url") or ""),
                        "published_at": str(item.get("created_at") or ""),
                        "filed_date": str(item.get("created_at") or "")[:10],
                        "catalyst_type": catalyst,
                        # HINT ONLY. Never reaches sizing; exists so a
                        # conjunction can say the news and the filings
                        # disagree.
                        "direction_hint": hint,
                        "matched_pattern": key,
                    }))
        token = (body or {}).get("next_page_token")
        if not token:
            break
    else:
        result.truncated = True
    result.distinct_symbols = len(all_symbols)
    return result


def default_window(now: datetime | None = None, days: int = 3) -> tuple[date, date]:
    """News ages far faster than a filing. A three-day window is what
    "recent" means here - TRAPS.md's rule that freshness is judged by
    TYPE, applied to the fastest-moving source the bot has."""
    today = (now or datetime.now(timezone.utc)).date()
    return today - timedelta(days=days), today
