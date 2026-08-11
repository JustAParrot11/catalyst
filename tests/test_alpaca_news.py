"""The news feed, and the classification that lets it join a link.

Fully offline. Every headline below is a real shape observed live on
2026-08-11; the defects two of them caused are recorded beside them.
"""

import json
from datetime import date, datetime, timezone

import pytest

from catalyst.data.sources import alpaca_news as news


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def item(id="1", symbols=("ACME",), headline="Something happened",
         summary="", created="2026-08-10T12:00:00Z"):
    return {"id": id, "symbols": list(symbols), "headline": headline,
            "summary": summary, "created_at": created,
            "source": "benzinga", "url": "https://x", "author": "a"}


def page(items, token=None):
    return {"news": items, "next_page_token": token}


def getter(pages):
    calls = []

    def http_get(url, headers, params):
        calls.append(params)
        return FakeResponse(pages[min(len(calls) - 1, len(pages) - 1)])

    return http_get, calls


KEY = {"alpaca_key": "PKFAKE", "alpaca_secret": "SECFAKE"}


class TestClassificationDefectsFoundByRunningIt:
    def test_an_eps_miss_beside_a_sales_beat_is_not_good_news(self):
        """LIVE DEFECT 2026-08-11. Benzinga's format carries both verbs
        in one headline: "EPS $(0.57) Misses $(0.51) Estimate, Sales
        $9.5M Beats $8.9M". A loose 'beats? .*estimate' matched the
        SALES clause and stamped an EPS miss as +1 - seen on SCYX, AMPY,
        PAL and CJT. A sentiment read that calls a miss a beat is worse
        than no sentiment read at all."""
        catalyst, hint, _key = news.classify(
            "SCYNEXIS Q2 Adj. EPS $(0.57) Misses $(0.51) Estimate, "
            "Sales $9.5M Beats $8.9M Estimate")
        assert catalyst == "earnings_result"
        assert hint == -1, "an EPS miss was read as good news"

    def test_a_genuine_beat_is_still_positive(self):
        """The fix must not simply stop matching beats."""
        _c, hint, _k = news.classify(
            "Red Violet Q2 Adj. EPS $0.50 Beats $0.33 Estimate, Sales $22M")
        assert hint == +1

    def test_a_headline_with_both_directions_is_not_directional(self):
        """Picking whichever pattern sits higher in the table would turn
        the table's ORDER into a market view."""
        _c, hint, key = news.classify("Widget Co EPS Beats and EPS Misses")
        assert hint == 0
        assert key.endswith("+mixed")

    def test_dilution_outranks_an_earnings_beat(self):
        """Observed live on COGT the same day: "Files Prospectus for
        At-The-Market Offering" alongside "Q2 EPS Beats". A naive read
        sees good news; the offering is what moves the share count."""
        catalyst, hint, _k = news.classify(
            "Cogent Biosciences Files Prospectus for At-The-Market "
            "Offering Of Up To $300M")
        assert catalyst == "dilution" and hint == -1

    def test_a_failed_trial_is_not_read_as_a_readout_happening(self):
        catalyst, hint, _k = news.classify(
            "Acme Fails To Meet Primary Endpoint In Phase 3 Trial")
        assert catalyst == "clinical_readout" and hint == -1

    def test_an_unmatched_headline_is_neutral_news_not_a_guess(self):
        catalyst, hint, key = news.classify("Acme opens a new office")
        assert (catalyst, hint, key) == ("news", 0, "")

    def test_the_summary_is_only_a_fallback(self):
        """A summary often recaps unrelated background - "shares fell
        after the company, which last year raised guidance, ..." - and
        matching that attributes the wrong event to the story."""
        _c, hint, key = news.classify(
            "Acme downgraded by Baird",
            "The company, which raised guidance last year, ...")
        assert key == "analyst_down" and hint == -1


class TestTheSymbolsTrap:
    def test_an_empty_symbol_list_is_refused_rather_than_sent(self):
        """TRAPS.md: an empty symbol list is a filter matching NOTHING,
        not "everything" - verified live, it returns 0 items and looks
        exactly like a quiet news day."""
        with pytest.raises(ValueError, match="NOTHING"):
            news.fetch_events(date(2026, 8, 8), symbols=[], **KEY)

    def test_omitting_symbols_sends_no_symbols_param_at_all(self):
        """Verified live: omitting it returns the firehose (50 items, 48
        distinct symbols). Sending symbols="" instead returns zero. The
        difference is the whole discovery capability."""
        http_get, calls = getter([page([item()])])
        news.fetch_events(date(2026, 8, 8), symbols=None,
                          http_get=http_get, **KEY)
        assert "symbols" not in calls[0]

    def test_named_symbols_are_sent_for_enrichment(self):
        http_get, calls = getter([page([item()])])
        news.fetch_events(date(2026, 8, 8), symbols=["ACME", "BBIO"],
                          http_get=http_get, **KEY)
        assert calls[0]["symbols"] == "ACME,BBIO"


class TestFetching:
    def test_news_becomes_ticker_attributed_events(self):
        http_get, _ = getter([page([item(symbols=("ACME",),
                                         headline="Acme upgraded by Baird")])])
        res = news.fetch_events(date(2026, 8, 8), http_get=http_get, **KEY)
        assert len(res.events) == 1
        payload = res.events[0].payload_raw
        assert payload["ticker"] == "ACME"
        assert payload["catalyst_type"] == "analyst_action"
        assert payload["direction_hint"] == +1

    def test_a_story_about_two_tickers_becomes_two_events(self):
        http_get, _ = getter([page([item(symbols=("AAA", "BBB"))])])
        res = news.fetch_events(date(2026, 8, 8), http_get=http_get, **KEY)
        assert {e.payload_raw["ticker"] for e in res.events} == {"AAA", "BBB"}

    def test_a_wire_roundup_is_dropped_rather_than_credited_to_everyone(self):
        """LIVE: "Earnings Volatility Watch: Applied Materials and 10
        Other Stocks" carried 14 tickers. Attributing it to all of them
        lets one wire story manufacture fourteen conjunctions."""
        many = tuple(f"T{i}" for i in range(14))
        http_get, _ = getter([page([item(symbols=many)])])
        res = news.fetch_events(date(2026, 8, 8), http_get=http_get, **KEY)
        assert res.events == []
        assert res.items_seen == 1, "it should still be COUNTED as seen"

    def test_a_foreign_listing_is_dropped(self):
        """LIVE: "TSX:CJT". This is a US-equities cash account, so a
        Toronto listing can never become an order - carrying it would
        put unfillable candidates in the funnel."""
        http_get, _ = getter([page([item(symbols=("TSX:CJT",))])])
        assert news.fetch_events(date(2026, 8, 8), http_get=http_get,
                                 **KEY).events == []

    def test_pagination_follows_the_token_and_dedups(self):
        http_get, calls = getter([
            page([item(id="1")], token="tok"),
            page([item(id="1"), item(id="2")], token=None),
        ])
        res = news.fetch_events(date(2026, 8, 8), http_get=http_get, **KEY)
        assert res.items_seen == 2, "a repeated id was counted twice"
        assert calls[1]["page_token"] == "tok"

    def test_paging_is_bounded_and_says_when_it_truncated(self):
        """A discovery pass that quietly stopped early looks exactly like
        a quiet news day."""
        http_get, calls = getter([page([item(id="x")], token="always")])
        res = news.fetch_events(date(2026, 8, 8), http_get=http_get,
                                max_pages=3, **KEY)
        assert len(calls) == 3
        assert res.truncated is True

    def test_a_non_200_keeps_the_body_and_does_not_raise(self):
        """The trading loop must survive a feed outage, and house rule 3
        wants the raw answer beside the zero."""
        def http_get(url, headers, params):
            return FakeResponse("upstream unavailable", status_code=503)

        res = news.fetch_events(date(2026, 8, 8), http_get=http_get, **KEY)
        assert res.events == []
        assert "503" in res.error and "upstream unavailable" in res.error

    def test_an_empty_result_still_prints_its_own_payload(self):
        http_get, _ = getter([page([])])
        res = news.fetch_events(date(2026, 8, 8), http_get=http_get, **KEY)
        assert res.events == []
        assert res.raw_sample is not None
        assert res.raw_sample["first_page_item_count"] == 0

    def test_credentials_never_reach_the_query_string(self):
        http_get, calls = getter([page([item()])])
        news.fetch_events(date(2026, 8, 8), http_get=http_get, **KEY)
        assert "PKFAKE" not in json.dumps(calls)
        assert "SECFAKE" not in json.dumps(calls)


class TestTheDirectionHintIsOnlyAHint:
    def test_no_pattern_claims_to_size_anything(self):
        for pattern in news.PATTERNS:
            assert pattern.hint in (-1, 0, 1), (
                "a hint is a sign, never a magnitude - a magnitude "
                "invites arithmetic and the model never sizes anything")

    def test_every_pattern_declares_a_catalyst_type(self):
        for pattern in news.PATTERNS:
            assert pattern.catalyst_type.strip()
            assert pattern.key.strip()
        assert len({p.key for p in news.PATTERNS}) == len(news.PATTERNS)

    def test_dilution_and_distress_are_ordered_before_the_soft_signals(self):
        """A company announcing an offering on the day it beats earnings
        is diluting. Order in the table is what decides which of the two
        a headline is recorded as, so it is pinned."""
        keys = [p.key for p in news.PATTERNS]
        for hard in ("offering", "going_concern", "trial_fail"):
            for soft in ("earnings_beat", "analyst_up"):
                assert keys.index(hard) < keys.index(soft), (
                    f"{hard} must outrank {soft}")
