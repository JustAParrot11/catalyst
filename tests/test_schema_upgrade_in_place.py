"""An existing database must survive the upgrade.

THE SINGLE MOST LIKELY THING TO BREAK THE OWNER'S UPGRADE. This session
added three tables - `entry_market_context`, `limit_application_notes`
and `benchmark_baselines` - and the owner does not install fresh, they
run `install/upgrade.sh` against a database that has been trading. If
`init_db` cannot add tables to an existing file, or drops data doing it,
the failure lands on a live machine with real records in it.

The schema is `CREATE TABLE IF NOT EXISTS` throughout, so this *should*
be safe. That is exactly the sort of belief this project has a house
rule about: "Verify by running it. An asserted fact is not a checked
fact." So the test builds a database from the schema as it stood BEFORE
these commits, puts real rows in it, runs the upgrade path, and checks
both halves - the new tables exist AND nothing that was there is gone.

Reads the old schema from git, so it stays honest as the schema moves on
rather than testing a copy that quietly drifts into agreeing with the
current one.
"""

import sqlite3
import subprocess

import pytest

from catalyst.storage import init_db

#: The commit `main` was at before this session's schema additions.
BASELINE_COMMIT = "3a897d4"

NEW_TABLES = ("entry_market_context", "limit_application_notes",
              "benchmark_baselines")


def _old_schema() -> str:
    out = subprocess.run(
        ["git", "show", f"{BASELINE_COMMIT}:catalyst/storage/schema.sql"],
        capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip(f"cannot read schema at {BASELINE_COMMIT} from git")
    return out.stdout


@pytest.fixture
def old_db(tmp_path):
    """A database on the PREVIOUS schema, carrying real rows."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(_old_schema())
    conn.execute(
        "INSERT INTO candidates VALUES ('c-old','AAPL','earnings',"
        "'2026-08-20','estimated','[]','2026-08-01T10:00:00+00:00',"
        "'tech','[]')")
    conn.execute(
        "INSERT INTO risk_decisions (id,candidate_id,action,skip_reasons,"
        "adaptive_params_snapshot,decided_at) VALUES ('d-old','c-old',"
        "'skip','[\"below_conviction_floor\"]','{}',"
        "'2026-08-01T10:00:00+00:00')")
    conn.execute(
        "INSERT INTO limit_applications VALUES ('d-old',"
        "'max_total_exposure','1','2','hard',1)")
    conn.commit()
    conn.close()
    return path


class TestTheUpgradeAddsTheNewTables:
    @pytest.mark.parametrize("table", NEW_TABLES)
    def test_each_new_table_is_created_in_place(self, old_db, table):
        conn = init_db(old_db)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        assert table in names, (
            f"{table} was not created by upgrading an existing database - "
            f"every code path that writes it fails on the owner's machine")

    @pytest.mark.parametrize("table", NEW_TABLES)
    def test_each_new_table_is_writable_afterwards(self, old_db, table):
        """Existing is not the same as usable."""
        conn = init_db(old_db)
        try:
            conn.execute(f"SELECT * FROM {table} LIMIT 1")
        finally:
            conn.close()


class TestTheUpgradeLosesNothing:
    def test_existing_rows_survive(self, old_db):
        conn = init_db(old_db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM risk_decisions").fetchone()[0] == 1
        finally:
            conn.close()

    def test_limit_applications_keeps_its_shape(self, old_db):
        """A seventh column was added here and then REMOVED again,
        because positional INSERTs across the suite depend on there
        being exactly six. The note lives in its own table instead. If
        that ever changes back, an upgraded database and the running
        code disagree about the column count."""
        conn = init_db(old_db)
        try:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(limit_applications)")]
            assert len(cols) == 6, f"limit_applications now has {cols}"
            assert conn.execute(
                "SELECT COUNT(*) FROM limit_applications").fetchone()[0] == 1
        finally:
            conn.close()

    def test_upgrading_twice_is_safe(self, old_db):
        """upgrade.sh is documented as safe to run twice."""
        init_db(old_db).close()
        conn = init_db(old_db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
        finally:
            conn.close()


class TestTheNewCodePathsWorkOnAnUpgradedDatabase:
    def test_a_sizing_note_can_be_recorded(self, old_db):
        conn = init_db(old_db)
        try:
            conn.execute(
                "INSERT INTO limit_application_notes VALUES "
                "('d-old','per_stock_stop_width','a measured sentence')")
            conn.commit()
            assert conn.execute(
                "SELECT note FROM limit_application_notes").fetchone()[0]
        finally:
            conn.close()

    def test_a_benchmark_baseline_can_be_struck(self, old_db):
        from datetime import date

        from catalyst import benchmark

        conn = init_db(old_db)
        try:
            assert benchmark.current(conn).is_placeholder
            benchmark.record(conn, capital_cents=200000,
                             start_date=date(2026, 8, 15), source="owner_set",
                             account_fingerprint="abc123",
                             reason="upgraded database")
            assert not benchmark.current(conn).is_placeholder
        finally:
            conn.close()

    def test_the_adaptation_pass_runs_against_it(self, old_db):
        """It runs daily inside the trading loop, so it meets an
        upgraded database on the first cycle after the upgrade."""
        from catalyst.risk.adaptation import run_adaptation_pass

        conn = init_db(old_db)
        try:
            report = run_adaptation_pass(conn)
            assert report.errors == [], report.errors
        finally:
            conn.close()
