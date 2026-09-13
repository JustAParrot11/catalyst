"""A feed that answered and had nothing to say has RECOVERED.

OWNER-REPORTED 2026-09-13, on the Pipeline page, about a Saturday:

    NEEDS ATTENTION
    2  Insider trades (SEC Form 4) could not be read, 2 times since
       2026-09-12T17:02
       the server returned a web page instead of data (titled
       "SEC.gov | File Unavailable") - an error or maintenance page at
       the source, not a fault at this end.

The SENTENCE was right - that fix shipped the day before. What was
wrong is that the row was still under NEEDS ATTENTION more than a day
later, for a feed that was working perfectly.

MEASURED AGAINST THE REAL SEC BEFORE CHANGING ANYTHING (2026-09-13):

    Friday's daily index   -> HTTP 200, real data
    Saturday's index       -> HTTP 403 + AccessDenied  (routine absence,
                              already handled by the feed)
    an absent filing       -> HTTP 404 + NoSuchKey     (also handled)

So "SEC.gov | File Unavailable" is a genuine transient outage page, and
two occurrences in ~108 weekend attempts is a blip that was already
over. The defect is that nothing could say so:

    SELECT COUNT(*) FROM raw_events WHERE source = ? AND fetched_at > ?

is how the panel decided a fault was resolved - "did this feed produce
ROWS since it failed?" At a weekend the Form 4 feed correctly produces
ZERO rows, because EDGAR publishes no daily index. A successful read
that yields nothing wrote **no row anywhere**: only failures were
recorded. So a Saturday-evening outage could not be marked recovered
until EDGAR next published on the Monday.

"It answered" and "it had something to say" are different facts, and
only one of them was being recorded. That is CLAUDE.md's "routine
attrition must not look like damage", third instance.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard.db import Db

NOW = datetime.now(timezone.utc)


def _ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


@pytest.fixture
def db(tmp_path):
    """init_db, NOT a raw connect - the fixture must match production or
    it will agree with a bug (docs/WHAT-WE-TRIED.md section 14)."""
    from catalyst.storage import init_db

    path = str(tmp_path / "t.db")
    conn = init_db(path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()
    return path


def _err(path, source, hours_ago, text="SEC.gov | File Unavailable"):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO raw_events_errors (source, attempted_at, "
            "error_text) VALUES (?,?,?)", (source, _ago(hours_ago), text))
        conn.commit()
    finally:
        conn.close()


def _read(path, source, hours_ago, items):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO feed_reads (source, read_at, item_count) "
            "VALUES (?,?,?)", (source, _ago(hours_ago), items))
        conn.commit()
    finally:
        conn.close()


def _event(path, source, hours_ago, sid):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO raw_events (source, source_id, fetched_at, "
            "payload_raw) VALUES (?,?,?,?)",
            (source, sid, _ago(hours_ago), "{}"))
        conn.commit()
    finally:
        conn.close()


def _form4(data):
    return [f for f in data if "Form 4" in f[0]]


class TestTheOwnersWeekend:
    """The exact shape of the report: a failure, then the feed answering
    with nothing, for a day and a half."""

    def test_a_read_returning_zero_items_clears_the_fault(self, db):
        """THE BUG. Before this, an empty read left the fault standing,
        so a working feed reported as broken all weekend."""
        from catalyst.dashboard import queries

        _err(db, "edgar_form4", 30)
        _err(db, "edgar_form4", 29)
        # Every cycle since has answered. EDGAR has no Saturday index, so
        # every one of them correctly returned nothing.
        for hours in (28, 24, 12, 1):
            _read(db, "edgar_form4", hours, 0)

        data = queries.funnel(Db(db))
        assert _form4(data.feed_faults) == [], (
            "a feed that has answered four times since it failed is still "
            "under NEEDS ATTENTION, which is what the owner reported:\n"
            + "\n".join(f[0] for f in data.feed_faults))
        healed = _form4(data.feed_healed)
        assert healed, "and it is not in the recovered list either"
        assert "answered" in healed[0][2], healed[0][2]

    def test_a_fault_with_no_read_since_is_still_attention_worthy(self, db):
        """THE GUARD MUST STILL GUARD. A feed that failed and has not
        answered since is exactly what the panel is for, and the fix
        must not turn it off."""
        from catalyst.dashboard import queries

        _err(db, "edgar_form4", 2)
        # A read BEFORE the failure proves nothing about now.
        _read(db, "edgar_form4", 5, 0)

        data = queries.funnel(Db(db))
        assert _form4(data.feed_faults), (
            "a feed that has not answered since it failed must stay under "
            "NEEDS ATTENTION")
        assert _form4(data.feed_healed) == []

    def test_another_feeds_read_does_not_clear_this_feeds_fault(self, db):
        from catalyst.dashboard import queries

        _err(db, "edgar_form4", 2)
        _read(db, "alpaca_news", 1, 40)

        data = queries.funnel(Db(db))
        assert _form4(data.feed_faults), (
            "the news feed answering cleared the Form 4 fault")


class TestTheOldTestIsKeptAsAFallback:
    """A failure recorded before feed_reads existed has no read row to
    find. An upgraded database must not suddenly show every historic
    fault as unresolved."""

    def test_rows_produced_since_still_resolve_a_fault(self, db):
        from catalyst.dashboard import queries

        _err(db, "edgar_form4", 5)
        _event(db, "edgar_form4", 4, "acc-1")

        data = queries.funnel(Db(db))
        assert _form4(data.feed_faults) == []
        healed = _form4(data.feed_healed)
        assert healed
        assert "returned data" in healed[0][2], healed[0][2]

    def test_the_two_routes_say_which_one_answered(self, db):
        """"Alpaca answered" and "rows exist" are different evidence and
        the panel should not present them as the same sentence."""
        from catalyst.dashboard import queries

        _err(db, "edgar_form4", 5)
        _read(db, "edgar_form4", 4, 0)
        _err(db, "alpaca_news", 5)
        _event(db, "alpaca_news", 4, "news-1")

        healed = {h[0]: h[2] for h in queries.funnel(Db(db)).feed_healed}
        form4 = [v for k, v in healed.items() if "Form 4" in k]
        news = [v for k, v in healed.items() if "Alpaca" in k]
        assert form4 and "answered" in form4[0]
        assert news and "returned data" in news[0]


class TestTheSchedulerRecordsTheAnswer:
    """Asserting the OUTCOME by running a real cycle, not by grepping the
    source. Two changes in a row had a call-site grep walk straight past a
    sabotage (docs/WHAT-WE-TRIED.md sections 22 and 24), so this drives
    scheduler._run_one_cycle with run_cycle stubbed to CALL the injected
    feed function - which is the closure under test.
    """

    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        import catalyst.execution.broker as broker_mod
        import catalyst.orchestrator.cycle as cycle_mod
        from catalyst.setup import credentials as creds
        from catalyst.storage import init_db

        monkeypatch.setenv("CATALYST_CREDENTIALS",
                           str(tmp_path / "creds.json"))
        monkeypatch.setenv("CATALYST_SERVICE_USER",
                           "catalyst-does-not-exist-in-tests")
        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
        creds.save_credentials("PKTESTTESTTESTTEST01", "s" * 40,
                               "sk-ant-" + "x" * 40, "tok-0000000000",
                               settings={"account_mode": "paper"})

        class StubBroker:
            def __init__(self, *a, **kw):
                pass

            def get_account(self):
                return {"id": "acct-1", "account_number": "PA1",
                        "equity": "2000", "buying_power": "2000",
                        "cash": "2000"}

            def close(self):
                pass

        monkeypatch.setattr(broker_mod, "Broker", StubBroker)

        # run_cycle is replaced by something that does exactly one thing:
        # call the feed the scheduler handed it. That makes the closure
        # run for real rather than being described.
        called = {}

        def _call_the_feed(conn, broker, transport, feed_fetch, *a, **kw):
            from datetime import datetime, timedelta, timezone as _tz

            end = datetime.now(_tz.utc)
            called["events"] = feed_fetch(end - timedelta(days=1), end)
            return type("R", (), {"errors": [], "funnel": {},
                                  "drop_reasons": {}})()

        monkeypatch.setattr(cycle_mod, "run_cycle", _call_the_feed)

        db_file = str(tmp_path / "cycle.db")
        init_db(db_file).close()
        return db_file, called, monkeypatch

    @staticmethod
    def _reads(db_file):
        conn = sqlite3.connect(db_file)
        try:
            return list(conn.execute(
                "SELECT source, item_count FROM feed_reads"))
        finally:
            conn.close()

    @staticmethod
    def _errors(db_file):
        conn = sqlite3.connect(db_file)
        try:
            return [r[0] for r in conn.execute(
                "SELECT source FROM raw_events_errors")]
        finally:
            conn.close()

    def test_a_pass_that_found_nothing_still_records_that_it_answered(
            self, wired):
        """THE WHOLE POINT. A weekend Form 4 pass finds nothing, and that
        has to be recorded or the dashboard cannot tell it from broken."""
        from catalyst.data.sources import edgar_form4 as f4
        from catalyst.orchestrator import scheduler

        db_file, called, monkeypatch = wired
        monkeypatch.setattr(f4, "fetch_form4", lambda *a, **kw: type(
            "G", (), {"events": [], "requests_made": 1, "from_cache": 0,
                      "index_days_from_cache": 2})())
        scheduler._run_one_cycle(db_file, {})

        assert "events" in called, "the stub never reached the feed"
        form4 = [r for r in self._reads(db_file) if r[0] == "edgar_form4"]
        assert form4 == [("edgar_form4", 0)], (
            "a Form 4 pass that answered with nothing recorded no read, so "
            f"the dashboard cannot tell it from broken. Got: "
            f"{self._reads(db_file)}")

    def test_a_rate_limit_block_records_the_failure_and_NOT_a_read(
            self, wired):
        """THE TRAP THIS FIX HAD TO AVOID, found by reading the scheduler
        before writing the recorder.

        On RateLimitBlocked the scheduler records an error and then
        `return []` - which run_cycle sees as a perfectly successful empty
        fetch. Recording the read in run_cycle would have cleared the
        fault written one line above it, every single time. So the read is
        recorded per-feed HERE, where the truth is known."""
        from catalyst.data.sources import edgar_form4 as f4
        from catalyst.orchestrator import scheduler

        db_file, _called, monkeypatch = wired

        def blocked(*_a, **_kw):
            raise f4.RateLimitBlocked("sec.gov blocked this IP")

        monkeypatch.setattr(f4, "fetch_form4", blocked)
        scheduler._run_one_cycle(db_file, {})

        assert "edgar_form4" in self._errors(db_file), (
            "a rate-limit block recorded no failure")
        assert [r for r in self._reads(db_file) if r[0] == "edgar_form4"] \
            == [], (
            "a BLOCKED Form 4 pass recorded a successful read, which would "
            "clear the fault it had just written")
