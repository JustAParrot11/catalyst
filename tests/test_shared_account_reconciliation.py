"""The Cost API bills the whole ORGANISATION, not just this bot.

OWNER-REPORTED 2026-08-23, with the figures on screen:

    2026-08-22 - local $0.08 vs Cost API $0.08, discrepancy $0.00
    against a threshold of $0.50
    "1 unacknowledged reconciliation discrepancy(ies). Scheduled spend
     is PAUSED until a human acknowledges each one."

The day agreed to the cent and it halted trading anyway, repeatedly.
The accumulated drift did it: this owner runs Claude Code on the same
Anthropic account, so on any day they work, the ORGANISATION's bill
far exceeds the BOT's spend. Summing the signed difference and taking
its absolute value counted every hour of the owner's own use as
evidence against the bot's ledger.

Only one direction can indicate a bot fault, and these tests hold that
line. Fully offline.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.cost.tracker import CostApiPage, reconcile_day
from catalyst.storage import init_db

YESTERDAY = datetime.now(timezone.utc).date() - timedelta(days=1)


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "recon.db"))
    yield conn
    conn.close()


def page(amount):
    recs = [{"amount": str(amount)}]
    return CostApiPage(records=recs, has_more=False, raw_response={"data": recs})


def seed_local(conn, cents, day=None):
    day = day or YESTERDAY
    conn.execute(
        "INSERT INTO cost_events (id, raw_usage_json, model, kind, component, "
        "priced_cents, priced_at) VALUES (?,?,?,?,?,?,?)",
        (f"e{day}{cents}", "{}", "claude-sonnet-5", "scheduled", "research",
         str(cents), f"{day.isoformat()}T12:00:00+00:00"))
    conn.commit()


def prior_day(conn, day, local, api):
    """A reconciled day already on the record."""
    conn.execute(
        "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
        "component, local_total_cents, cost_api_total_cents, "
        "discrepancy_cents, threshold_cents, api_raw_response, "
        "api_record_count, action_taken, acknowledged_by, acknowledged_at, "
        "reconciled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"r{day}", day.isoformat(), "all", "{}", str(local), str(api),
         "0", "50", "{}", 1, "none", "auto",
         datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()))
    conn.commit()


class TestTheOwnersOwnApiUseDoesNotPauseTheBot:
    """THE REPORTED BUG. api > local is the normal state of a shared
    account and says nothing about this bot's arithmetic."""

    def test_a_day_that_agrees_exactly_does_not_pause(self, db):
        """The owner's exact case: local 8c, API 8c, and a month of
        their own Claude Code usage behind it."""
        for n in range(2, 20):
            day = YESTERDAY - timedelta(days=n)
            prior_day(db, day, local=8, api=5000)   # heavy personal use
        seed_local(db, "8")
        result = reconcile_day(YESTERDAY, db, lambda d: page("8"))
        assert result.action_taken == "none", (
            "a day that agreed to the cent still halted trading")

    def test_a_single_heavy_personal_day_does_not_pause(self, db):
        """The bot spent 8c; the organisation was billed $50 because the
        owner worked that day. Nothing about the bot is wrong."""
        seed_local(db, "8")
        result = reconcile_day(YESTERDAY, db, lambda d: page("5000"))
        assert result.action_taken == "none"

    def test_a_month_of_personal_use_never_accumulates_into_a_pause(self, db):
        for n in range(2, 31):
            prior_day(db, YESTERDAY - timedelta(days=n), local=10, api=8000)
        seed_local(db, "10")
        result = reconcile_day(YESTERDAY, db, lambda d: page("8000"))
        assert result.action_taken == "none"


class TestTheDirECTIONThatStillMatters:
    """The check is narrowed, not removed. The bot claiming to have
    outspent the entire organisation is impossible and still halts it."""

    def test_the_bot_outspending_the_whole_account_still_pauses(self, db):
        seed_local(db, "1000")
        result = reconcile_day(YESTERDAY, db, lambda d: page("1"))
        assert result.action_taken == "scheduled_paused"

    def test_and_the_reason_names_the_impossibility(self, db):
        seed_local(db, "1000")
        reconcile_day(YESTERDAY, db, lambda d: page("1"))
        reason = db.execute(
            "SELECT pause_reason FROM cost_reconciliation_events "
            "WHERE action_taken = 'scheduled_paused'").fetchone()[0]
        assert "cannot have outspent" in reason
        assert "1000" in reason and "1" in reason

    def test_accumulated_OVERSTATEMENT_still_pauses(self, db):
        """Small daily overstatements that each pass the floor must
        still add up to a halt - that check is why drift exists."""
        for n in range(2, 31):
            prior_day(db, YESTERDAY - timedelta(days=n), local=40, api=10)
        seed_local(db, "40")
        result = reconcile_day(YESTERDAY, db, lambda d: page("40"))
        assert result.action_taken == "scheduled_paused"
        reason = db.execute(
            "SELECT pause_reason FROM cost_reconciliation_events "
            "WHERE action_taken='scheduled_paused' ORDER BY reconciled_at DESC"
        ).fetchone()[0]
        assert "drift" in reason


class TestTheRecordStillShowsTheTruth:
    def test_the_full_gap_is_still_recorded_even_when_it_does_not_pause(self, db):
        """Only what PAUSES is narrowed. The row must still carry the
        real size of the difference, or the dashboard starts lying."""
        seed_local(db, "8")
        result = reconcile_day(YESTERDAY, db, lambda d: page("5000"))
        assert result.discrepancy_cents == Decimal("4992")
        assert result.cost_api_total_cents == Decimal("5000")
        assert result.local_total_cents == Decimal("8")

    def test_an_empty_api_answer_with_local_spend_still_pauses(self, db):
        """Unchanged: an empty answer is not agreement."""
        seed_local(db, "3")
        result = reconcile_day(YESTERDAY, db,
                               lambda d: CostApiPage(records=[], has_more=False,
                                                     raw_response={"data": []}))
        assert result.action_taken == "scheduled_paused"


class TestTheAutoClearNoLongerLoops:
    """A drift-caused pause has a SMALL day figure by definition, so
    re-judging it against that figure always cleared it - and the next
    cycle paused again on the same drift. That loop is what the owner
    saw as the discrepancy 'showing up frequently'."""

    def test_the_drift_behind_a_pause_is_recorded(self, db):
        for n in range(2, 31):
            prior_day(db, YESTERDAY - timedelta(days=n), local=40, api=10)
        seed_local(db, "40")
        reconcile_day(db and YESTERDAY, db, lambda d: page("40"))
        drift = db.execute(
            "SELECT drift_cents FROM cost_reconciliation_events "
            "WHERE action_taken='scheduled_paused'").fetchone()[0]
        assert drift is not None and Decimal(str(drift)) > 0, (
            "the drift that caused the pause was not stored, so nothing "
            "can re-judge it correctly later")

    def test_a_real_drift_pause_is_NOT_auto_cleared(self, db):
        from catalyst.cost.tracker import clear_pauses_that_no_longer_qualify

        for n in range(2, 31):
            prior_day(db, YESTERDAY - timedelta(days=n), local=40, api=10)
        seed_local(db, "40")
        reconcile_day(YESTERDAY, db, lambda d: page("40"))

        cleared = clear_pauses_that_no_longer_qualify(db)
        assert cleared == 0, (
            "a genuine drift pause was cleared on the day's small figure, "
            "so the drift check has no teeth and the pause returns next cycle")

    def test_a_row_from_before_the_column_existed_still_re_judges(self, db):
        """Rows written before drift_cents existed carry NULL and must
        fall back rather than crash the upgrade."""
        from catalyst.cost.tracker import clear_pauses_that_no_longer_qualify

        db.execute(
            "INSERT INTO cost_reconciliation_events (id, target_date, kind, "
            "component, local_total_cents, cost_api_total_cents, "
            "discrepancy_cents, threshold_cents, api_raw_response, "
            "api_record_count, action_taken, reconciled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("old", "2026-08-01", "all", "{}", "100", "100", "0", "50",
             "{}", 1, "scheduled_paused",
             datetime.now(timezone.utc).isoformat()))
        db.commit()
        assert clear_pauses_that_no_longer_qualify(db) == 1
