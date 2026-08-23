"""A halted bot must not read as a quiet one.

governor.authorize() refuses ALL new spend on either an unacknowledged
reconciliation OR an unpriced cost row. Both are silent from outside:
the service keeps running, open positions stay protected, and it simply
stops researching. Only the reconciliation case appeared on the
always-visible state line, so a bot halted on a Tuesday looked exactly
like a quiet one until somebody opened the Cost page.

That matters most in precisely the situation the state line exists for -
the owner glancing at it after leaving the bot alone for a week.
"""

from datetime import datetime, timezone

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard import panels
from catalyst.storage import init_db


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "state.db")
    init_db(p).close()
    return p


def add_unpriced(path, n=1):
    conn = init_db(path)
    for i in range(n):
        conn.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at) VALUES (?,?,?,?,?,?,?)",
            (f"u{i}", '{"mystery_tokens": 1}', "claude-sonnet-5", "scheduled",
             "research", None, datetime.now(timezone.utc).isoformat()))
    conn.commit(); conn.close()


def add_paused_reconciliation(path):
    conn = init_db(path)
    conn.execute(
        "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
        "component, local_total_cents, cost_api_total_cents, "
        "discrepancy_cents, threshold_cents, api_raw_response, "
        "api_record_count, action_taken, reconciled_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("r1", "2026-08-20", "all", "{}", "100", "200", "100", "50", "{}", 1,
         "scheduled_paused", datetime.now(timezone.utc).isoformat()))
    conn.commit(); conn.close()


class TestBothPausesInterruptAGlance:
    def test_an_unpriced_row_says_spending_is_paused(self, db_path):
        """THE ONE THAT WAS MISSING. An unknown billing field halts the
        governor by design - Anthropic has added two such fields in this
        project's lifetime - and it used to be visible only on the Cost
        page."""
        add_unpriced(db_path)
        html = panels.state_line(Db(db_path))
        assert "PAUSED" in html
        assert "stopped researching" in html

    def test_it_says_how_many_rows(self, db_path):
        add_unpriced(db_path, n=3)
        assert "3 cost row(s)" in panels.state_line(Db(db_path))

    def test_the_reconciliation_pause_still_shows(self, db_path):
        """The one that already worked must keep working."""
        add_paused_reconciliation(db_path)
        html = panels.state_line(Db(db_path))
        assert "PAUSED" in html and "acknowledge" in html

    def test_both_at_once_both_appear(self, db_path):
        add_unpriced(db_path)
        add_paused_reconciliation(db_path)
        html = panels.state_line(Db(db_path))
        assert "acknowledge" in html and "stopped researching" in html

    def test_a_healthy_bot_says_nothing_about_pauses(self, db_path):
        """The line must not cry wolf - it is read at a glance, and a
        permanent warning is one nobody reads."""
        assert "PAUSED" not in panels.state_line(Db(db_path))

    def test_a_priced_row_is_not_mistaken_for_an_unpriced_one(self, db_path):
        conn = init_db(db_path)
        conn.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at) VALUES (?,?,?,?,?,?,?)",
            ("ok", "{}", "claude-sonnet-5", "scheduled", "research", "19",
             datetime.now(timezone.utc).isoformat()))
        conn.commit(); conn.close()
        assert "PAUSED" not in panels.state_line(Db(db_path))


class TestItCannotBreakEveryPage:
    def test_a_missing_table_loses_the_warning_not_the_line(self, db_path):
        """state_line renders on every page. A read fault must cost one
        clause, never the whole strip."""
        conn = init_db(db_path)
        conn.execute("DROP TABLE cost_events")
        conn.commit(); conn.close()
        html = panels.state_line(Db(db_path))
        assert "state-line" in html
