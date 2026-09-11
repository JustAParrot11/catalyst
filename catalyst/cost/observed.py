"""What a call actually costs, read from the ledger rather than typed in.

OWNER-ASKED 2026-09-11: *"i want to be absolutely certain aswell we have
not hard coded api costs, remember we have access with the admin API. We
are testing and getting this to work but eventually will be full
autonomous, we dont want to be changing estimates manually."*

THE AUDIT THAT PROMPTED THIS, in full, because the answer has two halves
and only one of them was already right.

WHAT WAS ALREADY SELF-CORRECTING - the PRICES, which decide what a call
cost once it has happened:

  - per-token rates: `cost/measured_rates.py` divides Anthropic's charge
    for a closed day by Anthropic's own token counts for the same day
    and makes the rate table the result. Owner-set 2026-09-05: "stop
    locally calculating the new price full stop trust the admin API".
  - the cache and web-search multipliers: `cost/factors.py` derives them
    from the itemised bill, using the documented values as a seed and
    discarding any derivation whose components do not add back up to the
    day's billed total.
  - input tokens per web search: `research/boundary.py` seeds 12k from
    one early call and replaces it with the observed 75th percentile
    once eight searching turns exist.

WHAT WAS NOT - the ESTIMATES, which decide before a call whether it is
affordable and how many to allow:

    TYPICAL_RESEARCH_CALL_CENTS  50c   measured blended cost: 22.8c
    HUNT_ESTIMATE_CENTS          60c   measured: 11.6c (18 calls, $2.08)
    HUNT_TURN_ESTIMATE_CENTS     20c   never measured at all

Each was a number someone typed after looking at a bundle once. They
were WRONG by a factor of two to five, and nothing anywhere would have
said so - which is precisely the manual maintenance the owner is asking
to be rid of. This module removes the last of it: the same evidence that
prices a call is now what estimates the next one.

THE SHAPE, deliberately identical to boundary.py's calibration so there
is one pattern in this codebase and not two:

  - a MINIMUM SAMPLE before the measurement is used at all, because
    adapting on four observations is fitting noise;
  - a HIGH PERCENTILE rather than the mean, because an estimate exists
    to cover the expensive calls and the mean is dragged down by cheap
    ones;
  - the built-in constant as the COLD-START SEED, never the authority;
  - and a window, so a rate change six months ago cannot still be
    setting today's estimate.

WHY A LOW MEASUREMENT IS SAFE, which is the question to ask of anything
that can loosen a limit. These estimates are not reservations. The
governor compares them against ACTUAL month-to-date spend, and beneath
that `governor.DAILY_CAP_CENTS` bounds a day's real spend whatever any
estimate says. So an estimate that reads too low costs at most one
call's overshoot at the cap boundary, and the next day's reading
corrects it. An estimate that reads too HIGH is the one that costs
opportunity - and that is the direction the old hard-coded numbers all
erred in.

UNPRICED ROWS ARE EXCLUDED. A row nobody could price is a hole in the
count; the governor already refuses to authorise anything while one
exists, and averaging over it here would understate every estimate.

Fully offline: this reads the local ledger. The Admin API reaches it
only through the reconciliation that corrects the rates, and stays
read-only (GET cost_report and usage_report/messages).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

#: Priced calls of a component needed before its measurement is used.
#: Eight, matching boundary.MIN_CALIBRATION_SAMPLE - one calibration
#: idea, one number, so the two cannot drift apart.
MIN_OBSERVED_CALLS = 8

#: Which point of the observed distribution to estimate from. The mean
#: is dragged down by cheap calls and an estimate exists to cover the
#: dear ones, so this sits high without chasing the single worst.
OBSERVED_PERCENTILE = Decimal("0.75")

#: How far back to look. Long enough for a sample at a few calls a day,
#: short enough that a rate change cannot still be setting the estimate
#: a quarter later.
OBSERVED_WINDOW_DAYS = 30


def _percentile(values: list[Decimal], q: Decimal) -> Decimal:
    """Nearest-rank percentile. No interpolation: these are prices, and
    inventing a value between two observed ones adds nothing."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile of an empty sample")
    idx = int((Decimal(len(ordered) - 1) * q).to_integral_value())
    return ordered[max(0, min(idx, len(ordered) - 1))]


def observed_call_cents(conn, component: str, seed,
                        *, now: datetime | None = None,
                        min_calls: int = MIN_OBSERVED_CALLS,
                        window_days: int = OBSERVED_WINDOW_DAYS,
                        ) -> tuple[Decimal, int]:
    """(estimated cents for one `component` call, sample size).

    Returns the seed with a sample of 0 whenever there is not yet
    enough evidence, the table is missing, or the rows will not parse -
    so a cold start, an upgraded database and a corrupt ledger all
    behave exactly as the code did before this module existed.

    NEVER RAISES. An estimate is not worth a failed cycle: the caller is
    deciding whether to make a call, and the honest fallback is the
    number it used yesterday.
    """
    try:
        floor = Decimal(str(seed))
        if not floor.is_finite() or floor <= 0:
            return Decimal(str(seed)), 0
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return Decimal("0"), 0

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=window_days)).isoformat()
    try:
        rows = conn.execute(
            "SELECT priced_cents FROM cost_events "
            "WHERE component = ? AND kind = 'scheduled' "
            "  AND priced_cents IS NOT NULL AND priced_at >= ?",
            (str(component), since)).fetchall()
    except (sqlite3.Error, AttributeError):
        return floor, 0

    seen: list[Decimal] = []
    for row in rows:
        try:
            value = Decimal(str(row[0]))
        except (ArithmeticError, InvalidOperation, TypeError, ValueError):
            continue
        # A NEGATIVE ROW IS A CORRECTION, NOT A CALL. `backfill_adjustment`
        # rows carry the reconciliation's own true-up and would drag an
        # average below what any call has ever cost.
        if value.is_finite() and value > 0:
            seen.append(value)

    if len(seen) < max(1, int(min_calls)):
        return floor, len(seen)
    return _percentile(seen, OBSERVED_PERCENTILE), len(seen)


def observed_or_seed(conn, component: str, seed, **kw) -> Decimal:
    """`observed_call_cents` when the caller only wants the figure.

    Convenience only - every caller that reports to a human should use
    the pair and show the sample size, because "50c because we measured
    140 calls" and "50c because we have never measured one" are
    different facts and the dashboard must not render them the same
    (BUILD-BRIEF: every number says where it came from).
    """
    return observed_call_cents(conn, component, seed, **kw)[0]
