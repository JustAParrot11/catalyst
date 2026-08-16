"""Replay engine with point-in-time data access.

Owner: backtest-engineer.

At every simulated moment the strategy may only see what was genuinely
knowable then — signal functions receive a PointInTimeView that
physically cannot return bars after its as_of date (see data.py).

Pessimistic in every assumption, deliberately:

- **Fills**: a signal formed from day D's data (i.e. at/after D's close)
  fills at day D+1's OPEN, never day D's close — you cannot trade a
  close you needed to observe to generate the signal. Entries pay
  open*(1+cost), exits receive open*(1-cost).
- **Costs**: per-side spread+slippage haircut, default 15 bps per side
  (30 bps round trip). Liquid large-cap spreads are 1-2 bps, so this is
  intentionally punitive; Alpaca paper fills pay no spread at all
  (TRAPS.md), so a modeled cost is the only honest cost. A strategy
  that only works below 15 bps has no margin for the real world.
- **Account**: $1,000 cash account. Long-only (shorting_enabled=false,
  verified), no leverage, T+1 settlement — sale proceeds are unusable
  until the NEXT session. Fractional shares allowed.
- **Portfolio**: max 5 position slots, equal-weight risk budget
  (equity / max_positions per position), and BOTH of the risk engine's
  portfolio hard bounds - 90% total exposure and 35% per correlated
  cluster. Every position carries a planned exit date at entry; holds
  are clamped to 15 trading days (~3 calendar weeks, the brief's hard
  ceiling).

  THE TWO BOUNDS WERE ADDED AFTER MEASURING THE TWO ENGINES AGAINST
  EACH OTHER, and they changed the answer. Production sizes by risk,
  notional = (equity x max_loss_per_position_pct) / max(gap, stop),
  which for insider_cluster lands at exactly $200 on a $1,000 account -
  identical to equity/max_positions here. The PER-POSITION size agreed,
  so nothing looked wrong. The PORTFOLIO did not:

    * five slots at $200 is 100% invested; the exposure bound is 90%
    * the Form 4 payload carries no sector field, so every insider
      candidate keys on "unknown|insider_cluster|<week>" and they all
      bind against each other. Five candidates completing in one week
      open 2 positions for $350, not 5 for $1,000.

  Effect on the graded strategy, out of sample 2024-01..2026-08
  (SPY +68.7%), no API cost: excess went +31.6% -> +10.4% with the
  exposure bound -> -20.1% with both. The signal is unchanged - 229
  trades either way, same 52.8% hit rate, same +1.75% mean per trade.
  What changed is that the cluster bound bites hardest in the weeks
  when several clusters complete at once, which is when the signal is
  strongest, so it is a selection effect rather than a scale one:
  average capital deployed barely moved (66.7% -> 64.6%) while terminal
  equity fell 23%.

  Set max_total_exposure_pct / max_cluster_pct to 1 to model an
  unbounded account - useful for isolating a signal, never the
  headline.
- **Benchmark**: SPY total-return (adjustment=all) over the identical
  period. excess_return_net is THE headline number: a strategy that
  makes money but loses to SPY is a failure and reports as one.

Survivorship: see SURVIVORSHIP_STATEMENT below — embedded in every
result's costs_applied and market_regime_notes, not just in a doc.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

from catalyst.backtest import BacktestResult
from catalyst.backtest.data import Bar, BarCache, PointInTimeView
from catalyst.discovery.correlation import cluster_key_for
from catalyst.backtest.scoring import (
    ZERO,
    benchmark_comparison,
    compute_sample_stats,
    period_months,
)

SURVIVORSHIP_STATEMENT = (
    "SURVIVORSHIP BIAS: the universe was built from symbols listed today; "
    "Alpaca can price a dead ticker but cannot enumerate dead tickers "
    "(DATA-SOURCES.md §1.4-1.5), so delisted/bankrupt/acquired names are "
    "absent by construction. This FLATTERS long strategies — the names that "
    "went to zero are missing, so returns are overstated and tail risk "
    "understated. Treat any long edge that is small relative to this bias "
    "as unproven until a point-in-time universe exists."
)


@dataclass(frozen=True)
class ReplayConfig:
    starting_cash: Decimal = Decimal("1000")
    max_positions: int = 5
    per_side_cost_pct: Decimal = Decimal("0.0015")   # 15 bps per side, pessimistic
    api_cost_monthly_usd: Decimal = Decimal("8")     # BUILD-BRIEF "workable" tier
    conviction_floor: Decimal = Decimal("0")         # strategies gate via direction
    max_holding_trading_days: int = 15               # ~3 weeks; brief's hard ceiling
    split_date: date = date(2023, 12, 31)            # chronological in/out split
    benchmark_symbol: str = "SPY"
    min_notional_usd: Decimal = Decimal("1")
    #: THE PRODUCTION EXPOSURE CAP, mirrored here so the grade describes
    #: the bot that will actually run.
    #:
    #: Found by measuring rather than reading: production sizes by risk,
    #: notional = (equity x max_loss_per_position_pct) / max(gap, stop),
    #: which for insider_cluster lands at exactly $200 on a $1,000
    #: account - identical to this harness's equity/max_positions. So the
    #: per-position size agreed. The PORTFOLIO did not: five slots at
    #: $200 is 100% invested, and HARD_BOUNDS.max_total_exposure_pct is
    #: 0.90, so live the fifth position is cut to $100 and the bot runs
    #: at 90% invested.
    #:
    #: Without this the harness graded a portfolio the risk engine would
    #: never assemble, and it graded it ~11% larger. Set to 1 to model an
    #: uncapped account.
    max_total_exposure_pct: Decimal = Decimal("0.90")
    #: HARD_BOUNDS.max_correlated_cluster_pct. "Correlated positions are
    #: one bet wearing several hats" (BUILD-BRIEF), so the risk engine
    #: caps how much can sit in a single sector/type/resolution-week
    #: bucket. It matters far more here than it looks: the Form 4
    #: payload carries NO sector, so every insider candidate keys on
    #: "unknown|insider_cluster|<week>" and they all bind against each
    #: other. Measured against the live engine: five candidates in ONE
    #: week open 2 positions for $350, not 5 for $1,000.
    #:
    #: Set to 1 to model an account with no cluster bound.
    max_cluster_pct: Decimal = Decimal("0.35")


@dataclass(frozen=True)
class TradeRecord:
    candidate_id: str
    ticker: str
    signal_day: date
    entry_day: date
    entry_fill: Decimal
    exit_day: date
    exit_fill: Decimal
    qty: Decimal
    notional: Decimal
    ret: Decimal                 # (exit_fill / entry_fill) - 1, net of both haircuts
    exit_reason: str             # "planned_exit" | "end_of_range" | "no_more_data"


@dataclass(frozen=True)
class SkipRecord:
    candidate_id: str
    day: date | None
    reason: str


@dataclass
class _Open:
    candidate_id: str
    ticker: str
    qty: Decimal
    entry_fill: Decimal
    entry_day: date
    signal_day: date
    notional: Decimal
    planned_exit_idx: int
    #: The production correlated-cluster key. Insider clusters carry no
    #: sector (the Form 4 payload has no such field), so every one of
    #: them keys on the same "unknown|insider_cluster|<week>" and the
    #: 35% cluster bound binds between them.
    cluster_key: str = ""


@dataclass(frozen=True)
class ReplayDetail:
    result: BacktestResult
    trades: tuple[TradeRecord, ...]
    skips: tuple[SkipRecord, ...]
    equity_curve: tuple[tuple[date, Decimal], ...]


# Coarse, honest regime notes so a result can never claim more than its window.
_REGIMES = [
    (date(2016, 1, 1), date(2018, 1, 31), "2016-17 low-volatility grind higher"),
    (date(2018, 2, 1), date(2019, 12, 31), "2018 vol shocks (Feb, Q4 -19%), 2019 recovery"),
    (date(2020, 1, 1), date(2020, 12, 31), "2020 COVID crash (-34% in 5 weeks) then V-recovery"),
    (date(2021, 1, 1), date(2021, 12, 31), "2021 stimulus bull, speculative froth"),
    (date(2022, 1, 1), date(2022, 12, 31), "2022 rate-hike bear (SPY ~-25% peak-to-trough)"),
    (date(2023, 1, 1), date(2024, 12, 31), "2023-24 AI-led bull, narrow leadership"),
    (date(2025, 1, 1), date(2026, 12, 31), "2025-26 continued bull (recent, regime label provisional)"),
]


def describe_regime(start: date, end: date) -> str:
    hit = [label for lo, hi, label in _REGIMES if lo <= end and hi >= start]
    windows = "; ".join(hit) if hit else "no regime notes for this window"
    return (
        f"Period {start.isoformat()} to {end.isoformat()}: {windows}. "
        "Net of regime, this window is bull-heavy: long strategies get a "
        "tailwind here that a full cycle would not provide. | "
        + SURVIVORSHIP_STATEMENT
    )


def replay(
    signal_fn: Callable,
    universe: list,
    date_range: tuple[date, date],
    *,
    cache: BarCache,
    config: ReplayConfig | None = None,
    strategy_name: str = "unnamed",
) -> BacktestResult:
    """ARCHITECTURE.md §3.2 contract. signal_fn(candidate, view) -> ResearchView."""
    return replay_detailed(
        signal_fn, universe, date_range,
        cache=cache, config=config, strategy_name=strategy_name,
    ).result


def replay_detailed(
    signal_fn: Callable,
    universe: list,
    date_range: tuple[date, date],
    *,
    cache: BarCache,
    config: ReplayConfig | None = None,
    strategy_name: str = "unnamed",
) -> ReplayDetail:
    cfg = config or ReplayConfig()
    start, end = date_range
    bench_bars = [b for b in cache.load_bars(cfg.benchmark_symbol)
                  if start <= b.day <= end]
    if len(bench_bars) < 2:
        raise ValueError(
            f"Fewer than 2 {cfg.benchmark_symbol} sessions cached in "
            f"{start}..{end}; cannot build a calendar or a benchmark."
        )
    calendar: list[date] = [b.day for b in bench_bars]
    last_idx = len(calendar) - 1

    # Per-ticker lookup structures, built lazily.
    _days: dict[str, list[date]] = {}
    _bars: dict[str, dict[date, Bar]] = {}

    def _load(ticker: str) -> None:
        if ticker not in _days:
            bars = cache.load_bars(ticker)
            _days[ticker] = [b.day for b in bars]
            _bars[ticker] = {b.day: b for b in bars}

    def bar_on(ticker: str, day: date) -> Bar | None:
        _load(ticker)
        return _bars[ticker].get(day)

    def last_close_leq(ticker: str, day: date) -> Decimal | None:
        _load(ticker)
        i = bisect_right(_days[ticker], day)
        return _bars[ticker][_days[ticker][i - 1]].close if i else None

    def has_bar_after(ticker: str, day: date, through: date) -> bool:
        _load(ticker)
        i = bisect_right(_days[ticker], day)
        return i < len(_days[ticker]) and _days[ticker][i] <= through

    # Map candidates to the session their signal fires (first session >= catalyst_date).
    signals_at: dict[int, list] = {}
    skips: list[SkipRecord] = []
    for cand in universe:
        if cand.catalyst_date < calendar[0]:
            skips.append(SkipRecord(cand.id, None, "catalyst_before_range"))
            continue
        idx = bisect_left(calendar, cand.catalyst_date)
        if idx > last_idx:
            skips.append(SkipRecord(cand.id, None, "catalyst_after_range"))
            continue
        signals_at.setdefault(idx, []).append(cand)

    settled_cash = cfg.starting_cash
    pending: dict[int, Decimal] = {}      # calendar idx -> cash that settles then
    positions: list[_Open] = []
    entries_at: dict[int, list] = {}      # calendar idx -> [(candidate, view)]
    trades: list[TradeRecord] = []
    equity_curve: list[tuple[date, Decimal]] = []
    cost = cfg.per_side_cost_pct

    def unsettled_total() -> Decimal:
        return sum(pending.values(), ZERO)

    def mark_positions(day: date) -> Decimal:
        total = ZERO
        for p in positions:
            px = last_close_leq(p.ticker, day)
            total += p.qty * (px if px is not None else p.entry_fill)
        return total

    def close_position(p: _Open, i: int, fill: Decimal, reason: str) -> None:
        proceeds = p.qty * fill
        pending[i + 1] = pending.get(i + 1, ZERO) + proceeds   # T+1 settlement
        trades.append(TradeRecord(
            candidate_id=p.candidate_id, ticker=p.ticker,
            signal_day=p.signal_day, entry_day=p.entry_day,
            entry_fill=p.entry_fill, exit_day=calendar[i], exit_fill=fill,
            qty=p.qty, notional=p.notional,
            ret=fill / p.entry_fill - 1, exit_reason=reason,
        ))

    for i, day in enumerate(calendar):
        # 1. T+1 settlement: proceeds from earlier sales become spendable.
        if i in pending:
            settled_cash += pending.pop(i)

        # 2. Exits at today's open (planned, forced end-of-range, or dead data).
        still_open: list[_Open] = []
        for p in positions:
            due = p.planned_exit_idx <= i or i == last_idx
            if not due:
                still_open.append(p)
                continue
            bar = bar_on(p.ticker, day)
            if bar is not None:
                reason = "end_of_range" if (i == last_idx and p.planned_exit_idx > i) \
                    else "planned_exit"
                close_position(p, i, bar.open * (1 - cost), reason)
            elif i == last_idx or not has_bar_after(p.ticker, day, calendar[-1]):
                # No bar today and none coming: value at last known close,
                # haircut applied. Pessimistic-but-honest for a symbol whose
                # data simply stops (halt into delisting would be worse; a
                # survivorship-biased universe rarely exercises this path,
                # which is itself part of the bias statement).
                px = last_close_leq(p.ticker, day)
                close_position(p, i, (px if px is not None else p.entry_fill) * (1 - cost),
                               "no_more_data")
            else:
                # Bar missing today but the symbol trades again: postpone one session.
                p.planned_exit_idx = i + 1
                still_open.append(p)
        positions = still_open

        # 3. Entries queued for today's open (signal fired yesterday or earlier).
        for cand, view in entries_at.pop(i, []):
            if len(positions) >= cfg.max_positions:
                skips.append(SkipRecord(cand.id, day, "no_free_slot"))
                continue
            bar = bar_on(cand.ticker, day)
            if bar is None:
                skips.append(SkipRecord(cand.id, day, "no_bar_on_entry_day"))
                continue
            mark_day = calendar[i - 1] if i > 0 else day
            equity = settled_cash + unsettled_total() + mark_positions(mark_day)
            budget = equity / cfg.max_positions
            # The exposure cap, exactly as the risk engine applies it:
            # the position is CUT to whatever headroom remains, not
            # refused. Measured against the live engine - four full
            # $200 slots leave $100 of headroom and it sizes the fifth
            # at $100 rather than skipping it.
            headroom = (equity * cfg.max_total_exposure_pct
                        - sum(p.notional for p in positions))
            # The correlated-cluster bound, keyed exactly as production
            # keys it (discovery.correlation.cluster_key_for), so a week
            # in which several clusters complete is treated as the one
            # bet it is rather than as several independent ones.
            ckey = cluster_key_for(cand.sector, cand.catalyst_type,
                                   cand.catalyst_date)
            cluster_room = (equity * cfg.max_cluster_pct
                            - sum(p.notional for p in positions
                                  if p.cluster_key == ckey))
            notional = min(budget, settled_cash, headroom, cluster_room)
            if notional < cfg.min_notional_usd:
                # NAME THE RULE THAT ACTUALLY BOUND. "Insufficient
                # settled cash" and "the exposure cap is full" are
                # different diagnoses with opposite fixes, and a skip
                # log that conflates them cannot tell you which.
                smallest = min(budget, settled_cash, headroom, cluster_room)
                reason = ("max_correlated_cluster" if smallest == cluster_room
                          else "max_total_exposure_pct" if smallest == headroom
                          else "insufficient_settled_cash")
                skips.append(SkipRecord(cand.id, day, reason))
                continue
            fill = bar.open * (1 + cost)
            qty = notional / fill
            settled_cash -= notional
            hold = max(1, min(view.expected_holding_days, cfg.max_holding_trading_days))
            positions.append(_Open(
                candidate_id=cand.id, ticker=cand.ticker, qty=qty,
                entry_fill=fill, entry_day=day, signal_day=calendar[i - 1] if i else day,
                notional=notional, planned_exit_idx=i + hold,
                cluster_key=ckey,
            ))

        # 4. Signals at today's close: view sees data through today only.
        for cand in signals_at.pop(i, []):
            view = signal_fn(cand, PointInTimeView(cache, as_of=day))
            if view is None or view.direction == "no_trade":
                skips.append(SkipRecord(cand.id, day, "signal_no_trade"))
                continue
            if view.direction == "short":
                skips.append(SkipRecord(cand.id, day, "short_not_supported_cash_account"))
                continue
            if view.priced_in:
                skips.append(SkipRecord(cand.id, day, "priced_in"))
                continue
            if Decimal(str(view.conviction)) < cfg.conviction_floor:
                skips.append(SkipRecord(cand.id, day, "below_conviction_floor"))
                continue
            if i + 1 >= last_idx:
                # i+1 > last_idx: the signal fired on the last session, so
                #   there is no session left to enter in at all.
                # i+1 == last_idx: the entry would open on the FINAL session,
                #   with no later session to exit in. The only fill allowed
                #   that day is the open — the same price the forced
                #   end-of-range exit uses — so opening would book a
                #   zero-information round trip whose only content is two
                #   cost haircuts, and (before this guard) the position
                #   survived the replay and tripped the end-of-range
                #   assertion below. Skip it instead, and say why.
                reason = ("range_ended_before_entry" if i + 1 > last_idx
                          else "range_end_no_entry")
                skips.append(SkipRecord(cand.id, day, reason))
                continue
            entries_at.setdefault(i + 1, []).append((cand, view))

        # 5. Mark to market at today's close.
        equity_curve.append(
            (day, settled_cash + unsettled_total() + mark_positions(day))
        )

    assert not positions, "replay bug: positions survived the forced end-of-range exit"

    # Anything still queued past the end never traded.
    for i, entries in entries_at.items():
        for cand, _view in entries:
            skips.append(SkipRecord(cand.id, None, "range_ended_before_entry"))

    final_equity = settled_cash + unsettled_total()
    months = period_months(calendar[0], calendar[-1])
    api_cost_total = cfg.api_cost_monthly_usd * months
    net_final = final_equity - api_cost_total
    strategy_return_net = net_final / cfg.starting_cash - 1

    benchmark = benchmark_comparison(bench_bars, strategy_return_net)

    split = cfg.split_date
    in_trades = [t for t in trades if t.entry_day <= split]
    out_trades = [t for t in trades if t.entry_day > split]
    in_curve = [eq for d, eq in equity_curve if d <= split]
    out_curve = [eq for d, eq in equity_curve if d > split]
    in_end = min(split, calendar[-1])
    in_months = period_months(calendar[0], in_end) if calendar[0] <= split else ZERO
    out_months = period_months(max(split, calendar[0]), calendar[-1]) \
        if calendar[-1] > split else ZERO

    in_sample = compute_sample_stats(
        [t.ret for t in in_trades], [t.notional for t in in_trades],
        in_curve, in_months, cfg.api_cost_monthly_usd)
    out_sample = compute_sample_stats(
        [t.ret for t in out_trades], [t.notional for t in out_trades],
        out_curve, out_months, cfg.api_cost_monthly_usd)

    costs_applied = {
        "fill_policy": ("signal at close of day D fills at open of D+1; "
                        "entry pays open*(1+cost), exit receives open*(1-cost); "
                        "never a same-day close fill"),
        "per_side_cost_pct": str(cfg.per_side_cost_pct),
        "api_cost_monthly_usd": str(cfg.api_cost_monthly_usd),
        "api_cost_total_usd": str(api_cost_total.quantize(Decimal("0.01"))),
        "api_cost_note": ("deducted from final equity before the benchmark "
                          "comparison; assumption per BUILD-BRIEF $8/mo tier"),
        "settlement": "T+1: sale proceeds unusable until the next session",
        "account": (f"${cfg.starting_cash} cash account, long-only, no leverage, "
                    f"fractional shares, max {cfg.max_positions} positions, "
                    "equal-weight risk budget"),
        "holding_clamp_trading_days": str(cfg.max_holding_trading_days),
        "split_date": cfg.split_date.isoformat(),
        "split_method": "chronological (see scoring.py for why never random)",
        "benchmark": (f"{cfg.benchmark_symbol} feed=sip adjustment=all "
                      "(total return), first-session open to last-session close"),
        "survivorship": SURVIVORSHIP_STATEMENT,
    }

    result = BacktestResult(
        strategy_name=strategy_name,
        date_range=(calendar[0], calendar[-1]),
        in_sample=in_sample,
        out_of_sample=out_sample,
        benchmark=benchmark,
        costs_applied=costs_applied,
        market_regime_notes=describe_regime(calendar[0], calendar[-1]),
    )
    return ReplayDetail(
        result=result,
        trades=tuple(trades),
        skips=tuple(skips),
        equity_curve=tuple(equity_curve),
    )
