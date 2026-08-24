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
    # The ACCUMULATED drift that caused a pause, as opposed to the day's
    # own discrepancy. Without it, clear_pauses_that_no_longer_qualify
    # re-judged a drift-caused pause against the DAY figure - which is
    # small by definition in that case - cleared it, and the next cycle
    # paused on the same drift again. That loop is what the owner saw as
    # the discrepancy "showing up frequently".
    ("cost_reconciliation_events", "drift_cents", "TEXT"),
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


#: The evidence graph's tables. A SEPARATE FILE for the reason
#: catalyst/graph/store.py gives - schema.sql is single-session-routed -
#: and, until now, a file nothing ever ran.
#:
#: OWNER'S BUNDLE, 2026-08-24: "sqlite3.OperationalError: no such table:
#: graph_entities", every time research tried to record what it had
#: found. store.py's docstring says "the stage-5 orchestrator folds it
#: in"; it never did, so the graph existed as a schema, a store, a set
#: of hooks and a page, and had no table to write to on any machine that
#: had ever run. Same shape as the logs table before it: everything
#: built except the one line that creates it.
SCHEMA_GRAPH_PATH = Path(__file__).parent / "schema_graph.sql"


def init_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    # CREATE TABLE IF NOT EXISTS throughout, so this is safe on every
    # start and adds the graph to databases that predate it.
    conn.executescript(SCHEMA_GRAPH_PATH.read_text())
    add_missing_columns(conn)
    conn.commit()
    return conn
