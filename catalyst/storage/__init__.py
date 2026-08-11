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
def init_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn
