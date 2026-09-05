"""The hunt's hands: search filings, search news, read a filing.

WHY. Until 2026-09-05 the hunt was ONE forced call: read a digest of
220 feed items cut at 320 characters each, and nominate from it. No
search, no reading, no way to chase a hunch. The owner's ask - "he
needs to get really creative with finding links" - is not answerable
with a prompt change; a model that cannot look anything up cannot find
a link the feed did not already print.

THE RULE THAT DOES NOT CHANGE. A nomination may only cite events that
exist. What changes is who decides which events exist: every tool
result here is written to `raw_events` (INSERT OR IGNORE, same row
shape as the feeds) and added to the hunt's citable set, so the model
can search for something, find it, and cite it - and still cannot
conjure a filing, move one onto a different ticker, or name a date the
source does not carry. hunt._validate runs unchanged on the result.

WHAT IT CAN DO:

  search_filings  EDGAR full-text search over the last 21 days, with a
                  phrase the MODEL chose. The mechanical feed runs a
                  fixed table of sixteen phrases (edgar_fts.QUERIES);
                  this is everything that table does not say.
  search_news     Alpaca news for named symbols - the same feed the
                  bot already pays for, pointed at a company the model
                  wants to know about.
  read_filing     The text of any filing in the feed or found above,
                  from the SEC archive, capped. The digest is 320
                  characters; the DATE of a shareholder vote, a PDUFA
                  goal, a court hearing lives in the body.

WHAT BOUNDS IT. MAX_TOOL_CALLS per hunt, READ_CHARS per filing,
SEARCH_HITS per search, every SEC request through the shared pacer, and
every turn re-authorised by the governor - see hunt.py. A rate-limit
block from sec.gov disables the SEC tools for the rest of the hunt.

Every tool is a plain callable so tests inject fakes and the hunt
never touches the network offline. Never raises into the hunt: a tool
that fails returns an is_error result the model can read.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from catalyst.data import RawEvent

#: Tool calls per hunt, all kinds together. Eight is enough to search
#: twice, read four filings and check the news on two names; the
#: governor's per-turn check is the hard bound underneath.
MAX_TOOL_CALLS = 8
#: Characters of a filing handed back per read. ~3k tokens; a 10-K is
#: hundreds of thousands, and the dated event is almost always in the
#: first pages or the cover.
READ_CHARS = 12_000
#: Hits per search. More is a longer prompt, not a better answer.
SEARCH_HITS = 25
#: How far back a hunt search looks - the same window the fixed feed
#: uses, so a hit is comparable evidence.
SEARCH_DAYS = 21
#: Digest line length for a tool result, matching hunt._digest.
DIGEST_CHARS = 320

SEC_TOOLS = ("search_filings", "read_filing")

SEARCH_FILINGS_TOOL = {
    "name": "search_filings",
    "description": (
        "Full-text search of SEC filings from the last 21 days for a phrase "
        "YOU choose. Use it for what the fixed feed does not look for. "
        "Returns up to 25 filings with their source_id, ticker, company and "
        "a snippet; each result is citable in nominate_candidates by its "
        "source_id. Quote exact phrases in double quotes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "phrase": {"type": "string",
                       "description": "EDGAR full-text query, e.g. "
                                      "'\"special meeting\" \"merger\"'."},
            "forms": {"type": "string",
                      "description": "Optional form filter, e.g. '8-K', "
                                     "'DEFA14A', 'S-4'. Empty = every form."},
            "catalyst_type": {"type": "string",
                              "description": "What kind of catalyst you are "
                                             "looking for with this search."},
        },
        "required": ["phrase", "catalyst_type"],
        "additionalProperties": False,
    },
}

SEARCH_NEWS_TOOL = {
    "name": "search_news",
    "description": (
        "Recent news for up to four named symbols, from the same feed the "
        "system already reads. Use it to check whether an event you found "
        "in a filing has been reported, or what is being said about a name. "
        "Each item is citable by its source_id."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {"type": "array", "items": {"type": "string"},
                        "minItems": 1, "maxItems": 4},
        },
        "required": ["symbols"],
        "additionalProperties": False,
    },
}

READ_FILING_TOOL = {
    "name": "read_filing",
    "description": (
        "The text of one SEC filing - anything in the feed or anything "
        "search_filings returned - capped at the first 12,000 characters. "
        "The digest shows 320 characters; the date of the vote, hearing, "
        "decision or readout is in the body. Pass the source_id exactly."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"source_id": {"type": "string"}},
        "required": ["source_id"],
        "additionalProperties": False,
    },
}

TOOL_SCHEMAS = {
    "search_filings": SEARCH_FILINGS_TOOL,
    "search_news": SEARCH_NEWS_TOOL,
    "read_filing": READ_FILING_TOOL,
}


def tool_schemas(searchers: dict | None) -> list[dict]:
    """The tools to offer: only those a live searcher exists for."""
    return [TOOL_SCHEMAS[name] for name in TOOL_SCHEMAS
            if searchers and callable(searchers.get(name))]


def _digest_events(events: list) -> str:
    lines = []
    for e in events:
        try:
            text = json.dumps(e.payload_raw, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(e.payload_raw)
        lines.append(f"[{e.source}] {e.source_id}\n  "
                     + text[:DIGEST_CHARS].replace("\n", " "))
    return "\n".join(lines)


def store_events(conn, events: list) -> int:
    """Write found events beside the feed's own, same row shape. Never
    raises - an unstorable event is still citable this hunt."""
    n = 0
    for ev in events:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO raw_events VALUES (?,?,?,?)",
                (ev.source, ev.source_id, ev.fetched_at.isoformat(),
                 json.dumps(ev.payload_raw, default=str)))
            n += 1
        except Exception:  # noqa: BLE001
            continue
    try:
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    return n


def run_tool(name: str, inputs: dict, searchers: dict, by_id: dict, conn,
             now: datetime) -> tuple[str, bool]:
    """Execute one tool call -> (text for the model, is_error).

    New events land in `by_id` (citable) and in raw_events (stored).
    """
    fn = (searchers or {}).get(name)
    if not callable(fn):
        return f"{name} is not available in this hunt.", True
    inputs = inputs if isinstance(inputs, dict) else {}
    try:
        if name == "read_filing":
            sid = str(inputs.get("source_id") or "").strip()
            ev = by_id.get(sid)
            if ev is None:
                return (f"No event with source_id {sid!r} is in the feed or "
                        "in anything you searched. Cite ids exactly."), True
            text = fn(ev)
            if not text:
                return (f"{sid}: nothing readable came back (not an SEC "
                        "archive document, or the fetch failed)."), True
            return f"[{ev.source}] {sid}\n{text[:READ_CHARS]}", False
        if name == "search_filings":
            events = list(fn(str(inputs.get("phrase") or ""),
                             str(inputs.get("forms") or ""),
                             str(inputs.get("catalyst_type") or "hunt"),
                             now) or ())
        elif name == "search_news":
            symbols = [str(s).strip().upper() for s in
                       (inputs.get("symbols") or []) if str(s).strip()][:4]
            if not symbols:
                return "search_news needs at least one symbol.", True
            events = list(fn(symbols, now) or ())
        else:
            return f"unknown tool {name!r}", True
    except Exception as exc:  # noqa: BLE001 - the model reads the failure
        blocked = type(exc).__name__ == "RateLimitBlocked"
        if blocked:
            for sec_tool in SEC_TOOLS:
                searchers.pop(sec_tool, None)
            return ("sec.gov rate-limited this address; no further filing "
                    "searches or reads are possible in this hunt. Nominate "
                    "from what you have."), True
        return f"{name} failed: {type(exc).__name__}: {str(exc)[:300]}", True

    events = [e for e in events if isinstance(e, RawEvent) and e.source_id]
    if not events:
        return (f"{name} found nothing for {json.dumps(inputs)[:200]}. That "
                "is an answer: try a different phrase, or move on."), False
    for e in events:
        by_id.setdefault(e.source_id, e)
    store_events(conn, events)
    return (f"{len(events)} result(s); each source_id below is citable.\n"
            + _digest_events(events)), False


# --------------------------------------------------------------- live


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def _strip_markup(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = _WS_RE.sub(" ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _archive_url(ev: RawEvent) -> str | None:
    p = ev.payload_raw if isinstance(ev.payload_raw, dict) else {}
    accession = str(p.get("accession") or "").strip()
    cik = str(p.get("cik") or p.get("issuer_cik") or "").strip().lstrip("0")
    if not accession or not cik:
        return None
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accession.replace('-', '')}/{accession}.txt")


def live_searchers(alpaca_key: str | None, alpaca_secret: str | None,
                   http_get=None) -> dict:
    """The real tools, bound to credentials. Each is a plain callable.

    search_filings(phrase, forms, catalyst_type, now) -> [RawEvent]
    search_news(symbols, now) -> [RawEvent]
    read_filing(event) -> str
    """
    from catalyst.data.sources import edgar_fts
    from catalyst.data.sources.edgar_form4 import (
        _default_http_get, _request, sec_pacer, user_agent,
    )

    def search_filings(phrase, forms, catalyst_type, now):
        key = "hunt:" + re.sub(r"[^a-z0-9]+", "_", phrase.lower())[:40]
        q = edgar_fts.Query(key=key, phrase=phrase, catalyst_type=catalyst_type,
                            forms=forms or "")
        until = now.date()
        since = until - timedelta(days=SEARCH_DAYS)
        hits, _made, _summary = edgar_fts.search_one(q, since, until,
                                                     max_hits=SEARCH_HITS)
        out = []
        for hit in hits:
            ev = edgar_fts.hit_to_event(hit, q, now)
            if ev is not None:
                out.append(ev)
        return out

    def search_news(symbols, now):
        if not alpaca_key or not alpaca_secret:
            raise RuntimeError("no Alpaca credentials for the news feed")
        from catalyst.data.sources import alpaca_news

        start, end = alpaca_news.default_window(now, days=SEARCH_DAYS)
        res = alpaca_news.fetch_events(start, end, alpaca_key=alpaca_key,
                                       alpaca_secret=alpaca_secret,
                                       symbols=list(symbols), max_pages=2)
        if res.error:
            raise RuntimeError(res.error[:300])
        return list(res.events)

    def read_filing(ev):
        url = _archive_url(ev)
        if url is None:
            return ""
        resp = _request(url, http_get=http_get or _default_http_get,
                        limiter=sec_pacer(), sleep=time.sleep,
                        headers={"User-Agent": user_agent(None),
                                 "Accept-Encoding": "gzip, deflate"},
                        tolerate=(403, 404))
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            return ""
        return _strip_markup(str(getattr(resp, "text", "") or ""))[:READ_CHARS]

    out = {"search_filings": search_filings, "read_filing": read_filing}
    if alpaca_key and alpaca_secret:
        out["search_news"] = search_news
    return out
