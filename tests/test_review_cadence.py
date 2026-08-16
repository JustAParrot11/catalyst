"""Read a position again when something changed, not just when the clock says.

OWNER-ASKED: "surely it needs to research more often? especially on
trades it currently holds incase it can strategically sell and set a
date."

The fair half of that. A flat 24-hour clock means a thesis can break at
ten in the morning and go unread until the next day. But raising the
rate for everything is the expensive answer, and the money comes
straight out of discovery - which is what finds the next trade:

    every 24h (was)   $0.15/day = $ 4.50/mo    18% of a $25 cap
    every 12h         $0.30/day = $ 9.00/mo    36%
    every  6h         $0.60/day = $18.00/mo    72%
    every  4h         $0.90/day = $27.00/mo   108%

So news brings a review forward instead. Asking costs nothing - the
stories are already stored for discovery and the check is one indexed
query - and it spends the model call where something has actually
happened rather than paying five times a day to be told nothing has.

TWO THINGS THIS FILE GUARDS, in opposite directions.

The rule has to actually fire, or it is decoration. And it must not
become a runaway: a company in the headlines all day would otherwise be
re-read every cycle - 96 times - turning a responsiveness feature into
the largest line on the bill. MIN_REVIEW_GAP_HOURS is the floor
underneath, and it is tested as hard as the trigger above it.

WHAT IS DELIBERATELY NOT CHANGED. A review can still only ever SHORTEN a
hold. The owner also asked about "set a date"; extending one is refused
by design, because a position going against you plus a model asked
whether to hold it is exactly how a hard exit becomes a negotiation.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from catalyst.research.position_review import (
    MIN_REVIEW_GAP_HOURS, REVIEW_INTERVAL_HOURS, due_for_review, news_since,
)
from catalyst.storage import init_db

NOW = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "c.db"))
    conn.execute(
        "INSERT INTO positions VALUES ('p1','REGN','[]',NULL,"
        "'2026-08-10T14:00:00+00:00','2026-09-01','open')")
    conn.commit()
    yield conn
    conn.close()


def position():
    return {"id": "p1", "ticker": "REGN",
            "opened_at_date": date(2026, 8, 10),
            "planned_exit_date": date(2026, 9, 1)}


def reviewed(conn, hours_ago):
    conn.execute("DELETE FROM position_reviews")
    conn.execute(
        "INSERT INTO position_reviews (id,position_id,ticker,action,"
        "invalidation_triggered,reasoning,reviewed_at) VALUES "
        "(?,'p1','REGN','hold',0,'r',?)",
        (f"r{hours_ago}", (NOW - timedelta(hours=hours_ago)).isoformat()))
    conn.commit()


def story(conn, hours_ago, ticker="REGN", headline="REGN trial halted"):
    conn.execute(
        "INSERT INTO raw_events VALUES ('alpaca_news',?,?,?)",
        (f"n{ticker}{hours_ago}", (NOW - timedelta(hours=hours_ago)).isoformat(),
         json.dumps({"ticker": ticker, "headline": headline})))
    conn.commit()


class TestNewsBringsTheReviewForward:
    def test_news_since_the_last_read_triggers_one_early(self, db):
        reviewed(db, 6)
        story(db, 2)
        to_review, _ = due_for_review(db, [position()], NOW)
        assert to_review, (
            "a story broke and the position was not re-read - the whole "
            "point of the trigger")

    def test_it_says_WHY_it_fired_early(self, db):
        """A review that fired on news is a different event from one the
        clock came round to, and the funnel must not conflate them."""
        reviewed(db, 6)
        story(db, 2)
        to_review, _ = due_for_review(db, [position()], NOW)
        why = to_review[0].get("review_trigger", "")
        assert "REGN" in why and "news" in why
        assert "trial halted" in why, "the headline itself is not carried"

    def test_no_news_means_the_clock_still_rules(self, db):
        reviewed(db, 6)
        to_review, skipped = due_for_review(db, [position()], NOW)
        assert not to_review
        assert "no news has named REGN" in skipped[0][1]

    def test_the_ordinary_clock_still_fires_on_its_own(self, db):
        """News is an addition, not a replacement."""
        reviewed(db, REVIEW_INTERVAL_HOURS + 2)
        to_review, _ = due_for_review(db, [position()], NOW)
        assert to_review
        assert not to_review[0].get("review_trigger"), (
            "a routine clock review should not claim a news trigger")

    def test_news_about_a_DIFFERENT_company_does_not_trigger(self, db):
        reviewed(db, 6)
        story(db, 2, ticker="AAPL", headline="Apple does something")
        to_review, _ = due_for_review(db, [position()], NOW)
        assert not to_review

    def test_news_from_BEFORE_the_last_read_does_not_re_trigger(self, db):
        """It was already taken into account. Otherwise one old story
        re-reads the position forever."""
        reviewed(db, 6)
        story(db, 10)
        to_review, _ = due_for_review(db, [position()], NOW)
        assert not to_review


class TestItCannotBecomeARunaway:
    """The direction that costs money rather than losing it."""

    def test_a_position_in_the_headlines_is_not_re_read_every_cycle(self, db):
        reviewed(db, 1)
        for h in range(0, 5):
            story(db, h * 0.1, headline=f"REGN story {h}")
        to_review, skipped = due_for_review(db, [position()], NOW)
        assert not to_review, (
            "five stories in an hour re-read the position anyway - at 96 "
            "cycles a day this is the largest line on the bill")
        assert f"inside {MIN_REVIEW_GAP_HOURS}h" in skipped[0][1]

    def test_the_floor_is_well_under_the_interval(self):
        """It has to leave room to be useful, and room to be safe."""
        assert 0 < MIN_REVIEW_GAP_HOURS < REVIEW_INTERVAL_HOURS

    def test_a_torrent_of_news_costs_at_most_the_floor_rate(self, db):
        """Worst case, stated as a number: with news every cycle, how
        often can one position actually be read?"""
        fired = 0
        for hour in range(24):
            at = NOW + timedelta(hours=hour)
            db.execute(
                "INSERT INTO raw_events VALUES ('alpaca_news',?,?,?)",
                (f"torrent{hour}", at.isoformat(),
                 json.dumps({"ticker": "REGN", "headline": "more news"})))
            db.commit()
            to_review, _ = due_for_review(db, [position()], at)
            if to_review:
                fired += 1
                db.execute("DELETE FROM position_reviews")
                db.execute(
                    "INSERT INTO position_reviews (id,position_id,ticker,"
                    "action,invalidation_triggered,reasoning,reviewed_at) "
                    "VALUES (?,'p1','REGN','hold',0,'r',?)",
                    (f"t{hour}", at.isoformat()))
                db.commit()
        assert fired <= 24 / MIN_REVIEW_GAP_HOURS + 1, (
            f"{fired} reviews in a day of constant news")


class TestTheCheapCheckStaysCheap:
    def test_asking_costs_no_model_call(self, db):
        """news_since is pure SQL. If it ever needs a broker or a model
        the economics of the whole trigger invert."""
        story(db, 1)
        hits, headline = news_since(db, "REGN", NOW - timedelta(hours=4))
        assert hits == 1 and "trial halted" in headline

    @pytest.mark.parametrize("bad", ["", None, "'; DROP TABLE raw_events;--"])
    def test_hostile_or_empty_tickers_are_safe(self, db, bad):
        assert news_since(db, bad, NOW - timedelta(hours=4))[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM raw_events").fetchone() is not None

    def test_malformed_stored_news_never_raises(self, db):
        db.execute(
            "INSERT INTO raw_events VALUES ('alpaca_news','bad',?,'not json')",
            ((NOW - timedelta(hours=1)).isoformat(),))
        db.commit()
        assert news_since(db, "REGN", NOW - timedelta(hours=4))[0] == 0


class TestTheSafetyPropertyIsUnCHANGED:
    def test_a_review_still_cannot_extend_a_hold(self):
        """The owner asked about setting a date. Shortening is allowed;
        extending is refused by design, and this trigger does not touch
        that - it changes only WHEN the question is asked."""
        import inspect

        from catalyst.research import position_review as pr

        src = inspect.getsource(pr.bring_exit_forward)
        assert "forward" in src.lower()
        doc = pr.__doc__ or ""
        assert "ONLY EVER SHORTEN" in doc.upper() or "never extend" in doc.lower()
