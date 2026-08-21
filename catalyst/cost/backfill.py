"""Make the local ledger agree with the bill that was actually paid.

OWNER-REPORTED 2026-08-20: "it doesnt accurately reflect my costings, i
need it updating historically so it looks correct".

They were right, and the gap is real. On 2026-08-15 Anthropic billed
45.7446c against a single API key and the local ledger recorded $0.00 -
so a day of genuine spend is missing from every historical figure the
dashboard draws, and from the governor's own idea of what has been
spent this month.

WHY THIS IS SAFE TO DO AT ALL. Our pricing is not an estimate: given
Anthropic's own token counts, `price()` reproduces their charges to the
cent, verified on two independent days (45.7446c on 2026-08-15 and
364.2052c on 2026-08-17, zero difference both times). So the Usage API's
counts, priced by us, ARE the bill - which means a missing day can be
reconstructed rather than guessed at.

HOW IT CORRECTS, and why not by editing history. Nothing already
recorded is touched. The day gets ONE adjustment row carrying the
difference, the way a ledger is corrected anywhere else that money is
counted: the original entries stay exactly as they were written, and
the correction is visible as a correction. Rewriting the past would
destroy the only evidence of what the bot believed at the time, which
is what every reconciliation afterwards is judged against.

IDEMPOTENT, AND SELF-CORRECTING. The adjustment id is derived from the
date, and the gap is always computed against the day's REAL rows with
any previous adjustment excluded. Running it twice changes nothing;
running it again after the missing events finally arrive shrinks the
adjustment to zero.

READ-ONLY UPSTREAM. One GET, /v1/organizations/usage_report/messages.
Nothing here may call an Admin endpoint that modifies anything.
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

import httpx

from catalyst.cost.tracker import make_usage_components, price

USAGE_REPORT_URL = "https://api.anthropic.com/v1/organizations/usage_report/messages"
ANTHROPIC_VERSION = "2023-06-01"
ADMIN_KEY_ENV = "ANTHROPIC_ADMIN_KEY"
PAGE_LIMIT = 31
#: Seconds between retries of a 5xx, multiplied by the attempt number.
_RETRY_BACKOFF_S = 2.0

#: Marks the correcting row. Never 'research' or any real component, so
#: a corrected day is always tellable from a recorded one.
BACKFILL_COMPONENT = "backfill_adjustment"


class BackfillError(RuntimeError):
    def __init__(self, message, status_code=None, body=""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class BackfillResult:
    target_date: date
    local_before_cents: Decimal = Decimal("0")
    billed_cents: Decimal = Decimal("0")
    adjustment_cents: Decimal = Decimal("0")
    groups: list = field(default_factory=list)
    applied: bool = False
    reason: str = ""


def fetch_usage_day(target_date: date, admin_key: str | None = None,
                    http_get: Callable[..., httpx.Response] | None = None
                    ) -> list[dict]:
    """One closed day's usage, per API key and model.

    Grouped rather than totalled so the stored evidence can answer WHICH
    key spent the money - the Cost API cannot be grouped by api_key_id
    at all (it accepts only description and workspace_id), so this is
    the only place that distinction exists.
    """
    key = admin_key or os.environ.get(ADMIN_KEY_ENV, "")
    if not key:
        raise BackfillError(
            "no admin key: set the Anthropic ADMIN key (the regular API "
            "key cannot read the usage report)")
    get = http_get or httpx.get
    # TRANSIENT 5xx GETS RETRIED, 4xx NEVER DOES - the same rule
    # TRAPS.md sets for EDGAR, and it earned its place here too: a live
    # sweep of the month hit a single 503 on 2026-08-15, which without
    # this leaves that day uncorrected until the next night.
    resp = None
    for attempt in range(3):
        resp = get(
            USAGE_REPORT_URL,
            headers={"x-api-key": key,
                     "anthropic-version": ANTHROPIC_VERSION},
            params={"starting_at": target_date.isoformat() + "T00:00:00Z",
                    "ending_at": (target_date + timedelta(days=1)).isoformat()
                    + "T00:00:00Z",
                    "bucket_width": "1d", "limit": PAGE_LIMIT,
                    "group_by[]": ["api_key_id", "model"]},
            timeout=30.0)
        if resp.status_code < 500:
            break
        if attempt < 2:
            time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
    if resp.status_code != 200:
        raise BackfillError(f"usage_report answered HTTP {resp.status_code}",
                            status_code=resp.status_code,
                            body=resp.text[:2000])
    try:
        body = resp.json()
    except ValueError:
        raise BackfillError("usage_report body is not JSON",
                            status_code=resp.status_code,
                            body=resp.text[:2000]) from None
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise BackfillError("usage_report shape unexpected: no data list",
                            body=str(body)[:2000])
    if body.get("has_more"):
        # Same refusal as the Cost API adapter: a truncated reference is
        # worse than none, because it looks like agreement.
        raise BackfillError(
            f"usage_report for {target_date} reports has_more=True; "
            "refusing to correct a ledger against a partial day")
    out = []
    for bucket in body["data"]:
        for res in (bucket.get("results") or []):
            if isinstance(res, dict):
                out.append(res)
    return out


def _as_usage(group: dict) -> dict:
    """The Usage API's shape, rewritten as a Messages usage object so the
    SAME price() the live path uses can price it. Deliberately not a
    second pricing implementation - a reconstruction priced by different
    code would prove nothing about the code that does the real work."""
    creation = group.get("cache_creation") or {}
    return {
        "input_tokens": int(group.get("uncached_input_tokens") or 0),
        "output_tokens": int(group.get("output_tokens") or 0),
        "cache_read_input_tokens": int(
            group.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": (
            int(creation.get("ephemeral_5m_input_tokens") or 0)
            + int(creation.get("ephemeral_1h_input_tokens") or 0)),
        "cache_creation": {
            "ephemeral_5m_input_tokens": int(
                creation.get("ephemeral_5m_input_tokens") or 0),
            "ephemeral_1h_input_tokens": int(
                creation.get("ephemeral_1h_input_tokens") or 0)},
        "server_tool_use": {
            "web_search_requests": int(
                (group.get("server_tool_use") or {}).get(
                    "web_search_requests") or 0)},
    }


def price_usage_day(groups: list, target_date: date) -> tuple[Decimal, list]:
    """(total cents, [(model, api_key_id, cents)]) for one day."""
    total, itemised = Decimal("0"), []
    for g in groups:
        model = str(g.get("model") or "")
        if not model:
            raise BackfillError(
                f"a usage group for {target_date} carries no model, so it "
                "cannot be priced - refusing to treat it as free "
                f"({str(g)[:300]})")
        cents = price(make_usage_components(_as_usage(g)), model,
                      on_date=target_date)
        total += cents
        itemised.append((model, str(g.get("api_key_id") or "?"), cents))
    return total, itemised


def local_real_total(conn, target_date: date) -> Decimal:
    """What the ledger recorded that day, EXCLUDING any adjustment this
    module wrote. Excluded so re-running measures the same gap rather
    than a gap it has already closed."""
    rows = conn.execute(
        "SELECT priced_cents FROM cost_events "
        "WHERE date(priced_at) = ? AND priced_cents IS NOT NULL "
        "AND component != ?",
        (target_date.isoformat(), BACKFILL_COMPONENT)).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal("0"))


def backfill_day(conn, target_date: date, *, fetch=None,
                 now=None) -> BackfillResult:
    """Correct one CLOSED day so the ledger sums to what was billed."""
    now = now or datetime.now(timezone.utc)
    if target_date >= now.date():
        raise ValueError(
            "the usage report covers whole days only; today is not closed "
            "yet (TRAPS.md)")
    groups = (fetch or fetch_usage_day)(target_date)
    billed, itemised = price_usage_day(groups, target_date)
    local = local_real_total(conn, target_date)
    result = BackfillResult(target_date=target_date, local_before_cents=local,
                            billed_cents=billed, groups=itemised)
    adjustment = billed - local
    result.adjustment_cents = adjustment
    if adjustment == 0:
        # Still clear any stale adjustment: a day that has since been
        # recorded properly must stop carrying a correction.
        conn.execute("DELETE FROM cost_events WHERE id = ?",
                     (_adjustment_id(target_date),))
        conn.commit()
        result.reason = "the ledger already matches the bill for this day"
        return result

    conn.execute(
        "INSERT OR REPLACE INTO cost_events "
        "(id, raw_usage_json, model, kind, component, priced_cents, "
        " priced_at, api_call_id) VALUES (?,?,?,?,?,?,?,?)",
        (_adjustment_id(target_date),
         json.dumps({"backfill": True, "target_date": target_date.isoformat(),
                     "billed_cents": str(billed),
                     "ledger_cents_before": str(local),
                     "groups": [{"model": m, "api_key_id": k,
                                 "cents": str(c)} for m, k, c in itemised]},
                    sort_keys=True),
         # The model of the largest group, so a repricing pass has
         # something real to work from rather than a placeholder.
         (max(itemised, key=lambda t: t[2])[0] if itemised
          else "claude-sonnet-5"),
         "scheduled", BACKFILL_COMPONENT, str(adjustment),
         datetime.combine(target_date, datetime.min.time(),
                          timezone.utc).isoformat(),
         None))
    conn.commit()
    result.applied = True
    result.reason = (
        f"ledger recorded {local}c, Anthropic billed {billed}c; "
        f"a {adjustment}c adjustment now makes the day sum to the bill")
    return result


def _adjustment_id(target_date: date) -> str:
    """Derived from the date, so running twice replaces rather than
    doubles - the failure mode a random id would have."""
    return f"backfill-{target_date.isoformat()}"


def backfill_range(conn, start: date, end: date, *, fetch=None,
                   now=None) -> list:
    """Every closed day in [start, end]. Never raises out: one day that
    cannot be fetched must not abandon the rest of the month."""
    now = now or datetime.now(timezone.utc)
    out, day = [], start
    while day <= end:
        if day >= now.date():
            break
        try:
            out.append(backfill_day(conn, day, fetch=fetch, now=now))
        except Exception as exc:            # noqa: BLE001 - reported, not raised
            out.append(BackfillResult(
                target_date=day,
                reason=f"could not be corrected: {type(exc).__name__}: {exc}"))
        day += timedelta(days=1)
    return out
