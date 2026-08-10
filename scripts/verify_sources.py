#!/usr/bin/env python3
"""Verify every data source the catalyst backtest and pipeline depend on.

One minimal request per source. Prints, for each:

    source name | HTTP status | response-shape summary | fields relied on | PASS/FAIL

Run it on the VPS after install, and any time something looks empty. It is
deliberately self-contained: stdlib + httpx, no project imports, no config
file, no database. It never writes anything.

    python3 scripts/verify_sources.py            # all sources
    python3 scripts/verify_sources.py --only sec # substring filter on name

Credentials: read from the environment by name only (ALPACA_KEY,
ALPACA_SECRET_KEY). They are never printed, never logged, and never included
in an error message. If they are absent the Alpaca checks SKIP with an
explanation -- a skip is not a failure.

Rules this script obeys, because they cost money to learn:

  * A network failure prints the raw exception. "No data" and "the socket
    died" must never look the same.
  * A 200 with an empty body is a FAIL with the raw body printed beside it.
  * SEC is rate limited to 10 req/s across all its APIs and answers an
    overrun with a temporary IP block, so every request is spaced and every
    SEC request carries a contactable User-Agent.
  * clinicaltrials.gov sits behind bot detection that cross-checks the
    declared User-Agent against the TLS client fingerprint. httpx with a
    custom UA that does not mention httpx gets a hard 403. See CTGOV_UA.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

try:
    import httpx
except ImportError:  # pragma: no cover - install guidance, not logic
    sys.stderr.write("httpx is required: pip install httpx\n")
    raise SystemExit(2)


# --------------------------------------------------------------------------
# User-Agent strings. These are not interchangeable -- see the module docstring.
# --------------------------------------------------------------------------

CONTACT = os.environ.get("CATALYST_CONTACT_EMAIL", "billysawyer0@gmail.com")

# SEC *requires* a contactable UA and blocks generic ones.
SEC_UA = f"Catalyst Research {CONTACT}"

# clinicaltrials.gov's edge rejects httpx traffic whose UA does not look like
# a Python HTTP client. Verified 2026-08-10: "catalyst-research/0.1" -> 403,
# "catalyst-research/0.1 python-httpx" -> 200, from the identical client.
CTGOV_UA = f"catalyst-research/0.1 python-httpx ({CONTACT})"

# Everything else is happy with a plain descriptive UA.
GENERIC_UA = f"catalyst-research/0.1 ({CONTACT})"

TIMEOUT = 45.0
PAUSE = 0.30  # seconds between requests; keeps us <= ~3 req/s at SEC


# --------------------------------------------------------------------------
# Result plumbing
# --------------------------------------------------------------------------

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Result:
    name: str = ""
    url: str = ""
    status: str = ""          # HTTP status as a string, or "" when none
    shape: str = ""           # one-line description of what came back
    fields: str = ""          # the specific fields we rely on
    verdict: str = FAIL
    detail: str = ""          # raw error / raw body when something is wrong
    notes: list[str] = field(default_factory=list)


def _client(ua: str) -> httpx.Client:
    ctx: Any = True
    ca = os.environ.get("SSL_CERT_FILE")
    if ca and os.path.exists(ca):
        ctx = ssl.create_default_context(cafile=ca)
    return httpx.Client(
        verify=ctx,
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": ua},
    )


def _preview(text: str, n: int = 300) -> str:
    return text[:n].replace("\n", " ").replace("\r", " ")


def check(name: str) -> Callable:
    """Decorator: wrap a probe so an exception becomes a FAIL with a traceback
    rather than killing the run."""

    def wrap(fn: Callable[[], Result]) -> Callable[[], Result]:
        def inner() -> Result:
            try:
                r = fn()
                r.name = name
                return r
            except Exception as exc:  # noqa: BLE001 - reporting, not handling
                return Result(
                    name=name,
                    verdict=FAIL,
                    status="-",
                    shape="request raised before a response was seen",
                    detail=f"{type(exc).__name__}: {exc}\n"
                    + "".join(traceback.format_exc().splitlines(keepends=True)[-4:]),
                )

        inner._probe_name = name  # type: ignore[attr-defined]
        return inner

    return wrap


def _http_result(
    r: httpx.Response,
    shape: str,
    fields: str,
    ok: bool,
    detail: str = "",
) -> Result:
    return Result(
        url=str(r.request.url),
        status=str(r.status_code),
        shape=shape,
        fields=fields,
        verdict=PASS if ok else FAIL,
        detail=detail if detail else ("" if ok else f"raw body: {_preview(r.text, 500)}"),
    )


# --------------------------------------------------------------------------
# Date helpers -- probes must not go stale, so windows are computed, not fixed
# --------------------------------------------------------------------------

def _recent_weekday(back: int = 3) -> date:
    d = datetime.now(timezone.utc).date() - timedelta(days=back)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _last_completed_quarter() -> str:
    today = date.today()
    q = (today.month - 1) // 3 + 1
    y = today.year
    q -= 1
    if q == 0:
        q, y = 4, y - 1
    # the quarter just ended is published ~6 weeks later; step back one more
    q -= 1
    if q == 0:
        q, y = 4, y - 1
    return f"{y}q{q}"


# ==========================================================================
# Alpaca -- the only paid source. Keys by env var name only.
# ==========================================================================

def _alpaca_headers() -> dict[str, str] | None:
    k = os.environ.get("ALPACA_KEY")
    s = os.environ.get("ALPACA_SECRET_KEY")
    if not k or not s:
        return None
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


_ALPACA_SKIP = (
    "ALPACA_KEY / ALPACA_SECRET_KEY not set in the environment. "
    "This is a skip, not a failure -- set them (or run the dashboard setup "
    "form) and re-run to check the paid feed."
)


@check("Alpaca daily bars (SIP, adjustment=all) -- backtest price spine + SPY benchmark")
def probe_alpaca_daily() -> Result:
    h = _alpaca_headers()
    if h is None:
        return Result(name="", verdict=SKIP, shape=_ALPACA_SKIP)
    with _client(GENERIC_UA) as c:
        r = c.get(
            "https://data.alpaca.markets/v2/stocks/SPY/bars",
            params={
                "start": "2016-01-04",
                "end": "2016-01-08",
                "timeframe": "1Day",
                "feed": "sip",
                "adjustment": "all",
                "limit": 3,
            },
            headers=h,
        )
    fields = "bars[].t,o,h,l,c,v,vw,n (adjustment=all gives the dividend+split adjusted series the SPY total-return benchmark needs)"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    bars = j.get("bars")
    if not bars:
        return _http_result(
            r, "200 but bars was null/empty", fields, ok=False,
            detail=f"raw body: {_preview(r.text, 400)}",
        )
    b = bars[0]
    ok = all(k in b for k in ("t", "o", "h", "l", "c", "v"))
    res = _http_result(
        r,
        f"{len(bars)} bars; first={b.get('t')} o={b.get('o')} c={b.get('c')} v={b.get('v')}; "
        f"next_page_token={'yes' if j.get('next_page_token') else 'no'}",
        fields, ok=ok,
    )
    res.notes.append(
        "SIP daily history on this account starts 2016-01-04; earlier starts return "
        "bars:null with HTTP 200. feed=iex is NOT a substitute (see DATA-SOURCES.md)."
    )
    return res


@check("Alpaca minute bars (SIP) -- intraday fills and gap measurement")
def probe_alpaca_minute() -> Result:
    h = _alpaca_headers()
    if h is None:
        return Result(name="", verdict=SKIP, shape=_ALPACA_SKIP)
    with _client(GENERIC_UA) as c:
        r = c.get(
            "https://data.alpaca.markets/v2/stocks/SPY/bars",
            params={
                "start": "2016-01-04T14:30:00Z",
                "end": "2016-01-04T14:35:00Z",
                "timeframe": "1Min",
                "feed": "sip",
                "limit": 3,
            },
            headers=h,
        )
    fields = "bars[].t,o,h,l,c,v,n -- minute granularity for entry/exit modelling"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    bars = r.json().get("bars")
    if not bars:
        return _http_result(r, "200 but bars was null/empty", fields, ok=False)
    return _http_result(
        r, f"{len(bars)} minute bars; first={bars[0].get('t')} c={bars[0].get('c')}",
        fields, ok=True,
    )


@check("Alpaca assets (paper trading API) -- tradable universe")
def probe_alpaca_assets() -> Result:
    h = _alpaca_headers()
    if h is None:
        return Result(name="", verdict=SKIP, shape=_ALPACA_SKIP)
    with _client(GENERIC_UA) as c:
        r = c.get(
            "https://paper-api.alpaca.markets/v2/assets",
            params={"status": "active", "asset_class": "us_equity"},
            headers=h,
        )
    fields = "symbol, exchange, tradable, fractionable, shortable, easy_to_borrow, status"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    if not isinstance(j, list) or not j:
        return _http_result(r, "200 but not a non-empty list", fields, ok=False)
    tradable = sum(1 for a in j if a.get("tradable"))
    frac = sum(1 for a in j if a.get("fractionable"))
    res = _http_result(
        r,
        f"{len(j)} active us_equity assets; tradable={tradable} fractionable={frac}; "
        f"exchanges={sorted({a.get('exchange') for a in j})}",
        fields, ok=True,
    )
    res.notes.append(
        "status=inactive does NOT enumerate delisted names (SIVB/FRC/TWTR/ATVI absent), "
        "so the backtest universe cannot be made survivorship-free from this endpoint alone."
    )
    return res


@check("Alpaca news (Benzinga) -- event provenance")
def probe_alpaca_news() -> Result:
    h = _alpaca_headers()
    if h is None:
        return Result(name="", verdict=SKIP, shape=_ALPACA_SKIP)
    with _client(GENERIC_UA) as c:
        r = c.get(
            "https://data.alpaca.markets/v1beta1/news",
            params={"symbols": "AAPL", "limit": 1},
            headers=h,
        )
    fields = "news[].created_at, headline, symbols[], source, url"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    items = r.json().get("news")
    if not items:
        return _http_result(r, "200 but news was empty", fields, ok=False)
    return _http_result(
        r, f"{len(items)} article(s); latest={items[0].get('created_at')} "
           f"symbols={items[0].get('symbols')}",
        fields, ok=True,
    )


@check("Alpaca corporate actions -- splits, mergers, dividends")
def probe_alpaca_corpactions() -> Result:
    h = _alpaca_headers()
    if h is None:
        return Result(name="", verdict=SKIP, shape=_ALPACA_SKIP)
    end = _recent_weekday(1)
    start = end - timedelta(days=14)
    with _client(GENERIC_UA) as c:
        r = c.get(
            "https://data.alpaca.markets/v1/corporate-actions",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "types": "cash_merger,stock_merger,stock_and_cash_merger,forward_split,reverse_split",
                "limit": 10,
            },
            headers=h,
        )
    fields = "corporate_actions.{type}[].symbol, process_date, ex_date, payable_date"
    if r.status_code != 200:
        return _http_result(r, "non-200 (note: /v2/corporate-actions 404s -- the path is /v1/)", fields, ok=False)
    ca = r.json().get("corporate_actions") or {}
    counts = {k: len(v) for k, v in ca.items() if isinstance(v, list)}
    return _http_result(
        r, f"types returned: {counts or 'none in window'} over {start}..{end}",
        fields, ok=True,
        detail="" if counts else f"empty is plausible for a 14-day merger window; raw body: {_preview(r.text, 300)}",
    )


# ==========================================================================
# SEC -- all keyless, all require a contactable User-Agent, 10 req/s ceiling
# ==========================================================================

@check("SEC company_tickers.json -- ticker to CIK map")
def probe_sec_tickers() -> Result:
    with _client(SEC_UA) as c:
        r = c.get("https://www.sec.gov/files/company_tickers.json")
    fields = "{index: {cik_str, ticker, title}}"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    return _http_result(
        r, f"{len(j)} ticker->CIK entries; sample={list(j.values())[0] if j else None}",
        fields, ok=bool(j),
    )


@check("SEC data.sec.gov submissions -- per-company filing history (Candidate A/C event clock)")
def probe_sec_submissions() -> Result:
    with _client(SEC_UA) as c:
        r = c.get("https://data.sec.gov/submissions/CIK0000320193.json")
    fields = ("cik, name, tickers[], sic, filings.recent.{accessionNumber, filingDate, "
              "reportDate, acceptanceDateTime, form, primaryDocument}")
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    rec = (j.get("filings") or {}).get("recent") or {}
    n = len(rec.get("form", []))
    ok = n > 0 and "acceptanceDateTime" in rec
    return _http_result(
        r,
        f"entity={j.get('name')} tickers={j.get('tickers')}; recent filings={n}; "
        f"acceptanceDateTime present={'acceptanceDateTime' in rec}",
        fields, ok=ok,
    )


@check("SEC XBRL companyconcept -- as-reported fundamentals (Candidate A core dependency)")
def probe_sec_companyconcept() -> Result:
    url = ("https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/"
           "RevenueFromContractWithCustomerExcludingAssessedTax.json")
    with _client(SEC_UA) as c:
        r = c.get(url)
    fields = "units.USD[].{start, end, val, fy, fp, form, filed, accn, frame} -- 'filed' is the point-in-time key"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    usd = (j.get("units") or {}).get("USD") or []
    ok = bool(usd) and "filed" in (usd[0] if usd else {})
    return _http_result(
        r, f"{len(usd)} USD facts for tag={j.get('tag')}; first filed={usd[0].get('filed') if usd else None}",
        fields, ok=ok,
    )


@check("SEC XBRL companyfacts -- every tag for one company (ranged GET)")
def probe_sec_companyfacts() -> Result:
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    with _client(SEC_UA) as c:
        r = c.get(url, headers={"Range": "bytes=0-300"})
    fields = "cik, entityName, facts.{dei,us-gaap}.<tag>.units.<uom>[].{val, end, filed, form, accn}"
    ok = r.status_code in (200, 206) and len(r.content) > 0
    size = r.headers.get("content-range", "").split("/")[-1] or "unknown"
    res = _http_result(
        r, f"HTTP {r.status_code}, full document {size} bytes; head={_preview(r.text, 90)}",
        fields, ok=ok,
    )
    res.notes.append("HEAD on data.sec.gov returns 403 -- use a ranged GET to size a document.")
    return res


@check("SEC XBRL frames -- one tag across every filer for one period")
def probe_sec_frames() -> Result:
    with _client(SEC_UA) as c:
        r = c.get("https://data.sec.gov/api/xbrl/frames/us-gaap/"
                  "EarningsPerShareDiluted/USD-per-shares/CY2024Q1.json")
    fields = "taxonomy, tag, ccp, uom, data[].{cik, entityName, val, start, end, accn}"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    data = j.get("data") or []
    res = _http_result(
        r, f"{len(data)} filers for {j.get('tag')} {j.get('ccp')}; sample cik={data[0].get('cik') if data else None}",
        fields, ok=bool(data),
    )
    res.notes.append(
        "frames carries NO 'filed' date -- it is restatement-contaminated. "
        "Use companyconcept/companyfacts 'filed' for anything point-in-time."
    )
    return res


@check("SEC EDGAR daily index -- every filing by form, by day")
def probe_sec_daily_index() -> Result:
    d = _recent_weekday(3)
    qtr = (d.month - 1) // 3 + 1
    url = f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{qtr}/form.{d:%Y%m%d}.idx"
    with _client(SEC_UA) as c:
        r = c.get(url)
    fields = "fixed-width columns: Form Type | Company Name | CIK | Date Filed | File Name"
    if r.status_code != 200:
        return _http_result(
            r, f"non-200 for {d} (a holiday or not-yet-posted day is a legitimate 404)",
            fields, ok=False,
        )
    lines = r.text.splitlines()
    body = [ln for ln in lines if ln[:1].isalnum() and "edgar/data/" in ln]
    return _http_result(
        r, f"{len(lines)} lines, {len(body)} filing rows for {d}; header={_preview(lines[0] if lines else '', 60)}",
        fields, ok=bool(body),
    )


@check("SEC Insider Transactions data sets -- quarterly Form 3/4/5 (Candidate C core dependency)")
def probe_sec_insider() -> Result:
    q = _last_completed_quarter()
    url = f"https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip"
    with _client(SEC_UA) as c:
        r = c.get(url, headers={"Range": "bytes=0-200"})
    fields = ("zip of TSVs: SUBMISSION.tsv(ACCESSION_NUMBER, FILING_DATE, PERIOD_OF_REPORT, "
              "DOCUMENT_TYPE, ISSUERCIK, ISSUERTRADINGSYMBOL, AFF10B5ONE), "
              "NONDERIV_TRANS.tsv(TRANS_DATE, TRANS_CODE, TRANS_SHARES, TRANS_PRICEPERSHARE, "
              "TRANS_ACQUIRED_DISP_CD, SHRS_OWND_FOLWNG_TRANS), REPORTINGOWNER.tsv(RPTOWNERCIK, "
              "RPTOWNERNAME, RPTOWNER_RELATIONSHIP, RPTOWNER_TITLE)")
    ok = r.status_code in (200, 206) and r.content[:2] == b"PK"
    size = r.headers.get("content-range", "").split("/")[-1] or "unknown"
    res = _http_result(
        r, f"HTTP {r.status_code} for {q}; zip magic={'yes' if r.content[:2] == b'PK' else 'no'}; "
           f"full archive {size} bytes",
        fields, ok=ok,
    )
    res.notes.append(
        "Deliberately a ranged GET -- the full archive is ~8-14 MB per quarter and the "
        "backtest wants ~42 of them. TRANS_CODE 'P' is an open-market purchase; "
        "AFF10B5ONE flags a 10b5-1 plan, which is the noise Candidate C must exclude."
    )
    return res


@check("SEC Financial Statement data sets -- quarterly as-filed XBRL extract")
def probe_sec_finstmt() -> Result:
    q = _last_completed_quarter()
    url = f"https://www.sec.gov/files/dera/data/financial-statement-data-sets/{q}.zip"
    with _client(SEC_UA) as c:
        r = c.get(url, headers={"Range": "bytes=0-200"})
    fields = "zip of sub.txt, num.txt, pre.txt, tag.txt -- sub.txt carries adsh, cik, form, period, filed, accepted"
    ok = r.status_code in (200, 206) and r.content[:2] == b"PK"
    size = r.headers.get("content-range", "").split("/")[-1] or "unknown"
    return _http_result(
        r, f"HTTP {r.status_code} for {q}; full archive {size} bytes", fields, ok=ok,
    )


@check("SEC Failure-to-Deliver data -- semi-monthly settlement fails")
def probe_sec_ftd() -> Result:
    d = date.today().replace(day=1) - timedelta(days=32)
    url = f"https://www.sec.gov/files/data/fails-deliver-data/cnsfails{d:%Y%m}a.zip"
    with _client(SEC_UA) as c:
        r = c.get(url, headers={"Range": "bytes=0-200"})
    fields = "pipe-delimited: SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE"
    ok = r.status_code in (200, 206) and r.content[:2] == b"PK"
    res = _http_result(r, f"HTTP {r.status_code} for {d:%Y-%m} first half", fields, ok=ok)
    res.notes.append(
        "The path is /files/data/fails-deliver-data/ -- the older "
        "/files/data/frequently-requested-foia-document-fails-deliver-data/ path now 404s."
    )
    return res


@check("SEC EDGAR full-text search (efts.sec.gov) -- phrase search inside filing bodies")
def probe_sec_efts() -> Result:
    with _client(SEC_UA) as c:
        r = c.get(
            "https://efts.sec.gov/LATEST/search-index",
            params={"q": '"PDUFA date"', "forms": "8-K"},
        )
    fields = ("hits.total.value, hits.hits[]._id (accession:document), "
              "_source.{ciks, display_names, file_date, file_type, root_form}")
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    total = ((j.get("hits") or {}).get("total") or {}).get("value")
    hits = (j.get("hits") or {}).get("hits") or []
    if j.get("error"):
        return _http_result(r, f"API error: {j['error']}", fields, ok=False)
    res = _http_result(
        r, f"total={total} hits, page={len(hits)}; sample={hits[0]['_source'].get('display_names') if hits else None}",
        fields, ok=bool(hits),
    )
    res.notes.append("Coverage starts 2001. A blank q returns a 200 with an error body, not a 4xx.")
    return res


# ==========================================================================
# FINRA -- keyless
# ==========================================================================

@check("FINRA Reg SHO daily short volume (CDN) -- consolidated short volume per symbol")
def probe_finra_regsho() -> Result:
    d = _recent_weekday(3)
    url = f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt"
    with _client(GENERIC_UA) as c:
        r = c.get(url)
    fields = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market"
    if r.status_code != 200:
        return _http_result(
            r, f"non-200 for {d} (403 AccessDenied = outside the retained window or a non-trading day)",
            fields, ok=False,
        )
    lines = r.text.splitlines()
    ok = bool(lines) and lines[0].startswith("Date|Symbol|ShortVolume")
    res = _http_result(
        r, f"{len(lines)} lines for {d}; header={lines[0] if lines else '(empty)'}; "
           f"row1={lines[1] if len(lines) > 1 else '(none)'}",
        fields, ok=ok,
    )
    res.notes.append(
        "Retention measured 2026-08-10: 2018-10-01 available, 2018-09-03 returns 403 "
        "AccessDenied. Treat as a rolling window and archive daily rather than "
        "assuming history stays fetchable. Values are fractional (odd-lot allocation)."
    )
    return res


@check("FINRA consolidated short interest API -- bi-monthly short interest")
def probe_finra_shortinterest() -> Result:
    with _client(GENERIC_UA) as c:
        r = c.post(
            "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
            json={"limit": 1},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
    fields = ("settlementDate, symbolCode, currentShortPositionQuantity, "
              "previousShortPositionQuantity, averageDailyVolumeQuantity, daysToCoverQuantity")
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    if not j:
        return _http_result(r, "200 but empty array", fields, ok=False)
    row = j[0]
    res = _http_result(
        r, f"{len(j)} row(s); settlementDate={row.get('settlementDate')} "
           f"symbol={row.get('symbolCode')} shortPos={row.get('currentShortPositionQuantity')}",
        fields, ok=True,
    )
    res.notes.append(
        "Keyless. Default response is CSV; send Accept: application/json for JSON. "
        "Unsorted default starts 2020-04-15. Sorting is rejected unless the partition "
        "key is pinned -- filter with compareFilters on settlementDate instead."
    )
    return res


# ==========================================================================
# Nasdaq Trader -- keyless
# ==========================================================================

@check("Nasdaq Trader trade halts RSS -- LULD and news halts, live")
def probe_nasdaq_halts() -> Result:
    with _client(GENERIC_UA) as c:
        r = c.get("https://www.nasdaqtrader.com/rss.aspx", params={"feed": "tradehalts"})
    fields = ("item/ndaq:{IssueSymbol, IssueName, HaltDate, HaltTime, ReasonCode, "
              "PauseThresholdPrice, ResumptionDate, ResumptionQuoteTime, ResumptionTradeTime}")
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    body = r.text
    n = body.count("<item>")
    ok = "<rss" in body and "ndaq:IssueSymbol" in body
    res = _http_result(
        r, f"RSS with {n} <item> entries; numItems tag present={'ndaq:numItems' in body}",
        fields, ok=ok,
        detail="" if ok else f"raw body: {_preview(body, 400)}",
    )
    res.notes.append(
        "LIVE ONLY. The &haltdate=MM/DD/YYYY parameter is accepted but returns "
        "numItems=0 for past dates -- it does not give history. A backtest needs "
        "halts captured forward from today, or reconstructed from minute bars."
    )
    return res


# ==========================================================================
# ClinicalTrials.gov -- keyless; the versioned history is Candidate D's premise
# ==========================================================================

@check("ClinicalTrials.gov v2 current record")
def probe_ctgov_current() -> Result:
    with _client(CTGOV_UA) as c:
        r = c.get(
            "https://clinicaltrials.gov/api/v2/studies/NCT04368728",
            params={"fields": "NCTId,OverallStatus,PrimaryCompletionDate,LastUpdatePostDate"},
        )
    fields = "protocolSection.identificationModule.nctId, statusModule.{overallStatus, primaryCompletionDateStruct, lastUpdatePostDateStruct}"
    if r.status_code != 200:
        return _http_result(
            r, "non-200", fields, ok=False,
            detail=("403 here almost always means the User-Agent was changed. This edge "
                    "cross-checks the declared UA against the TLS fingerprint: httpx must "
                    f"send a UA containing 'python-httpx'. Sent: {CTGOV_UA!r}. "
                    f"raw body: {_preview(r.text, 200)}"),
        )
    j = r.json()
    sm = (j.get("protocolSection") or {}).get("statusModule") or {}
    return _http_result(
        r, f"nctId={(j.get('protocolSection') or {}).get('identificationModule', {}).get('nctId')} "
           f"status={sm.get('overallStatus')} lastUpdatePost={sm.get('lastUpdatePostDateStruct')}",
        fields, ok=bool(sm),
    )


@check("ClinicalTrials.gov VERSION HISTORY (/api/int) -- point-in-time replay for Candidate D")
def probe_ctgov_history() -> Result:
    nct = "NCT04368728"
    with _client(CTGOV_UA) as c:
        r = c.get(f"https://clinicaltrials.gov/api/int/studies/{nct}/history")
        if r.status_code != 200:
            return _http_result(r, "non-200 on the change list", "changes[].{version,date,status,moduleLabels}", ok=False)
        changes = r.json().get("changes") or []
        if not changes:
            return _http_result(r, "200 but changes[] empty", "changes[]", ok=False)
        time.sleep(PAUSE)
        v = changes[len(changes) // 2]["version"]
        r2 = c.get(f"https://clinicaltrials.gov/api/int/studies/{nct}/history/{v}")
    fields = ("changes[].{version, date, status, studyType, moduleLabels[], lastUpdateSubmitQcDate}; "
              "then history/{version} -> {studyVersion, study.protocolSection.*} in the same shape as v2")
    if r2.status_code != 200:
        return _http_result(r2, f"change list OK ({len(changes)} versions) but version fetch failed", fields, ok=False)
    j2 = r2.json()
    sm = ((j2.get("study") or {}).get("protocolSection") or {}).get("statusModule") or {}
    ok = j2.get("studyVersion") is not None and bool(sm)
    res = _http_result(
        r2,
        f"{len(changes)} versions for {nct} from {changes[0]['date']} to {changes[-1]['date']}; "
        f"fetched version {j2.get('studyVersion')} -> overallStatus={sm.get('overallStatus')} "
        f"primaryCompletion={sm.get('primaryCompletionDateStruct')}",
        fields, ok=ok,
    )
    res.notes.append(
        "This is the /api/int/ INTERNAL endpoint. /api/v2/studies/{nct}/history returns 404. "
        "It is undocumented and unversioned -- it can change without notice, so the "
        "backtest should snapshot what it pulls rather than re-fetching on every run."
    )
    return res


# ==========================================================================
# Federal Register / openFDA -- keyless
# ==========================================================================

@check("Federal Register API -- scheduled agency actions and advisory committee meetings")
def probe_federal_register() -> Result:
    with _client(GENERIC_UA) as c:
        r = c.get(
            "https://www.federalregister.gov/api/v1/documents.json",
            params={
                "per_page": 2,
                "order": "newest",
                "conditions[type][]": "NOTICE",
                "conditions[agencies][]": "food-and-drug-administration",
            },
        )
    fields = "count, results[].{document_number, title, type, publication_date, agencies[], html_url, dates}"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    res_ = j.get("results") or []
    return _http_result(
        r, f"count={j.get('count')} total_pages={j.get('total_pages')}; "
           f"newest={res_[0].get('publication_date') if res_ else None}",
        fields, ok=bool(res_),
    )


@check("openFDA drugsfda -- approvals and submission status (retrospective only)")
def probe_openfda() -> Result:
    with _client(GENERIC_UA) as c:
        r = c.get("https://api.fda.gov/drug/drugsfda.json", params={"limit": 1})
    fields = ("meta.{last_updated, results.total}; results[].{application_number, sponsor_name, "
              "openfda.brand_name, products[], submissions[].{submission_type, submission_status, submission_status_date}}")
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    j = r.json()
    meta = j.get("meta") or {}
    res_ = j.get("results") or []
    res = _http_result(
        r, f"last_updated={meta.get('last_updated')} total={((meta.get('results') or {}).get('total'))}; "
           f"sample application={res_[0].get('application_number') if res_ else None}",
        fields, ok=bool(res_),
    )
    res.notes.append(
        "Retrospective by construction -- it can never tell you a decision is coming. "
        "Date-range syntax is search=field:[YYYYMMDD+TO+YYYYMMDD]; the '+' must reach the "
        "server unencoded or openFDA returns HTTP 500 with a parse_exception."
    )
    return res


# ==========================================================================
# Benchmarks: risk-free rate
# ==========================================================================

@check("FRED fredgraph.csv -- keyless daily Treasury series (T-bill benchmark)")
def probe_fred() -> Result:
    with _client(GENERIC_UA) as c:
        r = c.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": "DGS3MO"})
    fields = "CSV: observation_date, DGS3MO (percent per annum, blank on holidays)"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    lines = r.text.strip().splitlines()
    ok = len(lines) > 100 and lines[0].lower().startswith("observation_date")
    return _http_result(
        r, f"{len(lines)} rows; header={lines[0] if lines else ''}; "
           f"first={lines[1] if len(lines) > 1 else ''}; last={lines[-1] if lines else ''}",
        fields, ok=ok,
    )


@check("Treasury FiscalData -- average interest rates by security type")
def probe_treasury() -> Result:
    with _client(GENERIC_UA) as c:
        r = c.get(
            "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/"
            "accounting/od/avg_interest_rates",
            params={"page[size]": 2, "sort": "-record_date"},
        )
    fields = "data[].{record_date, security_type_desc, security_desc, avg_interest_rate_amt}"
    if r.status_code != 200:
        return _http_result(r, "non-200", fields, ok=False)
    d = r.json().get("data") or []
    return _http_result(
        r, f"{len(d)} row(s); newest record_date={d[0].get('record_date') if d else None} "
           f"{d[0].get('security_desc') if d else ''}={d[0].get('avg_interest_rate_amt') if d else ''}",
        fields, ok=bool(d),
    )


# ==========================================================================
# Sources checked and NOT adopted -- kept so the failure stays visible
# ==========================================================================

@check("Stooq CSV (candidate free daily history) -- EXPECTED FAIL, kept as a regression check")
def probe_stooq() -> Result:
    with _client(GENERIC_UA) as c:
        r = c.get("https://stooq.com/q/d/l/", params={"s": "spy.us", "i": "d"})
    fields = "CSV: Date,Open,High,Low,Close,Volume -- if it ever starts working again"
    text = r.text
    is_csv = text[:4].lower() == "date"
    res = _http_result(
        r, ("CSV returned -- Stooq is usable again, update DATA-SOURCES.md"
            if is_csv else
            f"HTTP {r.status_code} but body is an anti-bot / error page, not CSV: {_preview(text, 160)}"),
        fields, ok=is_csv,
        detail="" if is_csv else f"raw body: {_preview(text, 300)}",
    )
    res.notes.append(
        "Verified 2026-08-10: stooq.com and stooq.pl serve a JavaScript browser "
        "challenge to server-side clients (curl gets a 200 with a JS noscript page; "
        "httpx gets a 404 HTML page). Not usable unattended. This probe is expected "
        "to FAIL -- it exists so we notice if that ever changes."
    )
    return res


PROBES = [
    probe_alpaca_daily,
    probe_alpaca_minute,
    probe_alpaca_assets,
    probe_alpaca_news,
    probe_alpaca_corpactions,
    probe_sec_tickers,
    probe_sec_submissions,
    probe_sec_companyconcept,
    probe_sec_companyfacts,
    probe_sec_frames,
    probe_sec_daily_index,
    probe_sec_insider,
    probe_sec_finstmt,
    probe_sec_ftd,
    probe_sec_efts,
    probe_finra_regsho,
    probe_finra_shortinterest,
    probe_nasdaq_halts,
    probe_ctgov_current,
    probe_ctgov_history,
    probe_federal_register,
    probe_openfda,
    probe_fred,
    probe_treasury,
    probe_stooq,
]

EXPECTED_FAIL = {"Stooq CSV (candidate free daily history) -- EXPECTED FAIL, kept as a regression check"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="", help="substring filter on the source name")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON as well")
    args = ap.parse_args()

    print("=" * 100)
    print(f"catalyst data source verification -- {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    print(f"contact UA: {CONTACT}   (set CATALYST_CONTACT_EMAIL to change)")
    print("=" * 100)

    results: list[Result] = []
    for probe in PROBES:
        name = probe._probe_name  # type: ignore[attr-defined]
        if args.only and args.only.lower() not in name.lower():
            continue
        r = probe()
        results.append(r)

        mark = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP"}[r.verdict]
        if r.verdict == FAIL and r.name in EXPECTED_FAIL:
            mark = "FAIL (expected)"
        print(f"\n[{mark}] {r.name}")
        if r.url:
            print(f"    url    : {r.url}")
        if r.status:
            print(f"    status : HTTP {r.status}")
        if r.shape:
            print(f"    shape  : {r.shape}")
        if r.fields:
            print(f"    fields : {r.fields}")
        for n in r.notes:
            print(f"    note   : {n}")
        if r.detail:
            for line in r.detail.splitlines():
                print(f"    detail : {line}")
        time.sleep(PAUSE)

    n_pass = sum(1 for r in results if r.verdict == PASS)
    n_skip = sum(1 for r in results if r.verdict == SKIP)
    real_fail = [r for r in results if r.verdict == FAIL and r.name not in EXPECTED_FAIL]
    exp_fail = [r for r in results if r.verdict == FAIL and r.name in EXPECTED_FAIL]

    print("\n" + "=" * 100)
    print(f"{n_pass} PASS   {len(real_fail)} FAIL   {len(exp_fail)} expected-FAIL   {n_skip} SKIP   "
          f"({len(results)} checked)")
    for r in real_fail:
        print(f"  FAIL: {r.name} -> HTTP {r.status or '-'} :: {_preview(r.detail or r.shape, 200)}")
    for r in results:
        if r.verdict == SKIP:
            print(f"  SKIP: {r.name}")
    print("=" * 100)

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2, default=str))

    return 1 if real_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
