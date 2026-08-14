"""`budget_denied` must say WHICH gate denied it.

OWNER-REPORTED, from a live funnel: "125 research skipped: budget_denied"
and the follow-up question - "why are we still tracking local cost ...
surely we dont need a daily check to see cost is correct". That question
is what an unexplained denial produces. The owner could see 125
candidates refused and could not tell whether the cause was the budget
or the reconciliation machinery, so the machinery became the suspect.

authorize() distinguishes THREE gates, and they have three completely
different fixes:

  cap_exceeded                              -> raise the budget
  reconciliation_discrepancy_unacknowledged -> click acknowledge
  unpriced_cost_rows                        -> a pricing bug, needs code

boundary.py threw `decision.reason` away and recorded the bare string
"budget_denied" for all three. "Out of money" and "a discrepancy nobody
acknowledged is blocking every call" are not the same problem, and the
second one is invisible until somebody guesses at it.
"""

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from catalyst.research import boundary, prompts


@pytest.fixture(autouse=True)
def _cheap_prompts(monkeypatch):
    monkeypatch.setattr(prompts, "render_research_prompt", lambda c, **kw: "r")
    monkeypatch.setattr(prompts, "exploration_tools", lambda *a, **kw: [])


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "d.db"))
    c.executescript(open("catalyst/storage/schema.sql").read())
    c.commit()
    return c


def _candidate():
    from datetime import timedelta

    from catalyst.discovery import Candidate

    now = datetime.now(timezone.utc)
    return Candidate(
        id="cand-deny", ticker="DENY", catalyst_type="insider_cluster",
        catalyst_date=now.date() + timedelta(days=5),
        catalyst_date_confidence="confirmed", source_event_ids=("e1",),
        discovered_at=now, sector="tech", correlation_tags=("tech",))


def _investigate(conn):
    return boundary.investigate(
        _candidate(),
        boundary.CostContext(conn=conn, governor_profit_share=Decimal("0"),
                             cycle_id="cyc-d", kind="scheduled"),
        lambda payload: pytest.fail("a denied call must never reach the API"))


def _burn_the_cap(conn):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO cost_events (id, raw_usage_json, model, kind, component, "
        "priced_cents, priced_at, api_call_id) VALUES "
        "('spent','{}','claude-sonnet-5','scheduled','research','9999',?,'a')",
        (now,))
    conn.commit()


def _unacknowledged_discrepancy(conn):
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
        "component, local_total_cents, cost_api_total_cents, "
        "discrepancy_cents, threshold_cents, api_raw_response, "
        "api_record_count, action_taken, reconciled_at) "
        "VALUES ('r1',?,'scheduled','research','100','160','60','5','{}',1,"
        "'scheduled_paused',?)",
        (now.date().isoformat(), now.isoformat()))
    conn.commit()


class TestTheDenialNamesItsGate:
    def test_running_out_of_budget_says_so(self, conn):
        _burn_the_cap(conn)
        log = _investigate(conn)
        assert log.skipped_reason is not None
        assert "cap_exceeded" in log.skipped_reason, log.skipped_reason

    def test_an_unacknowledged_discrepancy_says_so(self, conn):
        """THE ONE THAT CAUSED THE QUESTION. This is not "out of money" -
        it is the daily reconciliation holding spending until a human
        acknowledges it, and the fix is one click, not a bigger budget."""
        _unacknowledged_discrepancy(conn)
        log = _investigate(conn)
        assert log.skipped_reason is not None
        assert "reconciliation" in log.skipped_reason, log.skipped_reason

    def test_the_two_are_DISTINGUISHABLE(self, conn, tmp_path):
        """The whole defect: both used to read `budget_denied`, so a
        funnel full of denials could not tell the owner which of two
        unrelated problems they had."""
        _burn_the_cap(conn)
        capped = _investigate(conn).skipped_reason

        other = sqlite3.connect(str(tmp_path / "d2.db"))
        other.executescript(open("catalyst/storage/schema.sql").read())
        other.commit()
        _unacknowledged_discrepancy(other)
        blocked = _investigate(other)
        other.close()

        assert capped != blocked.skipped_reason, (
            f"both denials read {capped!r} - the owner cannot tell a "
            "budget that ran out from a discrepancy nobody acknowledged")

    def test_an_unpriced_row_says_so(self, conn):
        """The third gate: a cost row that could not be priced. That is a
        code fault, not a budget one."""
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at, api_call_id) VALUES "
            "('u','{}','who-knows','scheduled','research',NULL,?,'a')", (now,))
        conn.commit()
        log = _investigate(conn)
        assert "unpriced" in (log.skipped_reason or ""), log.skipped_reason

    def test_the_denial_still_costs_nothing(self, conn):
        """Naming the gate must not change the one thing that matters
        about a denial: no call is made."""
        _burn_the_cap(conn)
        log = _investigate(conn)     # the transport pytest.fail()s if used
        assert log.api_turns == ()
