"""The download must ask how far back, and honour the answer.

OWNER-ASKED: "When i click download log i want it to ask me how many
days of logs so im not getting a massive file."

A diagnostic export defaulting to everything becomes, on a machine that
has been running a while, a file nobody can open - which is the same as
having no export at all.

THREE THINGS MUST HOLD, and the third is the one that would rot:

  1. The window is APPLIED. Rows older than it do not appear.
  2. The window is DECLARED, including which tables it could not be
     applied to. A table with no timestamp comes out whole, and saying
     so is the difference between a short file and a wrong one.
  3. EVERY TABLE THAT HAS A TIMESTAMP IS WINDOWABLE. The column is found
     by naming, from a list. A list is exactly the thing that goes stale
     the moment a table is added, and the failure is silent: the table
     is exported in full while the bundle claims a window. So the list
     is checked against the shipped schema, not against a fixture.

Four tables were already being missed when this was written -
cost_reprice_events (repriced_at), equity_snapshots (taken_at),
kill_switch_events (triggered_at) and stop_confirmations (checked_at).
"""

import json
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard.server import (
    DEFAULT_WINDOW_DAYS,
    LOG_WINDOW_DAYS,
    _time_column,
    diagnostics_bundle,
    window_days,
)

SCHEMA_FILES = ("catalyst/storage/schema.sql",
                "catalyst/storage/schema_graph.sql",
                "catalyst/dashboard/schema_logs.sql")


def _schema_conn(path=":memory:"):
    conn = sqlite3.connect(path)
    root = pathlib.Path(__file__).resolve().parents[1]
    for f in SCHEMA_FILES:
        conn.executescript((root / f).read_text())
    return conn


@pytest.fixture
def aged(tmp_path):
    """One log line and one candidate at each of 0, 3, 20 and 200 days
    old, so a window either bites or it does not."""
    p = str(tmp_path / "aged.db")
    conn = _schema_conn(p)
    now = datetime.now(timezone.utc)
    for age in (0, 3, 20, 200):
        ts = (now - timedelta(days=age)).isoformat()
        conn.execute(
            "INSERT INTO logs (ts, level, component, message) VALUES (?,?,?,?)",
            (ts, "INFO", "catalyst.research", f"line from {age} days ago"))
        conn.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
            (f"c{age}", "ABCD", "financing", "2026-08-20", "confirmed", "[]",
             ts, "2870", "[]"))
    conn.commit()
    conn.close()
    return p


class TestTheWindowIsApplied:
    @pytest.mark.parametrize("days,expected", [(1, 1), (7, 2), (30, 3)])
    def test_older_rows_are_left_out(self, aged, days, expected):
        b = diagnostics_bundle(Db(aged), scope="everything", days=days)
        assert len(b["rows"]["candidates"]) == expected
        assert len(b["recent_logs"]) == expected

    def test_no_window_means_everything(self, aged):
        b = diagnostics_bundle(Db(aged), scope="everything", days=None)
        assert len(b["rows"]["candidates"]) == 4

    def test_a_shorter_window_is_a_smaller_file(self, aged):
        """The whole point of the request."""
        small = len(json.dumps(
            diagnostics_bundle(Db(aged), scope="everything", days=1),
            default=str))
        big = len(json.dumps(
            diagnostics_bundle(Db(aged), scope="everything", days=30),
            default=str))
        assert small < big

    def test_the_window_applies_to_a_SCOPED_bundle_too(self, aged):
        b = diagnostics_bundle(Db(aged), scope="logic", days=1)
        assert len(b["rows"]["candidates"]) == 1


class TestTheWindowIsDeclared:
    def test_it_says_how_far_back_it_went(self, aged):
        b = diagnostics_bundle(Db(aged), scope="everything", days=7)
        assert b["window_days"] == 7
        assert "window_note" in b

    def test_it_names_the_column_it_cut_each_table_on(self, aged):
        b = diagnostics_bundle(Db(aged), scope="everything", days=7)
        assert b["window_applied_to"]["candidates"] == "discovered_at"
        assert b["window_applied_to"]["logs"] == "ts"

    def test_a_table_it_COULD_NOT_window_is_listed(self, aged):
        """A short file and a wrong file look identical otherwise.

        This used to name research_call_turns, which is exactly how the
        defect hid: that table has no clock of its own, came out ENTIRE
        on every bundle at every window, and the honest declaration made
        it read as deliberate. One day of it was 17.5MB. It is windowed
        through its parent now (test_bundle_fits_in_an_upload.py), so the
        declaration is asserted against a table that genuinely has no
        window - the CURRENT positions, which a window would hide the
        point of."""
        b = diagnostics_bundle(Db(aged), scope="everything", days=7)
        assert b["window_not_applicable"], (
            "a table exported in full under a window must say so")
        from catalyst.dashboard.server import _ALWAYS_WHOLE

        assert set(b["window_not_applicable"]) <= set(_ALWAYS_WHOLE), (
            "a table came out whole without a declared reason: "
            f"{sorted(set(b['window_not_applicable']) - set(_ALWAYS_WHOLE))}")

    def test_the_table_that_hid_behind_that_declaration_is_windowed_now(self, aged):
        b = diagnostics_bundle(Db(aged), scope="everything", days=7)
        assert "research_call_turns" not in b["window_not_applicable"]

    def test_no_window_declares_that_too(self, aged):
        b = diagnostics_bundle(Db(aged), scope="everything", days=None)
        assert b["window_days"] is None
        assert "No time window" in b["window_note"]


class TestEveryTimestampedTableCanBeWindowed:
    """THE ONE THAT ROTS. A table added later, with a timestamp column
    under a name not on the list, is exported in full while the bundle
    claims a window - and nothing says so."""

    def _tables(self):
        conn = _schema_conn()
        try:
            return [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")]
        finally:
            conn.close()

    def test_every_table_with_a_time_column_has_one_recognised(self, tmp_path):
        p = str(tmp_path / "schema.db")
        _schema_conn(p).close()
        db = Db(p)
        missed = []
        for table in self._tables():
            cols = db.columns(table)
            looks_timed = [c for c in cols if c.endswith("_at") or c == "ts"]
            if looks_timed and not _time_column(db, table):
                missed.append((table, looks_timed))
        assert not missed, (
            "these tables have a timestamp the window cannot see, so they "
            f"would be exported in full: {missed}")


class TestTheFormAsks:
    def test_the_page_offers_a_days_selector(self, aged):
        from catalyst.dashboard import panels

        html_out = panels.logs_panel(Db(aged), {})
        assert 'name="days"' in html_out, "nothing asks how far back"
        assert 'name="scope"' in html_out, "nothing asks what to collect"
        assert "<button" in html_out and "Download" in html_out

    def test_it_defaults_to_a_window_rather_than_to_everything(self, aged):
        """A default of 'everything' is how the export becomes a file
        nobody can open."""
        from catalyst.dashboard import panels

        html_out = panels.logs_panel(Db(aged), {})
        assert f'value="{DEFAULT_WINDOW_DAYS}" selected' in html_out
        assert DEFAULT_WINDOW_DAYS is not None

    def test_every_offered_window_is_a_real_choice(self, aged):
        from catalyst.dashboard import panels

        html_out = panels.logs_panel(Db(aged), {})
        for d in LOG_WINDOW_DAYS:
            assert f'value="{d or 0}"' in html_out

    def test_everything_however_old_is_still_reachable(self, aged):
        """Bounded by default must not mean unavailable."""
        from catalyst.dashboard import panels

        assert 'value="0"' in panels.logs_panel(Db(aged), {})
        assert window_days("0") is None


class TestAHostileDaysValueCannotBreakTheExport:
    """A diagnostic export must not be the thing that 500s while someone
    is diagnosing something else."""

    @pytest.mark.parametrize("raw", ["abc", "", None, "-5", "1e9", "'; DROP"])
    def test_it_falls_back_instead_of_raising(self, raw, aged):
        days = window_days(raw)
        b = diagnostics_bundle(Db(aged), scope="everything", days=days)
        assert "rows" in b

    def test_an_absurd_window_is_capped(self):
        assert window_days("99999") == 3650
