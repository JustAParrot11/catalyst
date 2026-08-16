"""The spread the paper account never paid, recorded beside the real fill.

TRAPS.md, learned expensively: "Paper fills pay no spread. Model the
cost, but record it BESIDE the broker's price, not instead of it -
reconciliation compares against the real fill."

The column existed. It was written as NULL, every time, for every fill.

WHY THAT MATTERS MORE THAN IT SOUNDS. A paper account fills at the mid
and charges nothing to cross the spread. Real money crosses it twice -
once in, once out. On the small and micro caps where an insider-cluster
edge plausibly lives, a half-spread of 25-50bp a side is ordinary, so a
round trip quietly flatters paper P&L by something like 0.5-1.0%. The
bot's whole claim is "did this beat the S&P net of costs", and a
strategy whose per-trade edge is a couple of per cent can be entirely
manufactured by the cost nobody modelled.

TWO RULES, AND THE SECOND ONE IS THE TRAP.

  1. The modelled cost must actually be computed and stored.
  2. It must NEVER overwrite `broker_reported_price`. Reconciliation
     compares local records against the broker's own fills; a price
     adjusted by a model no longer matches anything the broker will
     say, and the comparison that catches real divergence breaks
     silently.
"""

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.execution.reconcile import _modeled_slippage, reconcile

NOW = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    root = Path(__file__).resolve().parents[1]
    conn.executescript((root / "catalyst/storage/schema.sql").read_text())
    return conn


def seed_order(conn, *, order_id="ord-1", half_spread_bp="30"):
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        (order_id, "dec-1", "brok-1", "buy", "4", "market", "day",
         NOW.isoformat(), "accepted", "{}"))
    if half_spread_bp is not None:
        conn.execute(
            "INSERT INTO entry_market_context "
            "(order_id, half_spread_bp, last_close, recorded_at) "
            "VALUES (?,?,?,?)",
            (order_id, half_spread_bp, "50.00", NOW.isoformat()))
    conn.commit()


class FakeBroker:
    def __init__(self, price="50.00", qty="4"):
        self.price, self.qty = price, qty

    def get_order_by_client_id(self, cid):
        return {"id": "brok-1", "status": "filled", "filled_qty": self.qty,
                "filled_avg_price": self.price,
                "filled_at": NOW.isoformat()}

    def get_order(self, oid):
        return self.get_order_by_client_id(oid)


class TestTheCostIsActuallyComputed:
    def test_a_measured_spread_produces_a_dollar_figure(self, db):
        seed_order(db, half_spread_bp="30")
        # 30bp of $50 x 4 shares = 0.003 x 200 = $0.60
        assert _modeled_slippage(db, "ord-1", Decimal("50"), Decimal("4")) \
            == "0.6000"

    def test_it_scales_with_the_spread(self, db):
        seed_order(db, half_spread_bp="60")
        assert _modeled_slippage(db, "ord-1", Decimal("50"), Decimal("4")) \
            == "1.2000"

    def test_reconcile_writes_it_onto_the_fill(self, db):
        seed_order(db, half_spread_bp="30")
        reconcile(FakeBroker(), db)
        row = db.execute(
            "SELECT broker_reported_price, modeled_slippage FROM fills "
            "WHERE order_id = 'ord-1'").fetchone()
        assert row is not None, "no fill was recorded at all"
        assert row[1] is not None, (
            "modeled_slippage is still NULL - the paper account's free "
            "spread is unmodelled and P&L is optimistic by it")
        assert Decimal(row[1]) > 0


class TestItNeverReplacesTheRealFill:
    """The trap. Reconciliation compares against the BROKER's number."""

    def test_the_brokers_price_is_untouched(self, db):
        seed_order(db, half_spread_bp="30")
        reconcile(FakeBroker(price="50.00"), db)
        row = db.execute(
            "SELECT price, broker_reported_price FROM fills "
            "WHERE order_id = 'ord-1'").fetchone()
        assert row[0] == "50.00", "the fill price was adjusted by the model"
        assert row[1] == "50.00", (
            "broker_reported_price was modified - reconciliation now "
            "compares against a number the broker will never report")

    def test_the_two_are_separate_columns(self, db):
        seed_order(db, half_spread_bp="30")
        reconcile(FakeBroker(), db)
        price, slippage = db.execute(
            "SELECT broker_reported_price, modeled_slippage FROM fills"
        ).fetchone()
        assert Decimal(price) != Decimal(slippage)


class TestUnmeasuredIsNoneNotZero:
    def test_no_recorded_spread_gives_None(self, db):
        seed_order(db, half_spread_bp=None)
        assert _modeled_slippage(db, "ord-1", Decimal("50"),
                                 Decimal("4")) is None

    def test_zero_would_be_a_LIE_and_None_is_the_fact(self, db):
        """A zero reads as "crossing this spread was free", which is
        never true of any real book. None reads as "not measured"."""
        seed_order(db, half_spread_bp=None)
        assert _modeled_slippage(db, "ord-1", Decimal("50"),
                                 Decimal("4")) != "0"

    @pytest.mark.parametrize("junk", ["", "abc", "NaN", "Infinity", "-Inf"])
    def test_unusable_upstream_values_give_None_not_a_crash(self, db, junk):
        seed_order(db, half_spread_bp=junk)
        assert _modeled_slippage(db, "ord-1", Decimal("50"),
                                 Decimal("4")) is None

    def test_an_unknown_order_gives_None(self, db):
        assert _modeled_slippage(db, "never-seen", Decimal("50"),
                                 Decimal("4")) is None


class TestItIsRecordedAtEntry:
    def test_the_cycle_stores_the_measured_spread_for_its_entry(self, db):
        """The context row can only be written at entry - reconcile runs
        on a later cycle, by which time the book has moved."""
        seed_order(db, half_spread_bp="42.5")
        row = db.execute(
            "SELECT half_spread_bp, last_close FROM entry_market_context "
            "WHERE order_id = 'ord-1'").fetchone()
        assert row == ("42.5", "50.00")
