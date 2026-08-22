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

#: ASYMMETRIC SPEED, which is the brief's rule verbatim: "tighten
#: quickly on evidence of harm; loosen slowly on evidence of
#: over-caution." A rate that is too HIGH only costs opportunity - the
#: bot runs more cautiously than the owner allowed. A rate that is too
#: LOW costs money. So a rise applies on one day's evidence at up to
#: MAX_STEP, and a CUT has to clear a higher bar in both directions:
#: more days agreeing, and a smaller move when they do.
#:
#: Owner's decision, asked directly: "3 agreeing days, small step".
#:
#: Worst case from a single bad measurement is therefore bounded at
#: DOWN_MAX_STEP of under-pricing for one day, and only after two
#: earlier days already said the same thing.
DOWN_MAX_STEP = Decimal("0.10")     # at most -10% per adjustment
DOWN_AGREEING_DAYS = 3              # this reading plus two before it


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


#: How far ahead to look for the next change in the BUILT-IN schedule.
#: Comfortably past any announced pricing window; the probe is a few
#: hundred calls to a pure function, run at most once a day.
SCHEDULE_HORIZON_DAYS = 400


def _future_schedule_changes(
    model: str, after: date,
) -> list[tuple[date, tuple[Decimal, Decimal]]]:
    """Dates after `after` where the BUILT-IN rate for `model` changes.

    PROBED FROM THE REAL FUNCTION, never a hand-written list of known
    windows (house rule 7). Whoever adds the next introductory-pricing
    window will not remember to update a list here, and the failure that
    causes is silent and expensive - see below for what it costs.
    """
    from catalyst.cost.pricing import rates_for

    out: list[tuple[date, tuple[Decimal, Decimal]]] = []
    prev = rates_for(model, after)
    for i in range(1, SCHEDULE_HORIZON_DAYS + 1):
        d = after + timedelta(days=i)
        cur = rates_for(model, d)
        if cur != prev:
            out.append((d, cur))
            prev = cur
    return out


def _preserve_scheduled_changes(
    conn: sqlite3.Connection, model: str, effective: date,
) -> list[date]:
    """Stop a learned rate from swallowing a future scheduled change.

    THE BUG THIS EXISTS FOR. rates_for_on() resolves a rate as "the
    newest override effective on or before that day wins". An override
    is therefore not a correction to the schedule - it REPLACES the
    schedule from its date onward, forever. So a rate learned on 25
    August would still be winning on 1 September, and Sonnet 5's
    introductory pricing would never end as far as the ledger was
    concerned: the bot would price at ~220 against a real 300 and
    under-price itself by 27%, which is the overspending direction this
    module exists to prevent.

    THE CORRECTION IS NOT CARRIED ACROSS THE BOUNDARY, deliberately. The
    measurement said "our pricing ran k low WHILE PRICING AT THE OLD
    RATE" and cannot tell whether the old rate was itself the reason. At
    a scheduled change the schedule wins and the correction is re-learned
    from fresh evidence - which costs at most one day of measurement,
    and self-corrects. Carrying it would compound a guess, and since
    corrections downward need a human, an over-tightened rate would then
    stay over-tightened.
    """
    restored = []
    for when, (sched_in, sched_out) in _future_schedule_changes(model, effective):
        set_override(
            conn, model, when, sched_in, sched_out,
            set_by="scheduled rate restored",
            note=(f"the published rate changes to {sched_in}/{sched_out} per "
                  f"Mtok on {when}. Re-stated here so the correction learned "
                  f"for {effective} cannot outlive it - an override otherwise "
                  "replaces the schedule from its date onward for good."),
            allow_large_change=True)
        restored.append(when)
    return restored


def _recent_ratios(
    conn: sqlite3.Connection, model: str, before: date, limit: int,
) -> list[Decimal]:
    """The `limit` most recent measured ratios for `model` before `before`."""
    rows = conn.execute(
        "SELECT ratio FROM measured_rate_observations "
        "WHERE model = ? AND target_date < ? "
        "ORDER BY target_date DESC, observed_at DESC LIMIT ?",
        (model, before.isoformat(), limit)).fetchall()
    return [Decimal(str(r[0])) for r in rows]


def _prior_high_readings(
    conn: sqlite3.Connection, model: str, before: date, want: int,
) -> bool:
    """True when the `want` readings before `before` ALL said the table
    is running high.

    Counts READINGS, not calendar days: a day with no spend produces no
    evidence either way, and demanding literal consecutive dates would
    let a quiet weekend reset a run that is genuinely three measurements
    long. Every reading still has to agree - one disagreement in the
    window and the run is broken.
    """
    if want <= 0:
        return True
    got = _recent_ratios(conn, model, before, want)
    if len(got) < want:
        return False
    return all(r < Decimal("1") - DEADBAND for r in got)


def _high_run_length(
    conn: sqlite3.Connection, model: str, upto: date,
) -> int:
    """How many readings in a row now say the table is high, counting
    the one being taken. Shown to the owner so a pending cut reads as
    progress rather than as nothing happening."""
    run = 1
    for r in _recent_ratios(conn, model, upto, DOWN_AGREEING_DAYS):
        if r < Decimal("1") - DEADBAND:
            run += 1
        else:
            break
    return min(run, DOWN_AGREEING_DAYS)


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
        # THE DAY WAS PRICED AT THIS...
        old_in, old_out = rates_for_on(conn, model, target_date)
        # ...BUT THIS IS WHAT WOULD OTHERWISE APPLY once the correction
        # takes effect, and the two are NOT always the same rate.
        #
        # The rate table is a SCHEDULE, not a constant: Sonnet 5's
        # introductory pricing ends 2026-08-31, so 31 August prices at
        # 200 and 1 September at 300 with nothing wrong. An override
        # written from the measured day's rate alone would land on 1
        # September at 200 x step and REPLACE the scheduled 300 -
        # under-pricing the bot into spending more, which is the exact
        # direction this module exists to make impossible.
        #
        # Found by the clock sweep, in this module's own tests, on the
        # one date in the year where it bites.
        effective = target_date + timedelta(days=1)
        base_in, base_out = rates_for_on(conn, model, effective)

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
                # This direction LOOSENS the budget, so it needs more
                # evidence than a rise and moves less far when it gets it.
                agreeing = _prior_high_readings(
                    conn, model, target_date, DOWN_AGREEING_DAYS - 1)
                if not agreeing:
                    m = _replace(base, reason=(
                        f"billed {billed}c against {local}c priced locally - "
                        f"the table is running {(1 - ratio) * 100:.1f}% HIGH. "
                        f"Lowering a rate lets the bot spend more, so it needs "
                        f"{DOWN_AGREEING_DAYS} days agreeing; this is day "
                        f"{_high_run_length(conn, model, target_date)} of "
                        f"{DOWN_AGREEING_DAYS}."))
                    _record(conn, m)
                    return m

                down = max(ratio, Decimal("1") - DOWN_MAX_STEP)
                cut_in = (base_in * down).quantize(Decimal("1"))
                cut_out = (base_out * down).quantize(Decimal("1"))
                if cut_in <= 0 or cut_out <= 0:
                    return None
                capped = (" (capped at the maximum single cut)"
                          if down > ratio else "")
                reason = (
                    f"billed {billed}c against {local}c priced locally - the "
                    f"table has run {(1 - ratio) * 100:.1f}% HIGH for "
                    f"{DOWN_AGREEING_DAYS} readings running, so it has been "
                    f"lowered by {(1 - down) * 100:.1f}%{capped}.")
                set_override(conn, model, effective, cut_in, cut_out,
                             set_by="measured against the bill", note=reason)
                _preserve_scheduled_changes(conn, model, effective)
                m = _replace(base, applied=True, reason=reason,
                             old_input=base_in, old_output=base_out,
                             new_input=cut_in, new_output=cut_out)
                _record(conn, m)
                return m
            else:
                m = _replace(base, reason=(
                    f"agreed within {DEADBAND * 100:.0f}%: billed {billed}c "
                    f"against {local}c priced locally"))
            _record(conn, m)
            return m

        # Under-priced. Raise the rate - bounded, and toward the truth.
        step = min(ratio, Decimal("1") + MAX_STEP)
        # What the measurement says the true rate is: the rate the day
        # was actually priced at, scaled by how far the bill came out.
        implied_in = (old_in * step).quantize(Decimal("1"))
        implied_out = (old_out * step).quantize(Decimal("1"))
        # NEVER BELOW WHAT IS ALREADY SCHEDULED. This floor is what makes
        # "only ever tightens" structural rather than an argument - no
        # arithmetic above it, however wrong, can produce a rate lower
        # than the table would have used anyway.
        new_in = max(implied_in, base_in)
        new_out = max(implied_out, base_out)
        if new_in <= 0 or new_out <= 0:
            return None

        if new_in == base_in and new_out == base_out:
            # The schedule already covers the whole discrepancy - which
            # is precisely what a priced-at-the-old-rate day looks like
            # on the eve of a known price change. Nothing to write.
            m = _replace(base, old_input=base_in, old_output=base_out,
                         reason=(
                             f"billed {billed}c against {local}c priced locally, "
                             f"which the rate already scheduled for {effective} "
                             f"({base_in}/{base_out} per Mtok) covers in full - "
                             "no override needed"))
            _record(conn, m)
            return m

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
        set_override(conn, model, effective,
                     new_in, new_out, set_by="measured against the bill",
                     note=reason)
        # ...and immediately re-state any scheduled change that the
        # override just buried. Order matters: written AFTER, so a later
        # effective_from wins on its own day.
        _preserve_scheduled_changes(conn, model, effective)
        m = _replace(base, applied=True, reason=reason,
                     old_input=base_in, old_output=base_out,
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
