"""Scheduled vs manual spend ledger and monthly rollup.

Deliberately exposes NO function that multiplies a partial-month figure
into an annual estimate (ARCHITECTURE.md section 7.4) - annualizing is
refused, not performed.

Audit notes (cost-auditor, stage 3): the BUILD-BRIEF $5/$8/$20/$36
hurdle table applies to kind="scheduled" (runtime) spend ONLY. Manual
spend draws on the one-off $200 build budget, which creates no annual
hurdle but IS a lifetime ceiling - hence lifetime_cents() (F7).
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
    (ARCHITECTURE section 7.1). Kinds are never pooled. Unpriced rows
    (NULL) are excluded here but block authorization entirely via
    tracker.has_unpriced_rows()."""
    month_prefix = as_of.strftime("%Y-%m")
    rows = conn.execute(
        "SELECT priced_cents FROM cost_events "
        "WHERE kind = ? AND strftime('%Y-%m', priced_at) = ? AND priced_cents IS NOT NULL",
        (kind, month_prefix),
    ).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal("0"))


def day_to_date_cents(
    kind: Literal["scheduled", "manual"],
    conn: sqlite3.Connection,
    as_of: date,
) -> Decimal:
    """The same figure for ONE day.

    A monthly cap alone bounds the total and not the rate. Three
    research calls per 15-minute cycle is 288 investigations a day, and
    at conjunction prices that spends a month's budget in an afternoon
    and then sits dark for thirty days. cycle.py already records this
    class happening once ("~51c a cycle, which spends the whole $5
    monthly cap in under an hour"); the fix applied then bounded repeat
    attempts on ONE candidate, which does not bound the rate.

    Local ledger only, same as the month: the Cost API cannot see today
    at any price (TRAPS.md), so a daily gate that consulted it would
    read zero and pass everything.
    """
    day_prefix = as_of.strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT priced_cents FROM cost_events "
        "WHERE kind = ? AND date(priced_at) = ? AND priced_cents IS NOT NULL",
        (kind, day_prefix),
    ).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal("0"))


def lifetime_cents(kind: Literal["scheduled", "manual"], conn: sqlite3.Connection) -> Decimal:
    """All-time priced spend for `kind`. Exists so the $200 one-off build
    budget is enforceable as a LIFETIME ceiling, not a resetting monthly
    allowance (audit F7; BUILD-BRIEF: 'It is not a monthly allowance')."""
    rows = conn.execute(
        "SELECT priced_cents FROM cost_events WHERE kind = ? AND priced_cents IS NOT NULL",
        (kind,),
    ).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal("0"))


def net_realized_profit_cents_prior_month(
    conn: sqlite3.Connection, as_of: date
) -> Decimal:
    """NET realized P&L for the PRIOR calendar month, LIVE trades only,
    floored at zero for the month as a whole (ARCHITECTURE section 9.13).

    Audit F5: paper P&L is fictional and must never raise the real-money
    spending cap - only account_mode='live' rows count, and the basis is
    the prior CLOSED month so a profit realized on the 28th cannot
    retroactively legitimize spend from the 3rd."""
    year, month = as_of.year, as_of.month
    if month == 1:
        prior_prefix = f"{year - 1}-12"
    else:
        prior_prefix = f"{year}-{month - 1:02d}"
    rows = conn.execute(
        "SELECT realized_pnl_cents FROM closed_trades "
        "WHERE strftime('%Y-%m', closed_at) = ? AND account_mode = 'live'",
        (prior_prefix,),
    ).fetchall()
    net = sum((Decimal(r[0]) for r in rows), Decimal("0"))
    return max(Decimal("0"), net)
