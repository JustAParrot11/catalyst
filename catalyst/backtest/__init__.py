"""Backtest harness. Owner: backtest-engineer.

The scoreboard every strategy decision rests on. Two modes: structural
(free, no model calls, run hundreds of times) and judgement (costed,
reuses research.investigate, tracked as manual spend). Every result
reports the strategy's return ALONGSIDE the S&P 500's over the identical
period, net of costs - a strategy that makes money but loses to SPY is a
failure and must be visible as one.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class BacktestSampleStats:
    sample_size: int
    hit_rate: Decimal
    mean_return: Decimal
    median_return: Decimal
    worst_single_outcome: Decimal
    max_drawdown: Decimal
    return_per_trade_needed_to_break_even: Decimal


@dataclass(frozen=True)
class BenchmarkComparison:
    """The measure of success: excess return over SPY, net of all costs."""

    spy_total_return: Decimal            # over the identical period
    strategy_total_return_net: Decimal   # net of spread, slippage, API costs
    excess_return_net: Decimal           # strategy_net - spy
    period_start: date
    period_end: date


@dataclass(frozen=True)
class BacktestResult:
    strategy_name: str
    date_range: tuple[date, date]
    in_sample: BacktestSampleStats
    out_of_sample: BacktestSampleStats
    benchmark: BenchmarkComparison
    costs_applied: dict = field(hash=False)
    market_regime_notes: str = ""
