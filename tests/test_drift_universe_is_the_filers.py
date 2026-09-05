"""The drift arm looked for earnings among the companies insiders traded.

OWNER'S 7-DAY LOGIC BUNDLE, 2026-09-05: candidates in the window by
type - insider_cluster 51, earnings 15, merger_vote 12, ... and
earnings_drift ZERO. The arm that graded best on the bake-off (57.1%
out of sample, 8.8% max drawdown) found nothing for a week.

WHY. Its universe was `_issuer_pairs(raw_events)`: the companies in the
Form 4 feed, 141 tickers, and none of them happened to file a 10-Q that
week. A post-earnings strategy whose universe is "companies with recent
insider trades" is looking in the wrong place - the event it trades is
the filing, and the daily filing index lists every one.

THREE PIECES:

  1. edgar_form4.daily_filers reads 10-Q/10-K rows from the SAME index
     file fetch_form4 already downloads for Form 4s, through the same
     cache and pacer.
  2. edgar_xbrl.cik_ticker_map turns the index's CIKs into tickers from
     SEC's own company_tickers.json, cached for a week.
  3. The scheduler puts the filers FIRST in the fetch queue, so a
     company that filed this morning is fetched before the backlog -
     it is the only kind that can become a live candidate today.

AND THE PROMPT. A drift candidate used to fall into the insider-cluster
text and be described as a cluster of purchases that never happened,
then asked whether "these filings" were priced in - the wrong question
for an arm that BUYS AFTER THE MOVE. It has its own brief now.

Fully offline: SEC is a fake, the clock is fixed.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.data.sources import edgar_form4 as f4
from catalyst.data.sources.edgar_xbrl import (
    EARNINGS_FORMS, FILER_LOOKBACK_DAYS, MAX_EVENT_AGE_DAYS,
    TICKER_MAP_FILE, cik_ticker_map, earnings_filer_pairs,
)
from catalyst.discovery import Candidate
from catalyst.research import prompts

DAY = date(2026, 9, 4)
INDEX = "\n".join([
    "Description:           Daily Index of EDGAR Dissemination Feed",
    "Form Type   Company Name                                              CIK         Date Filed  File Name",
    "---------------------------------------------------------------------------------------------------------",
    "10-Q        EMBECTA CORP                                              1872789     20260904    edgar/data/1872789/0001872789-26-000012.txt",
    "4           SMITH JOHN                                                1234567     20260904    edgar/data/1234567/0001234567-26-000001.txt",
    "10-K        ZEBRA NATIONAL BANCORP                                    1747661     20260904    edgar/data/1747661/0001747661-26-000030.txt",
    "10-Q/A      RESTATER INC                                              1111111     20260904    edgar/data/1111111/0001111111-26-000002.txt",
    "8-K         SOMEONE ELSE                                              2222222     20260904    edgar/data/2222222/0002222222-26-000009.txt",
    "",
])


class Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def clean_caches():
    """Module-level caches and the PROCESS-WIDE pacer: a test that
    trips the block path would otherwise leave every later SEC call in
    the suite sleeping out the cooldown."""
    f4._index_cache.clear()
    f4._absent_since.clear()
    f4.reset_sec_pacer()
    yield
    f4._index_cache.clear()
    f4._absent_since.clear()
    f4.reset_sec_pacer()


def fake_sec(calls, status=200, text=INDEX):
    def get(url, headers):
        calls.append(url)
        return Resp(status, text)
    return get


class TestDailyFilersReadsTheOtherHalfOfTheIndex:
    def test_the_10q_and_10k_rows_come_back(self):
        calls = []
        rows = f4.daily_filers(DAY, EARNINGS_FORMS, http_get=fake_sec(calls),
                               now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))
        assert sorted(r.cik for r in rows) == ["1747661", "1872789"]
        assert {r.form_type for r in rows} == {"10-Q", "10-K"}

    def test_amendments_are_not_earnings(self):
        """10-Q/A restates; the signal is the FIRST-filed value."""
        rows = f4.daily_filers(DAY, EARNINGS_FORMS, http_get=fake_sec([]),
                               now=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))
        assert "1111111" not in {r.cik for r in rows}

    def test_a_past_day_is_fetched_once_and_then_cached(self):
        calls = []
        clock = lambda: datetime(2026, 9, 5, tzinfo=timezone.utc)  # noqa: E731
        f4.daily_filers(DAY, EARNINGS_FORMS, http_get=fake_sec(calls), now=clock)
        f4.daily_filers(DAY, EARNINGS_FORMS, http_get=fake_sec(calls), now=clock)
        assert len(calls) == 1, "a final index was downloaded twice"

    def test_today_is_never_cached(self):
        """Today's index is still being written to until the evening."""
        calls = []
        clock = lambda: datetime(2026, 9, 4, 15, tzinfo=timezone.utc)  # noqa: E731
        f4.daily_filers(DAY, EARNINGS_FORMS, http_get=fake_sec(calls), now=clock)
        f4.daily_filers(DAY, EARNINGS_FORMS, http_get=fake_sec(calls), now=clock)
        assert len(calls) == 2

    def test_an_absent_index_is_an_empty_day_asked_again_later_not_now(self):
        calls = []
        get = fake_sec(calls, status=404, text="NoSuchKey")
        assert f4.daily_filers(DAY, EARNINGS_FORMS, http_get=get) == []
        assert f4.daily_filers(DAY, EARNINGS_FORMS, http_get=get) == []
        assert len(calls) == 1, (
            "a weekend index was re-requested inside the recheck window - "
            "that is the SEC budget spent on a file that cannot exist")

    def test_a_rate_limit_block_propagates(self):
        """A block must stop the whole pass, exactly as it does for the
        Form 4 feed - one more request extends the timeout."""
        def blocked(url, headers):
            return Resp(403, "Request Rate Threshold Exceeded")
        with pytest.raises(f4.RateLimitBlocked):
            f4.daily_filers(DAY, EARNINGS_FORMS, http_get=blocked)

    def test_a_transport_failure_is_an_empty_day(self):
        def boom(url, headers):
            raise ConnectionError("no route")
        assert f4.daily_filers(DAY, EARNINGS_FORMS, http_get=boom) == []


class TestTheTickerMap:
    PAYLOAD = {"0": {"cik_str": 1872789, "ticker": "EMBC", "title": "Embecta"},
               "1": {"cik_str": 1747661, "ticker": "ZNB", "title": "Zebra"},
               "2": {"cik_str": 1747661, "ticker": "ZNB-P", "title": "Zebra pref"},
               "3": {"cik_str": "junk", "ticker": "X", "title": "?"}}

    def test_it_maps_cik_to_the_primary_ticker(self, tmp_path):
        m, note = cik_ticker_map(
            tmp_path, http_get=lambda u, h: Resp(200, payload=self.PAYLOAD))
        assert m == {1872789: "EMBC", 1747661: "ZNB"}
        assert note == ""

    def test_it_is_cached_and_not_refetched_inside_a_week(self, tmp_path):
        calls = []
        def get(u, h):
            calls.append(u)
            return Resp(200, payload=self.PAYLOAD)
        cik_ticker_map(tmp_path, http_get=get)
        cik_ticker_map(tmp_path, http_get=get)
        assert len(calls) == 1
        assert (Path(tmp_path) / TICKER_MAP_FILE).exists()

    def test_a_failed_refresh_uses_the_cached_copy_and_says_so(self, tmp_path):
        cik_ticker_map(tmp_path, http_get=lambda u, h: Resp(200, payload=self.PAYLOAD))
        old = datetime.now(timezone.utc) + timedelta(days=30)
        m, note = cik_ticker_map(tmp_path, http_get=lambda u, h: Resp(503, "down"),
                                 now=old)
        assert m[1872789] == "EMBC"
        assert "not refreshed" in note and "503" in note

    def test_no_map_at_all_is_empty_with_a_reason(self, tmp_path):
        m, note = cik_ticker_map(tmp_path, http_get=lambda u, h: Resp(500, "x"))
        assert m == {} and note


class TestFilerPairs:
    def test_pairs_are_ticker_cik_in_filing_order_and_deduplicated(self):
        rows = f4.parse_daily_index(INDEX, forms=EARNINGS_FORMS)
        rows = rows + rows
        pairs = earnings_filer_pairs(rows, {1872789: "EMBC", 1747661: "ZNB"})
        assert pairs == [("EMBC", "1872789"), ("ZNB", "1747661")]

    def test_a_filer_the_map_cannot_name_is_dropped(self):
        rows = f4.parse_daily_index(INDEX, forms=EARNINGS_FORMS)
        assert earnings_filer_pairs(rows, {1872789: "EMBC"}) == [("EMBC", "1872789")]


class TestTheSchedulerLeadsWithTheFilers:
    def _src(self):
        import catalyst.orchestrator.scheduler as sch

        return Path(sch.__file__).read_text()

    def test_it_reads_the_filing_index(self):
        assert "daily_filers(" in self._src()

    def test_filers_come_before_the_form4_footprint(self):
        """Order IS priority: refresh_facts fetches pairs in the order
        given, at most a few a pass, so whoever is first is fetched
        first. A company that filed this morning must be."""
        src = self._src()
        assert "pairs = filer_pairs + " in src, (
            "the filers are not at the head of the fetch queue")

    def test_the_lookback_covers_a_weekend_inside_the_drift_window(self):
        assert 3 <= FILER_LOOKBACK_DAYS < MAX_EVENT_AGE_DAYS


def drift_candidate(**over):
    base = dict(
        id="A-2026-09-04-EMBC-2026-06-30", ticker="EMBC",
        catalyst_type="earnings_drift", catalyst_date=date(2026, 9, 4),
        catalyst_date_confidence="confirmed",
        source_event_ids=("xbrl:EMBC:2026-06-30",),
        discovered_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        sector="3841",
        correlation_tags=("type:earnings_drift", "fact:sue=+2.31",
                          "fact:period_end=2026-06-30",
                          "fact:filed=2026-09-04", "fact:form=10-Q"))
    base.update(over)
    return Candidate(**base)


class TestThePromptKnowsWhichArmItIsJudging:
    def test_a_drift_candidate_is_not_described_as_an_insider_cluster(self):
        text = prompts.render_research_prompt(drift_candidate())
        assert "distinct insiders bought" not in text, (
            "the model is being told about insider purchases that never "
            "happened")
        assert "POST-EARNINGS DRIFT" in text

    def test_it_states_the_surprise_and_the_filing(self):
        text = prompts.render_research_prompt(drift_candidate())
        assert "+2.31" in text and "2026-06-30" in text and "10-Q" in text

    def test_priced_in_is_asked_the_drift_way(self):
        """The arm buys AFTER the move. Asking "has the market consumed
        this" of a stock that has moved gets 'yes' every time, which is
        how 31 of 33 researches in the owner's window came back
        priced_in."""
        text = prompts.render_research_prompt(drift_candidate())
        assert "CONFIRMING the setup" in text
        assert "has the market already consumed these filings" not in text

    def test_the_thesis_question_is_about_the_surprise_not_the_insiders(self):
        text = prompts.render_research_prompt(drift_candidate())
        assert "what these insiders plausibly knew" not in text
        assert "how big the surprise was" in text

    def test_it_says_what_the_arm_is_and_is_not(self):
        text = prompts.render_research_prompt(drift_candidate())
        assert "57%" in text and "not a proven edge" in text
        assert "fell on a beat is the refusal case" in text

    def test_an_insider_candidate_is_unchanged(self):
        c = drift_candidate(id="insider_cluster-X", catalyst_type="insider_cluster",
                            correlation_tags=("type:insider_cluster",))
        text = prompts.render_research_prompt(c)
        assert "distinct insiders bought" in text
        assert "POST-EARNINGS DRIFT" not in text

    def test_the_guardrails_survive_on_the_drift_brief(self):
        text = prompts.render_research_prompt(drift_candidate()).lower()
        for must in ("advisory only", "submit_research_view",
                     "declining is not free", "no order, entry, stop or exit"):
            assert must in text, must


class TestTheCheckCanFail:
    def test_the_old_prompt_shape_is_detectable(self):
        """Render the insider branch for a drift candidate the way it
        used to happen, and confirm the assertion above catches it."""
        c = drift_candidate(catalyst_type="insider_cluster")
        text = prompts.render_research_prompt(c)
        assert "distinct insiders bought" in text

    def test_a_form4_only_universe_is_detectable(self):
        rows = f4.parse_daily_index(INDEX, forms=("4",))
        assert earnings_filer_pairs(rows, {1234567: "SMTH"}) == [("SMTH", "1234567")]
        assert not any(r.form_type in EARNINGS_FORMS for r in rows)
