"""Scheduled vs manual spend ledger and monthly rollup.

Deliberately exposes NO function that multiplies a partial-month figure
into an annual estimate (ARCHITECTURE.md section 7.4) - annualizing is
refused, not performed. The dashboard computes an annual hurdle only
from a window that meets a stated minimum sample.
"""

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Literal


def month_to_date_cents(
    kind: Literal["scheduled", "manual"],
    conn: sqlite3.Connection,
    as_of: date,
) -> Decimal:
    """Total priced spend for `kind` in as_of's calendar month, from the
    LOCAL ledger - never the Cost API, which cannot see today
    (ARCHITECTURE section 7.1). Kinds are never pooled."""
    month_prefix = as_of.strftime("%Y-%m")
    rows = conn.execute(
        "SELECT priced_cents FROM cost_events "
        "WHERE kind = ? AND strftime('%Y-%m', priced_at) = ?",
        (kind, month_prefix),
    ).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal("0"))


def net_realized_profit_cents_this_month(conn: sqlite3.Connection, as_of: date) -> Decimal:
    """NET realized P&L for the month as a whole, floored at zero for
    the MONTH, not per trade (ARCHITECTURE section 9.13): a month with
    one $50 winner and one $80 loser nets to -$30 and contributes
    nothing to the governor's cap."""
    month_prefix = as_of.strftime("%Y-%m")
    rows = conn.execute(
        "SELECT realized_pnl_cents FROM closed_trades "
        "WHERE strftime('%Y-%m', closed_at) = ?",
        (month_prefix,),
    ).fetchall()
    net = sum((Decimal(r[0]) for r in rows), Decimal("0"))
    return max(Decimal("0"), net)
