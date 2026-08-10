"""Owner-entered token prices, date-effective.

Published rates change - Sonnet 5's introductory pricing ends
2026-08-31 - and until now the only way to follow them was editing
pricing.py and redeploying. This module lets the owner enter a new rate
from the dashboard, with three properties that matter more than the
convenience:

EFFECTIVE-FROM, NEVER RETROACTIVE. A cost row keeps the rate that was in
force when the tokens were actually bought. Repricing history to a new
rate would make the nightly comparison against the real Anthropic bill
drift for reasons nobody could reconstruct afterwards - and that
comparison is the only external check the ledger has.

APPEND-ONLY. A correction is a new row, so the record of what was
believed when survives. Cost reconstruction has to be possible from the
database alone.

REFUSES A ZERO, AND REFUSES AN ORDER-OF-MAGNITUDE TYPO. A zero or
negative rate prices every subsequent call at nothing, which is
precisely the silent-understatement failure TRAPS.md exists to prevent.
The near miss is the same shape: the unit is cents per MILLION tokens,
so typing 3 for "$3 per million" understates every later call by 100x
and nothing about the dashboard would look wrong. Both are refused at
entry rather than discovered a month later against the real bill. A
genuine large change is still possible - it needs the confirmation flag,
so it is deliberate rather than a slip.

pricing.py stays free of database access; the lookup lives here and in
tracker, where a connection is already in hand.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from catalyst.cost.pricing import MODEL_RATES_CENTS_PER_MTOK, UnknownModelError, rates_for


def _as_rate(value, field: str) -> Decimal:
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not a number: {value!r}") from exc
    if rate <= 0:
        raise ValueError(
            f"{field} must be greater than zero (got {rate}). A zero rate "
            "prices every later call at nothing, which is the silent "
            "understatement this ledger is built to prevent.")
    return rate


MAGNITUDE_FACTOR = Decimal("20")


def _check_magnitude(field: str, new: Decimal, current: Decimal) -> None:
    """Refuse a rate wildly away from the one in force.

    The unit is cents per million tokens. Typing the dollar figure - 3
    rather than 300 - is the obvious slip, and it understates the bill by
    100x with nothing on the dashboard looking wrong until the monthly
    reconciliation against the real Anthropic bill catches it. Published
    rates have never moved by 20x, so a factor that large is a typo
    until a human says otherwise.
    """
    if current <= 0:
        return
    if new > current * MAGNITUDE_FACTOR or new * MAGNITUDE_FACTOR < current:
        raise ValueError(
            f"{field} of {new} is more than {MAGNITUDE_FACTOR}x away from the "
            f"{current} in force. The unit is CENTS per MILLION tokens, so "
            f"$3.00 per million is 300, not 3 - check that first. If the "
            f"rate really has moved this far, tick the confirmation box to "
            f"record it anyway.")


def set_override(
    conn: sqlite3.Connection,
    model: str,
    effective_from: date,
    input_cents_per_mtok,
    output_cents_per_mtok,
    *,
    set_by: str,
    note: str = "",
    allow_large_change: bool = False,
) -> str:
    """Record a new rate for `model` from `effective_from` onward."""
    if model not in MODEL_RATES_CENTS_PER_MTOK:
        raise UnknownModelError(
            f"No such model {model!r}. Add it to pricing.py first - an "
            "override for a model the ledger does not know would never "
            "be consulted.")
    if not str(set_by).strip():
        raise ValueError("set_by is required: a rate change must carry who "
                         "made it")
    inp = _as_rate(input_cents_per_mtok, "input rate")
    outp = _as_rate(output_cents_per_mtok, "output rate")
    if not allow_large_change:
        cur_in, cur_out = rates_for_on(conn, model, effective_from)
        _check_magnitude("input rate", inp, cur_in)
        _check_magnitude("output rate", outp, cur_out)
    row_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO pricing_overrides
           (id, model, effective_from, input_cents_per_mtok,
            output_cents_per_mtok, set_by, set_at, note)
           VALUES (?,?,?,?,?,?,?,?)""",
        (row_id, model, effective_from.isoformat(), str(inp), str(outp),
         str(set_by), datetime.now(timezone.utc).isoformat(), note or None))
    conn.commit()
    return row_id


def rates_for_on(conn, model: str, on_date: date) -> tuple[Decimal, Decimal]:
    """(input, output) cents/MTok for `model` on `on_date`.

    The newest override effective on or before that day wins; with none,
    the built-in table answers - including its Sonnet 5 intro window.
    An unknown model still raises rather than pricing at zero.
    """
    if model not in MODEL_RATES_CENTS_PER_MTOK:
        raise UnknownModelError(
            f"No pricing for model {model!r}. Add it to pricing.py - "
            "an unknown model must never price itself at zero (TRAPS.md).")
    try:
        row = conn.execute(
            """SELECT input_cents_per_mtok, output_cents_per_mtok
               FROM pricing_overrides
               WHERE model = ? AND effective_from <= ?
               ORDER BY effective_from DESC, set_at DESC LIMIT 1""",
            (model, on_date.isoformat())).fetchone()
    except sqlite3.Error:
        row = None          # table absent on an older database: fall back
    if row:
        return Decimal(str(row[0])), Decimal(str(row[1]))
    return rates_for(model, on_date)


def all_overrides(conn) -> list[dict]:
    """Every recorded change, newest first - the audit trail."""
    try:
        rows = conn.execute(
            """SELECT model, effective_from, input_cents_per_mtok,
                      output_cents_per_mtok, set_by, set_at, note
               FROM pricing_overrides
               ORDER BY set_at DESC LIMIT 50""").fetchall()
    except sqlite3.Error:
        return []
    return [{"model": r[0], "effective_from": r[1], "input": r[2],
             "output": r[3], "set_by": r[4], "set_at": r[5], "note": r[6]}
            for r in rows]
