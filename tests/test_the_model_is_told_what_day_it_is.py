"""Every prompt the bot pays for says what today is.

OWNER-ASKED 2026-09-13: *"is the bot 100% aware of the active current
date and time it is making these searches?"*

**IT WAS NOT.** Measured from the owner's own diagnostic bundle -
`catalyst-logic-7d-20260913-125402`, the verbatim `prompt_rendered` for
candidate `conj-b3caf562223b246f8844` (CHYM, called 2026-09-13T00:06) -
the prompt carried "2026-09-08", "2026-09-10" and "Newest signal:
2026-09-10", and **nowhere said what today was**. Neither did the hunt
prompt, whose one hard rule is enforced by code against `as_of.date()`.
Neither did the position-review prompt, which asks for a check-in
"number of days from today".

WHY IT COSTS MONEY RATHER THAN BEING UNTIDY:

    1. **Question 6 is entirely a question about elapsed time.** "Has
       the market already consumed these filings?" gets opposite
       answers for a filing two days old and a filing five weeks old,
       and the difference was not on the page. `priced_in` was set on
       62 of 65 no_trades.
    2. **The hunt's hard date rule is measured from a date the model was
       never given.** 88% of nominations were once refused for a past
       catalyst date; §3 row 12 fixed that by stating the RULE, and the
       date the rule is measured against was still missing.
    3. **The review prompt prints a fixed exit date with no distance to
       it.** Six days left and one day left want different answers.

AND THE SECOND DEFECT THIS FOUND, which is a falsehood rather than a
gap: `build_closed_market_snapshot` sets `half_spread_bp = 100000` as a
deliberately REFUSING sentinel (zero would sail through the owner's 20bp
hard bound as the tightest book ever measured). That went straight into
the prompt as *"half-spread now: 100000 bp. This is what it costs to get
in and out; a thesis worth less than the round trip is not a trade"* - a
1000% round trip, under a heading claiming it was measured, on every
weekend research call.

Fully offline. No test here is anchored to a calendar date: each one
supplies its own clock and asserts against that (house rule 6).
"""

import contextlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.discovery.candidates import Candidate
from catalyst.research import prompts
from catalyst.research.position_review import render_prompt as render_review
from catalyst.risk import MarketSnapshot
from tests.test_boundary import db  # noqa: F401 - fixture used by name


def cand(ticker="CHYM", catalyst_date=None, now=None):
    now = now or datetime.now(timezone.utc)
    return Candidate(
        id="conj-b3caf562223b246f8844", ticker=ticker,
        catalyst_type="financing",
        catalyst_date=catalyst_date or (now.date() - timedelta(days=3)),
        catalyst_date_confidence="estimated",
        source_event_ids=("0001193125-26-385383:credit_amendment",),
        discovered_at=now, sector="6199",
        correlation_tags=("financing", "news"))


def shut_snapshot(ticker="CHYM"):
    """Exactly what `build_closed_market_snapshot` produces."""
    return MarketSnapshot(
        ticker=ticker, last_close=Decimal("32.31"),
        half_spread_bp=Decimal("100000"),
        median_daily_dollar_volume=Decimal("0"),
        priced_off="daily_close")


def live_snapshot(ticker="CHYM", half_spread_bp="8"):
    return MarketSnapshot(
        ticker=ticker, last_close=Decimal("32.31"),
        half_spread_bp=Decimal(half_spread_bp),
        median_daily_dollar_volume=Decimal("5000000"))


class TestTheResearchPromptCarriesTheDate:

    def test_todays_date_is_in_the_prompt(self):
        now = datetime(2026, 9, 13, 0, 6, tzinfo=timezone.utc)
        text = prompts.render_research_prompt(
            cand(now=now), market=shut_snapshot(), now=now)
        assert "2026-09-13" in text, (
            "the prompt does not say what day it is, so every judgement "
            "about how stale the evidence is rests on a guess")

    def test_the_weekday_is_named_too(self):
        """A bare ISO date does not say 'this is a Saturday, the market
        has been shut for a day and EDGAR has filed nothing'."""
        now = datetime(2026, 9, 12, 15, 9, tzinfo=timezone.utc)
        text = prompts.render_research_prompt(
            cand(now=now), market=shut_snapshot(), now=now)
        assert "Saturday" in text

    def test_the_time_is_in_the_prompt(self):
        now = datetime(2026, 9, 13, 0, 6, tzinfo=timezone.utc)
        text = prompts.render_research_prompt(
            cand(now=now), market=shut_snapshot(), now=now)
        assert "00:06" in text and "UTC" in text

    def test_the_date_comes_before_the_dated_evidence(self):
        """Order matters: the clock has to be read before the first
        dated line, not after the judgement has been formed."""
        now = datetime(2026, 9, 13, 0, 6, tzinfo=timezone.utc)
        cat = date(2026, 9, 10)
        text = prompts.render_research_prompt(
            cand(catalyst_date=cat, now=now), market=shut_snapshot(),
            now=now)
        assert text.index("2026-09-13") < text.index("CANDIDATE")

    def test_the_age_of_the_evidence_is_stated_in_days(self):
        """The arithmetic the whole priced_in question turns on, done for
        the model rather than left to it."""
        now = datetime(2026, 9, 13, 0, 6, tzinfo=timezone.utc)
        text = prompts.render_research_prompt(
            cand(catalyst_date=date(2026, 9, 10), now=now),
            market=shut_snapshot(), now=now)
        assert "3 day(s) ago" in text

    def test_evidence_dated_today_says_today(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        text = prompts.render_as_of_section(
            now, shut_snapshot(), cand(catalyst_date=date(2026, 9, 13)))
        assert "is today." in text
        assert "0 day(s) ago" not in text

    def test_a_future_catalyst_is_not_described_as_days_ago(self):
        """A hunted candidate's catalyst date is in the FUTURE by rule.
        Rendering it as '-4 day(s) ago' would be gibberish pointing the
        wrong way."""
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        text = prompts.render_as_of_section(
            now, live_snapshot(), cand(catalyst_date=date(2026, 9, 17)))
        assert "4 day(s) in the FUTURE" in text
        assert "-4" not in text

    def test_no_clock_supplied_still_carries_a_date(self):
        """A caller that forgets must get a CORRECT date, not none - no
        date is the defect being fixed."""
        text = prompts.render_research_prompt(cand(), market=live_snapshot())
        assert datetime.now(timezone.utc).date().isoformat() in text


class TestTheWiringIsAsserted:
    """THE FALLBACK HIDES THE WIRING, and sabotage proved it.

    `render_research_prompt(now=None)` reads the real clock, which is the
    right behaviour and makes "the date is present" true whether or not
    anything passes one. Two sabotages - removing `now=now` from
    `investigate`, and removing it from `cycle.py`'s call - both came back
    GREEN against the rest of this module for exactly that reason.

    Section 6 of the memory doc, fifth instance: **assert the call site,
    not just the function.** And one behavioural test that only a
    threaded clock can pass.
    """

    def test_investigate_renders_the_clock_IT_WAS_GIVEN(self, db):
        """Behavioural, not a grep: a date the real clock cannot produce
        must appear in the prompt actually sent."""
        from catalyst.research import boundary
        from tests.test_boundary import (
            candidate as bcandidate, ctx, end_turn, extraction_response,
            transport_script,
        )

        given = datetime(2019, 3, 14, 7, 5, tzinfo=timezone.utc)
        transport, log = transport_script([end_turn(), extraction_response()])
        boundary.investigate(bcandidate(), ctx(db), transport, now=given)
        assert log, "no request was sent"
        sent = log[0]["messages"][0]["content"][0]["text"]
        assert "2019-03-14" in sent, (
            "investigate did not pass its `now` to the prompt renderer, so "
            "the date in the prompt is whatever the clock says rather than "
            "the decision time the cycle recorded")
        assert "Thursday" in sent and "07:05" in sent

    def test_a_real_cycle_renders_ITS_OWN_clock_into_the_prompt(
            self, tmp_path, monkeypatch):
        """THE LIVE CALLER, exercised rather than grepped.

        A grep for `now=now)` in `run_cycle` passed the sabotage: the
        function contains a SECOND one (`ensure_history(..., now=now)`),
        so removing the research call's kept the assertion true. **A
        substring that also occurs elsewhere is not a call-site
        assertion.**

        So this runs a whole cycle with a clock the wall clock cannot
        produce and reads the prompt the cycle actually recorded. It
        cannot reuse `test_the_weekend_is_not_wasted.run` - that module
        autouse-stubs `render_research_prompt` to a constant, which is
        the one thing this test needs real.
        """
        import sqlite3

        import catalyst.risk.kill_switches as kill_switches
        from catalyst.data import RawEvent
        from catalyst.orchestrator.cycle import run_cycle
        from catalyst.storage import init_db
        from tests.test_the_weekend_is_not_wasted import (
            NOW, bars_for, broker_for, candidate as wcandidate,
            model_transport,
        )

        # THE TEST IS ONLY MEANINGFUL WHILE THE CYCLE'S CLOCK DIFFERS
        # FROM THE WALL CLOCK, so that is asserted rather than assumed:
        # if they coincided, wired and unwired code would render the same
        # date and this would pass vacuously (house rule 6, and §6's
        # "a vacuously true assertion").
        assert NOW.date() != datetime.now(timezone.utc).date(), (
            "the harness clock is today, so this test can no longer tell "
            "a threaded clock from the wall clock")

        class _Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        monkeypatch.setattr(kill_switches, "datetime", _Frozen)

        conn = init_db(str(tmp_path / "c.db"))
        try:
            broker, _state = broker_for(market_open=False)
            cands = [wcandidate()]
            report = run_cycle(
                conn, broker, model_transport(),
                feed_fetch=lambda s, u: [RawEvent(
                    source="edgar_form4", source_id="acc-1", fetched_at=NOW,
                    payload_raw={"accession": "acc-1"})],
                build_candidates_fn=lambda evs, as_of: cands,
                cluster_fn=lambda cs, ops: {c.id: "tech-w34" for c in cs},
                now=NOW, bars_dir=bars_for(tmp_path))
            assert report.funnel.get("researched") == 1, (
                "the cycle researched nothing, so this test cannot see "
                f"what it is guarding: {report.funnel}")
            rows = conn.execute(
                "SELECT prompt_rendered FROM research_calls "
                "WHERE prompt_rendered != ''").fetchall()
            assert rows, "no prompt was recorded"
            assert NOW.date().isoformat() in rows[0][0], (
                "the cycle did not hand investigate() its own clock, so "
                "the prompt's date is whatever the wall clock says rather "
                "than the decision time every row is stamped with")
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def test_the_review_hands_its_own_clock_to_its_prompt(self):
        import inspect

        from catalyst.research import position_review

        src = inspect.getsource(position_review.review_position)
        assert "render_prompt(position, view, market, now=now)" in src, (
            "the review renders its prompt without the clock it already "
            "computed, so the prompt's date can differ from the row's")


class TestTheMarketStateIsStated:

    def test_a_closed_book_is_called_shut(self):
        now = datetime(2026, 9, 13, 0, 6, tzinfo=timezone.utc)
        text = prompts.render_research_prompt(
            cand(now=now), market=shut_snapshot(), now=now)
        assert "SHUT" in text
        assert "cached daily close" in text.lower()

    def test_a_live_book_is_not_called_shut(self):
        now = datetime(2026, 9, 15, 14, 32, tzinfo=timezone.utc)
        text = prompts.render_research_prompt(
            cand(now=now), market=live_snapshot(), now=now)
        assert "SHUT" not in text
        assert "OPEN" in text

    def test_no_snapshot_claims_neither(self):
        """None is not 'closed'. Nobody looked, and saying the market is
        shut when nobody looked is the same mistake in a different
        direction."""
        now = datetime(2026, 9, 15, 14, 32, tzinfo=timezone.utc)
        text = prompts.render_as_of_section(now, None, cand(now=now))
        assert "SHUT" not in text
        assert "is OPEN" not in text
        assert "2026-09-15" in text

    def test_market_is_live_classifies_by_the_provenance_rule(self):
        """House rule 7: by the rule, not by a list of known strings. Any
        provenance that is not the live one is not live, including a
        value invented after this test was written."""
        assert prompts.market_is_live(live_snapshot()) is True
        assert prompts.market_is_live(shut_snapshot()) is False
        assert prompts.market_is_live(None) is None
        odd = live_snapshot()
        object.__setattr__(odd, "priced_off", "some_future_cache")
        assert prompts.market_is_live(odd) is False


class TestTheRefusingSentinelIsNeverShownAsASpread:
    """`half_spread_bp = 100000` is a gate, not a measurement."""

    def test_the_closed_market_number_does_not_reach_the_prompt(self):
        now = datetime(2026, 9, 13, 0, 6, tzinfo=timezone.utc)
        text = prompts.render_research_prompt(
            cand(now=now), market=shut_snapshot(), now=now)
        assert "100000" not in text, (
            "the model is being told the round trip costs 1000%, which "
            "kills every thesis that exists")

    def test_it_says_the_spread_is_unmeasurable_rather_than_saying_nothing(self):
        """Silence is not the fix: the model would fill it, and the round
        trip is a real cost on the microcaps this screen surfaces (BWFG
        measured 99.2bp and was refused)."""
        text = prompts.render_market_section(shut_snapshot())
        assert "NOT MEASURABLE" in text
        assert "cheap or expensive" in text

    def test_a_real_measured_spread_is_still_shown_with_its_warning(self):
        text = prompts.render_market_section(live_snapshot("BWFG", "99.2"))
        assert "99.2 bp" in text
        assert "worth less than the round trip is not a trade" in text

    def test_the_risk_engine_still_sees_the_refusing_value(self):
        """The sentinel exists for the spread gate and must be untouched
        by a change to how it is DESCRIBED."""
        assert shut_snapshot().half_spread_bp == Decimal("100000")


class TestTheHuntPromptCarriesTheDate:
    """The prompt whose one hard rule is measured against a date it never
    named."""

    def test_the_date_is_stated(self):
        as_of = datetime(2026, 9, 13, 0, 3, tzinfo=timezone.utc)
        from catalyst.discovery.hunt import render_hunt_prompt

        text = render_hunt_prompt([], as_of)
        assert "2026-09-13" in text
        assert "Sunday" in text

    def test_the_date_is_stated_before_the_hard_date_rule(self):
        """The rule says "today or later"; the date it is measured from
        has to be on the page before it."""
        as_of = datetime(2026, 9, 13, 0, 3, tzinfo=timezone.utc)
        from catalyst.discovery.hunt import render_hunt_prompt

        text = render_hunt_prompt([], as_of)
        assert text.index("2026-09-13") < text.index("HARD RULE ON DATES")

    def test_the_date_is_the_one_validation_measures_against(self):
        """The prompt and the gate must not be able to disagree: both
        read `as_of`, so a nomination dated the stated day passes and the
        day before does not."""
        as_of = datetime(2026, 9, 13, 0, 3, tzinfo=timezone.utc)
        from catalyst.discovery.hunt import _validate, render_hunt_prompt

        text = render_hunt_prompt([], as_of)
        assert as_of.date().isoformat() in text
        rejected: list = []

        class Event:
            payload_raw = '{"ticker": "ACME", "headline": "ACME PDUFA"}'

        feed = {"e1": Event()}
        nom = {"ticker": "ACME", "catalyst_type": "fda_decision",
               "catalyst_date": as_of.date().isoformat(),
               "catalyst_date_confidence": "estimated",
               "source_event_ids": ["e1"], "mechanism": "m",
               "why_now": "w"}
        assert _validate(nom, feed, as_of, rejected) is not None, \
            f"a nomination dated the stated day was refused: {rejected}"
        nom_yesterday = dict(
            nom, catalyst_date=(as_of.date() - timedelta(days=1)).isoformat())
        assert _validate(nom_yesterday, feed, as_of, rejected) is None
        assert any("in the past" in why for _t, why in rejected)


class TestTheReviewPromptCarriesTheDate:

    def review_position(self, now, opened_ago=3, exit_in=10):
        return {"id": "pos-1", "ticker": "ACME",
                "opened_at_date": (now.date()
                                   - timedelta(days=opened_ago)).isoformat(),
                "planned_exit_date": (now.date()
                                      + timedelta(days=exit_in)).isoformat()}

    def test_todays_date_is_in_the_prompt(self):
        now = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc)
        text = render_review(self.review_position(now), {}, {}, now=now)
        assert "2026-09-13" in text
        assert "Sunday" in text

    def test_it_says_how_many_days_remain(self):
        """It printed a fixed exit date and never how far away it was,
        while asking for a check-in measured in days from today."""
        now = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc)
        text = render_review(self.review_position(now, exit_in=6), {}, {},
                             now=now)
        assert "6 day(s) remain" in text

    def test_it_says_how_long_it_has_been_held(self):
        now = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc)
        text = render_review(self.review_position(now, opened_ago=4), {}, {},
                             now=now)
        assert "Held 4 day(s)" in text

    def test_an_exit_date_today_is_not_reported_as_days_remaining(self):
        now = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc)
        text = render_review(self.review_position(now, exit_in=0), {}, {},
                             now=now)
        assert "exit date is TODAY" in text
        assert "0 day(s) remain" not in text

    def test_an_exit_date_already_past_says_so(self):
        """EMBC sat past its exit date for two days. A prompt claiming
        '-2 day(s) remain' would read as time in hand."""
        now = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc)
        text = render_review(self.review_position(now, exit_in=-2), {}, {},
                             now=now)
        assert "passed 2 day(s) ago" in text
        assert "-2" not in text

    @pytest.mark.parametrize("bad", ["", None, "not-a-date", "2026-13-99"])
    def test_an_unreadable_date_produces_no_day_count_at_all(self, bad):
        """A confident wrong number of days is worse than none. House
        rule 3's sibling: the fact is unavailable, so say nothing about
        it rather than computing from a guess."""
        now = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc)
        pos = self.review_position(now)
        pos["planned_exit_date"] = bad
        text = render_review(pos, {}, {}, now=now)
        assert "day(s) remain" not in text
        assert "could not be computed" in text
        # The clock itself is still stated - one unreadable field must
        # not cost the whole block.
        assert "2026-09-13" in text

    def test_the_prompt_still_says_the_exit_date_is_fixed(self):
        """The property that must survive: a review can only bring an
        exit forward. Telling the model how many days are left must not
        read as an invitation to ask for more."""
        now = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc)
        text = render_review(self.review_position(now), {}, {}, now=now)
        assert "FIXED" in text and "cannot extend it" in text


class TestNothingAboutMoneyChanged:
    """`now` reaches prompt text and nothing else. The one rule that does
    not move."""

    def test_the_research_tool_schema_gained_no_field(self):
        from catalyst.research.boundary import SUBMIT_RESEARCH_VIEW_TOOL

        props = SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["properties"]
        assert "now" not in props
        assert "date" not in props
        assert len(props) == 8, (
            "the submission tool has eight fields and none of them is a "
            "number that touches money")

    def test_the_new_block_names_no_threshold_and_no_size(self):
        """`test_conviction_is_defined` holds that the floor never leaks
        from the prompt as a whole. This holds it for the block added
        here, which is the part that talks about what happens next and is
        therefore where a bar or a size would be tempting to mention."""
        now = datetime(2026, 9, 13, 0, 6, tzinfo=timezone.utc)
        block = prompts.render_as_of_section(now, shut_snapshot(),
                                            cand(now=now)).lower()
        for banned in ("conviction", "floor", "0.5", "0.6", "threshold",
                       "shares", "position size", "$"):
            assert banned not in block, f"the clock block mentions {banned!r}"

    def test_sizing_takes_no_clock(self):
        """The model never sizes, and a date the model was shown must not
        become an input to how large anything is."""
        import inspect

        from catalyst.risk import sizing

        params = inspect.signature(sizing.size).parameters
        assert "now" not in params and "as_of" not in params
        assert "date" not in params
