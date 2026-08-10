#!/usr/bin/env python3
"""Stage 4 bake-off: grade every implemented strategy on the SAME
harness, date range, costs, and account rules. Fully offline — reads
only local caches and derived event tables.

Runs, per strategy:
  full   2016-01-04..2026-08-07  (split 2023-12-31 for per-trade IS/OOS stats)
  oos    2024-01-02..2026-08-07  (OOS-only run: OOS excess-vs-SPY, fresh $1k)
Primary config: harness defaults ($8/mo API, 15bps/side).
Sensitivity:    --api0 adds a $0/mo API run (isolates market edge from
                the fixed-cost drag; labeled, never the headline).

Every result is persisted via scoring.persist_result into data/bakeoff.db.

Usage: python3 scripts/run_bakeoff.py [--only E,A,C] [--variant pre|tuned]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from catalyst.backtest.data import BarCache  # noqa: E402
from catalyst.backtest.harness import ReplayConfig, replay_detailed  # noqa: E402
from catalyst.backtest.scoring import persist_result  # noqa: E402
from catalyst.storage import init_db  # noqa: E402
from catalyst.strategies import earnings_drift, etf_rotation, insider_cluster  # noqa: E402

BARS = REPO_ROOT / "data" / "bars"
BARS_INSIDER = REPO_ROOT / "data" / "bars_insider"
DB = REPO_ROOT / "data" / "bakeoff.db"

FULL = (date(2016, 1, 4), date(2026, 8, 7))
OOS = (date(2024, 1, 2), date(2026, 8, 7))
IS = (date(2016, 1, 4), date(2023, 12, 29))


def pct(x) -> str:
    return f"{Decimal(x) * 100:+.2f}%"


def show(tag: str, detail) -> None:
    r = detail.result
    b = r.benchmark
    print(f"\n=== {r.strategy_name} [{tag}] "
          f"{b.period_start}..{b.period_end} ===")
    skip_reasons: dict[str, int] = {}
    for s in detail.skips:
        skip_reasons[s.reason] = skip_reasons.get(s.reason, 0) + 1
    print(f"trades={len(detail.trades)} skips={skip_reasons}")
    for kind, s in (("in ", r.in_sample), ("out", r.out_of_sample)):
        print(f"  {kind}: n={s.sample_size:4d} hit={s.hit_rate:.3f} "
              f"mean={pct(s.mean_return)} med={pct(s.median_return)} "
              f"worst={pct(s.worst_single_outcome)} maxDD={pct(s.max_drawdown)} "
              f"BE/trade={pct(s.return_per_trade_needed_to_break_even)}")
    print(f"  SPY={pct(b.spy_total_return)} net={pct(b.strategy_total_return_net)} "
          f"EXCESS={pct(b.excess_return_net)}")


def grade(name, signal_fn, universe, cache, conn, *, api_monthly="8",
          ranges=(("full", FULL), ("oos", OOS)), cost_bps=15):
    cfg_kwargs = {"api_cost_monthly_usd": Decimal(api_monthly),
                  "per_side_cost_pct": Decimal(cost_bps) / Decimal(10000)}
    for tag, rng in ranges:
        cfg = ReplayConfig(**cfg_kwargs)
        # (A workaround that pre-filtered candidates near the range end
        # used to live here; the harness now skips final-session entries
        # itself with reason "range_end_no_entry" — see
        # tests/test_backtest.py::test_entry_queued_for_final_session_is_skipped_not_opened.)
        detail = replay_detailed(
            signal_fn, universe, rng, cache=cache, config=cfg,
            strategy_name=f"{name}|{tag}|api{api_monthly}|c{cost_bps}bp")
        show(f"{tag} api=${api_monthly}/mo cost={cost_bps}bp/side", detail)
        persist_result(conn, detail.result, mode="structural")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="E,A,C")
    ap.add_argument("--variant", default="pre", choices=["pre", "tuned"])
    ap.add_argument("--api0", action="store_true",
                    help="add a $0/mo API sensitivity run")
    ap.add_argument("--cost-bps", type=int, default=15,
                    help="per-side spread/slippage haircut in bps (default 15)")
    ap.add_argument("--is-only", action="store_true",
                    help="grade the 2016..2023 in-sample window only "
                         "(variant tuning must never see the OOS window)")
    args = ap.parse_args()
    which = {w.strip().upper() for w in args.only.split(",")}

    conn = init_db(str(DB))
    cache = BarCache(BARS)
    apis = ["8"] + (["0"] if args.api0 else [])
    ranges = (("is", IS),) if args.is_only else (("full", FULL), ("oos", OOS))

    if "E" in which:
        if args.variant == "pre":
            cands = etf_rotation.build_candidates(cache, step=5)
            fn = etf_rotation.make_signal_fn(hold_days=4)
            name = "E-etf-rotation-pre(step5,hold4,top4,lb60)"
        else:
            cands = etf_rotation.build_candidates(cache, step=10)
            fn = etf_rotation.make_signal_fn(hold_days=9)
            name = "E-etf-rotation-tuned(step10,hold9,top4,lb60)"
        for api in apis:
            grade(name, fn, cands, cache, conn, api_monthly=api, ranges=ranges, cost_bps=args.cost_bps)

    if "A" in which:
        events = earnings_drift.read_events_csv(
            REPO_ROOT / "data" / "xbrl" / "earnings_events.csv")
        if args.variant == "pre":
            cands, table = earnings_drift.build_candidates(events, sue_min=1.0)
            fn = earnings_drift.make_signal_fn(table)
            name = "A-earnings-drift-pre(sue1.0,react3,hold12)"
        else:
            cands, table = earnings_drift.build_candidates(events, sue_min=2.0)
            fn = earnings_drift.make_signal_fn(table)
            name = "A-earnings-drift-tuned(sue2.0,react3,hold12)"
        for api in apis:
            grade(name, fn, cands, cache, conn, api_monthly=api, ranges=ranges, cost_bps=args.cost_bps)

    if "C" in which:
        icache = BarCache(BARS_INSIDER)
        events = insider_cluster.read_events_csv(
            REPO_ROOT / "data" / "insider" / "cluster_events.csv")
        cands, table = insider_cluster.build_candidates(events)
        fn = insider_cluster.make_signal_fn(table)
        name = "C-insider-cluster-pre(2x10d,50k,hold12,liq5/1M)"
        for api in apis:
            grade(name, fn, cands, icache, conn, api_monthly=api, ranges=ranges, cost_bps=args.cost_bps)

    conn.close()
    print(f"\nresults persisted -> {DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
