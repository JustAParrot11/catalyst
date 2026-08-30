"""Execution layer tests - fully offline via httpx.MockTransport.

The transport seam is the same one the live Broker uses; nothing here
opens a socket (conftest enforces that regardless).
"""

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst.execution import broker as broker_mod
from catalyst.execution.broker import Broker, BrokerError, OrderRejected
from catalyst.execution.exits import manage_exits, reopen_stops
from catalyst.execution.orders import (
    confirm_stops_resting, place, place_stop, replace_stop,
)
from catalyst.execution.reconcile import reconcile
from catalyst.risk import RiskDecision


#: manage_exits now asks the broker which stops are actually resting
#: against the symbol, because the id on the position row is a cache the
#: design guarantees goes stale nightly (DAY stops expire; TRAPS.md).
#: For every fixture below the truthful answer is "none resting" - each
#: was written with no live stop, or with one the test intends to be
#: already gone - so answering the new route with an empty list keeps
#: each test asserting exactly what it asserted before.
_NO_RESTING_STOPS = httpx.Response(200, json=[])


def _is_open_orders_list(request) -> bool:
    """The LIST route (/v2/orders), not a single order (/v2/orders/{id})."""
    return (request.method == "GET"
            and request.url.path.rstrip("/").endswith("/v2/orders"))


def make_broker(handler) -> Broker:
    return Broker("test-key", "test-secret",
                  transport=httpx.MockTransport(handler), backoff_s=0)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.executescript(open("catalyst/storage/schema.sql").read())
    yield conn
    conn.close()


def decision(qty="2.5", action="trade", side="long"):
    return RiskDecision(
        candidate_id="cand-1", action=action, side=side,
        notional_usd=Decimal("125.00"), qty=Decimal(qty),
        stop_price=Decimal("45.00"),
        planned_exit_date=date(2026, 8, 22),
        limits_applied=(), skip_reasons=(), adaptive_params_snapshot={})


# ---------------------------------------------------------------- broker

class TestBroker:
    def test_5xx_retries_then_succeeds(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(500, json={"boom": True})
            return httpx.Response(200, json={"status": "ACTIVE"})

        b = make_broker(handler)
        assert b.get_account() == {"status": "ACTIVE"}
        assert calls["n"] == 3

    def test_4xx_never_retries(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(403, json={"message": "forbidden"})

        b = make_broker(handler)
        with pytest.raises(BrokerError) as e:
            b.get_account()
        assert calls["n"] == 1
        assert e.value.status_code == 403
        assert e.value.body == {"message": "forbidden"}

    def test_order_rejection_carries_verbatim_body(self):
        def handler(request):
            return httpx.Response(422, json={"message": "insufficient qty"})

        b = make_broker(handler)
        with pytest.raises(OrderRejected) as e:
            b.submit_order(symbol="TEST", qty="1", side="buy",
                           order_type="market", time_in_force="day",
                           client_order_id="x")
        assert e.value.body == {"message": "insufficient qty"}

    def test_repr_and_errors_never_contain_credentials(self):
        def handler(request):
            return httpx.Response(500, json={})

        b = make_broker(handler)
        assert "test-key" not in repr(b) and "test-secret" not in repr(b)
        with pytest.raises(BrokerError) as e:
            b.get_account()
        assert "test-key" not in str(e.value)
        assert "test-secret" not in str(e.value)

    def test_from_env_refuses_when_absent(self):
        # conftest strips ALPACA*/APCA* from the environment for every test
        with pytest.raises(BrokerError):
            Broker.from_env()

    def test_submit_sends_client_order_id(self):
        seen = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "b1", "status": "accepted"})

        b = make_broker(handler)
        b.submit_order(symbol="TEST", qty="1", side="buy",
                       order_type="market", time_in_force="day",
                       client_order_id="local-42")
        assert seen["client_order_id"] == "local-42"


# ----------------------------------------------------------------- place

class TestPlace:
    def test_accepted_order_recorded_with_raw_response(self, db):
        def handler(request):
            return httpx.Response(
                200, json={"id": "brok-1", "status": "accepted"})

        r = place(decision(), "TEST", make_broker(handler), db)
        assert r.status == "accepted" and r.broker_order_id == "brok-1"
        row = db.execute(
            "SELECT side, qty, order_type, time_in_force, status, raw_response "
            "FROM orders").fetchone()
        assert row[:5] == ("buy", "2.5", "market", "day", "accepted")
        assert json.loads(row[5])["id"] == "brok-1"

    def test_rejection_still_recorded_verbatim(self, db):
        def handler(request):
            return httpx.Response(403, json={"message": "account blocked"})

        r = place(decision(), "TEST", make_broker(handler), db)
        assert r.status == "rejected" and r.broker_order_id is None
        raw = json.loads(db.execute(
            "SELECT raw_response FROM orders").fetchone()[0])
        assert raw["message"] == "account blocked"
        assert raw["_http_status"] == 403

    def test_refuses_skip_decisions_and_missing_qty(self, db):
        b = make_broker(lambda r: httpx.Response(200, json={}))
        with pytest.raises(ValueError):
            place(decision(action="skip", side=None), "TEST", b, db)
        with pytest.raises(ValueError):
            bad = RiskDecision(
                candidate_id="c", action="trade", side="long",
                notional_usd=Decimal("10"), qty=None, stop_price=None,
                planned_exit_date=None, limits_applied=(), skip_reasons=(),
                adaptive_params_snapshot={})
            place(bad, "TEST", b, db)


# ---------------------------------------------------------- replace_stop

def position(stop="old-stop"):
    return {"id": "pos-1", "ticker": "TEST", "qty": "2.5",
            "decision_id": "cand-1", "stop_order_id": stop}


class TestReplaceStop:
    def test_confirmed_cancel_then_new_stop(self, db):
        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(204)
            if "orders/old-stop" in str(request.url):
                return httpx.Response(200, json={"id": "old-stop",
                                                 "status": "canceled"})
            return httpx.Response(200, json={"id": "new-stop",
                                             "status": "accepted"})

        r = replace_stop(position(), Decimal("46.00"), make_broker(handler),
                         db, poll_interval_s=0)
        assert r.status == "replaced"
        assert r.new_stop_order_id == "new-stop"
        assert db.execute("SELECT status FROM stop_replacements").fetchone()[0] == "replaced"

    def test_unconfirmed_cancel_places_nothing(self, db):
        posts = {"n": 0}

        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.method == "POST":
                posts["n"] += 1
                return httpx.Response(200, json={"id": "new"})
            return httpx.Response(200, json={"id": "old-stop",
                                             "status": "pending_cancel"})

        r = replace_stop(position(), Decimal("46.00"), make_broker(handler),
                         db, poll_attempts=2, poll_interval_s=0)
        assert r.status == "failed_cancel_unconfirmed"
        assert r.new_stop_order_id is None
        assert posts["n"] == 0  # THE invariant: no second live stop

    def test_stop_filled_during_cancel_places_nothing(self, db):
        posts = {"n": 0}

        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.method == "POST":
                posts["n"] += 1
                return httpx.Response(200, json={"id": "new"})
            return httpx.Response(200, json={"id": "old-stop",
                                             "status": "filled"})

        r = replace_stop(position(), Decimal("46.00"), make_broker(handler),
                         db, poll_interval_s=0)
        assert r.status == "failed_cancel_unconfirmed"
        assert posts["n"] == 0  # selling again would open a short


# ------------------------------------------------- confirm_stops_resting

class TestConfirmStops:
    def _run(self, db, open_orders):
        def handler(request):
            return httpx.Response(200, json=open_orders)

        return confirm_stops_resting(
            [{"id": "pos-1", "ticker": "TEST"}], make_broker(handler), db)

    def test_single_stop_ok(self, db):
        [c] = self._run(db, [{"id": "s1", "symbol": "TEST", "side": "sell",
                              "type": "stop"}])
        assert c.status == "ok" and c.live_stop_order_ids == ("s1",)

    def test_no_stop_unprotected(self, db):
        [c] = self._run(db, [])
        assert c.status == "unprotected"
        assert db.execute(
            "SELECT status FROM stop_confirmations").fetchone()[0] == "unprotected"

    def test_two_stops_duplicate(self, db):
        [c] = self._run(db, [
            {"id": "s1", "symbol": "TEST", "side": "sell", "type": "stop"},
            {"id": "s2", "symbol": "TEST", "side": "sell", "type": "stop"}])
        assert c.status == "duplicate_stops"

    def test_buy_orders_and_other_symbols_ignored(self, db):
        [c] = self._run(db, [
            {"id": "s1", "symbol": "OTHER", "side": "sell", "type": "stop"},
            {"id": "s2", "symbol": "TEST", "side": "buy", "type": "stop"},
            {"id": "s3", "symbol": "TEST", "side": "sell", "type": "market"}])
        assert c.status == "unprotected"


# ------------------------------------------------------------- reconcile

def _seed_order(db, order_id="ord-1", status="accepted"):
    db.execute(
        """INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (order_id, "cand-1", "brok-1", "buy", "2.5", "market", "day",
         datetime.now(timezone.utc).isoformat(), status, "{}"))
    db.commit()


class TestReconcile:
    def test_fill_recorded_at_broker_price_once(self, db):
        _seed_order(db)

        def handler(request):
            return httpx.Response(200, json={
                "id": "brok-1", "status": "filled", "filled_qty": "2.5",
                "filled_avg_price": "49.87",
                "filled_at": "2026-08-10T14:31:00Z"})

        b = make_broker(handler)
        fills = reconcile(b, db)
        assert len(fills) == 1
        assert fills[0].broker_reported_price == Decimal("49.87")
        assert reconcile(b, db) == []  # idempotent second pass
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
        assert db.execute("SELECT status FROM orders").fetchone()[0] == "filled"
        # modeled slippage column exists BESIDE the broker price, not
        # instead of it (TRAPS.md)
        assert db.execute(
            "SELECT broker_reported_price, modeled_slippage FROM fills"
        ).fetchone() == ("49.87", None)

    def test_unknown_order_404_needs_two_passes_to_terminalize(self, db):
        """One transient 404 must NOT terminalize an order (risk round 3
        finding 1: a single flaky lookup rippled into voiding a live
        position). The first pass parks it; the second confirms."""
        _seed_order(db)

        def handler(request):
            return httpx.Response(404, json={"message": "order not found"})

        b = make_broker(handler)
        assert reconcile(b, db) == []
        status, raw = db.execute(
            "SELECT status, raw_response FROM orders").fetchone()
        assert status == "reconcile_404_once"     # NOT terminal yet
        raw = json.loads(raw)
        assert raw["reconcile_404"] is True and raw["confirmed"] is False
        assert raw["body"] == {"message": "order not found"}

        assert reconcile(b, db) == []             # second consecutive 404
        status, raw = db.execute(
            "SELECT status, raw_response FROM orders").fetchone()
        assert status == "rejected"
        assert json.loads(raw)["confirmed"] is True

    def test_transient_404_recovers_on_next_pass(self, db):
        _seed_order(db)
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] <= 2:   # client-id lookup AND broker-id check
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={
                "id": "brok-1", "status": "filled", "filled_qty": "2.5",
                "filled_avg_price": "49.87",
                "filled_at": "2026-08-10T14:31:00Z"})

        b = make_broker(handler)
        reconcile(b, db)          # transient 404 -> parked
        fills = reconcile(b, db)  # broker answers now -> fill recorded
        assert len(fills) == 1
        assert db.execute("SELECT status FROM orders").fetchone()[0] == "filled"

    def test_terminal_orders_not_requeried(self, db):
        _seed_order(db, status="filled")
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(200, json={})

        reconcile(make_broker(handler), db)
        assert calls["n"] == 0


# ----------------------------------------------------------------- exits

class TestExits:
    def test_due_position_cancels_stop_then_sells(self, db):
        order_log = []

        def handler(request):
            if _is_open_orders_list(request):
                return _NO_RESTING_STOPS
            if request.method == "DELETE":
                order_log.append("cancel")
                return httpx.Response(204)
            if request.method == "POST":
                order_log.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "exit-1",
                                                 "status": "accepted"})
            return httpx.Response(200, json={"id": "old-stop",
                                             "status": "canceled"})

        [r] = manage_exits([position()], datetime.now(timezone.utc),
                           make_broker(handler), db, poll_interval_s=0)
        assert r.status == "accepted"
        assert order_log[0] == "cancel"          # stop cancelled FIRST
        sell = order_log[1]
        assert (sell["side"], sell["type"], sell["qty"]) == ("sell", "market", "2.5")

    def test_unconfirmed_stop_cancel_skips_the_sell(self, db):
        posts = {"n": 0}

        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.method == "POST":
                posts["n"] += 1
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"status": "pending_cancel"})

        out = manage_exits([position()], datetime.now(timezone.utc),
                           make_broker(handler), db, poll_attempts=2,
                           poll_interval_s=0)
        assert out == [] and posts["n"] == 0

    def test_position_without_stop_sells_directly(self, db):
        def handler(request):
            if _is_open_orders_list(request):
                return _NO_RESTING_STOPS
            assert request.method == "POST"
            return httpx.Response(200, json={"id": "exit-1",
                                             "status": "accepted"})

        [r] = manage_exits([position(stop=None)], datetime.now(timezone.utc),
                           make_broker(handler), db)
        assert r.status == "accepted"

    def test_reopen_stops_places_day_stop(self, db):
        seen = {}

        def handler(request):
            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "s-new",
                                             "status": "accepted"})

        [r] = reopen_stops([{"ticker": "TEST", "qty": "2.5",
                             "decision_id": "cand-1", "stop_price": "45.00"}],
                           make_broker(handler), db)
        assert r.status == "accepted"
        assert (seen["type"], seen["time_in_force"],
                seen["stop_price"]) == ("stop", "day", "45.00")


# ---------------------------------------------------- position settlement

from catalyst.execution.reconcile import close_filled_positions  # noqa: E402


def _seed_trade(db, *, pos_id="pos-1", entry_price="50.00", qty="2",
                sells=()):
    """A filled entry plus optional sell fills. sells: (price, qty, type)."""
    db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
               ("ord-buy", "cand-1", "b1", "buy", qty, "market", "day",
                "2026-08-01T14:00:00+00:00", "filled", "{}"))
    db.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
               ("ord-buy", entry_price, qty, "2026-08-01T14:00:00+00:00",
                entry_price))
    db.execute(
        "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("dec-1", "cand-1", "trade", "long", "100", qty, "45.00",
         "2026-08-13", "[]", "{}", "2026-08-01T13:00:00+00:00"))
    for i, (price, sqty, otype) in enumerate(sells):
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (f"ord-sell-{i}", "cand-1", f"s{i}", "sell", sqty, otype,
                    "day", "2026-08-09T14:00:00+00:00", "filled", "{}"))
        db.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                   (f"ord-sell-{i}", price, sqty,
                    "2026-08-09T14:30:00+00:00", price))
    db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
               (pos_id, "TEST", json.dumps(["ord-buy"]), None,
                "2026-08-01T14:00:00+00:00", "2026-08-13", "open"))
    db.commit()


class TestClosePositions:
    def test_full_exit_closes_with_pnl_and_reason(self, db):
        _seed_trade(db, sells=[("55.00", "2", "market")])
        assert close_filled_positions(db) == 1
        row = db.execute(
            "SELECT entry_price, exit_price, exit_reason, "
            "realized_pnl_cents, actual_holding_days, account_mode "
            "FROM closed_trades").fetchone()
        assert row[0] == "50.00" and Decimal(row[1]) == Decimal("55")
        assert row[2] == "hard_exit"
        assert row[3] == 1000            # (55-50)*2 = $10 = 1000 cents
        assert row[4] == 8
        assert row[5] == "paper"
        assert db.execute("SELECT status FROM positions").fetchone()[0] == "closed"

    def test_stop_fill_labeled_stop(self, db):
        _seed_trade(db, sells=[("45.00", "2", "stop")])
        close_filled_positions(db)
        assert db.execute("SELECT exit_reason FROM closed_trades"
                          ).fetchone()[0] == "stop"
        assert db.execute("SELECT realized_pnl_cents FROM closed_trades"
                          ).fetchone()[0] == -1000

    def test_partial_exit_stays_open(self, db):
        _seed_trade(db, sells=[("55.00", "1", "market")])
        assert close_filled_positions(db) == 0
        assert db.execute("SELECT status FROM positions").fetchone()[0] == "open"
        assert db.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0] == 0

    def test_unfilled_entry_never_closes(self, db):
        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ord-buy", "cand-1", "b1", "buy", "2", "market", "day",
                    "2026-08-01T14:00:00+00:00", "accepted", "{}"))
        db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                   ("pos-1", "TEST", json.dumps(["ord-buy"]), None,
                    "2026-08-01T14:00:00+00:00", "2026-08-13", "open"))
        db.commit()
        assert close_filled_positions(db) == 0


class TestExitsStaleStop:
    def test_expired_stop_cancel_422_still_exits(self, db):
        """Risk review B1: a DAY stop expired overnight; cancelling it
        422s and the status reads 'expired' - the hard exit must still
        go out instead of crashing the cycle forever."""
        order_log = []

        def handler(request):
            if _is_open_orders_list(request):
                return _NO_RESTING_STOPS
            if request.method == "DELETE":
                return httpx.Response(422, json={"message": "order is not cancelable"})
            if request.method == "POST":
                order_log.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "exit-1",
                                                 "status": "accepted"})
            return httpx.Response(200, json={"id": "old-stop",
                                             "status": "expired"})

        [r] = manage_exits([position()], datetime.now(timezone.utc),
                           make_broker(handler), db, poll_interval_s=0)
        assert r.status == "accepted"
        assert order_log[0]["type"] == "market"

    def test_broker_unknown_stop_404_still_exits(self, db):
        def handler(request):
            if _is_open_orders_list(request):
                return _NO_RESTING_STOPS
            if request.method == "DELETE":
                return httpx.Response(404, json={"message": "order not found"})
            if request.method == "POST":
                return httpx.Response(200, json={"id": "exit-1",
                                                 "status": "accepted"})
            return httpx.Response(404, json={"message": "order not found"})

        [r] = manage_exits([position()], datetime.now(timezone.utc),
                           make_broker(handler), db, poll_interval_s=0)
        assert r.status == "accepted"

    def test_broker_5xx_on_cancel_fails_closed(self, db):
        posts = {"n": 0}

        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(500, json={})
            if request.method == "POST":
                posts["n"] += 1
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"status": "new"})

        out = manage_exits([position()], datetime.now(timezone.utc),
                           make_broker(handler), db, poll_interval_s=0)
        assert out == [] and posts["n"] == 0

    def test_replace_stop_tolerates_expired_old_stop(self, db):
        def handler(request):
            if request.method == "DELETE":
                return httpx.Response(422, json={"message": "not cancelable"})
            if request.method == "POST":
                return httpx.Response(200, json={"id": "new-stop",
                                                 "status": "accepted"})
            return httpx.Response(200, json={"id": "old-stop",
                                             "status": "expired"})

        r = replace_stop(position(), Decimal("46.00"), make_broker(handler),
                         db, poll_interval_s=0)
        assert r.status == "replaced" and r.new_stop_order_id == "new-stop"


class TestReconcilePartialGrowth:
    def test_partial_fill_growth_updates_row(self, db):
        _seed_order(db)
        stage = {"qty": "1.0"}

        def handler(request):
            return httpx.Response(200, json={
                "id": "brok-1", "status": "partially_filled",
                "filled_qty": stage["qty"], "filled_avg_price": "49.90",
                "filled_at": "2026-08-10T14:31:00Z"})

        b = make_broker(handler)
        reconcile(b, db)
        assert db.execute("SELECT qty FROM fills").fetchone()[0] == "1.0"
        stage["qty"] = "2.5"
        reconcile(b, db)
        row = db.execute("SELECT qty FROM fills").fetchone()
        assert row[0] == "2.5"     # grew, not frozen at first observation
        assert db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Rate limiting: a 429 is not "your request is wrong"
# ---------------------------------------------------------------------------


class TestRateLimitedByTheBroker:
    """Owner-asked 2026-08-11: "are we adhering to all API limits, i dont
    want us to get IP banned."

    The blanket "never retry a 4xx" rule is right for a request that is
    wrong by construction. A 429 is the opposite: the request is correct
    and the answer is "wait". Failing fast on one aborted the whole
    cycle - on a stop-placement call that is worse than waiting.
    """

    def test_a_429_is_retried_and_then_succeeds(self):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            if len(seen) < 3:
                return httpx.Response(429, json={"message": "rate limit"})
            return httpx.Response(200, json={"id": "acct", "buying_power": "1000"})

        account = make_broker(handler).get_account()
        assert account["id"] == "acct"
        assert len(seen) == 3, "a 429 was not retried"

    def test_a_persistent_429_raises_rather_than_looping_forever(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(429, json={"message": "slow down"})

        with pytest.raises(BrokerError) as exc:
            make_broker(handler).get_account()
        assert exc.value.status_code == 429
        assert len(calls) == 4, "retries are unbounded"

    def test_a_real_4xx_still_never_retries(self):
        """The rule this sits beside, so the exception cannot quietly
        widen into 'retry everything'."""
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(422, json={"message": "bad symbol"})

        with pytest.raises(BrokerError):
            make_broker(handler).get_account()
        assert len(calls) == 1, "a 4xx was retried, spending the rate budget"

    def test_retry_after_is_honoured_but_bounded(self):
        """A header is upstream input. An unbounded sleep on it would let
        a mistaken 'Retry-After: 86400' park the trading loop for a day
        with open positions unattended."""
        from catalyst.execution.broker import (
            _MAX_RETRY_AFTER_S, _retry_after_seconds,
        )

        class R:
            def __init__(self, value):
                self.headers = {"Retry-After": value} if value is not None else {}

        assert _retry_after_seconds(R("5"), 1.0) == 5.0
        assert _retry_after_seconds(R("86400"), 1.0) == _MAX_RETRY_AFTER_S
        # absent, unparseable, or zero all fall back to our own backoff -
        # never to a zero-length wait
        assert _retry_after_seconds(R(None), 2.5) == 2.5
        assert _retry_after_seconds(R("soon"), 2.5) == 2.5
        assert _retry_after_seconds(R("0"), 2.5) == 2.5
