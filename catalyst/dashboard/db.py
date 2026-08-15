"""Read-only sqlite access, with every query carrying its own SQL.

The rule this module exists to serve (ui-designer brief, rule 2): a zero
must explain itself. Every read returns a QueryResult that carries the
exact SQL and parameters that produced it and how many rows came back,
so the UI can print "0 rows from <this query>" beside an empty panel.
"No data yet" and "the query is broken" then look different on screen.

The connection is opened `mode=ro` on purpose: the dashboard is
read-only over the trade database except for the two write endpoints in
server.py, which open their own read-write connection explicitly.
"""

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from catalyst.benchmark import FALLBACK_CAPITAL_CENTS

DEFAULT_DB = "data/catalyst.db"
DEFAULT_BARS = "data/bars"

#: THE FALLBACK, AND ONLY THE FALLBACK. This was a hardcoded $1,000 that
#: drove net equity, the SPY index, the performance curve and the annual
#: hurdle, so pointing the bot at a $2,000 account silently compared the
#: new account against the old base.
#:
#: The live figure is now DATA - `catalyst.benchmark.current(conn)` reads
#: the latest `benchmark_baselines` row, which carries how much, from
#: when, and why. This constant is that module's documented placeholder,
#: re-exported here so the name keeps working and cannot drift from it.
#: Anything rendering a figure must read the baseline, and must say so
#: when `Baseline.is_placeholder` is true.
START_CAPITAL_CENTS = int(FALLBACK_CAPITAL_CENTS)


def db_path() -> str:
    return os.environ.get("CATALYST_DB", DEFAULT_DB)


def bars_path() -> str:
    return os.environ.get("CATALYST_BARS", DEFAULT_BARS)


@dataclass(frozen=True)
class QueryResult:
    """Rows plus the provenance of the rows."""

    sql: str
    params: tuple
    rows: list
    error: str | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def is_empty(self) -> bool:
        return not self.rows

    def dicts(self) -> list[dict]:
        return [dict(r) for r in self.rows]

    def scalar(self, default=None):
        if not self.rows:
            return default
        value = self.rows[0][0]
        return default if value is None else value


class Db:
    """A read-only handle. Never raises out of q(): a broken query is a
    thing the dashboard must *display*, not a 500 that hides it."""

    def __init__(self, path: str | None = None):
        self.path = path or db_path()
        self.open_error: str | None = None
        self._conn: sqlite3.Connection | None = None
        self._tables: set[str] | None = None
        try:
            if not Path(self.path).exists():
                raise FileNotFoundError(
                    f"no database file at {self.path} "
                    "(set CATALYST_DB, or the bot has never run)"
                )
            uri = f"file:{quote(str(Path(self.path).resolve()))}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        except Exception as exc:  # surfaced on the page, never swallowed
            self.open_error = f"{type(exc).__name__}: {exc}"

    @property
    def conn(self) -> sqlite3.Connection | None:
        """The read-only connection, exposed so panels can cross-check
        their own arithmetic against the module that owns the number
        (e.g. cost/ledger.py). Read-only by construction: a write through
        this handle raises 'attempt to write a readonly database'."""
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def q(self, sql: str, params: tuple = ()) -> QueryResult:
        if self._conn is None:
            return QueryResult(sql, params, [], self.open_error or "no connection")
        try:
            rows = self._conn.execute(sql, params).fetchall()
            return QueryResult(sql, params, list(rows), None)
        except Exception as exc:
            return QueryResult(sql, params, [], f"{type(exc).__name__}: {exc}")

    def count(self, table: str, where: str = "", params: tuple = ()) -> QueryResult:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return self.q(sql, params)

    def tables(self) -> set[str]:
        if self._tables is None:
            res = self.q("SELECT name FROM sqlite_master WHERE type='table'")
            self._tables = {r[0] for r in res.rows}
        return self._tables

    def table_exists(self, name: str) -> bool:
        return name in self.tables()

    def columns(self, table: str) -> list[str]:
        if not self.table_exists(table):
            return []
        return [r[1] for r in self.q(f"PRAGMA table_info({table})").rows]


def jload(text, default):
    """schema.sql stores several columns as JSON strings. A malformed one
    is shown as itself rather than crashing the page."""
    if text is None:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default
