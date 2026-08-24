"""The SPY comparison must be able to come back.

OWNER-REPORTED 2026-08-20: "the SPY comparison line has disappeared so i
cant visually see if we're beating SPY" - days after replacing the
Alpaca keys to move the account from $1,000 to $2,000.

THE MECHANISM. refresh_benchmark pins the feed to whatever the cache was
built on, and that pin is right: a series half consolidated tape and
half one exchange's prints makes every comparison against it quietly
wrong. But a cache built on `sip` keeps asking for `sip`, and a new key
without that subscription is refused every time - so the comparison dies
and no amount of waiting revives it.

Correct rule, missing door. These tests hold both halves: the pin still
holds against an ordinary outage, and a deliberate rebuild can move the
series to a feed the current credentials can actually reach.
"""

import httpx
import pytest

from catalyst.data.benchmark import (
    BENCHMARK_SYMBOL, rebuild_benchmark, refresh_benchmark,
)
from catalyst.backtest.data import BarCache


class Boom(Exception):
    def __init__(self, status, text="no subscription"):
        super().__init__(text)
        self.response = httpx.Response(
            status, text=text, request=httpx.Request("GET", "https://x"))


def client_that(fail_on=None, status=403):
    """A fake Alpaca client: refuses `fail_on` feed, serves others."""
    class C:
        def __init__(self, headers=None):
            self.asked = []

        def get(self, url, params=None, **kw):
            feed = (params or {}).get("feed")
            self.asked.append(feed)
            if fail_on and feed == fail_on:
                raise Boom(status)
            return httpx.Response(200, json={"bars": {BENCHMARK_SYMBOL: [
                {"t": "2026-08-18T00:00:00Z", "o": 1, "h": 1, "l": 1,
                 "c": 500.0, "v": 10}]}, "next_page_token": None},
                request=httpx.Request("GET", url))

        def close(self):
            pass
    return C


def seeded(tmp_path, feed):
    cache = BarCache(str(tmp_path))
    cache.write_meta({"symbol": BENCHMARK_SYMBOL, "feed": feed,
                      "adjustment": "all", "rows": 0})
    return str(tmp_path)


class TestThePinStillHolds:
    def test_an_ordinary_outage_does_not_switch_feed(self, tmp_path):
        """A flaky upstream must never quietly re-base the series."""
        root = seeded(tmp_path, "sip")
        r = refresh_benchmark(root, "k", "s",
                              client_factory=client_that("sip", status=500))
        assert r.skipped_reason.startswith("fetch_failed_http_500")
        assert (BarCache(root).read_meta() or {}).get("feed") == "sip"


class TestALostSubscriptionIsNamed:
    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refused_pinned_feed_says_so_specifically(self, tmp_path,
                                                        status):
        """It will never fix itself, so it must not be reported as if it
        might. 'fetch_failed_http_403' reads as an outage; this reads as
        a decision waiting to be made."""
        root = seeded(tmp_path, "sip")
        r = refresh_benchmark(root, "k", "s",
                              client_factory=client_that("sip", status))
        assert r.skipped_reason == "feed_no_longer_available_sip"
        assert "no subscription" in (r.raw_response or "")

    def test_the_raw_upstream_body_is_kept(self, tmp_path):
        """House rule 3."""
        root = seeded(tmp_path, "sip")
        r = refresh_benchmark(root, "k", "s",
                              client_factory=client_that("sip", 403))
        assert r.raw_response


class TestTheRebuildIsTheDoorOut:
    def test_it_moves_the_series_to_a_reachable_feed(self, tmp_path):
        root = seeded(tmp_path, "sip")
        r = rebuild_benchmark(root, "k", "s",
                              client_factory=client_that("sip", 403))
        assert r.skipped_reason in (None, "already_current"), r.skipped_reason
        assert r.feed and r.feed != "sip"
        assert (BarCache(root).read_meta() or {}).get("feed") == r.feed

    def test_it_says_which_feed_it_landed_on(self, tmp_path):
        """The comparison is only honest if the page can name its basis."""
        root = seeded(tmp_path, "sip")
        r = rebuild_benchmark(root, "k", "s",
                              client_factory=client_that("sip", 403))
        assert r.feed in ("iex", "sip", "boats", "otc") or r.feed

    def test_it_does_not_splice_the_old_series_onto_the_new_feed(self,
                                                                tmp_path):
        """The whole reason the pin exists. A rebuild must DISCARD, not
        merge, or it produces exactly the mixed-basis series the pin was
        protecting against."""
        from datetime import date

        from catalyst.backtest.data import Bar

        root = seeded(tmp_path, "sip")
        cache = BarCache(root)
        cache.write_bars(BENCHMARK_SYMBOL, [
            Bar(day=date(2020, 1, 2), open=1, high=1, low=1, close=1,
                volume=1)])
        rebuild_benchmark(root, "k", "s",
                          client_factory=client_that("sip", 403))
        days = [b.day for b in cache.load_bars(BENCHMARK_SYMBOL)]
        assert date(2020, 1, 2) not in days, (
            "a bar from the old feed survived the rebuild, so the series "
            "is now half one basis and half another")

    def test_it_never_runs_without_evidence_that_waiting_cannot_help(self):
        """THIS TEST USED TO SAY "nothing calls it on a schedule".

        That was the right call while the alternative was a human
        pressing a button. It stopped being the right call on
        2026-08-24, when the owner asked for the opposite in as many
        words - "can we sort it so its got it historical and ready for
        the future" - against a feed that had refused sixteen times and
        would refuse forever.

        A button nobody finds is a benchmark that stays dead. So the
        scheduler may now rebuild, and what this guards is the thing the
        old rule was really protecting: that it never happens casually.
        The conditions are asserted directly in
        tests/test_spy_self_heals.py; what is checked here is that the
        scheduler's path to it still goes through those conditions
        rather than calling it outright.
        """
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler)
        calls = [ln.strip() for ln in src.splitlines()
                 if "rebuild_benchmark(" in ln and "def " not in ln]
        assert len(calls) == 1, (
            f"the destructive rebuild is reachable from {len(calls)} places "
            "in the scheduler; it must have exactly one guarded caller")

        guarded = inspect.getsource(scheduler._maybe_rebuild_refused_feed)
        assert "rebuild_benchmark(" in guarded, (
            "the one caller is not the guarded one")
        for condition in ("FEED_REFUSED_DAYS_BEFORE_REBUILD",
                          "_REFUSAL_MARKERS",
                          "benchmark_rebuild_day"):
            assert condition in guarded, (
                f"the rebuild no longer checks {condition}: a transient "
                "outage, a wrong kind of failure, or a per-cycle loop can "
                "now throw away real history")


# ==========================================================================
# The button. Owner-asked for nothing to be left outstanding, and an
# escape hatch nothing can reach is not an escape hatch.
# ==========================================================================


def _perf(spy_error):
    """A Performance carrying just the field the offer reads."""
    from catalyst.dashboard.db import QueryResult
    from catalyst.dashboard.queries import Performance

    empty = QueryResult("", (), [], None)
    return Performance(closed_q=empty, costs_q=empty, spy_stale=True,
                       spy_error=spy_error)


class TestTheOwnerCanActuallyReachTheRebuild:
    def test_the_offer_appears_only_for_a_refused_feed(self):
        """A generic outage gets no button, because for that one waiting
        IS the answer. Offering a destructive rebuild for a blip trains
        the owner to reach for it."""
        from catalyst.dashboard import panels
        from catalyst.dashboard.queries import Performance

        refused = _perf("feed_no_longer_available_sip")
        outage = _perf("fetch_failed_http_500")
        assert "rebuild-benchmark" in panels._spy_rebuild_offer(refused, "p")
        assert panels._spy_rebuild_offer(outage, "p") == ""

    def test_it_demands_a_typed_confirmation(self):
        from catalyst.dashboard.server import rebuild_spy_series

        okay, message = rebuild_spy_series("")
        assert not okay and "REBUILD" in message
        okay, message = rebuild_spy_series("yes")
        assert not okay

    def test_the_route_exists(self):
        from catalyst.dashboard import server

        assert "/rebuild-benchmark" in (
            open(server.__file__).read())

    def test_the_form_says_what_will_be_destroyed(self):
        """It discards real history. Someone clicking it must know that
        from the page, not from the source."""
        from catalyst.dashboard import panels

        html = panels._spy_rebuild_offer(
            _perf("feed_no_longer_available_sip"), "p")
        assert "discards the stored series" in html
        assert "will not recover by itself" in html
