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
def stub_prompts(monkeypatch):
    monkeypatch.setattr(prompts, "render_research_prompt",
                        lambda c, graph_context=None: "research")
    monkeypatch.setattr(prompts, "exploration_tools", lambda: [])


ACCOUNT = {"equity": "1000", "cash": "1000", "last_equity": "1000",
           "non_marginable_buying_power": "1000"}
QUOTE = {"quote": {"bp": 49.95, "ap": 50.05}}   # mid 50, half-spread 10bp


def broker_for(state=None):
    """MockTransport broker: healthy account, tight quote, orders accepted."""
    state = state if state is not None else {}
    state.setdefault("posts", [])

    def handler(request):
        url = str(request.url)
        if "/v2/account" in url:
            return httpx.Response(200, json=dict(ACCOUNT))
        if "/quotes/latest" in url:
            return httpx.Response(200, json=dict(QUOTE))
        if request.method == "POST" and url.endswith("/v2/orders"):
            body = json.loads(request.content)
            state["posts"].append(body)
            return httpx.Response(200, json={
                "id": f"brok-{len(state['posts'])}", "status": "accepted"})
        if "by_client_order_id" in url:
            return httpx.Response(200, json={
                "id": "brok-x", "status": "filled", "filled_qty": "1",
                "filled_avg_price": "50.00",
                "filled_at": "2026-08-10T13:31:00Z"})
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
