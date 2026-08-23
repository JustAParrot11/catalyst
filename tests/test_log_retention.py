"""Logs older than 30 days go; the evidence stays.

Owner-asked 2026-08-23: "if a log is older than 30 days, delete log".

Nothing in this system ever deleted anything. The logs table gains rows
on every cycle - 96 a day, forever - at roughly 17MB a week measured.
That is about 900MB a year, and a full disk is the one failure systemd
cannot restart out of.

The risk in a change like this is deleting too much. Most of these
tests are about what must SURVIVE.

No calendar dates (house rule 6): every fixture is relative to now.
"""

from datetime import datetime, timedelta, timezone

import pytest

from catalyst.orchestrator.retention import LOG_RETENTION_DAYS, prune_logs
from catalyst.storage import init_db

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "r.db"))
    yield conn
    conn.close()


def log(conn, days_ago, level="INFO", tb=None, msg="m"):
    conn.execute(
        "INSERT INTO logs (ts, level, component, message, traceback_text) "
        "VALUES (?,?,?,?,?)",
        ((NOW - timedelta(days=days_ago)).isoformat(), level, "c", msg, tb))
    conn.commit()


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]


class TestItDeletesWhatItShould:
    def test_a_line_older_than_the_window_goes(self, db):
        log(db, LOG_RETENTION_DAYS + 1)
        assert prune_logs(db) == 1
        assert count(db) == 0

    def test_a_line_inside_the_window_stays(self, db):
        log(db, LOG_RETENTION_DAYS - 1)
        assert prune_logs(db) == 0
        assert count(db) == 1

    def test_todays_lines_stay(self, db):
        for h in range(5):
            log(db, 0)
        prune_logs(db)
        assert count(db) == 5

    def test_it_reports_how_many_it_deleted(self, db):
        for n in range(7):
            log(db, LOG_RETENTION_DAYS + n + 1)
        assert prune_logs(db) == 7


class TestItKeepsWhatMatters:
    """The dangerous half. Deleting too much is worse than keeping too
    much, and every one of these is load-bearing."""

    def test_an_old_traceback_is_kept_regardless_of_age(self, db):
        """The thing you go looking for when something has been quietly
        wrong for five weeks. Rare, so it costs nothing to keep."""
        log(db, LOG_RETENTION_DAYS * 10, level="ERROR", tb="Traceback...")
        assert prune_logs(db) == 0
        assert count(db) == 1

    def test_the_money_ledger_is_untouched(self, db):
        """cost_events is compared against the real bill, feeds a
        30-day drift window and the governor's month-to-date. Deleting
        one loses money that was really spent."""
        db.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at) VALUES (?,?,?,?,?,?,?)",
            ("old", "{}", "claude-sonnet-5", "scheduled", "research", "100",
             (NOW - timedelta(days=400)).isoformat()))
        db.commit()
        prune_logs(db)
        assert db.execute("SELECT COUNT(*) FROM cost_events").fetchone()[0] == 1

    def test_refusals_are_untouched(self, db):
        """Scored 12 days later, and the conviction floor needs 30 of
        them before it can move. Pruning these would destroy the
        feedback loop the brief calls the most important one."""
        db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                   ("c", "AAA", "insider_cluster", "2026-08-20", "confirmed",
                    "[]", (NOW - timedelta(days=400)).isoformat(), "x", "[]"))
        db.execute(
            "INSERT INTO risk_decisions (id, candidate_id, action, "
            "skip_reasons, adaptive_params_snapshot, decided_at) "
            "VALUES (?,?,?,?,?,?)",
            ("d", "c", "skip", "[]", "{}",
             (NOW - timedelta(days=400)).isoformat()))
        db.execute(
            "INSERT INTO refusals (decision_id, candidate_id, "
            "price_at_refusal, refused_at) VALUES (?,?,?,?)",
            ("d", "c", "10", (NOW - timedelta(days=400)).isoformat()))
        db.commit()
        prune_logs(db)
        assert db.execute("SELECT COUNT(*) FROM refusals").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM risk_decisions").fetchone()[0] == 1

    def test_the_trade_record_is_untouched(self, db):
        """'Every trade must be explainable after the fact' has no
        expiry date."""
        db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                   ("c2", "BBB", "insider_cluster", "2026-01-01", "confirmed",
                    "[]", (NOW - timedelta(days=900)).isoformat(), "x", "[]"))
        db.execute(
            "INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
            ("c2", "long", 0.7, "t", "i", 12, 0, "r"))
        db.commit()
        prune_logs(db)
        assert db.execute(
            "SELECT COUNT(*) FROM research_views").fetchone()[0] == 1


class TestItCannotTakeTradingDown:
    def test_a_missing_table_returns_zero_rather_than_raising(self, db):
        """Housekeeping runs inside an unattended service. It must never
        be the reason a cycle fails."""
        db.execute("DROP TABLE logs")
        db.commit()
        assert prune_logs(db) == 0

    def test_an_empty_table_is_fine(self, db):
        assert prune_logs(db) == 0

    def test_it_is_idempotent(self, db):
        log(db, LOG_RETENTION_DAYS + 5)
        assert prune_logs(db) == 1
        assert prune_logs(db) == 0


class TestItIsWiredIntoTheScheduler:
    def test_the_daily_job_runs_and_is_once_a_day(self, tmp_path):
        from catalyst.orchestrator import scheduler

        path = str(tmp_path / "s.db")
        conn = init_db(path)
        log(conn, LOG_RETENTION_DAYS + 3)
        conn.close()

        state: dict = {}
        scheduler._maybe_prune_logs(path, state)
        conn = init_db(path)
        assert count(conn) == 0, "the scheduler never called the prune"
        log(conn, LOG_RETENTION_DAYS + 3)
        conn.close()

        # Second call the same day must be a no-op: it is a whole-table
        # scan and running it 96 times a day is wasted I/O.
        scheduler._maybe_prune_logs(path, state)
        conn = init_db(path)
        assert count(conn) == 1, "it pruned twice in one day"
        conn.close()
