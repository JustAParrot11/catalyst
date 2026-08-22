#!/usr/bin/env python3
"""Join the issuer SIC cache onto ticker symbols for the backtest.

The insider dataset keys cluster events on SYMBOL, while EDGAR keys
companies on CIK. purchases.csv carries both, so this joins them:

    purchases.csv   symbol <-> issuer_cik
    issuer_sic.csv  issuer_cik -> sic
    symbol_sic.csv  symbol -> sic          (written here)

A symbol can belong to more than one CIK across a decade - tickers are
reused after a delisting. The CIK with the MOST purchase rows for that
symbol wins, which picks the company the dataset is actually about
rather than whichever happened to be read last.

Offline: reads only local CSVs. Run scripts/fetch_sic.py first.
"""
import collections
import csv
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PURCHASES = ROOT / "data" / "insider" / "purchases.csv"
ISSUER_SIC = ROOT / "data" / "insider" / "issuer_sic.csv"
OUT = ROOT / "data" / "insider" / "symbol_sic.csv"


def main() -> int:
    if not ISSUER_SIC.exists():
        print(f"{ISSUER_SIC} is missing - run the SIC fetch first", file=sys.stderr)
        return 1

    sic_by_cik = {}
    with ISSUER_SIC.open(newline="") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2 and row[0] and row[0] != "cik":
                sic_by_cik[row[0].strip().lstrip("0")] = row[1].strip()

    counts = collections.defaultdict(collections.Counter)
    with PURCHASES.open(newline="") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("symbol") or "").strip().upper()
            cik = (row.get("issuer_cik") or "").strip().lstrip("0")
            if sym and cik:
                counts[sym][cik] += 1

    written = blank = 0
    with OUT.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "sic"])
        for sym in sorted(counts):
            cik = counts[sym].most_common(1)[0][0]
            sic = sic_by_cik.get(cik, "")
            w.writerow([sym, sic])
            written += 1
            blank += 0 if sic else 1

    # BOTH NUMBERS. "0 blank" and "half blank" are very different states
    # and only one of them means the enrichment is working.
    print(f"{written} symbol(s) written to {OUT}")
    print(f"  with a SIC: {written - blank}")
    print(f"  without   : {blank}  (these still cluster as 'unknown')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
