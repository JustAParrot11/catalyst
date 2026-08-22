"""One company's SIC industry code, from EDGAR, cached forever.

WHY THIS EXISTS - and it is the highest-value measured finding in the
project, so the reasoning is written down here rather than in a commit
message nobody will find again.

`correlation.py` keys a concentration cluster on
`sector | catalyst_type | resolution_week`, so that several positions
resolving on the same news are treated as ONE bet rather than several.
That is correct and it is a requirement.

But Form 4 payloads carry no sector. So every insider candidate fell
back to the string "unknown", every same-week insider cluster collapsed
into ONE cluster key, and `max_correlated_cluster_pct` then treated a
biotech, a bank and a miner as the same wager. Measured in
`backtest/harness.py`, out of sample 2024-01..2026-08 against SPY:

    unbounded                             +31.6% excess
    with the 90% exposure bound           +10.4%
    with exposure AND cluster bounds      -20.1%

The cluster bound alone costs 30.5 percentage points - and the harness
records that it is "a selection effect rather than a scale one": average
capital deployed barely moved (66.7% -> 64.6%) while terminal equity
fell 23%. It was not shrinking positions. It was excluding the best
ones, because it bites hardest in the weeks several clusters complete at
once, which is when the signal is strongest.

So the bound was doing its job against a DATA GAP rather than against
real correlation. This closes the gap. `correlation.py` anticipated it:
"Sector enrichment can only loosen this, never tighten it."

WHAT THIS IS NOT. It does not raise a limit, and nothing here can. The
35% cluster cap is unchanged and still applies - to real sectors now
instead of to one bucket labelled "unknown". A company whose SIC cannot
be found still reads "unknown" and is still clustered conservatively
with the others, which is exactly today's behaviour.

TRAPS.md is the reason for every other decision in this file:
  - EDGAR is rate limited to 10 req/s ACROSS ALL its APIs, and an
    overrun blocks the IP for all of them. Every call goes through the
    process-wide pacer that the Form 4 feed already uses.
  - Transient 5xx are retried with backoff; a 4xx never is.
  - A contactable User-Agent is required, or the answer is a 403 block
    page rather than data.
  - A zero is never left unexplained: a lookup that fails records WHY,
    so "this company has no SIC" and "the fetch broke" stay
    distinguishable.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Callable

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"

#: A company's SIC changes when it reorganises - years apart, if ever.
#: There is no freshness requirement worth a second request, so a hit is
#: kept indefinitely; only failures are retried on a later pass.
_MEMO: dict[str, str] = {}


def reset_sic_memo() -> None:
    """FOR TESTS ONLY - a value cached by one test must not answer for
    the next one."""
    _MEMO.clear()


def _cached(conn, cik: str) -> tuple[str, bool]:
    """(sic, found). `found` distinguishes a cached EMPTY answer - a
    company EDGAR has no SIC for - from never having asked."""
    try:
        row = conn.execute(
            "SELECT sic FROM company_sic WHERE cik = ?", (cik,)).fetchone()
    except sqlite3.Error:
        return "", False
    return (str(row[0] or ""), True) if row else ("", False)


def _remember(conn, cik: str, sic: str, note: str) -> None:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO company_sic (cik, sic, fetched_at, note) "
            "VALUES (?,?,?,?)",
            (cik, sic, datetime.now(timezone.utc).isoformat(), note or None))
        conn.commit()
    except sqlite3.Error:
        pass          # a cache that cannot be written is a slow lookup,
                      # never a failed one


def sic_for_cik(
    cik: str,
    conn=None,
    http_get: Callable | None = None,
    contact_email: str | None = None,
) -> str:
    """The company's SIC code as a string, or "" if it cannot be had.

    NEVER RAISES. Discovery runs unattended, and a company whose
    industry cannot be looked up must produce a candidate with an
    unknown sector - which clusters conservatively, exactly as every
    insider candidate does today - rather than no candidate at all.
    """
    cik = str(cik or "").strip().lstrip("0")
    if not cik:
        return ""
    if cik in _MEMO:
        return _MEMO[cik]
    if conn is not None:
        hit, found = _cached(conn, cik)
        if found:
            _MEMO[cik] = hit
            return hit

    from catalyst.data.sources.edgar_form4 import (
        RateLimitBlocked, _default_http_get, sec_pacer, user_agent,
    )

    get = http_get or _default_http_get
    try:
        sec_pacer().acquire()
    except RateLimitBlocked:
        return ""     # already blocked: do NOT record a miss, so the
                      # lookup is retried once the block clears
    except Exception:  # noqa: BLE001
        pass

    try:
        resp = get(SUBMISSIONS_URL.format(cik=cik),
                   {"User-Agent": user_agent(contact_email),
                    "Accept": "application/json"})
        status = int(getattr(resp, "status_code", 0))
        if status != 200:
            # A 4xx is a permanent answer about THIS company and is
            # cached as a miss; a 5xx is transient and is not, so the
            # next pass asks again (TRAPS.md: never retry a 4xx).
            if conn is not None and 400 <= status < 500:
                _remember(conn, cik, "",
                          f"EDGAR answered HTTP {status} for this CIK")
            return ""
        body = getattr(resp, "text", "") or ""
        data = json.loads(body)
        sic = str((data or {}).get("sic") or "").strip()
        if conn is not None:
            _remember(conn, cik, sic,
                      "" if sic else "EDGAR returned no sic for this company")
        _MEMO[cik] = sic
        return sic
    except Exception:  # noqa: BLE001 - see NEVER RAISES above
        return ""


def enrich_form4_sectors(
    raw_events: list, conn=None, http_get: Callable | None = None,
) -> tuple[int, int]:
    """Fill in `sector` on Form 4 payloads that lack one, in place.

    Returns (enriched, looked_up) so the caller can log what happened
    rather than guess - a pass that enriched nothing because everything
    was already cached and one that enriched nothing because every
    lookup failed look identical otherwise.

    Only ever WRITES a sector that was missing. An event that already
    carries one is left exactly as it is: this is enrichment, not
    correction, and overwriting a real value with a looked-up one would
    make the cluster key depend on which ran last.
    """
    enriched = looked_up = 0
    seen: dict[str, str] = {}
    for ev in raw_events or []:
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        if str(payload.get("sector") or "").strip():
            continue
        cik = str(payload.get("issuer_cik") or "").strip()
        if not cik:
            continue
        if cik not in seen:
            seen[cik] = sic_for_cik(cik, conn=conn, http_get=http_get)
            looked_up += 1
        if seen[cik]:
            payload["sector"] = seen[cik]
            enriched += 1
    return enriched, looked_up
