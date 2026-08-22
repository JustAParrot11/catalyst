"""Learn the real token rate from the bill, instead of asserting it.

WHY THIS EXISTS. `pricing.py` is a hand-maintained table of published
list prices. Every entry in it is a claim about the world that nothing
checks, and the project has already paid for that twice:

  - Sonnet 5 launched on INTRODUCTORY pricing. Nothing in the code knew.
    It was found empirically on 2026-08-10, after the local ledger
    priced a day ~41% above the owner's real console figure - by a human
    noticing two numbers disagreeing.
  - The table then declares itself stale 90 days after a human last
    typed a date into it, which is a calendar guess standing in for
    evidence. It measures how long ago someone looked, not whether the
    number is right.

Meanwhile the bot already fetches, every closed day, the two figures
that settle the question: what it THOUGHT the day cost (the local
ledger) and what Anthropic actually CHARGED for it (the Cost API).
Their ratio is the correction factor for the rate table, measured rather
than asserted.

WHY THE TABLE CANNOT SIMPLY BE DELETED. The governor has to decide
whether the NEXT call is affordable, in the middle of a day. The Cost
API reports whole CLOSED days only and cannot answer that at any price
(TRAPS.md). So a local rate is structurally required. What is not
required is that a human maintains it by hand - after this, the table is
the last measured value rather than a standing assertion.

THE SAFETY RULE, and the reason this is not symmetric. A rate that is
too HIGH makes the bot spend LESS than the owner allowed; a rate that is
too LOW lets it spend MORE. So:

    measured bill HIGHER than we priced  -> our rate is too low
                                         -> raise it, automatically.
                                            This TIGHTENS the budget and
                                            is always safe.

    measured bill LOWER than we priced   -> our rate is too high
                                         -> record it, change nothing.
                                            Lowering a rate loosens the
                                            budget, and this project does
                                            not let the system loosen its
                                            own limits (ARCHITECTURE 6.1,
                                            BUILD-BRIEF "two tiers").

Owner's decision, asked and answered directly: "correct down, alarm up".

Every guard below exists to stop a confidently wrong number reaching the
governor, because a wrong rate here is not a display bug - it is money.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation

from catalyst.cost.overrides import rates_for_on, set_override

#: A day smaller than this is not evidence about a RATE, it is evidence
#: about rounding. The Cost API reports to five decimal places and the
#: local ledger rounds per event, so on a 5c day a single cent of
#: rounding reads as a 20% pricing error. The real observed day used as
#: this project's reference sample was 45.7c, comfortably above it.
MIN_DAY_CENTS = Decimal("25")

#: Agreement is never exact. Below this the two numbers are saying the
#: same thing and moving the table would be fitting noise - which is
#: precisely what the reconciliation was rewritten to stop doing after
#: it halted the bot for a day over five cents.
DEADBAND = Decimal("0.02")          # 2%

#: BOUNDED STEP (BUILD-BRIEF: "no parameter moves more than a small
#: fraction per adjustment, however emphatic the evidence"). A single
#: strange day - a billing correction, a credit, a partial outage -
#: cannot move the table far. A real rate change converges over a few
#: days instead of arriving in one jump, and every step is on the record.
MAX_STEP = Decimal("0.25")          # at most +25% per adjustment


@dataclass(frozen=True)
class MeasuredRate:
    """One day's verdict on the rate table. `applied` is the only field
    that means money changed hands differently tomorrow."""

    target_date: date
    model: str
    local_total_cents: Decimal
    billed_total_cents: Decimal
    ratio: Decimal                  # billed / local; >1 means we under-priced
    applied: bool
    reason: str
    old_input: Decimal | None = None
    new_input: Decimal | None = None
    old_output: Decimal | None = None
    new_output: Decimal | None = None


def _sole_model(conn: sqlite3.Connection, target_date: date) -> str | None:
    """The one model billed on `target_date`, or None if not exactly one.

    THE RATIO IS ONLY A RATE IF ONE MODEL PRODUCED IT. The Cost API
    returns the day's money without a per-model split on this account
    (every breakdown field came back null in the recorded response), so
    on a two-model day the ratio is a blend and attributing it to either
    model's rate would be arithmetic dressed up as measurement.

    Today the live path bills exactly one model - research and position
    review are both Sonnet 5 - so this passes. The day a second model is
    added it stops passing, silently and correctly, rather than quietly
    writing a wrong rate. Classified by the rule, not by enumeration
    (house rule 7).
    """
    rows = conn.execute(
        "SELECT DISTINCT model FROM cost_events "
        "WHERE date(priced_at) = ? AND priced_cents IS NOT NULL "
        "AND model IS NOT NULL",
        (target_date.isoformat(),),
    ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _record(conn: sqlite3.Connection, m: MeasuredRate) -> None:
    """Every observation lands, applied or not - including the ones that
    changed nothing. 'The rate was checked and agreed' is the evidence
    that retires the 90-day staleness guess, and it only exists if the
    quiet days are written down too."""
    conn.execute(
        "INSERT INTO measured_rate_observations "
        "(id, target_date, model, local_total_cents, billed_total_cents, "
        " ratio, applied, reason, old_input_cents_per_mtok, "
        " new_input_cents_per_mtok, old_output_cents_per_mtok, "
        " new_output_cents_per_mtok, observed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), m.target_date.isoformat(), m.model,
         str(m.local_total_cents), str(m.billed_total_cents), str(m.ratio),
         1 if m.applied else 0, m.reason,
         None if m.old_input is None else str(m.old_input),
         None if m.new_input is None else str(m.new_input),
         None if m.old_output is None else str(m.old_output),
         None if m.new_output is None else str(m.new_output),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def learn_from_closed_day(
    conn: sqlite3.Connection,
    target_date: date,
    local_total_cents: Decimal,
    billed_total_cents: Decimal,
) -> MeasuredRate | None:
    """Compare what a closed day was priced at against what it cost, and
    tighten the rate table if the bill was higher.

    Returns None when there is nothing to say (no spend, no model, or
    figures that cannot be read). NEVER raises: this runs inside the
    reconciliation, and a fault in learning a rate must not take down the
    check that the ledger is honest.
    """
    try:
        local = Decimal(local_total_cents)
        billed = Decimal(billed_total_cents)
        if not (local.is_finite() and billed.is_finite()):
            return None
        if local <= 0 or billed <= 0:
            # Not "the rate is wrong" - a day with no spend, or an empty
            # API answer that the reconciliation itself already refuses
            # to read as agreement. Nothing to learn either way.
            return None

        model = _sole_model(conn, target_date)
        if model is None:
            return None

        ratio = billed / local
        old_in, old_out = rates_for_on(conn, model, target_date)

        base = MeasuredRate(
            target_date=target_date, model=model, local_total_cents=local,
            billed_total_cents=billed, ratio=ratio, applied=False, reason="",
            old_input=old_in, old_output=old_out,
        )

        if min(local, billed) < MIN_DAY_CENTS:
            m = _replace(base, reason=(
                f"day too small to price from: {min(local, billed)}c is under "
                f"the {MIN_DAY_CENTS}c needed before rounding stops dominating"))
            _record(conn, m)
            return m

        if ratio <= Decimal("1") + DEADBAND:
            if ratio < Decimal("1") - DEADBAND:
                # The table is charging the bot MORE than Anthropic does.
                # Correcting it would loosen the budget, so it is shown
                # and not done - the owner decides (BUILD-BRIEF: hard
                # bounds and anything that loosens need a human).
                m = _replace(base, reason=(
                    f"billed {billed}c against {local}c priced locally - the "
                    f"table is running {(1 - ratio) * 100:.1f}% HIGH. Lowering "
                    "a rate would let the bot spend more, so this is reported "
                    "and not applied; a human decides."))
            else:
                m = _replace(base, reason=(
                    f"agreed within {DEADBAND * 100:.0f}%: billed {billed}c "
                    f"against {local}c priced locally"))
            _record(conn, m)
            return m

        # Under-priced. Raise the rate - bounded, and toward the truth.
        step = min(ratio, Decimal("1") + MAX_STEP)
        new_in = (old_in * step).quantize(Decimal("1"))
        new_out = (old_out * step).quantize(Decimal("1"))
        if new_in <= 0 or new_out <= 0:
            return None

        capped = " (capped at the maximum single step)" if step < ratio else ""
        reason = (
            f"billed {billed}c against {local}c priced locally - the table was "
            f"running {(ratio - 1) * 100:.1f}% LOW, so it has been raised by "
            f"{(step - 1) * 100:.1f}%{capped}. Raising a rate can only make "
            f"the bot spend less, so it is applied without waiting."
        )
        # Effective from the day AFTER the day it was measured on, so
        # already-priced history keeps the rate that was actually in
        # force when it was priced, and a backfill of an earlier day
        # still reprices it correctly.
        set_override(conn, model, target_date + timedelta(days=1),
                     new_in, new_out, set_by="measured against the bill",
                     note=reason)
        m = _replace(base, applied=True, reason=reason,
                     new_input=new_in, new_output=new_out)
        _record(conn, m)
        return m
    except (ArithmeticError, DivisionByZero, InvalidOperation, TypeError,
            ValueError, sqlite3.Error):
        # House rule: a rate that cannot be learned leaves the table
        # exactly as it was. The reconciliation's own verdict, which is
        # what actually guards the ledger, is untouched.
        return None


def _replace(m: MeasuredRate, **kw) -> MeasuredRate:
    from dataclasses import replace

    return replace(m, **kw)


def latest_observation(conn: sqlite3.Connection) -> dict | None:
    """The most recent verdict, for the dashboard to show instead of a
    calendar age. 'Checked against the real bill on <date> and agreed' is
    a fact; 'this table is 90 days old' is a guess about one."""
    row = conn.execute(
        "SELECT target_date, model, local_total_cents, billed_total_cents, "
        "       ratio, applied, reason, observed_at "
        "FROM measured_rate_observations ORDER BY target_date DESC, "
        "observed_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {"target_date": row[0], "model": row[1],
            "local_total_cents": row[2], "billed_total_cents": row[3],
            "ratio": row[4], "applied": bool(row[5]), "reason": row[6],
            "observed_at": row[7]}
