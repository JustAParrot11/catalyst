"""Four things the owner reported on 2026-08-11, each reproduced first.

  1. "research skipped: transport_error: HTTPStatusError: Client error
     '400 Bad Request'" - prevalent, and blocking the whole pipeline.
  2. "in maintenance ... it says there are 7 discrepencies, it doesnt
     actually say what for example."
  3. "EDGAR also says this 9 hours ago, 405 events stored - EDGAR is
     publishing right now and nothing new has arrived - the feed or the
     scheduler may be stuck."
  4. "The graph that has catalyst and SPY in blue and red has no red SPY
     line to show how SPY is performing."
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard import maintenance
from catalyst.dashboard.db import Db
from catalyst.research.boundary import invalid_payload_reason


def _payload(messages, **kw):
    base = {"model": "claude-sonnet-5", "max_tokens": 2048,
            "messages": messages}
    base.update(kw)
    return base


def _guard_candidate():
    from catalyst.discovery import Candidate

    now = datetime.now(timezone.utc)
    return Candidate(
        id="cand-guard", ticker="GRD", catalyst_type="insider_cluster",
        catalyst_date=now.date() + timedelta(days=5),
        catalyst_date_confidence="confirmed", source_event_ids=("e1",),
        discovered_at=now, sector="tech", correlation_tags=("tech",))


class TestTheFourHundredNamesItselfNow:
    """The instance was fixed on 2026-08-10 (an empty content array in
    the echoed assistant turn). Fixing one instance is not fixing the
    class: any other invalid shape would again cost a paid call and
    surface as a bare status code. These are checked BEFORE sending."""

    def test_the_shape_that_actually_broke_it_is_named(self):
        why = invalid_payload_reason(_payload([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": []},
            {"role": "user", "content": "continue"},
        ]))
        assert why and "EMPTY content array" in why
        assert "message 1" in why, "it must say WHICH message"

    def test_a_valid_payload_is_not_rejected(self):
        """A false positive silently skips a candidate that would have
        worked, which is worse than the 400 it prevents."""
        assert invalid_payload_reason(_payload([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user", "content": "go on"},
        ])) is None

    @pytest.mark.parametrize("messages, expect", [
        ([], "empty"),
        ([{"role": "system", "content": "x"}], "role"),
        ([{"role": "user", "content": ""}], "empty text"),
        ([{"role": "user", "content": 42}], "not a list or a string"),
        ([{"role": "user", "content": [{"text": "no type"}]}], "no 'type'"),
        ([{"role": "user", "content": ["not an object"]}], "not an object"),
    ])
    def test_every_known_invalid_shape_is_named_in_english(self, messages, expect):
        why = invalid_payload_reason(_payload(messages))
        assert why and expect in why, f"{messages} -> {why!r}"

    def test_a_dangling_assistant_turn_is_caught(self):
        """An echo appended without its follow-up user turn asks the API
        to CONTINUE the assistant - valid HTTP, never what this loop
        intends."""
        why = invalid_payload_reason(_payload([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]))
        assert why and "no user turn after it" in why

    def test_a_bad_max_tokens_is_caught_before_it_is_billed(self):
        assert "max_tokens" in (invalid_payload_reason(
            {"model": "m", "max_tokens": 0,
             "messages": [{"role": "user", "content": "x"}]}) or "")

    def test_the_guard_runs_before_the_transport(self, tmp_path):
        """It must cost nothing. A payload the API would certainly reject
        must not consume a paid call.

        Proved by RUNNING, not by reading investigate()'s source for the
        order of two strings: that spelling broke the moment the guard
        grew a keyword argument, and a test that tracks the spelling of
        a call rather than its effect protects nothing.
        """
        import sqlite3
        from decimal import Decimal

        from catalyst.research import boundary, prompts

        conn = sqlite3.connect(str(tmp_path / "guard.db"))
        conn.executescript(open("catalyst/storage/schema.sql").read())
        conn.commit()

        # An empty rendered prompt makes the FIRST payload invalid, so
        # the guard has to fire before anything is sent.
        original = prompts.render_research_prompt
        prompts.render_research_prompt = lambda c, **kw: ""
        calls = []
        try:
            log = boundary.investigate(
                _guard_candidate(),
                boundary.CostContext(conn=conn,
                                     governor_profit_share=Decimal("0"),
                                     cycle_id="c", kind="scheduled"),
                lambda payload: calls.append(payload) or {})
        finally:
            prompts.render_research_prompt = original
        conn.close()

        assert calls == [], (
            "the payload was SENT before it was validated - that is a "
            "paid call spent on a request the API would reject")
        assert (log.skipped_reason or "").startswith(
            "invalid_request_not_sent"), log.skipped_reason


def _db_with(tmp_path, rows):
    path = str(tmp_path / "m.db")
    conn = sqlite3.connect(path)
    conn.executescript(open("catalyst/storage/schema.sql").read())
    for r in rows:
        conn.execute(
            "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
            "component, local_total_cents, cost_api_total_cents, "
            "discrepancy_cents, threshold_cents, api_raw_response, "
            "api_record_count, action_taken, reconciled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()
    return Db(path)


class TestTheMaintenancePageSaysWhatTheDiscrepanciesAre:
    """Owner-reported: "it says there are 7 discrepencies, it doesnt
    actually say what". A bare count cannot be acted on - it does not
    say which days, how big, or whether they are all the same harmless
    thing."""

    def test_the_days_and_the_figures_are_named(self, tmp_path):
        db = _db_with(tmp_path, [
            ("r1", "2026-08-09", "all", "{}", "6", "0", "6", "5", "{}", 0,
             "scheduled_paused", "2026-08-10T00:00:00+00:00"),
            ("r2", "2026-08-10", "all", "{}", "0", "412", "412", "5", "{}", 3,
             "scheduled_paused", "2026-08-11T00:00:00+00:00"),
        ])
        check = next(c for c in maintenance.passive_checks(db)
                     if c.name == "Spending not paused")
        assert check.state == maintenance.FAIL
        assert "2026-08-09" in check.summary and "2026-08-10" in check.summary
        assert "$0.06" in check.summary, "the local figure is not shown"
        assert "$4.12" in check.summary, "the billed figure is not shown"
        db.close()

    def test_it_says_a_gap_on_an_idle_day_is_your_own_usage(self, tmp_path):
        db = _db_with(tmp_path, [
            ("r1", "2026-08-09", "all", "{}", "0", "500", "500", "5", "{}", 2,
             "scheduled_paused", "2026-08-10T00:00:00+00:00")])
        check = next(c for c in maintenance.passive_checks(db)
                     if c.name == "Spending not paused")
        assert "your OWN Anthropic usage" in check.detail
        assert "changes no figure" in check.detail
        db.close()

    def test_a_clean_ledger_still_reads_as_ok(self, tmp_path):
        db = _db_with(tmp_path, [])
        check = next(c for c in maintenance.passive_checks(db)
                     if c.name == "Spending not paused")
        assert check.state == maintenance.OK
        db.close()


#: A weekday midday in New York, derived from the real clock rather than
#: frozen. The frozen version rotted: the fixture dated its rows from
#: datetime.now() while the check was handed a hardcoded 2026-08-11, so
#: the two drifted apart by a day for every day that passed.
def _weekday_midday_utc():
    now = datetime.now(timezone.utc).replace(
        hour=16, minute=0, second=0, microsecond=0)
    while now.weekday() >= 5:            # 16:00 UTC == midday in New York
        now -= timedelta(days=1)
    return now


def _db_with_event(tmp_path, hours_ago, now=None):
    path = str(tmp_path / "e.db")
    conn = sqlite3.connect(path)
    conn.executescript(open("catalyst/storage/schema.sql").read())
    when = (now or _weekday_midday_utc()) - timedelta(hours=hours_ago)
    conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                 ("edgar_form4", "acc-1", when.isoformat(), "{}"))
    conn.commit()
    conn.close()
    return Db(path)


class TestEdgarIsNotStuckJustBecauseItIsMidday:
    """Owner-reported: "EDGAR also says this 9 hours ago, 405 events
    stored - EDGAR is publishing right now and nothing new has arrived -
    the feed or the scheduler may be stuck."

    It was not stuck. This feed reads the DAILY INDEX - one file per
    day, published in the evening - so between publishes there is
    genuinely nothing new to store. Warning after six hours measured
    EDGAR's filing-ACCEPTANCE window, which is not what this consumes."""

    def _check(self, db, now):
        return next(c for c in maintenance.passive_checks(db, now=now)
                    if c.name.startswith("Filing feed"))

    def test_nine_hours_old_at_midday_is_normal_not_a_warning(self, tmp_path):
        midday_ny = _weekday_midday_utc()
        db = _db_with_event(tmp_path, hours_ago=9, now=midday_ny)
        check = self._check(db, midday_ny)
        assert check.state == maintenance.OK, check.summary
        assert "may be stuck" not in check.summary
        assert "once a day" in check.summary
        db.close()

    def test_a_gap_longer_than_a_day_IS_still_a_warning(self, tmp_path):
        """The check must not simply stop reporting. Longer than the gap
        between daily indexes means something is genuinely wrong."""
        midday_ny = _weekday_midday_utc()
        db = _db_with_event(tmp_path, hours_ago=50, now=midday_ny)
        check = self._check(db, midday_ny)
        assert check.state == maintenance.WARN
        assert "may be stuck" in check.summary
        db.close()

    def test_the_explanation_names_the_daily_index(self, tmp_path):
        midday_ny = _weekday_midday_utc()
        db = _db_with_event(tmp_path, hours_ago=9, now=midday_ny)
        check = self._check(db, midday_ny)
        assert "DAILY INDEX" in check.detail
        db.close()


class TestTheMissingSpyLineIsDiagnosable:
    """Owner-reported: "The graph that has catalyst and SPY in blue and
    red has no red SPY line". The chart draws SPY only when there are
    points, and there are none when the cache is empty - but nothing
    said why it was empty or when the refresh last tried."""

    def _check(self, db):
        return next(c for c in maintenance.passive_checks(db)
                    if c.name == "SPY benchmark cache")

    def test_a_missing_cache_is_NAMED_rather_than_a_silent_gap(
            self, tmp_path, monkeypatch):
        """UNKNOWN rather than FAIL: a machine where nothing has run yet
        legitimately has no cache, and a fresh install that reports a
        failure teaches the owner to ignore this page. The point is that
        it is NAMED, with the path, not that it shouts."""
        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "no-such-bars"))
        db = _db_with(tmp_path, [])
        check = self._check(db)
        assert check.state == maintenance.UNKNOWN
        assert "no SPY.csv" in check.summary
        assert "red SPY line" in check.detail
        assert "no-such-bars" in check.summary, "it must name the path it looked in"
        db.close()

    def test_a_populated_cache_reports_its_row_count_and_feed(
            self, tmp_path, monkeypatch):
        bars = tmp_path / "bars"
        bars.mkdir()
        (bars / "SPY.csv").write_text(
            "date,open,high,low,close,volume\n"
            "2026-08-10,600,601,599,600.5,1000000\n"
            "2026-08-11,600.5,602,600,601.5,1000000\n")
        (bars / "cache_meta.json").write_text('{"feed": "iex"}')
        monkeypatch.setenv("CATALYST_BARS", str(bars))
        db = _db_with(tmp_path, [])
        check = self._check(db)
        assert check.state == maintenance.OK
        assert "2 daily bar(s)" in check.summary
        assert "2026-08-11" in check.summary
        assert "IEX" in check.summary
        assert "consolidated tape" in check.detail
        db.close()


class TestTheSecRateLimitBlock:
    """Owner-reported live, after upgrading: "SEC.gov | Request Rate
    Threshold Exceeded ... Your access to SEC.gov will be limited for 10
    minutes." Plus: "Also this error is squished in dashboard".

    The pacer held 5 req/s, under the 10/s ceiling. The volume was the
    problem: ~2,815 requests per pass, 9.4 minutes of continuous traffic
    inside a 15-minute cycle."""

    def test_the_block_page_becomes_a_sentence_not_a_wall_of_markup(self):
        from catalyst.dashboard.panels import _fault_gist

        block = ('<!DOCTYPE html><html><head><title>SEC.gov | Request Rate '
                 'Threshold Exceeded</title><style>html {height:100%}'
                 '</style></head><body>You have exceeded...</body></html>')
        gist = _fault_gist(block)
        assert "<" not in gist and "{" not in gist
        assert "rate-limited" in gist
        assert "resumes on its own" in gist
        assert len(gist) < 300

    def test_an_absent_file_is_not_called_a_fault(self):
        from catalyst.dashboard.panels import _fault_gist

        assert "Not a fault" in _fault_gist(
            "<Error><Code>AccessDenied</Code></Error>")

    def test_an_unknown_body_is_stripped_and_truncated(self):
        from catalyst.dashboard.panels import _fault_gist

        gist = _fault_gist("<p>" + ("x" * 900) + "</p>")
        assert len(gist) <= 205 and "<p>" not in gist

    def test_the_raw_body_is_still_there_verbatim(self):
        """House rule 3. Folded, never deleted."""
        import sqlite3

        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        path = str(_tmp_db_with_feed_error())
        db = Db(path)
        html_out = panels.funnel_panel(db, p="f")
        assert "Threshold Exceeded" in html_out, "the raw body was dropped"
        assert "raw-fold" in html_out, "it must be folded, not inline"
        assert "the exact response from the server" in html_out
        db.close()


def _tmp_db_with_feed_error():
    import sqlite3
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / "fe.db"
    conn = sqlite3.connect(path)
    conn.executescript(open("catalyst/storage/schema.sql").read())
    conn.execute("INSERT INTO raw_events_errors VALUES (?,?,?)",
                 ("edgar_form4", "2026-08-11T12:00:00+00:00",
                  "<html><title>SEC.gov | Request Rate Threshold Exceeded"
                  "</title><body>" + ("padding " * 400) + "</body></html>"))
    conn.commit()
    conn.close()
    return path
