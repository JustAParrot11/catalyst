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
  each pass the floor accumulate; the trailing signed drift pauses
  spend when it exceeds the floor even if no single day did.
- A truncated page writes its paused reconciliation row BEFORE raising
  (F4): the refusal is on the record, not just in the traceback.
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
    MODEL_RATES_CENTS_PER_MTOK,
    WEB_SEARCH_CENTS_PER_QUERY,
    UnknownModelError,
)
from catalyst.cost import CostEvent
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
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return UsageComponents(
        input_tokens=_int(raw_usage.get("input_tokens", 0)),
        output_tokens=_int(raw_usage.get("output_tokens", 0)),
        cache_creation_input_tokens=_int(raw_usage.get("cache_creation_input_tokens", 0)),
        cache_read_input_tokens=_int(raw_usage.get("cache_read_input_tokens", 0)),
        web_search_requests=_int(server_tool_use.get("web_search_requests", 0)),
        raw=raw_usage,
    )


def price(usage: UsageComponents, model: str) -> Decimal:
    """Cents, as Decimal. Reads ALL usage fields, always. Refuses to
    price a usage object carrying unrecognized billing fields - pricing
    a payload we do not fully understand understates it silently.

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
    if model not in MODEL_RATES_CENTS_PER_MTOK:
        raise UnknownModelError(
            f"No pricing for model {model!r}. Add it to pricing.py - "
            "an unknown model must never price itself at zero (TRAPS.md)."
        )
    input_rate, output_rate = MODEL_RATES_CENTS_PER_MTOK[model]

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
        Decimal(cache_5m) * input_rate * CACHE_WRITE_MULTIPLIER / _MTOK
        + Decimal(cache_1h) * input_rate * CACHE_WRITE_MULTIPLIER_1H / _MTOK
    )
    cache_read_cents = (
        Decimal(usage.cache_read_input_tokens) * input_rate * CACHE_READ_MULTIPLIER / _MTOK
    )
    web_search_cents = Decimal(usage.web_search_requests) * WEB_SEARCH_CENTS_PER_QUERY

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
    priced = None
    pricing_error = None
    try:
        priced = price(usage, model)
    except (UnknownModelError, UnrecognizedUsageFieldError) as exc:
        pricing_error = exc

    event = CostEvent(
        id=str(uuid.uuid4()),
        usage=usage,
        kind=kind,
        component=component,
        priced_cents=priced,
        priced_at=datetime.now(timezone.utc),
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
        "SELECT id, raw_usage_json, model, priced_cents FROM cost_events"
    ).fetchall()
    now = datetime.now(timezone.utc).isoformat()
    try:
        for row_id, raw_json, model, old in rows:
            usage = make_usage_components(json.loads(raw_json))
            try:
                new = price(usage, model)
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
RECONCILE_FLOOR_CENTS = Decimal("5")
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

    def _insert_row(api_total, discrepancy, threshold, drift, action, auto_ack):
        conn.execute(
            "INSERT INTO cost_reconciliation_events "
            "(id, target_date, kind, component, local_total_cents, cost_api_total_cents, "
            " discrepancy_cents, threshold_cents, api_raw_response, api_record_count, "
            " action_taken, acknowledged_by, acknowledged_at, reconciled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), target_date.isoformat(),
             "all", json.dumps({k: str(v) for k, v in by_kind.items()}),
             str(local_total), str(api_total), str(discrepancy), str(threshold),
             json.dumps(page.raw_response, sort_keys=True), len(page.records),
             action,
             "auto" if auto_ack else None,
             datetime.now(timezone.utc).isoformat() if auto_ack else None,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    if page.has_more:
        # The refusal itself is on the record BEFORE the raise (audit F4):
        # a caller that logs-and-continues still leaves a paused row behind.
        _insert_row(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
                    "scheduled_paused", auto_ack=False)
        raise TruncatedCostPageError(
            f"Cost API page for {target_date} reports has_more=True; refusing to "
            "compare against a truncated reference. The adapter must drain "
            "pagination with an explicit page limit (TRAPS.md)."
        )

    api_total = sum((Decimal(str(rec["amount"])) for rec in page.records), Decimal("0"))
    signed = local_total - api_total
    discrepancy = signed.copy_abs()
    threshold = max(RECONCILE_FLOOR_CENTS,
                    RECONCILE_REL_THRESHOLD * max(local_total, api_total))

    drift = signed + _trailing_signed_drift(conn, target_date)
    suspicious_empty = (not page.records) and local_total > 0
    paused = (discrepancy > threshold
              or suspicious_empty
              or drift.copy_abs() > RECONCILE_FLOOR_CENTS)
    action = "scheduled_paused" if paused else "none"
    _insert_row(api_total, discrepancy, threshold, drift, action, auto_ack=not paused)

    return ReconciliationResult(
        target_date=target_date,
        local_total_cents=local_total, cost_api_total_cents=api_total,
        discrepancy_cents=discrepancy, cumulative_drift_cents=drift,
        action_taken=action,
    )


def _trailing_signed_drift(conn: sqlite3.Connection, before: date) -> Decimal:
    """Sum of signed (local - api) over the trailing DRIFT_WINDOW_DAYS of
    already-reconciled days strictly before `before`."""
    rows = conn.execute(
        "SELECT local_total_cents, cost_api_total_cents FROM cost_reconciliation_events "
        "WHERE target_date < ? AND action_taken != 'check_failed' "
        "ORDER BY target_date DESC LIMIT ?",
        (before.isoformat(), DRIFT_WINDOW_DAYS),
    ).fetchall()
    return sum((Decimal(l) - Decimal(a) for l, a in rows), Decimal("0"))


def has_unacknowledged_discrepancy(conn: sqlite3.Connection) -> bool:
    """True while any reconciliation event that paused spend remains
    unacknowledged. Pauses ALL new spend authorization, both kinds -
    deliberately global (audit F11)."""
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
