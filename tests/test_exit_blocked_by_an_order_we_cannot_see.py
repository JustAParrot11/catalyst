"""The exit fix worked and the exit still failed, 93 more times.

OWNER'S BUNDLE, 2026-08-30. The 2026-08-29 fix - cancel every stop the
BROKER lists, not only the id on the position row - shipped and was
running from 02:38. It changed nothing:

    93 x POST /v2/orders -> 403 "insufficient qty available for order
                                 (requested: 79.1295, available: 0)"
                                 held_for_orders: 79.1295
                                 related_orders: [68fc7415-...]
    93 x DELETE /v2/orders/ff142651-... -> 204   (the recorded stop id)
    93 x stop_confirmations: live_stop_order_ids [], status 'unprotected'

Read those three lines together and the defect is exact. Every pass
cancelled exactly one order - the stale recorded id - and both this
module and confirm_stops_resting agreed there was NO stop resting.
Alpaca held all 79.1295 shares anyway, and named the order doing it.

WHY. `GET /v2/orders?status=open&limit=500` did not list 68fc7415, and
the filter looked for `"stop" in type` on top of that. Two independent
reasons the pre-emptive cancel could not reach it, and the sell was
refused by an order our view of the account did not contain.

TWO FIXES, and they are different kinds of fix:

  1. THE CLASS IS WRONG. A resting sell reserves the quantity whatever
     its type - an accepted market sell or a stale limit holds the
     shares exactly as a stop does. Filtering on stops was classifying
     by enumeration (house rule 7). Now: any resting sell on the symbol.

  2. THE LISTING IS NOT THE TRUTH. Whatever the listing shows, the
     REJECTION named the blocking order explicitly. Reading
     `related_orders` and retrying once removes the dependence on our
     view of the account being complete - which is the part that was
     wrong and could be wrong again in a way nobody predicted.

AND ONE THING WIDENING THE CLASS MUST NOT BREAK. An exit submitted
outside market hours is ACCEPTED and rests until the open. Under rule 1
alone the next cycle would cancel it and submit another, every fifteen
minutes, and the position would never reach an opening auction. Our own
working exit is recognised by its client_order_id and left alone - but
a protective STOP is also a sell on the same decision_id, so the
carve-out is bounded to market orders.

HUMAN REVIEW REQUIRED (house rule 5): execution code.

Fully offline: the broker is a stub.
"""

from datetime import datetime, timezone

import pytest

from catalyst.execution.broker import BrokerError, OrderRejected

NOW = datetime(2026, 8, 30, 20, 41, tzinfo=timezone.utc)

DECISION = "insider_cluster-EMBC-2026-08-13-e0aa1df6061c"
DUE = {"id": "85fb5edc", "ticker": "EMBC", "qty": "79.1295",
       "decision_id": DECISION, "stop_order_id": "ff142651"}

#: Verbatim from the owner's orders table, 2026-08-30T20:41:24.
HELD = {
    "available": "0", "code": 40310000, "existing_qty": "79.1295",
    "held_for_orders": "79.1295",
    "message": "insufficient qty available for order (requested: 79.1295, "
               "available: 0)",
    "related_orders": ["68fc7415-042c-490d-88cf-80ca2b1cc743"],
    "symbol": "EMBC", "_http_status": 403,
}
GHOST = "68fc7415-042c-490d-88cf-80ca2b1cc743"


@pytest.fixture
def conn(tmp_path):
    import sqlite3
    from pathlib import Path

    schema = (Path(__file__).resolve().parents[1] / "catalyst" / "storage"
              / "schema.sql")
    c = sqlite3.connect(tmp_path / "t.db")
    c.executescript(schema.read_text())
    yield c
    c.close()


class Broker:
    """The account as it actually was: one order holding every share,
    and a listing that does not mention it.

    `listed` is what GET /v2/orders returns; `holding` is what the
    account really reserves. The gap between them IS the defect.
    """

    def __init__(self, *, listed=(), holding=(GHOST,), orders=None):
        self.listed = list(listed)
        self.holding = set(holding)
        self.orders = dict(orders or {})
        for oid in self.holding:
            self.orders.setdefault(
                oid, {"id": oid, "symbol": "EMBC", "side": "sell",
                      "type": "market", "status": "accepted"})
        for o in self.listed:
            self.orders.setdefault(str(o["id"]), dict(o, status="new"))
        self.cancelled = []
        self.submitted = []

    def get_open_orders(self):
        return [dict(o) for o in self.listed]

    def get_order(self, order_id):
        o = self.orders.get(str(order_id))
        if o is None:
            raise BrokerError("unknown order", status_code=404)
        return dict(o, status="canceled" if str(order_id) not in self.holding
                    and str(order_id) in self.cancelled else o["status"])

    def cancel_order(self, order_id):
        self.cancelled.append(str(order_id))
        if str(order_id) not in self.orders:
            raise BrokerError("unknown order", status_code=404)
        self.holding.discard(str(order_id))
        self.orders[str(order_id)] = dict(self.orders[str(order_id)],
                                          status="canceled")
        self.listed = [o for o in self.listed if str(o["id"]) != str(order_id)]

    def submit_order(self, **kw):
        self.submitted.append(kw)
        if self.holding:
            raise OrderRejected(403, dict(HELD))
        oid = f"sell-{len(self.submitted)}"
        self.orders[oid] = {"id": oid, "symbol": "EMBC", "side": "sell",
                            "type": "market", "status": "accepted",
                            "client_order_id": kw.get("client_order_id")}
        return dict(self.orders[oid])

    def get_order_by_client_id(self, client_order_id):
        for o in self.orders.values():
            if o.get("client_order_id") == client_order_id:
                return dict(o)
        raise BrokerError("unknown", status_code=404)


def run(broker, conn, pos=None):
    from catalyst.execution.exits import manage_exits

    return manage_exits([dict(pos or DUE)], NOW, broker, conn,
                        poll_attempts=1, poll_interval_s=0)


class TestTheOwnersCase:
    """One order holds the shares, the listing does not mention it, and
    the position row's stop id is stale. That is 2026-08-30 exactly."""

    def test_the_position_is_finally_sold(self, conn):
        b = Broker(listed=[], holding=(GHOST,))
        results = run(b, conn)
        assert results and results[-1].status != "rejected", (
            "the exit is still refused; this is the 93rd rejection in the "
            f"owner's bundle. Last raw: {results[-1].raw_response if results else None}")

    def test_the_order_the_broker_named_is_cancelled(self, conn):
        b = Broker(listed=[], holding=(GHOST,))
        run(b, conn)
        assert GHOST in b.cancelled, (
            "related_orders named the blocker and it was never cancelled")

    def test_it_retries_exactly_once_not_in_a_loop(self, conn):
        b = Broker(listed=[], holding=(GHOST,))
        run(b, conn)
        assert len(b.submitted) == 2, (
            f"{len(b.submitted)} sells submitted in one pass; one attempt, "
            "one retry after clearing what the broker named")

    def test_a_blocker_that_will_not_die_does_not_get_a_sell(self, conn):
        """If the named order cannot be confirmed gone, we are back to
        selling into something live. Refuse."""
        class Stubborn(Broker):
            def cancel_order(self, order_id):
                # Stubborn about the NAMED blocker only, so the sell
                # still reaches the broker and the retry is what is
                # under test rather than the pre-emptive loop.
                if str(order_id) == GHOST:
                    self.cancelled.append(str(order_id))
                    raise BrokerError("upstream down", status_code=503)
                return super().cancel_order(order_id)

        b = Stubborn(listed=[], holding=(GHOST,))
        run(b, conn)
        assert len(b.submitted) == 1, (
            "a second sell went out while the blocking order could still "
            "be live")


class TestAnyRestingSellBlocks:
    """Fix 1: the class is 'a resting sell', not 'a stop'."""

    def test_a_resting_limit_sell_is_cancelled(self, conn):
        limit = {"id": "limit-1", "symbol": "EMBC", "side": "sell",
                 "type": "limit", "qty": "79.1295"}
        b = Broker(listed=[limit], holding=("limit-1",))
        run(b, conn)
        assert "limit-1" in b.cancelled, (
            "a resting limit sell reserves the quantity exactly as a stop "
            "does, and the old filter could not see it")

    def test_a_resting_stop_is_still_cancelled(self, conn):
        stop = {"id": "stop-TODAY", "symbol": "EMBC", "side": "sell",
                "type": "stop", "qty": "79.1295"}
        b = Broker(listed=[stop], holding=("stop-TODAY",))
        run(b, conn)
        assert "stop-TODAY" in b.cancelled

    def test_a_buy_order_is_left_alone(self, conn):
        buy = {"id": "buy-1", "symbol": "EMBC", "side": "buy",
               "type": "limit", "qty": "5"}
        b = Broker(listed=[buy], holding=())
        run(b, conn)
        assert "buy-1" not in b.cancelled, (
            "a buy reserves cash, not shares, and cancelling it is a "
            "decision this function does not get to make")

    def test_another_symbols_orders_are_left_alone(self, conn):
        other = {"id": "stop-AAPL", "symbol": "AAPL", "side": "sell",
                 "type": "stop", "qty": "10"}
        b = Broker(listed=[other], holding=())
        run(b, conn)
        assert "stop-AAPL" not in b.cancelled, (
            "an unrelated position was left unprotected")


class TestOurOwnWorkingExitIsNotChurned:
    """The hazard fix 1 introduces, closed by name."""

    def _record_our_sell(self, conn, order_id, *, order_type="market"):
        conn.execute(
            "INSERT INTO orders (id, decision_id, broker_order_id, side, qty, "
            "order_type, time_in_force, submitted_at, status, raw_response) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (order_id, DECISION, "broker-" + order_id, "sell", "79.1295",
             order_type, "day", NOW.isoformat(), "accepted", "{}"))
        conn.commit()

    def test_a_resting_exit_of_ours_is_not_cancelled(self, conn):
        self._record_our_sell(conn, "local-exit-1")
        mine = {"id": "broker-local-exit-1", "symbol": "EMBC", "side": "sell",
                "type": "market", "qty": "79.1295",
                "client_order_id": "local-exit-1"}
        b = Broker(listed=[mine], holding=("broker-local-exit-1",))
        run(b, conn)
        assert b.cancelled == [], (
            "our own working exit was cancelled; every fifteen minutes it "
            "is replaced and never reaches an opening auction")

    def test_and_no_second_sell_is_submitted(self, conn):
        self._record_our_sell(conn, "local-exit-1")
        mine = {"id": "broker-local-exit-1", "symbol": "EMBC", "side": "sell",
                "type": "market", "qty": "79.1295",
                "client_order_id": "local-exit-1"}
        b = Broker(listed=[mine], holding=("broker-local-exit-1",))
        results = run(b, conn)
        assert b.submitted == []
        assert [r.status for r in results] == ["exit_already_working"]

    def test_the_result_names_the_order_so_it_can_be_chased(self, conn):
        self._record_our_sell(conn, "local-exit-1")
        mine = {"id": "broker-local-exit-1", "symbol": "EMBC", "side": "sell",
                "type": "market", "qty": "79.1295",
                "client_order_id": "local-exit-1"}
        b = Broker(listed=[mine], holding=("broker-local-exit-1",))
        results = run(b, conn)
        assert results[0].broker_order_id == "broker-local-exit-1"

    def test_our_own_STOP_is_still_cancelled(self, conn):
        """A stop carries the same decision_id and side. If the carve-out
        matched it, the exit would skip forever behind its own
        protection."""
        self._record_our_sell(conn, "local-stop-1", order_type="stop")
        stop = {"id": "broker-local-stop-1", "symbol": "EMBC", "side": "sell",
                "type": "stop", "qty": "79.1295",
                "client_order_id": "local-stop-1"}
        b = Broker(listed=[stop], holding=("broker-local-stop-1",))
        run(b, conn)
        assert "broker-local-stop-1" in b.cancelled
        assert b.submitted, "the position was never sold"

    def test_another_positions_exit_does_not_excuse_this_one(self, conn):
        """Same symbol is impossible for two open positions, but the
        match must be on OUR decision, not merely on any sell we ever
        placed."""
        self._record_our_sell(conn, "local-exit-9")
        conn.execute("UPDATE orders SET decision_id='someone-else' "
                     "WHERE id='local-exit-9'")
        conn.commit()
        theirs = {"id": "broker-local-exit-9", "symbol": "EMBC",
                  "side": "sell", "type": "market", "qty": "79.1295",
                  "client_order_id": "local-exit-9"}
        b = Broker(listed=[theirs], holding=("broker-local-exit-9",))
        run(b, conn)
        assert "broker-local-exit-9" in b.cancelled


class TestTheBlockerReaderIsBoundedAndHonest:
    def test_it_reads_the_ids_out_of_the_owners_rejection(self):
        from catalyst.execution.exits import _blocking_order_ids

        assert _blocking_order_ids(HELD) == [GHOST]

    def test_it_reads_them_through_the_resolved_wrapper_too(self):
        """_submit nests the original body under 'rejection' when it
        went back to the broker afterwards."""
        from catalyst.execution.exits import _blocking_order_ids

        assert _blocking_order_ids({"rejection": HELD,
                                    "resolved_by_client_order_id": {}}) == [GHOST]

    def test_a_rejection_with_no_related_orders_names_nothing(self):
        from catalyst.execution.exits import _blocking_order_ids

        assert _blocking_order_ids({"code": 40310000, "message": "no"}) == []

    def test_junk_does_not_raise(self):
        from catalyst.execution.exits import _blocking_order_ids

        for junk in (None, "text", 7, {"related_orders": "not-a-list"},
                     {"related_orders": [None, ""]}):
            assert _blocking_order_ids(junk) == []

    def test_a_named_order_on_another_symbol_is_never_cancelled(self, conn):
        """The id comes from a response body. It bounds what may be
        cancelled to sells of the symbol being exited."""
        b = Broker(listed=[], holding=(GHOST,),
                   orders={GHOST: {"id": GHOST, "symbol": "AAPL",
                                   "side": "sell", "type": "market",
                                   "status": "accepted"}})
        run(b, conn)
        assert GHOST not in b.cancelled, (
            "a rejection body was able to cancel an order on a different "
            "symbol")

    def test_a_named_BUY_is_never_cancelled(self, conn):
        b = Broker(listed=[], holding=(GHOST,),
                   orders={GHOST: {"id": GHOST, "symbol": "EMBC",
                                   "side": "buy", "type": "limit",
                                   "status": "accepted"}})
        run(b, conn)
        assert GHOST not in b.cancelled


class TestTheOldContractsStillHold:
    def test_an_unreachable_listing_still_fails_closed(self, conn):
        class Down(Broker):
            def get_open_orders(self):
                raise BrokerError("upstream down", status_code=503)

        b = Down()
        assert run(b, conn) == []
        assert b.submitted == []

    def test_a_clean_position_still_exits_in_one_submit(self, conn):
        b = Broker(listed=[], holding=())
        results = run(b, conn)
        assert len(b.submitted) == 1
        assert results[0].status != "rejected"


class TestTheCheckCanFail:
    """House rule 4, against the code that shipped on 2026-08-30."""

    def test_the_stop_only_filter_would_be_caught(self, conn):
        """Reproduce the shipped filter over the owner's account and
        confirm it finds nothing to cancel."""
        listing = [{"id": "limit-1", "symbol": "EMBC", "side": "sell",
                    "type": "limit", "qty": "79.1295"}]
        shipped = [o for o in listing if "stop" in str(o.get("type")).lower()]
        assert shipped == [], (
            "the old filter can see a limit sell, so it would not have "
            "missed the order holding EMBC")

    def test_without_the_retry_the_owners_case_still_fails(self, conn):
        """The listing-based path alone cannot reach the ghost order -
        which is why the retry exists and not merely the wider filter."""
        b = Broker(listed=[], holding=(GHOST,))
        resting = [o for o in b.get_open_orders()
                   if str(o.get("side")) == "sell"]
        assert resting == [], (
            "the listing does contain the blocker, so this test no longer "
            "reproduces the owner's account and proves nothing")
