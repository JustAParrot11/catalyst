"""Raw usage capture and cent-accurate pricing.

price() is the ONLY function permitted to convert a usage object into a
dollar figure, and it reads every UsageComponents field - there is no
code path that prices from input_tokens/output_tokens alone
(ARCHITECTURE.md section 3.2; TRAPS.md cache-token trap).

Audit-driven invariants (cost-auditor, stage 3):
- RECORD FIRST, PRICE SECOND (F2): a billed call whose model is unknown
  still lands a cost_events row (priced_cents NULL) and the governor
  blocks all further spend while any unpriced row exists. The loudness
  arrives beside the record, not instead of it.
- The model is stored on every row (F3), so history is genuinely
  repriceable; reprice_all() exists and is tested.
- The reconciliation threshold is relative with an absolute floor (F1),
  an empty API response for a day with local spend is its own paused
  outcome, and every reconciliation row carries the verbatim API
  payload beside it (F6, house rule 3).
- The Cost API adapter returns a structured page (F4); reconcile_day
  REFUSES to compare a truncated page.
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

# Usage-object keys the parser understands. An unrecognized token-ish key
# is the renamed-field trap arriving (TRAPS.md) - make_usage_components
# raises rather than silently pricing it at zero.
_KNOWN_USAGE_KEYS = {
    "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
    "cache_creation", "server_tool_use", "service_tier",
}


class UnrecognizedUsageFieldError(ValueError):
    pass


def price(usage: UsageComponents, model: str) -> Decimal:
    """Cents, as Decimal. Reads ALL usage fields, always.

    Interface note: ARCHITECTURE section 3.2 wrote price(usage) alone;
    pricing requires the model's rate, so `model` is an explicit second
    parameter - recorded as an interface amendment in the stage-3 PR.
    """
    if model not in MODEL_RATES_CENTS_PER_MTOK:
        raise UnknownModelError(
            f"No pricing for model {model!r}. Add it to pricing.py - "
            "an unknown model must never price itself at zero (TRAPS.md)."
        )
    input_rate, output_rate = MODEL_RATES_CENTS_PER_MTOK[model]

    # 1h-TTL cache writes bill at 2x, 5m at 1.25x (audit F3 follow-up).
    # The nested cache_creation breakdown, when present, is authoritative.
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


def make_usage_components(raw_usage: dict) -> UsageComponents:
    """Parse a raw Anthropic usage object, keeping it verbatim in .raw.

    Raises UnrecognizedUsageFieldError on a token-shaped key it does not
    understand - a renamed billing field must be loud, never zero.
    """
    unknown = {
        k for k in raw_usage
        if k not in _KNOWN_USAGE_KEYS and ("token" in k or "cache" in k or "search" in k)
    }
    if unknown:
        raise UnrecognizedUsageFieldError(
            f"Usage object carries unrecognized billing fields {sorted(unknown)}; "
            "update cost/tracker.py before pricing (TRAPS.md renamed-field trap)."
        )
    server_tool_use = raw_usage.get("server_tool_use") or {}
    return UsageComponents(
        input_tokens=int(raw_usage.get("input_tokens", 0)),
        output_tokens=int(raw_usage.get("output_tokens", 0)),
        cache_creation_input_tokens=int(raw_usage.get("cache_creation_input_tokens", 0)),
        cache_read_input_tokens=int(raw_usage.get("cache_read_input_tokens", 0)),
        web_search_requests=int(server_tool_use.get("web_search_requests", 0)),
        raw=raw_usage,
    )


def record(event: CostEvent, model: str, conn: sqlite3.Connection) -> None:
    """Append to cost_events. Raw usage verbatim; model stored beside it
    so the row is repriceable (audit F3)."""
    conn.execute(
        "INSERT INTO cost_events (id, raw_usage_json, model, kind, component, priced_cents, priced_at, api_call_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.id,
            json.dumps(event.usage.raw, sort_keys=True),
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
    """RECORD FIRST, PRICE SECOND (audit F2).

    The row lands unconditionally - money was already spent by the time
    this runs. Pricing failure leaves priced_cents NULL, which
    has_unpriced_rows() surfaces and the governor treats as blocking.
    """
    usage = make_usage_components(raw_usage)
    priced = None
    pricing_error = None
    try:
        priced = price(usage, model)
    except UnknownModelError as exc:
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
    """True while any cost_events row has NULL priced_cents - the ledger
    has holes and the governor must not authorize on top of them."""
    return conn.execute(
        "SELECT COUNT(*) FROM cost_events WHERE priced_cents IS NULL"
    ).fetchone()[0] > 0


def reprice_all(conn: sqlite3.Connection) -> list[tuple[str, Decimal | None, Decimal]]:
    """Reprice every row from its verbatim raw usage + stored model
    against the CURRENT pricing table. Returns (id, old, new) for every
    row whose price changed or was previously NULL. This is the recovery
    path the verbatim storage exists for (audit F3) - now real, and
    tested."""
    changes = []
    rows = conn.execute(
        "SELECT id, raw_usage_json, model, priced_cents FROM cost_events"
    ).fetchall()
    for row_id, raw_json, model, old in rows:
        usage = make_usage_components(json.loads(raw_json))
        new = price(usage, model)  # raises loudly if model STILL unknown
        old_dec = Decimal(old) if old is not None else None
        if old_dec != new:
            changes.append((row_id, old_dec, new))
            conn.execute(
                "UPDATE cost_events SET priced_cents = ? WHERE id = ?",
                (str(new), row_id),
            )
    conn.commit()
    return changes


@dataclass(frozen=True)
class CostApiPage:
    """Structured Cost API day result (audit F4). A truncated page is
    detectable and reconcile_day refuses to compare against one."""

    records: list[dict]
    has_more: bool
    raw_response: dict


@dataclass(frozen=True)
class ReconciliationResult:
    target_date: date
    kind: str
    component: str
    local_total_cents: Decimal
    cost_api_total_cents: Decimal
    discrepancy_cents: Decimal
    action_taken: str


# Relative threshold with an absolute floor (audit F1): at $5/month
# (~17c/day) a fixed 50c threshold could never fire. 10% of the larger
# side, floored at 5c, fires on any meaningful divergence at any scale.
RECONCILE_REL_THRESHOLD = Decimal("0.10")
RECONCILE_FLOOR_CENTS = Decimal("5")


class TruncatedCostPageError(RuntimeError):
    pass


def reconcile_day(
    target_date: date,
    kind: Literal["scheduled", "manual"],
    component: str,
    conn: sqlite3.Connection,
    fetch_cost_api_day: Callable[[date], CostApiPage],
) -> ReconciliationResult:
    """Compare the local ledger against the Cost API for ONE closed day.

    The adapter must drain within-day pagination itself (explicit page
    limit per TRAPS.md); if it still reports has_more, we REFUSE to
    compare - a truncated reference figure reconciling "clean" is worse
    than no reconciliation (audit F4).
    """
    if target_date >= datetime.now(timezone.utc).date():
        raise ValueError(
            f"reconcile_day({target_date}) called for a day that has not closed. "
            "The Cost API reports whole days only (TRAPS.md)."
        )

    rows = conn.execute(
        "SELECT priced_cents FROM cost_events "
        "WHERE kind = ? AND component = ? AND date(priced_at) = ? AND priced_cents IS NOT NULL",
        (kind, component, target_date.isoformat()),
    ).fetchall()
    local_total = sum((Decimal(r[0]) for r in rows), Decimal("0"))

    page = fetch_cost_api_day(target_date)
    if page.has_more:
        raise TruncatedCostPageError(
            f"Cost API page for {target_date} reports has_more=True; refusing to "
            "compare against a truncated reference. Raise the adapter's page limit."
        )
    api_total = sum(
        (Decimal(str(rec["amount"])) for rec in page.records
         if rec.get("kind") == kind and rec.get("component") == component),
        Decimal("0"),
    )

    discrepancy = (local_total - api_total).copy_abs()
    threshold = max(RECONCILE_FLOOR_CENTS,
                    RECONCILE_REL_THRESHOLD * max(local_total, api_total))

    # An empty API day against non-zero local spend is its own paused
    # outcome (audit F1): "the adapter returned nothing" must never
    # auto-acknowledge as agreement.
    suspicious_empty = (not page.records) and local_total > 0
    paused = discrepancy > threshold or suspicious_empty
    action = "scheduled_paused" if paused else "none"

    conn.execute(
        "INSERT INTO cost_reconciliation_events "
        "(id, target_date, kind, component, local_total_cents, cost_api_total_cents, "
        " discrepancy_cents, threshold_cents, api_raw_response, api_record_count, "
        " action_taken, acknowledged_by, acknowledged_at, reconciled_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), target_date.isoformat(), kind, component,
         str(local_total), str(api_total), str(discrepancy), str(threshold),
         json.dumps(page.raw_response, sort_keys=True), len(page.records),
         action,
         None if paused else "auto",
         None if paused else datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()

    return ReconciliationResult(
        target_date=target_date, kind=kind, component=component,
        local_total_cents=local_total, cost_api_total_cents=api_total,
        discrepancy_cents=discrepancy, action_taken=action,
    )


def has_unacknowledged_discrepancy(conn: sqlite3.Connection) -> bool:
    """True while any reconciliation event that paused spend remains
    unacknowledged. Pauses ALL new spend authorization, both kinds - a
    mispriced table poisons both ledgers, so the pause is deliberately
    global (audit F11, made explicit)."""
    return conn.execute(
        "SELECT COUNT(*) FROM cost_reconciliation_events "
        "WHERE action_taken = 'scheduled_paused' AND acknowledged_at IS NULL"
    ).fetchone()[0] > 0
