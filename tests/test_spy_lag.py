"""Why the SPY line stops before the bot's line does.

OWNER-REPORTED 2026-08-24: "its stopped tracking SPY" - a performance
chart whose blue line ran to the 24th and whose red one ended on the
21st. The 24th was a Monday. Friday's close was the newest bar there
was, and the refresher only ever asks up to yesterday, so the picture
was correct and said nothing at all about being correct.

A dead feed draws the identical picture. The existing alarms could not
help: they fire only when the window holds NO SPY data, which is a
different situation with a different shape.

CLAUDE.md: routine attrition must not look like damage - "a working bot
reading as a broken one has cost real debugging time twice". These hold
the two halves apart:

  - the gap is a closed market      -> a quiet note, no alarm;
  - the bot's own refresh failed    -> an alarm, with the raw upstream.

The second is read from benchmark_refreshes, which the refresh writes
now instead of only logging. It is a FACT about what happened, not an
inference from the shape of the gap.

Fully offline.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.data.benchmark import RefreshResult, _weekdays_between
from catalyst.dashboard.panels import SPY_LAG_ROUTINE_DAYS, _spy_lag_note
from catalyst.orchestrator.scheduler import record_benchmark_refresh

SCHEMA = Path(__file__).resolve().parents[1] / "catalyst" / "storage" / "schema.sql"


class FakePerf:
    """Only the fields _spy_lag_note reads. Built by hand so a test says
    exactly which situation it is describing."""

    def __init__(self, *, last_spy, end_day, outcome=None, routine=None,
                 at="2026-08-24T06:00:00+00:00", raw="",
                 failure=None, failure_at="2026-08-24T00:12:01+00:00",
                 failure_raw="", failures=0):
        self.spy_points = [(last_spy, 100.0, 200000)]
        self.spy_lag_days = max((end_day - last_spy).days, 0)
        self.spy_refresh_outcome = outcome
        self.spy_refresh_routine = routine
        self.spy_refresh_at = at
        self.spy_refresh_raw = raw
        self.spy_error = None
        self.spy_failure_outcome = failure
        self.spy_failure_at = failure_at
        self.spy_failure_raw = failure_raw
        self.spy_failure_count = failures or (1 if failure else 0)


#: HOUSE RULE 6 does not apply: _spy_lag_note takes both dates from the
#: object it is given and never reads the clock, so a fixed pair cannot
#: drift out of any window.
FRIDAY = date(2026, 8, 21)
MONDAY = date(2026, 8, 24)


class TestAClosedMarketIsNotDamage:
    def test_the_owners_monday_reads_as_normal(self):
        """The exact case reported: SPY to Friday, bot to Monday."""
        html = _spy_lag_note(
            FakePerf(last_spy=FRIDAY, end_day=MONDAY,
                     outcome="no_new_bars_upstream", routine=True), "perf")
        assert "nothing is wrong" in html
        assert "2026-08-21" in html
        assert "alarm" not in html.lower(), (
            "a weekend rendered as an alarm is the exact failure CLAUDE.md "
            "names - it teaches the owner to distrust a working benchmark")

    def test_a_line_that_reaches_today_says_nothing_at_all(self):
        assert _spy_lag_note(
            FakePerf(last_spy=MONDAY, end_day=MONDAY), "perf") == ""

    def test_no_spy_line_at_all_is_left_to_the_existing_alarms(self):
        perf = FakePerf(last_spy=FRIDAY, end_day=MONDAY)
        perf.spy_points = []
        assert _spy_lag_note(perf, "perf") == ""

    def test_a_holiday_weekend_is_still_routine(self):
        """Friday close, Monday holiday, read on Tuesday: four days and
        nothing wrong."""
        html = _spy_lag_note(
            FakePerf(last_spy=FRIDAY, end_day=FRIDAY + timedelta(days=3),
                     outcome="no_new_bars_upstream", routine=True), "perf")
        assert "nothing is wrong" in html


class TestARealFailureIsLoud:
    def test_a_refused_feed_is_an_alarm_even_over_a_weekend(self):
        """THE HALF THAT MATTERS. A three-day gap looks like a weekend,
        so shape alone would call this normal. The bot's own recorded
        outcome is what tells the truth."""
        html = _spy_lag_note(
            FakePerf(last_spy=FRIDAY, end_day=MONDAY,
                     failure="feeds_refused_http_403",
                     failure_raw="forbidden: subscription does not permit sip"),
            "perf")
        assert "alarm" in html.lower()
        assert "feeds_refused_http_403" in html
        assert "has been failing" in html
        assert "forbidden" in html, (
            "house rule 3: the raw upstream response goes beside the failure")

    def test_a_weekend_on_top_of_failures_still_alarms(self):
        """THE OWNER'S ACTUAL BUNDLE, 2026-08-24: sixteen SIP refusals
        overnight, then fifty routine weekend no-ops on top. Reading only
        the newest attempt reports that nothing is wrong, while the feed
        has in fact stopped answering - and the weekend is the only thing
        hiding it."""
        html = _spy_lag_note(
            FakePerf(last_spy=FRIDAY, end_day=MONDAY,
                     outcome="no_new_bars_upstream", routine=True,
                     failure="feed_no_longer_available_sip", failures=16,
                     failure_raw='{"message":"subscription does not permit '
                                 'querying recent SIP data"}'),
            "perf")
        assert "alarm" in html.lower()
        assert "16 failed attempt" in html
        assert "feed_no_longer_available_sip" in html
        assert "hiding it" in html, (
            "the page shows two readings that appear to contradict each "
            "other; it has to say why")
        assert "REBUILD" in html, (
            "a refused feed never recovers by itself - the one fault on "
            "this page with a button must offer it")

    def test_a_gap_longer_than_a_weekend_is_an_alarm_with_no_record(self):
        """An older database has no benchmark_refreshes row. The gap
        itself still has to be answered for."""
        html = _spy_lag_note(
            FakePerf(last_spy=FRIDAY, end_day=FRIDAY + timedelta(days=9)),
            "perf")
        assert "alarm" in html.lower()
        assert "longer than a weekend" in html

    def test_trading_is_always_said_to_be_unaffected(self):
        for failure in ("feeds_refused_http_403", None):
            html = _spy_lag_note(
                FakePerf(last_spy=FRIDAY, end_day=FRIDAY + timedelta(days=9),
                         failure=failure), "perf")
            assert "rading is unaffected" in html, (
                "the benchmark is reporting only, and an alarm that does not "
                "say so reads as the bot being broken")


class TestTheRoutineWindowIsTheRightSize:
    def test_a_monday_is_inside_it(self):
        assert (MONDAY - FRIDAY).days <= SPY_LAG_ROUTINE_DAYS, (
            "a Monday reading of a Friday close is the commonest view "
            "there is; if it falls outside the routine window the page "
            "alarms every week")


class TestTheRefreshDecidesRoutineForItself:
    """`routine` is set in data/benchmark.py beside the reasons, so a
    reason added later cannot be misclassified by a list of strings kept
    somewhere else (house rule 7)."""

    def test_a_weekend_window_holds_no_weekday(self):
        assert _weekdays_between(date(2026, 8, 22), date(2026, 8, 23)) == 0

    def test_a_holiday_window_holds_one(self):
        # Saturday through a Monday holiday.
        assert _weekdays_between(date(2026, 8, 22), date(2026, 8, 24)) == 1

    def test_a_week_of_silence_holds_several(self):
        assert _weekdays_between(date(2026, 8, 22), date(2026, 8, 28)) == 5


class TestTheOutcomeReachesTheDatabase:
    @pytest.fixture
    def conn(self, tmp_path):
        c = sqlite3.connect(tmp_path / "t.db")
        c.executescript(SCHEMA.read_text())
        yield c
        c.close()

    def test_a_success_is_recorded_as_routine(self, conn):
        assert record_benchmark_refresh(
            conn, RefreshResult(written=2, last_day=FRIDAY, feed="iex",
                                routine=True),
            now=datetime(2026, 8, 24, 6, tzinfo=timezone.utc))
        row = conn.execute(
            "SELECT outcome, routine, bars_written, last_bar_day, feed "
            "FROM benchmark_refreshes").fetchone()
        assert row == ("updated", 1, 2, "2026-08-21", "iex")

    def test_a_failure_keeps_the_raw_upstream_body(self, conn):
        record_benchmark_refresh(
            conn, RefreshResult(skipped_reason="feeds_refused_http_403",
                                routine=False,
                                raw_response="sip: forbidden"),
            now=datetime(2026, 8, 24, 6, tzinfo=timezone.utc))
        row = conn.execute(
            "SELECT outcome, routine, raw_response FROM benchmark_refreshes"
        ).fetchone()
        assert row == ("feeds_refused_http_403", 0, "sip: forbidden")

    def test_a_database_without_the_table_costs_the_note_not_the_refresh(
            self, tmp_path):
        c = sqlite3.connect(tmp_path / "old.db")
        c.executescript(SCHEMA.read_text())
        c.execute("DROP TABLE benchmark_refreshes")
        c.commit()
        assert record_benchmark_refresh(c, RefreshResult(routine=True)) is False
        c.close()
