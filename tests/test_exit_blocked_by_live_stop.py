"""The bot could not close its only position, and said nothing.

OWNER'S LOG, 2026-08-26 to 2026-08-30. The hard exit date for EMBC
arrived on the 29th, and from 00:05 that day to 01:10 the next:

    99 x "Hard exit date reached for EMBC - selling at market."
   102 x POST /v2/orders  ->  HTTP 403 Forbidden
     0 x any WARNING or ERROR
    99 x "Cycle done ... No problems recorded."

Twenty-five hours of failed exits, once every fifteen minutes, with the
cycle reporting no problems each time.

DEFECT A - THE EXIT CANCELS THE WRONG STOP.

`manage_exits` neutralizes the ONE stop id recorded on the position row.
Fractional stops are DAY-only and expire every night (TRAPS.md), so
`reopen_stops` re-places them each session under a NEW broker id - and
the loop in cycle.py that writes the new id back to the position row
begins:

    for p in open_rows:
        if p["due"] or p["id"] in pending:
            continue

A DUE position is skipped by that loop. So on the one day the id matters
most, it is the stale one. `_neutralize_stop` 404s on it and correctly
returns 'gone' (risk review B1: a purged id must not brick the exit
forever) - the sell proceeds - and the LIVE stop, under its new id, is
still reserving every share. Alpaca answers 403.

The recorded id is a cache that the design guarantees goes stale
nightly. The broker's open orders are the truth.

DEFECT B - A REJECTED EXIT IS SILENT.

`_submit` catches OrderRejected and records status='rejected' with the
raw body, which is right - the row is the evidence. But it does not
raise, so `except BrokerError` in the cycle never fires, report.errors
stays empty, and the pass logs "No problems recorded". An exit that
failed 99 times has to reach a person.

MONEY-CRITICAL (house rule 5): execution code. Landing it needs
risk-reviewer's read, a sabotaged copy going red, and the full
suite green - not the owner's sign-off, which they removed on
2026-08-31.

Fully offline: the broker is a stub.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from catalyst.execution.broker import BrokerError, OrderRejected

NOW = datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc)

#: The position as cycle.py hands it over, carrying the id written when
#: the stop was FIRST placed.
DUE = {"id": "85fb5edc", "ticker": "EMBC", "qty": "79.1295",
       "decision_id": "insider_cluster-EMBC-2026-08-13-e0aa1df6061c",
       "stop_order_id": "stop-MONDAY"}

#: What Alpaca actually answers when a resting order holds the shares.
INSUFFICIENT = {
    "code": 40310000,
    "message": "insufficient qty available for order (requested: 79.1295, "
               "available: 0)",
}


class Stub:
    """A broker where yesterday's stop id is purged and today's is live."""

    def __init__(self, *, live_stop_ids=("stop-TODAY",), sell_succeeds=False):
        self.live = set(live_stop_ids)
        self.sell_succeeds = sell_succeeds
        self.cancelled = []
        self.submitted = []

    # --- the stop side -------------------------------------------------
    def get_open_orders(self):
        return [{"id": i, "symbol": "EMBC", "side": "sell",
                 "type": "stop", "qty": "79.1295", "status": "new"}
                for i in sorted(self.live)]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        if order_id not in self.live:
            raise BrokerError("unknown order", status_code=404)
        self.live.discard(order_id)

    def get_order(self, order_id):
        if order_id in self.live:
            return {"id": order_id, "status": "new"}
        raise BrokerError("unknown order", status_code=404)

    # --- the sell side -------------------------------------------------
    def submit_order(self, **kw):
        self.submitted.append(kw)
        if not self.sell_succeeds and self.live:
            # Shares are reserved by the resting stop.
            raise OrderRejected(403, INSUFFICIENT)
        return {"id": "sell-1", "status": "accepted"}

    def get_order_by_client_id(self, client_order_id):
        raise BrokerError("unknown", status_code=404)


@pytest.fixture
def conn(tmp_path):
    """Plain schema load, matching tests/test_execution.py - the exit
    path is what is under test, not referential integrity."""
    import sqlite3
    from pathlib import Path

    schema = Path(__file__).resolve().parents[1] / "catalyst" / "storage" / "schema.sql"
    c = sqlite3.connect(tmp_path / "t.db")
    c.executescript(schema.read_text())
    yield c
    c.close()


def run(broker, conn, pos=None):
    from catalyst.execution.exits import manage_exits

    return manage_exits([dict(pos or DUE)], NOW, broker, conn,
                        poll_attempts=1, poll_interval_s=0)


class TestDefectA_TheExitMustClearEveryLiveStop:
    def test_the_owners_case_the_sell_is_not_rejected(self, conn):
        """Yesterday's id is purged; today's stop is live under a new
        id. This is the exact shape that failed 99 times."""
        b = Stub(live_stop_ids=("stop-TODAY",))
        run(b, conn)
        assert b.submitted, "no sell was attempted at all"
        rejected = [o for o in b.submitted]
        assert not b.live, (
            "the live stop was left resting, so the market sell hits "
            "'insufficient qty available' - the 403 in the owner's log")

    def test_it_cancels_the_live_stop_not_only_the_recorded_one(self, conn):
        b = Stub(live_stop_ids=("stop-TODAY",))
        run(b, conn)
        assert "stop-TODAY" in b.cancelled, (
            "only the stale recorded id was cancelled; the recorded id is "
            "a cache the design guarantees goes stale nightly")

    def test_several_live_stops_are_all_cleared(self, conn):
        """A duplicate-stops position that becomes due must not leave
        one behind."""
        b = Stub(live_stop_ids=("stop-TODAY", "stop-EXTRA"))
        run(b, conn)
        assert not b.live

    def test_a_stop_that_cannot_be_confirmed_still_blocks_the_sell(self, conn):
        """The existing safety contract, unchanged: never sell into a
        possibly-live stop."""
        class Stubborn(Stub):
            def cancel_order(self, order_id):
                self.cancelled.append(order_id)
                raise BrokerError("upstream down", status_code=503)

        b = Stubborn(live_stop_ids=("stop-TODAY",))
        run(b, conn)
        assert not b.submitted, (
            "a market sell went out while a stop could still fire - that "
            "is the double-sell this module exists to prevent")

    def test_no_live_stop_at_all_still_sells(self, conn):
        b = Stub(live_stop_ids=())
        run(b, conn)
        assert b.submitted, "a clean position must still exit"

    def test_only_this_symbols_stops_are_touched(self, conn):
        """Another position's protection must not be cancelled."""
        class TwoSymbols(Stub):
            def get_open_orders(self):
                return [
                    {"id": "stop-TODAY", "symbol": "EMBC", "side": "sell",
                     "type": "stop", "qty": "79.1295", "status": "new"},
                    {"id": "stop-OTHER", "symbol": "AAPL", "side": "sell",
                     "type": "stop", "qty": "10", "status": "new"},
                ]

        b = TwoSymbols(live_stop_ids=("stop-TODAY", "stop-OTHER"))
        run(b, conn)
        assert "stop-OTHER" not in b.cancelled, (
            "an unrelated position was left unprotected")


class TestDefectB_ARejectedExitMustReachAPerson:
    def test_the_result_says_it_was_rejected(self, conn):
        b = Stub(live_stop_ids=())
        b.sell_succeeds = False
        b.live = {"ghost"}          # force the rejection path
        b.get_open_orders = lambda: []   # nothing to cancel, so it sells
        results = run(b, conn)
        assert results, "no OrderResult was returned at all"
        assert any(r.status == "rejected" for r in results), (
            "a 403 was recorded as something other than rejected")

    def test_the_raw_upstream_reason_is_kept(self, conn):
        """House rule 3: the raw upstream body beside the failure."""
        b = Stub(live_stop_ids=())
        b.live = {"ghost"}
        b.get_open_orders = lambda: []
        run(b, conn)
        raw = conn.execute(
            "SELECT raw_response FROM orders ORDER BY submitted_at DESC "
            "LIMIT 1").fetchone()
        assert raw and "insufficient qty" in str(raw[0])
