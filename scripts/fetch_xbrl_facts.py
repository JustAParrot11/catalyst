#!/usr/bin/env python3
"""Fetch SEC XBRL companyfacts for the cached ~100-symbol universe.

Candidate A's data step. Run ONCE; the backtest then derives earnings
events offline from the saved JSON. Never run by tests.

Point-in-time notes (docs/DATA-SOURCES.md §2.3):
- companyfacts carries a `filed` date on every fact; the derivation step
  (catalyst/strategies/earnings_drift.py) uses only facts with
  filed <= as_of. The `frames` endpoint is NOT used anywhere: it has no
  filed date and silently includes restatements (look-ahead).
- SEC rate limit is 10 req/s across all SEC hosts; this script paces at
  ~3 req/s. User-Agent must be contactable.
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from fetch_history import DEFAULT_UNIVERSE  # noqa: E402

OUT_DIR = REPO_ROOT / "data" / "xbrl" / "facts"
SEC_UA = "Catalyst Research (billysawyer0@gmail.com)"
PACE = 0.35


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": SEC_UA, "Accept-Encoding": "gzip"}
    with httpx.Client(headers=headers, timeout=60.0, follow_redirects=True) as client:
        r = client.get("https://www.sec.gov/files/company_tickers.json")
        r.raise_for_status()
        tick2cik = {row["ticker"].upper(): int(row["cik_str"])
                    for row in r.json().values()}
        missing = [t for t in DEFAULT_UNIVERSE if t not in tick2cik]
        if missing:
            print(f"no CIK found for {missing} — these are skipped and the raw "
                  f"mapping file simply has no such ticker entries", file=sys.stderr)
        ok = failed = 0
        for i, ticker in enumerate(DEFAULT_UNIVERSE):
            if ticker not in tick2cik:
                continue
            dest = OUT_DIR / f"{ticker}.json.gz"
            if dest.exists():
                ok += 1
                continue
            cik = tick2cik[ticker]
            url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
            time.sleep(PACE)
            try:
                resp = client.get(url)
                resp.raise_for_status()
                dest.write_bytes(gzip.compress(resp.content))
                ok += 1
                if (i + 1) % 20 == 0:
                    print(f"  {i+1}/{len(DEFAULT_UNIVERSE)} fetched")
            except httpx.HTTPStatusError as e:
                failed += 1
                print(f"  {ticker} CIK{cik}: HTTP {e.response.status_code} "
                      f"raw body: {e.response.text[:200]!r}", file=sys.stderr)
    print(f"companyfacts: {ok} saved, {failed} failed -> {OUT_DIR}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
