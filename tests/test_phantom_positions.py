"""A position holding nothing must not stop the bot trading forever.

ESCALATION-7. An entry order that is accepted and then never fills
leaves a positions row with no fill record. Before this fix, three
things followed from that, all of them wrong:

  1. `_open_position_dicts` fell back to the ORDERED quantity, so the
     bot believed it held shares it had never bought.
  2. Every cycle armed a protective SELL stop for those shares. Had one
     ever triggered it would have sold stock that did not exist,
     leaving a short in a cash account that cannot hold one.
  3. The phantom could never be confirmed protected, and an unprotected
     position blocks EVERY new entry - so one order that quietly failed
     to fill stopped the bot trading indefinitely, with no alarm.

The third is the expensive one. The risk the block existed to prevent
was one unprotected position; the cost it actually imposed was every
future trade. This file exists to keep both halves fixed at once: no
stop for shares nobody owns, AND no permanent block from a position
that holds nothing.

THE TEST FOR "HOLDS NOTHING" IS DELIBERATELY TWO-SIDED. A missing local
fill row is not on its own proof of zero shares - if reconcile failed,
the stock can be real and merely unrecorded, and skipping the stop
there would leave a genuine holding naked. So the broker must also
report holding none of that ticker. Where the broker will not answer,
nothing changes at all: the unsafe direction is assuming an emptiness
that has not been confirmed.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from catalyst.execution.broker import BrokerError
from catalyst.orchestrator import cycle as cyc

NOW = datetime(2026, 8, 15, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "c.db")
    root = Path(__file__).resolve().parents[1]
    conn.executescript((root / "catalyst/storage/schema.sql").read_text())
    return conn


def seed_position(conn, *, pos_id="pos-1", ticker="TEST", filled=False,
                  exit_date="2026-08-20", stop_order_id=None):
    """One open position, with or without a recorded entry fill."""
    conn.execute(
        "INSERT INTO risk_decisions (id, candidate_id, action, notional_usd, "
        "qty, stop_price, planned_exit_date, decided_at, skip_reasons, "
        "adaptive_params_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"dec-{pos_id}", f"cand-{pos_id}", "trade", "200.00", "4", "45.00",
         exit_date, NOW.isoformat(), "[]", "{}"))
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"ord-{pos_id}", f"cand-{pos_id}", f"brok-{pos_id}", "buy", "4",
         "market", "day", NOW.isoformat(), "accepted", "{}"))
    if filled:
        conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                     (f"ord-{pos_id}", "50.00", "4", NOW.isoformat(), "50.00"))
    conn.execute(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
        (pos_id, ticker, json.dumps([f"ord-{pos_id}"]), stop_order_id,
         NOW.isoformat(), exit_date, "open"))
    conn.commit()


class FakeBroker:
    """Records every order it is asked to place, and says what it holds."""

    def __init__(self, holds=(), positions_raise=False, reject_stops=False):
        self.holds = list(holds)
        self.positions_raise = positions_raise
        self.reject_stops = reject_stops
        self.placed = []
        self.cancelled = []
        self.known = set()

    def get_positions(self):
        if self.positions_raise:
            raise BrokerError("the broker will not say what it holds")
        return self.holds

    def get_open_orders(self):
        return []

    def get_account(self):
        return {"equity": "1000", "cash": "1000", "last_equity": "1000"}

    def get_clock(self):
        return {"is_open": True}

    def submit_order(self, **kw):
        self.placed.append(kw)
        if not (self.reject_stops and kw.get("order_type") == "stop"):
            # Only orders the broker ACCEPTED become findable by their
            # client id. This matters: `_submit` re-asks the broker
            # after any failure, because a network error can hide an
            # order that really landed. A fake that answers for every id
            # ever invented resolves its own rejections into successes.
            self.known.add(kw.get("client_order_id"))
        if self.reject_stops and kw.get("order_type") == "stop":
            # 403, not a bare error: a 4xx is a DEFINITIVE refusal, so
            # the position is left genuinely unprotected. A bare
            # BrokerError is ambiguous - the order may still have
            # reached the broker - and is correctly recorded rather
            # than discarded, which would not test blocking at all.
            raise BrokerError("the broker refused the stop", 403)
        return {"id": f"b{len(self.placed)}", "status": "accepted"}

    def get_latest_quote(self, symbol):
        return {"bp": "49.90", "ap": "50.10"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def get_order(self, order_id):
        return {"id": order_id, "status": "accepted", "filled_qty": "0"}

    def get_order_by_client_id(self, client_order_id):
        """Still live, still unfilled - the ESCALATION-7 shape exactly.
        Not terminal, so `_void_dead_entries` correctly leaves it alone
        and the phantom reaches the logic under test here.

        An id the broker never accepted does not exist, which is what
        makes a refused stop stay refused."""
        if client_order_id.startswith("ord-") \
                or client_order_id in self.known:
            return {"id": client_order_id, "status": "accepted",
                    "filled_qty": "0"}
        raise BrokerError("order not found", 404)


def duties(conn, broker, now=NOW):
    report = cyc.CycleReport(cycle_id="c1", started_at=now, kill_switch=None)
    ok, rows = cyc._protective_duties(conn, broker, report, now)
    return ok, rows, report


class TestNoStopForSharesNobodyOwns:
    def test_an_unfilled_entry_arms_no_stop(self, db):
        seed_position(db, filled=False)
        broker = FakeBroker(holds=[])          # broker agrees: nothing held
        duties(db, broker)
        assert [o for o in broker.placed if o.get("side") == "sell"] == [], \
            "a sell stop was armed for shares that were never bought"

    def test_a_confirmed_holding_is_still_protected(self, db):
        """The safety property this must not break. No local fill row but
        the broker DOES hold the stock - the shares are real, merely
        unrecorded, and they must still get a stop."""
        seed_position(db, filled=False)
        broker = FakeBroker(holds=[{"symbol": "TEST", "qty": "4"}])
        _, rows, _ = duties(db, broker)
        assert cyc._pending_entry_ids(rows, {"TEST"}) == set(), \
            "a position the broker confirms holding was treated as empty"


class TestItDoesNotBlockTheBotForever:
    """The half that costs money. This is the whole point of the fix."""

    def test_a_phantom_does_not_block_new_entries(self, db):
        """THE EXPENSIVE DEFECT, stated as a test.

        The broker holds nothing and refuses the stop, which is what a
        real broker does when asked to sell shares that do not exist -
        there is nothing to sell. Before the fix that refusal left the
        phantom permanently unprotected, and an unprotected position
        blocks every entry, so the bot stopped trading for good.

        With the fix no stop is attempted at all, so there is nothing to
        refuse and nothing to block on.
        """
        seed_position(db, filled=False)
        broker = FakeBroker(holds=[], reject_stops=True)
        ok, _, _ = duties(db, broker)
        assert ok, ("a position holding nothing was counted as unprotected, "
                    "which blocks every future entry the bot would make")

    def test_a_real_unprotected_position_STILL_blocks(self, db):
        """The guard must keep working where it was right. A confirmed
        holding with no resting stop is genuine unprotected exposure."""
        seed_position(db, filled=True)
        # The broker refuses the stop, so the re-arm cannot rescue it and
        # the holding really is naked. Without this the system simply
        # arms a stop and is right to report itself protected.
        broker = FakeBroker(holds=[{"symbol": "TEST", "qty": "4"}],
                            reject_stops=True)
        ok, _, _ = duties(db, broker)
        assert not ok, "a genuinely unprotected holding stopped blocking"


class TestItRefusesToGuessWhenTheBrokerIsSilent:
    def test_a_broker_that_will_not_answer_changes_nothing(self, db):
        """`held_syms is None` must return no pending ids at all, so
        behaviour is exactly what it was before this fix. Assuming an
        emptiness we cannot confirm is the one unsafe direction."""
        seed_position(db, filled=False)
        _, rows, _ = duties(db, FakeBroker(positions_raise=True))
        assert cyc._pending_entry_ids(rows, None) == set()

    def test_a_silent_broker_does_not_authorise_entries(self, db):
        seed_position(db, filled=False)
        ok, _, report = duties(db, FakeBroker(positions_raise=True))
        assert not ok
        assert any("get_positions" in e for e in report.errors)


class TestAPhantomIsVoidedNeverSold:
    def test_a_due_phantom_is_voided(self, db):
        seed_position(db, filled=False, exit_date="2026-08-01")   # past
        broker = FakeBroker(holds=[])
        _, _, report = duties(db, broker)
        status = db.execute("SELECT status FROM positions").fetchone()[0]
        assert status == "void", f"expected void, got {status!r}"
        assert any("never filled" in e for e in report.errors)

    def test_a_due_phantom_is_never_market_sold(self, db):
        """Selling shares that were never bought is the naked short this
        whole fix exists to prevent - and a due position's other path is
        exactly a market sell."""
        seed_position(db, filled=False, exit_date="2026-08-01")
        broker = FakeBroker(holds=[])
        duties(db, broker)
        assert [o for o in broker.placed if o.get("side") == "sell"] == []

    def test_it_never_reaches_closed_trades(self, db):
        """Nothing was held, so there is no P&L. A void must not pollute
        the record the adaptive parameters learn from."""
        seed_position(db, filled=False, exit_date="2026-08-01")
        duties(db, FakeBroker(holds=[]))
        assert db.execute(
            "SELECT COUNT(*) FROM closed_trades").fetchone()[0] == 0

    def test_a_due_REAL_position_is_still_exited(self, db):
        """The void path must not swallow genuine exits."""
        seed_position(db, filled=True, exit_date="2026-08-01")
        broker = FakeBroker(holds=[{"symbol": "TEST", "qty": "4"}])
        duties(db, broker)
        assert [o for o in broker.placed if o.get("side") == "sell"], \
            "a real position reached its exit date and was not sold"


class TestTheFillFlagIsRealProvenance:
    def test_it_distinguishes_held_from_merely_ordered(self, db):
        """"we hold four shares" and "we asked for four and heard
        nothing back" were the same value of the same type before this."""
        seed_position(db, pos_id="p-filled", ticker="AAA", filled=True)
        seed_position(db, pos_id="p-empty", ticker="BBB", filled=False)
        rows = {r["id"]: r for r in cyc._open_position_dicts(db, NOW)}
        assert rows["p-filled"]["fill_confirmed"] is True
        assert rows["p-empty"]["fill_confirmed"] is False
        # and both still carry a qty, which is exactly why the flag is
        # needed - the qty alone cannot tell them apart
        assert rows["p-empty"]["qty"] == "4"
