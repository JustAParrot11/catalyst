"""The Pipeline page says what the bot is doing NOW, and a new tracked
stock is fetched on the spot.

OWNER-ASKED 2026-09-13, two things:

    "I can see it was doing active research and finding potential stocks
     on 12/09, this is good. What did it do, are any queued up its not
     clear anywhere what it is doing."

    "why cant it just make the API call to immediately get the historical
     predicted data for tracking on the graph, why do i need to wait a
     day when the data is available"

WHAT WAS MEASURED BEFORE BUILDING EITHER:

**The queue.** The funnel counts a LIFETIME population - in the owner's
bundle 6,999 candidates and 299 researched - which answers "what has
happened" and structurally cannot answer "what is happening". Its largest
single drop reason is `deferred_max_research_per_cycle` at **6,581**.
That IS the queue: named, counted, and nowhere described as one. A reader
saw a six-thousand-line loss and no statement that those candidates were
still in the running.

**The wait.** §19 had already cut it from a day to one cycle, by keying
the refresh marker on the tracked SET as well as the date. But one cycle
is up to fifteen minutes of an empty row, and an empty row is
indistinguishable from a mistyped ticker - the state this dashboard has
been reported for twice.

Fully offline: no test here touches the network, and the fetch is driven
through an injected transport. Every clock is supplied (house rule 6).
"""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db

NOW = datetime(2026, 9, 13, 12, 54, tzinfo=timezone.utc)


def _visible(html: str) -> str:
    body = re.sub(r"<title>.*?</title>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


@pytest.fixture
def queued(tmp_path):
    """60 candidates: 4 researched and decided, 2 holding a weekend view
    and waiting for the open, 54 queued. Plus the deferral rows the funnel
    reports as a loss."""
    from catalyst.storage import init_db

    path = str(tmp_path / "q.db")
    conn = init_db(path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def ago(mins):
        return (NOW - timedelta(minutes=mins)).isoformat()

    for i in range(60):
        cid = f"c{i}"
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     (cid, f"TK{i}", "insider_cluster", "2026-09-20",
                      "estimated", json.dumps([]), ago(i * 7), "6199",
                      json.dumps([])))
        conn.execute("INSERT INTO candidate_origin VALUES (?,?,?,?)",
                     (cid, "conjunction" if i % 3 else "screen", "",
                      ago(i * 7)))
    for i in range(4):
        cid = f"c{i}"
        conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"rc{i}", cid, "claude-sonnet-5", "p", "[]", "16.13",
                      41059, None, ago(30 + i * 15)))
        conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                     (cid, "long" if i == 0 else "no_trade",
                      0.62 if i == 0 else 0.68, "t", "i", 10, 1, "r"))
        conn.execute(
            "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"d{i}", cid, "skip", None, None, None, None, None,
             json.dumps(["model_said_no_trade"]), "{}", ago(29 + i * 15)))
    for i in (10, 11):
        cid = f"c{i}"
        conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"rcw{i}", cid, "claude-sonnet-5", "p", "[]", "11.47",
                      55138, None, ago(120)))
        conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                     (cid, "long", 0.58, "t", "i", 10, 0, "r"))
        conn.execute("INSERT INTO research_view_context VALUES (?,?,?,?)",
                     (cid, "32.31", "daily_close", ago(120)))
    for i in range(20, 40):
        conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"na{i}", f"c{i}", "", "", "[]", "0", 0,
                      "not_attempted: deferred_max_research_per_cycle",
                      ago(20)))
    conn.commit()
    conn.close()
    return path


class TestItCountsWhatIsQueued:

    def test_the_queue_is_counted_and_named(self, queued):
        d = queries.what_it_is_doing(Db(queued), now=NOW)
        assert d.waiting_for_research == 54, (
            "a candidate with no view and no decision is queued; the "
            "funnel reports these as a loss")

    def test_a_deferral_row_does_not_make_a_candidate_finished(self, queued):
        """The 6,581 the funnel loses are `not_attempted` rows. A skipped
        research call is not a research call, and counting it as one would
        report the queue as empty."""
        d = queries.what_it_is_doing(Db(queued), now=NOW)
        assert d.waiting_for_research == 54
        assert d.finished == 4

    def test_a_weekend_view_is_its_own_state(self, queued):
        """Not queued (it has been judged and cost nothing more) and not
        finished (no risk decision). §13's whole point, and it needs its
        own count or it reads as one of the other two."""
        d = queries.what_it_is_doing(Db(queued), now=NOW)
        assert d.holding_a_weekend_view == 2

    def test_paid_calls_today_excludes_the_skipped_ones(self, queued):
        d = queries.what_it_is_doing(Db(queued), now=NOW)
        assert d.calls_today == 6, (
            "20 deferral rows were counted as spending" )

    def test_the_belt_is_the_cycle_s_own_number(self, queued):
        """Read through `cycle.research_per_cycle`, so the figure on the
        page cannot drift from the one the belt applies."""
        import sqlite3

        from catalyst.orchestrator.cycle import research_per_cycle

        conn = sqlite3.connect(queued)
        try:
            expected = research_per_cycle(conn=conn)
        finally:
            conn.close()
        d = queries.what_it_is_doing(Db(queued), now=NOW)
        assert d.belt_per_cycle == expected

    def test_the_cycle_interval_is_read_from_the_scheduler(self, queued,
                                                           monkeypatch):
        """Including the env override, so the page follows a changed
        cadence with no second number to remember."""
        monkeypatch.setenv("CATALYST_CYCLE_SECONDS", "300")
        assert queries.what_it_is_doing(Db(queued), now=NOW).cycle_seconds \
            == 300


class TestTheNextInLineComesFromTheRealRotation:

    def test_it_rotates_by_arm_rather_than_listing_one_arm(self, queued):
        """FOUND BY RENDERING, and the first version was WRONG: it ordered
        by `discovered_at DESC` and captioned that "the order the belt
        takes them in", which `interleave_by_arm`'s own docstring
        contradicts. The rotation is applied by calling the cycle's
        function, not described."""
        d = queries.what_it_is_doing(Db(queued), now=NOW)
        arms = [arm for _t, arm, _c, _f in d.next_up]
        assert len(arms) == 8
        assert len(set(arms)) > 1, (
            "every name in the queue preview is from one arm, so the "
            "rotation is not being applied")
        # One per arm per round: no arm may take two consecutive slots
        # while another has candidates waiting.
        assert all(a != b for a, b in zip(arms, arms[1:])), (
            f"an arm took consecutive slots: {arms}")

    def test_it_calls_the_cycle_s_own_rotation(self, queued):
        """Assert the call site (§6). A local reimplementation would drift
        from the belt the moment the rotation changed."""
        import inspect

        src = inspect.getsource(queries.what_it_is_doing)
        assert "interleave_by_arm" in src

    def test_a_researched_candidate_is_not_offered_as_next(self, queued):
        d = queries.what_it_is_doing(Db(queued), now=NOW)
        assert "TK0" not in [t for t, *_ in d.next_up]
        assert "TK10" not in [t for t, *_ in d.next_up]


class TestThePanelAnswersTheQuestionAsked:

    def test_it_says_when_it_last_spent_anything(self, queued):
        html = panels.working_on(Db(queued), p="doing", now=NOW)
        assert "30 minutes ago" in _visible(html)

    def test_it_names_the_queue_in_words_not_only_a_number(self, queued):
        html = panels.working_on(Db(queued), p="doing", now=NOW)
        text = _visible(html)
        assert "Queued for research" in text
        assert "54" in text

    def test_it_says_what_each_recent_call_CONCLUDED(self, queued):
        """"What did it do" is not answered by a count of calls."""
        html = panels.working_on(Db(queued), p="doing", now=NOW)
        text = _visible(html)
        assert "TK0" in text and "long at 0.62" in text
        assert "no_trade at 0.68" in text

    def test_it_names_the_stocks_that_are_next(self, queued):
        html = panels.working_on(Db(queued), p="doing", now=NOW)
        assert "the next 8 in line" in _visible(html)

    def test_it_states_the_rate_and_refuses_to_promise_an_empty_queue(
            self, queued):
        """The screens rebuild the candidate list every cycle, so a
        countdown would be a promise the code does not make."""
        html = panels.working_on(Db(queued), p="doing", now=NOW)
        text = _visible(html)
        assert "18 cycles" in text, "the rate arithmetic is missing"
        assert "not a countdown" in text

    def test_it_says_nothing_in_the_queue_is_discarded(self, queued):
        html = panels.working_on(Db(queued), p="doing", now=NOW)
        assert "Nothing in the queue is discarded" in _visible(html)

    def test_it_is_above_the_lifetime_funnel_on_the_page(self):
        """The reader arrives with "what is it doing"; the funnel answers
        a different question and must not be read first. Assert the call
        site rather than the function (§6)."""
        import inspect

        from catalyst.dashboard import server

        src = inspect.getsource(server.route_funnel)
        assert "panels.working_on(" in src
        assert src.index("panels.working_on(") < src.index("funnel_panel(")


class TestAZeroIsNeverSilent:
    """House rule 3, and §14's rule: "no queue" and "the query is broken"
    look identical as a 0."""

    def test_an_unreadable_count_is_a_dash_and_not_a_zero(self, tmp_path):
        d = queries.what_it_is_doing(Db(str(tmp_path / "missing.db")))
        assert d.waiting_for_research is None
        assert d.error, "a failure with no error text is a silent zero"

    def test_the_panel_prints_the_error_verbatim(self, tmp_path):
        html = panels.working_on(Db(str(tmp_path / "missing.db")), p="doing",
                                 now=NOW)
        text = _visible(html)
        assert "&mdash;" in html or "—" in text
        assert "could not be read" in text

    def test_an_empty_but_healthy_database_says_nothing_has_been_billed(
            self, tmp_path):
        """Zero is the RIGHT answer here, and it needs a sentence rather
        than four zeros - a fresh install and a broken governor look the
        same otherwise."""
        from catalyst.storage import init_db

        path = str(tmp_path / "empty.db")
        init_db(path).close()
        html = panels.working_on(Db(path), p="doing", now=NOW)
        assert "No paid research call is on record" in _visible(html)


class TestAgoIsWordsNotArithmetic:

    def test_a_moment_ago_is_words(self):
        assert panels._ago(NOW.isoformat(), NOW) == "just now"

    def test_an_unreadable_timestamp_produces_no_duration_at_all(self):
        """A confident wrong duration is worse than none."""
        for bad in ("", None, "not-a-date", "2026-13-99"):
            assert panels._ago(bad, NOW) == ""

    def test_a_future_timestamp_is_not_reported_as_negative_minutes(self):
        later = (NOW + timedelta(hours=3)).isoformat()
        assert panels._ago(later, NOW) == "dated in the future"

    def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing(self):
        """Rows are written with tzinfo, but an older row or a hand-edited
        one may not be, and a TypeError here would take the page down."""
        naive = NOW.replace(tzinfo=None).isoformat()
        assert panels._ago(naive, NOW) == "just now"


class TestANewTrackedStockIsFetchedImmediately:
    """Owner: "why do i need to wait a day when the data is available"."""

    def test_the_add_calls_the_refresh_rather_than_only_writing_a_row(self):
        import inspect

        from catalyst.dashboard import server

        src = inspect.getsource(server.track_stock)
        assert "_fetch_one_comparison_now(" in src, (
            "the row is written and nothing fetches its closes, so the "
            "line stays empty until the next cycle")

    def test_it_uses_the_SAME_function_the_scheduler_uses(self):
        """Two fetch paths is two places to diverge - and the per-symbol
        cache metadata §18 added exists precisely so a second writer
        cannot corrupt SPY's feed pin."""
        import inspect

        from catalyst.dashboard import server

        src = inspect.getsource(server._fetch_one_comparison_now)
        assert "refresh_comparisons" in src

    def test_a_successful_fetch_says_the_line_is_there_now(self, monkeypatch):
        from catalyst.data.benchmark import RefreshResult
        from catalyst.dashboard import server

        monkeypatch.setattr(
            "catalyst.setup.credentials.load_credentials",
            lambda: type("C", (), {"alpaca_key": "k",
                                   "alpaca_secret": "s"})())
        monkeypatch.setattr(
            "catalyst.data.benchmark.refresh_comparisons",
            lambda *a, **k: {"VOO": RefreshResult(written=2412, feed="sip")})
        said = server._fetch_one_comparison_now("VOO")
        assert "fetched immediately" in said
        assert "2412" in said and "sip" in said

    def test_a_REFUSED_fetch_prints_the_raw_upstream_response(self,
                                                              monkeypatch):
        """House rule 3, and the VUAG case: the form accepts a London
        ticker by shape on purpose, so this is the moment it can say it
        has no US bars."""
        from catalyst.data.benchmark import RefreshResult
        from catalyst.dashboard import server

        monkeypatch.setattr(
            "catalyst.setup.credentials.load_credentials",
            lambda: type("C", (), {"alpaca_key": "k",
                                   "alpaca_secret": "s"})())
        monkeypatch.setattr(
            "catalyst.data.benchmark.refresh_comparisons",
            lambda *a, **k: {"VUAG": RefreshResult(
                skipped_reason="no_bars_returned",
                raw_response='{"bars":{},"next_page_token":null}')})
        said = server._fetch_one_comparison_now("VUAG")
        assert "no_bars_returned" in said
        assert '{"bars":{}' in said
        assert "not US-listed" in said

    def test_missing_credentials_are_a_sentence_not_a_traceback(self,
                                                               monkeypatch):
        from catalyst.dashboard import server

        monkeypatch.setattr(
            "catalyst.setup.credentials.load_credentials",
            lambda: type("C", (), {"alpaca_key": "", "alpaca_secret": ""})())
        said = server._fetch_one_comparison_now("VOO")
        assert "No Alpaca credentials are saved" in said

    def test_a_raising_fetch_still_leaves_the_stock_tracked(self, monkeypatch,
                                                            tmp_path):
        """THE PROPERTY THAT MATTERS. The row is committed before the
        fetch, so this is an accelerator and never the only path - the
        scheduler still picks it up next cycle."""
        from catalyst.benchmark import comparisons as _cmp
        from catalyst.dashboard import server
        from catalyst.storage import init_db

        path = str(tmp_path / "t.db")
        init_db(path).close()

        def boom(*_a, **_k):
            raise RuntimeError("upstream exploded")

        monkeypatch.setattr("catalyst.setup.credentials.load_credentials",
                            boom)
        okay, message = server.track_stock(
            path, {"ticker": "VOO", "amount_usd": "2000",
                   "start_date": "2026-09-01", "reason": "test"})
        assert okay is True, message
        assert "were not fetched now" in message
        import sqlite3

        conn = sqlite3.connect(path)
        try:
            tracked = [c.ticker for c in _cmp.stored(conn)]
        finally:
            conn.close()
        assert "VOO" in tracked, (
            "a failed fetch removed the tracked row, so the owner's add "
            "silently did nothing")

    def test_nothing_here_can_size_spend_or_trade(self):
        """Same guard §18 set for the table itself."""
        import subprocess

        out = subprocess.run(
            ["grep", "-rn", "_fetch_one_comparison_now",
             "catalyst/risk", "catalyst/execution", "catalyst/cost"],
            capture_output=True, text=True, cwd="/home/user/catalyst")
        assert out.stdout.strip() == ""
