"""Upgrading must actually unblock, and Download must download.

Two owner reports, both of the same shape: the fix landed but the
symptom did not change.

1. "still says 125 spending was blocked:
    reconciliation_discrepancy_unacknowledged"

   The block-only-if-large rule governs whether a NEW reconciliation
   pauses. It does nothing about a row ALREADY written under the old
   five-cent rule, which sits in the database unacknowledged and keeps
   blocking every call forever. So the owner upgrades, sees the same
   sentence, and reasonably concludes the fix did nothing. Verified by
   running before this was written: has_unacknowledged_discrepancy()
   still returned True on a 6c row after the new rule shipped.

   Re-judging is not "ignore a fault". It asks the same question with
   the rule the owner chose on 2026-08-14 and clears only rows whose own
   recorded discrepancy does not clear that bar. A row that WOULD still
   pause is left alone.

2. "The download button just opens the text in a new tab and doesnt
    start a download"

   /diagnostics.json had no Content-Disposition, so browsers render JSON
   inline. BUILD-BRIEF asks for "one click exports a diagnostic bundle";
   selecting a wall of text and pasting it is not that.
"""

import sqlite3
from datetime import datetime, timezone

import pytest

from catalyst.cost.tracker import (
    clear_pauses_that_no_longer_qualify,
    has_unacknowledged_discrepancy,
)


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "s.db"))
    c.executescript(open("catalyst/storage/schema.sql").read())
    c.commit()
    return c


def _paused(conn, row_id, discrepancy, api_total):
    conn.execute(
        "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
        "component, local_total_cents, cost_api_total_cents, "
        "discrepancy_cents, threshold_cents, api_raw_response, "
        "api_record_count, action_taken, reconciled_at) "
        "VALUES (?,'2026-08-12','all','{}',?,?,?,'5','{}',1,"
        "'scheduled_paused',?)",
        (row_id, str(api_total + discrepancy), str(api_total),
         str(discrepancy), datetime.now(timezone.utc).isoformat()))
    conn.commit()


class TestUpgradingActuallyUnblocks:
    def test_a_stale_five_cent_pause_is_cleared(self, conn):
        """THE REPORTED SYMPTOM. 6c against a $1 day - the exact shape
        that blocked 125 candidates."""
        _paused(conn, "old", discrepancy=6, api_total=100)
        assert has_unacknowledged_discrepancy(conn), "fixture must block"
        assert clear_pauses_that_no_longer_qualify(conn) == 1
        assert not has_unacknowledged_discrepancy(conn), (
            "the bot is still blocked after the upgrade - the owner sees "
            "the identical sentence and concludes nothing was fixed")

    def test_a_pause_that_STILL_qualifies_is_left_alone(self, conn):
        """Re-judging must not become a blanket amnesty. A genuine
        billing fault has to keep blocking."""
        _paused(conn, "real", discrepancy=5000, api_total=1000)
        assert clear_pauses_that_no_longer_qualify(conn) == 0
        assert has_unacknowledged_discrepancy(conn), (
            "a 500% discrepancy was cleared - that is a real fault")

    def test_the_clearance_records_WHY(self, conn):
        """An audit trail that says a human acknowledged something no
        human saw would be a lie in the record."""
        _paused(conn, "old", discrepancy=6, api_total=100)
        clear_pauses_that_no_longer_qualify(conn)
        by, at = conn.execute(
            "SELECT acknowledged_by, acknowledged_at FROM "
            "cost_reconciliation_events WHERE id='old'").fetchone()
        assert at, "no timestamp recorded"
        assert "auto" in by and "re-judged" in by, by
        assert "2026-08-14" in by, "the decision it rests on is not named"

    def test_an_unreadable_row_keeps_blocking(self, conn):
        """A row we cannot judge is not a row we may clear. It stays,
        for a human."""
        conn.execute(
            "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
            "component, local_total_cents, cost_api_total_cents, "
            "discrepancy_cents, threshold_cents, api_raw_response, "
            "api_record_count, action_taken, reconciled_at) "
            "VALUES ('bad','2026-08-12','all','{}','x','y','not-a-number',"
            "'5','{}',1,'scheduled_paused',?)",
            (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        assert clear_pauses_that_no_longer_qualify(conn) == 0
        assert has_unacknowledged_discrepancy(conn)

    def test_it_is_idempotent(self, conn):
        _paused(conn, "old", discrepancy=6, api_total=100)
        assert clear_pauses_that_no_longer_qualify(conn) == 1
        assert clear_pauses_that_no_longer_qualify(conn) == 0


# The download half is checked in scripts/dashboard_smoke.py, which
# serves the app over a real socket. It cannot live here: tests/conftest
# blocks every connection, by contract - the suite is fully offline, and
# a test that needs a listening port does not belong in it.
