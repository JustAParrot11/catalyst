"""Delete log lines older than the retention window.

OWNER-ASKED 2026-08-23: "if a log is older than 30 days, delete log".

WHY IT MATTERS. Nothing in this system ever deleted anything. The logs
table gains rows on every cycle - 96 cycles a day, unattended, forever -
at roughly 17MB a week measured. Invisible over a week, about 900MB a
year, and a full disk on a small VPS is the one failure systemd cannot
restart out of: the service comes back, fails to write, and dies again.

WHAT IS DELIBERATELY *NOT* PRUNED, because "delete old rows" is easy to
apply too widely and every one of these is load-bearing:

  cost_events            the money ledger. reconcile_day compares it
                         against the real bill, the drift check reads a
                         trailing 30-day window, and the governor's
                         month-to-date is computed from it. Deleting a
                         cost row loses money that was really spent.
  refusals               scored 12 days after the fact, and the
                         conviction floor needs 30 of them before it can
                         move. A 30-day prune would destroy the feedback
                         loop the brief calls the most important one.
  positions / orders /   the trade record. "Every trade must be
  fills / risk_decisions explainable after the fact" is not optional,
                         and it has no expiry date.
  research_views         what the model concluded, which is the other
                         half of explaining a trade.

Logs are the chatter. Everything above is the evidence.

A row is kept if it is younger than the window OR carries a traceback:
an ERROR from five weeks ago is the thing you go looking for when
something has been quietly wrong for five weeks, and it is a rounding
error in volume terms.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

#: Owner's figure, 2026-08-23.
LOG_RETENTION_DAYS = 30


def prune_logs(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Delete log rows older than the window. Returns how many went.

    NEVER RAISES. This runs as a maintenance job in an unattended
    service; housekeeping that cannot complete must not take trading
    down with it. A prune that fails simply leaves the rows for the
    next pass.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=LOG_RETENTION_DAYS)).isoformat()
    try:
        cur = conn.execute(
            # Tracebacks are kept regardless of age. They are rare, and
            # they are exactly what someone needs when a fault turns out
            # to have started weeks ago.
            "DELETE FROM logs WHERE ts < ? AND traceback_text IS NULL",
            (cutoff,))
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        return deleted
    except sqlite3.Error:
        return 0
