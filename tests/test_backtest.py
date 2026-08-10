"""Backtest harness tests — fully offline.

Every fixture writes synthetic bars directly into the cache format
(BarCache CSV) — the format is the contract, the network is never
touched (conftest's socket guard enforces that mechanically).

What is covered, mapped to the biases the harness exists to prevent:
- look-ahead: the point-in-time view cannot see past as_of, and an
  explicit request for the future raises;
- fill optimism: fills are next-session open, never same-session close;
- free money: T+1 settlement blocks same-day reuse of sale proceeds;
- benchmark math: SPY comparison verified on a known synthetic series;
- costs: adding costs strictly reduces reported returns;
- optimisation on noise: the in/out-of-sample split is chronological;
- manufactured edge: a random strategy on trending synthetic data still
  lags buy-and-hold of the same synthetic index.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from catalyst.backtest.data import Bar, BarCache, LookAheadError, PointInTimeView
from catalyst.backtest.harness import (
    SURVIVORSHIP_STATEMENT,
    ReplayConfig,
    replay,
    replay_detailed,
)
from catalyst.backtest.scoring import (
    benchmark_comparison,
    compute_sample_stats,
    max_drawdown,
    period_months,
    persist_result,
)
from catalyst.discovery import Candidate
from catalyst.research.schema import ResearchView

D = Decimal


# ---------------------------------------------------------------- fixtures

def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_bar(day: date, open_, close=None, volume="1000") -> Bar:
    o, c = D(str(open_)), D(str(close if close is not None else open_))
    return Bar(day=day, open=o, high=max(o, c), low=min(o, c), close=c,
               volume=D(volume))


def flat_bars(days: list[date], price) -> list[Bar]:
    return [make_bar(d, price) for d in days]


def mk_candidate(ticker: str, catalyst_date: date, cid: str) -> Candidate:
    return Candidate(
        id=cid, ticker=ticker, catalyst_type="test",
        catalyst_date=catalyst_date, catalyst_date_confidence="confirmed",
        source_event_ids=("t",), discovered_at=datetime(2020, 1, 1),
        sector="test", correlation_tags=(),
    )


def mk_view(cid: str, direction="long", holding=5, priced_in=False,
            conviction=1.0) -> ResearchView:
    return ResearchView(
        candidate_id=cid, direction=direction, conviction=conviction,
        thesis="t", invalidation="t", expected_holding_days=holding,
        priced_in=priced_in, priced_in_reasoning="t",
    )


def const_signal(holding=5, **kw):
    def fn(candidate, view):
        return mk_view(candidate.id, holding=holding, **kw)
    return fn


START = date(2020, 1, 6)  # a Monday


# ------------------------------------------------------ point-in-time view

def test_point_in_time_view_cannot_see_the_future(tmp_path):
    days = weekdays(START, 10)
    cache = BarCache(tmp_path)
    cache.write_bars("AAA", [make_bar(d, 100 + i) for i, d in enumerate(days)])

    as_of = days[4]
    view = PointInTimeView(cache, as_of=as_of)

    got = view.bars("AAA")
    assert got, "expected bars up to as_of"
    assert max(b.day for b in got) == as_of, "view returned a bar after as_of"
    assert len(got) == 5

    assert view.last_bar("AAA").day == as_of
    assert view.last_close("AAA") == D("104")

    with pytest.raises(LookAheadError):
        view.bars("AAA", end=days[5])


def test_harness_hands_signal_fn_a_view_clamped_to_signal_day(tmp_path):
    days = weekdays(START, 10)
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    cache.write_bars("AAA", flat_bars(days, 10))
    seen = {}

    def spying_signal(candidate, view):
        seen["as_of"] = view.as_of
        seen["last_day"] = view.bars("AAA")[-1].day
        with pytest.raises(LookAheadError):
            view.bars("AAA", end=view.as_of + timedelta(days=1))
        return mk_view(candidate.id)

    replay_detailed(
        spying_signal, [mk_candidate("AAA", days[3], "c1")], (days[0], days[-1]),
        cache=cache,
    )
    assert seen["as_of"] == days[3]
    assert seen["last_day"] == days[3]


# ------------------------------------------------------------------- fills

def test_fill_is_next_session_open_not_same_session_close(tmp_path):
    days = weekdays(START, 10)
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    # Signal day (d3) closes at 10; next session gaps open to 12. An
    # optimistic (lying) harness fills at 10; the honest one pays 12.
    bars = [make_bar(d, 10) for d in days]
    bars[4] = make_bar(days[4], 12, 12)
    cache.write_bars("AAA", bars)

    cost = D("0.0015")
    detail = replay_detailed(
        const_signal(), [mk_candidate("AAA", days[3], "c1")], (days[0], days[-1]),
        cache=cache, config=ReplayConfig(per_side_cost_pct=cost),
    )
    assert len(detail.trades) == 1
    t = detail.trades[0]
    assert t.signal_day == days[3]
    assert t.entry_day == days[4], "fill must be the session AFTER the signal"
    assert t.entry_fill == D("12") * (1 + cost), (
        "entry must be next-session open plus the cost haircut - "
        f"got {t.entry_fill}"
    )


def test_forced_exit_at_end_of_range_and_no_open_positions(tmp_path):
    days = weekdays(START, 10)
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    cache.write_bars("AAA", flat_bars(days, 10))
    detail = replay_detailed(
        const_signal(holding=15),  # far beyond the range
        [mk_candidate("AAA", days[5], "c1")], (days[0], days[-1]),
        cache=cache,
    )
    assert len(detail.trades) == 1
    assert detail.trades[0].exit_reason == "end_of_range"
    assert detail.trades[0].exit_day == days[-1]


# -------------------------------------------------------------- settlement

def test_t_plus_1_settlement_blocks_same_day_reuse_of_proceeds(tmp_path):
    days = weekdays(START, 13)
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    for sym in ("AAA", "BBB", "CCC"):
        cache.write_bars(sym, flat_bars(days, 10))

    cfg = ReplayConfig(per_side_cost_pct=D("0"), api_cost_monthly_usd=D("0"),
                       max_positions=1)
    universe = [
        mk_candidate("AAA", days[0], "cA"),   # enters d1, exits d6 (5-day hold)
        mk_candidate("BBB", days[5], "cB"),   # entry d6: proceeds NOT settled yet
        mk_candidate("CCC", days[6], "cC"),   # entry d7: proceeds settled
    ]
    detail = replay_detailed(const_signal(holding=5), universe,
                             (days[0], days[-1]), cache=cache, config=cfg)

    traded = {t.candidate_id for t in detail.trades}
    assert "cA" in traded and "cC" in traded
    assert "cB" not in traded, "T+1: cB was funded with same-day sale proceeds"
    reasons = {s.candidate_id: s.reason for s in detail.skips}
    assert reasons.get("cB") == "insufficient_settled_cash"
    cC = next(t for t in detail.trades if t.candidate_id == "cC")
    assert cC.entry_day == days[7], "proceeds must be usable the NEXT session"


def test_max_position_slots_cap(tmp_path):
    days = weekdays(START, 10)
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    syms = ["S1", "S2", "S3", "S4", "S5", "S6"]
    for s in syms:
        cache.write_bars(s, flat_bars(days, 10))
    universe = [mk_candidate(s, days[0], f"c{s}") for s in syms]
    detail = replay_detailed(const_signal(), universe, (days[0], days[-1]),
                             cache=cache)
    assert len(detail.trades) == 5
    assert [s.reason for s in detail.skips] == ["no_free_slot"]


# --------------------------------------------------------------- benchmark

def test_benchmark_comparison_math_on_known_series(tmp_path):
    days = weekdays(START, 21)
    cache = BarCache(tmp_path)
    # SPY: first open 100, last close 110 -> +10.00% total return.
    spy = flat_bars(days, 100)
    spy[-1] = make_bar(days[-1], 100, 110)
    cache.write_bars("SPY", spy)

    cfg = ReplayConfig()  # $8/month API cost, default
    result = replay(const_signal(), [], (days[0], days[-1]),
                    cache=cache, config=cfg)

    b = result.benchmark
    assert b.spy_total_return == D("110") / D("100") - 1
    assert b.period_start == days[0] and b.period_end == days[-1]

    months = period_months(days[0], days[-1])
    api_cost = cfg.api_cost_monthly_usd * months
    expected_net = (cfg.starting_cash - api_cost) / cfg.starting_cash - 1
    assert b.strategy_total_return_net == expected_net
    assert b.excess_return_net == expected_net - b.spy_total_return
    assert b.excess_return_net < 0, (
        "an all-cash strategy against a rising index must show negative excess"
    )


def test_benchmark_comparison_uses_first_open_not_first_close():
    days = weekdays(START, 3)
    bars = [make_bar(days[0], 100, 105), make_bar(days[1], 105),
            make_bar(days[2], 105, 110)]
    cmp_ = benchmark_comparison(bars, D("0"))
    assert cmp_.spy_total_return == D("110") / D("100") - 1  # open 100, not close 105


# ------------------------------------------------------------------- costs

def test_costs_strictly_reduce_reported_returns(tmp_path):
    days = weekdays(START, 15)
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    # A winning trade: buy at 10, price rises to 11.
    bars = [make_bar(d, 10 + i * D("0.1")) for i, d in enumerate(days)]
    cache.write_bars("AAA", bars)
    universe = [mk_candidate("AAA", days[2], "c1")]

    def run(spread, api):
        return replay_detailed(
            const_signal(), universe, (days[0], days[-1]), cache=cache,
            config=ReplayConfig(per_side_cost_pct=spread, api_cost_monthly_usd=api),
        )

    free = run(D("0"), D("0"))
    with_spread = run(D("0.002"), D("0"))
    with_api = run(D("0"), D("8"))

    assert with_spread.trades[0].ret < free.trades[0].ret
    assert (with_spread.result.benchmark.strategy_total_return_net
            < free.result.benchmark.strategy_total_return_net)
    # API cost does not touch per-trade returns, only the net figure.
    assert with_api.trades[0].ret == free.trades[0].ret
    assert (with_api.result.benchmark.strategy_total_return_net
            < free.result.benchmark.strategy_total_return_net)


# ------------------------------------------------------------------- split

def test_in_out_of_sample_split_is_chronological(tmp_path):
    days = weekdays(START, 40)
    split = days[19]
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    # A linear ramp, not a flat price and not a %-per-session compounding
    # ramp: the % return realised over a fixed holding window depends on
    # WHERE on the ramp a trade sits, so early-window and late-window
    # trades are guaranteed distinguishable returns. With a flat price
    # (or exponential drift) every trade nets the same %, and a swapped
    # or reversed split would produce identical sample_size counts (2
    # early, 2 late either way round) and slip straight past a
    # count-only check.
    bars = [make_bar(d, D("10") + D("0.5") * i) for i, d in enumerate(days)]
    cache.write_bars("AAA", bars)
    universe = [
        mk_candidate("AAA", days[2], "early1"),
        mk_candidate("AAA", days[8], "early2"),
        mk_candidate("AAA", days[25], "late1"),
        mk_candidate("AAA", days[30], "late2"),
    ]
    cfg = ReplayConfig(split_date=split)
    detail = replay_detailed(const_signal(holding=3), universe,
                             (days[0], days[-1]), cache=cache, config=cfg)
    r = detail.result
    assert r.in_sample.sample_size == 2
    assert r.out_of_sample.sample_size == 2
    in_days = [t.entry_day for t in detail.trades if t.candidate_id.startswith("early")]
    out_days = [t.entry_day for t in detail.trades if t.candidate_id.startswith("late")]
    assert max(in_days) <= split < min(out_days), (
        "split must be chronological: every in-sample trade strictly precedes "
        "every out-of-sample trade"
    )

    # Counts alone (2 in, 2 out) cannot tell a correct chronological split
    # from a swapped or reversed one on this fixture - verify the CONTENT
    # of each sample matches the chronologically correct side, using the
    # trade list (built independently of the in/out bucketing under test)
    # as ground truth.
    early_returns = [t.ret for t in detail.trades if t.candidate_id.startswith("early")]
    late_returns = [t.ret for t in detail.trades if t.candidate_id.startswith("late")]
    expected_in_mean = sum(early_returns) / len(early_returns)
    expected_out_mean = sum(late_returns) / len(late_returns)
    assert expected_in_mean != expected_out_mean, (
        "fixture bug: early and late trades must realise different returns "
        "or this test cannot distinguish a correct split from a swapped one"
    )
    assert r.in_sample.mean_return == expected_in_mean, (
        "in_sample must be built from the chronologically EARLY trades, not "
        f"got mean_return={r.in_sample.mean_return}, expected the early "
        f"trades' mean {expected_in_mean}"
    )
    assert r.out_of_sample.mean_return == expected_out_mean, (
        "out_of_sample must be built from the chronologically LATE trades, "
        f"got mean_return={r.out_of_sample.mean_return}, expected the late "
        f"trades' mean {expected_out_mean}"
    )


# ------------------------------------------------- the null-edge property

def test_random_strategy_on_trending_data_still_lags_buy_and_hold(tmp_path):
    """Synthetic uptrend, everywhere: +0.1%/session for every symbol and
    for the synthetic SPY. A random long-only strategy sits partly in
    cash and pays costs, so it MUST lag buy-and-hold of the same index.
    If this fails, the harness is manufacturing edge."""
    import random

    days = weekdays(START, 300)
    price = [D("100") * (D("1.001") ** i) for i in range(len(days))]
    cache = BarCache(tmp_path)
    syms = ["T1", "T2", "T3", "T4", "T5", "T6"]
    for s in syms + ["SPY"]:
        cache.write_bars(s, [make_bar(d, price[i]) for i, d in enumerate(days)])

    rng = random.Random(42)
    universe = [
        mk_candidate(rng.choice(syms), rng.choice(days[:-25]), f"c{i}")
        for i in range(40)
    ]

    def random_signal(candidate, view):
        r = random.Random(f"seed:{candidate.id}")
        return mk_view(candidate.id, holding=r.randint(5, 15))

    detail = replay_detailed(random_signal, universe, (days[0], days[-1]),
                             cache=cache, config=ReplayConfig())
    b = detail.result.benchmark
    assert len(detail.trades) >= 20, "scenario must actually produce a sample"
    assert b.spy_total_return > D("0.25")
    assert b.excess_return_net < 0, (
        f"random strategy shows edge over its own index "
        f"(excess {b.excess_return_net}) - the harness is lying somewhere"
    )


# ------------------------------------------------------------- persistence

def test_persist_result_writes_both_tables(tmp_db, tmp_path):
    days = weekdays(START, 15)
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    cache.write_bars("AAA", flat_bars(days, 10))
    result = replay(const_signal(), [mk_candidate("AAA", days[2], "c1")],
                    (days[0], days[-1]), cache=cache, strategy_name="persist-test")

    result_id = persist_result(tmp_db, result, mode="structural")

    row = tmp_db.execute(
        "SELECT strategy_name, mode, spy_total_return, excess_return_net,"
        " market_regime_notes, costs_applied FROM backtest_results WHERE id=?",
        (result_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "persist-test"
    assert row[1] == "structural"
    assert row[2] == str(result.benchmark.spy_total_return)
    assert row[3] == str(result.benchmark.excess_return_net)
    assert "SURVIVORSHIP" in row[4], "bias statement must be persisted, not doc-only"
    assert "SURVIVORSHIP" in row[5]

    kinds = {r[0]: r[1] for r in tmp_db.execute(
        "SELECT sample_kind, sample_size FROM backtest_sample_stats"
        " WHERE result_id=?", (result_id,)).fetchall()}
    assert set(kinds) == {"in_sample", "out_of_sample"}


def test_persist_rejects_unknown_mode(tmp_db, tmp_path):
    days = weekdays(START, 5)
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    result = replay(const_signal(), [], (days[0], days[-1]), cache=cache)
    with pytest.raises(ValueError, match="mode"):
        persist_result(tmp_db, result, mode="vibes")


# --------------------------------------------------------- cache and stats

def test_cache_round_trip_and_metadata(tmp_path):
    days = weekdays(START, 5)
    bars = [make_bar(d, D("10.123456"), D("10.2")) for d in days]
    cache = BarCache(tmp_path)
    cache.write_bars("aaa", bars)  # case-insensitive on write...
    cache.write_meta({"feed": "sip", "adjustment": "all", "symbol_count": 1})

    fresh = BarCache(tmp_path)     # ...and on a cold read
    loaded = fresh.load_bars("AAA")
    assert list(loaded) == bars, "round trip must preserve exact Decimal values"
    assert fresh.symbols() == ["AAA"]
    meta = fresh.read_meta()
    assert meta["feed"] == "sip" and meta["adjustment"] == "all"

    with pytest.raises(KeyError, match="fetch_history"):
        fresh.load_bars("MISSING")


def test_max_drawdown_and_sample_stats_math():
    assert max_drawdown([D(x) for x in (100, 120, 90, 110, 80)]) == D("40") / D("120")
    assert max_drawdown([D("100"), D("110")]) == 0

    stats = compute_sample_stats(
        trade_returns=[D("0.10"), D("-0.05"), D("0.02")],
        trade_notionals=[D("200"), D("200"), D("200")],
        equity_segment=[D("1000"), D("1050"), D("1000")],
        months=D("3"), api_cost_monthly_usd=D("8"),
    )
    assert stats.sample_size == 3
    assert stats.hit_rate == D("2") / D("3")
    assert stats.worst_single_outcome == D("-0.05")
    # $24 API over 3 trades = $8/trade on $200 notional = 4% needed per trade.
    assert stats.return_per_trade_needed_to_break_even == D("8") / D("200")

    empty = compute_sample_stats([], [], [D("1000")], D("1"), D("8"))
    assert empty.sample_size == 0


def test_every_result_carries_the_survivorship_statement(tmp_path):
    days = weekdays(START, 5)
    cache = BarCache(tmp_path)
    cache.write_bars("SPY", flat_bars(days, 100))
    result = replay(const_signal(), [], (days[0], days[-1]), cache=cache)
    assert result.costs_applied["survivorship"] == SURVIVORSHIP_STATEMENT
    assert SURVIVORSHIP_STATEMENT in result.market_regime_notes
    assert "FLATTERS long strategies" in SURVIVORSHIP_STATEMENT
