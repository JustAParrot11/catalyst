"""An EXISTING database must gain new columns, or the bot breaks on
exactly the machines that have history worth keeping.

CAUGHT BEFORE SHIPPING, 2026-08-20. `pause_reason` was added to
schema.sql and to the reconciliation INSERT. Every test passed - and it
would have taken the owner's cost reconciliation down completely,
because:

  - init_db runs CREATE TABLE IF NOT EXISTS, which does nothing at all
    to a table that already exists;
  - so a running bot keeps the old shape;
  - and the first INSERT naming pause_reason fails with "no such
    column", which is the nightly reconciliation.

THE SUITE CANNOT SEE THIS BY ITSELF. Every other test builds its
database from scratch, where the schema is current by construction. The
only way to test an upgrade is to build the OLD shape on purpose, which
is what this file does.

ADDITIVE ONLY. ALTER TABLE ... ADD COLUMN is the one change SQLite makes
without rewriting a table, and existing rows get NULL. Dropping,
renaming or retyping is a data migration and does not belong here.
"""

import sqlite3

import pytest

from catalyst.storage import ADDED_COLUMNS, add_missing_columns, init_db


class TestAnExistingDatabaseIsUpgraded:
    def test_a_table_missing_the_column_gains_it(self, tmp_path):
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE cost_reconciliation_events "
                     "(id TEXT PRIMARY KEY, target_date TEXT)")
        conn.commit()
        conn.close()

        conn = init_db(path)
        cols = {r[1] for r in
                conn.execute("PRAGMA table_info(cost_reconciliation_events)")}
        assert "pause_reason" in cols
        conn.close()

    def test_existing_rows_survive_with_null(self, tmp_path):
        """An upgrade that loses a row is worse than one that fails."""
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE cost_reconciliation_events "
                     "(id TEXT PRIMARY KEY, target_date TEXT)")
        conn.execute("INSERT INTO cost_reconciliation_events VALUES "
                     "('e1','2026-08-17')")
        conn.commit()
        conn.close()

        conn = init_db(path)
        row = conn.execute("SELECT id, target_date, pause_reason FROM "
                           "cost_reconciliation_events").fetchone()
        assert row == ("e1", "2026-08-17", None)
        conn.close()

    def test_it_is_idempotent(self, tmp_path):
        """install/upgrade.sh is safe to run twice, so this must be too."""
        path = str(tmp_path / "t.db")
        conn = init_db(path)
        assert add_missing_columns(conn) == []      # nothing left to add
        conn.close()
        conn = init_db(path)                         # and again
        conn.close()

    def test_it_reports_what_it_changed(self, tmp_path):
        """A silent schema change is a schema change nobody can debug."""
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE cost_reconciliation_events (id TEXT)")
        conn.commit()
        added = add_missing_columns(conn)
        # EVERY column it added must be reported, not a hardcoded one.
        # A fixed list here rots the next time a column is added, and
        # the failure looks like a migration bug rather than a stale
        # test - which is exactly the confusion this file exists to
        # prevent (house rule 7: assert the rule, not an enumeration).
        expected = [f"{t}.{c}" for t, c, _ in ADDED_COLUMNS
                    if t == "cost_reconciliation_events"]
        assert added == expected, (
            "a column was added to the database without being reported")
        assert expected, "this test asserts nothing if the list is empty"
        conn.close()

    def test_a_table_that_does_not_exist_yet_is_left_alone(self, tmp_path):
        """schema.sql owns creation. Trying to ALTER a table that is not
        there would turn a fresh install into a crash."""
        conn = sqlite3.connect(str(tmp_path / "empty.db"))
        assert add_missing_columns(conn) == []
        conn.close()


class TestTheMigrationListIsHonest:
    @pytest.mark.parametrize("table,column,decl", ADDED_COLUMNS)
    def test_every_declared_column_is_in_the_schema_too(
            self, table, column, decl):
        """A column added by migration but never added to schema.sql
        would exist on upgraded machines and be missing on fresh ones -
        the same split this file exists to close, pointing the other
        way."""
        from catalyst.storage import SCHEMA_PATH

        sql = SCHEMA_PATH.read_text()
        block = sql[sql.index(f"CREATE TABLE IF NOT EXISTS {table}"):]
        block = block[:block.index(");")]
        assert column in block, (
            f"{table}.{column} is migrated onto existing databases but is "
            "not in schema.sql, so a FRESH install would not have it")

    @pytest.mark.parametrize("table,column,decl", ADDED_COLUMNS)
    def test_nothing_here_is_destructive(self, table, column, decl):
        assert "NOT NULL" not in decl.upper(), (
            "ALTER TABLE ADD COLUMN cannot add NOT NULL without a default; "
            "existing rows have no value for it")


class TestTheReconciliationInsertWorksOnAnUpgradedDatabase:
    """The actual failure that was nearly shipped, end to end."""

    def test_a_pause_reason_can_be_written_after_upgrade(self, tmp_path):
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE cost_reconciliation_events ("
                     "id TEXT PRIMARY KEY, target_date TEXT, kind TEXT, "
                     "component TEXT, local_total_cents TEXT, "
                     "cost_api_total_cents TEXT, discrepancy_cents TEXT, "
                     "threshold_cents TEXT, api_raw_response TEXT, "
                     "api_record_count INTEGER, action_taken TEXT, "
                     "acknowledged_by TEXT, acknowledged_at TEXT, "
                     "reconciled_at TEXT)")
        conn.commit()
        conn.close()

        conn = init_db(path)
        conn.execute(
            "INSERT INTO cost_reconciliation_events "
            "(id, target_date, kind, component, local_total_cents, "
            " cost_api_total_cents, discrepancy_cents, threshold_cents, "
            " api_raw_response, api_record_count, action_taken, "
            " pause_reason, acknowledged_by, acknowledged_at, reconciled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e1", "2026-08-17", "all", "{}", "364", "364", "0", "50",
             "{}", 3, "none", None, None, None, "2026-08-18T00:00:00Z"))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM "
                            "cost_reconciliation_events").fetchone()[0] == 1
        conn.close()
