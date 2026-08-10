#!/usr/bin/env python3
"""Download SEC Insider Transactions quarterly data sets (2016q1..latest)
and distill them into one open-market-purchase table.

Candidate C's data step. Run ONCE. Never run by tests.

Output: data/insider/purchases.csv with one row per (accession, owner):
    issuer_cik, symbol, owner_cik, filing_date, trans_date, value_usd,
    shares, shares_owned_after, aff10b5one

Filters applied HERE (mechanical, not strategy):
- DOCUMENT_TYPE = 4 (Form 4)
- NONDERIV_TRANS: TRANS_CODE = 'P' (open-market purchase),
  TRANS_ACQUIRED_DISP_CD = 'A'
- value = TRANS_SHARES * TRANS_PRICEPERSHARE (rows missing either are
  dropped and counted)

Point-in-time note: FILING_DATE is the tradable date, never TRANS_DATE
(Form 4 is due 2 business days after the trade; docs/DATA-SOURCES.md
§2.5). Dates in the files are DD-MON-YYYY.
"""
from __future__ import annotations

import csv
import io
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
ZIP_DIR = REPO_ROOT / "data" / "insider" / "zips"
OUT_CSV = REPO_ROOT / "data" / "insider" / "purchases.csv"
SEC_UA = "Catalyst Research (billysawyer0@gmail.com)"
BASE = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets"

QUARTERS = [f"{y}q{q}" for y in range(2016, 2027) for q in range(1, 5)
            if not (y == 2026 and q > 1)]


def parse_date(s: str) -> str | None:
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d-%b-%Y").date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None


def read_tsv(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as f:
        text = io.TextIOWrapper(f, encoding="utf-8", errors="replace", newline="")
        reader = csv.DictReader(text, delimiter="\t")
        yield from reader


def main() -> int:
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    rows_out: list[list] = []
    dropped_no_price = 0
    with httpx.Client(headers={"User-Agent": SEC_UA}, timeout=120.0,
                      follow_redirects=True) as client:
        for q in QUARTERS:
            dest = ZIP_DIR / f"{q}_form345.zip"
            if not dest.exists():
                time.sleep(0.4)
                r = client.get(f"{BASE}/{q}_form345.zip")
                if r.status_code == 404:
                    print(f"  {q}: 404 (not yet published) — skipped")
                    continue
                r.raise_for_status()
                dest.write_bytes(r.content)
            with zipfile.ZipFile(dest) as zf:
                subs: dict[str, dict] = {}
                for row in read_tsv(zf, "SUBMISSION.tsv"):
                    if row.get("DOCUMENT_TYPE", "").strip() != "4":
                        continue
                    acc = row["ACCESSION_NUMBER"].strip()
                    subs[acc] = {
                        "issuer_cik": row.get("ISSUERCIK", "").strip(),
                        "symbol": (row.get("ISSUERTRADINGSYMBOL") or "").strip().upper(),
                        "filing_date": parse_date(row.get("FILING_DATE", "")),
                        "aff": (row.get("AFF10B5ONE") or "").strip().lower(),
                    }
                owners: dict[str, list[str]] = {}
                for row in read_tsv(zf, "REPORTINGOWNER.tsv"):
                    acc = row["ACCESSION_NUMBER"].strip()
                    if acc in subs:
                        owners.setdefault(acc, []).append(
                            row.get("RPTOWNERCIK", "").strip())
                n_q = 0
                for row in read_tsv(zf, "NONDERIV_TRANS.tsv"):
                    acc = row["ACCESSION_NUMBER"].strip()
                    sub = subs.get(acc)
                    if sub is None or sub["filing_date"] is None:
                        continue
                    if (row.get("TRANS_CODE", "").strip() != "P"
                            or row.get("TRANS_ACQUIRED_DISP_CD", "").strip() != "A"):
                        continue
                    try:
                        shares = float(row.get("TRANS_SHARES") or "")
                        price = float(row.get("TRANS_PRICEPERSHARE") or "")
                    except ValueError:
                        dropped_no_price += 1
                        continue
                    if shares <= 0 or price <= 0:
                        dropped_no_price += 1
                        continue
                    own_after = row.get("SHRS_OWND_FOLWNG_TRANS", "").strip()
                    tdate = parse_date(row.get("TRANS_DATE", ""))
                    for owner_cik in owners.get(acc, ["?"]):
                        rows_out.append([
                            sub["issuer_cik"], sub["symbol"], owner_cik,
                            sub["filing_date"], tdate or "",
                            f"{shares * price:.2f}", f"{shares:.4f}",
                            own_after, sub["aff"],
                        ])
                        n_q += 1
            print(f"  {q}: cumulative purchase rows={len(rows_out)}")
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["issuer_cik", "symbol", "owner_cik", "filing_date",
                    "trans_date", "value_usd", "shares", "shares_owned_after",
                    "aff10b5one"])
        w.writerows(rows_out)
    print(f"wrote {len(rows_out)} purchase rows -> {OUT_CSV} "
          f"(dropped {dropped_no_price} rows missing shares/price — raw fields "
          f"were empty or non-numeric in the source TSV)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
