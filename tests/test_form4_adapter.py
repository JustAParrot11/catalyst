"""Adapter tests: the live feed's filing-level payload must flatten to
exactly the rows the backtest's CSV reader would have produced."""

from datetime import datetime, timedelta, timezone

from catalyst.data import RawEvent
from catalyst.data.form4_adapter import flatten_form4_events
from catalyst.discovery.candidates import build_candidates

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


def filing(accession="0001-26-000001", ticker="ACME", owners=None,
           transactions=None, aff="0", filed="2026-08-05"):
    return RawEvent(
        source="edgar_form4", source_id=accession, fetched_at=NOW,
        payload_raw={
            "source_url": "https://example/x.txt",
            "accession": accession,
            "submission_text": "...",
            "parsed": {
                "accession": accession,
                "issuer_cik": "123", "issuer_name": "Acme",
                "ticker": ticker, "filed_date": filed,
                "owners": owners if owners is not None else [
                    {"cik": "901", "name": "DOE JANE", "role": "officer:CEO"}],
                "transactions": transactions if transactions is not None else [
                    {"table": "non_derivative", "code": "P",
                     "acquired_disposed": "A", "transaction_date": "2026-08-04",
                     "shares": "1000", "price_per_share": "30",
                     "shares_owned_following": "5000",
                     "value_usd": "30000"}],
                "ten_b5_1": {"element": aff, "footnote_mention": False,
                             "plan_flagged": aff in ("1", "true")},
            }})


class TestFlatten:
    def test_purchase_flattens_to_csv_schema(self):
        [row] = flatten_form4_events([filing()])
        p = row.payload_raw
        assert p["symbol"] == "ACME" and p["owner_cik"] == "901"
        assert p["filing_date"] == "2026-08-05"
        assert p["value_usd"] == "30000"
        assert p["aff10b5one"] == "0"
        assert p["trans_code"] == "P"
        assert row.source_id.startswith("0001-26-000001:")

    def test_multi_owner_filing_gets_row_per_owner(self):
        ev = filing(owners=[{"cik": "901", "name": "A", "role": "officer"},
                            {"cik": "902", "name": "B", "role": "director"}])
        rows = flatten_form4_events([ev])
        assert [r.payload_raw["owner_cik"] for r in rows] == ["901", "902"]

    def test_sales_derivatives_and_dispositions_excluded(self):
        ev = filing(transactions=[
            {"table": "non_derivative", "code": "S", "acquired_disposed": "D",
             "shares": "1", "price_per_share": "1", "value_usd": "1"},
            {"table": "derivative", "code": "P", "acquired_disposed": "A",
             "shares": "1", "price_per_share": "1", "value_usd": "1"},
            {"table": "non_derivative", "code": "P", "acquired_disposed": "A",
             "shares": None, "price_per_share": "1", "value_usd": None}])
        assert flatten_form4_events([ev]) == []

    def test_aff_spellings_pass_through_verbatim(self):
        for spelling in ("0", "false", "1", "true"):
            [row] = flatten_form4_events([filing(aff=spelling)])
            assert row.payload_raw["aff10b5one"] == spelling

    def test_end_to_end_feed_to_candidate(self):
        # two distinct insiders, >$50k total, within the cluster window ->
        # exactly one candidate through the REAL discovery path
        events = [
            filing(accession="acc-1", filed="2026-08-04",
                   owners=[{"cik": "901", "name": "DOE", "role": "officer"}]),
            filing(accession="acc-2", filed="2026-08-06",
                   owners=[{"cik": "902", "name": "ROE", "role": "director"}]),
        ]
        flat = flatten_form4_events(events)
        cands = build_candidates(flat, NOW)
        assert len(cands) == 1
        assert cands[0].ticker == "ACME"
        assert cands[0].catalyst_type == "insider_cluster"

    def test_plan_trades_excluded_end_to_end(self):
        events = [
            filing(accession="acc-1", filed="2026-08-04", aff="true",
                   owners=[{"cik": "901", "name": "DOE", "role": "officer"}]),
            filing(accession="acc-2", filed="2026-08-06", aff="true",
                   owners=[{"cik": "902", "name": "ROE", "role": "director"}]),
        ]
        assert build_candidates(flatten_form4_events(events), NOW) == []
