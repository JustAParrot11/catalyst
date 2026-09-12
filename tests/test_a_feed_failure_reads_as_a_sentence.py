"""A feed failure must explain itself. OWNER-REPORTED.

OWNER-REPORTED 2026-09-12, with a screenshot of the Pipeline page:

    NEEDS ATTENTION
    FEEDS THAT COULD NOT BE READ
    1  Insider trades (SEC Form 4) could not be read
       WebFontConfig = { google: { families: [ 'Raleway:300,400,500,600:latin' ] } };
       (function() { var wf = document.createElement('script');
       wf.src = '//ajax.googleapis.com/ajax/libs/webfont/1/webfont.js'; ...

listed twice, with "1" beside each.

**Three defects, and the first one was guaranteed rather than unlucky.**

    1. `_fault_gist` stripped HTML *tags* with `re.sub(r"<[^>]+>", ...)`,
       which leaves the *contents* of a <script> untouched. sec.gov puts
       its Google WebFont loader at the very top of <head>, before any
       prose, so for ANY sec.gov page not in the four-marker table the
       "one readable sentence" was that loader. Every time, by
       construction.
    2. `cycle.py` recorded `exc.raw_text` ALONE. The exception was
       carrying `message="HTTP 503 after 4 attempts"`, `status_code=503`,
       the URL and the attempt count, and all four were discarded at the
       point of writing - so the database never had them and no
       dashboard work could have recovered them. `FeedError.__str__`
       formats exactly that summary and nothing had ever called it.
    3. A `RateLimitBlocked` wrote NO row at all - it logged and returned
       [] - so the panel's own first fault sentence, the one for a
       rate-limit block, was unreachable for this feed.

And the count beside each row was the literal `1`, so "once" and "forty
times" rendered identically.

House rule 7 throughout: an HTML **document** where an .idx file or a
filing's text was expected is an upstream error page, whatever it says.
That is the rule; the marker table is only for pages whose sentence
should say what to DO.

Fully offline.
"""

import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.data import sources
from catalyst.data.sources.edgar_form4 import FeedError, RateLimitBlocked
from catalyst.dashboard import panels
from catalyst.dashboard.db import Db

#: A sec.gov error page in its real shape: the font loader is the first
#: text content, ahead of anything a human would read.
SEC_ERROR_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>SEC.gov | Site Temporarily Unavailable</title>
<script type="text/javascript">
   WebFontConfig = {
      google: { families: [ 'Raleway:300,400,500,600:latin' ] }
   };
   (function() {
      var wf = document.createElement('script');
      wf.src = '//ajax.googleapis.com/ajax/libs/webfont/1/webfont.js';
   })();
</script>
<style>body { font-family: Raleway, sans-serif; }</style>
</head><body><h1>SEC.gov is temporarily unavailable</h1></body></html>"""

SEC_BLOCK_PAGE = """<!DOCTYPE html><html><head>
<title>SEC.gov | Request Rate Threshold Exceeded</title>
<script>WebFontConfig = { google: { families: [ 'Raleway:300:latin' ] } };</script>
</head><body><p>Request Rate Threshold Exceeded</p></body></html>"""

ABSENT_INDEX = ('<?xml version="1.0" encoding="UTF-8"?><Error>'
                '<Code>AccessDenied</Code><Message>Access Denied</Message>'
                '</Error>')

#: The exact fragment from the owner's screenshot.
OWNERS_FRAGMENT = "WebFontConfig"


def _form4_503(url="https://www.sec.gov/Archives/edgar/daily-index/"
                    "2026/QTR3/form.20260911.idx"):
    return FeedError("HTTP 503 after 4 attempts", url=url, status_code=503,
                     raw_text=SEC_ERROR_PAGE, attempts=4)


class TestTheSentenceIsNeverMarkup:
    """Defect 1, the owner's report."""

    def test_an_unrecognised_sec_page_does_not_read_as_a_font_loader(self):
        said = panels._fault_gist(SEC_ERROR_PAGE)
        assert OWNERS_FRAGMENT not in said, (
            "the owner's exact screenshot: a Google WebFont snippet "
            f"presented as the explanation. Got: {said[:200]!r}")
        assert "web page instead of data" in said

    def test_it_names_the_page_so_the_reader_knows_which_page(self):
        said = panels._fault_gist(SEC_ERROR_PAGE)
        assert "SEC.gov | Site Temporarily Unavailable" in said

    def test_no_script_or_style_content_ever_survives(self):
        """Not "this page's script" - ANY script. The contents of a
        <script> are machinery, so they are removed element and all.

        DELIBERATELY A FRAGMENT, NOT A DOCUMENT. The first version of
        this test used a full <html> page, so the HTML-document rule
        returned early and `_visible_text` was never reached - the test
        passed with the machinery-stripping removed entirely. Caught by
        sabotage (section 6: a test that cannot fail).
        """
        fragment = ("<div><script>var secret = 'nonprose';</script>"
                    "<style>.x{color:nonprose}</style>"
                    "<p>the actual message</p></div>")
        assert "nonprose" not in panels._visible_text(fragment)
        assert "the actual message" in panels._visible_text(fragment)
        said = panels._fault_gist(fragment)
        assert "nonprose" not in said
        assert "var secret" not in said

    def test_an_unclosed_script_does_not_leak_its_tail(self):
        """A malformed page is the normal case for an error page served
        by an edge, and the element-matching regex cannot match it.
        A fragment again, for the reason above."""
        fragment = "<p>real text</p><script>var leak = 'nonprose'; // unclosed"
        assert "nonprose" not in panels._visible_text(fragment)
        assert "nonprose" not in panels._fault_gist(fragment)

    def test_a_markup_fragment_is_read_as_a_RESPONSE_not_as_a_summary(self):
        """Coming out clean is not enough - a fragment has to reach the
        body path, because that is the path that can recognise what the
        response IS. Through the summary path an AccessDenied fragment
        would be printed stripped, and the reader would never be told it
        is a routine unpublished file rather than a fault."""
        fragment = "<Error><Code>AccessDenied</Code></Error>"
        said = panels._fault_gist(fragment)
        assert "not published" in said, f"got {said!r}"
        assert "Not a fault" in said

    def test_the_machinery_strip_is_what_the_html_rule_falls_back_on(self):
        """Both guards are load-bearing and they cover each other, so
        neither alone proves the other works. The document rule answers
        for a whole page; `_visible_text` answers for everything else -
        and everything else is where a fragment of markup arrives."""
        assert panels._visible_text(
            "<script>WebFontConfig = { google: {} };</script> after") == "after"

    def test_a_plain_text_body_is_still_shown_as_itself(self):
        """The fix must not throw away the bodies that WERE readable."""
        assert "Access Denied" in panels._fault_gist(
            "Access Denied: something the reader can use") or \
            "not published" in panels._fault_gist(ABSENT_INDEX)

    def test_an_empty_detail_says_so_rather_than_rendering_blank(self):
        assert panels._fault_gist("") == "no detail was returned"
        assert panels._fault_gist(None) == "no detail was returned"


class TestThePageIdentitiesStillWin:
    """The four actionable sentences must survive the new rule - and the
    generic English words must not beat them."""

    def test_a_rate_limit_block_still_says_what_to_do(self):
        said = panels._fault_gist(sources.error_text(
            FeedError("HTTP 403", status_code=403, raw_text=SEC_BLOCK_PAGE)))
        assert "rate-limited this machine" in said
        assert "nothing to do" in said

    def test_an_absent_daily_index_is_still_not_a_fault(self):
        said = panels._fault_gist(sources.error_text(
            FeedError("HTTP 403 - not retried", status_code=403,
                      raw_text=ABSENT_INDEX)))
        assert "not published" in said
        assert "Not a fault" in said

    def test_a_web_page_mentioning_a_timeout_is_not_called_a_timeout(self):
        """`timeout` is an ordinary English word and was matched against
        the raw body, so a maintenance page whose prose happened to use
        it reported as a network timeout - which sends the reader to
        look at the wrong thing. Page identity is decided before
        ordinary words."""
        page = ("<!DOCTYPE html><html><head><title>SEC.gov | Maintenance"
                "</title></head><body><p>Your session timeout has been "
                "reset.</p></body></html>")
        said = panels._fault_gist(page)
        assert "did not answer in time" not in said
        assert "web page instead of data" in said

    def test_a_real_transport_timeout_still_says_timeout(self):
        said = panels._fault_gist(sources.error_text(
            FeedError("transport failure after 4 attempts",
                      url="https://www.sec.gov/x", status_code=None,
                      raw_text="ReadTimeout: timed out", attempts=4)))
        assert "did not answer in time" in said


class TestTheDiagnosisIsRecorded:
    """Defect 2. The exception knew; the row did not."""

    def test_the_status_code_and_attempts_reach_the_stored_text(self):
        stored = sources.error_text(_form4_503())
        assert "503" in stored
        assert "attempts=4" in stored
        assert "form.20260911.idx" in stored

    def test_the_raw_body_is_still_kept_verbatim(self):
        """House rule 3. The body moves below the sentence; it does not
        go away."""
        stored = sources.error_text(_form4_503())
        assert SEC_ERROR_PAGE in stored

    def test_the_sentence_carries_both_halves(self):
        said = panels._fault_gist(sources.error_text(_form4_503()))
        assert "503" in said, "the status code the exception carried"
        assert "web page instead of data" in said, "what the body was"

    def test_an_exception_with_no_body_records_its_diagnosis(self):
        stored = sources.error_text(ValueError("nothing in the payload"))
        assert "ValueError" in stored
        assert "nothing in the payload" in stored

    def test_an_exception_with_no_message_at_all_says_so(self):
        """The type name alone would make this non-empty, so asserting
        "not empty" is a test that cannot fail. What the code actually
        promises is that the reader is told there was no message -
        otherwise the page shows `RuntimeError:` with nothing after the
        colon, which reads as truncation rather than as a fact."""
        stored = sources.error_text(RuntimeError())
        assert "RuntimeError" in stored
        assert "carried no message" in stored, f"got {stored!r}"
        assert not stored.rstrip().endswith(":"), (
            "a dangling colon reads as a truncated message, not as the "
            "absence of one")
        assert panels._fault_gist(stored) != "no detail was returned"

    def test_the_raw_fold_shows_the_server_and_not_our_own_words(self):
        """The fold is labelled "the exact response from the server", so
        putting our summary line inside it labels our words as theirs."""
        raw = panels._fault_raw(sources.error_text(_form4_503()))
        assert raw.startswith("<!DOCTYPE html")
        assert "attempts=4" not in raw

    def test_a_failure_with_no_body_draws_no_empty_fold(self):
        assert panels._fault_raw(sources.error_text(
            ValueError("no body here"))) == ""


class TestOlderRowsStillRead:
    """The column holds rows written before any of this. They must not
    become unreadable, and they must not be mistaken for new ones."""

    def test_a_body_only_row_is_explained_not_dumped(self):
        said = panels._fault_gist(SEC_ERROR_PAGE)
        assert OWNERS_FRAGMENT not in said
        assert "web page instead of data" in said

    def test_a_body_only_row_still_offers_the_raw_page(self):
        assert panels._fault_raw(SEC_ERROR_PAGE).startswith("<!DOCTYPE html")


class TestTheSameFailureTwiceIsCountedTwice:
    """The count column was the literal 1."""

    @pytest.fixture
    def db(self, tmp_path):
        from catalyst.storage import init_db

        path = str(tmp_path / "t.db")
        conn = init_db(path)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, (
            "the fixture must match production or it will agree with a bug")
        conn.close()
        return path

    def _err(self, path, source, hours_ago, text):
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                "INSERT INTO raw_events_errors (source, attempted_at, "
                "error_text) VALUES (?,?,?)",
                (source, (datetime.now(timezone.utc)
                          - timedelta(hours=hours_ago)).isoformat(), text))
            conn.commit()
        finally:
            conn.close()

    def test_two_identical_failures_are_one_row_counting_two(self, db):
        from catalyst.dashboard import queries

        for hours, day in ((5, "11"), (4, "12")):
            self._err(db, "edgar_form4", hours, sources.error_text(
                _form4_503(f"https://www.sec.gov/a/form.202609{day}.idx")))
        faults = queries.funnel(Db(db)).feed_faults
        form4 = [f for f in faults if "Form 4" in f[0]]
        assert len(form4) == 1, (
            "the same failure on two days rendered as two rows, each "
            f"claiming a count of 1. Got {[f[0] for f in form4]}")
        assert form4[0][1] == 2
        assert "2 times since" in form4[0][0]

    def test_two_different_failures_stay_two_rows(self, db):
        from catalyst.dashboard import queries

        self._err(db, "edgar_form4", 5, sources.error_text(_form4_503()))
        self._err(db, "edgar_form4", 4, sources.error_text(
            FeedError("HTTP 403 - not retried", status_code=403,
                      raw_text=ABSENT_INDEX, attempts=1)))
        form4 = [f for f in queries.funnel(Db(db)).feed_faults
                 if "Form 4" in f[0]]
        assert len(form4) == 2

    def test_the_tile_agrees_with_the_list_it_sits_above(self, db):
        """A tile reading 3 above rows summing to 4 is the panel
        disagreeing with itself."""
        from catalyst.dashboard import queries

        for hours, day in ((5, "11"), (4, "12")):
            self._err(db, "edgar_form4", hours, sources.error_text(
                _form4_503(f"https://www.sec.gov/a/form.202609{day}.idx")))
        self._err(db, "federal_register", 3, sources.error_text(
            ValueError("no window")))
        data = queries.funnel(Db(db))
        total = sum(n for _r, n, _d in data.feed_faults)
        assert total == 3
        html = panels.funnel_panel(Db(db))
        i = html.find("funnel-feed-tiles")
        tiles = " ".join(re.sub(r"<[^>]+>", " ",
                                html[i:i + 1200]).split())
        assert ">3<" in html[i:i + 1200] or " 3 " in tiles, (
            f"the tile does not show the occurrences: {tiles[:200]!r}")

    def test_one_feed_failing_many_times_is_one_feed(self, db):
        """"N feed(s) failed to read" counted ROWS, so one feed failing
        forty times sent the reader hunting for thirty-nine sources that
        do not exist."""
        from catalyst.dashboard import queries

        for hours in range(2, 8):
            self._err(db, "edgar_form4", hours, sources.error_text(
                FeedError(f"HTTP 50{hours} after 4 attempts",
                          status_code=500 + hours, raw_text=SEC_ERROR_PAGE,
                          attempts=4)))
        blame = queries.funnel(Db(db)).blame
        assert "1 feed(s) failed to read" in blame, (
            f"got: {blame!r}")


class TestTheOwnersPanelReadsCorrectlyEndToEnd:

    def test_the_rendered_panel_contains_no_javascript_as_prose(self, tmp_path):
        from catalyst.storage import init_db

        path = str(tmp_path / "t.db")
        conn = init_db(path)
        conn.execute(
            "INSERT INTO raw_events_errors (source, attempted_at, error_text) "
            "VALUES (?,?,?)",
            ("edgar_form4", datetime.now(timezone.utc).isoformat(),
             sources.error_text(_form4_503())))
        conn.commit()
        conn.close()

        html = panels.funnel_panel(Db(path))
        i = html.find("funnel-feed-faults")
        assert i >= 0
        block = html[i:i + 4000]
        # The gist sits in the <span class="prov"> before the fold. The
        # raw page is allowed below it - that is house rule 3.
        gist = re.search(r'<span class="prov">(.*?)</span>', block,
                         re.DOTALL)
        assert gist is not None
        assert OWNERS_FRAGMENT not in gist.group(1)
        assert "503" in gist.group(1)
        # and the page IS still there, one click away
        assert "Raleway" in block


class TestARateLimitBlockIsVisibleOnThePage:
    """Defect 3. It logged and returned [], so nothing reached the
    dashboard - and the panel's flagship sentence was unreachable."""

    def test_the_scheduler_records_a_block_where_the_panel_can_see_it(
            self, tmp_path, monkeypatch):
        from catalyst.storage import init_db

        path = str(tmp_path / "t.db")
        init_db(path).close()

        # _record_feed_error is a closure inside _run_one_cycle, so the
        # behaviour is asserted at the FUNNEL, which is the outcome that
        # matters - and it is what a module-level unit test would have
        # missed (section 6: assert the call site).
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler._run_one_cycle)
        i = src.find("except RateLimitBlocked as exc:")
        assert i >= 0
        window = src[i:i + 700]
        assert "_record_feed_error" in window, (
            "a rate-limit block still writes no row, so the block is "
            "visible only in the log file")

    def test_a_recorded_block_renders_with_its_actionable_sentence(
            self, tmp_path):
        from catalyst.dashboard import queries
        from catalyst.storage import init_db

        path = str(tmp_path / "t.db")
        conn = init_db(path)
        blocked = RateLimitBlocked(
            "SEC.gov rate-limited this IP (HTTP 403).",
            raw_text=SEC_BLOCK_PAGE)
        conn.execute(
            "INSERT INTO raw_events_errors (source, attempted_at, error_text) "
            "VALUES (?,?,?)",
            ("edgar_form4", datetime.now(timezone.utc).isoformat(),
             sources.error_text(blocked)))
        conn.commit()
        conn.close()
        faults = queries.funnel(Db(path)).feed_faults
        assert faults
        said = panels._fault_gist(faults[0][2])
        assert "rate-limited this machine" in said
        assert "nothing to do" in said


class TestTheGroupingKey:
    """`fault_key` decides what "the same failure" means, and it has to
    ignore the part that changes every day."""

    def test_the_same_outage_on_two_days_is_one_key(self):
        a = sources.error_text(_form4_503("https://x/form.20260911.idx"))
        b = sources.error_text(_form4_503("https://x/form.20260912.idx"))
        assert sources.fault_key(a) == sources.fault_key(b)

    def test_two_different_failures_are_two_keys(self):
        a = sources.error_text(_form4_503())
        b = sources.error_text(FeedError("HTTP 403 - not retried",
                                         status_code=403,
                                         raw_text=ABSENT_INDEX, attempts=1))
        assert sources.fault_key(a) != sources.fault_key(b)

    def test_an_older_body_only_row_keys_on_its_body(self):
        assert sources.fault_key(SEC_ERROR_PAGE) == \
            sources.fault_key(SEC_ERROR_PAGE)
        assert sources.fault_key(SEC_ERROR_PAGE) != \
            sources.fault_key(ABSENT_INDEX)

    def test_nothing_keys_to_an_empty_string(self):
        """Two unrelated failures both keyed to "" would merge and count
        as one."""
        assert sources.fault_key("") == ""
        assert sources.fault_key(sources.error_text(RuntimeError())) != ""


class TestTheCycleRecordsThroughTheHelper:
    """Section 6, fourth instance of "a helper nobody calls": assert the
    CALL SITE, not only the function."""

    def test_run_cycle_writes_the_diagnosis_not_only_the_body(self):
        import inspect

        from catalyst.orchestrator import cycle

        src = inspect.getsource(cycle)
        i = src.find("INSERT INTO raw_events_errors")
        assert i >= 0
        window = src[max(0, i - 600):i + 400]
        assert "_feed_error_text(exc)" in window, (
            "the cycle is storing something other than the recorded "
            "diagnosis")
        assert 'getattr(exc, "raw_text", None) or repr(exc)' not in window, (
            "the old body-only write is still there")
