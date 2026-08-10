"""Orchestrator cycle tests - the whole pipeline, fully offline.

Broker runs on httpx.MockTransport; feed/candidates/cluster/model
transport are injected stubs. These tests pin the ORDER of stages, the
funnel bookkeeping, and the fail-closed behaviors.
"""

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst.data import RawEvent
from catalyst.discovery import Candidate
from catalyst.execution.broker import Broker
from catalyst.orchestrator.cycle import (
    build_market_snapshot, build_portfolio_state, run_cycle,
)
from catalyst.research import prompts

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.executescript(open("catalyst/storage/schema.sql").read())
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def frozen_kill_switch_clock(monkeypatch):
    """Staleness is deliberately judged against the WALL clock at check
    time (NTP-jump/suspended-VM protection - stress escalation 10), so
    these tests pin that clock to NOW; otherwise the whole file goes red
    the moment the real clock passes NOW + 10 minutes."""
    import catalyst.risk.kill_switches as kill_switches

    class _FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(kill_switches, "datetime", _FrozenClock)


@pytest.fixture(autouse=True)
def stub_prompts(monkeypatch):
    monkeypatch.setattr(prompts, "render_research_prompt",
                        lambda c, graph_context=None: "research")
    monkeypatch.setattr(prompts, "exploration_tools", lambda: [])


ACCOUNT = {"equity": "1000", "cash": "1000", "last_equity": "1000",
           "non_marginable_buying_power": "1000"}
QUOTE = {"quote": {"bp": 49.95, "ap": 50.05, "t": "2026-08-10T13:59:30Z"}}   # mid 50, half-spread 10bp


def broker_for(state=None):
    """MockTransport broker: healthy account, market open, tight quote,
    orders accepted, market orders fill immediately at 50."""
    state = state if state is not None else {}
    state.setdefault("posts", [])
    state.setdefault("qty_by_id", {})
    state.setdefault("market_open", True)

    def handler(request):
        url = str(request.url)
        if "/v2/account" in url:
            return httpx.Response(200, json=dict(ACCOUNT))
        if "/v2/clock" in url:
            return httpx.Response(200, json={"is_open": state["market_open"]})
        if "/quotes/latest" in url:
            return httpx.Response(200, json=dict(QUOTE))
        if request.method == "POST" and url.endswith("/v2/orders"):
            body = json.loads(request.content)
            state["posts"].append(body)
            broker_id = f"brok-{len(state['posts'])}"
            state["qty_by_id"][broker_id] = body["qty"]
            return httpx.Response(200, json={
                "id": broker_id, "status": "accepted"})
        if "by_client_order_id" in url:
            return httpx.Response(200, json={
                "id": "brok-x", "status": "filled", "filled_qty": "1",
                "filled_avg_price": "50.00",
                "filled_at": "2026-08-10T13:31:00Z"})
        if request.method == "GET" and "/v2/orders/" in url:
            broker_id = url.rsplit("/", 1)[1]
            return httpx.Response(200, json={
                "id": broker_id, "status": "filled",
                "filled_qty": state["qty_by_id"].get(broker_id, "1"),
                "filled_avg_price": "50.00",
                "filled_at": "2026-08-10T13:31:00Z"})
        if "/v2/positions" in url:
            return httpx.Response(200, json=[])
        if "/v2/orders" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "unexpected"})

    return Broker("k", "s", transport=httpx.MockTransport(handler),
                  backoff_s=0), state


def candidate(cid="cand-1", ticker="TEST"):
    return Candidate(
        id=cid, ticker=ticker, catalyst_type="insider_cluster",
        catalyst_date=date(2026, 8, 20), catalyst_date_confidence="estimated",
        source_event_ids=("e1",), discovered_at=NOW, sector="tech",
        correlation_tags=("tech",))


def event(sid="acc-1"):
    return RawEvent(source="edgar_form4", source_id=sid, fetched_at=NOW,
                    payload_raw={"accession": sid})


GOOD_VIEW = {
    "direction": "long", "conviction": 0.8, "thesis": "t",
    "invalidation": "i", "expected_holding_days": 12,
    "priced_in": False, "priced_in_reasoning": "r",
}
USAGE = {"input_tokens": 100, "output_tokens": 50}


def model_transport(view=None):
    v = view or dict(GOOD_VIEW)

    def transport(payload):
        if (payload.get("tool_choice") or {}).get("type") == "tool":
            return {"content": [{"type": "tool_use",
                                 "name": "submit_research_view", "input": v}],
                    "stop_reason": "tool_use", "usage": dict(USAGE)}
        return {"content": [], "stop_reason": "end_turn",
                "usage": dict(USAGE)}

    return transport


def run(db, broker, transport, cands, events=None, **kw):
    return run_cycle(
        db, broker, transport,
        feed_fetch=lambda s, u: events if events is not None else [event()],
        build_candidates_fn=lambda evs, as_of: cands,
        cluster_fn=lambda cs, ops: {c.id: f"{c.sector}-w34" for c in cs},
        now=NOW, **kw)


class TestKillSwitch:
    def test_broker_down_trips_and_stops_everything(self, db):
        def handler(request):
            return httpx.Response(500, json={})

        b = Broker("k", "s", transport=httpx.MockTransport(handler),
                   backoff_s=0)
        feed_called = {"n": 0}

        def feed(s, u):
            feed_called["n"] += 1
            return []

        report = run_cycle(db, b, None, feed, lambda e, a: [],
                           lambda c, o: {}, now=NOW)
        assert report.kill_switch.tripped
        assert report.kill_switch.reason == "portfolio_state_unreliable"
        assert feed_called["n"] == 0            # nothing after the trip
        row = db.execute("SELECT switch_name, portfolio_state_snapshot "
                         "FROM kill_switch_events").fetchone()
        assert row[0] == "portfolio_state_unreliable"
        assert json.loads(row[1])["portfolio"] is None


class TestFeedFailure:
    def test_unreachable_is_not_empty(self, db):
        broker, _ = broker_for()

        def feed(s, u):
            raise RuntimeError("connection refused by efts.sec.gov")

        report = run_cycle(db, broker, None, feed, lambda e, a: [],
                           lambda c, o: {}, now=NOW)
        assert report.funnel["raw_events"] == 0
        assert report.drop_reasons["raw_events"] == [
            "feed_unreachable_see_raw_events_errors"]
        raw = db.execute("SELECT error_text FROM raw_events_errors"
                         ).fetchone()[0]
        assert "connection refused" in raw     # the raw response, beside the zero
        # a genuinely empty (but successful) fetch is different:
        report2 = run(db, broker, None, [], events=[])
        assert report2.funnel["raw_events"] == 0
        assert "raw_events" not in report2.drop_reasons


class TestHappyPath:
    def test_trade_end_to_end(self, db):
        broker, state = broker_for()
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel == {"raw_events": 1, "candidates": 1,
                                 "screened": 1, "researched": 1,
                                 "proposed": 1, "orders_placed": 1}
        # entry buy + protective stop, in that order
        assert [p["side"] for p in state["posts"]] == ["buy", "sell"]
        assert state["posts"][1]["type"] == "stop"
        # position row with the stop's broker id and the hard exit date
        pos = db.execute("SELECT ticker, stop_order_id, planned_exit_date, "
                         "status FROM positions").fetchone()
        assert pos[0] == "TEST" and pos[1] == "brok-2"
        assert pos[3] == "open"
        # decision + limits + raw events + candidate persisted
        assert db.execute("SELECT action FROM risk_decisions").fetchone()[0] == "trade"
        assert db.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM refusals").fetchone()[0] == 0
        # daily equity mark from the confirmed broker read
        snap = db.execute("SELECT day, equity_usd, source "
                          "FROM equity_snapshots").fetchone()
        assert snap == ("2026-08-10", "1000", "broker_read")

    def test_second_cycle_screens_out_researched_candidate(self, db):
        broker, _ = broker_for()
        run(db, broker, model_transport(), [candidate()])
        report2 = run(db, broker, model_transport(), [candidate()])
        assert report2.funnel["screened"] == 0
        assert any("already_researched" in r
                   for r in report2.drop_reasons["screened"])


class TestRefusalPath:
    def test_skip_records_refusal_with_price(self, db):
        broker, state = broker_for()
        view = {**GOOD_VIEW, "conviction": 0.2}
        report = run(db, broker, model_transport(view), [candidate()])
        assert report.funnel["proposed"] == 0
        assert state["posts"] == []             # no orders at all
        price = db.execute("SELECT price_at_refusal FROM refusals"
                           ).fetchone()[0]
        assert Decimal(price) == Decimal("50")  # NBBO mid at decision time
        assert any("below_conviction_floor" in r
                   for r in report.drop_reasons["proposed"])


class TestNoTransport:
    def test_no_model_key_is_named_not_silent(self, db):
        broker, state = broker_for()
        report = run(db, broker, None, [candidate()])
        assert report.funnel["researched"] == 0
        assert any("no_model_transport_configured" in r
                   for r in report.drop_reasons["researched"])
        assert state["posts"] == []


class TestMarketSnapshot:
    def test_spread_computed_from_nbbo(self):
        broker, _ = broker_for()
        snap = build_market_snapshot(broker, "TEST")
        assert snap.last_close == Decimal("50")
        assert snap.half_spread_bp == Decimal("10.0")

    def test_bad_quote_returns_none(self):
        def handler(request):
            return httpx.Response(200, json={"quote": {"bp": 0, "ap": 0}})

        b = Broker("k", "s", transport=httpx.MockTransport(handler),
                   backoff_s=0)
        assert build_market_snapshot(b, "TEST") is None


class TestPortfolioState:
    def test_built_from_broker_read(self, db):
        broker, _ = broker_for()
        p = build_portfolio_state(broker, db, NOW)
        assert p.reliable and p.equity_usd == Decimal("1000")
        assert p.open_positions == ()

    def test_broker_failure_returns_none(self, db):
        def handler(request):
            return httpx.Response(500, json={})

        b = Broker("k", "s", transport=httpx.MockTransport(handler),
                   backoff_s=0)
        assert build_portfolio_state(b, db, NOW) is None

    def test_in_cycle_exposure_carries_to_next_candidate(self, db):
        # two candidates, both trade: second sizing must see the first's
        # exposure (settled cash reduced), so total never exceeds bounds
        broker, state = broker_for()
        report = run(db, broker, model_transport(),
                     [candidate("c1", "AAA"), candidate("c2", "BBB")])
        assert report.funnel["orders_placed"] == 2
        buys = [p for p in state["posts"] if p["side"] == "buy"]
        total = sum(Decimal(b["qty"]) * Decimal("50") for b in buys)
        assert total <= Decimal("1000") * Decimal("0.90")


# ------------------------------------------------- risk-review regressions

def _seed_open_position(db, *, due=False, stop_id="brok-stop",
                        qty="2", filled=True):
    """An open position with a filled entry and known decision."""
    db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
               ("ord-buy", "cand-old", "b1", "buy", qty, "market", "day",
                "2026-08-01T14:00:00+00:00", "filled", "{}"))
    if filled:
        db.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                   ("ord-buy", "50.00", qty, "2026-08-01T14:00:00+00:00",
                    "50.00"))
    db.execute(
        "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("dec-old", "cand-old", "trade", "long", "100", qty, "45.00",
         "2026-08-20", "[]", "{}", "2026-08-01T13:00:00+00:00"))
    exit_date = "2026-08-09" if due else "2026-08-20"
    db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
               ("pos-old", "OLDPOS", json.dumps(["ord-buy"]), stop_id,
                "2026-08-01T14:00:00+00:00", exit_date, "open"))
    db.commit()


class TestKillTripProtection:
    def test_loss_trip_still_runs_exits_but_blocks_entries(self, db):
        """Risk review B2: a daily-loss trip must not abandon the book."""
        _seed_open_position(db, due=True, stop_id=None)
        state = {"posts": []}
        account = {**ACCOUNT, "last_equity": "1100"}  # -100 today > 4% kill

        def handler(request):
            url = str(request.url)
            if "/v2/account" in url:
                return httpx.Response(200, json=account)
            if request.method == "POST" and url.endswith("/v2/orders"):
                state["posts"].append(json.loads(request.content))
                return httpx.Response(200, json={"id": "x", "status": "accepted"})
            if "by_client_order_id" in url:
                return httpx.Response(200, json={"id": "b1", "status": "filled",
                                                 "filled_qty": "2",
                                                 "filled_avg_price": "50.00",
                                                 "filled_at": "2026-08-01T14:00:00Z"})
            if "/v2/positions" in url:
                return httpx.Response(200, json=[])
            if "/v2/orders" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={})

        b = Broker("k", "s", transport=httpx.MockTransport(handler), backoff_s=0)
        feed_called = {"n": 0}

        def feed(s, u):
            feed_called["n"] += 1
            return []

        report = run_cycle(db, b, None, feed, lambda e, a: [],
                           lambda c, o: {}, now=NOW)
        assert report.kill_switch.reason == "daily_loss_kill"
        assert feed_called["n"] == 0            # no discovery, no entries
        # but the due position's hard exit STILL went out
        sells = [p for p in state["posts"] if p["side"] == "sell"]
        assert len(sells) == 1 and sells[0]["type"] == "market"

    def test_unreliable_portfolio_is_full_standdown(self, db):
        """Cannot trust state -> touch nothing, not even exits."""
        _seed_open_position(db, due=True, stop_id=None)
        posts = {"n": 0}

        def handler(request):
            if "/v2/account" in str(request.url):
                return httpx.Response(500, json={})
            posts["n"] += 1
            return httpx.Response(200, json={})

        b = Broker("k", "s", transport=httpx.MockTransport(handler), backoff_s=0)
        report = run_cycle(db, b, None, lambda s, u: [], lambda e, a: [],
                           lambda c, o: {}, now=NOW)
        assert report.kill_switch.reason == "portfolio_state_unreliable"
        assert posts["n"] == 0


class TestMarketClosed:
    def test_closed_market_blocks_entries_not_protection(self, db):
        broker, state = broker_for()
        state["market_open"] = False
        report = run(db, broker, model_transport(), [candidate()])
        assert report.funnel["researched"] == 0
        assert any("market_closed" in r
                   for r in report.drop_reasons["researched"])
        assert state["posts"] == []


class TestStopRejection:
    def _broker_rejecting_stops(self):
        state = {"posts": [], "qty_by_id": {}}

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
                state["posts"].append(body)
                if body["type"] == "stop":
                    return httpx.Response(403, json={"message": "no can do"})
                broker_id = f"brok-{len(state['posts'])}"
                state["qty_by_id"][broker_id] = body["qty"]
                return httpx.Response(200, json={"id": broker_id,
                                                 "status": "accepted"})
            if request.method == "GET" and "/v2/orders/" in url:
                broker_id = url.rsplit("/", 1)[1]
                return httpx.Response(200, json={
                    "id": broker_id, "status": "filled",
                    "filled_qty": state["qty_by_id"].get(broker_id, "1"),
                    "filled_avg_price": "50.00"})
            if "/v2/positions" in url:
                return httpx.Response(200, json=[])
            if "/v2/orders" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={})

        return Broker("k", "s", transport=httpx.MockTransport(handler),
                      backoff_s=0), state

    def test_rejected_stop_recorded_null_and_blocks_more_entries(self, db):
        """Risk review B3: a silently-rejected stop must not read as
        protection, and an unprotected book takes no more entries."""
        broker, state = self._broker_rejecting_stops()
        report = run(db, broker, model_transport(),
                     [candidate("c1", "AAA"), candidate("c2", "BBB")])
        # first entry opened, stop rejected -> recorded honestly
        assert db.execute("SELECT stop_order_id FROM positions"
                          ).fetchone()[0] is None
        assert any("entry_open_but_stop_not_armed" in r
                   for r in report.drop_reasons["orders_placed"])
        # second candidate was NOT entered
        assert any("unprotected_position_blocks_entries" in r
                   for r in report.drop_reasons["researched"])
        buys = [p for p in state["posts"] if p["side"] == "buy"]
        assert len(buys) == 1


class TestPartialFill:
    def test_stop_covers_filled_qty_not_ordered(self, db):
        """Risk review B4: sizing ordered 4 shares, only 1 filled -> the
        protective stop must be for 1."""
        broker, state = broker_for()
        # override fill responses to a partial fill of 1
        real_qty = state["qty_by_id"]

        class PartialDict(dict):
            def get(self, k, d=None):
                return "1"

        state["qty_by_id"] = PartialDict()
        run(db, broker, model_transport(), [candidate()])
        stop = [p for p in state["posts"] if p["type"] == "stop"]
        assert len(stop) == 1
        assert stop[0]["qty"] == "1"


class TestReopenPersistsStopId:
    def test_reopened_stop_id_written_back(self, db):
        """Risk review B1: the re-armed stop's broker id must land in
        positions.stop_order_id, or every later exit works a stale id."""
        _seed_open_position(db, due=False, stop_id="expired-old")
        broker, state = broker_for()
        # no open orders at the broker -> position reads unprotected;
        # reopen_stops will POST a new stop (brok-1)
        run(db, broker, None, [], events=[])
        assert db.execute("SELECT stop_order_id FROM positions WHERE id='pos-old'"
                          ).fetchone()[0] == "brok-1"


class TestQuoteFreshness:
    def test_stale_quote_refused(self):
        def handler(request):
            return httpx.Response(200, json={"quote": {
                "bp": 49.95, "ap": 50.05,
                "t": "2026-08-08T19:59:59Z"}})   # Friday's book

        b = Broker("k", "s", transport=httpx.MockTransport(handler),
                   backoff_s=0)
        assert build_market_snapshot(b, "TEST", NOW) is None

    def test_fresh_quote_accepted(self):
        def handler(request):
            return httpx.Response(200, json={"quote": {
                "bp": 49.95, "ap": 50.05,
                "t": "2026-08-10T13:59:00Z"}})

        b = Broker("k", "s", transport=httpx.MockTransport(handler),
                   backoff_s=0)
        snap = build_market_snapshot(b, "TEST", NOW)
        assert snap is not None and snap.last_close == Decimal("50")


class TestVoidAndPeakRegressions:
    """Risk round 3 findings 4 and 5."""

    def test_done_for_day_zero_fill_is_voided(self, db):
        """Finding 4: done_for_day is terminal-unfilled too; missing it
        left a naked-stop position blocked forever, not 'one session'."""
        from catalyst.orchestrator.cycle import CycleReport, _void_dead_entries
        from catalyst.risk import KillSwitchState
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ord-buy", "cand-old", "b1", "buy", "2", "market", "day",
                    "2026-08-01T14:00:00+00:00", "done_for_day", "{}"))
        db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                   ("pos-old", "OLDPOS", json.dumps(["ord-buy"]), None,
                    "2026-08-01T14:00:00+00:00", "2026-08-20", "open"))
        db.commit()
        report = CycleReport("c", NOW, KillSwitchState(False, None))
        _void_dead_entries(db, report)
        assert db.execute("SELECT status FROM positions").fetchone()[0] == "void"

    def test_intraday_high_keeps_the_maximum(self, db):
        """Finding 5: a later, lower prior must never replace a higher
        recorded intraday high."""
        account = {"equity": "1000", "cash": "1000", "last_equity": "1000",
                   "non_marginable_buying_power": "1000"}

        def handler(request):
            url = str(request.url)
            if "/v2/account" in url:
                return httpx.Response(200, json=dict(account))
            if "/v2/clock" in url:
                return httpx.Response(200, json={"is_open": True})
            if "/v2/positions" in url:
                return httpx.Response(200, json=[])
            if "/v2/orders" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={})

        b = Broker("k", "s", transport=httpx.MockTransport(handler),
                   backoff_s=0)
        for equity in ("1100", "1050", "1040"):
            account["equity"] = equity
            run(db, b, None, [], events=[])
        high = db.execute("SELECT equity_usd FROM equity_snapshots "
                          "WHERE source='intraday_high'").fetchone()[0]
        assert Decimal(high) == Decimal("1100")
        latest = db.execute("SELECT equity_usd FROM equity_snapshots "
                            "WHERE source='broker_read'").fetchone()[0]
        assert Decimal(latest) == Decimal("1040")
