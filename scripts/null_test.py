#!/usr/bin/env python3
"""The null test: prove the harness does not manufacture edge.

Runs a SEEDED RANDOM strategy — random entries from the cached universe,
random 5-15 trading-day holds, no information content whatsoever —
through the full replay harness against real cached prices, and asserts
the excess return over SPY is materially NEGATIVE after costs.

Why this proves anything: a random long-only strategy on a $1,000 cash
account holds a random ~fraction of the index's exposure, pays spread/
slippage on every round trip and the API bill on top, and knows nothing.
It MUST lag buy-and-hold SPY. If the harness shows a random strategy
beating the index, the harness is lying (look-ahead, fill optimism, or
accounting error) — that is the point of the test.

Zero-cost and offline: reads only the local cache. Exit code 0 iff the
null strategy shows no edge.

Usage: python3 scripts/null_test.py [--cache data/bars] [--seed 20260810]
                                    [--candidates 400]
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from catalyst.backtest.data import BarCache  # noqa: E402
from catalyst.backtest.harness import ReplayConfig, replay_detailed  # noqa: E402
from catalyst.discovery import Candidate  # noqa: E402
from catalyst.research.schema import ResearchView  # noqa: E402

# Excess must be at least this far below zero to count as "materially
# negative". Chosen well inside what cash drag alone guarantees: a
# random strategy averaging under ~2 of 5 slots invested through a
# +350% SPY decade must lag by a triple-digit percentage, not 5%.
MATERIALITY_FLOOR = Decimal("-0.05")


def build_random_candidates(cache: BarCache, rng: random.Random, n: int,
                            benchmark: str) -> list[Candidate]:
    symbols = [s for s in cache.symbols() if s != benchmark]
    calendar = [b.day for b in cache.load_bars(benchmark)]
    # Leave room at the end for entry + a full hold.
    eligible_days = calendar[: max(1, len(calendar) - 25)]
    out = []
    for i in range(n):
        out.append(Candidate(
            id=f"null-{i:04d}",
            ticker=rng.choice(symbols),
            catalyst_type="null_random",
            catalyst_date=rng.choice(eligible_days),
            catalyst_date_confidence="confirmed",
            source_event_ids=("null",),
            discovered_at=datetime(2016, 1, 1),
            sector="none",
            correlation_tags=(),
        ))
    return out


def make_null_signal_fn(seed: int):
    def signal_fn(candidate: Candidate, view) -> ResearchView:
        # Per-candidate RNG: deterministic, independent of call order.
        rng = random.Random(f"{seed}:{candidate.id}")
        return ResearchView(
            candidate_id=candidate.id,
            direction="long",
            conviction=1.0,
            thesis="null strategy: random entry, no information content",
            invalidation="n/a — this trade encodes zero belief",
            expected_holding_days=rng.randint(5, 15),
            priced_in=False,
            priced_in_reasoning="n/a",
        )
    return signal_fn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", default=str(REPO_ROOT / "data" / "bars"))
    ap.add_argument("--seed", type=int, default=20260810)
    ap.add_argument("--candidates", type=int, default=400)
    args = ap.parse_args()

    cache = BarCache(args.cache)
    meta = cache.read_meta()
    if meta is None or not cache.has("SPY"):
        print("ERROR: no cache/SPY at", args.cache,
              "- run scripts/fetch_history.py first", file=sys.stderr)
        return 2
    print(f"cache: {cache.root}  fetched_at={meta.get('fetched_at')} "
          f"feed={meta.get('feed')} adjustment={meta.get('adjustment')} "
          f"symbols={meta.get('symbols_fetched')}")

    rng = random.Random(args.seed)
    universe = build_random_candidates(cache, rng, args.candidates, "SPY")
    spy = cache.load_bars("SPY")
    date_range = (spy[0].day, spy[-1].day)

    detail = replay_detailed(
        make_null_signal_fn(args.seed), universe, date_range,
        cache=cache, config=ReplayConfig(),
        strategy_name=f"null-random-seed{args.seed}",
    )
    r = detail.result
    b = r.benchmark

    def pct(x: Decimal) -> str:
        return f"{x * 100:+.2f}%"

    print(f"\nNULL TEST — {r.strategy_name}")
    print(f"period: {b.period_start} .. {b.period_end}  "
          f"({len(detail.equity_curve)} sessions)")
    print(f"candidates: {len(universe)}  trades closed: {len(detail.trades)}  "
          f"skips: {len(detail.skips)}")
    skip_reasons: dict[str, int] = {}
    for s in detail.skips:
        skip_reasons[s.reason] = skip_reasons.get(s.reason, 0) + 1
    print(f"skip reasons: {skip_reasons}")
    print(f"costs applied: per_side={r.costs_applied['per_side_cost_pct']}, "
          f"api=${r.costs_applied['api_cost_total_usd']} total "
          f"(${r.costs_applied['api_cost_monthly_usd']}/mo)")

    for kind, s in (("in-sample ", r.in_sample), ("out-sample", r.out_of_sample)):
        print(f"{kind}: n={s.sample_size}  hit={s.hit_rate:.3f}  "
              f"mean={pct(s.mean_return)}  median={pct(s.median_return)}  "
              f"worst={pct(s.worst_single_outcome)}  maxDD={pct(s.max_drawdown)}  "
              f"break-even/trade={pct(s.return_per_trade_needed_to_break_even)}")

    print(f"\nSPY total return (adjustment=all): {pct(b.spy_total_return)}")
    print(f"strategy net return:                {pct(b.strategy_total_return_net)}")
    print(f"EXCESS RETURN NET (headline):       {pct(b.excess_return_net)}")
    print(f"\n{r.market_regime_notes}\n")

    if b.excess_return_net < MATERIALITY_FLOOR:
        print(f"PASS: random strategy lags SPY by {pct(-b.excess_return_net)} "
              f"(materiality floor {pct(-MATERIALITY_FLOOR)}). "
              "The harness does not manufacture edge.")
        return 0
    print("FAIL: a random strategy shows edge (or lags immaterially). "
          "The harness is lying — find the leak before trusting any result.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
