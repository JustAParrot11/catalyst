"""Raw usage capture and cent-accurate pricing.

price() is the ONLY function permitted to convert a usage object into a
dollar figure, and it reads every UsageComponents field - there is no
code path that prices from input_tokens/output_tokens alone
(ARCHITECTURE.md section 3.2; TRAPS.md cache-token trap).

Audit-driven invariants (cost-auditor, stage 3, two rounds):
- RECORD FIRST, ALWAYS (F2, N1): a billed call lands a cost_events row
  before ANY error can raise - unknown model AND unrecognized usage
  fields both record the verbatim payload with priced_cents NULL, then
  raise. The governor blocks all spend while unpriced rows exist.
- The unknown-field guard recurses into nested billing objects (N2):
  a new key inside server_tool_use or cache_creation is loud, never a
  silently-unbilled request class.
- reconcile_day compares WHOLE-DAY totals (N3): the Cost API cannot see
  this project's internal scheduled/manual split, so the comparison is
  local-day-total vs API-day-total, with the per-kind local breakdown
  recorded beside it. Per-kind pricing errors still cannot hide - they
  move the total.
- Cumulative drift tracking (F1 residual): small daily divergences that
  each pass the floor accumulate; the trailing signed drift is NOTED
  when it exceeds the floor even if no single day did.
- A truncated page writes its reconciliation row BEFORE raising (F4):
  the refusal is on the record, not just in the traceback.
- NOTHING HERE PAUSES SPENDING any more (owner-set 2026-09-05). Every
  discrepancy is recorded with the condition that fired and why; the
  budget cap in governor.py is the only spending stop, and the rate is
  corrected from the bill by measured_rates.py on the same pass.
- reprice_all is transactional (F8), continues past unknown models
  collecting them, and logs every change to cost_reprice_events (F3
  residuals).
- acknowledge_discrepancy() exists (F11 residual): a human path to
  clear a pause, recorded with who and when.
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Literal

from catalyst.cost.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER_1H,
    WEB_SEARCH_CENTS_PER_QUERY,
    UnknownModelError,
    rates_for,
)
from catalyst.cost import CostEvent
from catalyst.cost.measured_rates import (
    learn_factors_from_closed_day,
    learn_from_closed_day,
)
from catalyst.research.schema import UsageComponents

_MTOK = Decimal("1000000")

_KNOWN_TOP_KEYS = {
    "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
    "cache_creation", "server_tool_use",
    # observed live 2026-08-10: a BREAKDOWN of output_tokens (thinking
    # vs visible), not an additional billable quantity - thinking tokens
    # are billed as output tokens and output_tokens already includes
    # them. The nested check below still flags any NEW key inside it.
    "output_tokens_details",
}
# Non-billing metadata the API sends beside the token counts (both
# observed live 2026-08-10). service_tier is additionally checked at
# pricing time: any tier other than "standard" changes the rates and
# refuses to price until a multiplier exists.
_KNOWN_BENIGN_TOP_KEYS = {"service_tier", "inference_geo"}
# web_fetch_requests observed live 2026-08-10 (web search implies fetch).
# Web fetch is NOT metered per-request - the field is an informational
# counter, zero cost; only tokens are billed for fetched content. Web
# search remains the one per-request charge (WEB_SEARCH_CENTS_PER_QUERY).
_KNOWN_SERVER_TOOL_KEYS = {"web_search_requests", "web_fetch_requests"}
_KNOWN_CACHE_CREATION_KEYS = {"ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"}
_KNOWN_OUTPUT_DETAIL_KEYS = {"thinking_tokens"}


class UnrecognizedUsageFieldError(ValueError):
    pass


class ServiceTierUnpricedError(UnrecognizedUsageFieldError):
    """A service_tier other than "standard" changes the rates (batch is
    discounted, priority is a premium). Refusing beats silently billing
    a non-standard tier at the standard rate (cost-audit F7). Subclasses
    UnrecognizedUsageFieldError so record-first handling is identical:
    the row lands unpriced, then this raises, then the governor blocks."""


# A usage payload that is not a JSON object at all is wrapped under this
# key so the row can still be RECORDED verbatim, and is then treated as
# an unknown field so it can never price itself at zero (stress-tester
# defect 14: a non-dict usage raised inside make_usage_components, so
# the row was never written and money was spent with nothing in the
# ledger - the exact failure record-first exists to prevent).
UNPARSEABLE_USAGE_KEY = "unparseable_usage_object"


def _find_unknown_fields(raw_usage: dict) -> list[str]:
    """Token/billing-shaped keys the parser does not understand, at the
    top level AND inside known nested billing objects (audit N2)."""
    # ALLOWLIST, not a substring heuristic (cost-audit F1: under the old
    # token/cache/search substring check, a hypothetical new billed key
    # like "code_execution_requests" matched nothing and priced itself
    # at zero silently - the exact TRAPS.md renamed-field trap). Every
    # unrecognized top-level key now refuses, same as the nested guards.
    unknown = [
        k for k in raw_usage
        if k not in _KNOWN_TOP_KEYS and k not in _KNOWN_BENIGN_TOP_KEYS
    ]
    nested = raw_usage.get("server_tool_use") or {}
    unknown += [f"server_tool_use.{k}" for k in nested
                if k not in _KNOWN_SERVER_TOOL_KEYS]
    cache_nested = raw_usage.get("cache_creation") or {}
    unknown += [f"cache_creation.{k}" for k in cache_nested
                if k not in _KNOWN_CACHE_CREATION_KEYS]
    out_nested = raw_usage.get("output_tokens_details") or {}
    if isinstance(out_nested, dict):
        unknown += [f"output_tokens_details.{k}" for k in out_nested
                    if k not in _KNOWN_OUTPUT_DETAIL_KEYS]

    # A KNOWN KEY CARRYING A NON-FINITE VALUE refuses too, and reports
    # through this same channel rather than getting its own.
    #
    # json.loads("1e400") is inf, and int(inf) raises OverflowError -
    # which the parser now survives by reading it as 0. Surviving is
    # right; pricing it at zero is not. A row that silently costs
    # nothing is the exact TRAPS.md failure this allowlist was built
    # against, so an unreadable count leaves the row UNPRICED, which
    # the governor already blocks on until a person looks.
    for key, value in raw_usage.items():
        if isinstance(value, float) and value != value:      # NaN
            unknown.append(f"{key}=NaN")
        elif isinstance(value, float) and value in (
                float("inf"), float("-inf")):
            unknown.append(f"{key}=non_finite")
    return sorted(unknown)


def make_usage_components(raw_usage: dict) -> UsageComponents:
    """Parse a raw Anthropic usage object LENIENTLY, keeping it verbatim
    in .raw. Never raises: unknown fields are detected by callers via
    _find_unknown_fields so the row can be RECORDED before anything is
    loud (audit N1 - the guard must never prevent the record). That
    includes a usage payload that is not an object at all, and field
    values that are not numbers: both are kept verbatim and left
    unpriceable rather than lost."""
    if not isinstance(raw_usage, dict):
        raw_usage = {UNPARSEABLE_USAGE_KEY: raw_usage}
    server_tool_use = raw_usage.get("server_tool_use")
    if not isinstance(server_tool_use, dict):
        server_tool_use = {}

    def _int(value) -> int:
        # OverflowError TOO. int(float("inf")) raises OverflowError, not
        # ValueError, and json.loads turns "1e400" into inf quite
        # happily - so a usage blob containing one got past every guard
        # here and took the caller down. This function promises never to
        # raise, and audit N1 depends on that promise: the row must be
        # RECORDED before anything is loud, or a usage payload nobody
        # anticipated costs us the record of the spend as well.
        #
        # Found by feeding the dashboard's own reader a hostile ledger;
        # it crashed the whole detailed Overview, which is how a
        # non-finite value would have reached a person.
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return 0

    return UsageComponents(
        input_tokens=_int(raw_usage.get("input_tokens", 0)),
        output_tokens=_int(raw_usage.get("output_tokens", 0)),
        cache_creation_input_tokens=_int(raw_usage.get("cache_creation_input_tokens", 0)),
        cache_read_input_tokens=_int(raw_usage.get("cache_read_input_tokens", 0)),
        web_search_requests=_int(server_tool_use.get("web_search_requests", 0)),
        raw=raw_usage,
    )



def _owner_factors(conn, model: str, on_date: date):
    """Measured multipliers in force on `on_date`, or None for the
    documented defaults. Same contract and same reasons as _owner_rates
    below: never raises, and a missing table simply means nothing has
    been measured yet."""
    try:
        from catalyst.cost.factors import factors_for_on
        return factors_for_on(conn, model, on_date)
    except Exception:  # noqa: BLE001 - fall back to the documented values
        return None


def _owner_rates(conn, model: str, on_date: date):
    """Owner-entered rate in force on `on_date`, or None to use the
    built-in table. Never raises for a missing table - an older database
    simply has no overrides."""
    try:
        from catalyst.cost.overrides import rates_for_on
        return rates_for_on(conn, model, on_date)
    except UnknownModelError:
        raise
    except Exception:  # noqa: BLE001 - fall back to the built-in table
        return None


def price(usage: UsageComponents, model: str,
          on_date: date | None = None,
          rates: tuple | None = None,
          factors=None) -> Decimal:
    """Cents, as Decimal. Reads ALL usage fields, always. Refuses to
    price a usage object carrying unrecognized billing fields - pricing
    a payload we do not fully understand understates it silently.

    `on_date` is the date the SPEND happened (defaults to today): rates
    are date-effective (Sonnet 5 intro pricing through 2026-08-31), so
    repricing a historical row must use the rate in force when the
    tokens were bought, not the rate on the day of the reprice.

    Interface note: ARCHITECTURE section 3.2 wrote price(usage) alone;
    pricing requires the model's rate, so `model` is an explicit second
    parameter - recorded as an interface amendment in the stage-3 PR.
    """
    unknown = _find_unknown_fields(usage.raw)
    if unknown:
        raise UnrecognizedUsageFieldError(
            f"Usage object carries unrecognized billing fields {unknown}; "
            "update cost/tracker.py before pricing (TRAPS.md renamed-field trap)."
        )
    tier = usage.raw.get("service_tier") if isinstance(usage.raw, dict) else None
    if tier is not None and tier != "standard":
        raise ServiceTierUnpricedError(
            f"service_tier {tier!r} has no pricing multiplier; batch and "
            "priority tiers change the rate. Add the multiplier to "
            "cost/pricing.py before pricing this row."
        )
    if on_date is None:
        on_date = datetime.now(timezone.utc).date()
    if rates is not None:
        input_rate, output_rate = rates
    else:
        input_rate, output_rate = rates_for(model, on_date)
    # The MULTIPLIERS, same shape as `rates` above: passed in when the
    # caller has a database to read measured ones from, and otherwise
    # the documented defaults. None here prices exactly as this function
    # did before any of it was measurable.
    if factors is None:
        from catalyst.cost.factors import DEFAULT_FACTORS

        factors = DEFAULT_FACTORS

    cache_1h = 0
    cache_5m = usage.cache_creation_input_tokens
    nested = usage.raw.get("cache_creation")
    if isinstance(nested, dict):
        cache_1h = int(nested.get("ephemeral_1h_input_tokens", 0))
        cache_5m = int(nested.get("ephemeral_5m_input_tokens",
                                  usage.cache_creation_input_tokens - cache_1h))

    input_cents = Decimal(usage.input_tokens) * input_rate / _MTOK
    output_cents = Decimal(usage.output_tokens) * output_rate / _MTOK
    cache_write_cents = (
        Decimal(cache_5m) * input_rate * factors.cache_write / _MTOK
        + Decimal(cache_1h) * input_rate * factors.cache_write_1h / _MTOK
    )
    cache_read_cents = (
        Decimal(usage.cache_read_input_tokens) * input_rate * factors.cache_read / _MTOK
    )
    web_search_cents = (
        Decimal(usage.web_search_requests) * factors.web_search_cents)

    return input_cents + output_cents + cache_write_cents + cache_read_cents + web_search_cents


def record(event: CostEvent, model: str, conn: sqlite3.Connection) -> None:
    """Append to cost_events. Raw usage verbatim; model stored beside it
    so the row is repriceable (audit F3)."""
    conn.execute(
        "INSERT INTO cost_events (id, raw_usage_json, model, kind, component, priced_cents, priced_at, api_call_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.id,
            json.dumps(event.usage.raw, sort_keys=True, default=repr),
            model,
            event.kind,
            event.component,
            str(event.priced_cents) if event.priced_cents is not None else None,
            event.priced_at.isoformat(),
            event.api_call_id,
        ),
    )
    conn.commit()


def record_usage(
    raw_usage: dict,
    model: str,
    kind: Literal["scheduled", "manual"],
    component: str,
    conn: sqlite3.Connection,
    api_call_id: str | None = None,
) -> CostEvent:
    """RECORD FIRST, ALWAYS (audit F2 + N1).

    The row lands unconditionally - money was already spent by the time
    this runs. Both pricing failure modes (unknown model, unrecognized
    usage fields) leave priced_cents NULL and raise AFTER the record is
    safely on disk; has_unpriced_rows() then blocks the governor.
    """
    usage = make_usage_components(raw_usage)  # lenient - never raises
    priced_at = datetime.now(timezone.utc)
    priced = None
    pricing_error = None
    try:
        priced = price(usage, model, on_date=priced_at.date(),
                       rates=_owner_rates(conn, model, priced_at.date()),
                       factors=_owner_factors(conn, model, priced_at.date()))
    except (UnknownModelError, UnrecognizedUsageFieldError) as exc:
        pricing_error = exc

    event = CostEvent(
        id=str(uuid.uuid4()),
        usage=usage,
        kind=kind,
        component=component,
        priced_cents=priced,
        priced_at=priced_at,
        api_call_id=api_call_id,
    )
    record(event, model, conn)
    if pricing_error is not None:
        raise pricing_error  # loud, AFTER the row is safely recorded
    return event


def has_unpriced_rows(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM cost_events WHERE priced_cents IS NULL"
    ).fetchone()[0] > 0


@dataclass(frozen=True)
class RepriceOutcome:
    changes: list[tuple[str, Decimal | None, Decimal]]
    still_unpriced: list[tuple[str, str]]   # (row_id, model) price() still refuses


def reprice_all(conn: sqlite3.Connection) -> RepriceOutcome:
    """Reprice every row from its verbatim raw usage + stored model.

    Transactional (audit F8): all-or-nothing commit, explicit rollback
    on unexpected failure - no half-repriced ledger can survive.
    Continues past rows price() still refuses (F3 residual a),
    collecting them for the caller. Every change is logged to
    cost_reprice_events with old and new (F3 residual b - every
    adjustment carries its evidence, CLAUDE.md)."""
    changes: list[tuple[str, Decimal | None, Decimal]] = []
    still_unpriced: list[tuple[str, str]] = []
    rows = conn.execute(
        "SELECT id, raw_usage_json, model, priced_cents, priced_at FROM cost_events"
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    try:
        for row_id, raw_json, model, old, priced_at in rows:
            usage = make_usage_components(json.loads(raw_json))
            try:
                # the rate in force when the tokens were BOUGHT - a
                # September reprice of an August sonnet-5 row must still
                # use the August intro rate
                spend_date = datetime.fromisoformat(priced_at).date()
                new = price(usage, model, on_date=spend_date,
                            rates=_owner_rates(conn, model, spend_date),
                            factors=_owner_factors(conn, model, spend_date))
            except (UnknownModelError, UnrecognizedUsageFieldError):
                still_unpriced.append((row_id, model))
                continue
            old_dec = Decimal(old) if old is not None else None
            if old_dec != new:
                changes.append((row_id, old_dec, new))
                conn.execute(
                    "UPDATE cost_events SET priced_cents = ? WHERE id = ?",
                    (str(new), row_id),
                )
                conn.execute(
                    "INSERT INTO cost_reprice_events (id, cost_event_id, old_cents, new_cents, repriced_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), row_id,
                     str(old_dec) if old_dec is not None else None,
                     str(new), now),
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return RepriceOutcome(changes=changes, still_unpriced=still_unpriced)


@dataclass(frozen=True)
class CostApiPage:
    """Structured Cost API day result (audit F4). A truncated page is
    detectable, and the production adapter MUST pass an explicit page
    limit (TRAPS.md) - fetch_cost_api_full_day below is the reference
    adapter shape stage 5 implements against."""

    records: list[dict]
    has_more: bool
    raw_response: dict


@dataclass(frozen=True)
class ReconciliationResult:
    target_date: date
    local_total_cents: Decimal
    cost_api_total_cents: Decimal
    discrepancy_cents: Decimal
    cumulative_drift_cents: Decimal
    action_taken: str


RECONCILE_REL_THRESHOLD = Decimal("0.10")
#: The SINGLE-DAY floor. Kept as the published relative threshold's
#: partner, and no longer the thing that pauses spending on its own -
#: see the note on RECONCILE_PAUSE_FLOOR_CENTS below.
RECONCILE_FLOOR_CENTS = Decimal("5")
#: Below this, NOTHING pauses spending, whatever the proportion. The
#: pause test used RECONCILE_FLOOR_CENTS - five cents - against drift
#: accumulated over a 30-day window, so a month of whole-day billing
#: figures settling a cent or two away from a real-time local estimate
#: halted the entire bot until a human clicked acknowledge. It did:
#: 125 candidates refused across 2026-08-12/13, reported to the owner
#: as "spending was blocked".
#:
#: OWNER DECISION, 2026-08-14 (TRAPS.md): a daily figure is fine and the
#: budget re-bases the next day, so reconciliation is a correction, not
#: an alarm. Asked how a discrepancy should behave, the owner chose
#: "block only if large". This is what large means.
RECONCILE_PAUSE_FLOOR_CENTS = Decimal("50")


def drift_is_material(drift, conn, before, window_total=None) -> bool:
    """Is accumulated drift big enough to stop the bot?

    Two things must BOTH be true, because either alone gets it wrong:

      - it clears an absolute floor, so a bot that has spent almost
        nothing never halts on a rounding difference; and
      - it is a real fraction of what was actually spent in the window,
        so the same absolute number means what it should at $3/month and
        at $30/month.

    A fixed number cannot tell those apart, and that is precisely how
    five cents became a halt condition.
    """
    drift = Decimal(drift).copy_abs()
    if drift <= RECONCILE_PAUSE_FLOOR_CENTS:
        return False
    if window_total is None:
        window_total = _window_spend(conn, before)
    if window_total <= 0:
        return True        # drift with no recorded spend to explain it
    return drift > RECONCILE_REL_THRESHOLD * window_total


def _window_spend(conn, before: date) -> Decimal:
    """What the window's reconciled days actually cost, per the API -
    the denominator the drift is judged against."""
    if conn is None:
        return Decimal("0")
    try:
        rows = conn.execute(
            "SELECT cost_api_total_cents FROM cost_reconciliation_events "
            "WHERE target_date < ? AND action_taken != 'check_failed' "
            "ORDER BY target_date DESC LIMIT ?",
            (before.isoformat(), DRIFT_WINDOW_DAYS)).fetchall()
    except sqlite3.Error:
        return Decimal("0")
    return sum((Decimal(r[0]) for r in rows), Decimal("0"))
# F1 residual: a systematic sub-floor daily divergence accumulates; the
# trailing signed drift over this many closed days must also stay under
# the floor or spend pauses.
DRIFT_WINDOW_DAYS = 30


class TruncatedCostPageError(RuntimeError):
    pass


def reconcile_day(
    target_date: date,
    conn: sqlite3.Connection,
    fetch_cost_api_day: Callable[[date], CostApiPage],
) -> ReconciliationResult:
    """Compare the local ledger against the Cost API for ONE closed day,
    WHOLE-DAY totals (audit N3: the Cost API cannot see this project's
    internal scheduled/manual split, so per-kind API filtering would
    match nothing real; per-kind local breakdowns are recorded beside
    the comparison instead - a per-kind pricing error still moves the
    total and still trips the threshold)."""
    if target_date >= datetime.now(timezone.utc).date():
        raise ValueError(
            f"reconcile_day({target_date}) called for a day that has not closed. "
            "The Cost API reports whole days only (TRAPS.md)."
        )

    rows = conn.execute(
        "SELECT kind, priced_cents FROM cost_events "
        "WHERE date(priced_at) = ? AND priced_cents IS NOT NULL",
        (target_date.isoformat(),),
    ).fetchall()
    local_total = sum((Decimal(r[1]) for r in rows), Decimal("0"))
    by_kind = {}
    for kind, cents in rows:
        by_kind[kind] = by_kind.get(kind, Decimal("0")) + Decimal(cents)

    page = fetch_cost_api_day(target_date)

    def _insert_row(api_total, discrepancy, threshold, drift, action,
                    auto_ack, reason=None):
        conn.execute(
            "INSERT INTO cost_reconciliation_events "
            "(id, target_date, kind, component, local_total_cents, cost_api_total_cents, "
            " discrepancy_cents, threshold_cents, api_raw_response, api_record_count, "
            " action_taken, pause_reason, drift_cents, acknowledged_by, "
            " acknowledged_at, reconciled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), target_date.isoformat(),
             "all", json.dumps({k: str(v) for k, v in by_kind.items()}),
             str(local_total), str(api_total), str(discrepancy), str(threshold),
             json.dumps(page.raw_response, sort_keys=True), len(page.records),
             action, reason, str(drift),
             "auto" if auto_ack else None,
             datetime.now(timezone.utc).isoformat() if auto_ack else None,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    if page.has_more:
        # The refusal itself is on the record BEFORE the raise (audit F4):
        # a caller that logs-and-continues still leaves a paused row behind.
        _insert_row(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
                    "discrepancy_noted", auto_ack=True,
                    reason=("the Cost API answered with more pages than were "
                            "read, so the day's bill was incomplete and "
                            "nothing could be compared against it"))
        raise TruncatedCostPageError(
            f"Cost API page for {target_date} reports has_more=True; refusing to "
            "compare against a truncated reference. The adapter must drain "
            "pagination with an explicit page limit (TRAPS.md)."
        )

    api_total = sum((Decimal(str(rec["amount"])) for rec in page.records), Decimal("0"))
    signed = local_total - api_total
    # ONE-SIDED, for the reason spelled out on _trailing_signed_drift:
    # the Cost API bills the whole ORGANISATION, and this account also
    # runs Claude Code. `api > local` is the normal state of a shared
    # account and is not evidence about this bot; `local > api` says the
    # bot claims to have outspent the entire organisation, which cannot
    # happen and means its arithmetic is wrong.
    #
    # The absolute value is still RECORDED, so the row and the dashboard
    # keep showing the true size of the gap either way. Only what
    # PAUSES trading is narrowed.
    discrepancy = signed.copy_abs()
    pauseable = max(signed, Decimal("0"))
    # THE SAME TWO-PART TEST THE DRIFT PATH USES, and for the identical
    # reason. OWNER-REPORTED 2026-08-20: "on 17th we spent $2.95 yet
    # dashboard says $3.36 ... its yet again paused the bot".
    #
    # Reproduced: 41c on a 336c day is 12.2%, over the 10% relative
    # threshold, and the old floor was five cents - so the day check
    # halted the bot. The ACCUMULATED-DRIFT check, which carries the
    # owner's 2026-08-14 "block only if large" decision, would not have:
    # its floor is 50c.
    #
    # Two pause paths, and the decision had only been applied to one of
    # them. A rule the owner explicitly chose was being overridden by
    # the path nobody re-read - which is how "reconciliation is a
    # correction, not an alarm" turned back into an alarm.
    #
    # Both parts still have to be true, so a SYSTEMATIC mispricing is
    # caught exactly as before: a wrong rate table showing 40% on the
    # same day is 134c, over both bars.
    threshold = max(RECONCILE_PAUSE_FLOOR_CENTS,
                    RECONCILE_REL_THRESHOLD * max(local_total, api_total))

    # TODAY'S CONTRIBUTION IS ONE-SIDED TOO. The trailing sum already
    # is; leaving today raw meant a single heavy day of the owner's
    # OWN Claude Code use still halted the bot on its own.
    drift = pauseable + _trailing_signed_drift(conn, target_date)
    suspicious_empty = (not page.records) and local_total > 0
    # WHICH TEST FIRED, in the row itself. Owner-reported 2026-08-20:
    # seven consecutive "scheduled_paused" rows, three of them with a
    # $0.00 discrepancy, and nothing on the page saying why any of them
    # stopped the bot. Three different conditions pause here and they
    # need three different responses - a rounding difference, a broken
    # query and a month of accumulated drift are not the same problem.
    #
    # Recorded rather than re-derived at render time: the trailing
    # window moves, so a reason computed later is a reason for a
    # different day.
    reason = None
    if pauseable > threshold:
        reason = (f"the bot priced {local_total}c of its own spend on a day "
                  f"the whole organisation was billed only {api_total}c - it "
                  f"cannot have outspent the account by {pauseable}c, so its "
                  "arithmetic is wrong")
    elif suspicious_empty:
        reason = (f"the Cost API returned no records at all while the local "
                  f"ledger recorded {local_total}c - an empty answer is not "
                  "agreement, and the raw response is stored beside this row")
    elif drift_is_material(drift, conn, target_date):
        reason = (f"this day agreed, but {drift}c of drift has accumulated "
                  f"over the trailing {DRIFT_WINDOW_DAYS} days, which is "
                  "both over the absolute floor and a real fraction of what "
                  "the window cost")
    # NOTHING HERE PAUSES THE BOT ANY MORE. Owner-set 2026-09-05: "i dont
    # really need any hard limit except a hard stop to stop bot using
    # all the budget".
    #
    # Every condition above used to write `scheduled_paused` and halt
    # all spending until a human clicked acknowledge. The last time it
    # fired - 2026-09-02, on the 1 September figures - it was because
    # pricing.py had FORECAST a rate rise that never happened, the local
    # ledger came out 42% above the bill, and the bot sat idle for 3.5
    # trading days with 201 research calls denied. The ledger was the
    # thing that was wrong, and the fix was a rate correction, which is
    # exactly what the next block does on its own.
    #
    # The row still says WHICH condition fired and why, in
    # `pause_reason`, because that is the diagnosis; it is just
    # information now, never a gate. `has_unacknowledged_discrepancy`
    # therefore finds nothing new from here on.
    action = "discrepancy_noted" if reason is not None else "none"
    _insert_row(api_total, discrepancy, threshold, drift, action,
                auto_ack=True, reason=reason)

    # THE SAME TWO NUMBERS, ASKED A SECOND QUESTION. The comparison above
    # asks "is the ledger honest?"; this asks "is the RATE TABLE right?" -
    # and it is the only place both figures exist for a closed day. This
    # is what a discrepancy DOES now: it moves the rate to what the bill
    # divides to (measured_rates.py), so the same gap does not recur
    # tomorrow.
    learned = learn_from_closed_day(conn, target_date, local_total, api_total)

    # AND THE MULTIPLIERS, from the same day's ITEMISED bill. Only here:
    # page.records is the one place the per-line breakdown exists, and it
    # is not stored anywhere a later pass could read it from.
    if learned is not None:
        try:
            learn_factors_from_closed_day(
                conn, target_date, learned.model, api_total, page.records)
        except Exception:  # noqa: BLE001
            pass          # never at the cost of the reconciliation itself

    return ReconciliationResult(
        target_date=target_date,
        local_total_cents=local_total, cost_api_total_cents=api_total,
        discrepancy_cents=discrepancy, cumulative_drift_cents=drift,
        action_taken=action,
    )


def _trailing_signed_drift(conn: sqlite3.Connection, before: date) -> Decimal:
    """Drift over the trailing window that could indicate a BOT fault.

    ONE-SIDED, and this is the whole correctness of the check.

    OWNER-REPORTED 2026-08-23, with the numbers on screen: "local $0.08
    vs Cost API $0.08, discrepancy $0.00" - and spending PAUSED anyway.
    The day agreed to the cent; the accumulated drift is what halted it.

    The Cost API reports what Anthropic billed the whole ORGANISATION,
    and this owner runs Claude Code on the same account. So on any day
    they do their own work, `api` exceeds `local` by a lot - not because
    the bot's ledger drifted, but because the bot is one of several
    things spending on that account. Summing the signed difference and
    taking its absolute value counted every hour of the owner's own use
    as evidence against the bot, which is why the pause "keeps showing
    up" and stops trading.

    Only ONE direction can indicate a fault here:

      local > api   the bot claims to have spent MORE than the entire
                    organisation was billed. That is impossible, so it
                    is real evidence its arithmetic is wrong. COUNTED.

      api > local   the organisation spent more than the bot did. On a
                    shared account that is the normal state of affairs
                    and says nothing whatever about the bot. IGNORED.

    The other direction - the bot UNDER-pricing - is genuinely invisible
    to this comparison and always was, because a shared account cannot
    tell "the bot under-priced" from "someone else used the API". That
    case is now caught properly by cost/measured_rates.py, which
    compares the bot's OWN priced total against the bill and raises the
    rate table when the bill is higher. The two checks cover the two
    directions between them; this one stops pretending to cover both.
    """
    rows = conn.execute(
        "SELECT local_total_cents, cost_api_total_cents FROM cost_reconciliation_events "
        "WHERE target_date < ? AND action_taken != 'check_failed' "
        "ORDER BY target_date DESC LIMIT ?",
        (before.isoformat(), DRIFT_WINDOW_DAYS),
    ).fetchall()
    return sum((max(Decimal(l) - Decimal(a), Decimal("0")) for l, a in rows),
               Decimal("0"))


def has_unacknowledged_discrepancy(conn: sqlite3.Connection) -> bool:
    """True while any reconciliation event that paused spend remains
    unacknowledged.

    NO LONGER A GATE. reconcile_day stopped writing `scheduled_paused`
    on 2026-09-05 (owner: the budget stop is the only stop), and the
    governor stopped reading this. It is kept for the dashboard, which
    still shows any pre-existing paused row as history, and for the
    acknowledge endpoint that clears one."""
    return conn.execute(
        "SELECT COUNT(*) FROM cost_reconciliation_events "
        "WHERE action_taken = 'scheduled_paused' AND acknowledged_at IS NULL"
    ).fetchone()[0] > 0


def acknowledge_discrepancy(conn: sqlite3.Connection, event_id: str, acknowledged_by: str) -> None:
    """The human path out of a pause (audit F11 residual). Records who
    and when; the dashboard exposes this, never auto-invoked."""
    if not acknowledged_by or acknowledged_by == "auto":
        raise ValueError("acknowledge_discrepancy requires a human identifier")
    cur = conn.execute(
        "UPDATE cost_reconciliation_events "
        "SET acknowledged_by = ?, acknowledged_at = ? "
        "WHERE id = ? AND acknowledged_at IS NULL",
        (acknowledged_by, datetime.now(timezone.utc).isoformat(), event_id),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ValueError(f"no unacknowledged reconciliation event {event_id!r}")


def clear_pauses_that_no_longer_qualify(conn: sqlite3.Connection) -> int:
    """Re-judge OLD paused rows against the rule now in force. Returns
    how many were cleared.

    THE UPGRADE ALONE DOES NOT UNBLOCK ANYTHING, and that is the trap
    this exists to close. drift_is_material() governs whether a NEW
    reconciliation pauses; a row already written under the old five-cent
    rule sits in the database unacknowledged and keeps blocking every
    call forever. The owner would have upgraded, seen "spending was
    blocked" unchanged, and reasonably concluded the fix did nothing.

    This is not "ignore a fault". It re-asks the same question with the
    rule the owner chose on 2026-08-14 - block only if large - and
    clears only the rows whose own recorded discrepancy does not clear
    that bar. A row that WOULD still pause is left alone, and every
    clearance is stamped with why, so the audit trail says who decided
    and on what basis.
    """
    rows = conn.execute(
        "SELECT id, discrepancy_cents, cost_api_total_cents, drift_cents FROM "
        "cost_reconciliation_events WHERE action_taken = 'scheduled_paused' "
        "AND acknowledged_at IS NULL").fetchall()
    cleared = 0
    for row_id, discrepancy, api_total, drift_cents in rows:
        try:
            # THE NUMBER THAT CAUSED THE PAUSE, not the day's own gap.
            # A drift-caused pause has a small day figure BY DEFINITION,
            # so judging it on that always cleared it - and the next
            # cycle paused again on the same drift. Rows written before
            # this column existed carry NULL and fall back to the old
            # behaviour, which is the best that can be done for them.
            drift = Decimal(str(drift_cents if drift_cents is not None
                                else discrepancy))
            spend = Decimal(str(api_total))
        except (TypeError, ValueError, ArithmeticError):
            continue          # unreadable: leave it blocking, for a human
        if drift_is_material(drift, None, None, window_total=spend):
            continue          # still large under the current rule
        conn.execute(
            "UPDATE cost_reconciliation_events SET acknowledged_by = ?, "
            "acknowledged_at = ? WHERE id = ?",
            ("auto: re-judged under the block-only-if-large rule "
             "(owner decision 2026-08-14); this discrepancy does not "
             "clear the current bar",
             datetime.now(timezone.utc).isoformat(), row_id))
        cleared += 1
    if cleared:
        conn.commit()
    return cleared
