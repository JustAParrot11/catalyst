"""The production Cost API adapter - the piece that could not be built
until an ADMIN key existed (verified live 2026-08-10).

READ-ONLY BY CONSTRUCTION: this module calls exactly two endpoints, both
GET - /v1/organizations/cost_report and (for diagnostics)
/v1/organizations/usage_report/messages. Nothing in this codebase may
ever call an Admin API endpoint that modifies limits, budgets, keys or
settings; the owner's spend limits are theirs alone.

Verified against the real API:
- amounts are decimal strings in CENTS ("525.64452" alongside token
  volumes that price to ~$5-6, not ~$525) - TRAPS.md holds;
- the response is BUCKETS (starting_at/ending_at/results[]), and each
  result carries "amount"; reconcile_day sums rec["amount"], so this
  adapter flattens bucket results into CostApiPage.records;
- has_more/next_page exist at the top level; an explicit limit is
  mandatory (TRAPS.md: the default page size quietly drops days).
"""

import os
from datetime import date, timedelta
from typing import Callable

import httpx

from catalyst.cost.tracker import CostApiPage

COST_REPORT_URL = "https://api.anthropic.com/v1/organizations/cost_report"
ANTHROPIC_VERSION = "2023-06-01"
PAGE_LIMIT = 31            # explicit, always (TRAPS.md)
ADMIN_KEY_ENV = "ANTHROPIC_ADMIN_KEY"


class CostApiError(RuntimeError):
    """Non-200 or unparseable Cost API answer, raw body attached."""

    def __init__(self, message: str, status_code: int | None = None,
                 body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def admin_key_available() -> bool:
    return bool(os.environ.get(ADMIN_KEY_ENV))


def fetch_cost_api_day(
    target_date: date,
    admin_key: str | None = None,
    http_get: Callable[..., httpx.Response] | None = None,
) -> CostApiPage:
    """One CLOSED day's cost records, flattened for reconcile_day.

    The Cost API reports whole days only (TRAPS.md): reconcile_day
    already refuses today; this adapter just fetches faithfully."""
    key = admin_key or os.environ.get(ADMIN_KEY_ENV, "")
    if not key:
        raise CostApiError(
            "no admin key: set the Anthropic ADMIN key (the regular API "
            "key cannot read the Cost API)")
    get = http_get or httpx.get
    start = target_date.isoformat() + "T00:00:00Z"
    end = (target_date + timedelta(days=1)).isoformat() + "T00:00:00Z"
    resp = get(
        COST_REPORT_URL,
        headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
        params={"starting_at": start, "ending_at": end, "limit": PAGE_LIMIT},
        timeout=30.0)
    if resp.status_code != 200:
        raise CostApiError(
            f"cost_report answered HTTP {resp.status_code}",
            status_code=resp.status_code, body=resp.text[:2000])
    try:
        body = resp.json()
    except ValueError:
        raise CostApiError("cost_report body is not JSON",
                           status_code=resp.status_code,
                           body=resp.text[:2000]) from None
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise CostApiError("cost_report shape unexpected: no data list",
                           status_code=resp.status_code,
                           body=str(body)[:2000])

    records: list[dict] = []
    for bucket in body["data"]:
        if not isinstance(bucket, dict):
            raise CostApiError("cost_report bucket is not an object",
                               body=str(bucket)[:500])
        for rec in bucket.get("results") or []:
            if not isinstance(rec, dict) or "amount" not in rec:
                raise CostApiError(
                    "cost_report result lacks an amount - refusing to "
                    "treat an unreadable record as zero",
                    body=str(rec)[:500])
            records.append(rec)
    return CostApiPage(records=records,
                       has_more=bool(body.get("has_more")),
                       raw_response=body)
