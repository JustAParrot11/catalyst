"""A protective stop is never submitted into a working entry order.

OWNER-REPORTED, 2026-08-17, on the bot's first ever trade:

    "position 85fb5edc-a8f5-4bd8-a6a1-0b91c4953b4c is unprotected
     (checked 2026-08-17T16:43:57.192830+00:00)"

WHAT ACTUALLY HAPPENED. The entry was a market buy for 79.1295 shares of
EMBC. `_poll_entry_fill` returned the moment ANY quantity had filled -
but the buy order itself was still working at the broker. Alpaca refuses
an opposite-side order while one is live:

    40310000: potential wash trade detected. use complex orders

so the sell stop was rejected on submission. The position was naked from
16:28:59 until the next cycle's confirm_stops_resting armed it at
16:43:57 - fifteen minutes of an unprotected position, on a stop that
was never going to be accepted.

TWO SEPARATE BUGS, AND BOTH ARE TESTED HERE:

  1. Polling asked the wrong question. "Has anything filled?" is not
     "has the order finished?" - a partial fill answers yes to the first
     and no to the second. It now polls on the ORDER's state.
  2. The stop was submitted anyway, into a refusal that was certain.
     Skipping it changes no risk outcome (rejected and not-sent leave
     the position equally unprotected) but it stops the record claiming
     the broker refused us when in fact we knew better than to ask.

WHAT THIS FIX IS NOT. It does not close the unprotected window; it
shortens it and stops it happening for no reason. Alpaca's bracket and
OTO order types would remove the gap entirely by attaching the stop to
the entry atomically - and they do not accept fractional quantities,
which every position here has. That constraint is asserted at the bottom
so nobody re-derives it.
"""

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from catalyst.execution.broker import BrokerError
from catalyst.orchestrator.cycle import (
    _WORKING_ORDER_STATES, _poll_entry_fill,
)
from tests.test_stress_stage5 import (      # noqa: F401 - shared fixtures
    ACCOUNT, QUOTE, brk, candidate, db, frozen_kill_switch_clock,
    model_transport, run, stub_prompts,
)


class FakeBroker:
    """Answers get_order from a script, one entry per poll attempt.

    Deliberately not a Broker: this is about the polling rule, and a
    real transport would only add noise between the rule and its test.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def get_order(self, broker_order_id):
        self.calls += 1
        step = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        return step


def order(status, filled="0"):
    return {"id": "brok-1", "status": status, "filled_qty": filled}


def poll(script, attempts=5):
    b = FakeBroker(script)
    qty, working = _poll_entry_fill(b, "brok-1", attempts=attempts,
                                    interval_s=0)
    return qty, working, b.calls


# =====================================================================
# 1. The polling rule
# =====================================================================

class TestPollingAsksAboutTheOrderNotTheFill:
    def test_THE_DEFECT_a_partial_fill_is_not_a_finished_order(self):
        """The exact shape of the owner's report. Before the fix this
        returned (79.1295, ...) with no way for the caller to know the
        buy was still live, and the stop went out and was refused."""
        qty, working, _ = poll([order("partially_filled", "79.1295")])
        assert qty == Decimal("79.1295")
        assert working is True, (
            "a partially filled order that is still working was reported "
            "as done - the sell stop that follows is a certain wash-trade "
            "rejection")

    def test_a_filled_order_is_done_and_returns_at_once(self):
        """The ordinary case must not have become slower. One round trip,
        however large the budget."""
        qty, working, calls = poll([order("filled", "100")], attempts=15)
        assert (qty, working, calls) == (Decimal("100"), False, 1)

    def test_it_waits_out_a_working_order_and_arms_in_the_SAME_cycle(self):
        """The actual win. An entry that takes a few seconds used to cost
        a full cycle of exposure; now it is protected on the same pass."""
        qty, working, calls = poll([
            order("new", "0"),
            order("partially_filled", "40"),
            order("filled", "79.1295"),
        ], attempts=15)
        assert (qty, working) == (Decimal("79.1295"), False)
        assert calls == 3, "it kept polling after the order was finished"

    def test_a_budget_that_runs_out_reports_the_order_still_working(self):
        qty, working, calls = poll([order("partially_filled", "12")],
                                   attempts=4)
        assert (qty, working, calls) == (Decimal("12"), True, 4)

    @pytest.mark.parametrize("status", sorted(_WORKING_ORDER_STATES))
    def test_every_working_state_is_waited_on(self, status):
        _, working, _ = poll([order(status, "1")], attempts=2)
        assert working is True, f"{status!r} was treated as finished"

    @pytest.mark.parametrize("status", [
        "filled", "canceled", "expired", "rejected", "done_for_day",
        "replaced",
    ])
    def test_every_finished_state_releases_the_stop(self, status):
        _, working, _ = poll([order(status, "5")], attempts=3)
        assert working is False, f"{status!r} was treated as still working"


class TestTheUnknownStateFallsTheSafeWay:
    """HOUSE RULE 7 - classify by the rule, not by enumeration. A status
    nobody listed must not hold protection back."""

    @pytest.mark.parametrize("status", ["", "some_new_alpaca_state", "FILLED"])
    def test_an_unrecognised_status_lets_the_stop_be_placed(self, status):
        _, working, _ = poll([order(status, "5")])
        assert working is False, (
            f"{status!r} was treated as working, so the stop is withheld "
            "on a status nobody had thought of. If the guess is wrong the "
            "broker rejects the stop and the position is unprotected - "
            "exactly where withholding it leaves us - so trying costs a "
            "rejected order row and never costs protection")

    def test_an_unreadable_status_is_not_an_exception(self):
        _, working, _ = poll([{"id": "x", "filled_qty": "5"}])
        assert working is False

    def test_a_broker_that_will_not_answer_is_treated_as_working(self):
        """The one case that falls the other way, and it is not the same
        judgement: an unreadable STATUS is a fact about a state we can
        see, while an unanswered request is no information at all. The
        order may be live; submitting into it blind is how the owner's
        rejection happened."""
        qty, working, _ = poll([order("partially_filled", "9"),
                                BrokerError("connection reset")])
        assert working is True
        assert qty == Decimal("9"), "the last known fill was thrown away"

    def test_garbage_filled_qty_still_opens_a_recoverable_position(self):
        """DEFECT 10's behaviour is unchanged: unreadable quantity means
        'nothing filled yet', the position is recorded unprotected and
        the next cycle arms it. Asserted here because this fix touched
        the same return path."""
        qty, working, _ = poll([order("filled", "1,000")])
        assert (qty, working) == (Decimal("0"), False)


# =====================================================================
# 2. End to end: what the cycle does with the answer
# =====================================================================

def broker_with_entry(status, filled, posts):
    """A paper broker whose BUY sits in `status` with `filled` done."""
    def handler(request):
        url = str(request.url)
        if "/v2/account" in url:
            return httpx.Response(200, json=dict(ACCOUNT))
        if "/v2/clock" in url:
            return httpx.Response(200, json={"is_open": True})
        if "/quotes/latest" in url:
            return httpx.Response(200, json=dict(QUOTE))
        if request.method == "POST" and url.endswith("/v2/orders"):
            body = json.loads(request.content)
            posts.append(body)
            return httpx.Response(200, json={"id": f"brok-{len(posts)}",
                                             "status": "accepted"})
        if request.method == "GET" and "/v2/orders/" in url:
            return httpx.Response(200, json={
                "id": url.rsplit("/", 1)[1], "status": status,
                "filled_qty": filled})
        if "by_client_order_id" in url:
            return httpx.Response(200, json={
                "id": "brok-1", "status": status, "filled_qty": filled})
        if "/v2/positions" in url:
            return httpx.Response(200, json=[])
        if "/v2/orders" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "unexpected"})

    return brk(handler)


class TestTheCycleDoesNotSubmitACertainRejection:
    def test_no_stop_is_sent_while_the_entry_is_still_working(self, db):
        posts = []
        report = run(db, broker_with_entry("partially_filled", "40", posts),
                     model_transport(), [candidate()])
        stops = [p for p in posts if p.get("type") == "stop"]
        assert stops == [], (
            "a sell stop was submitted into a working buy order - Alpaca "
            "answers 40310000 and the position is unprotected either way")

    def test_the_position_is_still_recorded_and_still_unprotected(self, db):
        """The fallback that saved the first trade must survive: a row
        exists, it carries no stop, and the next cycle arms it."""
        posts = []
        report = run(db, broker_with_entry("partially_filled", "40", posts),
                     model_transport(), [candidate()])
        assert db.execute(
            "SELECT ticker, stop_order_id, status FROM positions"
        ).fetchone() == ("TEST", None, "open")
        assert any("entry_open_but_stop_not_armed" in r
                   for r in report.drop_reasons["orders_placed"])

    def test_the_reason_says_we_declined_not_that_we_were_refused(self, db):
        """'We did not ask' and 'we asked and were refused' need
        different responses from whoever reads the log, and only the
        second one used to be sayable."""
        posts = []
        report = run(db, broker_with_entry("partially_filled", "40", posts),
                     model_transport(), [candidate()])
        assert any("still working" in e and "deferred" in e
                   for e in report.errors), report.errors

    def test_a_finished_entry_is_armed_immediately_as_before(self, db):
        posts = []
        run(db, broker_with_entry("filled", "40", posts),
            model_transport(), [candidate()])
        assert [p for p in posts if p.get("type") == "stop"], (
            "the ordinary path stopped arming its stop")
        assert db.execute(
            "SELECT stop_order_id FROM positions").fetchone()[0] is not None

    def test_further_entries_are_blocked_while_one_is_unprotected(self, db):
        posts = []
        report = run(db, broker_with_entry("partially_filled", "40", posts),
                     model_transport(),
                     [candidate("c1", "AAA"), candidate("c2", "BBB")])
        assert len([p for p in posts if p.get("side") == "buy"]) == 1


class TestTheBracketOrderRouteIsClosed:
    """Why this defect cannot simply be designed away. Recorded as a test
    so the next session does not spend an afternoon rediscovering it."""

    def test_sizing_produces_fractional_quantities(self):
        """Alpaca's bracket/OTO types - the only way to attach a stop to
        an entry atomically - reject fractional qty. On a $2,000 account
        every position is fractional, so that route is closed. If this
        test ever fails because sizing began rounding to whole shares,
        the proper fix becomes available and this defect can be designed
        out rather than shortened."""
        import inspect

        from catalyst.risk.sizing import size

        src = inspect.getsource(size)
        assert 'quantize(Decimal("0.0001")' in src, (
            "sizing no longer quantizes qty to four decimal places - if "
            "it now rounds to whole shares, bracket/OTO orders are "
            "available and the unprotected window can be closed properly")
