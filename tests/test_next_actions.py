"""What happens next, and when.

OWNER-ASKED: "can we add a next actions tab e.g. when will claude next
evaluate the choice and say sell or keep".

THE ONE DESIGN RULE, and every test here defends it: the page does not
know the review schedule and must never learn it. Every rule is asked of
catalyst/research/position_review.py - the same functions the live cycle
calls. A dashboard that confidently names the wrong next action is worse
than one that says nothing, because the owner plans around it.

That is why the gate probe exists rather than arithmetic: to say WHEN a
position that is currently too new becomes reviewable, this asks
should_review() about each future day instead of re-deriving its rule.
Copying the rule would work today and drift the first time the rule
changes and nobody remembers there are two copies of it.

NO PINNED DATES (house rule 6). The code measures against
datetime.now(), so the fixtures are built relative to the real clock. A
pinned fixture drifts out of the window a day at a time and goes red for
a reason unrelated to what it tests - twice already in this project.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from catalyst.storage import init_db

NOW = datetime.now(timezone.utc)


def seed(tmp_path, *, opened_days_ago=3, exit_in_days=10, reviews=(),
         news=(), status="open"):
    """One open position, positioned relative to the REAL clock."""
    path = str(tmp_path / "t.db")
    conn = init_db(path)
    opened = NOW - timedelta(days=opened_days_ago)
    exits = (NOW + timedelta(days=exit_in_days)).date()
    cid, pos, order = "cand-1", "pos-1", "ord-1"
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (cid, "EMBC", "insider_cluster", str(exits), "confirmed",
                  "[]", opened.isoformat(), "health", "[]"))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 (cid, "long", 0.6, "thesis", "invalidation", 12, 0, "why"))
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (order, cid, "b1", "buy", "79", "market", "day",
                  opened.isoformat(), "filled", "{}"))
    conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?)",
                 (order, "5.06", "79", opened.isoformat(), "5.06", "0.4"))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 (pos, "EMBC", json.dumps([order]), "stop-1",
                  opened.isoformat(), str(exits), status))
    for hours_ago in reviews:
        when = (NOW - timedelta(hours=hours_ago)).isoformat()
        conn.execute(
            "INSERT INTO position_reviews (id,position_id,ticker,action,"
            "invalidation_triggered,reasoning,what_changed_json,"
            "prompt_rendered,raw_response_json,model,cost_cents,reviewed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"rv{hours_ago}", pos, "EMBC", "hold", 0, "intact", "[]",
             "p", "{}", "m", "1", when))
    for hours_ago, headline in news:
        conn.execute(
            "INSERT INTO raw_events VALUES (?,?,?,?)",
            ("alpaca_news", f"n{hours_ago}",
             (NOW - timedelta(hours=hours_ago)).isoformat(),
             json.dumps({"ticker": "EMBC", "headline": headline})))
    conn.commit()
    conn.close()
    return path


def actions(path):
    db = Db(path)
    try:
        return queries.next_actions(db)
    finally:
        db.close()


def page(path):
    db = Db(path)
    try:
        return panels.next_actions_panel(db, p="na")
    finally:
        db.close()


def review_for(d, ticker="EMBC"):
    return [a for a in d.actions
            if a.kind in ("review", "blocked") and a.ticker == ticker]


class TestItAnswersTheQuestionThatWasAsked:
    """"when will claude next evaluate the choice and say sell or keep"."""

    def test_a_reviewable_position_gets_a_dated_next_review(self, tmp_path):
        d = actions(seed(tmp_path, reviews=[10]))
        r = review_for(d)
        assert r and r[0].when, "no date for the next review"
        assert "hours" in r[0].when_words or "due now" == r[0].when_words

    def test_a_never_reviewed_position_is_due_now(self, tmp_path):
        d = actions(seed(tmp_path))
        r = review_for(d)[0]
        assert r.due_now and r.when_words == "due now"
        assert "Never reviewed" in r.detail

    def test_it_names_the_three_answers_claude_may_give(self, tmp_path):
        html = page(seed(tmp_path))
        for answer in ("keep holding", "close it now", "no opinion"):
            assert answer in html

    def test_the_hard_exit_is_listed_whatever_the_reviews_say(self, tmp_path):
        """It needs no model, cannot be deferred and always happens, so
        it belongs on a page about what happens next."""
        d = actions(seed(tmp_path, exit_in_days=10))
        assert [a for a in d.actions if a.kind == "exit"]

    def test_soonest_first(self, tmp_path):
        d = actions(seed(tmp_path, reviews=[10]))
        dated = [a.when for a in d.actions if a.when]
        assert dated == sorted(dated)


class TestTheGateProbe:
    """A position too new to review still has a date, and it is found by
    ASKING should_review rather than by copying its arithmetic."""

    def test_a_position_opened_today_still_says_when(self, tmp_path):
        d = actions(seed(tmp_path, opened_days_ago=0))
        r = review_for(d)[0]
        assert r.when, "a brand new position says 'not scheduled' again"
        assert "The first review falls on" in r.detail
        assert "nothing new to find yet" in r.detail

    def test_the_date_it_names_is_one_should_review_agrees_with(
            self, tmp_path):
        """The test that makes the probe worth having: whatever date the
        page prints, the real function must say yes on it and no on the
        day before."""
        from catalyst.research.position_review import should_review

        d = actions(seed(tmp_path, opened_days_ago=0))
        r = review_for(d)[0]
        named = datetime.fromisoformat(r.when).date()
        pos = {"opened_at_date": NOW.date(),
               "planned_exit_date": (NOW + timedelta(days=10)).date()}
        assert should_review(pos, named)[0] is True
        assert should_review(pos, named - timedelta(days=1))[0] is False

    def test_a_position_closing_tomorrow_is_honestly_never(self, tmp_path):
        d = actions(seed(tmp_path, opened_days_ago=5, exit_in_days=1))
        r = review_for(d)[0]
        assert r.when_words == "never"
        assert r.kind == "blocked"
        assert "closes in" in r.detail

    def test_the_probe_reaches_past_the_longest_hold(self):
        from catalyst.risk.hard_bounds import HARD_BOUNDS

        longest = getattr(HARD_BOUNDS, "max_hold_days", 31)
        assert queries._GATE_PROBE_DAYS > longest, (
            "the probe gives up before the longest position could ever "
            "become reviewable, so it would report 'never' wrongly")


class TestItAsksTheCodeRatherThanRestatingIt:
    """The rule this page lives or dies by."""

    def test_the_interval_shown_is_the_one_the_cycle_uses(self, tmp_path):
        from catalyst.research.position_review import (
            MIN_REVIEW_GAP_HOURS, REVIEW_INTERVAL_HOURS,
        )

        path = seed(tmp_path)          # one db: seed() is not idempotent
        d = actions(path)
        assert d.interval_hours == REVIEW_INTERVAL_HOURS
        assert d.min_gap_hours == MIN_REVIEW_GAP_HOURS
        assert f"{REVIEW_INTERVAL_HOURS}h" in page(path)

    def test_it_imports_the_real_scheduler_not_a_copy(self):
        import inspect

        src = inspect.getsource(queries.next_actions)
        for fn in ("should_review", "last_reviewed_at", "news_since"):
            assert fn in src, (
                f"next_actions no longer calls {fn} - if the schedule is "
                "being recomputed here, this page is now a second source "
                "of truth and will drift")

    def test_no_hardcoded_interval_arithmetic(self):
        import inspect

        src = inspect.getsource(queries.next_actions)
        assert " 24" not in src and " 4)" not in src, (
            "an interval looks hardcoded; it must come from the module "
            "that owns it")


class TestNewsBringsItForward:
    def test_news_since_the_last_read_makes_it_due_now(self, tmp_path):
        d = actions(seed(tmp_path, reviews=[10],
                         news=[(5, "EMBC announces something")]))
        r = review_for(d)[0]
        assert r.due_now, "news did not bring the review forward"
        assert "brings the review forward" in r.detail
        assert "EMBC announces something" in r.detail

    def test_no_news_means_it_waits_for_the_clock(self, tmp_path):
        d = actions(seed(tmp_path, reviews=[10]))
        r = review_for(d)[0]
        assert not r.due_now
        assert "no news has named" in r.detail

    def test_news_OLDER_than_the_last_read_does_not_count(self, tmp_path):
        """It was already in front of the model when it last answered."""
        d = actions(seed(tmp_path, reviews=[10],
                         news=[(30, "old story")]))
        assert not review_for(d)[0].due_now

    def test_the_four_hour_floor_holds_against_any_amount_of_news(
            self, tmp_path):
        """Without it a company in the headlines all day is re-read every
        cycle, which is how a responsive rule becomes the largest line on
        the bill."""
        d = actions(seed(tmp_path, reviews=[1],
                         news=[(0.5, "breaking"), (0.2, "more breaking")]))
        r = review_for(d)[0]
        assert not r.due_now
        assert "however much news there is" in r.detail


class TestItIsHonestAboutWhatItCannotPromise:
    def test_it_says_the_schedule_is_not_a_promise(self, tmp_path):
        """A kill switch, an empty budget or a stopped service each stop
        all of this, and none are visible from a schedule."""
        html = page(seed(tmp_path))
        assert "not a promise the cycle runs" in html
        for blocker in ("kill switch", "budget", "stopped service"):
            assert blocker in html

    def test_nothing_open_is_a_zero_with_its_query_beside_it(self, tmp_path):
        """House rule 3."""
        html = page(seed(tmp_path, status="closed"))
        assert "SELECT" in html
        assert "nothing is open" in html

    def test_a_broken_database_is_displayed_not_raised(self, tmp_path):
        db = Db(str(tmp_path / "missing.db"))
        try:
            html = panels.next_actions_panel(db, p="na")
        finally:
            db.close()
        assert "could not be read" in html or "SELECT" in html

    def test_the_page_never_raises_on_a_half_written_position(self, tmp_path):
        path = seed(tmp_path)
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                     ("pos-2", "ACME", "[]", None, "not-a-date", "", "open"))
        conn.commit()
        conn.close()
        assert page(path)


class TestItLooksLikeTheRestOfTheDashboard:
    def test_each_row_carries_an_icon_hidden_from_screen_readers(
            self, tmp_path):
        import re

        html = page(seed(tmp_path))
        assert 'class="step-ico"' in html
        for m in re.finditer(r'<span class="step-ico"([^>]*)>', html):
            assert 'aria-hidden="true"' in m.group(1)

    def test_the_reason_stays_in_its_own_row(self, tmp_path):
        """section() lifts every .prov into a fold at the foot of the
        panel. A reason lifted out of the row it explains is a reason
        attached to nothing - the same defect the chart captions had."""
        html = page(seed(tmp_path))
        assert "prov-inline" in html
        if "workings" in html:
            fold = html[html.index('class="workings"'):]
            assert "Never reviewed" not in fold

    def test_it_is_reachable_from_the_navigation(self):
        from catalyst.dashboard.render import NAV

        assert any(href == "/next" for href, _ in NAV), (
            "the page exists but nothing links to it")

    def test_the_route_is_registered(self):
        from catalyst.dashboard import server

        assert "/next" in server.HTML_ROUTES
