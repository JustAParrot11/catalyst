"""Seven days of logs would not fit in an upload, and one table was why.

OWNER-REPORTED 2026-09-05: "when im getting logs for 7 days the file is
too large to upload".

MEASURED, against their 2026-08-30 bundle. That file covered ONE day and
carried 25MB of JSON:

    17.52 MB  research_call_turns   <-   254 rows
     2.10 MB  logs                  <- 5,945 rows
     2.04 MB  research_calls        <- 6,326 rows
     0.90 MB  candidate_origin      <- 6,302 rows

Seventy per cent of the file came out of 254 rows, because
`raw_response` holds the model's verbatim reply - every web-search
result block echoed back, 34k input tokens median and 166k at worst.

THREE DEFECTS, and the first one is why a shorter window did not help.

  1. `research_call_turns` HAS NO TIMESTAMP COLUMN, so `_time_column`
     returned "" and the table was dumped ENTIRE - every turn ever
     recorded, on every bundle, at every window setting. The bundle
     listed it honestly under `window_not_applicable`, which is exactly
     why it was never chased: it looked deliberate. It is not
     unwindowable; its clock belongs to its parent, and a turn happens
     during its research call by construction.

  2. THE CAP WAS IN ROWS. 20,000 rows of a column that holds megabytes
     is gigabytes, and the cap never came close to firing while the file
     was already too big to send.

  3. NO `ORDER BY`, ANYWHERE. When a cap did fire it kept whatever
     SQLite scanned first, which for an append-only table is the OLDEST
     rows - a truncated diagnostic keeping the least useful half.

The general rule (house rule 7) is the last two: a byte budget applies
to every table, and every table comes out newest-first. The parent-window
map is the specific fix for the table that made it visible, and a test
below refuses any FUTURE table that has neither a clock of its own nor a
declared reason to come out whole.

Fully offline: a temporary SQLite file, no server, no network.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard.server import (
    _ALWAYS_WHOLE, _WINDOW_VIA_PARENT, MAX_BYTES_PER_TABLE, _fit_to_budget,
    _time_column, diagnostics_bundle,
)

NOW = datetime.now(timezone.utc)


def ago(days: float) -> str:
    """House rule 6: every date here is derived from now, never typed."""
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture
def db(tmp_path):
    """A database shaped like the owner's: a handful of research calls,
    each with one enormous verbatim turn."""
    schema = (Path(__file__).resolve().parents[1] / "catalyst" / "storage"
              / "schema.sql")
    path = tmp_path / "t.db"
    c = sqlite3.connect(path)
    c.executescript(schema.read_text())
    big = "x" * 200_000        # a web-search reply, roughly to scale
    for i, days in enumerate((0.2, 0.5, 3.0, 9.0, 40.0)):
        cid = f"cand-{i}"
        c.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                  (cid, "EMBC", "insider_cluster", "2026-08-13", "confirmed",
                   "[]", ago(days), "unknown", "[]"))
        c.execute(
            "INSERT INTO research_calls (id, candidate_id, model, "
            "prompt_rendered, tools_offered, cost_cents, latency_ms, "
            "skipped_reason, called_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"call-{i}", cid, "claude-sonnet-5", "prompt", "[]", "19", 1000,
             None, ago(days)))
        c.execute("INSERT INTO research_call_turns VALUES (?,?,?,?,?)",
                  (f"call-{i}", 0, json.dumps({"content": big}), "{}",
                   "end_turn"))
    c.commit()
    c.close()
    yield Db(str(path))


def bundle(db, days):
    return diagnostics_bundle(db, scope="everything", days=days)


class TestTheTableThatMadeItTooBig:
    def test_a_one_day_window_no_longer_carries_every_turn_ever(self, db):
        """The owner's exact complaint. Five turns exist; two are inside
        one day."""
        rows = bundle(db, 1)["rows"]["research_call_turns"]
        assert len(rows) == 2, (
            f"{len(rows)} turns came out of a one-day window - the table is "
            "still being dumped whole regardless of the window")

    def test_a_seven_day_window_carries_seven_days_of_turns(self, db):
        assert len(bundle(db, 7)["rows"]["research_call_turns"]) == 3

    def test_no_window_still_carries_everything(self, db):
        assert len(bundle(db, None)["rows"]["research_call_turns"]) == 5

    def test_the_window_is_the_calls_clock_not_a_guess(self, db):
        """Turn and call must agree exactly - a turn happens during its
        call, so this is not an approximation."""
        b = bundle(db, 7)
        kept = {r["call_id"] for r in b["rows"]["research_call_turns"]}
        calls = {r["id"] for r in b["rows"]["research_calls"]}
        assert kept == calls

    def test_the_bundle_says_how_it_was_windowed(self, db):
        """It used to say `window_not_applicable`, which read as
        deliberate and hid the defect for a week."""
        b = bundle(db, 1)
        assert "research_call_turns" not in b["window_not_applicable"]
        how = b["window_applied_to"]["research_call_turns"]
        assert "research_calls.called_at" in how

    def test_the_file_is_actually_smaller(self, db):
        """The point of all of it."""
        one = len(json.dumps(bundle(db, 1), default=str))
        everything = len(json.dumps(bundle(db, None), default=str))
        assert one < everything / 2, (
            f"a one-day bundle is {one} bytes against {everything} for the "
            "lot; the window is not doing real work")


class TestTheBudgetIsInBytesNotRows:
    def test_a_table_of_huge_rows_is_capped(self):
        rows = [{"raw_response": "x" * 1_000_000} for _ in range(20)]
        kept, dropped = _fit_to_budget(rows)
        assert dropped and len(kept) < 20
        assert len(json.dumps(kept)) <= MAX_BYTES_PER_TABLE * 1.2

    def test_a_table_of_small_rows_is_untouched(self):
        rows = [{"a": i} for i in range(5000)]
        kept, dropped = _fit_to_budget(rows)
        assert (len(kept), dropped) == (5000, 0)

    def test_one_row_over_budget_is_still_kept(self):
        """A table that says "0 rows" while the count says 1 is worse
        than one oversized row, and that row is often the whole reason
        the bundle was collected."""
        kept, dropped = _fit_to_budget(
            [{"raw_response": "x" * (MAX_BYTES_PER_TABLE * 2)}])
        assert (len(kept), dropped) == (1, 0)

    def test_an_unserialisable_row_does_not_raise(self):
        kept, dropped = _fit_to_budget([{"a": object()}, {"b": object()}])
        assert len(kept) >= 1

    def test_dropping_is_declared_never_silent(self, db, monkeypatch):
        import catalyst.dashboard.server as srv

        monkeypatch.setattr(srv, "MAX_BYTES_PER_TABLE", 250_000)
        b = srv.diagnostics_bundle(db, scope="everything", days=None)
        assert "research_call_turns" in b["rows_truncated"]
        note = b["rows_truncated"]["research_call_turns"]
        assert "dropped" in note and "shorter window" in note


class TestNewestFirst:
    def test_rows_come_back_newest_first(self, db):
        rows = bundle(db, None)["rows"]["research_calls"]
        stamps = [r["called_at"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)

    def test_a_capped_table_keeps_the_RECENT_rows(self, db, monkeypatch):
        """The old query had no ORDER BY, so truncation kept the oldest
        rows on an append-only table."""
        import catalyst.dashboard.server as srv

        monkeypatch.setattr(srv, "MAX_BYTES_PER_TABLE", 250_000)
        b = srv.diagnostics_bundle(db, scope="everything", days=None)
        kept = b["rows"]["research_call_turns"]
        assert kept and kept[0]["call_id"] == "call-0", (
            "the newest turn was dropped and an older one kept")


class TestEveryTableIsAccountedFor:
    """THE RULE, not the instance. A table added later with no timestamp
    fails here rather than quietly becoming the next 17MB."""

    def test_a_table_with_no_clock_is_either_windowed_or_declared(self, db):
        unaccounted = [
            t for t in db.tables()
            if not _time_column(db, t)
            and t not in _WINDOW_VIA_PARENT
            and t not in _ALWAYS_WHOLE
        ]
        assert not unaccounted, (
            f"{unaccounted} have no timestamp, no parent to window through, "
            "and no declared reason to come out whole - so they are dumped "
            "entire on every bundle at every window, which is exactly how "
            "research_call_turns reached 17.5MB unnoticed")

    def test_every_declared_reason_is_a_real_reason(self):
        for table, why in _ALWAYS_WHOLE.items():
            assert len(why) > 20, f"{table} has no real reason recorded"

    def test_the_parent_map_points_at_columns_that_exist(self, db):
        for table, (fk, parent, key, ptime) in _WINDOW_VIA_PARENT.items():
            assert fk in db.columns(table), f"{table}.{fk} is gone"
            assert key in db.columns(parent), f"{parent}.{key} is gone"
            assert ptime in db.columns(parent), f"{parent}.{ptime} is gone"

    def test_nothing_is_in_both_lists(self):
        assert not (set(_WINDOW_VIA_PARENT) & set(_ALWAYS_WHOLE))


class TestTheCheckCanFail:
    """House rule 4, against the code that shipped."""

    def test_the_unwindowed_shape_would_be_caught(self, db):
        """Reproduce the old behaviour - no parent map - and confirm the
        one-day window lets all five turns through."""
        got = db.q("SELECT * FROM research_call_turns LIMIT ?", (20_001,))
        assert len(got.dicts()) == 5, (
            "the shipped query cannot return every turn, so this test no "
            "longer reproduces the defect")

    def test_a_row_cap_would_not_have_fired(self, db):
        """20,000 rows against 254 rows of megabytes: the cap that
        existed could never have helped."""
        rows = bundle(db, None)["rows"]["research_call_turns"]
        from catalyst.dashboard.server import FULL_DUMP_ROWS_PER_TABLE

        assert len(rows) < FULL_DUMP_ROWS_PER_TABLE
        assert len(json.dumps(rows)) > 500_000, (
            "the fixture is no longer big enough to show that a row cap "
            "misses what a byte budget catches")

    def test_the_accounting_scan_can_see_an_unaccounted_table(self, db):
        pretend = [t for t in ["research_call_turns", "a_new_table"]
                   if t not in _WINDOW_VIA_PARENT and t not in _ALWAYS_WHOLE]
        assert pretend == ["a_new_table"]
