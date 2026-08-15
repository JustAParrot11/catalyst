#!/usr/bin/env python3
"""Measure the adverse gap instead of assuming it.

WHY THIS EXISTS. `adverse_gap_assumption` is the denominator of the
sizing formula:

    notional = (equity x max_loss_per_position_pct) / max(gap, stop)

so it does not merely inform the position size, it IS the position size.
Seventeen of the eighteen catalyst types carry a guess. The worst is
0.60 for `fda_decision` and `clinical_readout`, and BUILD-BRIEF names
that number as invented:

    "a previous build shipped with an assumed 60% adverse gap and a 0.65
    conviction floor, both invented, neither validated. If those numbers
    are wrong the system refuses good trades forever and never signals
    that it is doing so."

At 0.60 a $2,000 account takes a $66.67 position in exactly the two
catalyst types where edge most plausibly lives. If the true figure is
half that, the position doubles - on evidence, with no hard bound moved.

WHAT THE PARAMETER ACTUALLY MEANS, which decides what to measure. A stop
does not protect against a gap: TRAPS.md records that stop orders do not
trigger outside regular hours, so overnight gap risk cannot be removed
with stock alone. The loss you actually suffer is therefore driven by
the OVERNIGHT GAP - open against the previous close - not by the
intraday path. So that is what this measures:

    gap = (open - previous_close) / previous_close

reported as the left tail, because sizing cares about the bad end.

WHAT THIS CANNOT DO, stated plainly. There are no dated FDA or clinical
event histories in this repository, so these figures are NOT
event-conditional and must not be labelled `GRADED`. What they measure
is the gap distribution of the traded universe, and of its most volatile
decile as the closest available proxy for a binary-event name. A
measured proxy is strictly better than an invented number, and it is
only worth having if it is labelled as what it is.

Offline. Reads data/bars_insider/*.csv and touches no network.
"""

import csv
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
BARS = ROOT / "data" / "bars_insider"

#: Below this the name is not realistically tradeable by this bot and its
#: gaps would poison the measurement with sub-penny noise.
MIN_PRICE = 1.0
#: A gap this large is almost always a split, reverse split or other
#: corporate action the bar file did not adjust - not a market move.
#: Left in and they dominate the tail, which is how a 60% assumption
#: gets manufactured in the first place.
MAX_PLAUSIBLE_GAP = 0.90


def gaps_for(path: Path) -> list[float]:
    """Every overnight gap in one ticker's history, as fractions."""
    out = []
    try:
        with path.open(newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error):
        return out
    prev_close = None
    for row in rows:
        try:
            open_, close = float(row["open"]), float(row["close"])
        except (KeyError, TypeError, ValueError):
            prev_close = None
            continue
        if prev_close is not None and prev_close >= MIN_PRICE and open_ > 0:
            gap = (open_ - prev_close) / prev_close
            if abs(gap) <= MAX_PLAUSIBLE_GAP:
                out.append(gap)
        prev_close = close if close > 0 else None
    return out


def percentile(sorted_values: list[float], q: float) -> float:
    """The q-th percentile of an already-sorted list, 0 <= q <= 1."""
    if not sorted_values:
        return float("nan")
    idx = int(q * (len(sorted_values) - 1))
    return sorted_values[idx]


def summarise(label: str, gaps: list[float]) -> dict:
    downs = sorted(g for g in gaps if g < 0)
    allg = sorted(gaps)
    return {
        "label": label,
        "n_days": len(gaps),
        "n_down": len(downs),
        "p50_down": abs(median(downs)) if downs else float("nan"),
        "p95": abs(percentile(allg, 0.05)),
        "p99": abs(percentile(allg, 0.01)),
        "p99_9": abs(percentile(allg, 0.001)),
        "worst": abs(allg[0]) if allg else float("nan"),
    }


def main(argv=None) -> int:
    if not BARS.is_dir():
        print(f"No bar cache at {BARS} - nothing to measure.", file=sys.stderr)
        return 1
    files = sorted(BARS.glob("*.csv"))
    if not files:
        print(f"{BARS} is empty - nothing to measure.", file=sys.stderr)
        return 1

    print(f"Reading {len(files)} tickers from {BARS} ...")
    all_gaps: list[float] = []
    per_ticker: list[tuple[float, str, list[float]]] = []
    for path in files:
        g = gaps_for(path)
        if len(g) < 250:               # under a year of history proves little
            continue
        all_gaps.extend(g)
        downs = sorted(x for x in g if x < 0)
        vol = abs(percentile(downs, 0.05)) if downs else 0.0
        per_ticker.append((vol, path.stem, g))

    if not all_gaps:
        print("No usable bars found.", file=sys.stderr)
        return 1

    per_ticker.sort(reverse=True)
    decile = max(1, len(per_ticker) // 10)
    volatile = [x for _, _, g in per_ticker[:decile] for x in g]

    rows = [summarise("whole universe", all_gaps),
            summarise("most volatile decile", volatile)]

    print(f"\n{len(per_ticker)} tickers with >=250 sessions, "
          f"{len(all_gaps):,} overnight gaps measured.\n")
    head = (f"{'sample':24s} {'gaps':>12s} {'median down':>12s} "
            f"{'p95':>8s} {'p99':>8s} {'p99.9':>8s} {'worst':>8s}")
    print(head)
    print("-" * len(head))
    for r in rows:
        print(f"{r['label']:24s} {r['n_days']:12,d} "
              f"{r['p50_down']:11.1%} {r['p95']:7.1%} {r['p99']:7.1%} "
              f"{r['p99_9']:7.1%} {r['worst']:7.1%}")

    # THE MEASUREMENT THAT ACTUALLY BEARS ON A BINARY EVENT.
    #
    # The distributions above are unconditional - every ordinary day
    # included - and an FDA decision day is by definition not an
    # ordinary day. Sizing a binary against an all-days percentile
    # would understate it badly.
    #
    # So: take each ticker's SINGLE WORST overnight gap in ten years.
    # For a small-cap biotech that day is overwhelmingly likely to be
    # exactly the event this parameter exists for - the readout that
    # missed, the CRL, the trial halt. It is the closest thing to an
    # event-conditional sample obtainable without dated event history,
    # and it is deliberately biased toward the bad end: every ticker
    # contributes its worst day and nothing else.
    worst_per_ticker = sorted(
        abs(min(g)) for _, _, g in per_ticker if min(g) < 0)
    vol_worst = sorted(
        abs(min(g)) for _, _, g in per_ticker[:decile] if min(g) < 0)

    print("\nEach ticker's SINGLE WORST overnight gap "
          "(the closest proxy to a binary event day):")
    head2 = (f"{'sample':24s} {'tickers':>12s} {'median':>12s} "
             f"{'p75':>8s} {'p90':>8s} {'p95':>8s} {'worst':>8s}")
    print(head2)
    print("-" * len(head2))
    for label, sample in (("all tickers", worst_per_ticker),
                          ("most volatile decile", vol_worst)):
        if not sample:
            continue
        print(f"{label:24s} {len(sample):12,d} "
              f"{median(sample):11.1%} {percentile(sample, 0.75):7.1%} "
              f"{percentile(sample, 0.90):7.1%} "
              f"{percentile(sample, 0.95):7.1%} {sample[-1]:7.1%}")

    vol_row = rows[1]
    print("\nWhat this says about the 0.60 assumption:")
    if vol_worst:
        share = sum(1 for x in vol_worst if x >= 0.60) / len(vol_worst)
        print(f"  Of the most volatile decile, {share:.1%} ever saw an "
              f"overnight gap as bad as 60% - in ten years, on their own "
              f"worst day.")
        print(f"  Their MEDIAN worst-ever day was {median(vol_worst):.1%}, "
              f"and {percentile(vol_worst, 0.90):.1%} at the 90th "
              f"percentile.")
    print(f"  A 60% overnight gap sits beyond the 99.9th percentile of even "
          f"the most volatile decile ({vol_row['p99_9']:.1%}).")
    print(f"  Sizing against it means every position in those catalyst "
          f"types is cut to roughly {vol_row['p99'] / 0.60:.0%} of what the "
          f"99th-percentile gap would justify.")
    print("\nNOT event-conditional. There is no dated FDA or clinical event "
          "history in this repo, so these are universe and volatile-decile "
          "figures, not graded per-catalyst measurements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
