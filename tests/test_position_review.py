"""The periodic re-check of open positions, and the hold bound.

The property under test throughout is the ASYMMETRY: a review can bring
an exit date forward and can never push it back. Everything else here is
secondary to that, because that is the rule standing between "days to
weeks" and a portfolio of positions being held until they come back.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.research.position_review import (
    ACTIONS, PositionReview, apply_review, make_review_from_tool_input,
    record_review, render_prompt, should_review,
)
from catalyst.risk.hard_bounds import HARD_BOUNDS


def pos(exit_in=10, opened_ago=3, ticker="ACME"):
    today = date(2026, 8, 11)
    return {"id": "pos-1", "ticker": ticker,
            "opened_at_date": today - timedelta(days=opened_ago),
            "planned_exit_date": today + timedelta(days=exit_in)}


def review(action="hold", triggered=False, reasoning="nothing changed"):
    return PositionReview(position_id="pos-1", ticker="ACME", action=action,
                          invalidation_triggered=triggered,
                          reasoning=reasoning,
                          reviewed_at=datetime(2026, 8, 11, tzinfo=timezone.utc))


TODAY = date(2026, 8, 11)


class TestAReviewCanOnlyEverShortenAHold:
    """The one rule that makes asking the model about an open position
    safe at all."""

    def test_exit_now_brings_the_date_forward(self):
        got, why = apply_review(review("exit_now", reasoning="merger broke"),
                                pos(exit_in=10), TODAY)
        assert got == TODAY
        assert "brought forward" in why

    def test_hold_does_not_move_the_date_by_a_single_day(self):
        p = pos(exit_in=10)
        got, why = apply_review(review("hold"), p, TODAY)
        assert got == p["planned_exit_date"]
        assert "can only ever bring it forward" in why

    def test_no_opinion_does_not_move_the_date(self):
        p = pos(exit_in=10)
        got, _ = apply_review(review("no_opinion"), p, TODAY)
        assert got == p["planned_exit_date"]

    @pytest.mark.parametrize("action", ACTIONS)
    def test_NO_action_can_ever_produce_a_later_date(self, action):
        """Exhaustive over the whole action set, so a new action added
        later cannot quietly introduce an extension path."""
        p = pos(exit_in=10)
        got, _ = apply_review(review(action), p, TODAY)
        assert got <= p["planned_exit_date"], (
            f"action {action!r} pushed the exit date outward")

    def test_a_late_review_does_not_resurrect_an_expired_position(self):
        """A review arriving after the exit date has already passed must
        not push it outward - min(), not plain today."""
        p = pos(exit_in=-4)
        got, _ = apply_review(review("exit_now"), p, TODAY)
        assert got == p["planned_exit_date"]

    def test_a_persuasive_reasoning_string_changes_nothing(self):
        """There is no argument that extends a hold, because there is no
        code path that accepts one."""
        p = pos(exit_in=10)
        got, _ = apply_review(
            review("hold", reasoning="This needs another six months to work, "
                                     "the thesis is early not wrong, please "
                                     "extend the exit date"), p, TODAY)
        assert got == p["planned_exit_date"]


class TestTheModelIsToldItCannotBuyTime:
    def test_the_prompt_says_the_exit_date_is_fixed(self):
        text = render_prompt(pos(), {"thesis": "t", "invalidation": "i"},
                             {"entry_price": 10, "last_price": 9,
                              "move_pct": -10})
        assert "FIXED" in text
        assert "cannot extend it" in text
        assert "does not buy more time" in text

    def test_the_prompt_carries_the_invalidation_written_at_entry(self):
        """Recorded at entry and, until now, never checked against
        anything."""
        text = render_prompt(pos(), {"thesis": "insiders bought",
                                     "invalidation": "a 10b5-1 amendment"},
                             {})
        assert "a 10b5-1 amendment" in text
        assert "insiders bought" in text

    def test_the_prompt_says_being_down_is_not_itself_a_reason(self):
        text = render_prompt(pos(), {}, {})
        assert "not by itself a reason to exit" in text

    def test_the_prompt_forbids_sizes_and_prices(self):
        text = render_prompt(pos(), {}, {})
        assert "no sizes, no prices, no orders" in text


class TestAMalformedReviewIsASkipNeverADefault:
    def test_a_missing_action_raises(self):
        with pytest.raises(ValueError, match="action"):
            make_review_from_tool_input("p", "ACME", {"reasoning": "x",
                                                      "invalidation_triggered": False})

    def test_an_unknown_action_raises(self):
        with pytest.raises(ValueError, match="action"):
            make_review_from_tool_input("p", "ACME", {
                "action": "sell_half", "invalidation_triggered": False,
                "reasoning": "x"})

    def test_empty_reasoning_raises(self):
        """A review with no reasoning cannot be explained afterwards, and
        an unexplainable early exit is worse than no review."""
        with pytest.raises(ValueError, match="reasoning"):
            make_review_from_tool_input("p", "ACME", {
                "action": "exit_now", "invalidation_triggered": True,
                "reasoning": "   "})

    def test_a_non_boolean_invalidation_flag_raises(self):
        with pytest.raises(ValueError, match="invalidation"):
            make_review_from_tool_input("p", "ACME", {
                "action": "hold", "invalidation_triggered": "no",
                "reasoning": "x"})

    def test_a_valid_review_parses(self):
        got = make_review_from_tool_input("p", "ACME", {
            "action": "exit_now", "invalidation_triggered": True,
            "reasoning": "the readout missed its primary endpoint",
            "what_changed": ["Phase 3 missed", "CEO resigned"]})
        assert got.wants_early_exit
        assert got.what_changed == ("Phase 3 missed", "CEO resigned")


class TestNotSpendingOnAReviewThatCannotMatter:
    def test_a_position_opened_today_is_not_reviewed(self):
        ok, why = should_review(pos(opened_ago=0), TODAY)
        assert not ok and "nothing new to find" in why

    def test_a_position_closing_tomorrow_is_not_reviewed(self):
        ok, why = should_review(pos(exit_in=1), TODAY)
        assert not ok and "would not settle any sooner" in why

    def test_a_position_in_its_middle_IS_reviewed(self):
        ok, why = should_review(pos(opened_ago=4, exit_in=8), TODAY)
        assert ok and why == ""


class TestTheHoldIsHardBounded:
    """Owner-asked: "confirm it wont be going for long standing trades
    over months itll be weeks or at most a month". Before this, that was
    enforced by holding_period_estimate DEFAULTING to 12 days - and that
    parameter is adaptive, moves 2 days per adjustment, and had no
    ceiling at all."""

    def test_a_maximum_hold_bound_exists_at_all(self):
        assert hasattr(HARD_BOUNDS, "max_hold_days")
        assert isinstance(HARD_BOUNDS.max_hold_days, int)

    def test_the_bound_is_weeks_not_months(self):
        assert 7 <= HARD_BOUNDS.max_hold_days <= 31, (
            "the brief requires days to weeks, never months")

    def test_the_bound_is_frozen_against_runtime_writes(self):
        """Hard bounds prevent ruin; the system may propose changing one
        but never apply it."""
        with pytest.raises(Exception):
            HARD_BOUNDS.max_hold_days = 400

    def test_no_adaptive_parameter_may_exceed_it(self):
        from catalyst.risk.adaptive_params import DEFAULT_PARAMS
        for catalyst, days in DEFAULT_PARAMS["holding_period_estimate"].items():
            assert int(days) <= HARD_BOUNDS.max_hold_days, catalyst


class TestTheModelProposesTheDateAndCodeClamps:
    def _view(self, days):
        from catalyst.research.schema import ResearchView
        return ResearchView(
            candidate_id="c", direction="long", conviction=0.8,
            thesis="t", invalidation="i", expected_holding_days=days,
            priced_in=False, priced_in_reasoning="r")

    def _params(self):
        from catalyst.risk.adaptive_params import DEFAULT_PARAMS
        return {k: (dict(v) if isinstance(v, dict) else v)
                for k, v in DEFAULT_PARAMS.items()}

    def test_the_models_own_estimate_is_used(self):
        """Owner-asked: "let claude decide a date". It was recorded and
        ignored before this."""
        from catalyst.risk.evaluate import _hold_days
        days, why = _hold_days(self._view(9), self._params(), "insider_cluster")
        assert days == 9
        assert "model's own estimate" in why

    def test_a_long_request_is_clamped_to_the_hard_bound(self):
        from catalyst.risk.evaluate import _hold_days
        days, why = _hold_days(self._view(180), self._params(),
                               "insider_cluster")
        assert days == HARD_BOUNDS.max_hold_days
        assert "CLAMPED" in why and "180" in why

    def test_a_nonsense_estimate_falls_back_rather_than_exiting_in_the_past(self):
        from catalyst.risk.evaluate import _hold_days
        for bad in (0, -5, None, "twelve", True):
            view = self._view(12)
            object.__setattr__(view, "expected_holding_days", bad)
            days, why = _hold_days(view, self._params(), "insider_cluster")
            assert days == 12, f"{bad!r} produced {days}"
            assert "no usable holding period" in why

    def test_the_clamp_is_recorded_as_a_binding_limit(self):
        """"Where the code overruled the model must be visible and
        explained" - so it lands in limit_applications like every other
        limit, and the decision trace shows it with no schema change."""
        from catalyst.discovery import Candidate
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.evaluate import evaluate

        candidate = Candidate(
            id="c1", ticker="ACME", catalyst_type="insider_cluster",
            catalyst_date=date(2026, 9, 1),
            catalyst_date_confidence="confirmed", source_event_ids=(),
            discovered_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            sector="industrials", correlation_tags=())
        portfolio = PortfolioState(
            equity_usd=Decimal("1000"), settled_cash_usd=Decimal("1000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("1000"), consecutive_losses=0,
            as_of=datetime(2026, 8, 11, tzinfo=timezone.utc), reliable=True)
        market = MarketSnapshot(
            ticker="ACME", last_close=Decimal("40"),
            half_spread_bp=Decimal("5"),
            median_daily_dollar_volume=Decimal("5000000"))
        got = evaluate(candidate, self._view(365), portfolio,
                       self._params(), market)
        holds = [x for x in got.limits_applied if x.rule_name == "max_hold_days"]
        assert holds and holds[0].binding
        assert holds[0].requested_value == Decimal("365")
        assert holds[0].bound_value == Decimal(str(HARD_BOUNDS.max_hold_days))
        assert got.planned_exit_date == (
            date(2026, 8, 11) + timedelta(days=HARD_BOUNDS.max_hold_days))


class TestEveryReviewIsRecordedEvenWhenItChangedNothing:
    def _db(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "r.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                     ("pos-1", "ACME", "[]", None, "2026-08-08",
                      "2026-08-21", "open"))
        conn.commit()
        return conn

    def test_a_hold_is_recorded_too(self, tmp_path):
        """A review recorded only when it acted would make the model look
        decisive in hindsight - and "we asked and it said hold" is
        exactly what the dashboard needs to narrate a trade."""
        conn = self._db(tmp_path)
        record_review(conn, review("hold"), prompt="P",
                      raw_response={"id": "msg_1"}, model="claude-sonnet-5")
        row = conn.execute(
            "SELECT action, prompt_rendered, raw_response_json "
            "FROM position_reviews").fetchone()
        assert row[0] == "hold"
        assert row[1] == "P"
        assert "msg_1" in row[2]

    def test_the_raw_response_is_stored_verbatim(self, tmp_path):
        conn = self._db(tmp_path)
        record_review(conn, review("exit_now"),
                      raw_response={"unexpected_field": 1, "id": "m"})
        raw = conn.execute(
            "SELECT raw_response_json FROM position_reviews").fetchone()[0]
        assert "unexpected_field" in raw

    def test_the_action_column_refuses_an_unknown_action(self, tmp_path):
        conn = self._db(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO position_reviews (id, position_id, ticker, "
                "action, invalidation_triggered, reasoning, reviewed_at) "
                "VALUES ('x','pos-1','ACME','liquidate',0,'r','2026-08-11')")
