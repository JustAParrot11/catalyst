"""Insider candidates get a real sector, so the cluster cap stops
treating unrelated companies as one bet.

WHY THIS MATTERS, measured rather than argued (backtest/harness.py,
out of sample 2024-01..2026-08 against SPY):

    unbounded                          +31.6% excess
    with the exposure bound            +10.4%
    with exposure AND cluster bounds   -20.1%

The cluster bound alone costs 30.5 points, and does it by excluding the
weeks several clusters complete at once - which is when the signal is
strongest. It bound on a DATA GAP: Form 4 payloads carry no sector, so
every insider candidate keyed on "unknown" and they all capped against
each other.

Fully offline (house rule 5): every HTTP call is injected.
"""

import json
from dataclasses import dataclass
from datetime import date

import pytest

from catalyst.data.sources.edgar_company import (
    enrich_form4_sectors, reset_sic_memo, sic_for_cik,
)
from catalyst.discovery.correlation import cluster_key_for
from catalyst.storage import init_db


@dataclass
class Ev:
    payload: dict


@dataclass
class Resp:
    status_code: int
    text: str


@pytest.fixture(autouse=True)
def _clean_memo():
    reset_sic_memo()
    yield
    reset_sic_memo()


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "sic.db"))
    yield conn
    conn.close()


def ok(sic):
    return lambda url, headers: Resp(200, json.dumps({"sic": sic}))


def fail(status):
    return lambda url, headers: Resp(status, "nope")


class TestTheProblemItSolves:
    def test_without_a_sector_unrelated_companies_share_one_cluster_key(self):
        """The defect, stated as a test so it cannot come back quietly.

        A biotech and a bank, both with no sector, resolving the same
        week, produce the SAME cluster key - so the 35% cap treats them
        as a single bet and refuses the second one."""
        biotech = cluster_key_for("", "insider_cluster", date(2026, 8, 20))
        bank = cluster_key_for("", "insider_cluster", date(2026, 8, 20))
        assert biotech == bank == "unknown|insider_cluster|2026-W34"

    def test_with_a_sector_they_are_correctly_told_apart(self):
        biotech = cluster_key_for("2834", "insider_cluster", date(2026, 8, 20))
        bank = cluster_key_for("6021", "insider_cluster", date(2026, 8, 20))
        assert biotech != bank, (
            "two unrelated companies still cap against each other")


class TestTheLookup:
    def test_it_reads_the_sic_from_edgar(self, db):
        assert sic_for_cik("320193", conn=db, http_get=ok("3571")) == "3571"

    def test_it_is_cached_so_the_second_call_makes_no_request(self, db):
        calls = []

        def counting(url, headers):
            calls.append(url)
            return Resp(200, json.dumps({"sic": "2834"}))

        sic_for_cik("111", conn=db, http_get=counting)
        reset_sic_memo()                     # force it past the memo
        sic_for_cik("111", conn=db, http_get=counting)
        assert len(calls) == 1, (
            "EDGAR is rate limited across ALL its APIs; a re-fetch per "
            "cycle is what got this IP blocked before")

    def test_the_cik_is_zero_padded_to_ten_digits(self, db):
        seen = []

        def capture(url, headers):
            seen.append(url)
            return Resp(200, json.dumps({"sic": "2834"}))

        sic_for_cik("320193", conn=db, http_get=capture)
        assert "CIK0000320193.json" in seen[0]

    def test_a_contactable_user_agent_is_sent(self, db):
        seen = {}

        def capture(url, headers):
            seen.update(headers)
            return Resp(200, json.dumps({"sic": "2834"}))

        sic_for_cik("1", conn=db, http_get=capture)
        assert "Catalyst" in seen.get("User-Agent", ""), (
            "without a contactable User-Agent EDGAR answers with a 403 "
            "block page rather than data")


class TestItNeverBreaksDiscovery:
    """Discovery runs unattended. A company whose industry cannot be
    looked up must produce a candidate with an unknown sector - which
    clusters conservatively, exactly as today - never no candidate."""

    @pytest.mark.parametrize("responder", [
        fail(404), fail(500), fail(403),
        lambda url, headers: Resp(200, "not json"),
    ])
    def test_a_failed_lookup_returns_empty_rather_than_raising(self, db, responder):
        assert sic_for_cik("1", conn=db, http_get=responder) == ""

    def test_an_exception_from_the_transport_is_swallowed(self, db):
        def boom(url, headers):
            raise RuntimeError("network on fire")

        assert sic_for_cik("1", conn=db, http_get=boom) == ""

    def test_a_4xx_is_cached_but_a_5xx_is_not(self, db):
        """TRAPS.md: never retry a 4xx, always retry a transient 5xx.
        A 404 is a permanent fact about this company; a 500 is not."""
        sic_for_cik("444", conn=db, http_get=fail(404))
        sic_for_cik("555", conn=db, http_get=fail(500))
        cached = {r[0] for r in db.execute("SELECT cik FROM company_sic")}
        assert "444" in cached and "555" not in cached

    def test_it_works_with_no_database_at_all(self):
        assert sic_for_cik("1", conn=None, http_get=ok("2834")) == "2834"


class TestEnrichment:
    def test_it_fills_in_a_missing_sector(self, db):
        events = [Ev({"issuer_cik": "320193", "sector": ""})]
        enriched, looked = enrich_form4_sectors(events, conn=db,
                                                http_get=ok("3571"))
        assert events[0].payload["sector"] == "3571"
        assert (enriched, looked) == (1, 1)

    def test_it_never_overwrites_a_sector_that_is_already_there(self, db):
        """Enrichment, not correction. Overwriting would make the cluster
        key depend on which code ran last."""
        events = [Ev({"issuer_cik": "1", "sector": "6021"})]
        enrich_form4_sectors(events, conn=db, http_get=ok("2834"))
        assert events[0].payload["sector"] == "6021"

    def test_one_lookup_serves_every_event_for_the_same_company(self, db):
        calls = []

        def counting(url, headers):
            calls.append(url)
            return Resp(200, json.dumps({"sic": "2834"}))

        events = [Ev({"issuer_cik": "7", "sector": ""}) for _ in range(5)]
        enriched, looked = enrich_form4_sectors(events, conn=db,
                                                http_get=counting)
        assert len(calls) == 1 and looked == 1 and enriched == 5

    def test_a_failed_lookup_leaves_the_sector_alone(self, db):
        events = [Ev({"issuer_cik": "1", "sector": ""})]
        enriched, looked = enrich_form4_sectors(events, conn=db,
                                                http_get=fail(500))
        assert events[0].payload["sector"] == ""
        assert (enriched, looked) == (0, 1)

    def test_both_counts_are_returned_so_zero_can_be_explained(self, db):
        """House rule 3. 'enriched 0 of 0' (all cached) and 'enriched 0
        of 12' (every lookup failed) are different facts."""
        assert enrich_form4_sectors([], conn=db, http_get=ok("1")) == (0, 0)

    def test_a_payload_that_is_not_a_dict_is_skipped_not_fatal(self, db):
        events = [Ev(None), Ev({"issuer_cik": "1", "sector": ""})]
        enriched, _ = enrich_form4_sectors(events, conn=db, http_get=ok("2834"))
        assert enriched == 1


class TestTheEffectOnClustering:
    def test_enriched_events_produce_DIFFERENT_cluster_keys(self, db):
        """End to end, and the whole point: two companies that used to
        cap against each other no longer do."""
        pharma = Ev({"issuer_cik": "100", "sector": ""})
        bank = Ev({"issuer_cik": "200", "sector": ""})

        def by_cik(url, headers):
            return Resp(200, json.dumps(
                {"sic": "2834" if "0000000100" in url else "6021"}))

        enrich_form4_sectors([pharma, bank], conn=db, http_get=by_cik)

        when = date(2026, 8, 20)
        assert (cluster_key_for(pharma.payload["sector"], "insider_cluster", when)
                != cluster_key_for(bank.payload["sector"], "insider_cluster", when))

    def test_companies_in_the_SAME_industry_still_cluster_together(self, db):
        """The cap is not being removed. Two biotechs resolving the same
        week are still one bet and must still cap against each other."""
        a = Ev({"issuer_cik": "100", "sector": ""})
        b = Ev({"issuer_cik": "200", "sector": ""})
        enrich_form4_sectors([a, b], conn=db, http_get=ok("2834"))
        when = date(2026, 8, 20)
        assert (cluster_key_for(a.payload["sector"], "insider_cluster", when)
                == cluster_key_for(b.payload["sector"], "insider_cluster", when))
