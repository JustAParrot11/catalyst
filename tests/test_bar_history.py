"""Sizing read a cupboard nobody had stocked.

`risk/stock_gap.py` sizes each position against the STOCK'S own measured
history, falling back to its catalyst category's number when there is
none. That mechanism was correct, tested, and completely inert on the
owner's machine: `data/` is gitignored, and the only thing in the
running bot that ever wrote a bar cache is the daily SPY benchmark
refresh. Measured - 125 symbols in the live cache against 4,861 in
development - so essentially every candidate fell back to its category.

What it is worth, over 400 random tickers from the development cache:

    fda_decision   per-stock decides  89%   category binds  10%
    earnings       per-stock decides  37%   category binds  63%
    merger         per-stock decides   0%   category binds 100%

THE DANGEROUS DIRECTION IS SPECIFIC, and most of this file guards it.
A SHORTER history measures a SMALLER worst-case gap, and a smaller gap
divides into a LARGER position. So every way of accidentally getting
less history than exists - a dropped page, a half-written file, a
partial session - inflates risk rather than reducing it. Failures must
leave NO file at all, so that sizing finds nothing and the conservative
category value stands.

Offline throughout: the broker is a fake and `tests/conftest.py` blocks
sockets by contract.
"""

import csv
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.data import bar_history as bh

#: ANCHORED TO THE REAL CLOCK. `is_fresh` measures a file's age from
#: its MTIME, which is stamped by the operating system at the moment the
#: test writes it - the real now, never this constant. Pinned to a
#: calendar date, the gap between the two widens by a day per day, and
#: the staleness probe at NOW + MAX_CACHE_AGE_DAYS + 1 measures one day
#: less of age each morning until it drops under the threshold and the
#: refetch test fails.
#:
#: It went red exactly one day after the constant was written: probing
#: at 2026-09-16 against a file stamped 2026-08-17 measured 29 days
#: against a 30-day limit.
NOW = datetime.now(timezone.utc)


#: Deterministic, and crucially it goes DOWN sometimes. A generator that
#: only drifts upwards produces no adverse overnight gap at all, so
#: `worst_overnight_gap` correctly returns None and the fixture ends up
#: testing the no-history path while appearing to test the opposite.
_MOVES = (0.004, -0.006, 0.002, -0.003, 0.007, -0.011, 0.001, -0.002)


def make_bars(n, *, start_price=50.0):
    """`n` well-formed daily bars in Alpaca's shape, with real
    two-directional movement so a gap is actually measurable."""
    out, day, price = [], datetime(2023, 1, 2, tzinfo=timezone.utc), start_price
    for i in range(n):
        move = _MOVES[i % len(_MOVES)]
        price = price * (1 + move)
        out.append({"t": day.isoformat(), "o": price, "h": price * 1.01,
                    "l": price * 0.99, "c": price, "v": 1_000_000})
        day += timedelta(days=1)
    return out


class FakeBroker:
    """Records what was asked for, and can be told to misbehave."""

    _UNSET = object()

    def __init__(self, bars=_UNSET, raises=None, pages=None):
        # A SENTINEL, not None. `bars=None` is a payload the code must
        # survive, and defaulting it to good data meant that case was
        # silently never tested.
        self.bars = make_bars(400) if bars is FakeBroker._UNSET else bars
        self.raises = raises
        self.pages = pages
        self.calls = []

    def get_daily_bars(self, symbol, start, end, **kw):
        self.calls.append((symbol, start, end))
        if self.raises:
            raise self.raises
        return self.bars


class TestItPutsHistoryWhereSizingLooksForIt:
    def test_a_fetch_writes_a_file_stock_gap_can_read(self, tmp_path):
        from catalyst.risk.stock_gap import worst_overnight_gap

        broker = FakeBroker()
        assert bh.ensure_history(broker, tmp_path, "AAPL", now=NOW) is True
        assert (tmp_path / "AAPL.csv").exists()
        # the real consumer must be able to read it
        assert worst_overnight_gap(tmp_path, "AAPL") is not None

    def test_the_columns_are_the_ones_stock_gap_expects(self, tmp_path):
        bh.ensure_history(FakeBroker(), tmp_path, "AAPL", now=NOW)
        with (tmp_path / "AAPL.csv").open() as f:
            header = next(csv.reader(f))
        assert header == ["date", "open", "high", "low", "close", "volume"]

    def test_it_asks_for_years_of_history_not_days(self, tmp_path):
        """A worst-case gap wants more than one market regime in it."""
        broker = FakeBroker()
        bh.ensure_history(broker, tmp_path, "AAPL", now=NOW)
        _sym, start, end = broker.calls[0]
        span = (datetime.fromisoformat(end) - datetime.fromisoformat(start))
        assert span.days > 900, f"only asked for {span.days} days"

    def test_it_does_not_ask_for_todays_partial_session(self, tmp_path):
        broker = FakeBroker()
        bh.ensure_history(broker, tmp_path, "AAPL", now=NOW)
        assert broker.calls[0][2] < NOW.date().isoformat()

    def test_a_cached_symbol_is_not_refetched(self, tmp_path):
        broker = FakeBroker()
        bh.ensure_history(broker, tmp_path, "AAPL", now=NOW)
        bh.ensure_history(broker, tmp_path, "AAPL", now=NOW)
        assert len(broker.calls) == 1, "re-fetched an already-cached symbol"


class TestFailureLeavesNoFileAtAll:
    """A partial file is worse than none: less history measures a
    smaller worst gap, and a smaller gap makes a BIGGER position."""

    def test_a_broker_error_writes_nothing(self, tmp_path):
        broker = FakeBroker(raises=RuntimeError("upstream is down"))
        assert bh.ensure_history(broker, tmp_path, "AAPL", now=NOW) is False
        assert not list(tmp_path.glob("*.csv"))

    def test_too_little_history_writes_nothing(self, tmp_path):
        """Under the sessions stock_gap needs, a file is just a slower
        route to the same fallback."""
        broker = FakeBroker(bars=make_bars(60))
        assert bh.ensure_history(broker, tmp_path, "AAPL", now=NOW) is False
        assert not list(tmp_path.glob("*.csv"))

    def test_an_empty_response_writes_nothing(self, tmp_path):
        assert bh.ensure_history(FakeBroker(bars=[]), tmp_path, "AAPL",
                                 now=NOW) is False
        assert not list(tmp_path.glob("*.csv"))

    @pytest.mark.parametrize("junk", [
        None, "not a list", [None], ["string"], [{}], [{"t": "x"}],
        [{"t": "2024-01-01", "o": "abc", "h": 1, "l": 1, "c": 1}],
    ])
    def test_malformed_payloads_never_raise(self, tmp_path, junk):
        assert bh.ensure_history(FakeBroker(bars=junk), tmp_path, "AAPL",
                                 now=NOW) is False

    def test_zero_and_negative_prices_are_dropped_not_written(self, tmp_path):
        """A zero close reads as a -100% gap, the largest possible
        measured worst case, which would shrink every position in that
        name to nothing - a silent refusal caused by bad data."""
        bars = make_bars(400)
        bars[10]["c"] = 0
        bars[11]["o"] = -5
        bh.ensure_history(FakeBroker(bars=bars), tmp_path, "AAPL", now=NOW)
        with (tmp_path / "AAPL.csv").open() as f:
            rows = list(csv.DictReader(f))
        assert all(float(r["close"]) > 0 and float(r["open"]) > 0
                   for r in rows)

    def test_no_half_written_file_is_left_behind(self, tmp_path):
        """Written to a temp name and moved into place."""
        bh.ensure_history(FakeBroker(), tmp_path, "AAPL", now=NOW)
        assert not list(tmp_path.glob("*.part")), "a partial file survived"


class TestHostileTickers:
    @pytest.mark.parametrize("bad", [
        "", "   ", None, "../../etc/passwd", "A/B", "A.B", "A B",
        "x" * 200, "SPY;DROP", "\x00",
    ])
    def test_they_are_refused_before_any_request(self, tmp_path, bad):
        broker = FakeBroker()
        assert bh.ensure_history(broker, tmp_path, bad, now=NOW) is False
        assert broker.calls == [], f"{bad!r} reached the broker"

    def test_nothing_is_written_outside_the_cache_directory(self, tmp_path):
        bh.ensure_history(FakeBroker(), tmp_path, "../escape", now=NOW)
        assert not list(tmp_path.parent.glob("*.csv"))


class TestStaleness:
    def test_an_old_file_is_refetched(self, tmp_path):
        broker = FakeBroker()
        bh.ensure_history(broker, tmp_path, "AAPL", now=NOW)
        later = NOW + timedelta(days=bh.MAX_CACHE_AGE_DAYS + 1)
        bh.ensure_history(broker, tmp_path, "AAPL", now=later)
        assert len(broker.calls) == 2

    def test_a_short_cached_file_is_refetched(self, tmp_path):
        """A file under the useful length must not be trusted just
        because it is recent."""
        (tmp_path / "AAPL.csv").write_text(
            "date,open,high,low,close,volume\n2024-01-01,1,1,1,1,1\n")
        broker = FakeBroker()
        bh.ensure_history(broker, tmp_path, "AAPL", now=NOW)
        assert broker.calls, "a too-short cache file was accepted as fresh"


class TestTheBrokerFollowsPaging:
    def test_every_page_is_collected(self):
        """A dropped page shortens the history, which measures a smaller
        worst gap, which makes a LARGER position - the one direction an
        error here must never take."""
        import httpx

        from catalyst.execution.broker import Broker

        pages = [
            {"bars": [{"t": "2024-01-01T00:00:00Z", "o": 1, "h": 1, "l": 1,
                       "c": 1, "v": 1}], "next_page_token": "p2"},
            {"bars": [{"t": "2024-01-02T00:00:00Z", "o": 2, "h": 2, "l": 2,
                       "c": 2, "v": 1}], "next_page_token": None},
        ]
        seen = []

        def handler(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=pages[len(seen) - 1])

        b = Broker("k", "s", transport=httpx.MockTransport(handler))
        bars = b.get_daily_bars("AAPL", "2024-01-01", "2024-01-02")
        b.close()
        assert len(bars) == 2, "paging was not followed - history truncated"
        assert seen[1].get("page_token") == "p2"

    def test_it_cannot_loop_forever_on_a_repeating_token(self):
        import httpx

        from catalyst.execution.broker import Broker

        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={
                "bars": [{"t": "2024-01-01T00:00:00Z", "o": 1, "h": 1,
                          "l": 1, "c": 1, "v": 1}],
                "next_page_token": "always-the-same"})

        b = Broker("k", "s", transport=httpx.MockTransport(handler))
        b.get_daily_bars("AAPL", "2024-01-01", "2024-01-02")
        b.close()
        assert len(calls) <= 6, "unbounded paging loop"


class TestItReachesSizingForReal:
    def test_a_fetched_history_changes_the_position_size(self, tmp_path):
        """End to end: fetch, then size, and the number moves."""
        from decimal import Decimal

        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.hard_bounds import HARD_BOUNDS
        from catalyst.risk.sizing import size

        params = {"stop_width": {"fda_decision": Decimal("0.50")},
                  "adverse_gap_assumption": {"fda_decision": Decimal("0.60")}}
        portfolio = PortfolioState(
            equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("2000"), consecutive_losses=0,
            as_of=NOW, reliable=True)
        market = MarketSnapshot(ticker="CALM", last_close=Decimal("50"),
                                half_spread_bp=Decimal("5"),
                                median_daily_dollar_volume=Decimal("0"))

        before = size(True, "fda_decision", portfolio, params, HARD_BOUNDS,
                      market, bars_dir=str(tmp_path))
        bh.ensure_history(FakeBroker(), tmp_path, "CALM", now=NOW)
        after = size(True, "fda_decision", portfolio, params, HARD_BOUNDS,
                     market, bars_dir=str(tmp_path))

        assert after.notional_usd > before.notional_usd, (
            f"fetching the history changed nothing: {before.notional_usd} "
            f"-> {after.notional_usd}")


@pytest.fixture
def frozen_cycle_clock(monkeypatch):
    """kill_switches.check() measures snapshot age against the WALL
    clock, not the clock injected into run_cycle, so a test pinned to a
    fixed NOW stands the whole cycle down as `portfolio_state_stale` the
    moment real time passes it. test_stress_stage5 freezes it with an
    autouse fixture; that fixture does not reach this module, so the
    same freeze is applied here deliberately.

    (Worth recording: without this the cycle never reaches the risk
    engine, so a test asserting the fetch happened fails for a reason
    that has nothing to do with the fetch.)
    """
    import catalyst.risk.kill_switches as kill_switches
    import tests.test_stress_stage5 as s5

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return s5.NOW

    monkeypatch.setattr(kill_switches, "datetime", _Frozen)


class TestTheCycleActuallyCallsIt:
    """The module being correct is not the same as it being reached.

    The whole defect this fixes was a correct, tested mechanism that
    nothing invoked. Testing the module alone would repeat exactly that
    mistake one layer up.
    """

    def test_run_cycle_fetches_history_for_each_researched_candidate(
            self, monkeypatch, tmp_path, frozen_cycle_clock):
        from catalyst.orchestrator import cycle as cyc

        asked = []

        def spy(broker, cache_dir, ticker, **kw):
            asked.append(str(ticker))
            return False

        monkeypatch.setattr(cyc, "ensure_history", spy)

        import tests.test_stress_stage5 as s5

        conn = __import__("sqlite3").connect(str(tmp_path / "c.db"))
        root = __import__("pathlib").Path(__file__).resolve().parents[1]
        conn.executescript((root / "catalyst/storage/schema.sql").read_text())
        broker, _state = s5.broker_for()
        s5.run(conn, broker, s5.model_transport(), [s5.candidate()],
               bars_dir=str(tmp_path))
        conn.close()

        assert "TEST" in asked, (
            "the cycle never asked for the candidate's price history, so "
            "per-stock sizing falls back to the category for every name")

    def test_it_is_skipped_when_no_bars_dir_is_configured(
            self, monkeypatch, tmp_path, frozen_cycle_clock):
        """Every existing caller must behave exactly as before."""
        from catalyst.orchestrator import cycle as cyc

        asked = []
        monkeypatch.setattr(
            cyc, "ensure_history",
            lambda *a, **k: asked.append(1) or False)

        import tests.test_stress_stage5 as s5

        conn = __import__("sqlite3").connect(str(tmp_path / "c.db"))
        root = __import__("pathlib").Path(__file__).resolve().parents[1]
        conn.executescript((root / "catalyst/storage/schema.sql").read_text())
        broker, _state = s5.broker_for()
        s5.run(conn, broker, s5.model_transport(), [s5.candidate()])
        conn.close()
        assert asked == []


class TestNaiveDatetimesDoNotCrashTheLoop:
    """Found by stress, not by reading. A naive `now` does not fail on
    the way in - it raises on the SUBTRACTION, inside the trading loop:
    "can't subtract offset-naive and offset-aware datetimes". The same
    shape as the defect cycle.py already records for a feed that dropped
    its trailing Z.

    Every caller in this repo passes an aware datetime, which is
    precisely why the first one that does not would be a live failure.
    """

    def test_is_fresh_survives_a_naive_clock(self, tmp_path):
        (tmp_path / "AAPL.csv").write_text(
            "date,open,high,low,close,volume\n"
            + "2024-01-01,1,1,1,1,1\n" * 400)
        # datetime inside the European DST gap, deliberately
        assert bh.is_fresh(tmp_path, "AAPL",
                           now=datetime(2026, 3, 29, 2, 30)) in (True, False)

    def test_ensure_history_survives_a_naive_clock(self, tmp_path):
        broker = FakeBroker()
        assert bh.ensure_history(broker, tmp_path, "AAPL",
                                 now=datetime(2026, 3, 29, 2, 30)) is True
