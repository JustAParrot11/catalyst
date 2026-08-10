"""The benchmark series must maintain itself, or the comparison rots.

Found 2026-08-10 while answering an owner question about when the SPY
line would appear. Two separate defects, both silent:

1. `data/` is gitignored and NO bar file is tracked, so a fresh clone -
   which is exactly what the installer makes on the VPS - has no SPY
   history at all. The dashboard's headline comparison had nothing to
   draw against and said only that its cache was missing.
2. Nothing in the RUNNING bot ever writes to the bar cache. Even where a
   cache existed (a dev machine that had run scripts/fetch_history.py),
   it froze on the day it was fetched while the bot's own line kept
   advancing - so the two lines silently drifted out of a shared window.

The brief requires comparison against the S&P net of costs. A benchmark
that stops updating fails that requirement quietly, which is the worst
way to fail it.

Sabotage log (house rule 4) is at the bottom of this file.
"""

from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from catalyst.backtest.data import Bar, BarCache
from catalyst.data.benchmark import (
    BENCHMARK_SYMBOL, refresh_benchmark,
)


def bar(day: date, close: float) -> Bar:
    d = Decimal(str(close))
    return Bar(day=day, open=d, high=d, low=d, close=d, volume=Decimal("1000"))


def seeded_cache(root, days: list[date]) -> BarCache:
    cache = BarCache(root)
    cache.write_bars(BENCHMARK_SYMBOL,
                     [bar(d, 500 + i) for i, d in enumerate(days)])
    return cache


def client_returning(payload, status=200, capture=None):
    """An httpx.Client whose GET returns a canned Alpaca bars response."""
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(dict(httpx.QueryParams(request.url.query)))
        if status != 200:
            return httpx.Response(status, text="entitlement required",
                                  request=request)
        return httpx.Response(200, json=payload, request=request)
    return httpx.Client(transport=httpx.MockTransport(handler))


def bars_payload(days: list[date], start_close=600.0):
    return {"bars": {BENCHMARK_SYMBOL: [
        {"t": f"{d.isoformat()}T04:00:00Z", "o": start_close + i,
         "h": start_close + i, "l": start_close + i, "c": start_close + i,
         "v": 1000}
        for i, d in enumerate(days)
    ]}, "next_page_token": None}


class TestRefresh:
    def test_bootstraps_a_completely_missing_cache(self, tmp_path):
        """The fresh-clone case: data/ is gitignored, so the VPS has no
        SPY file at all. The bot must build one rather than render a
        permanently empty benchmark."""
        days = [date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7)]
        seen = []
        result = refresh_benchmark(
            str(tmp_path), "k", "s", today=date(2026, 8, 10),
            client_factory=lambda h: client_returning(bars_payload(days),
                                                      capture=seen))
        assert result.written == 3
        assert result.skipped_reason is None
        cached = BarCache(str(tmp_path)).load_bars(BENCHMARK_SYMBOL)
        assert [b.day for b in cached] == days
        # bootstrapped from the documented SIP floor, not from "today"
        assert seen[0]["start"] == "2016-01-04"

    def test_appends_only_the_missing_days(self, tmp_path):
        have = [date(2026, 8, 3), date(2026, 8, 4)]
        seeded_cache(tmp_path, have)
        new = [date(2026, 8, 5), date(2026, 8, 6)]
        seen = []
        result = refresh_benchmark(
            str(tmp_path), "k", "s", today=date(2026, 8, 7),
            client_factory=lambda h: client_returning(bars_payload(new),
                                                      capture=seen))
        cached = BarCache(str(tmp_path)).load_bars(BENCHMARK_SYMBOL)
        assert [b.day for b in cached] == have + new
        # the existing history is untouched, not refetched and rewritten
        assert cached[0].close == Decimal("500")
        assert seen[0]["start"] == "2026-08-05"   # day after the last cached
        assert result.written == 2

    def test_is_a_no_op_when_already_current(self, tmp_path):
        """No request at all on the many cycles a day when nothing is
        missing - the refresher runs on a schedule, not on demand."""
        seeded_cache(tmp_path, [date(2026, 8, 6), date(2026, 8, 7)])

        def explode(_headers):
            raise AssertionError("fetched despite the cache being current")

        result = refresh_benchmark(str(tmp_path), "k", "s",
                                   today=date(2026, 8, 8),
                                   client_factory=explode)
        assert result.written == 0
        assert result.skipped_reason == "already_current"

    def test_no_credentials_skips_without_touching_the_cache(self, tmp_path):
        seeded_cache(tmp_path, [date(2026, 8, 6)])

        def explode(_headers):
            raise AssertionError("fetched without credentials")

        result = refresh_benchmark(str(tmp_path), "", "", today=date(2026, 8, 9),
                                   client_factory=explode)
        assert result.skipped_reason == "no_alpaca_credentials"
        assert result.written == 0

    def test_http_failure_is_reported_with_its_body_and_never_raises(
            self, tmp_path):
        """A paper account without SIP entitlement 403s here. That must
        arrive as a stated reason carrying the raw body, not as an
        exception that kills the trading cycle, and not as a silent
        empty benchmark."""
        seeded_cache(tmp_path, [date(2026, 8, 6)])
        result = refresh_benchmark(
            str(tmp_path), "k", "s", today=date(2026, 8, 9),
            client_factory=lambda h: client_returning(None, status=403))
        assert result.written == 0
        assert "403" in (result.skipped_reason or "")
        assert "entitlement required" in (result.raw_response or "")
        # the cache the bot already had is intact
        assert len(BarCache(str(tmp_path)).load_bars(BENCHMARK_SYMBOL)) == 1

    def test_an_empty_response_does_not_wipe_the_cache(self, tmp_path):
        """A weekend, a holiday, or a broken query all return zero bars.
        None of them is a reason to lose ten years of history."""
        seeded_cache(tmp_path, [date(2026, 8, 6), date(2026, 8, 7)])
        result = refresh_benchmark(
            str(tmp_path), "k", "s", today=date(2026, 8, 9),
            client_factory=lambda h: client_returning(
                {"bars": {}, "next_page_token": None}))
        assert result.written == 0
        assert result.skipped_reason == "no_new_bars_upstream"
        assert len(BarCache(str(tmp_path)).load_bars(BENCHMARK_SYMBOL)) == 2

    def test_records_the_basis_it_fetched_on(self, tmp_path):
        """Mixing an adjustment=raw fetch into an adjustment=all series
        would silently corrupt a total-return benchmark (raw understates
        SPY by ~67pp over the cached window). The basis is pinned in the
        request AND recorded in the cache metadata."""
        seen = []
        refresh_benchmark(
            str(tmp_path), "k", "s", today=date(2026, 8, 10),
            client_factory=lambda h: client_returning(
                bars_payload([date(2026, 8, 7)]), capture=seen))
        assert seen[0]["adjustment"] == "all"
        assert seen[0]["feed"] == "sip"
        meta = BarCache(str(tmp_path)).read_meta() or {}
        assert meta.get("adjustment") == "all"
        assert meta.get("feed") == "sip"
        assert meta.get("fetched_at")

    def test_never_asks_for_days_beyond_today(self, tmp_path):
        seeded_cache(tmp_path, [date(2026, 8, 1)])
        seen = []
        refresh_benchmark(
            str(tmp_path), "k", "s", today=date(2026, 8, 10),
            client_factory=lambda h: client_returning(
                bars_payload([date(2026, 8, 4)]), capture=seen))
        assert seen[0]["end"] <= "2026-08-10"


class TestSchedulerWiring:
    def test_refresh_runs_at_most_once_a_day_and_never_kills_the_cycle(
            self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        import catalyst.data.benchmark as bench
        from catalyst.orchestrator import scheduler
        import catalyst.setup.credentials as creds_mod

        monkeypatch.setattr(
            creds_mod, "load_credentials",
            lambda *a, **k: SimpleNamespace(alpaca_key="k", alpaca_secret="s"))
        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
        calls = []

        def fake_refresh(*a, **kw):
            calls.append(1)
            raise RuntimeError("upstream on fire")   # must not escape

        monkeypatch.setattr(bench, "refresh_benchmark", fake_refresh)
        state = {}
        scheduler._maybe_refresh_benchmark(state)   # never raises
        scheduler._maybe_refresh_benchmark(state)
        assert len(calls) == 1, "second call in the same day must be skipped"


@pytest.mark.sabotage
class TestSabotage:
    """Each check below was verified by breaking a copy:

    - append changed to a full overwrite of the file with only the new
      bars: caught by test_appends_only_the_missing_days (history gone).
    - the empty-response guard removed so `bars: {}` wrote an empty
      list: caught by test_an_empty_response_does_not_wipe_the_cache.
    - adjustment switched to "raw": caught by
      test_records_the_basis_it_fetched_on.
    """

    def test_cache_write_is_a_merge_not_a_replace(self, tmp_path):
        seeded_cache(tmp_path, [date(2026, 8, 3)])
        refresh_benchmark(
            str(tmp_path), "k", "s", today=date(2026, 8, 6),
            client_factory=lambda h: client_returning(
                bars_payload([date(2026, 8, 4), date(2026, 8, 5)])))
        days = [b.day for b in BarCache(str(tmp_path)).load_bars(BENCHMARK_SYMBOL)]
        assert date(2026, 8, 3) in days, "the merge dropped existing history"
        assert len(days) == 3
