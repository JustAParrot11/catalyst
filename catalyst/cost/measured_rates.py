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

THE RULE, owner-set 2026-09-05: "stop locally calculating the new price
full stop trust the admin API".

    measured bill HIGHER than we priced  -> our rate was too low
    measured bill LOWER  than we priced  -> our rate was too high

    Either way the rate becomes what the bill divides to, in full, on
    one clean day's evidence.

THIS REPLACES AN ASYMMETRY, and the asymmetry is worth remembering
because it is right everywhere else in this codebase. Rises used to
apply at up to +25% on one day; cuts needed three agreeing days and
moved at most -10%, on the reasoning that lowering a rate lets the bot
spend more and the system must not loosen its own limits.

That reasoning belongs to ADAPTIVE PARAMETERS - conviction floors, stop
widths - which are inferred from noisy outcomes, where a run of luck
must never buy a looser limit. A price is not inferred. It is
Anthropic's charge for a day divided by Anthropic's token counts for
the same day: arithmetic on an invoice. Refusing to believe it in one
direction does not make the ledger safer, it makes it knowingly wrong
for longer - and it did. pricing.py carried a FORECAST that Sonnet 5's
introductory rate would end on 2026-08-31; on 1 September every call
priced 50% higher on a date somebody had typed, and the only mechanism
that could undo it was rationed to 10% per three agreeing days.

WHAT STILL GUARDS IT. A day too small to measure from (MIN_DAY_CENTS),
a reading inside the deadband, a day with more than one model billed,
and a factor beyond SANITY_MULTIPLE - all refused and recorded. Beneath
all of it, governor.DAILY_CAP_CENTS bounds a day's spend whatever the
rate says, so a bad reading costs one day and is corrected by the next
one.

WHY THE TABLE CANNOT SIMPLY BE DELETED, still. The governor has to
decide whether the NEXT call is affordable, in the middle of a day, and
the Cost API reports whole CLOSED days only (TRAPS.md). A local rate is
structurally required; what is no longer required is that anybody
maintains or predicts it.
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

#: THE BILL IS NOT AN INFERENCE, SO IT IS NOT WALKED TOWARD.
#:
#: OWNER-SET 2026-09-05: "stop locally calculating the new price full
#: stop trust the admin API".
#:
#: This module used to move the table a fraction at a time, and
#: asymmetrically: +25% maximum on one day's evidence, -10% maximum and
#: only after three agreeing days. That shape is the brief's rule for
#: ADAPTIVE PARAMETERS - conviction floors, stop widths - and it is
#: right for those, because they are inferred from noisy outcomes and a
#: run of luck must not loosen a limit.
#:
#: A price is not inferred. It is Anthropic's own charge for the day
#: divided by Anthropic's own token counts for the same day - arithmetic
#: on an invoice, not a hypothesis about the market. Walking toward it
#: does not make it safer; it just means the ledger is knowingly wrong
#: for longer, in whichever direction. Sonnet 5 going from 300 back to
#: 200 is a 33% cut: four adjustments, three agreeing days each, most of
#: a month priced 50% high while the owner's research budget throttles
#: against a number nobody was ever charged.
#:
#: So a clean reading applies in full, both ways. What survives is the
#: guard that a rate this far out is not a price at all:
#: A derived rate more than this far from the one in force, in either
#: direction, is refused and recorded rather than applied. Anthropic has
#: never moved a published price by 4x; a factor that large is a credit,
#: a refund, a partial outage or a shape change in the API's answer -
#: the same order-of-magnitude guard `overrides.py` puts on a rate the
#: owner types by hand, for the same reason.
#:
#: WHAT BOUNDS THE DAMAGE IF A BAD READING STILL LANDS. An under-priced
#: rate lets the governor authorise more calls than it should. That is
#: capped by governor.DAILY_CAP_CENTS (500c) regardless of any rate, and
#: the NEXT closed day's bill corrects the rate again - so the exposure
#: is one day, bounded by a limit that does not depend on this file
#: being right.
SANITY_MULTIPLE = Decimal("4")

#: The same idea for the cache and web-search MULTIPLIERS, stated
#: ABSOLUTELY rather than relatively - see learn_factors_from_closed_day
#: for why a relative bound would refuse the corrections they exist to
#: make. Nothing that discounts or surcharges an input rate can bill at
#: ten times it; a reading outside that is a misread bill split.
FACTOR_CEILING = Decimal("10")


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


def _day_token_counts(conn: sqlite3.Connection, target_date: date) -> dict:
    """The day's token counts by component, from the RAW usage objects.

    Read back from `raw_usage_json` rather than from any parsed column,
    because the raw object is the thing TRAPS.md insists is stored
    verbatim for exactly this reason: a field that was renamed or nested
    is still in there to be found.
    """
    import json as _json

    from catalyst.cost.tracker import make_usage_components

    got = {"input": 0, "output": 0, "cache_5m": 0, "cache_1h": 0,
           "cache_read": 0, "web_search": 0}
    for (raw,) in conn.execute(
            "SELECT raw_usage_json FROM cost_events "
            "WHERE date(priced_at) = ? AND priced_cents IS NOT NULL",
            (target_date.isoformat(),)).fetchall():
        try:
            u = make_usage_components(_json.loads(raw))
        except Exception:  # noqa: BLE001 - one bad row must not blind the day
            continue
        got["input"] += u.input_tokens
        got["output"] += u.output_tokens
        got["cache_read"] += u.cache_read_input_tokens
        got["web_search"] += u.web_search_requests
        nested = u.raw.get("cache_creation") if isinstance(u.raw, dict) else None
        if isinstance(nested, dict):
            h1 = int(nested.get("ephemeral_1h_input_tokens", 0) or 0)
            got["cache_1h"] += h1
            got["cache_5m"] += int(nested.get(
                "ephemeral_5m_input_tokens",
                u.cache_creation_input_tokens - h1) or 0)
        else:
            got["cache_5m"] += u.cache_creation_input_tokens
    return got


#: Below this many tokens a component's derived rate is division noise,
#: not a measurement. A handful of cache-read tokens against a
#: five-decimal cost figure can imply almost any multiplier.
MIN_COMPONENT_TOKENS = 10_000
MIN_COMPONENT_REQUESTS = 5


def _derive_factors(counts: dict, billed, input_rate: Decimal):
    """Measured multipliers from the split bill, or None per component.

    Each is a RATIO to the input rate, which is how price() uses them -
    so they stay meaningful when the input rate itself moves.
    """
    mtok = Decimal("1000000")

    def per_mtok(cents: Decimal, tokens: int):
        if tokens < MIN_COMPONENT_TOKENS or cents <= 0:
            return None
        return cents / Decimal(tokens) * mtok

    out = {}
    if input_rate > 0:
        for name, cents, tokens in (
                ("cache_write", billed.cache_write, counts["cache_5m"]),
                ("cache_write_1h", billed.cache_write_1h, counts["cache_1h"]),
                ("cache_read", billed.cache_read, counts["cache_read"])):
            rate = per_mtok(cents, tokens)
            if rate is not None:
                out[name] = rate / input_rate
    if (counts["web_search"] >= MIN_COMPONENT_REQUESTS
            and billed.web_search > 0):
        out["web_search_cents"] = (
            billed.web_search / Decimal(counts["web_search"]))
    return out


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


def learn_factors_from_closed_day(
    conn: sqlite3.Connection,
    target_date: date,
    model: str,
    billed_total_cents: Decimal,
    records: list[dict],
) -> str:
    """Measure the cache and web-search multipliers from the itemised
    bill. Returns a sentence saying what happened, always.

    THE MULTIPLIERS WERE THE LAST ASSUMPTIONS LEFT. The rate table is
    measured; these four ratios were still typed in from documentation,
    so a corrected day's cost was still built out of guesses. This
    measures them on the same evidence.

    Applies the same way a rate does, and for the same reason (owner-set
    2026-09-05, "trust the admin API"): these come out of the bill's own
    itemisation, so a clean reading is applied in whichever direction it
    points rather than walked toward. The guard that survives is the one
    that says a reading is not a multiplier at all - a split that does
    not add up is refused by `classify` before it reaches here.

    Never raises. A split that cannot be trusted leaves every multiplier
    exactly as it was, and says so.
    """
    from catalyst.cost.components import (
        ComponentSplitRefused, classify,
    )
    from catalyst.cost.factors import (
        COMPONENT_SUM_TOLERANCE, Factors, factors_for_on, set_measured_factors,
    )
    from catalyst.cost.overrides import rates_for_on

    effective = target_date + timedelta(days=1)
    try:
        billed = classify(records, Decimal(billed_total_cents),
                          COMPONENT_SUM_TOLERANCE)
    except ComponentSplitRefused as exc:
        # NOT SILENT. "there was no breakdown" and "the breakdown did not
        # add up" are different facts, and the second one means this code
        # is reading somebody else's API wrongly - which is worth seeing.
        return f"Multipliers not measured: {exc.why}."
    except Exception as exc:  # noqa: BLE001
        return f"Multipliers not measured ({type(exc).__name__})."

    try:
        counts = _day_token_counts(conn, target_date)
        input_rate = rates_for_on(conn, model, target_date)[0]
        derived = _derive_factors(counts, billed, input_rate)
        if not derived:
            return ("The bill split cleanly, but no component had enough "
                    "volume to measure a multiplier from.")

        current = factors_for_on(conn, model, effective)
        changes, held = [], []
        fields = {}
        for name in ("cache_write", "cache_write_1h", "cache_read",
                     "web_search_cents"):
            now = getattr(current, name)
            new = derived.get(name)
            if new is None:
                fields[name] = now
                continue
            new = new.quantize(Decimal("0.0001"))
            if new == now:
                fields[name] = now
            elif not (Decimal("0") < new <= FACTOR_CEILING):
                # THE GUARD ON A MULTIPLIER IS ABSOLUTE, NOT RELATIVE.
                #
                # SANITY_MULTIPLE bounds a RATE, where a 4x move is
                # impossible. These are ratios against the input rate,
                # living between 0.1 (a cache read) and 2.0 (a 1h cache
                # write), so a legitimate correction is routinely a
                # large multiple of the old value: the documented 0.1x
                # cache read measuring at 0.5x is a 5x move and a
                # perfectly ordinary finding. Bounding them relatively
                # would refuse exactly the corrections they exist to
                # make. What is impossible is a cache read costing ten
                # times the input rate it discounts.
                fields[name] = now
                held.append(f"{name} measured {new}, held at {now} - outside "
                            f"0 to {FACTOR_CEILING}x, so it is a misread "
                            "split rather than a multiplier")
            else:
                fields[name] = new
                changes.append(f"{name} {now}->{new}")

        if not changes:
            msg = "Multipliers measured from the bill and unchanged."
            return msg + (" " + "; ".join(held) + "." if held else "")

        set_measured_factors(
            conn, model, effective, Factors(**fields),
            set_by="measured against the bill",
            note=("measured from the bill's own itemisation for "
                  f"{target_date}: " + "; ".join(changes)
                  + ("; " + "; ".join(held) if held else "")))
        return ("Multipliers measured from the bill: " + "; ".join(changes)
                + (". Held: " + "; ".join(held) + "." if held else "."))
    except Exception as exc:  # noqa: BLE001
        return f"Multipliers not measured ({type(exc).__name__})."


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

        if abs(ratio - Decimal("1")) <= DEADBAND:
            m = _replace(base, reason=(
                f"agreed within {DEADBAND * 100:.0f}%: billed {billed}c "
                f"against {local}c priced locally"))
            _record(conn, m)
            return m

        # A FACTOR THIS LARGE IS NOT A PRICE. Refused and recorded, not
        # applied - see SANITY_MULTIPLE.
        if ratio > SANITY_MULTIPLE or ratio < (Decimal("1") / SANITY_MULTIPLE):
            m = _replace(base, reason=(
                f"billed {billed}c against {local}c priced locally is a "
                f"factor of {ratio:.2f} - beyond {SANITY_MULTIPLE}x, which no "
                "published price move has ever been. Treated as a credit, a "
                "refund or a changed API answer and NOT applied; the rate is "
                "unchanged and this reading is on the record."))
            _record(conn, m)
            return m

        # THE BILL, APPLIED IN FULL, IN WHICHEVER DIRECTION IT POINTS.
        # `ratio` is what Anthropic charged divided by what we priced, so
        # the rate that reproduces the bill is simply the rate the day
        # was priced at, scaled by it.
        new_in = (old_in * ratio).quantize(Decimal("1"))
        new_out = (old_out * ratio).quantize(Decimal("1"))
        if new_in <= 0 or new_out <= 0:
            return None

        if new_in == base_in and new_out == base_out:
            m = _replace(base, old_input=base_in, old_output=base_out,
                         reason=(
                             f"billed {billed}c against {local}c priced locally, "
                             f"which the rate already in force for {effective} "
                             f"({base_in}/{base_out} per Mtok) covers in full - "
                             "no override needed"))
            _record(conn, m)
            return m

        direction = "LOW" if ratio > 1 else "HIGH"
        reason = (
            f"billed {billed}c against {local}c priced locally - the table was "
            f"running {abs(ratio - 1) * 100:.1f}% {direction}, so it is now "
            f"{new_in}/{new_out} per Mtok, which is what the bill divides to. "
            "The Admin API is the price (owner-set 2026-09-05), so a clean "
            "reading is applied in full rather than walked toward."
        )
        # Effective from the day AFTER the day it was measured on, so
        # already-priced history keeps the rate that was actually in
        # force when it was priced, and a backfill of an earlier day
        # still reprices it correctly.
        set_override(conn, model, effective,
                     new_in, new_out, set_by="measured against the bill",
                     # The measured rate can be far from the last one when
                     # a real price changes; SANITY_MULTIPLE above is what
                     # bounds it, not overrides.py's typo guard.
                     allow_large_change=True, note=reason)
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
