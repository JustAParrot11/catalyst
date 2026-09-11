"""The card quoted Claude's reading of the evidence, never the evidence.

OWNER-ASKED 2026-09-11: "more detail, link the different articles from
the news, i feel we're heavily looking at insider trades not just claude
spotting potential. Edit this page more to be more fluid and read in
order of process. Also touch on the is it mainly looking at insider
trades or can we get it to do even more agentic research to find very
lucrative trades".

THREE THINGS, AND THE THIRD IS ABOUT THE SYSTEM RATHER THAN THE PAGE.

1. THE EVIDENCE WAS NEVER SHOWN. The card carried the thesis, the
   invalidation and the priced-in call - all of them Claude's READING of
   the sources - and not one link to a source. So the brief's own test
   ("someone who was not there can read a single trade and understand
   why it was made") could only ever be met on trust. The rows existed:
   `raw_events.payload_raw` has carried the news `url` since the feed
   was written, and the decision page has read them since August. They
   had simply never reached the trade card.

2. THE PAGE DID NOT READ IN PROCESS ORDER. It went summary -> numbers ->
   chart -> why -> Claude's view -> size, which is roughly the order the
   code was written in. Seven numbered steps now, in the order the trade
   actually happened.

3. "IS IT MAINLY INSIDER TRADES?" WAS UNANSWERABLE ON THE PAGE THAT
   EXISTS TO ANSWER IT. The origin panel showed how far each arm's
   candidates got and nothing about what any of it cost. Measured
   lifetime: 23 of the 25 directional views this system has ever
   produced came from insider clusters, and the hunt has had 2 paid
   research calls. The owner's feeling was correct and the page could
   neither confirm nor deny it.

Fully offline. No calendar dates in any assertion (house rule 6).
"""

import html as _html
import json
import re
import uuid

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from catalyst.dashboard.queries import TradeStory
from catalyst.storage import init_db

CID = "insider_cluster-EMBC-2026-08-13-x"

#: A real Form 4 payload, shaped as edgar_form4.py writes it.
FORM4 = {
    "accession": "0001895262-26-000031",
    "source_url": "https://www.sec.gov/x",
    "parsed": {
        "issuer_name": "Embecta Corp",
        "cik": "0001895262",
        "owners": [{"name": "Bern Richard", "officer_title": "CEO",
                    "role": "officer"}],
        "transactions": [{"table": "non_derivative", "code": "P",
                          "acquired_disposed": "A", "shares": "141000",
                          "price_per_share": "70.96"}],
    },
    "cik": "0001895262",
}
NEWS = {"headline": "Embecta CEO buys $1.2M of stock after Q2 miss",
        "source": "Benzinga",
        "url": "https://example.com/embecta-insider-buy"}
FTS = {"matched_phrase": "material weakness remediated",
       "cik": "0001895262", "accession": "0001895262-26-000044"}


def text(markup):
    return _html.unescape(re.sub("<[^>]+>", " ", markup))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "e.db")
    conn = init_db(path)
    for src, sid, payload in (("edgar_form4", "f4-001", FORM4),
                              ("alpaca_news", "news-991", NEWS),
                              ("edgar_fts", "fts-77", FTS)):
        conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                     (src, sid, "2026-08-13T12:00:00+00:00",
                      json.dumps(payload)))
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (CID, "EMBC", "insider_cluster", "2026-08-13", "confirmed",
                  json.dumps(["f4-001", "news-991", "fts-77"]),
                  "2026-08-13T12:05:00+00:00", "3841", "[]"))
    conn.execute("INSERT INTO candidate_origin VALUES (?,?,?,?)",
                 (CID, "screen", None, "2026-08-13T12:05:00+00:00"))
    call = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO research_calls (id,candidate_id,model,prompt_rendered,"
        "tools_offered,cost_cents,latency_ms,skipped_reason,called_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (call, CID, "m", "p", "[]", "18.2", 900, None,
         "2026-08-13T12:06:00+00:00"))
    conn.execute(
        "INSERT INTO research_call_turns (call_id,turn_index,raw_response,"
        "usage_raw,stop_reason) VALUES (?,?,?,?,?)",
        (call, 0, json.dumps({"content": [
            {"type": "server_tool_use",
             "input": {"query": "Embecta insider buying August 2026"}},
            {"type": "server_tool_use",
             "input": {"query": "Embecta Q2 2026 guidance cut"}}]}),
         "{}", "tool_use"))
    conn.commit()
    conn.close()
    return Db(path)


# ==========================================================================
# 1. The evidence, with links
# ==========================================================================

class TestASourceIsDescribedNotJustIdentified:
    """A source_id is not a description. "f4-001" told the reader
    nothing while the same payload already said the CEO bought 141,000
    shares at $70.96 - the entire reason the candidate exists."""

    def test_a_form_4_purchase_is_read_out_of_the_payload(self):
        assert queries._describe_source(FORM4) == \
            "Bern Richard (CEO) bought 141,000 shares at $70.96"

    def test_A_SALE_IS_NOT_DESCRIBED_AS_A_BUY(self):
        """The one way this could mislead about money."""
        sale = {"parsed": {
            "owners": [{"name": "Smith A", "officer_title": "CFO"}],
            "transactions": [{"code": "S", "acquired_disposed": "D",
                              "shares": "500", "price_per_share": "12"}]}}
        got = queries._describe_source(sale)
        assert "bought" not in got
        assert got == "Form 4 filed by Smith A (CFO)"

    def test_a_headline_is_used_where_there_is_one(self):
        assert queries._describe_source(NEWS) == NEWS["headline"]

    def test_a_full_text_hit_shows_the_phrase_that_matched(self):
        assert queries._describe_source(FTS) == "material weakness remediated"

    def test_unparseable_numbers_produce_no_number(self):
        got = queries._describe_source({"parsed": {
            "owners": [{"name": "X", "role": "director"}],
            "transactions": [{"code": "P", "acquired_disposed": "A",
                              "shares": "NaN", "price_per_share": "x"}]}})
        assert got == "Form 4 filed by X (director)"

    @pytest.mark.parametrize("junk", [
        {}, {"parsed": None}, {"parsed": {"owners": [None]}},
        {"parsed": {"transactions": ["nope"]}}, {"parsed": "a string"},
        {"headline": ""}, {"parsed": {"owners": [], "transactions": []}},
    ])
    def test_it_never_raises_on_a_broken_payload(self, junk):
        assert isinstance(queries._describe_source(junk), str)


class TestTheLinksActuallyOpen:
    def test_news_uses_its_own_url(self):
        assert queries._source_link("alpaca_news", NEWS) == NEWS["url"]

    def test_a_filing_link_is_derived_from_the_accession(self):
        got = queries._source_link("edgar_form4", FORM4)
        assert got.startswith("https://www.sec.gov/Archives/edgar/data/")
        assert "1895262" in got and "000189526226000031" in got

    def test_a_source_with_no_link_gets_no_link_rather_than_a_guess(self):
        assert queries._source_link("x", {"headline": "no url here"}) == ""

    @pytest.mark.parametrize("bad", [
        {"url": "javascript:alert(1)"}, {"url": "ftp://x"},
        {"url": "  "}, {"url": None},
    ])
    def test_only_http_urls_are_accepted(self, bad):
        """A payload is upstream data. A non-http scheme in a href is
        how a feed becomes a script."""
        assert queries._source_link("alpaca_news", bad) == ""


class TestTheCardShowsThemAll:
    def story(self, db):
        return TradeStory(
            ticker="EMBC", candidate_id=CID, origin="screen",
            catalyst_type="insider_cluster",
            sources=queries._candidate_sources(db, CID),
            searches=queries._candidate_searches(db, CID))

    def test_all_three_sources_reach_the_card(self, db):
        html = panels._evidence(self.story(db), "tr", 0)
        assert "Bern Richard (CEO) bought 141,000 shares" in html
        assert "Embecta CEO buys $1.2M" in html
        assert "material weakness remediated" in html

    def test_each_one_says_which_feed_it_came_from(self, db):
        html = panels._evidence(self.story(db), "tr", 0)
        assert "SEC Form 4 (insider purchase)" in html
        assert "news" in html
        assert "SEC full-text search" in html

    def test_the_news_row_carries_its_publisher(self, db):
        html = panels._evidence(self.story(db), "tr", 0)
        assert "Benzinga" in html

    def test_every_link_is_safe_to_click(self, db):
        """The dashboard holds an access code; it must not hand a window
        handle to a news site."""
        html = panels._evidence(self.story(db), "tr", 0)
        anchors = re.findall(r"<a [^>]*>", html)
        assert anchors
        for a in anchors:
            assert 'rel="noopener noreferrer"' in a, a
            assert 'target="_blank"' in a, a
            assert re.search(r'href="https?://', a), a

    def test_the_caption_counts_what_is_linkable(self, db):
        html = panels._evidence(self.story(db), "tr", 0)
        assert "3 source(s)" in html and "3 with a link" in html

    def test_the_searches_claude_chose_are_shown(self, db):
        html = panels._evidence(self.story(db), "tr", 0)
        assert "Embecta insider buying August 2026" in html
        assert "Embecta Q2 2026 guidance cut" in html
        assert "What Claude searched for (2)" in html

    def test_it_says_search_results_are_not_validated(self, db):
        """The one honest caveat about this section: a web result can
        move the direction and the conviction."""
        html = panels._evidence(self.story(db), "tr", 0)
        assert "NOT validated" in html
        assert "never a wrongly sized one" in html

    def test_no_sources_says_so_rather_than_rendering_nothing(self):
        """House rule 3. An empty list and a broken query must not look
        identical."""
        html = panels._evidence(TradeStory(ticker="X"), "tr", 0)
        assert "No raw source rows are on record" in html
        assert "aged out of the feed cache" in html

    def test_a_source_with_no_description_still_appears(self, db):
        """"we read this and cannot show it" is not "there was nothing"."""
        st = TradeStory(ticker="X", sources=[
            ("weird_feed", "id-42", "2026-08-13", "", "", "")])
        html = panels._evidence(st, "tr", 0)
        assert "id-42" in html or "no headline" in html

    def test_it_survives_a_database_without_the_tables(self, tmp_path):
        import sqlite3

        path = str(tmp_path / "bare.db")
        sqlite3.connect(path).close()
        assert queries._candidate_sources(Db(path), "x") == []
        assert queries._candidate_searches(Db(path), "x") == []


# ==========================================================================
# 2. Process order
# ==========================================================================

class TestThePageReadsInProcessOrder:
    def headings(self, html):
        return re.findall(r"<h4>.*?(\d)\. ([^<]*)</h4>", html)

    def test_the_steps_are_numbered_and_in_order(self, db):
        from catalyst.dashboard.queries import TradeStory as TS

        html = panels._trade_story(
            TS(ticker="EMBC", candidate_id=CID, origin="screen",
               entry_price="5.06", stop_price="4.55",
               opened_at="2026-08-17T16:00:00+00:00",
               planned_exit_date="2026-08-29", notional_usd="400",
               thesis="t", invalidation="i", direction="long",
               conviction=0.6,
               sources=queries._candidate_sources(db, CID)), "tr", 0)
        nums = [int(n) for n, _ in self.headings(html)]
        # NOT VACUOUS. The first version of this asserted only
        # `nums == sorted(nums)` and `nums == range(1, len+1)`, both of
        # which are TRUE for an empty list - so removing the numbering
        # entirely passed. Assert there are numbers before asserting
        # anything about them.
        assert len(nums) >= 5, (
            f"the steps are not numbered at all: {self.headings(html)}")
        assert nums == sorted(nums), f"out of order: {nums}"
        assert nums == list(range(1, len(nums) + 1)), f"gap: {nums}"

    def test_the_evidence_comes_before_the_conclusion(self, db):
        """The point of the reorder: you read what it read before you
        read what it decided."""
        st = TradeStory(ticker="EMBC", candidate_id=CID, origin="screen",
                        thesis="t", invalidation="i", direction="long",
                        conviction=0.6,
                        sources=queries._candidate_sources(db, CID))
        html = panels._trade_story(st, "tr", 0)
        assert html.index("The evidence it read") < \
            html.index("What Claude concluded")

    def test_how_it_was_found_comes_first_of_the_steps(self, db):
        st = TradeStory(ticker="EMBC", candidate_id=CID, origin="screen",
                        sources=queries._candidate_sources(db, CID))
        html = panels._trade_story(st, "tr", 0)
        assert html.index("How EMBC was found") < \
            html.index("The evidence it read")

    def test_the_outcome_summary_still_precedes_the_whole_process(self, db):
        """Process order is for the BODY. The answer still comes first -
        a reader who only wants to know what happened should not have to
        walk the pipeline."""
        st = TradeStory(ticker="EMBC", candidate_id=CID, entry_price="5.06",
                        stop_price="4.55", notional_usd="400",
                        opened_at="2026-08-17T16:00:00+00:00",
                        planned_exit_date="2026-08-29", status="open")
        html = panels._trade_story(st, "tr", 0)
        assert html.index('class="trade-sum"') < html.index("How EMBC was found")


# ==========================================================================
# 3. Is it mainly insider trades?
# ==========================================================================

def seed_arms(tmp_path, spec):
    """spec: {arm: (candidates, directional, cents_per_call, traded)}"""
    path = str(tmp_path / "arms.db")
    conn = init_db(path)
    for arm, (n, directional, cost, traded) in spec.items():
        for i in range(n):
            cid = f"{arm}-{i}-{uuid.uuid4().hex[:6]}"
            conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                         (cid, "AAA", "insider_cluster", "2026-08-13",
                          "confirmed", "[]", "2026-08-13T12:00:00+00:00",
                          "u", "[]"))
            conn.execute("INSERT INTO candidate_origin VALUES (?,?,?,?)",
                         (cid, arm, None, "2026-08-13T12:00:00+00:00"))
            conn.execute(
                "INSERT INTO research_calls (id,candidate_id,model,"
                "prompt_rendered,tools_offered,cost_cents,latency_ms,"
                "skipped_reason,called_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), cid, "m", "p", "[]", str(cost), 1, None,
                 "2026-08-13T12:00:00+00:00"))
            conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                         (cid, "long" if i < directional else "no_trade",
                          0.6, "t", "i", 12, 0, "w"))
            if i < traded:
                conn.execute(
                    "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), cid, "trade", "long", "400", "10",
                     "4.5", "2026-08-29", "[]", "{}",
                     "2026-08-13T12:00:00+00:00"))
    conn.commit()
    conn.close()
    return Db(path)


#: The lifetime record as measured on 2026-09-11.
REAL = {"screen": (189, 23, 17.8, 1), "conjunction": (89, 0, 27.7, 0),
        "earnings_drift": (13, 2, 18.9, 0), "hunt": (2, 0, 11.6, 0)}


class TestThePageAnswersTheConcentrationQuestion:
    def panel(self, db):
        return panels._arm_concentration(queries.origin_split(db))

    def test_it_says_yes_when_one_arm_is_nearly_everything(self, tmp_path):
        got = text(self.panel(seed_arms(tmp_path, REAL)))
        assert "23 of 25" in got
        assert "Insider clusters" in got
        assert "close to a single-arm system" in got

    def test_it_gives_each_arm_its_cost_per_call(self, tmp_path):
        got = text(self.panel(seed_arms(tmp_path, REAL)))
        for per_call in ("$0.178", "$0.277", "$0.189", "$0.116"):
            assert per_call in got, per_call

    def test_it_names_the_arm_that_has_spent_without_producing(self, tmp_path):
        got = text(self.panel(seed_arms(tmp_path, REAL)))
        assert "Cross-feed conjunctions" in got
        assert "without once producing a view" in got
        assert "probe share" in got

    def test_a_thin_record_is_called_thin_rather_than_bad(self, tmp_path):
        """The hunt's 2 calls and the conjunction's 89 both show zero
        views. Treating them the same would kill the one arm that has
        never been given a fair trial."""
        got = text(self.panel(seed_arms(tmp_path, REAL)))
        assert "far too few to judge it either way" in got
        assert "new, not proven bad" in got

    def test_it_says_the_split_is_zero_sum(self, tmp_path):
        got = text(self.panel(seed_arms(tmp_path, REAL)))
        assert "zero-sum" in got
        assert "fully committed" in got

    def test_no_directional_views_anywhere_says_so(self, tmp_path):
        got = text(self.panel(seed_arms(
            tmp_path, {"screen": (5, 0, 18.0, 0)})))
        assert "No arm has produced a directional view yet" in got
        assert "single-arm system" not in got

    def test_an_even_split_is_not_called_concentrated(self, tmp_path):
        got = text(self.panel(seed_arms(tmp_path, {
            "screen": (50, 10, 18.0, 0),
            "earnings_drift": (50, 10, 18.0, 0)})))
        assert "single-arm system" not in got
        assert "10 of 20" in got

    def test_an_empty_record_renders_nothing_rather_than_zeroes(self, tmp_path):
        assert self.panel(seed_arms(tmp_path, {})) == ""

    def test_IT_IS_ACTUALLY_ON_THE_PAGE(self, tmp_path):
        """A helper nobody calls answers nothing. This project has
        shipped that twice, so the wiring gets its own test."""
        import inspect

        assert "_arm_concentration(" in inspect.getsource(panels.origin_panel)
        rendered = panels.origin_panel(seed_arms(tmp_path, REAL))
        assert "close to a single-arm system" in text(rendered)
        assert "$0.277" in rendered, (
            "the per-arm costs are not reaching the rendered page")

    def test_the_panel_still_describes_all_four_arms(self, tmp_path):
        rendered = text(panels.origin_panel(seed_arms(tmp_path, REAL)))
        assert "Four things produce candidates" in rendered
        for word in ("Insider clusters", "earnings drift", "Conjunctions",
                     "hunt"):
            assert word.lower() in rendered.lower(), word

    def test_the_graded_arms_are_labelled_as_graded(self, tmp_path):
        got = text(self.panel(seed_arms(tmp_path, REAL)))
        assert "graded" in got
        assert "never graded" in got, (
            "the unbacktested arm is not marked as unbacktested"
        )

    def test_a_deferred_call_is_not_charged_to_an_arm(self, tmp_path):
        """It spent nothing. Charging it would demote an arm for cycles
        it was never given."""
        db = seed_arms(tmp_path, {"hunt": (2, 0, 11.6, 0)})
        import sqlite3

        path = db.path if hasattr(db, "path") else None
        conn = sqlite3.connect(str(path))
        cid = conn.execute(
            "SELECT candidate_id FROM candidate_origin").fetchone()[0]
        conn.execute(
            "INSERT INTO research_calls (id,candidate_id,model,"
            "prompt_rendered,tools_offered,cost_cents,latency_ms,"
            "skipped_reason,called_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), cid, "m", "p", "[]", "0", 1,
             "not_attempted: deferred_max_research_per_cycle",
             "2026-08-13T12:00:00+00:00"))
        conn.commit()
        conn.close()
        d = queries.origin_split(Db(str(path)))
        assert d.spend["hunt"][0] == 2, "a deferred call was counted as paid"


class TestTheHuntRateIsNotTheLever:
    """OWNER-ASKED: "can we get it to do even more agentic research to
    find very lucrative trades".

    THE ANSWER IS ARITHMETIC, NOT A CONSTANT. I tried halving the
    hunts_per_day divisor to take it from two a day to four, and the
    low-budget case caught it: that divisor is the reserve for
    RESEARCHING what a hunt finds, so halving it buys nominations
    nobody can afford to judge. Recorded here so the next session does
    not try the same thing."""

    def test_two_a_day_at_the_owners_cap(self):
        from catalyst.discovery.hunt import hunts_per_day

        assert hunts_per_day(10000) == 2

    def test_a_SMALL_BUDGET_HUNTS_NOT_AT_ALL(self):
        """The property that made halving the divisor wrong. At $20 a
        month one hunt would cost 60c of a 67c daily allowance, leaving
        nothing to research the nominations with."""
        from decimal import Decimal

        from catalyst.discovery.hunt import (
            HUNT_ESTIMATE_CENTS, hunts_per_day,
        )

        assert hunts_per_day(2000) == 0
        per_day = Decimal(2000) / Decimal(30)
        assert per_day < HUNT_ESTIMATE_CENTS * 2, (
            "the fixture no longer reproduces the budget that must not "
            "hunt, so this test is not measuring the reserve")

    def test_a_bigger_cap_is_the_dial(self):
        from catalyst.discovery.hunt import hunts_per_day

        assert hunts_per_day(30000) > hunts_per_day(10000)
        assert hunts_per_day(None) == 0 and hunts_per_day(0) == 0

    def test_it_is_still_bounded_however_large_the_cap(self):
        from catalyst.discovery.hunt import hunts_per_day

        assert hunts_per_day(10 ** 9) <= 4

    def test_the_estimate_stays_pessimistic(self):
        """The governor authorises against it, so an estimate that
        under-reaches is a bill that over-runs. Measured cost was 11.6c
        a hunt before web search was added."""
        from decimal import Decimal

        from catalyst.discovery.hunt import HUNT_ESTIMATE_CENTS

        assert HUNT_ESTIMATE_CENTS >= Decimal("60")

    def test_the_reason_is_written_down_where_the_number_lives(self):
        """So the next session reads it before re-trying it."""
        import inspect

        from catalyst.discovery import hunt

        src = inspect.getsource(hunt.hunts_per_day)
        assert "TRIED AND REJECTED" in src
        assert "RESERVE FOR RESEARCHING WHAT A HUNT FINDS" in src

    def test_the_hunt_is_subject_to_the_same_probe_rule(self):
        """What actually moved on 2026-09-11: research slots. And what
        makes that safe - no directional view in 40+ paid calls and the
        hunt demotes itself like any other arm."""
        from catalyst.orchestrator.cycle import (
            ARM_PROBE_MIN_CALLS, demoted_arms,
        )

        assert demoted_arms({"hunt": (ARM_PROBE_MIN_CALLS, 0)}) == {"hunt"}
        assert demoted_arms({"hunt": (2, 0)}) == set()
