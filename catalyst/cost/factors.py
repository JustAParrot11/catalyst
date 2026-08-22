"""The pricing multipliers, measured rather than assumed.

`pricing.py` holds four numbers that are not rates but RATIOS, and every
one of them was typed in from documentation:

    cache write (5m)  1.25x input
    cache write (1h)  2.00x input
    cache read        0.10x input
    web search        1c per query

The rate table is now measured against the real bill, but these were
still assertions - so a day's cost could be corrected while remaining
built out of four assumptions. This module makes them measurable on the
same evidence, with the built-in values as the seed rather than the
authority.

WHAT MAKES THIS SAFE TO BUILD AGAINST AN UNVERIFIED RESPONSE. Deriving
a component rate needs the Cost API to itemise the day's money by
`token_type`. Anthropic's documentation says it does; this project's own
recorded response from the owner's account came back with every
breakdown field null. Both can be true - the recorded call may simply
not have asked - and it cannot be settled from here.

So nothing trusts the breakdown on sight. A derivation is used only when
the classified components ADD BACK UP to the day's billed total
(within COMPONENT_SUM_TOLERANCE). If the token_type vocabulary is
different from what is matched below, or a category is missed entirely,
the sum does not reconcile and the whole derivation is discarded in
favour of the blended factor that already works. A wrong guess costs a
fallback, never a mispricing.

That check is also what stops the real failure mode here: a cost line
nobody recognised being silently treated as zero, which is the
TRAPS.md renamed-field trap wearing different clothes.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from catalyst.cost.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER_1H,
    WEB_SEARCH_CENTS_PER_QUERY,
)

#: How far the classified components may miss the day's billed total and
#: still be believed. Tight on purpose: this is the check that catches a
#: token_type nobody matched, and a loose tolerance would let exactly the
#: error it exists to find slip through as rounding.
COMPONENT_SUM_TOLERANCE = Decimal("0.01")      # 1%


@dataclass(frozen=True)
class Factors:
    """The four ratios, as used by price(). Defaults are the documented
    values - identical to what the code did before any of this, so a
    system that never measures anything prices exactly as it always
    has."""

    cache_write: Decimal = CACHE_WRITE_MULTIPLIER
    cache_write_1h: Decimal = CACHE_WRITE_MULTIPLIER_1H
    cache_read: Decimal = CACHE_READ_MULTIPLIER
    web_search_cents: Decimal = WEB_SEARCH_CENTS_PER_QUERY

    def __post_init__(self):
        for name in ("cache_write", "cache_write_1h", "cache_read",
                     "web_search_cents"):
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not v.is_finite() or v < 0:
                raise ValueError(
                    f"{name} must be a finite non-negative Decimal, got {v!r} - "
                    "a multiplier that is not a number would price silently "
                    "wrong rather than loudly fail")


DEFAULT_FACTORS = Factors()


def set_measured_factors(
    conn: sqlite3.Connection, model: str, effective_from: date,
    factors: Factors, *, set_by: str, note: str = "",
) -> str:
    """Record measured multipliers for `model` from `effective_from`."""
    if not str(set_by).strip():
        raise ValueError("set_by is required: a factor change must carry "
                         "who made it")
    row_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO measured_factors
           (id, model, effective_from, cache_write, cache_write_1h,
            cache_read, web_search_cents, set_by, set_at, note)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (row_id, model, effective_from.isoformat(), str(factors.cache_write),
         str(factors.cache_write_1h), str(factors.cache_read),
         str(factors.web_search_cents), str(set_by),
         datetime.now(timezone.utc).isoformat(), note or None))
    conn.commit()
    return row_id


def factors_for_on(conn, model: str, on_date: date) -> Factors:
    """The multipliers in force for `model` on `on_date`.

    Falls back to the documented defaults on ANY doubt - no row, no
    table, an unreadable value. A multiplier that cannot be read must
    price as it always did, never as zero: a zero here would silently
    make cache reads free and understate the bill, which is the exact
    class of failure TRAPS.md was written about.
    """
    try:
        row = conn.execute(
            """SELECT cache_write, cache_write_1h, cache_read,
                      web_search_cents
               FROM measured_factors
               WHERE model = ? AND effective_from <= ?
               ORDER BY effective_from DESC, set_at DESC LIMIT 1""",
            (model, on_date.isoformat())).fetchone()
    except sqlite3.Error:
        return DEFAULT_FACTORS
    if not row:
        return DEFAULT_FACTORS
    try:
        return Factors(cache_write=Decimal(str(row[0])),
                       cache_write_1h=Decimal(str(row[1])),
                       cache_read=Decimal(str(row[2])),
                       web_search_cents=Decimal(str(row[3])))
    except (ArithmeticError, ValueError, TypeError):
        return DEFAULT_FACTORS
