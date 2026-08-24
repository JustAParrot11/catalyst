"""The SPY comparison must come back on its own.

OWNER-ASKED 2026-08-24: "how do i fix it then? ... can we sort it so its
got it historical and ready for the future."

THE TRAP. refresh_benchmark pins the feed on purpose - a series half
consolidated tape and half one exchange's prints makes every comparison
against it quietly wrong. But a cache built on `sip` keeps asking for
`sip`, and a key without that subscription is refused every time,
forever. The owner's 2026-08-24 log has sixteen of exactly that:

    {"message":"subscription does not permit querying recent SIP data"}

Waiting cannot fix it, so a page that says "wait" is wrong, and a button
the owner has to find and press is a benchmark that stays dead until
somebody notices.

WHY REBUILDING IS SAFE HERE. The pin is about MIXING, and a rebuild does
not mix: it discards the series and refetches the whole thing on one
basis. So the only thing the manual gate really protected was the
decision to throw history away - and against a benchmark that can never
update again, that trade makes itself.

EVIDENCE, NOT A GUESS. It fires only when the refusals span two distinct
days with no success in between, and at most once a day.

Fully offline: no clock, no socket, every dependency injected.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.data.benchmark import RefreshResult
from catalyst.orchestrator import scheduler
from catalyst.orchestrator.scheduler import (
    FEED_REFUSED_DAYS_BEFORE_REBUILD, _maybe_rebuild_refused_feed,
)

SCHEMA = Path(__file__).resolve().parents[1] / "catalyst" / "storage" / "schema.sql"

#: HOUSE RULE 6 does not apply: TODAY is passed in as an argument and
#: every stored timestamp is written relative to it, so nothing here is
#: measured against the wall clock.
TODAY = date(2026, 8, 24)

REFUSED = RefreshResult(
    skipped_reason="feed_no_longer_available_sip", routine=False,
    raw_response='{"message":"subscription does not permit querying recent '
                 'SIP data"}')


class Creds:
    alpaca_key = "k"
    alpaca_secret = "s"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "t.db")
    c.executescript(SCHEMA.read_text())
    yield c
    c.close()


def refusals_on(conn, days, outcome="feed_no_longer_available_sip"):
    """One recorded refusal per named day."""
    for d in days:
        conn.execute(
            "INSERT OR REPLACE INTO benchmark_refreshes "
            "(checked_at, outcome, routine, bars_written, last_bar_day, "
            " feed, raw_response) VALUES (?,?,?,?,?,?,?)",
            (datetime.combine(d, datetime.min.time(),
                              timezone.utc).isoformat(),
             outcome, 0, 0, "2026-08-21", "sip", "refused"))
    conn.commit()


def success_on(conn, day):
    conn.execute(
        "INSERT OR REPLACE INTO benchmark_refreshes "
        "(checked_at, outcome, routine, bars_written, last_bar_day, feed, "
        " raw_response) VALUES (?,?,?,?,?,?,?)",
        (datetime.combine(day, datetime.min.time(),
                          timezone.utc).isoformat(),
         "updated", 1, 3, day.isoformat(), "sip", None))
    conn.commit()


@pytest.fixture
def rebuilds(monkeypatch):
    """Capture rebuild_benchmark rather than letting it touch a disk."""
    class Recorder(list):
        """A list of rebuild calls, with the result it should return
        beside it so a test can change the outcome."""

        outcome = RefreshResult(written=2600, last_day=date(2026, 8, 21),
                                feed="iex", routine=True)

    calls = Recorder()

    def fake(bars_root, key, secret, **kw):
        calls.append(bars_root)
        return calls.outcome

    from catalyst.data import benchmark
    monkeypatch.setattr(benchmark, "rebuild_benchmark", fake)
    monkeypatch.setattr("catalyst.dashboard.db.bars_path", lambda: "/tmp/bars")
    return calls


class TestItRebuildsOnlyOnRealEvidence:
    def test_two_days_of_refusals_trigger_a_rebuild(self, conn, rebuilds):
        refusals_on(conn, [TODAY - timedelta(days=1), TODAY])
        state = {}
        _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), state, TODAY)
        assert len(rebuilds) == 1, (
            "a feed that has refused every attempt for two days can never "
            "update again; waiting is not a plan")

    def test_one_day_is_not_enough(self, conn, rebuilds):
        refusals_on(conn, [TODAY])
        _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), {}, TODAY)
        assert rebuilds == [], (
            "one bad afternoon must not discard a real series")

    def test_a_success_in_the_window_means_it_was_an_outage(self, conn,
                                                            rebuilds):
        refusals_on(conn, [TODAY - timedelta(days=1), TODAY])
        success_on(conn, TODAY - timedelta(days=3))
        _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), {}, TODAY)
        assert rebuilds == [], (
            "the feed worked inside the window, so this is an outage that "
            "will pass, not an entitlement that has gone")

    def test_a_different_kind_of_failure_never_rebuilds(self, conn, rebuilds):
        """A timeout is not a refused feed. Discarding a series over a
        flaky network would be the worse defect."""
        refusals_on(conn, [TODAY - timedelta(days=1), TODAY],
                    outcome="fetch_failed_ReadTimeout")
        transient = RefreshResult(skipped_reason="fetch_failed_ReadTimeout",
                                  routine=False)
        _maybe_rebuild_refused_feed(conn, None, transient, Creds(), {}, TODAY)
        assert rebuilds == []

    def test_it_tries_at_most_once_a_day(self, conn, rebuilds):
        refusals_on(conn, [TODAY - timedelta(days=1), TODAY])
        state = {}
        for _ in range(5):
            _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), state,
                                        TODAY)
        assert len(rebuilds) == 1, (
            "the cycle runs every fifteen minutes; a rebuild per cycle "
            "would refetch a decade of bars 96 times a day")


class TestItSaysWhatItDid:
    def test_a_successful_rebuild_marks_the_day_done(self, conn, rebuilds):
        refusals_on(conn, [TODAY - timedelta(days=1), TODAY])
        state = {}
        _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), state, TODAY)
        assert state["benchmark_day"] == TODAY

    def test_the_rebuild_outcome_is_recorded(self, conn, rebuilds):
        refusals_on(conn, [TODAY - timedelta(days=1), TODAY])
        _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), {}, TODAY)
        row = conn.execute(
            "SELECT outcome, feed, bars_written FROM benchmark_refreshes "
            "ORDER BY checked_at DESC LIMIT 1").fetchone()
        assert row[0] == "updated" and row[1] == "iex"

    def test_a_rebuild_that_also_fails_does_not_mark_the_day_done(
            self, conn, rebuilds):
        rebuilds.outcome = RefreshResult(
            skipped_reason="feeds_refused_http_403", routine=False,
            raw_response="forbidden")
        refusals_on(conn, [TODAY - timedelta(days=1), TODAY])
        state = {}
        _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), state, TODAY)
        assert "benchmark_day" not in state, (
            "nothing was recovered; the next day must try again")

    def test_a_raising_rebuild_never_reaches_the_trading_loop(self, conn,
                                                             monkeypatch):
        from catalyst.data import benchmark

        def boom(*a, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(benchmark, "rebuild_benchmark", boom)
        monkeypatch.setattr("catalyst.dashboard.db.bars_path",
                            lambda: "/tmp/bars")
        refusals_on(conn, [TODAY - timedelta(days=1), TODAY])
        _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), {}, TODAY)


class TestTheThresholdIsWhatItSaysItIs:
    def test_the_constant_matches_the_behaviour(self, conn, rebuilds):
        days = [TODAY - timedelta(days=i)
                for i in range(FEED_REFUSED_DAYS_BEFORE_REBUILD)]
        refusals_on(conn, days)
        _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), {}, TODAY)
        assert len(rebuilds) == 1

    def test_one_day_short_does_nothing(self, conn, rebuilds):
        days = [TODAY - timedelta(days=i)
                for i in range(FEED_REFUSED_DAYS_BEFORE_REBUILD - 1)]
        refusals_on(conn, days)
        _maybe_rebuild_refused_feed(conn, None, REFUSED, Creds(), {}, TODAY)
        assert rebuilds == []
