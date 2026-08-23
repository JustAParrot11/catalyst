"""A five-cent drift must not halt the bot for a day.

OWNER-REPORTED, from the live funnel:

    125  spending was blocked: reconciliation_discrepancy_unacknowledged
         last seen yesterday (2026-08-13, first 2026-08-12)

125 candidates refused. Not the budget - the reconciliation gate. And I
guessed the cap when asked, which was wrong; the named-gate fix landed
first and it is what turned the guess into this answer.

THE CAUSE. The pause test was:

    drift.copy_abs() > RECONCILE_FLOOR_CENTS     # 5 cents, 30-day window

Five cents of ACCUMULATED signed difference across a month stopped all
spending until a human clicked acknowledge. The Cost API reports whole
days and settles slightly differently from a local estimate, so a few
cents of drift is the expected state, not a fault. Rounding alone
reaches it.

OWNER DECISION, 2026-08-14 (recorded in TRAPS.md): a daily figure is
fine, the budget re-bases the next day, so reconciliation is a
correction rather than an alarm. Asked how a discrepancy should behave,
the owner chose: BLOCK ONLY IF LARGE.

So the gate keeps its job - a genuine billing fault must still stop the
bot - but "large" is now measured against what is actually being spent
rather than against five cents.
"""

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest

from catalyst.cost import tracker


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "r.db"))
    c.executescript(open("catalyst/storage/schema.sql").read())
    c.commit()
    return c


def _event(conn, day, local, api, action="none"):
    conn.execute(
        "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
        "component, local_total_cents, cost_api_total_cents, "
        "discrepancy_cents, threshold_cents, api_raw_response, "
        "api_record_count, action_taken, reconciled_at) "
        "VALUES (?,?,'all','{}',?,?,?,'5','{}',1,?,?)",
        (f"e-{day}", day.isoformat(), str(local), str(api),
         str(abs(Decimal(local) - Decimal(api))), action,
         day.isoformat() + "T12:00:00+00:00"))
    conn.commit()


class TestSmallDriftDoesNotHaltTheBot:
    def test_a_few_cents_across_a_month_is_not_a_fault(self, conn):
        """THE REPORTED HALT. Thirty days each differing by a cent or two
        is the expected shape of a whole-day billing figure against a
        real-time local estimate - and it used to stop everything."""
        today = date(2026, 8, 13)
        for i in range(30):
            day = today - timedelta(days=i + 1)
            _event(conn, day, local=1001, api=1000)   # 1c a day, bot HIGH
        drift = tracker._trailing_signed_drift(conn, today)
        assert drift.copy_abs() >= 5, "the fixture should exceed the old 5c"
        assert not tracker.drift_is_material(drift, conn, today), (
            f"{drift}c of drift against a month of ~$300 of spend is "
            "noise, and it halted the bot for a day")

    def test_a_LARGE_drift_still_halts(self, conn):
        """The gate keeps its job. A month where local says half what the
        bill says is a real fault - a missed cost component, or double
        billing - and must still stop the bot."""
        today = date(2026, 8, 13)
        for i in range(30):
            day = today - timedelta(days=i + 1)
            _event(conn, day, local=1000, api=500)    # bot claims DOUBLE
        drift = tracker._trailing_signed_drift(conn, today)
        assert tracker.drift_is_material(drift, conn, today), (
            f"{drift}c of drift on a month of ~$300 is a 50% miss and "
            "must still block")

    def test_it_is_proportionate_not_a_fixed_number(self, conn):
        """The same absolute drift means different things at different
        spend. A fixed floor cannot tell them apart, which is exactly
        how five cents came to be a halt condition."""
        today = date(2026, 8, 13)
        for i in range(10):
            _event(conn, today - timedelta(days=i + 1), local=100, api=90)
        small_spend_drift = tracker._trailing_signed_drift(conn, today)
        assert tracker.drift_is_material(small_spend_drift, conn, today)

        big = sqlite3.connect(":memory:")
        big.executescript(open("catalyst/storage/schema.sql").read())
        for i in range(10):
            _event(big, today - timedelta(days=i + 1), local=100000, api=99990)
        big_spend_drift = tracker._trailing_signed_drift(big, today)
        big.close()
        assert big_spend_drift == small_spend_drift, "same absolute drift"
        assert not tracker.drift_is_material(big_spend_drift, None, today,
                                             window_total=Decimal("1000000")), (
            "the same 100c against $10,000 of spend is 0.01% - noise")

    def test_nothing_below_the_absolute_floor_ever_blocks(self, conn):
        """Below this, no proportion matters: a bot that has spent almost
        nothing must not halt on a rounding difference."""
        assert not tracker.drift_is_material(
            Decimal("20"), None, date(2026, 8, 13), window_total=Decimal("1"))


class TestTheSingleDayGateIsUnchangedInSpirit:
    def test_a_big_single_day_discrepancy_still_pauses(self):
        """This half already scaled with spend and is left alone."""
        assert tracker.RECONCILE_REL_THRESHOLD == Decimal("0.10")

    def test_the_pause_floor_is_stated_and_material(self):
        assert tracker.RECONCILE_PAUSE_FLOOR_CENTS >= 25, (
            "a floor small enough to be reached by rounding is how the "
            "bot came to be halted by five cents")
