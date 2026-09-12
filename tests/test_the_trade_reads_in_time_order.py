"""A dated timeline, a check-in Claude sets, and no emoji.

OWNER-ASKED 2026-09-12, on the rebuilt trade card:

  "i have a concern on the trades tab, when it references an old trade
   it references edgar and one url content is ttached, they all appear
   to say that, is there na issue with the data or way it is displayed?
   Also it still not very clear, i want each stage it took in
   chronological info and the data that was available and how price
   changed and what the bot thought when it re-evaluated as it should be
   doing now and again. It should be able to set itself a next to check
   in tab, i want claude if it does trade to suggest when is best to
   check back in e.g. 3 days it checks in makes whatever decision but if
   it holds then it sets another date to check back in"

and, separately: "remove emojis also we dont need them".

THREE THINGS.

1. THE EDGAR LINKS ALL 404'd, and that was mine. Covered by
   test_the_card_shows_its_evidence.py, which now carries the owner's
   exact broken key as a regression.

2. THE CARD WAS GROUPED BY TOPIC, NOT BY TIME. How it was found, the
   evidence, the conclusion, the size - which is the order the decision
   was made in, with no price beside any moment. So "the stock was $5.22
   when Claude held and $5.06 four days later when it held again" was
   not readable anywhere, and that sequence is what says whether the
   re-reads were doing anything at all.

3. REVIEWS RAN ON A FLAT 24-HOUR CLOCK. A position with nothing due for
   a week was paid for six times to be told nothing had changed, and one
   with a filing tomorrow waited the same 24 hours as everything else.
   Claude now says when to look again, and code bounds it.

Fully offline. No calendar dates measured against now (house rule 6):
every fixture date is fixed data ON the record, and the one place a
clock matters is handed the clock.
"""

import html as _html
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import pytest

from catalyst.dashboard import panels
from catalyst.dashboard.queries import TradeStory
from catalyst.research.position_review import (
    MAX_CHECK_IN_DAYS, PositionReview, make_review_from_tool_input,
    next_check_at, record_review, requested_check_at,
)
from catalyst.storage import init_db

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


class Bar:
    def __init__(self, day, close):
        self.day = day
        self.close = Decimal(str(round(close, 4)))
        self.low = Decimal(str(round(close * 0.99, 4)))
        self.high = Decimal(str(round(close * 1.01, 4)))


def bars(first="2026-08-10", n=30, start=5.40, step=-0.02):
    day0 = date.fromisoformat(first)
    return [Bar(day0 + timedelta(days=i), start + i * step) for i in range(n)]


def embc(**over):
    base = dict(
        position_id="abc12345", ticker="EMBC", catalyst_type="insider_cluster",
        catalyst_date="2026-08-13", origin="screen", direction="long",
        conviction=0.60, thesis="t", invalidation="i",
        expected_holding_days=12, notional_usd="400.00", qty="79.1295",
        stop_price="4.55", planned_exit_date="2026-08-29",
        opened_at="2026-08-17T16:28:00+00:00", entry_price="5.06",
        status="closed", exit_price="4.9736", exit_reason="hard_exit",
        realized_pnl_cents=-684, actual_holding_days=12,
        closed_at="2026-08-29T20:00:00+00:00", equity_at_entry="2000.00",
        reviews=[
            ("2026-08-19T12:00:00+00:00", "hold", "s",
             "Insider buying intact, tape quiet.", 0, 0),
            ("2026-08-22T12:00:00+00:00", "hold", "s",
             "No news; holding to the exit date.", 0, 0),
            ("2026-08-25T12:00:00+00:00", "exit_now", "s",
             "Guidance cut; the invalidation has triggered.", 0, 0),
            ("2026-08-27T12:00:00+00:00", "hold", "s", "", 0,
             "too soon; reviewed 3.1h ago"),
        ],
        limits=[])
    base.update(over)
    return TradeStory(**base)


def text(markup):
    return " ".join(_html.unescape(re.sub("<[^>]+>", " ", markup)).split())


def timeline(st, with_bars=None):
    if with_bars is None:
        return panels._timeline(st, "tr", 0)
    with mock.patch.object(panels, "_load_position_bars",
                           return_value=with_bars):
        return panels._timeline(st, "tr", 0)


def rows(html):
    """(when, price, what) per timeline row, header excluded."""
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        cells = [text(c) for c in
                 re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)]
        if len(cells) == 3 and cells[0] != "when":
            out.append(tuple(cells))
    return out


# ==========================================================================
# The timeline
# ==========================================================================

class TestEveryStageAppearsInTimeOrder:
    def test_there_is_a_timeline_at_all(self):
        assert timeline(embc(), bars()), (
            "the card still has no time-ordered view, so the reader "
            "assembles the sequence out of grouped sections")

    def test_the_dates_ascend(self):
        got = rows(timeline(embc(), bars()))
        assert got
        parsed = [datetime.strptime(r[0], "%d %b") for r in got]
        assert parsed == sorted(parsed), f"out of order: {[r[0] for r in got]}"

    def test_it_covers_the_whole_life_of_the_trade(self):
        got = " | ".join(r[2] for r in rows(timeline(embc(), bars())))
        assert "Catalyst date" in got
        assert "Bought EMBC" in got
        assert "Re-read the thesis" in got
        assert "Sold EMBC" in got

    def test_the_buy_row_carries_the_fill_and_the_stop(self):
        got = [r for r in rows(timeline(embc(), bars()))
               if "Bought" in r[2]][0]
        assert "$5.06" in got[2] and "$4.55" in got[2]

    def test_the_sell_row_carries_the_price_and_the_reason(self):
        got = [r for r in rows(timeline(embc(), bars()))
               if "Sold" in r[2]][0]
        assert "$4.97" in got[2]
        assert "hard exit" in got[2], "the exit reason is still an enum"

    def test_an_exit_review_is_distinguished_from_a_hold(self):
        got = " | ".join(r[2] for r in rows(timeline(embc(), bars())))
        assert "decided to EXIT" in got
        assert "and held" in got

    def test_what_claude_SAID_is_on_the_row(self):
        """The owner asked for "what the bot thought when it
        re-evaluated". A row saying only "reviewed" answers nothing."""
        got = " | ".join(r[2] for r in rows(timeline(embc(), bars())))
        assert "Insider buying intact, tape quiet." in got
        assert "Guidance cut; the invalidation has triggered." in got

    def test_a_SKIPPED_review_still_appears_with_its_reason(self):
        """A gap would read as the bot having forgotten the position.
        A skipped review cost nothing and is still an event."""
        got = [r for r in rows(timeline(embc(), bars()))
               if "skipped" in r[2].lower()]
        assert got, "a skipped review vanished from the timeline"
        assert "too soon" in got[0][2]

    def test_an_open_position_has_no_sold_row(self):
        got = " | ".join(r[2] for r in rows(timeline(
            embc(status="open", exit_price="", closed_at="",
                 realized_pnl_cents=None), bars())))
        assert "Sold" not in got
        assert "Bought EMBC" in got


class TestThePriceColumnShowsHowItMoved:
    def test_every_row_carries_the_close_at_that_time(self):
        for when, price, _what in rows(timeline(embc(), bars())):
            assert price.startswith("$"), f"{when} has no price"

    def test_the_move_is_against_the_fill(self):
        got = rows(timeline(embc(), bars()))
        assert any("%" in r[1] for r in got)
        buy = [r for r in got if "Bought" in r[2]][0]
        # The fill is $5.06 and the bar for 17 Aug is $5.26, so the row
        # reports the CLOSE against the fill rather than zero.
        assert "%" in buy[1]

    def test_it_NEVER_reads_a_price_LATER_than_the_row(self):
        """A timeline row says what the price was when the decision was
        taken. Reading forward would show the decision the benefit of
        hindsight."""
        b = bars(first="2026-08-10", n=30, start=5.40, step=-0.02)
        got = rows(timeline(embc(), b))
        by_day = {x.day: float(x.close) for x in b}
        for when, price, _what in got:
            day = datetime.strptime(when + " 2026", "%d %b %Y").date()
            value = float(price.split()[0].lstrip("$"))
            earlier = [v for d, v in by_day.items() if d <= day]
            assert value in earlier, (
                f"{when} shows ${value}, which is not a close on or "
                "before that day")

    def test_a_day_with_no_bar_uses_the_most_recent_earlier_one(self):
        b = [Bar(date(2026, 8, 18), 5.20)]
        got = [r for r in rows(timeline(embc(), b)) if "Sold" in r[2]][0]
        assert "$5.20" in got[1], (
            "a date past the last cached bar lost its price entirely")

    def test_a_day_before_every_bar_has_no_price_rather_than_a_guess(self):
        b = [Bar(date(2026, 8, 28), 5.00)]
        first = rows(timeline(embc(), b))[0]
        assert "$" not in first[1]

    def test_no_bars_at_all_says_so(self):
        html = timeline(embc(), [])
        assert rows(html), "the events vanished with the prices"
        assert "empty rather than guessed" in html


class TestItNeverRaises:
    @pytest.mark.parametrize("bad", [
        {"reviews": [("nonsense", "hold", "s", "w", 0, 0)]},
        {"reviews": [("2026-08-19T12:00:00+00:00",)]},
        {"reviews": [None]},
        {"reviews": "not a list"},
        {"opened_at": ""}, {"closed_at": "not-a-date"},
        {"entry_price": "abc"}, {"exit_price": ""},
        {"catalyst_date": "nope"}, {"stop_price": None},
    ])
    def test_a_broken_field_still_renders_or_returns_empty(self, bad):
        out = timeline(embc(**bad), bars())
        assert isinstance(out, str)

    def test_an_empty_story_renders_nothing_rather_than_a_header(self):
        assert panels._timeline(TradeStory(ticker="X"), "tr", 0) == ""


# ==========================================================================
# The check-in Claude sets
# ==========================================================================

class TestClaudeCanAskWhenToLookAgain:
    def test_the_tool_offers_the_field(self):
        from catalyst.research.position_review import POSITION_REVIEW_TOOL

        props = POSITION_REVIEW_TOOL["input_schema"]["properties"]
        assert "next_check_in_days" in props
        field = props["next_check_in_days"]
        assert field["type"] == "integer"
        assert field["minimum"] == 1

    def test_it_is_NOT_required(self):
        """A review that does not say is a review, not a failure."""
        from catalyst.research.position_review import POSITION_REVIEW_TOOL

        assert "next_check_in_days" not in \
            POSITION_REVIEW_TOOL["input_schema"]["required"]

    def test_the_description_tells_it_to_judge_on_what_is_DUE(self):
        from catalyst.research.position_review import POSITION_REVIEW_TOOL

        got = (POSITION_REVIEW_TOOL["input_schema"]["properties"]
               ["next_check_in_days"]["description"])
        assert "WHEN THE NEXT THING HAPPENS" in got
        assert "costs money" in got

    def test_a_valid_answer_is_carried_through(self):
        review = make_review_from_tool_input("p", "X", {
            "action": "hold", "invalidation_triggered": False,
            "reasoning": "r", "next_check_in_days": 3})
        assert review.next_check_in_days == 3

    @pytest.mark.parametrize("bad", [0, -3, 2.5, "3", True, None, [3]])
    def test_A_BAD_VALUE_IS_DROPPED_NOT_RAISED(self, bad):
        """The action, the invalidation and the reasoning are the
        answer; this is a scheduling preference. Discarding an otherwise
        sound review over it would leave the position unread."""
        review = make_review_from_tool_input("p", "X", {
            "action": "hold", "invalidation_triggered": False,
            "reasoning": "r", "next_check_in_days": bad})
        assert review.action == "hold"
        assert review.next_check_in_days is None

    def test_the_whole_review_still_validates_as_before(self):
        with pytest.raises(ValueError):
            make_review_from_tool_input("p", "X", {
                "action": "nonsense", "invalidation_triggered": False,
                "reasoning": "r", "next_check_in_days": 3})


class TestCodeBoundsTheDate:
    def review(self, days, action="hold"):
        return PositionReview(
            position_id="p", ticker="X", action=action,
            invalidation_triggered=False, reasoning="r",
            next_check_in_days=days)

    POS = {"planned_exit_date": "2026-09-29"}

    def test_a_reasonable_request_is_honoured_exactly(self):
        when, clamped = next_check_at(self.review(3), self.POS, NOW)
        assert when == NOW + timedelta(days=3)
        assert clamped == ""

    def test_a_long_request_is_capped_and_says_so(self):
        when, clamped = next_check_at(self.review(30), self.POS, NOW)
        assert when == NOW + timedelta(days=MAX_CHECK_IN_DAYS)
        assert "capped at" in clamped and "30d" in clamped

    def test_it_is_never_later_than_the_hard_exit_date(self):
        """A review after the position closes is a paid call about
        nothing."""
        when, clamped = next_check_at(
            self.review(7), {"planned_exit_date": "2026-09-01"}, NOW)
        assert when.date() <= date(2026, 9, 1)
        assert "exit date" in clamped

    def test_an_exit_review_schedules_nothing(self):
        assert next_check_at(self.review(3, "exit_now"), self.POS, NOW) == \
            (None, "")

    def test_no_request_means_the_standing_clock(self):
        assert next_check_at(self.review(None), self.POS, NOW) == (None, "")

    def test_an_unparseable_exit_date_still_schedules(self):
        when, _ = next_check_at(
            self.review(3), {"planned_exit_date": "nonsense"}, NOW)
        assert when == NOW + timedelta(days=3)

    def test_the_ceiling_is_stated_and_defended(self):
        """A missed review cannot cost money beyond the stop - the stop
        rests at the broker, the exit date stands, and a review can only
        bring an exit FORWARD. So the cost of waiting is a forgone early
        exit, which bounds how large this may be."""
        assert 2 <= MAX_CHECK_IN_DAYS <= 14


class TestTheScheduleHonoursIt:
    @pytest.fixture
    def db(self, tmp_path):
        conn = init_db(str(tmp_path / "r.db"))
        conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                     ("pos1", "EMBC", "[]", None,
                      "2026-08-17T16:00:00+00:00", "2026-09-29", "open"))
        conn.commit()
        yield conn
        conn.close()

    def record(self, conn, days, action="hold", at=NOW):
        review = PositionReview(
            position_id="pos1", ticker="EMBC", action=action,
            invalidation_triggered=False, reasoning="r", reviewed_at=at,
            next_check_in_days=days)
        return record_review(conn, review, prompt="p", model="m",
                             position={"planned_exit_date": "2026-09-29"})

    def test_the_request_is_stored_and_read_back(self, db):
        self.record(db, 4)
        got = requested_check_at(db, "pos1")
        assert got == NOW + timedelta(days=4)

    def test_both_the_request_and_the_honoured_date_are_kept(self, db):
        """"it asked for 30 and got 7" is the reading that says the
        bound is doing something."""
        self.record(db, 30)
        row = db.execute(
            "SELECT requested_days, next_check_at, clamped_by FROM "
            "position_review_checkins").fetchone()
        assert row[0] == 30
        assert "capped at" in (row[2] or "")

    def test_the_newest_request_supersedes_the_last(self, db):
        self.record(db, 7)
        self.record(db, 1, at=NOW + timedelta(hours=1))
        got = requested_check_at(db, "pos1")
        assert got < NOW + timedelta(days=2), (
            "an older, longer request is still in force")

    def test_nothing_is_stored_when_nothing_was_asked(self, db):
        self.record(db, None)
        assert requested_check_at(db, "pos1") is None
        assert db.execute(
            "SELECT COUNT(*) FROM position_review_checkins").fetchone()[0] == 0

    def test_an_exit_review_stores_no_check_in(self, db):
        self.record(db, 3, action="exit_now")
        assert requested_check_at(db, "pos1") is None

    def test_the_review_itself_is_recorded_regardless(self, db):
        """Scheduling is not the answer. A review whose check-in cannot
        be stored must still be a recorded review."""
        self.record(db, 3)
        assert db.execute(
            "SELECT COUNT(*) FROM position_reviews").fetchone()[0] == 1

    def test_a_database_without_the_table_falls_back_quietly(self, tmp_path):
        import sqlite3
        from pathlib import Path

        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        schema = Path("catalyst/storage/schema.sql").read_text()
        schema = schema.replace("position_review_checkins", "not_that_table")
        conn.executescript(schema)
        conn.commit()
        assert requested_check_at(conn, "pos1") is None
        conn.close()

    def test_due_for_review_waits_for_the_requested_date(self, db):
        from catalyst.research.position_review import due_for_review

        self.record(db, 5)
        pos = {"id": "pos1", "ticker": "EMBC",
               "opened_at_date": date(2026, 8, 17),
               "planned_exit_date": date(2026, 9, 29)}
        # Two days later: the standing 24h clock would review, the
        # requested 5-day date should not.
        to_review, skipped = due_for_review(
            db, [pos], NOW + timedelta(days=2))
        assert not to_review
        assert "Claude asked to be woken" in skipped[0][1]

    def test_it_reviews_once_the_requested_date_arrives(self, db):
        from catalyst.research.position_review import due_for_review

        self.record(db, 5)
        pos = {"id": "pos1", "ticker": "EMBC",
               "opened_at_date": date(2026, 8, 17),
               "planned_exit_date": date(2026, 9, 29)}
        to_review, _ = due_for_review(db, [pos], NOW + timedelta(days=6))
        assert to_review

    def test_NEWS_STILL_BRINGS_IT_FORWARD(self, db):
        """The one thing a long check-in must not be able to suppress."""
        from catalyst.research.position_review import due_for_review

        self.record(db, 7)
        db.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                   ("alpaca_news", "n1",
                    (NOW + timedelta(days=1)).isoformat(),
                    # news_since matches on the `ticker` key, not
                    # `symbols` - my first fixture used the wrong one
                    # and the test failed for its own reason rather
                    # than the code's.
                    '{"ticker": "EMBC", "headline": "Guidance cut"}'))
        db.commit()
        pos = {"id": "pos1", "ticker": "EMBC",
               "opened_at_date": date(2026, 8, 17),
               "planned_exit_date": date(2026, 9, 29)}
        to_review, _ = due_for_review(db, [pos], NOW + timedelta(days=2))
        assert to_review, (
            "a 7-day check-in suppressed a review after news named the "
            "company, which is the one override that must survive")


class TestTheCardShowsTheCheckIn:
    def test_an_open_position_shows_the_date_claude_asked_for(self):
        st = embc(status="open", exit_price="", closed_at="",
                  realized_pnl_cents=None,
                  checkin=(3, "2026-09-02T12:00:00+00:00", ""))
        got = text(panels._next_checkin(st, "tr", 0))
        assert "3 day(s)" in got and "2 Sep" in got

    def test_it_says_when_code_held_it_to_a_bound(self):
        st = embc(status="open", exit_price="", closed_at="",
                  realized_pnl_cents=None,
                  checkin=(30, "2026-09-06T12:00:00+00:00",
                           "asked for 30d, capped at 7d"))
        got = text(panels._next_checkin(st, "tr", 0))
        assert "capped at 7d" in got

    def test_it_says_news_overrides_it(self):
        st = embc(status="open", exit_price="", closed_at="",
                  realized_pnl_cents=None,
                  checkin=(3, "2026-09-02T12:00:00+00:00", ""))
        got = text(panels._next_checkin(st, "tr", 0))
        assert "brings the review forward" in got
        assert "stop and the hard exit date are unaffected" in got

    def test_a_closed_trade_shows_no_check_in(self):
        """A date that will never arrive."""
        assert panels._next_checkin(
            embc(checkin=(3, "2026-09-02T12:00:00+00:00", "")),
            "tr", 0) == ""

    def test_no_check_in_renders_nothing(self):
        assert panels._next_checkin(
            embc(status="open", checkin=None), "tr", 0) == ""


# ==========================================================================
# No emoji
# ==========================================================================

class TestThereAreNoEmoji:
    """Owner-asked 2026-09-12: "remove emojis also we dont need them"."""

    def test_the_step_icons_are_gone(self):
        assert set(panels._STEP_ICON.values()) == {""}

    def test_the_action_icons_are_gone(self):
        assert set(panels._ACTION_ICON.values()) == {""}

    def test_no_pictograph_is_left_in_the_package(self):
        import unicodedata
        from pathlib import Path

        offenders = {}
        for path in sorted(Path("catalyst").rglob("*.py")):
            body = path.read_text()
            for ch in set(body):
                if ord(ch) > 127 and unicodedata.category(ch) == "So":
                    # The four status glyphs are geometric shapes, not
                    # emoji, and they are load-bearing: a status must
                    # never be carried by colour alone, which matters in
                    # greyscale, in print and for a reader who cannot
                    # distinguish the hues. Kept deliberately.
                    if ch in "●▲■○":
                        continue
                    offenders.setdefault(path.as_posix(), set()).add(ch)
            for esc in re.findall(r"\\U0001F[0-9A-Fa-f]{3}", body):
                offenders.setdefault(path.as_posix(), set()).add(esc)
        assert not offenders, f"emoji are back: {offenders}"

    def test_the_headings_still_read_without_them(self):
        """What the icons were for. Removing them must not remove the
        words, which is the whole reason they were safe to remove."""
        html = panels._step("why", "How EMBC was found")
        assert "How EMBC was found" in html

    def test_the_status_glyphs_survive(self):
        """Geometric marks, not emoji, and they carry meaning colour
        must not carry alone."""
        from catalyst.dashboard.render import _PILL_GLYPH, pill

        assert set(_PILL_GLYPH.values()) == {"●", "▲", "■",
                                             "○"}
        assert 'aria-hidden="true"' in pill("good", "fine")


class TestTheCheckCanFail:
    """House rule 4, against what shipped."""

    def test_a_timeline_without_reviews_is_shorter(self):
        with_reviews = len(rows(timeline(embc(), bars())))
        without = len(rows(timeline(embc(reviews=[]), bars())))
        assert without < with_reviews

    def test_the_fixture_really_has_an_exit_review(self):
        assert any(r[1] == "exit_now" for r in embc().reviews)

    def test_removing_the_check_in_field_would_be_caught(self):
        from catalyst.research.position_review import POSITION_REVIEW_TOOL

        props = dict(POSITION_REVIEW_TOOL["input_schema"]["properties"])
        props.pop("next_check_in_days")
        assert "next_check_in_days" not in props
