"""Adapter: edgar_form4's rich live payload -> the flat purchase rows
discovery reads.

Written by the coordinating session because it joins two owned surfaces:
the live feed (data-engineer) emits one RawEvent per FILING with the
full parsed ownershipDocument; discovery/candidates.py (strategy-analyst)
reads the flat purchases.csv schema the BACKTEST was graded on
(scripts/fetch_insider_data.py: one row per (accession, owner) per
non-derivative code-P acquired-A transaction, value = shares x price,
rows missing shares or price dropped).

Parity rules replicated deliberately, not improved:
- the transaction's value is attributed to EVERY reporting owner of the
  filing (that is what the CSV did; changing it live would trade a
  strategy the backtest never graded);
- aff10b5one carries the SEC element's value VERBATIM ("0"/"false"/"1"/
  "true" all occur in the wild - data-engineer measured all four).
  candidates.py's membership test handles the spellings. The feed's
  broader footnote heuristic (ten_b5_1.footnote_mention) is surfaced as
  a separate field for the research prompt, never folded into the
  exclusion - the backtest only had the element.
"""

from catalyst.data import RawEvent

# One open-market purchase above this is treated as upstream garbage.
# The largest real insider buys on record are low tens of millions; the
# backtest's CSV never contained a row within two orders of magnitude of
# this ceiling, so it cannot introduce live/backtest divergence.
MAX_PLAUSIBLE_ROW_VALUE_USD = 100_000_000.0


def flatten_form4_events(feed_events: list[RawEvent]) -> list[RawEvent]:
    """One filing-level RawEvent -> N purchase-row RawEvents in the
    purchases.csv schema. Non-purchase filings flatten to nothing."""
    flat: list[RawEvent] = []
    for ev in feed_events:
        parsed = (ev.payload_raw or {}).get("parsed") or {}
        if not isinstance(parsed, dict):
            continue
        # A malformed entry drops ITSELF, never the rest of the batch: a
        # single non-object owner or transaction used to raise
        # AttributeError and lose every filing in the fetch
        # (stress-tester defect 16). Dropping an owner can only reduce
        # the distinct-insider count, so the cluster floor fails closed.
        owners = [o for o in (parsed.get("owners") or [])
                  if isinstance(o, dict)]
        owner_ciks = [str(o.get("cik", "")).strip() for o in owners] or ["?"]
        ten = parsed.get("ten_b5_1")
        ten = ten if isinstance(ten, dict) else {}
        aff = "" if ten.get("element") is None else str(ten.get("element"))
        row_n = 0
        for tx in parsed.get("transactions") or []:
            if not isinstance(tx, dict):
                continue
            if tx.get("table") != "non_derivative":
                continue
            if str(tx.get("code", "")).strip().upper() != "P":
                continue
            if str(tx.get("acquired_disposed", "")).strip().upper() != "A":
                continue
            if not tx.get("shares") or not tx.get("price_per_share"):
                continue   # the CSV dropped rows missing either
            try:
                row_value = float(tx.get("value_usd") or 0)
            except (TypeError, ValueError):
                continue
            if row_value > MAX_PLAUSIBLE_ROW_VALUE_USD:
                # a single insider purchase claiming >$100M is a parse
                # error or a poisoned filing, and either way it must not
                # clear the cluster's dollar floor by orders of magnitude
                # (stress escalation 8). The raw filing stays verbatim in
                # raw_events; only the flattened row is withheld.
                continue
            for i, (owner_cik, owner) in enumerate(zip(owner_ciks,
                                                       owners or [{}])):
                flat.append(RawEvent(
                    source=ev.source,
                    source_id=f"{ev.source_id}:{row_n}:{i}",
                    fetched_at=ev.fetched_at,
                    payload_raw={
                        "issuer_cik": str(parsed.get("issuer_cik", "")).strip(),
                        "symbol": str(parsed.get("ticker", "") or "").strip().upper(),
                        "owner_cik": owner_cik,
                        "filing_date": parsed.get("filed_date", ""),
                        "trans_date": tx.get("transaction_date", ""),
                        "value_usd": tx.get("value_usd"),
                        "shares": tx.get("shares"),
                        "shares_owned_after": tx.get("shares_owned_following"),
                        "aff10b5one": aff,
                        "trans_code": "P",
                        # live-only enrichments (candidates reads if present)
                        "owner_name": str(owner.get("name", "") or ""),
                        "owner_role": str(owner.get("role", "") or ""),
                        "ten_b5_1_footnote_mention":
                            bool(ten.get("footnote_mention")),
                        "accession": parsed.get("accession", ev.source_id),
                    }))
            row_n += 1
    return flat
