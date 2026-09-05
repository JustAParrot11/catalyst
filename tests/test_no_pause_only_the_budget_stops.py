"""The bot sat idle for 3.5 trading days because a forecast was wrong.

OWNER'S 7-DAY PRICING BUNDLE, 2026-09-05:

    2026-09-01  local 668.7856c   billed 470.1904c   -> scheduled_paused
                "the bot priced 668.7856c of its own spend on a day the
                 whole organisation was billed only 470.1904c - it
                 cannot have outspent the account ... its arithmetic is
                 wrong"
    2026-09-02 00:02  paused
    2026-09-05 17:52  acknowledged by the owner
    in between:  201 x budget_denied: reconciliation_discrepancy_unacknowledged
                 0 research calls, 0 position reviews, 3 trading days

The arithmetic WAS wrong - pricing.py had forecast that Sonnet 5's
introductory rate ended on 31 August and it had not - and the fix was
a rate correction, which the reconciliation performs itself on the same
pass. Halting every research call until a human clicked a button did
not make the ledger more honest for one second; it cost three days of
the only thing the bot exists to do.

OWNER-SET 2026-09-05: "i dont really need any hard limit except a hard
stop to stop bot using all the budget".

So: a discrepancy is RECORDED, with the condition that fired and why,
and never gates spending. The governor keeps exactly one integrity
gate - an unpriced row - because that is a hole in the count the budget
stop itself depends on. Everything else that stops spending is the
budget.

Fully offline.
"""

import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.cost import CostEstimate
from catalyst.cost.governor import authorize
from catalyst.cost.tracker import (
    CostApiPage, has_unacknowledged_discrepancy, reconcile_day,
)
from catalyst.storage import init_db

TODAY = datetime.now(timezone.utc).date()
YESTERDAY = TODAY - timedelta(days=1)
SHARE = Decimal("0.10")


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


def spend(conn, cents, day=YESTERDAY, model="claude-sonnet-5"):
    conn.execute(
        "INSERT INTO cost_events (id, raw_usage_json, model, kind, component, "
        "priced_cents, priced_at, api_call_id) VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "{}", model, "scheduled", "research",
         str(cents), f"{day.isoformat()}T12:00:00+00:00", None))
    conn.commit()


def page(amount):
    return CostApiPage(records=[{"amount": str(amount)}], has_more=False,
                       raw_response={"data": [{"amount": str(amount)}]})


#: The owner's actual cap. Without it the $5 base cap would refuse the
#: 1 September figures on BUDGET grounds - which is the stop that is
#: supposed to exist - and these tests would be asserting the wrong
#: gate.
OWNER_CAP = Decimal("10000")


def ask(conn):
    return authorize(CostEstimate(estimated_cents=Decimal("1"), basis="t",
                                  kind="scheduled", component="research"),
                     conn, SHARE, as_of=TODAY,
                     owner_monthly_cap_cents=OWNER_CAP)


class TestTheOwnersDayNoLongerPausesAnything:
    def test_the_first_of_september_figures_do_not_stop_the_bot(self, db):
        """The literal numbers from the bundle."""
        spend(db, "668.7856")
        result = reconcile_day(YESTERDAY, db, lambda d: page("470.1904"))
        assert result.action_taken != "scheduled_paused"
        assert not has_unacknowledged_discrepancy(db)
        d = ask(db)
        assert d.authorized, d.reason

    def test_the_discrepancy_is_still_on_the_record_with_its_reason(self, db):
        """Not silence: the row says what fired and why. That is the
        diagnosis; it is just not a gate any more."""
        spend(db, "668.7856")
        reconcile_day(YESTERDAY, db, lambda d: page("470.1904"))
        row = db.execute(
            "SELECT action_taken, pause_reason, discrepancy_cents, "
            "acknowledged_by FROM cost_reconciliation_events").fetchone()
        assert row[0] == "discrepancy_noted"
        assert "cannot have outspent" in row[1]
        assert Decimal(row[2]) == Decimal("198.5952")
        assert row[3] == "auto"

    def test_the_rate_is_corrected_on_the_same_pass(self, db):
        """What a discrepancy DOES now. 668 priced against 470 billed is
        the rate running 30% high; the next day prices lower."""
        from catalyst.cost.overrides import rates_for_on

        before = rates_for_on(db, "claude-sonnet-5", TODAY)
        spend(db, "668.7856")
        reconcile_day(YESTERDAY, db, lambda d: page("470.1904"))
        after = rates_for_on(db, "claude-sonnet-5", TODAY)
        assert after[0] < before[0], (
            "the discrepancy was noted but the rate that caused it was "
            "left where it was - that is how it recurs tomorrow")

    def test_an_empty_api_answer_does_not_pause_either(self, db):
        spend(db, "50")
        reconcile_day(YESTERDAY, db, lambda d: CostApiPage(
            records=[], has_more=False, raw_response={"data": []}))
        assert not has_unacknowledged_discrepancy(db)
        assert ask(db).authorized

    def test_a_month_of_accumulated_drift_does_not_pause_either(self, db):
        for n in range(2, 31):
            day = YESTERDAY - timedelta(days=n)
            db.execute(
                "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
                "component, local_total_cents, cost_api_total_cents, "
                "discrepancy_cents, threshold_cents, api_raw_response, "
                "api_record_count, action_taken, acknowledged_by, "
                "acknowledged_at, reconciled_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), day.isoformat(), "all", "{}", "40", "10",
                 "30", "50", "{}", 1, "none", "auto",
                 day.isoformat(), day.isoformat()))
        db.commit()
        spend(db, "40")
        result = reconcile_day(YESTERDAY, db, lambda d: page("40"))
        assert result.action_taken == "discrepancy_noted"
        assert "drift" in db.execute(
            "SELECT pause_reason FROM cost_reconciliation_events "
            "WHERE target_date = ?", (YESTERDAY.isoformat(),)).fetchone()[0]
        assert ask(db).authorized


class TestTheOnlyStopsLeft:
    def test_a_legacy_paused_row_is_history_not_a_gate(self, db):
        """A database upgraded from before 2026-09-05 may still carry
        one. The governor must not read it."""
        db.execute(
            "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
            "component, local_total_cents, cost_api_total_cents, "
            "discrepancy_cents, threshold_cents, api_raw_response, "
            "api_record_count, action_taken, reconciled_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy", YESTERDAY.isoformat(), "all", "{}", "100", "40",
             "60", "50", "{}", 1, "scheduled_paused", TODAY.isoformat()))
        db.commit()
        assert has_unacknowledged_discrepancy(db), "fixture sanity"
        d = ask(db)
        assert d.authorized, d.reason
        assert "reconciliation" not in (d.reason or "")

    def test_an_unpriced_row_still_stops_it(self, db):
        """The one integrity gate kept: a row nobody could price is a
        hole in the count, and a budget stop cannot count through a
        hole."""
        db.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at, api_call_id) VALUES "
            "('u','{}','who-knows','scheduled','research',NULL,?,'a')",
            (datetime.now(timezone.utc).isoformat(),))
        db.commit()
        d = ask(db)
        assert not d.authorized
        assert d.reason == "unpriced_cost_rows"

    def test_the_budget_still_stops_it(self, db):
        spend(db, "1000000", day=TODAY)
        d = ask(db)
        assert not d.authorized
        assert "cap_exceeded" in d.reason or "daily_cap" in d.reason

    def test_the_governor_source_names_no_reconciliation_gate(self):
        """The rule, not the instance: nothing in authorize() may consult
        the reconciliation table again."""
        import inspect

        from catalyst.cost import governor

        src = inspect.getsource(governor.authorize)
        assert "has_unacknowledged_discrepancy" not in src
        assert "reconciliation_discrepancy_unacknowledged" not in src

    def test_nothing_writes_a_pause_any_more(self):
        import inspect

        from catalyst.cost import tracker

        code = "\n".join(line for line in
                         inspect.getsource(tracker.reconcile_day).splitlines()
                         if not line.strip().startswith("#"))
        assert '"scheduled_paused"' not in code, (
            "reconcile_day still writes the action the governor used to "
            "gate on")


class TestTheCheckCanFail:
    """House rule 4: reproduce the gate that shipped and confirm these
    tests would have caught it."""

    def test_the_old_gate_would_have_denied_the_owners_day(self, db):
        from catalyst.cost.tracker import has_unpriced_rows

        db.execute(
            "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
            "component, local_total_cents, cost_api_total_cents, "
            "discrepancy_cents, threshold_cents, api_raw_response, "
            "api_record_count, action_taken, reconciled_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy", YESTERDAY.isoformat(), "all", "{}", "668", "470",
             "198", "66", "{}", 3, "scheduled_paused", TODAY.isoformat()))
        db.commit()
        # The shipped loop, verbatim in shape:
        old_gates = ((has_unpriced_rows, "unpriced_cost_rows"),
                     (has_unacknowledged_discrepancy,
                      "reconciliation_discrepancy_unacknowledged"))
        fired = [reason for check, reason in old_gates if check(db)]
        assert fired == ["reconciliation_discrepancy_unacknowledged"], (
            "the fixture does not reproduce the gate that held the bot")
        assert ask(db).authorized, "and the live governor no longer has it"
