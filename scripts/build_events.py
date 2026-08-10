#!/usr/bin/env python3
"""Derive the strategy event tables from already-fetched raw data.

Fully OFFLINE — reads data/xbrl/facts/*.json.gz and
data/insider/purchases.csv, writes:
    data/xbrl/earnings_events.csv     (Candidate A)
    data/insider/cluster_events.csv   (Candidate C)

Safe to re-run any time; deterministic given the inputs.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from fetch_history import DEFAULT_UNIVERSE  # noqa: E402

from catalyst.strategies import earnings_drift, insider_cluster  # noqa: E402


def main() -> int:
    facts_dir = REPO_ROOT / "data" / "xbrl" / "facts"
    ev_a = earnings_drift.build_events(facts_dir, DEFAULT_UNIVERSE)
    out_a = REPO_ROOT / "data" / "xbrl" / "earnings_events.csv"
    earnings_drift.write_events_csv(ev_a, out_a)
    print(f"A: {len(ev_a)} earnings events -> {out_a}")

    purchases = REPO_ROOT / "data" / "insider" / "purchases.csv"
    ev_c = insider_cluster.build_cluster_events(purchases)
    out_c = REPO_ROOT / "data" / "insider" / "cluster_events.csv"
    insider_cluster.write_events_csv(ev_c, out_c)
    print(f"C: {len(ev_c)} cluster events -> {out_c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
