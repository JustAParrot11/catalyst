#!/usr/bin/env python3
"""Fetch daily bar history from Alpaca into the local backtest cache.

Run ONCE (or occasionally to extend the window); the backtest then
replays from the cache at zero marginal cost, forever. Never run by
tests, never run mid-replay.

Usage:
    python3 scripts/fetch_history.py                     # default universe + SPY
    python3 scripts/fetch_history.py --symbols AAPL,MSFT
    python3 scripts/fetch_history.py --start 2016-01-04 --end 2026-08-07

Credentials: ALPACA_KEY / ALPACA_SECRET_KEY read from the environment by
name. Values are never printed, logged, or written anywhere.

Facts baked in (docs/DATA-SOURCES.md, verified live 2026-08-10):
- feed=sip, adjustment=all, explicitly — defaults would silently give a
  fragmentary IEX tape and a price-only (not total-return) series.
- SIP history floor is 2016-01-04; earlier starts return HTTP 200 with
  bars:null, which is recorded in cache metadata, not treated as error.
- Rate limit 200 req/min; this script paces well under it.

SURVIVORSHIP WARNING: the default universe below is 100 currently-listed
liquid names plus SPY. Alpaca cannot enumerate delisted tickers, so this
universe omits everything that died 2016-2026. Every backtest result
carries that statement; it originates here.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from catalyst.backtest.data import (  # noqa: E402
    ADJUSTMENT, FEED, SIP_START, BarCache, alpaca_auth_headers, fetch_daily_bars,
)

DEFAULT_CACHE = REPO_ROOT / "data" / "bars"

# 100 liquid, currently-listed US names across sectors, plus SPY (the
# benchmark, always fetched). Chosen for liquidity, not by any signal.
# CURRENTLY-LISTED = SURVIVORSHIP-BIASED, by construction (see module doc).
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NVDA", "TSLA", "AVGO", "AMD", "INTC",
    "CRM", "ORCL", "ADBE", "CSCO", "QCOM", "TXN", "MU", "AMAT", "LRCX", "ADI",
    "NOW", "PANW", "INTU", "IBM", "NFLX", "PYPL", "SHOP", "UBER", "ABNB", "PLTR",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "AXP", "V",
    "MA", "COF", "USB", "PNC", "KO", "PEP", "PG", "COST", "WMT", "TGT",
    "MCD", "SBUX", "NKE", "MDLZ", "PM", "MO", "CL", "KMB", "GIS", "HSY",
    "JNJ", "PFE", "MRK", "LLY", "ABBV", "BMY", "AMGN", "GILD", "UNH", "CVS",
    "TMO", "DHR", "ABT", "ISRG", "VRTX", "REGN", "MRNA", "BIIB", "XOM", "CVX",
    "COP", "SLB", "EOG", "OXY", "GE", "CAT", "DE", "BA", "HON", "MMM",
    "UNP", "UPS", "FDX", "LMT", "RTX", "NEE", "DUK", "SO", "AMT", "DIS",
]
BENCHMARK = "SPY"

BATCH_SIZE = 50            # symbols per request URL
PACE_SECONDS = 0.35        # ~170 req/min worst case, under the 200/min limit

SURVIVORSHIP_NOTE = ("universe is currently-listed names only; delisted "
                     "tickers cannot be enumerated from Alpaca "
                     "(DATA-SOURCES.md §1.4) — results are flattered for "
                     "long strategies")


def merge_meta(prior: dict | None, fetch_record: dict) -> dict:
    """Merge one fetch's provenance record into existing cache metadata.

    The cache accumulates across runs (e.g. the default universe first,
    then --symbols batches for other strategies), so the metadata must
    too: overwriting it left cache_meta.json describing only the LAST
    fetch, silently orphaning the provenance of every symbol written by
    earlier runs.

    Layout: top-level keys still describe the most recent fetch (existing
    consumers — null_test.py, tests — read meta["feed"] etc. directly),
    and meta["fetches"] appends every fetch's record, oldest first. A
    legacy single-fetch meta (no "fetches" key) is preserved as the first
    record rather than dropped.
    """
    fetches: list[dict] = []
    if prior:
        fetches = list(prior.get("fetches", []))
        if not fetches:
            legacy = {k: v for k, v in prior.items()
                      if k not in ("survivorship", "fetches")}
            if legacy:
                fetches.append(legacy)
    fetches.append(fetch_record)
    return {**fetch_record, "fetches": fetches, "survivorship": SURVIVORSHIP_NOTE}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbols", help="comma-separated tickers (default: built-in 100 + SPY)")
    ap.add_argument("--start", default=SIP_START.isoformat())
    ap.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else list(DEFAULT_UNIVERSE))
    if BENCHMARK not in symbols:
        symbols.append(BENCHMARK)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if start < SIP_START:
        print(f"note: start {start} predates SIP floor {SIP_START}; "
              "earlier dates return bars:null (recorded, not an error)")

    cache = BarCache(args.cache)
    headers = alpaca_auth_headers()   # values go into headers only, never printed
    all_notes: list[dict] = []
    total_bars = 0
    fetched_symbols = 0

    with httpx.Client(headers=headers, timeout=30.0) as client:
        for chunk_start in range(0, len(symbols), BATCH_SIZE):
            chunk = symbols[chunk_start:chunk_start + BATCH_SIZE]
            t0 = time.time()
            bars_by_symbol, notes = fetch_daily_bars(client, chunk, start, end)
            all_notes.extend(notes)
            for sym, bars in bars_by_symbol.items():
                cache.write_bars(sym, bars)
                total_bars += len(bars)
                fetched_symbols += 1
            elapsed = time.time() - t0
            print(f"  chunk {chunk[0]}..{chunk[-1]}: {len(bars_by_symbol)} symbols, "
                  f"{sum(len(b) for b in bars_by_symbol.values())} bars, {elapsed:.1f}s")
            time.sleep(PACE_SECONDS)

    fetch_record = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "alpaca",
        "feed": FEED,
        "adjustment": ADJUSTMENT,
        "start_requested": start.isoformat(),
        "end_requested": end.isoformat(),
        "symbols_requested": len(symbols),
        "symbols_requested_list": symbols,     # per-batch provenance
        "symbols_fetched": fetched_symbols,
        "total_bars": total_bars,
        "empty_or_odd_responses": all_notes,   # raw bodies beside every zero
    }
    cache.write_meta(merge_meta(cache.read_meta(), fetch_record))

    print(f"\ncached {fetched_symbols}/{len(symbols)} symbols, {total_bars} bars "
          f"-> {cache.root}")
    if fetched_symbols < len(symbols):
        missing = sorted(set(symbols) - set(cache.symbols()))
        print(f"symbols with NO bars written ({len(missing)}): {missing}")
        print("raw upstream context is in cache_meta.json['empty_or_odd_responses']")
    spy = cache.load_bars(BENCHMARK)
    print(f"{BENCHMARK}: {len(spy)} bars, {spy[0].day} .. {spy[-1].day}, "
          f"first close {spy[0].close}, last close {spy[-1].close} (adjustment=all)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
