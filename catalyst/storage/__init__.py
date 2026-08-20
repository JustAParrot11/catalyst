"""Storage - schema only, intentionally thin so it stays a safe shared
surface. Schema changes route through a single session, never two at
once (CLAUDE.md)."""

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


#: The dashboard owns its own log table, but SOMETHING has to create
#: it. Nothing did: the table, the page and the query all existed and no
#: code ever ran the schema, so the Logs page was blank forever and the
#: brief's "searchable from the browser, no SSH" was not true.
#: Columns added to a table AFTER it first shipped.
#:
#: CREATE TABLE IF NOT EXISTS cannot add a column to a database that
#: already exists, and there was no other migration path - so adding a
#: column to schema.sql gave fresh installs the new shape and left every
#: running bot on the old one. The first INSERT naming the column then
#: fails with "no such column", on exactly the machines that have
#: history worth keeping.
#:
#: Caught before shipping pause_reason, which would have taken the whole
#: cost reconciliation down on the owner's machine while leaving every
#: test green - the suite builds its databases from scratch, so it can
#: never see this class of failure by itself. test_storage_migrations.py
#: builds an OLD-shaped database on purpose.
#:
#: ADDITIVE ONLY. ALTER TABLE ... ADD COLUMN is the one schema change
#: SQLite makes without rewriting the table, and existing rows get NULL.
#: Nothing here may drop, rename or retype a column: that is a data
#: migration, and it goes through a session of its own with a backup.
ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("cost_reconciliation_events", "pause_reason", "TEXT"),
]


def add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    """Bring an existing database up to the current shape. Returns what
    it added, so an upgrade can say so rather than doing it silently."""
    added = []
    for table, column, decl in ADDED_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue          # table does not exist yet; schema.sql owns it
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append(f"{table}.{column}")
    return added


def init_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    add_missing_columns(conn)
    conn.commit()
    return conn
