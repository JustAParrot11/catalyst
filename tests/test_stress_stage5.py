"""Stage-5 adversarial suite (stress-tester).

Every test here is an attack that was RUN against the pipeline, not a
scenario that was imagined. Tests that assert a refusal document a
surface that survived; tests carrying a defect id in the docstring
were failing before the fix noted beside them.

Findings escalated for human review (risk semantics: sizing, stops,
kill switches) are marked xfail with the desired behaviour asserted, so
the day someone implements the guard the suite reports xpass rather
than silence.

Conventions: broker on httpx.MockTransport, model transport a stub, no
sleeps (backoff_s=0, poll attempts 1).
"""

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from catalyst.data import RawEvent
from catalyst.data.form4_adapter import flatten_form4_events
from catalyst.discovery import Candidate
from catalyst.discovery.candidates import build_candidates
from catalyst.execution.broker import Broker, BrokerError
from catalyst.execution.orders import confirm_stops_resting
from catalyst.execution.reconcile import close_filled_positions, reconcile
from catalyst.orchestrator.cycle import (
    CycleReport, _protective_duties, build_market_snapshot,
    build_portfolio_state, run_cycle,
)
from catalyst.research import prompts
from catalyst.research.boundary import CostContext, investigate
from catalyst.risk.hard_bounds import HARD_BOUNDS
from catalyst.risk.kill_switches import check as kill_check

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
SCHEMA = Path(__file__).resolve().parents[1] / "catalyst" / "storage" / "schema.sql"

ACCOUNT = {"equity": "1000", "cash": "1000", "last_equity": "1000",
           "non_marginable_buying_power": "1000"}
QUOTE = {"quote": {"bp": 49.95, "ap": 50.05, "t": "2026-08-10T13:59:30Z"}}
GOOD_VIEW = {"direction": "long", "conviction": 0.8, "thesis": "t",
             "invalidation": "i", "expected_holding_days": 12,
             "priced_in": False, "priced_in_reasoning": "r"}
USAGE = {"input_tokens": 100, "output_tokens": 50}


@pytest.fixture
def db(tmp_path):
    """Plain connection (foreign keys OFF) - matches tests/test_cycle.py."""
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.executescript(SCHEMA.read_text())
    yield conn
    conn.close()


@pytest.fixture
def prod_db(tmp_path):
    """The connection PRODUCTION uses: storage.init_db, foreign keys ON."""
    from catalyst.storage import init_db

    conn = init_db(str(tmp_path / "prod.db"))
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def frozen_kill_switch_clock(monkeypatch):
    """kill_switches.check() measures snapshot age against the WALL
    clock, not the clock injected into run_cycle. A suite pinned to a
    fixed NOW therefore goes red the moment real time passes NOW + 10
    minutes: with the wall clock at 2026-08-10T15:00Z, 52 tests across
    tests/test_cycle.py and this file fail with portfolio_state_stale
    (measured, see report ESCALATION-10). These tests freeze it so they
    measure what they claim to; test_stale_portfolio_read_is_a_full_
    standdown then exercises the staleness path deliberately."""
    import catalyst.risk.kill_switches as kill_switches

    class _FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(kill_switches, "datetime", _FrozenClock)


@pytest.fixture(autouse=True)
def stub_prompts(monkeypatch):
    # **kwargs, not a fixed signature: this stub exists to make the
    # prompt cheap, not to pin its parameters. Spelling them out meant
    # every stress test broke the moment the real function grew one,
    # which tests nothing about the behaviour under stress.
    monkeypatch.setattr(prompts, "render_research_prompt",
                        lambda c, **kw: "research")
    monkeypatch.setattr(prompts, "exploration_tools", lambda *a, **kw: [])


def brk(handler):
    return Broker("k", "s", transport=httpx.MockTransport(handler), backoff_s=0)


def candidate(cid="cand-1", ticker="TEST"):
    return Candidate(
        id=cid, ticker=ticker, catalyst_type="insider_cluster",
        catalyst_date=date(2026, 8, 20), catalyst_date_confidence="estimated",
        source_event_ids=("e1",), discovered_at=NOW, sector="tech",
        correlation_tags=("tech",))


def model_transport(view=None, usage=None, extraction=None):
    v = view if view is not None else dict(GOOD_VIEW)
    u = USAGE if usage is None else usage

    def transport(payload):
        if (payload.get("tool_choice") or {}).get("type") == "tool":
            if extraction is not None:
                return {**extraction, "usage": u}
            return {"content": [{"type": "tool_use",
                                 "name": "submit_research_view", "input": v}],
                    "stop_reason": "tool_use", "usage": u}
        return {"content": [], "stop_reason": "end_turn", "usage": u}

    return transport


def broker_for(*, fill_qty=None, on_post=None, market_open=True,
               account=None, quote=None, clock=None, clock_status=200,
               open_orders=None, buy_status=None, held=None):
    """Healthy paper broker, with hooks for each hostile response.

    Buys fill immediately at 50; SELL orders (stops) stay resting until
    they are triggered, which is what a real broker does and what makes
    the open-position state machine testable across cycles.
    """
    state = {"posts": [], "qty_by_id": {}, "side_by_client_id": {},
             "resting": []}

    def order_state(client_id, broker_id):
        side = state["side_by_client_id"].get(client_id, "buy")
        if side == "buy" and buy_status is not None:
            # a terminal-but-unfilled entry: the broker accepted the POST
            # and then killed the order without filling any of it
            return {"id": broker_id, "status": buy_status, "filled_qty": "0"}
        if side == "sell":
            return {"id": broker_id, "status": "accepted", "filled_qty": "0"}
        qty = (fill_qty if fill_qty is not None
               else state["qty_by_id"].get(broker_id, "1"))
        return {"id": broker_id, "status": "filled", "filled_qty": qty,
                "filled_avg_price": "50.00",
                "filled_at": "2026-08-10T13:31:00Z"}

    def handler(request):
        url = str(request.url)
        if "/v2/account" in url:
            return httpx.Response(200, json=dict(account or ACCOUNT))
        if "/v2/clock" in url:
            if clock_status != 200:
                return httpx.Response(clock_status, json={})
            return httpx.Response(200, json=(dict(clock) if clock is not None
                                             else {"is_open": market_open}))
        if "/quotes/latest" in url:
            return httpx.Response(200, json=dict(quote or QUOTE))
        if request.method == "POST" and url.endswith("/v2/orders"):
            body = json.loads(request.content)
            state["posts"].append(body)
            if on_post is not None:
                resp = on_post(body, len(state["posts"]))
                if resp is not None:
                    return resp
            broker_id = f"brok-{len(state['posts'])}"
            state["qty_by_id"][broker_id] = body["qty"]
            state["side_by_client_id"][body["client_order_id"]] = body["side"]
            state["side_by_client_id"][broker_id] = body["side"]
            if body["side"] == "buy" and buy_status is not None:
                return httpx.Response(200, json={"id": broker_id,
                                                 "status": buy_status})
            if body["side"] == "sell" and body["type"] == "stop":
                state["resting"].append(
                    {"id": broker_id, "symbol": body["symbol"],
                     "side": "sell", "type": "stop"})
            return httpx.Response(200, json={"id": broker_id,
                                             "status": "accepted"})
        if "by_client_order_id" in url:
            client_id = dict(request.url.params).get("client_order_id", "")
            if client_id not in state["side_by_client_id"]:
                # an order the broker never accepted does not exist
                return httpx.Response(404, json={"message": "order not found"})
            return httpx.Response(200, json=order_state(client_id, "brok-x"))
        if request.method == "GET" and "/v2/orders/" in url:
            broker_id = url.rsplit("/", 1)[1]
            return httpx.Response(200, json=order_state(broker_id, broker_id))
        if "/v2/positions" in url:
            # WHAT THE BROKER SAYS IT HOLDS. Defaults to empty, which is
            # the honest answer for most tests here (their positions are
            # locally recorded and the broker mock never really bought
            # anything). Tests that need the broker to CONFIRM a holding
            # pass `held` - since ESCALATION-7, a stop is only armed for
            # shares the broker agrees exist, so "the fill is unreadable
            # but the stock is really held" has to be sayable.
            resolved = held(state) if callable(held) else held
            return httpx.Response(200, json=list(resolved or []))
        if "/v2/orders" in url:
            return httpx.Response(200, json=(state["resting"]
                                             if open_orders is None
                                             else open_orders))
        return httpx.Response(404, json={"message": "unexpected"})

    return brk(handler), state


def run(conn, broker, transport, cands, events=None, **kw):
    kw.setdefault("entry_poll_attempts", 1)
    kw.setdefault("entry_poll_interval_s", 0)
    kw.setdefault("now", NOW)
    return run_cycle(
        conn, broker, transport,
        feed_fetch=lambda s, u: (events if events is not None
                                 else [RawEvent("edgar_form4", "e1", NOW, {})]),
        build_candidates_fn=lambda evs, as_of: cands,
        cluster_fn=lambda cs, ops: {c.id: "tech-w34" for c in cs}, **kw)


def seed_decision(conn, candidate_id="c1", ticker="T"):
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (candidate_id, ticker, "insider_cluster", "2026-08-20",
                  "estimated", "[]", "2026-08-10T14:00:00+00:00", "tech", "[]"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("dec-1", candidate_id, "trade", "long", "100", "2", "45.00",
                  "2026-08-20", "[]", "{}", "2026-08-01T13:00:00+00:00"))
    conn.commit()


# =====================================================================
# 1. The broker seam: malformed and adversarial responses
# =====================================================================

class TestBrokerMalformedResponses:
    """DEFECT 2 (fixed in execution/broker.py): a 200 whose body is not
    parseable JSON, or is JSON of the wrong shape, escaped as
    JSONDecodeError/AttributeError instead of BrokerError - so every
    caller's `except BrokerError` fail-closed path was bypassed and the
    cycle died with a traceback instead of tripping the kill switch."""

    @pytest.mark.parametrize("body", [b"", b"{oops", b"<html>502</html>"])
    def test_unparseable_body_is_a_broker_error(self, db, body):
        b = brk(lambda r: httpx.Response(200, content=body))
        with pytest.raises(BrokerError):
            b.get_account()
        assert build_portfolio_state(b, db, NOW) is None      # fails closed
        assert kill_check(build_portfolio_state(b, db, NOW),
                          HARD_BOUNDS).tripped

    @pytest.mark.parametrize("body", [b"null", b"[]", b'"a string"', b"42"])
    def test_wrong_shape_object_endpoints_are_broker_errors(self, body):
        b = brk(lambda r: httpx.Response(200, content=body))
        for call in (b.get_account, b.get_clock,
                     lambda: b.get_order("x"),
                     lambda: b.get_order_by_client_id("x"),
                     lambda: b.get_latest_quote("T")):
            with pytest.raises(BrokerError):
                call()

    @pytest.mark.parametrize("body", [b"null", b"{}", b'"x"'])
    def test_wrong_shape_list_endpoints_are_broker_errors(self, body):
        b = brk(lambda r: httpx.Response(200, content=body))
        with pytest.raises(BrokerError):
            b.get_open_orders()
        with pytest.raises(BrokerError):
            b.get_positions()

    def test_list_of_non_objects_is_refused_not_silently_dropped(self):
        """A stop we cannot parse must NOT read as 'no stop resting' -
        that is how a second stop gets placed on one position."""
        b = brk(lambda r: httpx.Response(200, json=["not-an-order"]))
        with pytest.raises(BrokerError):
            b.get_open_orders()

    def test_clock_missing_is_open_blocks_entries(self, db):
        """SURVIVED: a clock with no is_open reads as closed."""
        broker, state = broker_for(clock={"timestamp": "2026-08-10T14:00:00Z"})
        report = run(db, broker, model_transport(), [candidate()])
        assert any("market_closed" in x
                   for x in report.drop_reasons["researched"])
        assert state["posts"] == []

    def test_clock_endpoint_failure_blocks_entries(self, db):
        """SURVIVED: no clock, no entries."""
        broker, state = broker_for(clock_status=500)
        report = run(db, broker, model_transport(), [candidate()])
        assert any("market_clock_unavailable" in x
                   for x in report.drop_reasons["researched"])
        assert state["posts"] == []


class TestBrokerNumbers:
    def test_quote_with_json_nan_is_refused(self):
        """DEFECT 3 (fixed in orchestrator/cycle.py): Python's json parses
        the non-standard NaN/Infinity literals, Decimal('NaN') survives
        construction, and the FIRST comparison raised InvalidOperation
        out of build_market_snapshot - crashing the cycle on a quote."""
        for body in (b'{"quote":{"bp":NaN,"ap":50.05}}',
                     b'{"quote":{"bp":49.95,"ap":Infinity}}',
                     b'{"quote":{"bp":"nan","ap":"nan"}}',
                     b'{"quote":{"bp":-Infinity,"ap":1}}'):
            b = brk(lambda r, body=body: httpx.Response(200, content=body))
            assert build_market_snapshot(b, "T", NOW) is None

    def test_equity_nan_fails_closed(self, db):
        """DEFECT 4 (fixed in orchestrator/cycle.py): equity 'NaN' built a
        PortfolioState with reliable=True; the kill switch's first
        comparison then raised InvalidOperation - the one code path that
        exists to fail closed crashed instead."""
        for value in ("NaN", "Infinity", "-Infinity", "nan"):
            b = brk(lambda r, v=value: httpx.Response(
                200, json={**ACCOUNT, "equity": v}))
            state = build_portfolio_state(b, db, NOW)
            assert state is None, f"equity={value} produced {state}"
            assert kill_check(state, HARD_BOUNDS).tripped

    def test_cash_nan_fails_closed(self, db):
        b = brk(lambda r: httpx.Response(200, json={**ACCOUNT, "cash": "NaN"}))
        assert build_portfolio_state(b, db, NOW) is None

    @pytest.mark.parametrize("acct,expect", [
        ({"equity": None}, None),
        ({"equity": "abc"}, None),
        ({"equity": []}, None),
        ({"cash": None}, None),
    ])
    def test_absent_or_absurd_account_numbers_fail_closed(self, db, acct,
                                                          expect):
        """SURVIVED: null/garbage account numbers already returned None."""
        b = brk(lambda r: httpx.Response(200, json={**ACCOUNT, **acct}))
        assert build_portfolio_state(b, db, NOW) is expect

    def test_negative_equity_trips_kill_switch(self, db):
        """SURVIVED: negative equity is non-positive -> kill."""
        b = brk(lambda r: httpx.Response(
            200, json={**ACCOUNT, "equity": "-50", "cash": "0"}))
        p = build_portfolio_state(b, db, NOW)
        assert kill_check(p, HARD_BOUNDS).reason == "equity_nonpositive"

    def test_crossed_and_degenerate_quotes(self):
        """SURVIVED: bid==ask is a zero spread (legal); bid>ask, zero and
        negative sides are refused; exponent strings parse."""
        cases = {
            (50, 50): Decimal("0.0"),          # locked market
            ("4.995e1", "5.005e1"): Decimal("10.0"),
            (1, 100): Decimal("9802.0"),       # absurd spread -> gate's job
        }
        for (bp, ap), half in cases.items():
            b = brk(lambda r, bp=bp, ap=ap: httpx.Response(
                200, json={"quote": {"bp": bp, "ap": ap,
                                     "t": "2026-08-10T13:59:30Z"}}))
            snap = build_market_snapshot(b, "T", NOW)
            assert snap is not None and snap.half_spread_bp == half
        for bp, ap in [(50.05, 49.95), (0, 1), (-1, 1), (1, 0), (None, 1),
                       ("", ""), ({}, 1)]:
            b = brk(lambda r, bp=bp, ap=ap: httpx.Response(
                200, json={"quote": {"bp": bp, "ap": ap,
                                     "t": "2026-08-10T13:59:30Z"}}))
            assert build_market_snapshot(b, "T", NOW) is None

    def test_absurd_spread_is_skipped_not_traded(self, db):
        """SURVIVED: the hard spread gate refuses a 9802bp half-spread."""
        broker, state = broker_for(quote={"quote": {"bp": 1, "ap": 100, "t": "2026-08-10T13:59:30Z"}})
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel["proposed"] == 0
        assert any("spread_gate" in r for r in report.drop_reasons["proposed"])
        assert state["posts"] == []


class TestReconcileHostileFills:
    def _seeded(self, conn):
        seed_decision(conn)
        conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("o1", "c1", "b1", "buy", "2", "market", "day",
                      "2026-08-10T14:00:00+00:00", "accepted", "{}"))
        conn.commit()

    def _reconcile_with(self, conn, remote):
        self._seeded(conn)
        b = brk(lambda r: httpx.Response(200, json=remote))
        return reconcile(b, conn)

    @pytest.mark.parametrize("bad_qty", ["1,000", "abc", "NaN", "1 000", "-",
                                         "Infinity"])
    def test_unparseable_filled_qty_does_not_crash(self, db, bad_qty):
        """DEFECT 5 (fixed in execution/reconcile.py): a filled_qty the
        broker returns in any unexpected form raised InvalidOperation out
        of reconcile - which runs BEFORE stop re-arming and hard exits, so
        one malformed field left the whole book unmanaged for the day."""
        fills = self._reconcile_with(db, {
            "id": "b1", "status": "filled", "filled_qty": bad_qty,
            "filled_avg_price": "50.00"})
        assert fills == []
        # the raw upstream answer is kept beside the zero (house rule 3)
        raw = db.execute("SELECT raw_response FROM orders WHERE id='o1'"
                         ).fetchone()[0]
        assert bad_qty in raw

    @pytest.mark.parametrize("bad_price", ["-50", "0", "NaN", "abc"])
    def test_nonpositive_or_unparseable_fill_price_is_not_a_fill(self, db,
                                                                 bad_price):
        """DEFECT 6 (fixed in execution/reconcile.py): a negative or zero
        avg fill price was written into fills verbatim. realized P&L is
        computed from it, and realized P&L both feeds the drawdown kill
        switch's high-water mark and RAISES the cost governor's cap."""
        fills = self._reconcile_with(db, {
            "id": "b1", "status": "filled", "filled_qty": "2",
            "filled_avg_price": bad_price})
        assert fills == []
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
        raw = db.execute("SELECT raw_response FROM orders WHERE id='o1'"
                         ).fetchone()[0]
        assert bad_price in raw

    def test_remote_order_of_wrong_shape_is_a_broker_error(self, db):
        """DEFECT 2: a null/list order body crashed reconcile with
        AttributeError; it must be a BrokerError the cycle already
        catches and reports."""
        self._seeded(db)
        b = brk(lambda r: httpx.Response(200, content=b"null"))
        with pytest.raises(BrokerError):
            reconcile(b, db)

    def test_negative_filled_qty_is_not_a_fill(self, db):
        """SURVIVED: filled_qty < 0 never becomes a fill row."""
        assert self._reconcile_with(db, {
            "id": "b1", "status": "filled", "filled_qty": "-5",
            "filled_avg_price": "50"}) == []
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0

    def test_404_on_reconcile_marks_rejected_with_raw_body(self, db):
        """An order the broker never heard of is terminal only after TWO
        consecutive 404 passes (risk round 3 finding 1: one transient
        404 must not terminalize - it rippled into voiding a live
        position). The raw body is recorded on both passes."""
        self._seeded(db)
        b = brk(lambda r: httpx.Response(404, json={"message": "not found"}))
        assert reconcile(b, db) == []
        status, raw = db.execute(
            "SELECT status, raw_response FROM orders WHERE id='o1'").fetchone()
        assert status == "reconcile_404_once" and "not found" in raw
        assert reconcile(b, db) == []
        status, raw = db.execute(
            "SELECT status, raw_response FROM orders WHERE id='o1'").fetchone()
        assert status == "rejected" and "not found" in raw

    def test_partial_fill_that_grows_replaces_the_earlier_observation(self, db):
        """SURVIVED: a fill that grows updates qty and avg price."""
        self._seeded(db)
        seq = iter([
            {"id": "b1", "status": "partially_filled", "filled_qty": "1",
             "filled_avg_price": "50.00"},
            {"id": "b1", "status": "filled", "filled_qty": "2",
             "filled_avg_price": "50.50"},
        ])
        b = brk(lambda r: httpx.Response(200, json=next(seq)))
        reconcile(b, db)
        reconcile(b, db)
        assert db.execute("SELECT qty, price FROM fills").fetchone() == \
            ("2", "50.50")


class TestStopConfirmation:
    def test_open_order_without_id_still_counts_as_a_resting_stop(self, db):
        """DEFECT 7 (fixed in execution/orders.py): an open order missing
        'id' raised KeyError out of confirm_stops_resting. Dropping it
        instead would be worse - the position would read 'unprotected'
        and a SECOND stop would be placed on it."""
        b = brk(lambda r: httpx.Response(200, json=[
            {"symbol": "T", "side": "sell", "type": "stop"}]))
        confs = confirm_stops_resting([{"id": "p1", "ticker": "T"}], b, db)
        assert confs[0].status == "ok"

    def test_two_live_stops_are_reported(self, db):
        """SURVIVED as detection. See ESCALATION-3: nothing cancels the
        duplicate; the position can be sold twice."""
        b = brk(lambda r: httpx.Response(200, json=[
            {"id": "s1", "symbol": "T", "side": "sell", "type": "stop"},
            {"id": "s2", "symbol": "T", "side": "sell", "type": "stop"}]))
        confs = confirm_stops_resting([{"id": "p1", "ticker": "T"}], b, db)
        assert confs[0].status == "duplicate_stops"

    def test_duplicate_stops_are_reduced_to_one_BY_THE_CYCLE(self, db):
        """ESCALATION-3, resolved - but NOT here, and the distinction is
        the point.

        `confirm_stops_resting` only ever reports. It is called from
        several places and it is the function every other check trusts
        to tell it the truth; a reporting function that quietly cancels
        live orders at the broker as a side effect is the kind of thing
        nobody expects to have happened when they read the call site.

        The cancellation lives in `cycle._protective_duties`, which is
        the one place that owns the book and already decides what to
        arm, replace and neutralise. Its `duplicate_stops` branch keeps
        exactly one live stop - preferring the id already recorded
        locally - and cancels the rest.

        The detailed cases live in test_stage5_gaps.py
        (TestDuplicateStopReduction), including the subtle one: when the
        recorded id is the SECOND the broker lists, it must still be the
        one kept, or the position ends up recorded as protected by an
        order that was just cancelled. This test pins the headline
        property so the escalation cannot silently come back.
        """
        seed_decision(db, "c1", "T")
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ord-buy", "c1", "b1", "buy", "2", "market", "day",
                    "2026-08-01T14:00:00+00:00", "filled", "{}"))
        db.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                   ("ord-buy", "50.00", "2", "2026-08-01T14:00:00+00:00",
                    "50.00"))
        db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                   ("p1", "T", json.dumps(["ord-buy"]), "s1",
                    "2026-08-01T14:00:00+00:00", "2026-08-20", "open"))
        db.commit()

        two_stops = [
            {"id": "s1", "symbol": "T", "side": "sell", "type": "stop",
             "qty": "2"},
            {"id": "s2", "symbol": "T", "side": "sell", "type": "stop",
             "qty": "2"}]
        cancelled = []

        def handler(request):
            url = str(request.url)
            if request.method == "DELETE":
                cancelled.append(url.rsplit("/", 1)[1])
                return httpx.Response(204)
            if "/v2/account" in url:
                return httpx.Response(200, json=dict(ACCOUNT))
            if "/v2/clock" in url:
                return httpx.Response(200, json={"is_open": True})
            if "/v2/positions" in url:
                return httpx.Response(200, json=[{"symbol": "T", "qty": "2"}])
            if "by_client_order_id" in url or "/v2/orders/" in url:
                # every stop still live until it is cancelled
                oid = url.rsplit("/", 1)[1].split("?")[0]
                if oid in cancelled:
                    return httpx.Response(200, json={"id": oid,
                                                     "status": "canceled"})
                return httpx.Response(200, json={"id": oid,
                                                 "status": "accepted",
                                                 "filled_qty": "0"})
            if "/v2/orders" in url:
                return httpx.Response(
                    200, json=[o for o in two_stops
                               if o["id"] not in cancelled])
            return httpx.Response(404, json={})

        report = CycleReport(cycle_id="c", started_at=NOW, kill_switch=None)
        protected, _ = _protective_duties(db, brk(handler), report, NOW)

        assert cancelled == ["s2"], (
            f"expected exactly the extra stop to be cancelled, got "
            f"{cancelled!r} - two live stops can sell one position twice")
        assert db.execute("SELECT stop_order_id FROM positions"
                          ).fetchone()[0] == "s1"
        assert protected is True

    def test_unprotected_position_blocks_new_entries(self, db):
        """SURVIVED: no resting stop -> no new entries this cycle."""
        seed_decision(db, "cand-old", "OLDPOS")
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ord-buy", "cand-old", "b1", "buy", "2", "market", "day",
                    "2026-08-01T14:00:00+00:00", "filled", "{}"))
        db.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                   ("ord-buy", "50.00", "2", "2026-08-01T14:00:00+00:00",
                    "50.00"))
        db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                   ("pos-old", "OLDPOS", json.dumps(["ord-buy"]), None,
                    "2026-08-01T14:00:00+00:00", "2026-08-20", "open"))
        db.commit()
        # broker rejects the re-armed stop, so the book stays unprotected
        broker, state = broker_for(
            on_post=lambda body, n: (httpx.Response(403, json={"m": "no"})
                                     if body["type"] == "stop" else None))
        report = run(db, broker, model_transport(), [candidate()])
        assert any("unprotected_position_blocks_entries" in r
                   for r in report.drop_reasons["researched"])
        assert [p for p in state["posts"] if p["side"] == "buy"] == []


# =====================================================================
# 2. Order submission: the gap between "sent" and "recorded"
# =====================================================================

class TestSubmitAmbiguity:
    def test_transport_failure_records_the_order_before_giving_up(self, db):
        """DEFECT 8 (fixed in execution/orders.py + cycle.py): a network
        failure or 5xx on POST /v2/orders raised BrokerError out of
        place(), so the local order id - which IS the client_order_id and
        the only handle on a possibly-live order - was never written
        anywhere. The order may exist at the broker; nothing local could
        ever find it."""
        broker, state = broker_for(
            on_post=lambda body, n: httpx.Response(500, json={"m": "boom"}))
        report = run(db, broker, model_transport(), [candidate()])
        rows = db.execute("SELECT id, side, status FROM orders").fetchall()
        assert len(rows) == 1 and rows[0][1] == "buy"
        assert rows[0][2] == "submit_unconfirmed"
        # the recorded id is the client_order_id we actually sent
        assert rows[0][0] == state["posts"][0]["client_order_id"]
        # and no position is invented for an order we cannot confirm
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        assert report.funnel["orders_placed"] == 0

    def test_unconfirmed_submit_is_resolved_by_the_next_reconcile(self, db):
        """The recorded row is the recovery handle: reconcile asks the
        broker by client_order_id and learns the truth."""
        broker, state = broker_for(
            on_post=lambda body, n: httpx.Response(500, json={"m": "boom"}))
        run(db, broker, model_transport(), [candidate()])
        order_id = db.execute("SELECT id FROM orders").fetchone()[0]

        def handler(request):
            if "by_client_order_id" in str(request.url):
                return httpx.Response(200, json={
                    "id": "brok-real", "status": "filled", "filled_qty": "4",
                    "filled_avg_price": "50.00",
                    "filled_at": "2026-08-10T14:00:01Z"})
            return httpx.Response(404, json={})

        fills = reconcile(brk(handler), db)
        assert [f.order_id for f in fills] == [order_id]
        assert db.execute("SELECT status FROM orders").fetchone()[0] == "filled"

    def test_duplicate_client_order_id_rejection_is_not_a_rejection(self, db):
        """DEFECT 9 (fixed in execution/orders.py): _request retries a
        POST after a network error. Alpaca then rejects the retry BECAUSE
        THE FIRST ONE LANDED (duplicate client_order_id). The local record
        said 'rejected' while a live position existed at the broker - the
        most expensive lie the database can tell."""
        attempts = {"n": 0}

        def handler(request):
            url = str(request.url)
            if request.method == "POST" and url.endswith("/v2/orders"):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise httpx.ReadTimeout("timeout after the order landed")
                return httpx.Response(422, json={
                    "message": "client_order_id must be unique"})
            if "by_client_order_id" in url:
                return httpx.Response(200, json={
                    "id": "brok-live", "status": "accepted", "filled_qty": "0"})
            if "/v2/account" in url:
                return httpx.Response(200, json=dict(ACCOUNT))
            if "/v2/clock" in url:
                return httpx.Response(200, json={"is_open": True})
            if "/quotes/latest" in url:
                return httpx.Response(200, json=dict(QUOTE))
            if request.method == "GET" and "/v2/orders/" in url:
                return httpx.Response(200, json={
                    "id": "brok-live", "status": "accepted", "filled_qty": "0"})
            if "/v2/positions" in url:
                return httpx.Response(200, json=[])
            if "/v2/orders" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={})

        run(db, brk(handler), model_transport(), [candidate()])
        status, broker_id = db.execute(
            "SELECT status, broker_order_id FROM orders").fetchone()
        assert status != "rejected"
        assert broker_id == "brok-live"

    def test_genuine_rejection_is_still_a_rejection(self, db):
        """SURVIVED: a real 4xx (restricted account) records rejected,
        keeps the broker's verbatim body, and opens no position."""
        broker, state = broker_for(
            on_post=lambda body, n: httpx.Response(403, json={
                "message": "account is restricted from trading"}))
        report = run(db, broker, model_transport(), [candidate()])
        status, raw = db.execute(
            "SELECT status, raw_response FROM orders").fetchone()
        assert status == "rejected"
        assert "restricted" in raw and "403" in raw
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        assert any("entry_rejected" in r
                   for r in report.drop_reasons["orders_placed"])

    def test_account_restricted_mid_cycle_stops_at_the_stop(self, db):
        """The entry fills, then the account is restricted so the stop is
        refused: the position must be recorded unprotected and further
        entries blocked, never recorded as protected."""
        broker, state = broker_for(
            on_post=lambda body, n: (httpx.Response(403, json={"m": "no"})
                                     if body["type"] == "stop" else None))
        report = run(db, broker, model_transport(),
                     [candidate("c1", "AAA"), candidate("c2", "BBB")])
        assert db.execute("SELECT stop_order_id FROM positions"
                          ).fetchone()[0] is None
        assert len([p for p in state["posts"] if p["side"] == "buy"]) == 1


class TestEntryPollAndOverfill:
    def test_garbage_filled_qty_does_not_orphan_the_position(self, db):
        """DEFECT 10 (fixed in orchestrator/cycle.py): an unparseable
        filled_qty raised InvalidOperation from _poll_entry_fill - AFTER
        the entry order was live at the broker and BEFORE the positions
        row was written. The result was a live position with no local
        row: no stop, no hard exit date, and (proved below) no cycle ever
        recovers it."""
        broker, state = broker_for(fill_qty="1,000")
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel["orders_placed"] == 1
        pos = db.execute("SELECT ticker, stop_order_id, status "
                         "FROM positions").fetchone()
        assert pos == ("TEST", None, "open")      # recorded, unprotected
        assert any("entry_open_but_stop_not_armed" in r
                   for r in report.drop_reasons["orders_placed"])

    def test_recorded_position_is_armed_on_the_next_cycle(self, db):
        # Same broker across both cycles: a FRESH mock would answer 404
        # for the entry's client_order_id, and a position whose entry the
        # broker disowns (no order, no holding) is now correctly VOIDED
        # rather than armed - which is the void transition's test, not
        # this one. Here the broker still knows the order but keeps
        # reporting garbage filled_qty; the position must be re-armed
        # from the ordered qty on the next cycle.
        # The broker CONFIRMS it holds the stock - which is what a real
        # broker does when it has reported the order filled. That is the
        # whole difference between this and a phantom: the shares exist,
        # so they must be protected even though filled_qty is garbage.
        broker, state = broker_for(
            fill_qty="1,000",
            # Held only AFTER the buy is sent, as a real broker reports
            # it. A static holding would make the broker claim the stock
            # before it was bought, which correctly blocks entries as
            # unaccounted exposure and would test nothing.
            held=lambda st: ([{"symbol": "TEST", "qty": "1"}]
                             if any(o["side"] == "buy" for o in st["posts"])
                             else []))
        run(db, broker, model_transport(), [candidate()])
        run(db, broker, model_transport(), [], events=[])
        assert db.execute("SELECT stop_order_id FROM positions"
                          ).fetchone()[0] is not None

    def test_overfill_does_not_size_the_stop_beyond_the_order(self, db):
        broker, state = broker_for(fill_qty="9999")
        run(db, broker, model_transport(), [candidate()])
        buy = [p for p in state["posts"] if p["side"] == "buy"][0]
        stop = [p for p in state["posts"] if p["type"] == "stop"][0]
        assert Decimal(stop["qty"]) <= Decimal(buy["qty"])


class TestPhantomPositions:
    @pytest.mark.parametrize("status", ["canceled", "expired", "done_for_day"])
    def test_terminal_unfilled_entry_opens_no_position(self, db, status):
        """DEFECT 26 (fixed in orchestrator/cycle.py): a POST that returns
        HTTP 200 with a TERMINAL status ('canceled' - Alpaca cancels
        unfilled orders at the close) was treated as a live entry. A
        positions row was written for stock that was never bought, and
        every later cycle tried to arm a protective sell stop against
        shares the account does not hold."""
        broker, state = broker_for(buy_status=status)
        report = run(db, broker, model_transport(), [candidate()])
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        assert report.funnel["orders_placed"] == 0
        assert [p["side"] for p in state["posts"]] == ["buy"]   # no stop

    def test_accepted_entry_with_no_fill_yet_still_opens_a_position(self, db):
        """The other side of the same fix: an ACCEPTED order that has not
        filled yet must still be recorded (the fill can land later), just
        unprotected."""
        def handler(request):
            url = str(request.url)
            if "/v2/account" in url:
                return httpx.Response(200, json=dict(ACCOUNT))
            if "/v2/clock" in url:
                return httpx.Response(200, json={"is_open": True})
            if "/quotes/latest" in url:
                return httpx.Response(200, json=dict(QUOTE))
            if request.method == "POST" and url.endswith("/v2/orders"):
                return httpx.Response(200, json={"id": "b1",
                                                 "status": "accepted"})
            if "by_client_order_id" in url or "/v2/orders/" in url:
                return httpx.Response(200, json={"id": "b1",
                                                 "status": "accepted",
                                                 "filled_qty": "0"})
            if "/v2/positions" in url:
                return httpx.Response(200, json=[])
            if "/v2/orders" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={})

        report = run(db, brk(handler), model_transport(), [candidate()])
        assert report.funnel["orders_placed"] == 1
        assert db.execute("SELECT stop_order_id FROM positions"
                          ).fetchone()[0] is None

    def test_never_filled_position_does_not_arm_a_stop(self, db):
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
                return httpx.Response(200, json={"id": f"b{len(posts)}",
                                                 "status": "accepted"})
            if "by_client_order_id" in url or "/v2/orders/" in url:
                return httpx.Response(200, json={"id": "b1",
                                                 "status": "accepted",
                                                 "filled_qty": "0"})
            if "/v2/positions" in url:
                return httpx.Response(200, json=[])
            if "/v2/orders" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={})

        posts = []
        broker = brk(handler)
        run(db, broker, model_transport(), [candidate()])
        run(db, broker, model_transport(), [], events=[])
        assert [p for p in posts if p["type"] == "stop"] == []

    def test_broker_position_unknown_to_the_database_is_reported(self, db):
        def handler(request):
            url = str(request.url)
            if "/v2/account" in url:
                return httpx.Response(200, json=dict(ACCOUNT))
            if "/v2/positions" in url:
                return httpx.Response(200, json=[{"symbol": "GHOST",
                                                  "qty": "10"}])
            if "/v2/clock" in url:
                return httpx.Response(200, json={"is_open": True})
            if "/quotes/latest" in url:
                return httpx.Response(200, json=dict(QUOTE))
            if "/v2/positions" in url:
                return httpx.Response(200, json=[])
            if "/v2/orders" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={})

        report = run(db, brk(handler), None, [], events=[])
        assert any("GHOST" in e for e in report.errors)

    def test_two_candidates_for_one_ticker_enter_once(self, db):
        broker, state = broker_for()
        run(db, broker, model_transport(),
            [candidate("c1", "SAME"), candidate("c2", "SAME")])
        buys = [p for p in state["posts"] if p["side"] == "buy"]
        assert len(buys) == 1


class TestCrashRecovery:
    def test_filled_entry_without_a_position_row_is_adopted(self, db):
        seed_decision(db, "cand-1", "TEST")
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("orphan", "cand-1", "b9", "buy", "4", "market", "day",
                    "2026-08-10T13:00:00+00:00", "filled", "{}"))
        db.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                   ("orphan", "50.00", "4", "2026-08-10T13:00:00+00:00",
                    "50.00"))
        db.commit()
        broker, state = broker_for()
        run(db, broker, model_transport(), [], events=[])
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1


# =====================================================================
# 3. The model boundary
# =====================================================================

class TestResearchBoundary:
    def _investigate(self, conn, response, usage=None):
        seed_decision(conn, "cand-1", "TEST")
        transport = model_transport(usage=usage, extraction=response)
        return investigate(candidate(), CostContext(
            conn=conn, governor_profit_share=Decimal("0.10"),
            cycle_id="cy", kind="scheduled"), transport)

    @pytest.mark.parametrize("response", [
        {"content": "here is my view", "stop_reason": "end_turn"},
        {"content": ["a string block"], "stop_reason": "end_turn"},
        {"content": [None], "stop_reason": "end_turn"},
        {"content": 7, "stop_reason": "end_turn"},
    ])
    def test_content_that_is_not_a_block_list_is_refused(self, db, response):
        """DEFECT 11 (fixed in research/boundary.py): a response whose
        content is not a list of block objects raised AttributeError
        inside investigate() - after the call was billed and before the
        research_calls row was written, so a paid call vanished from the
        audit trail entirely."""
        log = self._investigate(db, response)
        assert log.parsed_view is None
        assert log.skipped_reason is not None
        assert db.execute("SELECT COUNT(*) FROM research_calls"
                          ).fetchone()[0] == 1
        # exploration + extraction + one bounded repair turn, each
        # recorded (the live API can ignore `required`; boundary now
        # repairs once before skipping)
        assert db.execute("SELECT COUNT(*) FROM cost_events"
                          ).fetchone()[0] in (2, 3)

    def test_two_tool_use_blocks_are_ambiguous_not_first_wins(self, db):
        """DEFECT 12 (fixed in research/boundary.py): two
        submit_research_view blocks in one response silently took the
        first. A model that retracts its view in the second block would
        have been traded on the first."""
        log = self._investigate(db, {"content": [
            {"type": "tool_use", "name": "submit_research_view",
             "input": dict(GOOD_VIEW, conviction=0.9)},
            {"type": "tool_use", "name": "submit_research_view",
             "input": dict(GOOD_VIEW, direction="no_trade")}],
            "stop_reason": "tool_use"})
        assert log.parsed_view is None
        assert "multiple" in log.skipped_reason

    @pytest.mark.parametrize("bad_input", [
        {**GOOD_VIEW, "conviction": float("nan")},
        {**GOOD_VIEW, "conviction": float("inf")},
        {**GOOD_VIEW, "conviction": "0.9"},
        {**GOOD_VIEW, "conviction": True},
        {**GOOD_VIEW, "conviction": 1.0000001},
        {**GOOD_VIEW, "direction": "Long"},
        {**GOOD_VIEW, "direction": "LONG"},
        {**GOOD_VIEW, "direction": " long"},
        {**GOOD_VIEW, "qty": 100},
        {**GOOD_VIEW, "notional_usd": 500},
        {**GOOD_VIEW, "position": {"shares": 12}},
        {**GOOD_VIEW, "thesis": {"qty": 100}},
        {**GOOD_VIEW, "thesis": ""},
        {**GOOD_VIEW, "expected_holding_days": True},
        {**GOOD_VIEW, "expected_holding_days": 0},
        {**GOOD_VIEW, "expected_holding_days": "12"},
        {**GOOD_VIEW, "priced_in": "false"},
        [1, 2, 3],
        "just a string",
        None,
    ])
    def test_adversarial_tool_input_never_becomes_a_view(self, db, bad_input):
        """SURVIVED: every size-shaped, mistyped or out-of-range field is
        refused; nothing defaults."""
        log = self._investigate(db, {"content": [
            {"type": "tool_use", "name": "submit_research_view",
             "input": bad_input}], "stop_reason": "tool_use"})
        assert log.parsed_view is None
        assert db.execute("SELECT COUNT(*) FROM research_views"
                          ).fetchone()[0] == 0

    def test_wrong_tool_name_is_not_accepted(self, db):
        """SURVIVED: only submit_research_view counts."""
        log = self._investigate(db, {"content": [
            {"type": "tool_use", "name": "submit_position_size",
             "input": GOOD_VIEW}], "stop_reason": "tool_use"})
        assert log.parsed_view is None

    def test_enormous_thesis_is_accepted_but_bounded_by_nothing(self, db):
        """SURVIVED (noted): a 200k-character thesis is stored verbatim.
        No injection into sizing is possible; the cost is DB bloat."""
        log = self._investigate(db, {"content": [
            {"type": "tool_use", "name": "submit_research_view",
             "input": {**GOOD_VIEW, "thesis": "x" * 200_000}}],
            "stop_reason": "tool_use"})
        assert log.parsed_view is not None
        assert len(db.execute("SELECT thesis FROM research_views"
                              ).fetchone()[0]) == 200_000


class TestCostDisciplineAtTheBoundary:
    def test_unknown_usage_field_records_then_blocks(self, db):
        """DEFECT 13 (fixed in research/boundary.py): the row WAS recorded
        (cost discipline held), but UnrecognizedUsageFieldError then
        escaped investigate() and killed the whole cycle mid-loop - after
        an earlier candidate may already have been traded. It must end
        the investigation, not the process."""
        seed_decision(db, "cand-1", "TEST")
        transport = model_transport(usage={"input_tokens": 10,
                                           "output_tokens": 5,
                                           "reasoning_tokens": 999})
        log = investigate(candidate(), CostContext(
            conn=db, governor_profit_share=Decimal("0.10"),
            cycle_id="cy", kind="scheduled"), transport)
        assert log.parsed_view is None
        assert "unpriced" in log.skipped_reason
        row = db.execute("SELECT raw_usage_json, priced_cents "
                         "FROM cost_events").fetchone()
        assert "reasoning_tokens" in row[0] and row[1] is None
        # ...and the governor is now blocked until a human reprices
        from catalyst.cost.tracker import has_unpriced_rows
        assert has_unpriced_rows(db)

    def test_usage_that_is_not_an_object_is_still_recorded(self, db):
        """DEFECT 14 (fixed in cost/tracker.py): a usage field that is not
        a dict raised AttributeError inside make_usage_components, so the
        row was NEVER recorded - money spent with nothing in the ledger,
        the exact failure record-first exists to prevent."""
        seed_decision(db, "cand-1", "TEST")
        for usage in ([], "none", 7, None):
            conn = db
            log = investigate(candidate(), CostContext(
                conn=conn, governor_profit_share=Decimal("0.10"),
                cycle_id="cy", kind="scheduled"),
                model_transport(usage=usage))
            assert log.parsed_view is None
        rows = db.execute("SELECT raw_usage_json, priced_cents "
                          "FROM cost_events").fetchall()
        assert rows, "no cost row recorded for a billed call"
        assert all(r[1] is None for r in rows)

    def test_null_priced_row_blocks_every_model_call(self, db):
        """SURVIVED: a manufactured unpriced row denies authorization
        before the transport is ever called."""
        seed_decision(db, "cand-1", "TEST")
        db.execute("INSERT INTO cost_events VALUES "
                   "('x','{}','claude-sonnet-5','scheduled','research',"
                   "NULL,'2026-08-10T00:00:00+00:00','a')")
        db.commit()
        calls = {"n": 0}

        def transport(payload):
            calls["n"] += 1
            return {"content": [], "stop_reason": "end_turn", "usage": USAGE}

        log = investigate(candidate(), CostContext(
            conn=db, governor_profit_share=Decimal("0.10"),
            cycle_id="cy", kind="scheduled"), transport)
        # ...and it says WHICH gate: an unpriced row is a pricing fault,
        # not an exhausted budget, and reading "budget_denied" for it
        # sends the reader to the settings page for a code bug.
        assert log.skipped_reason.startswith("budget_denied")
        assert "unpriced_cost_rows" in log.skipped_reason, log.skipped_reason
        assert calls["n"] == 0
        assert db.execute("SELECT decision, reason FROM cost_governor_events"
                          ).fetchone() == ("deny", "unpriced_cost_rows")

    def test_unpriced_row_blocks_the_whole_cycle_from_spending(self, db):
        db.execute("INSERT INTO cost_events VALUES "
                   "('x','{}','claude-sonnet-5','scheduled','research',"
                   "NULL,'2026-08-10T00:00:00+00:00','a')")
        db.commit()
        broker, state = broker_for()
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel["researched"] == 0
        assert state["posts"] == []


# =====================================================================
# 4. Feed payloads: form4_adapter -> build_candidates
# =====================================================================

def filing_event(sid, ciks, value, filed="2026-08-05", ticker="ABC",
                 issuer="999"):
    return RawEvent("edgar_form4", sid, NOW, {"parsed": {
        "issuer_cik": issuer, "ticker": ticker, "filed_date": filed,
        "accession": sid,
        "owners": [{"cik": c, "name": f"n{c}", "role": "CFO"} for c in ciks],
        "ten_b5_1": {"element": None, "footnote_mention": False},
        "transactions": [{"table": "non_derivative", "code": "P",
                          "acquired_disposed": "A", "shares": "1000",
                          "price_per_share": "10", "value_usd": value,
                          "transaction_date": filed,
                          "shares_owned_following": "5"}]}})


def total_usd(cand_):
    for tag in cand_.correlation_tags:
        if tag.startswith("fact:total_usd="):
            return Decimal(tag.split("=", 1)[1])
    return None


class TestFeedPayloads:
    def test_duplicate_delivery_does_not_manufacture_a_cluster(self):
        """DEFECT 15 (fixed in discovery/candidates.py): the same filing
        delivered twice (a feed retry, or overlapping pagination windows)
        was counted twice. Two insiders x $20k = $40k is below the $50k
        cluster floor; delivered twice it reads as $80k and BUYS. A
        duplicate row is not new evidence."""
        rows = flatten_form4_events([filing_event("a1", ["1", "2"], "20000")])
        assert build_candidates(rows, NOW) == []
        doubled = build_candidates(rows + rows, NOW)
        assert doubled == [], (
            f"duplicate delivery produced {[total_usd(c) for c in doubled]}")

    def test_duplicate_delivery_does_not_inflate_a_real_cluster(self):
        rows = flatten_form4_events([filing_event("a1", ["1", "2"], "30000")])
        once = build_candidates(rows, NOW)
        twice = build_candidates(rows + rows, NOW)
        assert [c.id for c in once] == [c.id for c in twice]
        assert total_usd(once[0]) == total_usd(twice[0]) == Decimal("60000")

    def test_owner_that_is_not_an_object_does_not_kill_the_feed(self):
        """DEFECT 16 (fixed in data/form4_adapter.py): a non-object entry
        in owners raised AttributeError and lost the ENTIRE batch of
        filings, not just the malformed one."""
        events = [
            RawEvent("edgar_form4", "bad", NOW, {"parsed": {
                "issuer_cik": "1", "ticker": "ABC", "filed_date": "2026-08-05",
                "owners": ["a string", None, 7],
                "transactions": [{"table": "non_derivative", "code": "P",
                                  "acquired_disposed": "A", "shares": "1",
                                  "price_per_share": "1", "value_usd": "1"}]}}),
            filing_event("good", ["1", "2"], "30000"),
        ]
        flat = flatten_form4_events(events)
        assert any(e.payload_raw["symbol"] == "ABC" for e in flat)
        assert len(build_candidates(flat, NOW)) == 1

    @pytest.mark.parametrize("payload", [
        {}, {"parsed": None}, {"parsed": {}}, {"parsed": {"owners": None}},
        {"parsed": {"transactions": None}},
        {"parsed": {"transactions": ["nope"]}},
    ])
    def test_missing_or_null_feed_sections_flatten_to_nothing(self, payload):
        """SURVIVED: absent sections drop the filing, they never raise."""
        assert flatten_form4_events(
            [RawEvent("edgar_form4", "s", NOW, payload)]) == []

    def test_payload_none_flattens_to_nothing(self):
        assert flatten_form4_events([RawEvent("edgar_form4", "s", NOW, None)]) == []

    def test_owners_without_ciks_collapse_to_one_insider(self):
        """SURVIVED: blank CIKs are one distinct owner, not N - the
        cluster floor fails closed."""
        flat = flatten_form4_events([filing_event("a1", [None, None], "60000")])
        assert build_candidates(flat, NOW) == []

    @pytest.mark.parametrize("symbol,expected", [
        ("$SPY", 0), ("BRK.B", 0), ("ABCDEF", 0), ("AB C", 0), ("", 0),
        ("A1", 0), ("abc", 1),
    ])
    def test_symbol_validity(self, symbol, expected):
        """SURVIVED: only 1-5 ASCII letters pass; lowercase is upcased."""
        flat = flatten_form4_events(
            [filing_event("s" + symbol, ["1", "2"], "60000", ticker=symbol)])
        assert len(build_candidates(flat, NOW)) == expected

    def test_form4_claiming_an_etf_ticker_is_refused(self):
        flat = flatten_form4_events(
            [filing_event("etf", ["1", "2"], "60000", ticker="SPY")])
        assert build_candidates(flat, NOW) == []

    def test_absurd_value_usd_is_refused(self):
        flat = flatten_form4_events([filing_event("big", ["1", "2"], "1e9")])
        assert build_candidates(flat, NOW) == []

    def test_negative_value_does_not_create_a_cluster(self):
        """SURVIVED: negative value sums below the floor."""
        flat = flatten_form4_events([filing_event("neg", ["1", "2"], "-90000")])
        assert build_candidates(flat, NOW) == []

    def test_future_filing_date_is_invisible(self):
        """SURVIVED: point-in-time discipline holds against a feed that
        claims tomorrow's filings."""
        flat = flatten_form4_events(
            [filing_event("fut", ["1", "2"], "60000", filed="2026-09-01")])
        assert build_candidates(flat, NOW) == []
        assert len(build_candidates(flat, datetime(2026, 9, 2, tzinfo=timezone.utc))) == 1

    @pytest.mark.parametrize("bad_date", ["05/08/2026", "", None, "2026-13-45",
                                          "2026-08-05T00:00:00Z"])
    def test_malformed_filing_dates_are_dropped(self, bad_date):
        """SURVIVED: an unparseable date drops the row rather than
        guessing a tradeable date."""
        flat = flatten_form4_events(
            [filing_event("bd", ["1", "2"], "60000", filed=bad_date)])
        assert build_candidates(flat, NOW) == []

    def test_missing_value_usd_drops_the_row(self):
        """SURVIVED (noted): a filing with shares and price but no
        value_usd is dropped, matching the CSV the backtest graded."""
        ev = filing_event("nv", ["1", "2"], None)
        assert build_candidates(flatten_form4_events([ev]), NOW) == []


# =====================================================================
# 5. State machine and persistence
# =====================================================================

class TestForeignKeys:
    def test_full_cycle_survives_production_foreign_keys(self, prod_db):
        """DEFECT 1 (fixed in storage/schema.sql): orders.decision_id is
        populated with the CANDIDATE id everywhere in execution (and
        reconcile joins risk_decisions ON candidate_id), but the schema
        declared REFERENCES risk_decisions(id). storage.init_db turns
        foreign keys ON, so in production the entry order INSERT raised
        IntegrityError - AFTER the buy had been sent to Alpaca. Every
        live trade would have left an unrecorded position. No test caught
        it because every execution test opens sqlite directly, where
        foreign keys default OFF."""
        broker, state = broker_for()
        report = run(prod_db, broker, model_transport(), [candidate()])
        assert report.funnel["orders_placed"] == 1
        assert prod_db.execute("SELECT COUNT(*) FROM positions"
                               ).fetchone()[0] == 1
        assert prod_db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert prod_db.execute("PRAGMA foreign_key_check").fetchall() == []

    def test_full_exit_path_survives_production_foreign_keys(self, prod_db):
        broker, state = broker_for()
        run(prod_db, broker, model_transport(), [candidate()])
        # jump past the exit date: the position must close cleanly
        later = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
        run_cycle(prod_db, broker, None, lambda s, u: [],
                  lambda e, a: [], lambda c, o: {}, now=later,
                  entry_poll_attempts=1, entry_poll_interval_s=0)
        assert prod_db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [p["side"] for p in state["posts"]].count("sell") >= 1


class TestBrokenPositionRows:
    def test_due_position_without_an_entry_order_sends_no_order(self, db):
        """DEFECT 17 (fixed in orchestrator/cycle.py): a position row with
        entry_order_ids '[]' resolved qty and decision_id to NULL, and the
        hard-exit path then POSTED A MARKET SELL WITH qty='None' before
        dying on a NOT NULL constraint. A garbage order reached the broker
        and the cycle never finished."""
        db.execute("INSERT INTO positions VALUES "
                   "('p1','ZZZ','[]',NULL,'2026-08-01T14:00:00+00:00',"
                   "'2026-08-09','open')")
        db.commit()
        broker, state = broker_for()
        report = run(db, broker, None, [], events=[])
        assert state["posts"] == []
        assert any("p1" in e for e in report.errors)

    def test_malformed_entry_order_ids_json_does_not_kill_the_cycle(self, db):
        """DEFECT 18 (fixed in orchestrator/cycle.py): SQLite's
        json_extract raises OperationalError('malformed JSON') on a
        corrupt entry_order_ids value, killing the cycle before reconcile,
        stops or exits ran."""
        db.execute("INSERT INTO positions VALUES "
                   "('p1','ZZZ','not json',NULL,'2026-08-01T14:00:00+00:00',"
                   "'2026-08-20','open')")
        db.commit()
        broker, state = broker_for()
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel["raw_events"] == 1     # the cycle completed

    def test_position_whose_entry_order_is_missing_is_reported(self, db):
        db.execute("INSERT INTO positions VALUES "
                   "('p1','ZZZ','[\"gone\"]',NULL,"
                   "'2026-08-01T14:00:00+00:00','2026-08-09','open')")
        db.commit()
        broker, state = broker_for()
        report = run(db, broker, None, [], events=[])
        assert state["posts"] == []
        assert any("p1" in e for e in report.errors)


class TestReconcileOrdering:
    def test_stop_fill_before_entry_fill_closes_correctly(self, db):
        """SURVIVED: a stop fill observed before the entry fill leaves the
        position open until the entry is known, then closes it with the
        right entry price and P&L."""
        seed_decision(db, "c1", "T")
        db.execute("INSERT INTO orders VALUES ('o-buy','c1','b1','buy','2',"
                   "'market','day','2026-08-01T14:00:00+00:00','filled','{}')")
        db.execute("INSERT INTO orders VALUES ('o-stop','c1','b2','sell','2',"
                   "'stop','day','2026-08-01T14:05:00+00:00','filled','{}')")
        db.execute("INSERT INTO fills VALUES ('o-stop','45.00','2',"
                   "'2026-08-03T15:00:00+00:00','45.00',NULL)")
        db.execute("INSERT INTO positions VALUES ('p1','T','[\"o-buy\"]','b2',"
                   "'2026-08-01T14:00:00+00:00','2026-08-20','open')")
        db.commit()
        assert close_filled_positions(db, now=NOW) == 0      # not yet
        assert db.execute("SELECT status FROM positions").fetchone()[0] == "open"
        db.execute("INSERT INTO fills VALUES ('o-buy','50.00','2',"
                   "'2026-08-01T14:00:00+00:00','50.00',NULL)")
        db.commit()
        assert close_filled_positions(db, now=NOW) == 1
        assert db.execute("SELECT entry_price, exit_price, exit_reason, "
                          "realized_pnl_cents FROM closed_trades").fetchone() \
            == ("50.00", "45.0000", "stop", -1000)

    def test_partial_exit_leaves_the_position_open(self, db):
        """SURVIVED: selling half does not close the trade."""
        seed_decision(db, "c1", "T")
        db.execute("INSERT INTO orders VALUES ('o-buy','c1','b1','buy','2',"
                   "'market','day','2026-08-01T14:00:00+00:00','filled','{}')")
        db.execute("INSERT INTO orders VALUES ('o-stop','c1','b2','sell','2',"
                   "'stop','day','2026-08-01T14:05:00+00:00','filled','{}')")
        db.execute("INSERT INTO fills VALUES ('o-buy','50.00','2',"
                   "'2026-08-01T14:00:00+00:00','50.00',NULL)")
        db.execute("INSERT INTO fills VALUES ('o-stop','45.00','1',"
                   "'2026-08-03T15:00:00+00:00','45.00',NULL)")
        db.execute("INSERT INTO positions VALUES ('p1','T','[\"o-buy\"]','b2',"
                   "'2026-08-01T14:00:00+00:00','2026-08-20','open')")
        db.commit()
        assert close_filled_positions(db, now=NOW) == 0
        assert db.execute("SELECT status FROM positions").fetchone()[0] == "open"


class TestClosingArithmetic:
    def _seed(self, conn, *, entry_qty="2", exit_qty="2",
              exit_at="2026-08-09T14:00:00+00:00"):
        seed_decision(conn, "c1", "T")
        conn.execute("INSERT INTO orders VALUES ('o-buy','c1','b1','buy','2',"
                     "'market','day','2026-08-05T14:00:00+00:00','filled','{}')")
        conn.execute("INSERT INTO orders VALUES ('o-sell','c1','b2','sell','2',"
                     "'stop','day','2026-08-05T14:05:00+00:00','filled','{}')")
        conn.execute("INSERT INTO fills VALUES ('o-buy','50.00',?,"
                     "'2026-08-05T14:00:00+00:00','50.00',NULL)", (entry_qty,))
        conn.execute("INSERT INTO fills VALUES ('o-sell','45.00',?,?,"
                     "'45.00',NULL)", (exit_qty, exit_at))
        conn.execute("INSERT INTO positions VALUES ('p1','T','[\"o-buy\"]',"
                     "'b2','2026-08-05T14:00:00+00:00','2026-08-20','open')")
        conn.commit()

    def test_zero_quantity_rows_do_not_divide_by_zero(self, db):
        """DEFECT 27 (fixed in execution/reconcile.py): a position whose
        fills carry qty 0 reached the qty-weighted exit price division
        and raised DivisionUndefined, killing the cycle."""
        self._seed(db, entry_qty="0", exit_qty="0")
        assert close_filled_positions(db, now=NOW) == 0
        assert db.execute("SELECT status FROM positions").fetchone()[0] == "open"

    def test_exit_dated_before_entry_does_not_crash(self, db):
        """SURVIVED (noted): an out-of-order broker timestamp produces a
        NEGATIVE actual_holding_days rather than an error. Harmless to
        money, but it is evidence the holding-period parameter adapts on
        - see the report."""
        self._seed(db, exit_at="2026-08-01T14:00:00+00:00")
        assert close_filled_positions(db, now=NOW) == 1
        assert db.execute("SELECT actual_holding_days FROM closed_trades"
                          ).fetchone()[0] == -4

    def test_exit_larger_than_the_entry_still_closes_cleanly(self, db):
        self._seed(db, exit_qty="200")
        assert close_filled_positions(db, now=NOW) == 1
        assert db.execute("SELECT realized_pnl_cents FROM closed_trades"
                          ).fetchone()[0] == -1000


class TestOverlappingCycles:
    def test_second_cycle_does_not_double_enter(self, db):
        """SURVIVED: re-running the same cycle back to back places one
        buy, not two."""
        broker, state = broker_for()
        r1 = run(db, broker, model_transport(), [candidate()])
        r2 = run(db, broker, model_transport(), [candidate()])
        assert r1.funnel["orders_placed"] == 1
        assert r2.funnel["orders_placed"] == 0
        assert len([p for p in state["posts"] if p["side"] == "buy"]) == 1
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1

    def test_same_ticker_from_a_different_candidate_is_screened(self, db):
        """SURVIVED: an open position in a ticker blocks a second
        candidate for the same ticker."""
        broker, state = broker_for()
        run(db, broker, model_transport(), [candidate("c1", "TEST")])
        report = run(db, broker, model_transport(), [candidate("c2", "TEST")])
        assert any("position_already_open" in r
                   for r in report.drop_reasons["screened"])

    def test_colliding_candidate_id_with_different_content_is_refused(self, db):
        """DEFECT 19 (fixed in orchestrator/cycle.py): candidate ids are
        content hashes, so a collision means two different clusters share
        an id. INSERT OR IGNORE kept the FIRST row while the SECOND
        candidate was researched and traded - the audit trail then
        described a trade in AAA as a trade in BBB. Every trade must be
        explainable after the fact (BUILD-BRIEF)."""
        broker, state = broker_for()
        run(db, broker, model_transport(), [candidate("dup", "AAA")])
        db.execute("DELETE FROM research_views")     # force re-research
        db.commit()
        report = run(db, broker, model_transport(), [candidate("dup", "BBB")])
        rows = db.execute("SELECT id, ticker FROM candidates").fetchall()
        assert rows == [("dup", "AAA")]
        assert report.funnel["orders_placed"] == 0
        assert any("collision" in r for r in report.drop_reasons["screened"])


class TestClockAndTimestamps:
    @pytest.mark.parametrize("is_open", ["false", "0", "no", [], {}, "closed"])
    def test_only_a_real_true_counts_as_open(self, db, is_open):
        """DEFECT 20 (fixed in orchestrator/cycle.py): is_open arriving as
        the STRING 'false' is truthy, so the bot researched and TRADED
        with the market shut. A queued market order fills at an opening
        price unrelated to the mid that sized it (risk review F5) - the
        exact hazard the check exists to prevent."""
        broker, state = broker_for(clock={"is_open": is_open})
        report = run(db, broker, model_transport(), [candidate()])
        assert state["posts"] == [], f"traded on is_open={is_open!r}"
        assert any("market_closed" in r
                   for r in report.drop_reasons["researched"])

    def test_genuinely_open_still_trades(self, db):
        broker, state = broker_for(clock={"is_open": True})
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel["orders_placed"] == 1

    @pytest.mark.parametrize("ts,fresh", [
        ("2026-08-10T13:59:00", True),          # naive, UTC assumed
        ("2026-08-10 13:59:00", True),          # naive, space separator
        ("2026-08-10T09:59:00", False),         # naive and stale
        ("2026-08-10T14:59:00+01:00", True),    # 13:59Z
        ("2026-08-10T10:00:00-04:00", True),    # 14:00Z, US/Eastern offset
        ("2026-08-10T09:00:00-04:00", False),   # 13:00Z, an hour stale
        ("2026-08-08T19:59:59Z", False),        # Friday's close
        ("not-a-time", False),
        (1234567, False),
    ])
    def test_quote_timestamps_in_every_shape(self, ts, fresh):
        """DEFECT 21 (fixed in orchestrator/cycle.py): a timestamp with no
        timezone raised TypeError ('can't subtract offset-naive and
        offset-aware') out of build_market_snapshot - only ValueError was
        caught. One feed dropping the trailing Z killed the cycle."""
        b = brk(lambda r: httpx.Response(200, json={"quote": {
            "bp": 49.95, "ap": 50.05, "t": ts}}))
        snap = build_market_snapshot(b, "T", NOW)
        assert (snap is not None) is fresh

    def test_stale_portfolio_read_is_a_full_standdown(self, db):
        """SURVIVED: a portfolio read older than 10 minutes stands the
        cycle down entirely. It is measured against the WALL clock, not
        the injected one - the one place in the pipeline that does not
        take its time from the caller (ESCALATION-10)."""
        from datetime import timedelta

        broker, state = broker_for()
        report = run(db, broker, model_transport(), [candidate()],
                     now=NOW - timedelta(minutes=20))
        assert report.kill_switch.reason == "portfolio_state_stale"
        assert state["posts"] == []

    def test_clock_moving_forward_mid_cycle_fails_closed(self, db,
                                                         monkeypatch):
        """An NTP correction (or a suspended VM) between the broker read
        and the kill-switch check makes the snapshot look stale. It must
        stand down, not proceed on state it cannot date."""
        import catalyst.risk.kill_switches as kill_switches
        from datetime import timedelta

        class _JumpedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW + timedelta(hours=1)

        monkeypatch.setattr(kill_switches, "datetime", _JumpedClock)
        broker, state = broker_for()
        report = run(db, broker, model_transport(), [candidate()])
        assert report.kill_switch.reason == "portfolio_state_stale"
        assert state["posts"] == []

    def test_corrupt_planned_exit_date_fails_closed(self, db):
        """DEFECT 22 (fixed in orchestrator/cycle.py): an unparseable date
        on an open position row raised ValueError inside
        build_portfolio_state - BEFORE the kill switch ran, so the cycle
        died rather than standing down. Dropping the row instead would
        understate exposure and let sizing over-allocate, so the portfolio
        is declared unreliable."""
        db.execute("INSERT INTO positions VALUES "
                   "('p1','ZZZ','[]',NULL,'2026-08-01T14:00:00+00:00',"
                   "'whenever','open')")
        db.commit()
        broker, state = broker_for()
        assert build_portfolio_state(broker, db, NOW) is None
        report = run(db, broker, model_transport(), [candidate()])
        assert report.kill_switch.tripped
        assert state["posts"] == []


class TestModelTransportFailure:
    def test_transport_exception_does_not_kill_the_cycle(self, db):
        """DEFECT 23 (fixed in research/boundary.py): the live transport
        raises on a network error or an API 5xx. That exception escaped
        investigate() and killed the cycle mid-loop - after an earlier
        candidate may already have been traded - and left no
        research_calls row for a call that may well have been billed."""
        def boom(payload):
            raise httpx.ConnectError("api.anthropic.com unreachable")

        broker, state = broker_for()
        report = run(db, broker, boom, [candidate()])
        assert report.funnel["researched"] == 0
        assert any("transport_error" in r
                   for r in report.drop_reasons["researched"])
        row = db.execute("SELECT skipped_reason FROM research_calls"
                         ).fetchone()
        assert row and "transport_error" in row[0]
        assert state["posts"] == []

    def test_response_without_a_usage_object_is_not_priced_at_zero(self, db):
        """DEFECT 24 (fixed in research/boundary.py): a response carrying
        no usage object at all priced itself at $0.00 and the governor
        never noticed. TRAPS.md's renamed-field trap is exactly this: the
        unknown-field guard inspected the usage object's CONTENTS, so it
        could not see the object being absent."""
        def no_usage(payload):
            if (payload.get("tool_choice") or {}).get("type") == "tool":
                return {"content": [{"type": "tool_use",
                                     "name": "submit_research_view",
                                     "input": GOOD_VIEW}],
                        "stop_reason": "tool_use"}
            return {"content": [], "stop_reason": "end_turn"}

        broker, state = broker_for()
        report = run(db, broker, no_usage, [candidate()])
        rows = db.execute("SELECT raw_usage_json, priced_cents "
                          "FROM cost_events").fetchall()
        assert rows and all(r[1] is None for r in rows)
        assert state["posts"] == []
        from catalyst.cost.tracker import has_unpriced_rows
        assert has_unpriced_rows(db)

    def test_unserializable_feed_payload_does_not_kill_the_cycle(self, db):
        """DEFECT 25 (fixed in orchestrator/cycle.py): a payload holding
        anything json.dumps cannot encode (a Decimal or datetime left in
        by a parser) raised TypeError while persisting raw_events, after
        a SUCCESSFUL fetch."""
        broker, state = broker_for()
        event = RawEvent("edgar_form4", "e1", NOW,
                         {"value": Decimal("1.5"), "when": NOW})
        report = run(db, broker, model_transport(), [candidate()],
                     events=[event])
        assert report.funnel["raw_events"] == 1
        stored = db.execute("SELECT payload_raw FROM raw_events").fetchone()[0]
        assert "1.5" in stored


class TestUnknownCatalystType:
    def test_unknown_catalyst_type_is_a_skip_not_a_crash(self, db):
        unknown = Candidate(
            id="x", ticker="TEST", catalyst_type="earnings_drift",
            catalyst_date=date(2026, 8, 20),
            catalyst_date_confidence="estimated", source_event_ids=(),
            discovered_at=NOW, sector="tech", correlation_tags=())
        broker, state = broker_for()
        report = run(db, broker, model_transport(), [unknown])
        assert report.funnel["proposed"] == 0
        assert state["posts"] == []


class TestRefusalScoring:
    def _due_refusal(self, conn):
        seed_decision(conn, "c1", "T")
        conn.execute("INSERT INTO refusals VALUES "
                     "('dec-1','c1','50.00','2026-07-01T14:00:00+00:00',"
                     "NULL,NULL,NULL)")
        conn.commit()

    @pytest.mark.parametrize("quote", [
        {"quote": {"bp": "NaN", "ap": "NaN"}},
        {"quote": {"bp": None, "ap": None}},
        {"quote": []},
        {"quote": {}},
        {},
    ])
    def test_hostile_quote_never_fabricates_a_refusal_outcome(self, db, quote):
        """The refusal tracker is the main feedback loop; a fabricated or
        crashing outcome corrupts the evidence that moves the conviction
        floor. NaN quotes used to raise InvalidOperation from the
        comparison (same root cause as defect 3)."""
        from catalyst.risk.refusal_tracker import score_due_refusals

        self._due_refusal(db)
        b = brk(lambda r: httpx.Response(200, json=quote))
        assert score_due_refusals(b, db, NOW) == 0
        assert db.execute("SELECT scored_at, outcome_return FROM refusals"
                          ).fetchone() == (None, None)

    def test_broker_failure_leaves_the_refusal_for_next_time(self, db):
        from catalyst.risk.refusal_tracker import score_due_refusals

        self._due_refusal(db)
        assert score_due_refusals(
            brk(lambda r: httpx.Response(500, json={})), db, NOW) == 0
        assert score_due_refusals(
            brk(lambda r: httpx.Response(200, json=QUOTE)), db, NOW) == 1

    def test_a_cycle_survives_a_refusal_scoring_failure(self, db):
        self._due_refusal(db)
        broker, state = broker_for(quote={"quote": {"bp": "NaN", "ap": 1}})
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel["raw_events"] == 1


class TestCorruptDecisionSnapshot:
    def test_unreadable_params_snapshot_does_not_kill_the_portfolio_read(
            self, db):
        """A corrupt adaptive_params_snapshot used to raise from
        json.loads inside build_portfolio_state. An empty cluster key is
        safe (the fallback re-derives one); a dead cycle is not."""
        seed_decision(db, "c1", "T")
        db.execute("UPDATE risk_decisions SET adaptive_params_snapshot='{ne'")
        db.execute("INSERT INTO orders VALUES ('o1','c1','b1','buy','2',"
                   "'market','day','2026-08-01T14:00:00+00:00','filled','{}')")
        db.execute("INSERT INTO positions VALUES ('p1','T','[\"o1\"]','s1',"
                   "'2026-08-01T14:00:00+00:00','2026-08-20','open')")
        db.commit()
        broker, state = broker_for()
        portfolio = build_portfolio_state(broker, db, NOW)
        assert portfolio is not None
        assert portfolio.open_positions[0].cluster_key == ""


class TestGovernorUnderTheBoundary:
    def test_cap_exhausted_mid_run_skips_and_says_so(self, db):
        """SURVIVED: BUILD-BRIEF - 'if a cycle would breach the cap, it
        skips and reports that it skipped'."""
        # authorize() sums month-to-date against datetime.now() - it takes
        # no as_of from the cycle - so the spend must be seeded into the
        # REAL current month. A literal '2026-08-...' here passed only for
        # as long as the real clock agreed with it, and from 2026-09-01
        # would have failed forever, blocking every upgrade (house rule 6).
        today = datetime.now(timezone.utc).date()
        seeded = today.replace(day=1)
        db.execute("INSERT INTO cost_events VALUES "
                   "('spent','{}','claude-sonnet-5','scheduled','research',"
                   "'499',?,'a')",
                   (datetime(seeded.year, seeded.month, seeded.day,
                             tzinfo=timezone.utc).isoformat(),))
        db.commit()
        # The daily ceiling is checked BEFORE the monthly cap, so which
        # one names the refusal depends on whether the seeded spend is
        # also today's spend. On the 1st the two windows are the same
        # window and the monthly cap cannot bind first - that is the
        # governor being right, not the test being loose.
        expect = "daily_cap_exceeded" if seeded == today else "cap_exceeded"
        broker, state = broker_for()
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel["researched"] == 0
        assert any("budget_denied" in r
                   for r in report.drop_reasons["researched"])
        assert state["posts"] == []
        assert db.execute(
            "SELECT reason FROM cost_governor_events WHERE decision='deny'"
        ).fetchone()[0].startswith(expect)

    def test_unacknowledged_reconciliation_pause_blocks_spend(self, db):
        """SURVIVED: a paused reconciliation stops all model spend until
        a human acknowledges it."""
        db.execute(
            # Columns NAMED, never positional (CLAUDE.md).
            "INSERT INTO cost_reconciliation_events "
            "(id, target_date, kind, component, local_total_cents, cost_api_total_cents, discrepancy_cents, threshold_cents, api_raw_response, api_record_count, action_taken, acknowledged_by, acknowledged_at, reconciled_at) "
            "VALUES ('r1','2026-08-09','all','{}','100','40','60','5',"
            "'{}',1,'scheduled_paused',NULL,NULL,"
            "'2026-08-10T00:00:00+00:00')")
        db.commit()
        broker, state = broker_for()
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel["researched"] == 0
        assert state["posts"] == []

    def test_usage_with_unserializable_values_is_still_recorded(self, db):
        """record() json.dumps the raw usage: a value it cannot encode
        must not stop the row landing - money was already spent."""
        from catalyst.cost.tracker import record_usage

        with pytest.raises(Exception):
            record_usage({"input_tokens": 1, "output_tokens": 1,
                          "weird_tokens": {1, 2}},
                         "claude-sonnet-5", "scheduled", "research", db)
        row = db.execute("SELECT raw_usage_json, priced_cents "
                         "FROM cost_events").fetchone()
        assert row is not None and row[1] is None


class TestMoneyEdges:
    def test_equity_below_the_minimum_position_size_skips(self, db):
        """SURVIVED: a $2 account sizes to nothing and skips with a
        reason, rather than sending a 0-share order."""
        broker, state = broker_for(account={
            "equity": "2", "cash": "2", "last_equity": "2",
            "non_marginable_buying_power": "2"})
        report = run(db, broker, model_transport(), [candidate()])
        assert state["posts"] == []
        assert any("notional_below_minimum" in r
                   for r in report.drop_reasons["proposed"])

    def test_settled_cash_exhausted_mid_cycle_stops_entering(self, db):
        """SURVIVED: cash spent by the first entry bounds the second."""
        broker, state = broker_for(account={
            "equity": "1000", "cash": "60", "last_equity": "1000",
            "non_marginable_buying_power": "60"})
        run(db, broker, model_transport(),
            [candidate("c1", "AAA"), candidate("c2", "BBB")])
        buys = [p for p in state["posts"] if p["side"] == "buy"]
        spent = sum(Decimal(b["qty"]) * Decimal("50") for b in buys)
        assert spent <= Decimal("60")

    def test_drawdown_kill_blocks_entries_and_records_the_switch(self, db):
        """SURVIVED: every kill switch trips deliberately."""
        db.execute("INSERT INTO equity_snapshots VALUES "
                   "('2026-08-01','2026-08-01T14:00:00+00:00','1300','1300',"
                   "'0','broker_read')")
        db.commit()
        broker, state = broker_for(account={
            "equity": "1000", "cash": "1000", "last_equity": "1000",
            "non_marginable_buying_power": "1000"})
        report = run(db, broker, model_transport(), [candidate()])
        assert report.kill_switch.reason == "drawdown_kill"
        assert state["posts"] == []
        assert db.execute("SELECT switch_name FROM kill_switch_events"
                          ).fetchone()[0] == "drawdown_kill"

    def test_consecutive_losses_kill(self, db):
        for i in range(5):
            db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                       (f"p{i}", "T", "[]", None, "2026-08-01T14:00:00+00:00",
                        "2026-08-20", "closed"))
            db.execute("INSERT INTO closed_trades VALUES "
                       "(?, 'paper','50','49','stop',-100,12,3,?)",
                       (f"p{i}", f"2026-08-0{i + 1}T20:00:00+00:00"))
        db.commit()
        broker, state = broker_for()
        report = run(db, broker, model_transport(), [candidate()])
        assert report.kill_switch.reason == "consecutive_losses_kill"
        assert state["posts"] == []
