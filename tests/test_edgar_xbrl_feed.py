"""The graded earnings-drift arm needed a live feed. It had none.

OWNER-ASKED 2026-08-30: "ensure it is made better so it will trade
profitably, feels like its idling too much".

Measured over the four days to 2026-08-30: 363 cycles, 28 research
calls, 0 trades. The bot runs ONE candidate arm - insider clusters -
and on the bake-off that is the worse-graded of the two that were
built:

    arm                       n     hit    mean/trade   maxDD   worst
    A  XBRL earnings drift    84   57.1%     +1.59%      8.8%  -18.5%
    C  insider clusters      203   49.3%     +0.87%     41.2%  -57.4%   <- live

A's hit rate is identical in and out of sample. C's fell from 53.1% to
49.3%, under a coin flip, with five times the drawdown.

`strategies/earnings_drift.py` was fully written, pre-registered and
graded - and nothing fetched the XBRL it needs, so it produced nothing
in production. This module is that feed, and only that feed: it derives
no signal and decides nothing. build_events and build_candidates stay
exactly as they were graded.

THE CAVEAT TRAVELS WITH IT. Neither arm beat SPY over the full range
once costs were applied, and A's out-of-sample n is 84. This is a
better-GRADED source, not a proven-profitable one.

Fully offline: every response is a stub.
"""

import gzip
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.data.sources.edgar_xbrl import (
    FACTS_REFRESH_DAYS, MAX_FETCHES_PER_PASS, refresh_facts,
)
from catalyst.storage import init_db

#: HOUSE RULE 6 does not apply: NOW is injected and every stored
#: timestamp is written relative to it.
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

FACTS = {"cik": 320193, "entityName": "Apple Inc.",
         "facts": {"us-gaap": {"NetIncomeLoss": {"units": {"USD": []}}}}}


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "t.db"))
    yield c
    c.close()


@pytest.fixture
def facts_dir(tmp_path):
    return tmp_path / "facts"


def getter(responses, calls):
    def get(url, headers):
        calls.append(url)
        return responses.pop(0) if responses else Resp(500, text="exhausted")
    return get


class TestItFetchesWhatIsMissing:
    def test_a_company_is_cached_where_build_events_looks(self, conn,
                                                          facts_dir):
        """The layout is earnings_drift.build_events's, unchanged - the
        graded code must not need editing to read this."""
        calls = []
        r = refresh_facts([("AAPL", "0000320193")], facts_dir, conn,
                          http_get=getter([Resp(200, FACTS)], calls), now=NOW)
        assert r.fetched == 1
        path = facts_dir / "AAPL.json.gz"
        assert path.exists()
        assert json.loads(gzip.decompress(path.read_bytes()))["cik"] == 320193

    def test_the_cik_is_zero_padded_into_the_url(self, conn, facts_dir):
        calls = []
        refresh_facts([("AAPL", "320193")], facts_dir, conn,
                      http_get=getter([Resp(200, FACTS)], calls), now=NOW)
        assert calls == [
            "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"]

    def test_several_companies_are_all_fetched(self, conn, facts_dir):
        calls = []
        pairs = [("AAPL", "320193"), ("MSFT", "789019")]
        r = refresh_facts(pairs, facts_dir, conn,
                          http_get=getter([Resp(200, FACTS)] * 2, calls),
                          now=NOW)
        assert r.fetched == 2 and len(calls) == 2


class TestItDoesNotSpendTheSecBudgetTwice:
    def test_a_fresh_cache_is_not_refetched(self, conn, facts_dir):
        calls = []
        refresh_facts([("AAPL", "320193")], facts_dir, conn,
                      http_get=getter([Resp(200, FACTS)], calls), now=NOW)
        r = refresh_facts([("AAPL", "320193")], facts_dir, conn,
                          http_get=getter([Resp(200, FACTS)], calls),
                          now=NOW + timedelta(days=1))
        assert len(calls) == 1, "companyfacts only changes when a company files"
        assert r.already_current == 1 and r.fetched == 0

    def test_a_stale_cache_is_refetched(self, conn, facts_dir):
        calls = []
        refresh_facts([("AAPL", "320193")], facts_dir, conn,
                      http_get=getter([Resp(200, FACTS)], calls), now=NOW)
        refresh_facts([("AAPL", "320193")], facts_dir, conn,
                      http_get=getter([Resp(200, FACTS)], calls),
                      now=NOW + timedelta(days=FACTS_REFRESH_DAYS + 1))
        assert len(calls) == 2

    def test_one_pass_is_bounded(self, conn, facts_dir):
        """A cold start must spread over days, not spend the SEC limit
        in a single cycle - it is shared with every other SEC feed."""
        calls = []
        pairs = [(f"T{i}", str(1000 + i)) for i in range(50)]
        r = refresh_facts(pairs, facts_dir, conn,
                          http_get=getter([Resp(200, FACTS)] * 50, calls),
                          now=NOW)
        assert len(calls) == MAX_FETCHES_PER_PASS
        assert r.considered == 50 and r.fetched == MAX_FETCHES_PER_PASS

    def test_a_company_with_no_xbrl_is_remembered(self, conn, facts_dir):
        """404 is a real answer, not a failure. Recorded so it is not
        re-asked every week for nothing."""
        calls = []
        r = refresh_facts([("SPAC", "999")], facts_dir, conn,
                          http_get=getter([Resp(404)], calls), now=NOW)
        assert r.absent == 1 and r.failed == 0
        r2 = refresh_facts([("SPAC", "999")], facts_dir, conn,
                           http_get=getter([Resp(200, FACTS)], calls),
                           now=NOW + timedelta(days=1))
        assert r2.already_current == 1 and len(calls) == 1


class TestItNeverTakesTheCycleDown:
    def test_a_500_is_counted_with_its_raw_body(self, conn, facts_dir):
        calls = []
        r = refresh_facts([("AAPL", "320193")], facts_dir, conn,
                          http_get=getter([Resp(503, text="upstream down")],
                                          calls), now=NOW)
        assert r.failed == 1 and r.fetched == 0
        assert "upstream down" in r.reasons[0][1], (
            "house rule 3: the raw upstream response beside the failure")

    def test_a_raising_transport_is_counted_not_propagated(self, conn,
                                                           facts_dir):
        def boom(url, headers):
            raise OSError("connection reset")

        r = refresh_facts([("AAPL", "320193")], facts_dir, conn,
                          http_get=boom, now=NOW)
        assert r.failed == 1

    def test_one_bad_company_does_not_stop_the_others(self, conn, facts_dir):
        calls = []
        r = refresh_facts([("BAD", "111"), ("AAPL", "320193")], facts_dir,
                          conn,
                          http_get=getter([Resp(500), Resp(200, FACTS)],
                                          calls), now=NOW)
        assert r.fetched == 1 and r.failed == 1

    def test_a_body_that_is_not_companyfacts_is_refused(self, conn,
                                                        facts_dir):
        """A 200 carrying something else must not be written to the
        cache as though it were facts."""
        calls = []
        r = refresh_facts([("AAPL", "320193")], facts_dir, conn,
                          http_get=getter([Resp(200, {"error": "nope"})],
                                          calls), now=NOW)
        assert r.fetched == 0 and r.failed == 1
        assert not (facts_dir / "AAPL.json.gz").exists()

    def test_a_rate_limit_block_stops_the_whole_pass(self, conn, facts_dir):
        """Every further request extends the timeout (TRAPS.md)."""
        from catalyst.data.sources.edgar_form4 import RateLimitBlocked

        calls = []

        def get(url, headers):
            calls.append(url)
            raise RateLimitBlocked("blocked")

        r = refresh_facts([("A", "1"), ("B", "2"), ("C", "3")], facts_dir,
                          conn, http_get=get, now=NOW)
        assert len(calls) == 1, "it kept asking after being blocked"
        assert "rate-limited" in r.skipped_reason


class TestItRefusesRubbishInput:
    def test_a_ticker_that_is_not_a_string_is_dropped(self, conn, facts_dir):
        calls = []
        r = refresh_facts([(None, "1"), (123, "2"), ("", "3")], facts_dir,
                          conn, http_get=getter([], calls), now=NOW)
        assert r.considered == 0 and calls == []

    def test_an_unusable_cik_is_dropped(self, conn, facts_dir):
        calls = []
        r = refresh_facts([("AAPL", None), ("MSFT", "abc"), ("X", "0")],
                          facts_dir, conn, http_get=getter([], calls),
                          now=NOW)
        assert r.considered == 0 and calls == []

    def test_duplicates_are_asked_once(self, conn, facts_dir):
        calls = []
        r = refresh_facts([("AAPL", "320193"), ("aapl", "320193")],
                          facts_dir, conn,
                          http_get=getter([Resp(200, FACTS)], calls), now=NOW)
        assert r.considered == 1 and len(calls) == 1


class TestEveryZeroExplainsItself:
    """House rule 3, at the level a reader meets it."""

    def test_an_empty_universe_says_why(self, conn, facts_dir):
        r = refresh_facts([], facts_dir, conn, now=NOW)
        assert "Form 4" in r.why_empty()

    def test_an_all_current_pass_says_why(self, conn, facts_dir):
        calls = []
        refresh_facts([("AAPL", "320193")], facts_dir, conn,
                      http_get=getter([Resp(200, FACTS)], calls), now=NOW)
        r = refresh_facts([("AAPL", "320193")], facts_dir, conn,
                          http_get=getter([], calls),
                          now=NOW + timedelta(days=1))
        assert "only changes when a company files" in r.why_empty()

    def test_a_failed_pass_names_the_first_reason(self, conn, facts_dir):
        calls = []
        r = refresh_facts([("AAPL", "320193")], facts_dir, conn,
                          http_get=getter([Resp(503, text="down")], calls),
                          now=NOW)
        assert "every fetch failed" in r.why_empty()

    def test_a_successful_pass_has_nothing_to_explain(self, conn, facts_dir):
        calls = []
        r = refresh_facts([("AAPL", "320193")], facts_dir, conn,
                          http_get=getter([Resp(200, FACTS)], calls), now=NOW)
        assert r.why_empty() == ""
