"""Live discovery for the promoted bake-off arm: insider-cluster buying.

Turns `edgar_form4` RawEvents into `catalyst_type="insider_cluster"`
Candidates using THE SAME cluster definition as the graded backtest arm,
`catalyst/strategies/insider_cluster.py`. The threshold constants and
symbol-validity rule are IMPORTED from that module, never re-typed here:
if discovery and the backtest ever disagree on what an event is, the
backtest graded a different strategy than the one being traded.
tests/test_discovery.py enforces both the import and behavioural parity
against `insider_cluster.build_cluster_events` on identical input.

Shared definition (values live in strategies/insider_cluster.py):
- open-market Form 4 purchases only (the feed's payload schema is the
  one scripts/fetch_insider_data.py produced for the backtest: rows are
  already TRANS_CODE='P' / TRANS_ACQUIRED_DISP_CD='A' filtered; if a
  payload carries an explicit trans_code, anything != 'P' is dropped);
- 10b5-1 exclusion: aff10b5one in ("1", "true") after strip/lower —
  the exact membership test the backtest ran;
- >= MIN_INSIDERS distinct owner CIKs within CLUSTER_WINDOW_DAYS
  calendar days, combined >= MIN_TOTAL_VALUE_USD;
- one event per issuer per DEDUPE_DAYS (overlaps collapse into the
  first), triggered on FILING_DATE — never TRANS_DATE (Form 4 is due 2
  business days after the trade; TRAPS/DATA-SOURCES).

The liquidity floor (last close >= $5, median dollar volume >= $1M) is
deliberately NOT here: in the backtest it is applied at signal time from
price data, and live it is `risk/`'s spread/market gate. Discovery has
no price feed and must not grow one.

Point-in-time discipline: only filings with filing_date <= as_of are
visible. Candidate.id is a deterministic hash of the cluster's contents
(issuer, symbol, completion date, owner set, total value), so the same
cluster rediscovered on a later run gets the same id and is not
re-researched. (ARCHITECTURE 3.1 says "ULID"; determinism is required
here precisely so re-runs dedupe — recorded as an intentional reading.)

Payload contract (per scripts/fetch_insider_data.py's purchases.csv,
one row per (accession, owner)): issuer_cik, symbol, owner_cik,
filing_date (ISO), trans_date, value_usd, shares, shares_owned_after,
aff10b5one. Optional live enrichments read if present, never required:
owner_name, owner_role, sector, trans_code.

Facts the research prompt needs (who bought, how much, when) travel on
`correlation_tags` as "fact:*" entries — the Candidate shape is frozen
and has no other field for them. correlation.py keys on the sector /
catalyst_type / catalyst_date FIELDS, so fact tags are inert for risk;
research/prompts.py parses them back out via `candidate_facts()`.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from catalyst.data import RawEvent
from catalyst.discovery import Candidate
from catalyst.strategies.insider_cluster import (
    CLUSTER_WINDOW_DAYS,
    DEDUPE_DAYS,
    MIN_INSIDERS,
    MIN_TOTAL_VALUE_USD,
    _valid_symbol,
)

CATALYST_TYPE = "insider_cluster"
FORM4_SOURCE = "edgar_form4"
FACT_TAG_PREFIX = "fact:"


def _parse_date(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_purchase(event: RawEvent) -> dict | None:
    """One payload row -> a normalized purchase, or None if it fails the
    same mechanical filters the backtest's CSV reader applied."""
    p = event.payload_raw
    try:
        # Same membership test as insider_cluster.build_cluster_events;
        # strip/lower mirrors what fetch_insider_data.py did before
        # writing the CSV the backtest read.
        if str(p.get("aff10b5one", "")).strip().lower() in ("1", "true"):
            return None
        trans_code = str(p.get("trans_code", "P")).strip().upper()
        if trans_code != "P":
            return None  # feed normally pre-filters; belt and braces
        symbol = str(p["symbol"]).strip().upper()
        if not _valid_symbol(symbol):
            return None
        filing_date = _parse_date(p["filing_date"])
        if filing_date is None:
            return None
        value = float(p["value_usd"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "issuer": str(p.get("issuer_cik", "")).strip(),
        "symbol": symbol,
        "owner": str(p.get("owner_cik", "")).strip(),
        "filing_date": filing_date,
        "value": value,
        "source_id": event.source_id,
        "sector": str(p.get("sector", "") or "").strip(),
        "owner_name": str(p.get("owner_name", "") or "").strip(),
        "owner_role": str(p.get("owner_role", "") or "").strip(),
    }


def build_candidates(raw_events: list[RawEvent], as_of: datetime) -> list[Candidate]:
    """RawEvent[] -> insider-cluster Candidate[], point-in-time at as_of.

    The clustering loop below is line-for-line the backtest arm's
    build_cluster_events algorithm (sort by filing date per issuer; skip
    triggers inside DEDUPE_DAYS of the last event; look back
    CLUSTER_WINDOW_DAYS from each trigger; count distinct owners and sum
    value) — only the row source differs (RawEvent payloads instead of
    the purchases CSV). tests/test_discovery.py runs both on identical
    input and asserts identical events.
    """
    cutoff = as_of.date()
    by_issuer: dict[str, list[dict]] = {}
    for event in raw_events:
        if event.source != FORM4_SOURCE:
            continue
        row = _parse_purchase(event)
        if row is None:
            continue
        if row["filing_date"] > cutoff:
            continue  # not yet filed at as_of — invisible
        by_issuer.setdefault(row["issuer"], []).append(row)

    out: list[Candidate] = []
    for _issuer, rows in sorted(by_issuer.items()):
        # The backtest sorts by filing date with CSV order breaking ties;
        # CSV order does not exist live, so ties break on stable row
        # content instead. Cluster membership is order-invariant — only
        # which same-day row labels the event could differ, and only if
        # an issuer filed under two symbols on one date.
        rows.sort(key=lambda r: (r["filing_date"], r["owner"], r["value"],
                                 r["symbol"], r["source_id"]))
        last_event: date | None = None
        for row in rows:
            fd = row["filing_date"]
            if last_event and (fd - last_event).days < DEDUPE_DAYS:
                continue
            window = [r for r in rows
                      if timedelta(0) <= fd - r["filing_date"]
                      <= timedelta(days=CLUSTER_WINDOW_DAYS)]
            owners = {r["owner"] for r in window}
            total = sum(r["value"] for r in window)
            if len(owners) >= MIN_INSIDERS and total >= MIN_TOTAL_VALUE_USD:
                out.append(_make_candidate(_issuer, row["symbol"], fd,
                                           window, as_of))
                last_event = fd
    out.sort(key=lambda c: (c.catalyst_date, c.ticker, c.id))
    return out


def _make_candidate(issuer: str, symbol: str, completed: date,
                    window: list[dict], as_of: datetime) -> Candidate:
    owners = sorted({r["owner"] for r in window})
    total = sum(r["value"] for r in window)
    first = min(r["filing_date"] for r in window)
    sector = next((r["sector"] for r in window if r["sector"]), "unknown")

    # Deterministic id from the cluster's contents: the same cluster
    # rediscovered on any later run hashes to the same id.
    basis = "|".join([issuer, symbol, completed.isoformat(),
                      ",".join(owners), f"{total:.2f}"])
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]
    cid = f"insider_cluster-{symbol}-{completed.isoformat()}-{digest}"

    per_owner: dict[str, dict] = {}
    for r in window:
        agg = per_owner.setdefault(r["owner"], {
            "value": 0.0, "last": r["filing_date"],
            "name": r["owner_name"], "role": r["owner_role"]})
        agg["value"] += r["value"]
        agg["last"] = max(agg["last"], r["filing_date"])
        agg["name"] = agg["name"] or r["owner_name"]
        agg["role"] = agg["role"] or r["owner_role"]

    iso_year, iso_week, _ = completed.isocalendar()
    tags = [
        f"sector:{sector}",
        f"type:{CATALYST_TYPE}",
        f"week:{iso_year}-W{iso_week:02d}",
        f"source:{FORM4_SOURCE}",
        f"{FACT_TAG_PREFIX}insiders={len(owners)}",
        f"{FACT_TAG_PREFIX}total_usd={round(total)}",
        f"{FACT_TAG_PREFIX}window={first.isoformat()}..{completed.isoformat()}",
    ]
    for owner in owners:
        agg = per_owner[owner]
        display = agg["name"] or f"CIK {owner}"
        if agg["role"]:
            display += f" ({agg['role']})"
        tags.append(f"{FACT_TAG_PREFIX}buyer={display}"
                    f"|usd={round(agg['value'])}|last={agg['last'].isoformat()}")

    return Candidate(
        id=cid,
        ticker=symbol,
        catalyst_type=CATALYST_TYPE,
        catalyst_date=completed,   # the generating event's date — past at
                                   # discovery, per STRATEGY-PROPOSALS 7.1
        catalyst_date_confidence="confirmed",  # a filing date is a fact
        source_event_ids=tuple(sorted({r["source_id"] for r in window})),
        discovered_at=as_of,
        sector=sector,
        correlation_tags=tuple(tags),
    )


def candidate_facts(candidate: Candidate) -> dict:
    """Parse the "fact:*" correlation tags back into a dict for the
    research prompt: {"insiders", "total_usd", "window", "buyers": [...]}.
    Each buyer entry is {"display", "usd", "last"}."""
    facts: dict = {"insiders": None, "total_usd": None, "window": None,
                   "buyers": []}
    for tag in candidate.correlation_tags:
        if not tag.startswith(FACT_TAG_PREFIX):
            continue
        key, _, value = tag[len(FACT_TAG_PREFIX):].partition("=")
        if key == "buyer":
            rest, _, last = value.rpartition("|last=")
            display, _, usd = rest.rpartition("|usd=")
            facts["buyers"].append(
                {"display": display, "usd": usd, "last": last})
        elif key in facts:
            facts[key] = value
    return facts
