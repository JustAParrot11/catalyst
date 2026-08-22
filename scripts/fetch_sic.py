#!/usr/bin/env python3
"""Fetch the SIC industry code for every issuer in the insider dataset.

One-off, resumable, and deliberately slower than the SEC's ceiling.
TRAPS.md: EDGAR is rate limited to 10 requests/second ACROSS ALL its
APIs and an overrun temporarily blocks this IP for every one of them.
This paces at 8/s, retries transient 5xx with backoff, and never retries
a 4xx.

Output: data/insider/issuer_sic.csv  (cik,sic) - "" sic means EDGAR has
none for that company, which is a real answer and is cached as one.
"""
import csv
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[0]
REPO = pathlib.Path("/home/user/catalyst")
SRC = REPO / "data" / "insider" / "purchases.csv"
OUT = REPO / "data" / "insider" / "issuer_sic.csv"
UA = "Catalyst Trading Bot (billysawyer0@gmail.com)"
RATE = 8.0
INTERVAL = 1.0 / RATE


def load_done():
    done = {}
    if OUT.exists():
        with OUT.open() as fh:
            for row in csv.reader(fh):
                if len(row) >= 2 and row[0] != "cik":
                    done[row[0]] = row[1]
    return done


def fetch(cik):
    url = f"https://data.sec.gov/submissions/CIK{cik:0>10}.json"
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return str(json.load(r).get("sic") or "")
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                return ""              # permanent answer about this CIK
            time.sleep(2 ** attempt)   # transient - back off and retry
        except Exception:
            time.sleep(2 ** attempt)
    return None                        # could not settle; do not cache


def main():
    ciks = []
    with SRC.open() as fh:
        for row in csv.DictReader(fh):
            c = (row.get("issuer_cik") or "").strip().lstrip("0")
            if c:
                ciks.append(c)
    ciks = sorted(set(ciks))
    done = load_done()
    todo = [c for c in ciks if c not in done]
    print(f"{len(ciks)} issuers, {len(done)} already cached, {len(todo)} to fetch",
          flush=True)

    new = OUT.exists()
    with OUT.open("a", newline="") as fh:
        w = csv.writer(fh)
        if not new:
            w.writerow(["cik", "sic"])
        nxt = time.monotonic()
        for i, cik in enumerate(todo, 1):
            wait = nxt - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            nxt = time.monotonic() + INTERVAL
            sic = fetch(cik)
            if sic is None:
                continue
            w.writerow([cik, sic])
            if i % 250 == 0:
                fh.flush()
                print(f"  {i}/{len(todo)}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    sys.exit(main())
