"""The position review must actually RUN.

The module, its schema table and 30-odd tests all existed and nothing
called it. `grep -rn position_review` outside its own file and its own
tests returned nothing: the periodic "should we still be holding this?"
check the owner asked for had never executed once.

A library with tests and no caller passes CI forever while doing
nothing, so these tests drive the LIVE PATH - run_cycle - rather than
the module's functions.

The safety rule under all of it: A REVIEW CAN ONLY EVER SHORTEN A HOLD,
NEVER EXTEND IT. Tests for both directions are below, and the extend
direction matters more.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst.orchestrator.cycle import run_cycle
from catalyst.research import position_review

#: Anchored to the REAL clock. A fixed wall-clock hour goes stale as
#: the day passes it and trips portfolio_state_stale, which is how a
#: suite rots with the calendar - the exact failure this branch already
#: had to fix in test_risk.py.
NOW = datetime.now(timezone.utc)


def _broker_handler(request):
    path = request.url.path
    if path.endswith("/account"):
        return httpx.Response(200, json={
            "id": "a", "equity": "1000", "cash": "1000",
            "last_equity": "1000", "buying_power": "1000",
            "non_marginable_buying_power": "1000", "status": "ACTIVE"})
    if path.endswith("/clock"):
        return httpx.Response(200, json={"is_open": True})
    if "/quotes/latest" in path:
        return httpx.Response(200, json={"quote": {
            "ap": 10.05, "bp": 9.95, "as": 100, "bs": 100,
            "t": datetime.now(timezone.utc).isoformat()}})
    if "/positions" in path or "/orders" in path:
        return httpx.Response(200, json=[])
    return httpx.Response(200, json={})


def _broker():
    from catalyst.execution.broker import Broker

    return Broker("k", "s", transport=httpx.MockTransport(_broker_handler),
                  backoff_s=0)


def _review_response(action, triggered=False):
    return {
        "id": "m", "model": position_review.REVIEW_MODEL,
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "id": "t",
                     "name": "submit_position_review",
                     "input": {"action": action,
                               "invalidation_triggered": triggered,
                               "reasoning": "the readout missed",
                               "what_changed": ["phase 3 failed"]}}],
        "usage": {"input_tokens": 900, "output_tokens": 80},
    }


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "r.db")
    conn = sqlite3.connect(path)
    conn.executescript(open("catalyst/storage/schema.sql").read())
    conn.commit()
    conn.close()
    return path


def _seed_open_position(conn, *, opened_days_ago=5, exits_in_days=10):
    """One open position with a thesis, an entry fill and an exit date.

    Positional inserts, matching tests/test_cycle.py: naming columns here
    means guessing the schema, and a seed that drifts from the schema
    fails for a reason that has nothing to do with what is under test.
    """
    opened = NOW - timedelta(days=opened_days_ago)
    opened_iso = opened.isoformat()
    exit_date = (NOW + timedelta(days=exits_in_days)).date()
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("cand-1", "ACME", "insider_cluster",
                  exit_date.isoformat(), "confirmed", "[]", opened_iso,
                  "bio", "[]"))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 ("cand-1", "long", 0.8,
                  "insiders bought before a phase 3 readout",
                  "the phase 3 readout misses its primary endpoint",
                  10, 0, "no move since the filings"))
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("ord-1", "cand-1", "b1", "buy", "20", "market", "day",
                  opened_iso, "filled", "{}"))
    conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                 ("ord-1", "10.00", "20", opened_iso, "10.00"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("dec-1", "cand-1", "trade", "long", "200", "20", "9.00",
                  exit_date.isoformat(), "[]", "{}", opened_iso))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 ("pos-1", "ACME", json.dumps(["ord-1"]), None,
                  opened_iso, exit_date.isoformat(), "open"))
    conn.commit()
    return exit_date


def _run(conn, transport, now=NOW):
    return run_cycle(conn, _broker(), transport,
                     lambda since, until: [],
                     lambda events, as_of: [],
                     lambda fresh, open_pos: {}, now=now)


def _exit_date(conn):
    row = conn.execute(
        "SELECT planned_exit_date FROM positions WHERE id='pos-1'").fetchone()
    return datetime.fromisoformat(row[0]).date()


class TestTheReviewActuallyRuns:
    def test_an_open_position_is_reviewed_by_the_live_cycle(self, db):
        """THE WHOLE POINT. Before this, run_cycle never called the
        module at all."""
        conn = sqlite3.connect(db)
        _seed_open_position(conn)
        calls = []

        def transport(payload):
            calls.append(payload)
            return _review_response("hold")

        _run(conn, transport)
        rows = conn.execute(
            "SELECT ticker, action FROM position_reviews").fetchall()
        conn.close()
        assert calls, "the cycle made no model call for the open position"
        assert rows == [("ACME", "hold")], rows

    def test_the_review_is_recorded_even_when_it_changes_nothing(self, db):
        """"We asked and the model said hold" is the evidence the
        dashboard needs to narrate the trade afterwards."""
        conn = sqlite3.connect(db)
        _seed_open_position(conn)
        _run(conn, lambda p: _review_response("hold"))
        row = conn.execute(
            "SELECT reasoning, prompt_rendered, raw_response_json "
            "FROM position_reviews").fetchone()
        conn.close()
        assert row[0] == "the readout missed"
        assert "THE THESIS WRITTEN AT ENTRY" in (row[1] or "")
        assert json.loads(row[2])["content"][0]["name"] == \
            "submit_position_review"

    def test_the_prompt_carries_the_thesis_and_the_invalidation(self, db):
        """A review with no thesis in front of it is asking the model to
        judge something it cannot see."""
        conn = sqlite3.connect(db)
        _seed_open_position(conn)
        _run(conn, lambda p: _review_response("hold"))
        prompt = conn.execute(
            "SELECT prompt_rendered FROM position_reviews").fetchone()[0]
        conn.close()
        assert "insiders bought before a phase 3 readout" in prompt
        assert "misses its primary endpoint" in prompt


class TestTheAsymmetry:
    def test_exit_now_BRINGS_THE_DATE_FORWARD(self, db):
        conn = sqlite3.connect(db)
        original = _seed_open_position(conn, exits_in_days=10)
        _run(conn, lambda p: _review_response("exit_now", triggered=True))
        moved = _exit_date(conn)
        conn.close()
        assert moved < original, (
            f"exit_now left the date at {moved}; it should be brought "
            f"forward from {original}")
        assert moved == NOW.date()

    def test_hold_CANNOT_PUSH_THE_DATE_OUT(self, db):
        """The failure mode this whole feature is shaped around: a
        losing position always has a story, each review buys another
        week, and 'days to weeks' becomes 'until it comes back'."""
        conn = sqlite3.connect(db)
        original = _seed_open_position(conn, exits_in_days=3)
        _run(conn, lambda p: _review_response("hold"))
        after = _exit_date(conn)
        conn.close()
        assert after == original, (
            f"hold moved the exit date from {original} to {after} - a "
            "review must never extend a hold")

    def test_no_opinion_leaves_the_date_alone(self, db):
        conn = sqlite3.connect(db)
        original = _seed_open_position(conn, exits_in_days=6)
        _run(conn, lambda p: _review_response("no_opinion"))
        after = _exit_date(conn)
        conn.close()
        assert after == original

    def test_a_brought_forward_exit_is_ACTED_ON_the_same_pass(self, db):
        """The review runs before the hard-exit sweep specifically so an
        exit does not wait fifteen minutes for the next cycle."""
        conn = sqlite3.connect(db)
        _seed_open_position(conn, exits_in_days=10)
        sells = []

        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/orders"):
                sells.append(json.loads(request.content))
                return httpx.Response(200, json={
                    "id": "sell-1", "status": "accepted", "qty": "20"})
            return _broker_handler(request)

        from catalyst.execution.broker import Broker
        broker = Broker("k", "s", transport=httpx.MockTransport(handler),
                        backoff_s=0)
        run_cycle(conn, broker, lambda p: _review_response("exit_now", True),
                  lambda since, until: [], lambda events, as_of: [],
                  lambda fresh, open_pos: {}, now=NOW)
        conn.close()
        assert any(o.get("side") == "sell" for o in sells), (
            "the exit was brought forward to today but no sell was placed "
            f"in the same pass; orders seen: {sells}")


class TestTheCostBound:
    def test_a_position_is_not_reviewed_twice_in_the_interval(self, db):
        """THE MONEY, and the reason this was never wired. The cycle
        runs every 15 minutes: without an interval, five positions would
        be reviewed 26 times a day each."""
        conn = sqlite3.connect(db)
        _seed_open_position(conn)
        calls = []

        def transport(payload):
            calls.append(payload)
            return _review_response("hold")

        for minutes in (0, 15, 30, 45):
            _run(conn, transport, now=NOW + timedelta(minutes=minutes))
        conn.close()
        assert len(calls) == 1, (
            f"{len(calls)} paid review calls across four cycles in one "
            "hour - the review interval is not bounding anything")

    def test_the_interval_ELAPSING_allows_another_review(self, db):
        """The bound must not be a one-shot: the point is a periodic
        check."""
        conn = sqlite3.connect(db)
        _seed_open_position(conn, opened_days_ago=5, exits_in_days=20)
        calls = []

        def transport(payload):
            calls.append(payload)
            return _review_response("hold")

        _run(conn, transport, now=NOW)
        _run(conn, transport,
             now=NOW + timedelta(hours=position_review.REVIEW_INTERVAL_HOURS + 1))
        conn.close()
        assert len(calls) == 2, (
            f"{len(calls)} calls - after the interval elapsed the position "
            "should be reviewed again")

    def test_a_position_opened_today_is_not_reviewed(self, db):
        conn = sqlite3.connect(db)
        _seed_open_position(conn, opened_days_ago=0)
        calls = []
        _run(conn, lambda p: calls.append(p) or _review_response("hold"))
        conn.close()
        assert calls == [], "a position opened today has nothing new to find"

    def test_a_position_closing_tomorrow_is_not_reviewed(self, db):
        conn = sqlite3.connect(db)
        _seed_open_position(conn, exits_in_days=1)
        calls = []
        _run(conn, lambda p: calls.append(p) or _review_response("hold"))
        conn.close()
        assert calls == [], (
            "an early exit would not settle sooner than the existing one")

    def test_every_skip_names_itself_in_the_funnel(self, db):
        """A position silently never reviewed looks identical to one
        reviewed and always held."""
        conn = sqlite3.connect(db)
        _seed_open_position(conn, opened_days_ago=0)
        report = _run(conn, lambda p: _review_response("hold"))
        conn.close()
        reasons = " ".join(report.drop_reasons.get("positions_reviewed", []))
        assert "ACME" in reasons and "day" in reasons.lower(), reasons


class TestFailuresDoNotTakeTheBookWithThem:
    def test_a_transport_error_is_a_recorded_skip_not_a_crash(self, db):
        conn = sqlite3.connect(db)
        original = _seed_open_position(conn)

        def boom(payload):
            raise RuntimeError("529 overloaded")

        report = _run(conn, boom)
        skipped = conn.execute(
            "SELECT skipped_reason FROM position_reviews").fetchone()
        after = _exit_date(conn)
        conn.close()
        assert skipped and "transport_error" in skipped[0]
        assert after == original, "a failed review must not move the date"
        assert any("ACME" in r for r in
                   report.drop_reasons.get("positions_reviewed", []))

    def test_a_malformed_review_is_a_skip_never_a_default(self, db):
        """Defaulting to hold lets a broken model keep every position to
        term; defaulting to exit_now liquidates the book."""
        conn = sqlite3.connect(db)
        original = _seed_open_position(conn)

        def bad(payload):
            return {"id": "m", "model": position_review.REVIEW_MODEL,
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "t",
                                 "name": "submit_position_review",
                                 "input": {"action": "sell_everything"}}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}

        _run(conn, bad)
        row = conn.execute(
            "SELECT action, skipped_reason FROM position_reviews "
            "ORDER BY reviewed_at DESC").fetchone()
        after = _exit_date(conn)
        conn.close()
        assert row[1], "a malformed review must record WHY it was skipped"
        assert after == original

    def test_two_tool_calls_are_ambiguous_not_first_wins(self, db):
        """A model that answered twice did not answer once, and picking
        one could close a position on the answer it discarded."""
        conn = sqlite3.connect(db)
        original = _seed_open_position(conn)

        def two(payload):
            forced = (payload.get("tool_choice") or {}).get("type") == "tool"
            block = {"type": "tool_use", "id": "t",
                     "name": "submit_position_review",
                     "input": {"action": "exit_now",
                               "invalidation_triggered": True,
                               "reasoning": "r"}}
            return {"id": "m", "model": position_review.REVIEW_MODEL,
                    "stop_reason": "tool_use",
                    "content": [block, dict(block, id="t2")],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}

        _run(conn, two)
        after = _exit_date(conn)
        conn.close()
        assert after == original, (
            "two contradictory-in-principle tool calls were treated as one "
            "answer and moved the exit date")

    def test_the_review_never_spends_without_the_governor(self, db):
        """Every review call is authorized, exactly like research."""
        conn = sqlite3.connect(db)
        _seed_open_position(conn)
        _run(conn, lambda p: _review_response("hold"))
        governed = conn.execute(
            "SELECT decision FROM cost_governor_events").fetchall()
        priced = conn.execute(
            "SELECT component FROM cost_events").fetchall()
        conn.close()
        assert governed, "the review call was made with no governor decision"
        assert any(r[0] == "position_review" for r in priced), (
            f"the review's spend was not attributed to it: {priced}")
