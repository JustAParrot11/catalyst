"""Raw usage capture and cent-accurate pricing.

price() is the ONLY function permitted to convert a usage object into a
dollar figure, and it reads every UsageComponents field - there is no
code path that prices from input_tokens/output_tokens alone
(ARCHITECTURE.md section 3.2; TRAPS.md cache-token trap).

reconcile_day() queries the Anthropic Cost API for exactly ONE closed
day at a time, parses amounts as decimal-string cents, compares per
(date, kind, component) - never pooled - and pauses scheduled
authorization on an unacknowledged discrepancy. No Anthropic key exists
in the build environment: the transport is injectable and tests use
recorded fixtures shaped per TRAPS.md.
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
    MODEL_RATES_CENTS_PER_MTOK,
    WEB_SEARCH_CENTS_PER_QUERY,
    UnknownModelError,
)
from catalyst.cost import CostEvent
from catalyst.research.schema import UsageComponents

_MTOK = Decimal("1000000")


def price(usage: UsageComponents, model: str) -> Decimal:
    """Cents, as Decimal. Reads ALL five usage fields, always.

    Interface note: ARCHITECTURE section 3.2 wrote price(usage) alone;
    pricing requires the model's rate, which UsageComponents does not
    carry, so `model` is an explicit second parameter. Recorded as an
    interface amendment in the stage-3 PR rather than smuggled in.
    """
    if model not in MODEL_RATES_CENTS_PER_MTOK:
        raise UnknownModelError(
            f"No pricing for model {model!r}. Add it to pricing.py - "
            "an unknown model must never price itself at zero (TRAPS.md)."
        )
    input_rate, output_rate = MODEL_RATES_CENTS_PER_MTOK[model]

    input_cents = Decimal(usage.input_tokens) * input_rate / _MTOK
    output_cents = Decimal(usage.output_tokens) * output_rate / _MTOK
    cache_write_cents = (
        Decimal(usage.cache_creation_input_tokens) * input_rate * CACHE_WRITE_MULTIPLIER / _MTOK
    )
    cache_read_cents = (
        Decimal(usage.cache_read_input_tokens) * input_rate * CACHE_READ_MULTIPLIER / _MTOK
    )
    web_search_cents = Decimal(usage.web_search_requests) * WEB_SEARCH_CENTS_PER_QUERY

    return input_cents + output_cents + cache_write_cents + cache_read_cents + web_search_cents


def make_usage_components(raw_usage: dict) -> UsageComponents:
    """Parse a raw Anthropic usage object, keeping it verbatim in .raw.

    Missing cache fields default to 0 only because genuinely-uncached
    calls omit them; the raw object is stored so a renamed field can be
    detected and repriced later (TRAPS.md: store the raw usage object
    verbatim).
    """
    server_tool_use = raw_usage.get("server_tool_use") or {}
    return UsageComponents(
        input_tokens=int(raw_usage.get("input_tokens", 0)),
        output_tokens=int(raw_usage.get("output_tokens", 0)),
        cache_creation_input_tokens=int(raw_usage.get("cache_creation_input_tokens", 0)),
        cache_read_input_tokens=int(raw_usage.get("cache_read_input_tokens", 0)),
        web_search_requests=int(server_tool_use.get("web_search_requests", 0)),
        raw=raw_usage,
    )


def record(event: CostEvent, conn: sqlite3.Connection) -> None:
    """Append to cost_events. The raw usage object goes in verbatim."""
    conn.execute(
        "INSERT INTO cost_events (id, raw_usage_json, kind, component, priced_cents, priced_at, api_call_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event.id,
            json.dumps(event.usage.raw, sort_keys=True),
            event.kind,
            event.component,
            str(event.priced_cents),
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
    """Convenience: parse, price, record - the path investigate() uses
    after every API turn."""
    usage = make_usage_components(raw_usage)
    event = CostEvent(
        id=str(uuid.uuid4()),
        usage=usage,
        kind=kind,
        component=component,
        priced_cents=price(usage, model),
        priced_at=datetime.now(timezone.utc),
        api_call_id=api_call_id,
    )
    record(event, conn)
    return event


@dataclass(frozen=True)
class ReconciliationResult:
    target_date: date
    kind: str
    component: str
    local_total_cents: Decimal
    cost_api_total_cents: Decimal
    discrepancy_cents: Decimal
    action_taken: Literal["none", "scheduled_paused"]


# Discrepancy beyond this pauses scheduled authorization until a human
# acknowledges the reconciliation event (ARCHITECTURE section 7.1).
RECONCILE_THRESHOLD_CENTS = Decimal("50")


def reconcile_day(
    target_date: date,
    kind: Literal["scheduled", "manual"],
    component: str,
    conn: sqlite3.Connection,
    fetch_cost_api_day: Callable[[date], list[dict]],
) -> ReconciliationResult:
    """Compare the local ledger against the Cost API for ONE closed day.

    fetch_cost_api_day is injectable (no Anthropic key in this build
    environment); the production implementation must pass an explicit
    page limit sized to one day (TRAPS.md: default page size quietly
    drops the newest days) and must only be called for days that have
    CLOSED - today's spend is not queryable (TRAPS.md).

    Cost API amounts arrive as decimal STRINGS denominated in CENTS
    (TRAPS.md) - parsed with Decimal, never float.
    """
    if target_date >= datetime.now(timezone.utc).date():
        raise ValueError(
            f"reconcile_day({target_date}) called for a day that has not closed. "
            "The Cost API reports whole days only (TRAPS.md)."
        )

    row = conn.execute(
        "SELECT COALESCE(SUM(CAST(priced_cents AS REAL)), 0) FROM cost_events "
        "WHERE kind = ? AND component = ? AND date(priced_at) = ?",
        (kind, component, target_date.isoformat()),
    ).fetchone()
    # Re-sum precisely with Decimal (SQLite SUM is float; cents-scale
    # error is possible) - the float sum above is only a fast emptiness
    # check.
    rows = conn.execute(
        "SELECT priced_cents FROM cost_events WHERE kind = ? AND component = ? AND date(priced_at) = ?",
        (kind, component, target_date.isoformat()),
    ).fetchall()
    local_total = sum((Decimal(r[0]) for r in rows), Decimal("0"))

    api_records = fetch_cost_api_day(target_date)
    api_total = sum(
        (Decimal(str(rec["amount"])) for rec in api_records
         if rec.get("kind") == kind and rec.get("component") == component),
        Decimal("0"),
    )

    discrepancy = (local_total - api_total).copy_abs()
    action: Literal["none", "scheduled_paused"] = "none"
    if discrepancy > RECONCILE_THRESHOLD_CENTS:
        action = "scheduled_paused"
        conn.execute(
            "INSERT INTO cost_reconciliation_events "
            "(id, target_date, kind, component, local_total_cents, cost_api_total_cents, "
            " discrepancy_cents, threshold_cents, action_taken, reconciled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), target_date.isoformat(), kind, component,
             str(local_total), str(api_total), str(discrepancy),
             str(RECONCILE_THRESHOLD_CENTS), action,
             datetime.now(timezone.utc).isoformat()),
        )
    else:
        conn.execute(
            "INSERT INTO cost_reconciliation_events "
            "(id, target_date, kind, component, local_total_cents, cost_api_total_cents, "
            " discrepancy_cents, threshold_cents, action_taken, acknowledged_by, acknowledged_at, reconciled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), target_date.isoformat(), kind, component,
             str(local_total), str(api_total), str(discrepancy),
             str(RECONCILE_THRESHOLD_CENTS), action, "auto",
             datetime.now(timezone.utc).isoformat(),
             datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()

    return ReconciliationResult(
        target_date=target_date,
        kind=kind,
        component=component,
        local_total_cents=local_total,
        cost_api_total_cents=api_total,
        discrepancy_cents=discrepancy,
        action_taken=action,
    )


def has_unacknowledged_discrepancy(conn: sqlite3.Connection) -> bool:
    """True while any reconciliation event that paused scheduled spend
    remains unacknowledged - authorize() checks this and refuses
    scheduled calls until a human clears it."""
    row = conn.execute(
        "SELECT COUNT(*) FROM cost_reconciliation_events "
        "WHERE action_taken = 'scheduled_paused' AND acknowledged_at IS NULL"
    ).fetchone()
    return row[0] > 0
