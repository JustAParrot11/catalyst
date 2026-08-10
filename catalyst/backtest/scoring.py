"""Scoring: sample stats, the SPY benchmark comparison, persistence.

Owner: backtest-engineer. A number without a sample size is not a result.

Documented assumptions
----------------------
- **Break-even math** is computed against a $1,000 account paying
  $8/month in runtime API costs (BUILD-BRIEF.md's "workable" tier — a
  10% annual hurdle). `return_per_trade_needed_to_break_even` is the
  per-trade net return that would exactly cover the period's API bill:
  (monthly_cost * months_in_period / n_trades) / average_trade_notional.
  If the strategy's mean net return per trade is below this figure, it
  loses money after costs even when every paper trade "works".
- **In/out-of-sample split is CHRONOLOGICAL, never random.** Trades are
  time-ordered and autocorrelated (regimes, volatility clustering,
  overlapping holds); a random split leaks the test period's regime into
  the training period and lets a strategy tuned on 2024 volatility be
  "validated" on interleaved 2024 days. Chronological holdout is the
  only split that answers the question that matters: does what was
  tuned on the past work on the genuinely unseen future?
- **The SPY benchmark uses the adjustment=all (total-return) series**,
  first-session open to last-session close over the identical period.
  DATA-SOURCES.md §1.2: the default `raw` series understates SPY by
  ~67pp over the full window — a comparison against it would flatter
  every strategy.
"""

from __future__ import annotations

import json
import statistics
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from catalyst.backtest import BacktestResult, BacktestSampleStats, BenchmarkComparison
from catalyst.backtest.data import Bar

#: Days per month used to pro-rate the monthly API cost over a period.
DAYS_PER_MONTH = Decimal("30.4375")

ZERO = Decimal("0")


def max_drawdown(equity: list[Decimal]) -> Decimal:
    """Largest peak-to-trough decline, as a positive fraction of the peak."""
    worst = ZERO
    peak: Decimal | None = None
    for value in equity:
        if peak is None or value > peak:
            peak = value
        elif peak > 0:
            dd = (peak - value) / peak
            if dd > worst:
                worst = dd
    return worst


def period_months(start: date, end: date) -> Decimal:
    days = (end - start).days
    return (Decimal(days) / DAYS_PER_MONTH) if days > 0 else ZERO


def compute_sample_stats(
    trade_returns: list[Decimal],
    trade_notionals: list[Decimal],
    equity_segment: list[Decimal],
    months: Decimal,
    api_cost_monthly_usd: Decimal,
) -> BacktestSampleStats:
    """Stats for one sample (in- or out-of-). sample_size=0 means every
    other field in the row is meaningless and must be read as such."""
    n = len(trade_returns)
    if n == 0:
        return BacktestSampleStats(
            sample_size=0, hit_rate=ZERO, mean_return=ZERO, median_return=ZERO,
            worst_single_outcome=ZERO,
            max_drawdown=max_drawdown(equity_segment),
            return_per_trade_needed_to_break_even=ZERO,
        )
    wins = sum(1 for r in trade_returns if r > 0)
    total_api_cost = api_cost_monthly_usd * months
    cost_per_trade = total_api_cost / n
    avg_notional = sum(trade_notionals, ZERO) / n
    break_even = (cost_per_trade / avg_notional) if avg_notional > 0 else ZERO
    return BacktestSampleStats(
        sample_size=n,
        hit_rate=Decimal(wins) / n,
        mean_return=sum(trade_returns, ZERO) / n,
        median_return=statistics.median(trade_returns),
        worst_single_outcome=min(trade_returns),
        max_drawdown=max_drawdown(equity_segment),
        return_per_trade_needed_to_break_even=break_even,
    )


def benchmark_total_return(bars: list[Bar]) -> Decimal:
    """Total return of the benchmark over the bars given: first-session
    OPEN to last-session close. Buying the index at the first open is
    the alternative use of the same capital from the same moment —
    using the (usually higher) first close instead would shrink the
    benchmark and flatter the strategy."""
    if len(bars) < 2:
        raise ValueError("benchmark comparison needs at least two sessions of bars")
    first, last = bars[0], bars[-1]
    if first.open <= 0:
        raise ValueError(f"benchmark first open is non-positive: {first.open}")
    return last.close / first.open - 1


def benchmark_comparison(
    benchmark_bars: list[Bar],
    strategy_total_return_net: Decimal,
) -> BenchmarkComparison:
    spy = benchmark_total_return(benchmark_bars)
    return BenchmarkComparison(
        spy_total_return=spy,
        strategy_total_return_net=strategy_total_return_net,
        excess_return_net=strategy_total_return_net - spy,
        period_start=benchmark_bars[0].day,
        period_end=benchmark_bars[-1].day,
    )


def persist_result(conn, result: BacktestResult, mode: str = "structural") -> str:
    """Write a BacktestResult into backtest_results + backtest_sample_stats.

    Returns the generated result id. Decimals are stored as strings per
    the schema's cents/decimal-string convention.
    """
    if mode not in ("structural", "judgement"):
        raise ValueError(f"mode must be structural|judgement, got {mode!r}")
    result_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO backtest_results (id, strategy_name, mode, date_range_start,"
        " date_range_end, spy_total_return, strategy_return_net, excess_return_net,"
        " costs_applied, market_regime_notes, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            result_id,
            result.strategy_name,
            mode,
            result.date_range[0].isoformat(),
            result.date_range[1].isoformat(),
            str(result.benchmark.spy_total_return),
            str(result.benchmark.strategy_total_return_net),
            str(result.benchmark.excess_return_net),
            json.dumps(result.costs_applied, default=str),
            result.market_regime_notes,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    for kind, stats in (("in_sample", result.in_sample),
                        ("out_of_sample", result.out_of_sample)):
        conn.execute(
            "INSERT INTO backtest_sample_stats (result_id, sample_kind, sample_size,"
            " hit_rate, mean_return, median_return, worst_single_outcome,"
            " max_drawdown, return_per_trade_needed_to_break_even)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                result_id, kind, stats.sample_size, str(stats.hit_rate),
                str(stats.mean_return), str(stats.median_return),
                str(stats.worst_single_outcome), str(stats.max_drawdown),
                str(stats.return_per_trade_needed_to_break_even),
            ),
        )
    conn.commit()
    return result_id
