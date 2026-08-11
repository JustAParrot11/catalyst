"""EDGAR full-text discovery, and the conjunction linker over it.

Fully offline: every EDGAR response here is a recorded shape, never a
live call. The live verification that produced these shapes is written
into edgar_fts.py's docstring with the date it was measured.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from catalyst.data import RawEvent
from catalyst.data.sources import edgar_fts as fts
from catalyst.discovery.links import (
    NOTABLE, Link, explain, find_links, link_summary, signals_from_events,
)


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)


def hit(ticker="ACME", adsh="0001-26-1", filed="2026-08-04", sic="2834",
        items=("2.02",), name="Acme Corp"):
    return {"_source": {
        "display_names": [f"{name}  ({ticker})  (CIK 0001234567)"],
        "adsh": adsh, "file_date": filed, "root_forms": ["8-K"],
        "file_type": "EX-99.1", "items": list(items), "sics": [sic],
        "period_ending": filed,
    }}


def page(hits, total=None):
    return {"hits": {"total": {"value": total if total is not None else len(hits),
                               "relation": "eq"},
                     "hits": hits}}


class NoWait(fts.RateLimiter):
    """A pacer that records but never sleeps, so tests run instantly and
    never touch the shared process-wide one."""

    def __init__(self):
        super().__init__(5.0, monotonic=lambda: 0.0, sleep=lambda s: None)


# ---------------------------------------------------------------- parsing


class TestParsingWhatEdgarActuallyReturns:
    def test_display_name_yields_ticker_name_and_cik(self):
        got = fts.parse_display_name(
            "Harmony Biosciences Holdings, Inc.  (HRMY)  (CIK 0001802665)")
        assert got["ticker"] == "HRMY"
        assert got["name"] == "Harmony Biosciences Holdings, Inc."
        assert got["cik"] == "1802665"

    def test_a_preferred_share_family_resolves_to_the_common_stock(self):
        """Live shape: "AGM, AGM-A, AGM-PD, AGM-PE, ...". That is one
        company with seven listed securities, not seven companies, and
        only the common stock is worth trading."""
        got = fts.parse_display_name(
            "FEDERAL AGRICULTURAL MORTGAGE CORP  (AGM, AGM-A, AGM-PD)  "
            "(CIK 0000845877)")
        assert got["ticker"] == "AGM"
        assert got["all_tickers"] == ["AGM", "AGM-A", "AGM-PD"]

    def test_a_filer_with_no_ticker_is_not_tradeable_and_is_dropped(self):
        """Funds, trusts and individuals file too. Carrying them would
        inflate every count on the funnel with rows that can never
        become a trade."""
        got = fts.parse_display_name("SOME TRUST  (CIK 0000999999)")
        assert got["ticker"] == ""
        q = fts.QUERIES[0]
        raw = {"_source": {"display_names": ["SOME TRUST  (CIK 0000999999)"],
                           "adsh": "x"}}
        assert fts.hit_to_event(raw, q, datetime.now(timezone.utc)) is None

    def test_an_unreadable_filer_keeps_its_raw_text(self):
        got = fts.parse_display_name("nonsense with no parens")
        assert got["raw"] == "nonsense with no parens"
        assert got["ticker"] == ""


# ---------------------------------------------------------------- fetching


class TestFetchingHonestly:
    def _get(self, pages):
        calls = []

        def http_get(url, headers, params):
            calls.append((url, headers, params))
            return FakeResponse(pages[min(len(calls) - 1, len(pages) - 1)])

        return http_get, calls

    def test_a_search_becomes_ticker_attributed_events(self):
        http_get, calls = self._get([page([hit("HRMY"), hit("BBIO", "0002-26-1")])])
        res = fts.fetch_events(date(2026, 7, 21), date(2026, 8, 11),
                               http_get=http_get, queries=[fts.QUERIES[1]],
                               limiter=NoWait())
        assert [e.payload_raw["ticker"] for e in res.events] == ["HRMY", "BBIO"]
        assert res.events[0].payload_raw["catalyst_type"] == "fda_decision"
        assert res.events[0].payload_raw["sic"] == "2834"
        assert res.requests_made == 1

    def test_every_request_carries_a_user_agent(self):
        """No User-Agent is a 403 block page, not data - and it looks
        like an outage rather than a mistake."""
        http_get, calls = self._get([page([hit()])])
        fts.fetch_events(date(2026, 7, 21), date(2026, 8, 11),
                         http_get=http_get, queries=[fts.QUERIES[0]],
                         limiter=NoWait())
        assert calls, "no request was made at all"
        for _url, headers, _params in calls:
            assert "User-Agent" in headers and headers["User-Agent"].strip()

    def test_every_request_spends_from_the_sec_pacer(self):
        """efts.sec.gov shares the 10 req/s per-IP budget with every
        other SEC API. A source that paced itself separately would sit
        on top of the feed's rate, not inside it."""
        pacer = NoWait()
        http_get, _ = self._get([page([hit()])])
        fts.fetch_events(date(2026, 7, 21), date(2026, 8, 11),
                         http_get=http_get, queries=[fts.QUERIES[0]],
                         limiter=pacer)
        assert pacer.acquisitions == 1

    def test_the_same_filing_matching_two_queries_is_two_claims(self):
        """One 8-K can mention both a readout and a PDUFA date. Those
        are different claims about it, so they must not collapse into
        one event - but the SAME query twice must."""
        http_get, _ = self._get([page([hit(adsh="SAME")])])
        res = fts.fetch_events(date(2026, 7, 21), date(2026, 8, 11),
                               http_get=http_get, limiter=NoWait(),
                               queries=[fts.QUERIES[1], fts.QUERIES[2]])
        assert len(res.events) == 2
        assert len({e.source_id for e in res.events}) == 2
        res2 = fts.fetch_events(date(2026, 7, 21), date(2026, 8, 11),
                                http_get=http_get, limiter=NoWait(),
                                queries=[fts.QUERIES[1], fts.QUERIES[1]])
        assert len(res2.events) == 1, "the same claim was counted twice"

    def test_truncation_is_reported_never_silent(self):
        """A discovery pass that quietly stopped early looks exactly
        like a quiet market."""
        http_get, _ = self._get([page([hit()], total=500)])
        res = fts.fetch_events(date(2026, 7, 21), date(2026, 8, 11),
                               http_get=http_get, queries=[fts.QUERIES[0]],
                               limiter=NoWait(), max_hits_per_query=1)
        assert res.per_query[0]["truncated"] is True
        assert res.per_query[0]["reported_total"] == 500

    def test_paging_stops_before_the_hard_result_window(self):
        """`from` past 10,000 is a 400 from Elasticsearch, not an empty
        page. A caller that pages blindly walks straight into it."""
        http_get, calls = self._get([page([hit(adsh=f"a{i}") for i in range(100)],
                                          total=50_000)])
        fts.fetch_events(date(2026, 7, 21), date(2026, 8, 11),
                         http_get=http_get, queries=[fts.QUERIES[0]],
                         limiter=NoWait(), max_hits_per_query=50_000)
        offsets = [p.get("from", 0) for _u, _h, p in calls]
        assert max(offsets) < fts.MAX_RESULT_WINDOW, offsets

    def test_a_403_block_page_is_not_read_as_zero_results(self):
        """House rule 3. HTML where JSON was expected is the shape of a
        block, and reporting it as "no hits" hides an IP problem."""
        def http_get(url, headers, params):
            return FakeResponse("<!DOCTYPE html><html>blocked</html>",
                                status_code=200)

        res = fts.fetch_events(date(2026, 7, 21), date(2026, 8, 11),
                               http_get=http_get, queries=[fts.QUERIES[0]],
                               limiter=NoWait())
        assert res.events == []
        assert res.errors and "not JSON" in res.errors[0]["error"]
        assert "blocked" in res.errors[0]["raw_text"]

    def test_one_broken_query_does_not_lose_the_others(self):
        seen = []

        def http_get(url, headers, params):
            seen.append(params["q"])
            if len(seen) == 1:
                return FakeResponse("not json at all")
            return FakeResponse(page([hit()]))

        res = fts.fetch_events(date(2026, 7, 21), date(2026, 8, 11),
                               http_get=http_get, limiter=NoWait(),
                               queries=list(fts.QUERIES[:3]))
        assert len(res.errors) == 1
        assert len(res.events) >= 1, "a single bad query took the whole pass"

    def test_a_backwards_window_is_refused(self):
        with pytest.raises(ValueError, match="before"):
            fts.fetch_events(date(2026, 8, 11), date(2026, 7, 21),
                             http_get=lambda *a: None, limiter=NoWait())

    def test_every_query_declares_what_a_hit_MEANS(self):
        """A query whose meaning is ambiguous should not be in the table:
        catalyst_type is what the risk engine keys its assumptions off."""
        for q in fts.QUERIES:
            assert q.catalyst_type.strip()
            assert q.phrase.strip()
            assert q.key.strip()
        assert len({q.key for q in fts.QUERIES}) == len(fts.QUERIES)


# ------------------------------------------------------------------- links


def ev(ticker, catalyst, source="edgar_fts", when="2026-08-04", sid=None,
       sic="2834"):
    return RawEvent(
        source=source, source_id=sid or f"{ticker}-{catalyst}",
        fetched_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        payload_raw={"ticker": ticker, "catalyst_type": catalyst,
                     "filed_date": when, "sic": sic})


class TestConjunctions:
    def test_two_kinds_on_one_ticker_is_a_link(self):
        links = find_links([ev("ACME", "earnings"),
                            ev("ACME", "insider_cluster", source="edgar_form4")])
        assert len(links) == 1
        assert links[0].ticker == "ACME"
        assert links[0].kinds == ("earnings", "insider_cluster")
        assert links[0].is_conjunction

    def test_one_kind_many_times_is_NOT_a_link(self):
        """Ten Form 4 rows from one cluster are ONE kind of evidence, not
        ten. Counting rows instead of kinds would make every insider
        cluster look like a conjunction with itself."""
        rows = [ev("ACME", "insider_cluster", sid=f"r{i}") for i in range(10)]
        assert find_links(rows) == []
        assert len(find_links(rows, min_kinds=1)) == 1

    def test_the_owners_example_reads_as_english(self):
        """The ask, verbatim: "it found company x is expected to show Q4
        reports and they are looking promising"."""
        links = find_links([ev("ACME", "earnings"),
                            ev("ACME", "insider_cluster", source="edgar_form4")])
        why = links[0].why
        assert "earnings call has been scheduled" in why
        assert "insiders bought" in why
        assert "buying into a print they could see coming" in why

    def test_a_same_feed_conjunction_says_it_is_weaker(self):
        """Measured live 2026-08-11: 20 of 30 conjunctions were
        clinical_readout + fda_decision, both from EDGAR full-text
        search, because one biotech 8-K routinely mentions both. That is
        one company saying one thing, not two observations agreeing."""
        links = find_links([ev("BBIO", "clinical_readout"),
                            ev("BBIO", "fda_decision")])
        assert "same feed" in links[0].why
        assert len(links[0].sources) == 1

    def test_a_cross_feed_conjunction_says_it_is_stronger(self):
        links = find_links([ev("ACME", "earnings"),
                            ev("ACME", "insider_cluster", source="edgar_form4")])
        assert "independent feeds" in links[0].why

    def test_sector_concentration_is_reported_as_the_warning_it_is(self):
        """The brief: "Four small-cap biotech binaries all resolving the
        same fortnight is a single wager on biotech sentiment, not four
        independent trades." A link finder over filings finds biotech."""
        events = []
        for t in ("AAA", "BBB", "CCC", "DDD"):
            events += [ev(t, "clinical_readout", sic="2834"),
                       ev(t, "fda_decision", sic="2834")]
        events += [ev("ZZZ", "earnings", sic="7372"),
                   ev("ZZZ", "guidance", sic="7372")]
        summary = link_summary(find_links(events))
        assert summary["largest_sector"] == "2834"
        assert summary["largest_sector_n"] == 4
        assert summary["sector_concentration"] == 0.8
        assert "ONE bet, not 4" in summary["warning"]

    def test_no_warning_when_the_links_are_genuinely_spread(self):
        """A warning that is always on is not a warning."""
        events = []
        for t, sic in (("AAA", "2834"), ("BBB", "7372"), ("CCC", "6021")):
            events += [ev(t, "earnings", sic=sic), ev(t, "guidance", sic=sic)]
        assert link_summary(find_links(events))["warning"] == ""

    def test_an_unknown_pairing_is_called_a_coincidence_not_a_signal(self):
        summary = link_summary(find_links(
            [ev("ACME", "merger_vote"), ev("ACME", "clinical_readout")]))
        pair = summary["pairs"][0]
        assert "no established meaning" in pair["meaning"]

    def test_ordering_is_deterministic(self):
        """A list that reshuffles between refreshes is one nobody
        trusts."""
        events = [ev("BBB", "earnings"), ev("BBB", "guidance"),
                  ev("AAA", "earnings"), ev("AAA", "guidance"),
                  ev("CCC", "earnings"), ev("CCC", "guidance"),
                  ev("CCC", "fda_decision")]
        first = [l.ticker for l in find_links(events)]
        for _ in range(5):
            assert [l.ticker for l in find_links(list(reversed(events)))] == first

    def test_a_form4_row_without_a_catalyst_type_still_links(self):
        """The Form 4 feed predates the catalyst_type stamp. Its rows are
        insider purchases by construction, and losing them would make
        the most valuable half of every conjunction disappear."""
        row = RawEvent(source="edgar_form4", source_id="x",
                       fetched_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
                       payload_raw={"symbol": "ACME", "trans_date": "2026-08-01"})
        links = find_links([row, ev("ACME", "earnings")])
        assert links and "insider_cluster" in links[0].kinds

    def test_an_event_with_no_ticker_cannot_link_to_anything(self):
        row = RawEvent(source="x", source_id="y",
                       fetched_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
                       payload_raw={"catalyst_type": "earnings"})
        assert signals_from_events([row]) == {}

    def test_the_explanation_never_predicts_a_price(self):
        """Evidence supports observations. "This will go up" is a
        conclusion evidence alone cannot reach, and the model - not this
        module - is the only thing allowed to form a view at all."""
        banned = ("will rise", "will go up", "will fall", "buy ", "sell ",
                  "target price", "guaranteed", "should trade")
        events = []
        for a, b in NOTABLE:
            events += [ev(f"T{a[:2]}{b[:2]}", a), ev(f"T{a[:2]}{b[:2]}", b)]
        for link in find_links(events):
            low = link.why.lower()
            for phrase in banned:
                assert phrase not in low, f"{link.why!r} contains {phrase!r}"

    def test_the_window_between_signals_is_stated(self):
        links = find_links([ev("ACME", "earnings", when="2026-07-22"),
                            ev("ACME", "guidance", when="2026-08-10")])
        assert "19 day(s) apart" in links[0].why
