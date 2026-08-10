"""Replay engine with point-in-time data access.

At every simulated moment the strategy may only see what was genuinely
knowable then (backtest-engineer's brief: look-ahead is the single most
common way a backtest lies). Pessimistic in every assumption: ambiguous
fills take the worse price.
"""

from datetime import date
from typing import Callable

from catalyst.backtest import BacktestResult


def replay(signal_fn: Callable, universe: list, date_range: tuple[date, date]) -> BacktestResult:
    raise NotImplementedError("stage 2")
