"""The lag diagnostic must survive the line being drawn to the edge.

WHY THIS FILE EXISTS. tests/test_spy_lag.py builds a FakePerf and sets
`spy_lag_days` by hand, so it tests the NOTE and never the number the
note depends on. When SPY's line was carried forward to the chart's
right-hand edge, `spy_lag_days` was still measured against the last
point in the series - which was now the held one - so it became zero on
every render and the entire diagnostic switched itself off: the weekend
note, the failing-refresh alarm and the rebuild offer with it. Every
test still passed.

Nothing here fakes the middle. It writes bars and rows, calls
`queries.performance`, and asserts on what a reader would actually see.

Fully offline: bars are written to a temporary cache, never fetched.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from catalyst.storage import init_db

#: HOUSE RULE 6 does not apply to the fixture dates: they are written
#: into the cache AND are what the code measures against. `performance`
#: does read today to extend the lines rightwards, which only widens the
#: lag these tests assert on - it can never close it.
LAST_CLOSE = date(2026, 8, 21)


@pytest.fixture
def build(tmp_path, monkeypatch):
    def _build(last_bar=LAST_CLOSE, baseline=date(2026, 8, 17)):
        db = str(tmp_path / f"t{last_bar}.db")
        conn = init_db(db)
        conn.execute(
            "INSERT INTO benchmark_baselines (id, capital_cents, start_date, "
            " source, account_fingerprint, reason, set_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("b1", "200000", baseline.isoformat(), "first_run", "acct",
             "first run",
             datetime.combine(baseline, datetime.min.time(),
                              timezone.utc).isoformat()))
        conn.execute(
            "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
            ("c1", "{}", "claude-sonnet-5", "scheduled", "research", "100",
             f"{baseline}T12:00:00+00:00", None))
        conn.commit()
        conn.close()

        bars = tmp_path / f"bars{last_bar}"
        bars.mkdir()
        rows = ["date,open,high,low,close,volume"]
        d, px = date(2026, 8, 3), 600.0
        while d <= last_bar:
            if d.weekday() < 5:
                rows.append(f"{d},{px},{px},{px},{px},1000000")
                px -= 1.0
            d += timedelta(days=1)
        (bars / "SPY.csv").write_text("\n".join(rows) + "\n")
        (bars / "SPY.meta.json").write_text(
            '{"feed":"sip","adjustment":"all"}')

        monkeypatch.setenv("CATALYST_DB", db)
        monkeypatch.setenv("CATALYST_BARS", str(bars))
        import catalyst.backtest.data as bd
        bd.BarCache._mem = {}
        return queries.performance(Db(db))

    return _build


class TestTheLagIsMeasuredAgainstARealClose:
    def test_a_stale_cache_still_reports_a_lag(self, build):
        """THE REGRESSION. Held-forward points made this zero, and a
        zero lag silences every explanation on the panel."""
        perf = build()
        assert perf.spy_lag_days > 0, (
            "SPY has no close since 2026-08-21 and the page thinks it is "
            "current - the whole lag diagnostic is switched off")

    def test_the_note_actually_renders(self, build):
        perf = build()
        assert panels._spy_lag_note(perf, "perf") != "", (
            "no explanation is drawn beside a benchmark days behind the "
            "bot's own line")

    def test_the_note_names_the_real_close_not_the_drawn_edge(self, build):
        perf = build()
        html = panels._spy_lag_note(perf, "perf")
        assert str(LAST_CLOSE) in html
        assert str(perf.end_day) not in html, (
            "the note quotes the day the line is drawn to, which is not a "
            "day SPY closed on")

    def test_it_does_not_claim_the_line_ends_there(self, build):
        """Both lines reach the same edge now, so wording about the line
        stopping describes a picture nobody is looking at."""
        html = panels._spy_lag_note(build(), "perf")
        assert "line ends on" not in html
        assert "line stops on" not in html


class TestTheHeldPointsAreNotMistakenForPrices:
    def test_the_last_close_day_skips_the_held_point(self, build):
        perf = build()
        assert queries.last_real_close_day(perf.spy_points) == LAST_CLOSE
        assert perf.spy_points[-1][0] > LAST_CLOSE, (
            "this test is pointless unless a held point is really there")

    def test_the_drawn_line_still_reaches_the_edge(self, build):
        perf = build()
        assert perf.spy_points[-1][0] == perf.bot_points[-1][0]

    def test_every_real_point_is_flagged_as_real(self, build):
        perf = build()
        real = [p for p in perf.spy_points if len(p) > 3 and not p[3]]
        assert real, "no point is marked as a measured close"
        assert all(p[0] <= LAST_CLOSE for p in real)


class TestTheCheckCanFail:
    """House rule 4, aimed at the exact mistake that shipped."""

    def test_measuring_to_the_last_point_would_read_as_current(self, build):
        perf = build()
        naive = max((perf.end_day - perf.spy_points[-1][0]).days, 0)
        assert naive == 0, (
            "the pre-fix arithmetic no longer produces the bug, so this "
            "test no longer proves anything - re-derive it")
        assert perf.spy_lag_days != naive, (
            "spy_lag_days is being measured the broken way")
