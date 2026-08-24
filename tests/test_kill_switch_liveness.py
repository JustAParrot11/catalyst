"""A kill switch that tripped once must not read as tripped forever.

OWNER'S DIAGNOSTIC BUNDLE, 2026-08-24: the Maintenance page reported
"Kill switches: 2 active" and FAIL, on a day the bot had researched 115
candidates, placed an order and logged no trip at all.

THE CAUSE. `cleared_at` is written by nothing - there is no code path
anywhere that sets it - so "WHERE cleared_at IS NULL" meant "has ever
tripped". The first trip in the bot's life turned that check red
permanently, and the alerts strip said "kill switch ACTIVE ... since"
about a day that was long over.

Same class as the resolved-unprotected alarm and the reconciliation
prompt before it: a historical fact rendered as a live one. CLAUDE.md:
routine attrition must not look like damage.

THE RULE, from how the cycle is actually written: a `broker_read` equity
snapshot is taken immediately AFTER the kill check, and the tripped
branch returns before reaching it. So a broker read newer than a trip is
proof that a later cycle ran the same check and passed. That is a fact
about the bot's own record, not a guess, and it needs no risk code to
change.

Fully offline.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard.maintenance import passive_checks
from catalyst.dashboard.queries import alerts

SCHEMA = Path(__file__).resolve().parents[1] / "catalyst" / "storage" / "schema.sql"

#: HOUSE RULE 6 does not apply: every timestamp below is written into
#: the fixture AND read back by SQL that only ever compares rows against
#: each other. Nothing here is measured against datetime.now().
TRIPPED_AT = datetime(2026, 8, 19, 14, 35, tzinfo=timezone.utc)


class Ledger:
    """A writable database plus the read-only view the dashboard gets."""

    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA.read_text())

    def trip(self, name, at=TRIPPED_AT, cleared=None):
        self.conn.execute(
            "INSERT INTO kill_switch_events "
            "(triggered_at, switch_name, portfolio_state_snapshot, cleared_at) "
            "VALUES (?,?,?,?)",
            (at.isoformat(), name, '{"equity_usd": "1980.00"}',
             cleared.isoformat() if cleared else None))
        self.conn.commit()

    def broker_read(self, at):
        """What the cycle writes immediately after the kill check passes."""
        self.conn.execute(
            "INSERT OR REPLACE INTO equity_snapshots "
            "(day, taken_at, equity_usd, settled_cash_usd, "
            " positions_notional, source) VALUES (?,?,?,?,?,?)",
            (at.date().isoformat(), at.isoformat(), "1990.00", "1500.00",
             "490.00", "broker_read"))
        self.conn.commit()

    def view(self) -> Db:
        return Db(self.path)

    def close(self):
        self.conn.close()


@pytest.fixture
def led(tmp_path):
    l = Ledger(tmp_path / "t.db")
    yield l
    l.close()


def live_alarms(led):
    view = led.view()
    try:
        return [t for sev, t, _ in alerts(view).items if sev == "alarm"
                and "kill switch" in t]
    finally:
        view.close()


def kill_check(led):
    view = led.view()
    try:
        got = [c for c in passive_checks(view)
               if c.name == "Kill switches"]
        assert len(got) == 1
        return got[0]
    finally:
        view.close()


class TestATripThatIsOverStopsBeingAnAlarm:
    def test_a_later_cycle_getting_past_the_check_clears_it(self, led):
        """THE OWNER'S CASE. The broker mark is written only after the
        kill check passes, so one dated after the trip is proof the bot
        is running normally again."""
        led.trip("daily_loss_limit")
        led.broker_read(TRIPPED_AT + timedelta(hours=2))
        assert live_alarms(led) == []
        assert kill_check(led).state == "ok"
        assert "none tripped" in kill_check(led).summary

    def test_two_old_trips_do_not_make_the_page_fail(self, led):
        """Verbatim from the bundle: 'Kill switches: 2 active', FAIL."""
        led.trip("daily_loss_limit")
        led.trip("max_drawdown", at=TRIPPED_AT + timedelta(minutes=15))
        led.broker_read(TRIPPED_AT + timedelta(hours=5))
        assert kill_check(led).state == "ok"

    def test_the_earlier_trips_are_still_counted_on_the_page(self, led):
        """Not deleted and not hidden: they are real, and a page that
        silently drops them cannot answer 'has this ever stopped?'."""
        led.trip("daily_loss_limit")
        led.broker_read(TRIPPED_AT + timedelta(hours=2))
        assert "1 earlier trip(s)" in kill_check(led).summary


class TestATripThatIsLiveStillStopsEverything:
    def test_a_trip_with_no_later_broker_read_is_active(self, led):
        led.trip("daily_loss_limit")
        assert kill_check(led).state == "fail"
        assert live_alarms(led), "a live kill switch must still alarm"

    def test_a_broker_read_from_BEFORE_the_trip_proves_nothing(self, led):
        """The direction matters. A mark taken earlier in the day was
        written by a cycle that had not yet seen the loss."""
        led.broker_read(TRIPPED_AT - timedelta(hours=3))
        led.trip("daily_loss_limit")
        assert kill_check(led).state == "fail"

    def test_the_live_switch_is_named(self, led):
        led.trip("max_drawdown")
        assert "max_drawdown" in kill_check(led).summary, (
            "'1 active' tells the owner nothing about which rule stopped "
            "the bot or what to do about it")

    def test_one_live_trip_beside_an_old_one_still_fails(self, led):
        led.trip("daily_loss_limit")
        led.broker_read(TRIPPED_AT + timedelta(hours=2))
        led.trip("max_drawdown", at=TRIPPED_AT + timedelta(hours=6))
        check = kill_check(led)
        assert check.state == "fail"
        assert "max_drawdown" in check.summary
        assert "daily_loss_limit" not in check.summary

    def test_an_explicitly_cleared_trip_is_never_live(self, led):
        """cleared_at is written by nothing today, but it is the column
        that MEANS this - so it has to keep meaning it."""
        led.trip("daily_loss_limit",
                 cleared=TRIPPED_AT + timedelta(hours=1))
        assert kill_check(led).state == "ok"


class TestNothingTrippedReadsAsNothingTripped:
    def test_a_bot_that_has_never_tripped_is_ok(self, led):
        assert kill_check(led).state == "ok"
        assert live_alarms(led) == []

    def test_the_two_readers_can_never_disagree(self, led):
        """The alerts strip and the Maintenance page share one rule on
        purpose: two answers to 'is the bot stopped?' is worse than
        either answer being wrong."""
        led.trip("daily_loss_limit")
        assert bool(live_alarms(led)) is (kill_check(led).state == "fail")
        led.broker_read(TRIPPED_AT + timedelta(hours=2))
        assert bool(live_alarms(led)) is (kill_check(led).state == "fail")
