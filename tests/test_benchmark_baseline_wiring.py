"""Swapping the Alpaca keys must restart the S&P comparison.

OWNER-ASKED, 2026-08-14: "when I change the Alpaca keys i want it to
register there is a new account and restart the SPY tracker."

The failure this prevents is silent, which is why it is worth a file of
its own. Point the bot at a $2,000 account while the benchmark is still
struck against the old $1,000 one and every performance figure compares
the new account's profit against the old account's starting money. The
page keeps drawing, nothing errors, and the number is wrong in whichever
direction happens to flatter or damn.

What is tested here:

1. the baseline is reconciled against the broker account on every cycle,
   from a confirmed read, and does nothing when the account is the same;
2. a genuine change is announced in English an owner can read, with both
   account fingerprints;
3. the SPY series is brought up to date at that moment rather than
   tomorrow - and the bar HISTORY is not thrown away doing it;
4. saving or replacing credentials on the setup page makes the bot check
   straight away instead of on the next quarter-hour;
5. nothing secret reaches the baseline row.

Fully offline: the broker is a fake object, the benchmark refresh is
injected, and no socket is opened (conftest blocks them anyway).

Sabotage log (house rule 4) is at the bottom of this file.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta

import pytest

from catalyst import benchmark
from catalyst.orchestrator import scheduler
from catalyst.storage import init_db

# Planted, fake, and never real. If one of these ever appears in a
# database row or a log line, the test that found it found a leak.
FAKE_ALPACA_KEY = "PKFAKE123456789TEST"
FAKE_ALPACA_SECRET = "fakealpacasecret0000000000000000TESTONLY"
FAKE_ANTHROPIC_KEY = "sk-ant-fake0000000000000000000000TESTONLY"

ACCOUNT_ONE = "8f1c0d2e-1111-4a1b-9999-aaaaaaaaaaaa"
ACCOUNT_TWO = "3b7e9f4a-2222-4c2d-8888-bbbbbbbbbbbb"


def account(account_id: str, equity: str) -> dict:
    """The fields of Alpaca's /v2/account that matter here. No PDT
    fields: they were removed from the API in July 2026 (TRAPS.md)."""
    return {"id": account_id, "account_number": "PA123456",
            "equity": equity, "cash": equity, "status": "ACTIVE",
            "buying_power": equity}


class FakeBroker:
    """Everything `_sync_benchmark_baseline` is allowed to touch."""

    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.reads = 0

    def get_account(self) -> dict:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return dict(self.payload)

    def close(self) -> None:
        pass


@pytest.fixture
def conn(tmp_path):
    connection = init_db(str(tmp_path / "catalyst.db"))
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _no_real_refresh(monkeypatch):
    """The SPY refresh is network code. Every test in this file replaces
    it; the list it appends to is what the test asserts on."""
    calls: list[dict] = []

    def fake(state, *, force=False):
        calls.append({"force": force, "state": state})

    monkeypatch.setattr(scheduler, "_maybe_refresh_benchmark", fake)
    return calls


def rows(conn) -> list[tuple]:
    return conn.execute(
        "SELECT capital_cents, start_date, source, account_fingerprint, "
        "reason FROM benchmark_baselines ORDER BY set_at, rowid").fetchall()


# ==========================================================================
# 1. Detecting the account, every cycle, from a confirmed read
# ==========================================================================


class TestTheAccountIsReconciledEveryCycle:
    def test_the_first_broker_read_strikes_the_baseline(self, conn):
        broker = FakeBroker(account(ACCOUNT_ONE, "1000"))

        changed = scheduler._sync_benchmark_baseline(conn, broker)

        assert changed is True
        current = benchmark.current(conn)
        assert current.source == "first_run"
        assert current.capital_cents == 100000
        assert current.account_fingerprint == \
            benchmark.fingerprint_account(ACCOUNT_ONE)
        assert not current.is_placeholder

    def test_the_same_account_next_cycle_writes_nothing(self, conn):
        broker = FakeBroker(account(ACCOUNT_ONE, "1000"))
        scheduler._sync_benchmark_baseline(conn, broker)

        for _ in range(5):
            assert scheduler._sync_benchmark_baseline(conn, broker) is False

        assert len(rows(conn)) == 1, (
            "a routine cycle wrote a baseline row for an account that had "
            "not changed - the history becomes noise and the append-only "
            "audit trail stops meaning anything")
        assert broker.reads == 6, "the account is read once per cycle"

    def test_a_swapped_account_restarts_the_comparison(self, conn):
        """The owner's actual move: $1,000 paper account to a $2,000 one."""
        scheduler._sync_benchmark_baseline(
            conn, FakeBroker(account(ACCOUNT_ONE, "1000")))

        changed = scheduler._sync_benchmark_baseline(
            conn, FakeBroker(account(ACCOUNT_TWO, "2000")))

        assert changed is True
        current = benchmark.current(conn)
        assert current.source == "account_changed"
        assert current.capital_cents == 200000, (
            "the new account's own equity must be what SPY is bought with; "
            "anything else compares two different pots of money")
        assert current.account_fingerprint == \
            benchmark.fingerprint_account(ACCOUNT_TWO)

    def test_the_old_baseline_stays_in_the_history(self, conn):
        scheduler._sync_benchmark_baseline(
            conn, FakeBroker(account(ACCOUNT_ONE, "1000")))
        scheduler._sync_benchmark_baseline(
            conn, FakeBroker(account(ACCOUNT_TWO, "2000")))

        history = rows(conn)
        assert len(history) == 2, "the replaced baseline was deleted"
        assert history[0][0] == "100000" and history[0][2] == "first_run"
        assert history[1][0] == "200000" and history[1][2] == "account_changed"

    def test_a_broker_that_will_not_answer_leaves_the_baseline_alone(
            self, conn):
        from catalyst.execution.broker import BrokerError

        scheduler._sync_benchmark_baseline(
            conn, FakeBroker(account(ACCOUNT_ONE, "1000")))
        before = rows(conn)

        broken = FakeBroker(error=BrokerError("HTTP 503 on GET /v2/account"))
        assert scheduler._sync_benchmark_baseline(conn, broken) is False
        assert rows(conn) == before, (
            "a failed read invented a baseline - striking a comparison from "
            "a number nobody could read is worse than keeping the old one")

    def test_a_broker_object_without_the_method_never_breaks_the_cycle(
            self, conn):
        """A stub, a partially-built client, a future refactor. Reporting
        must not be able to take the trading loop down."""
        class NotReallyABroker:
            def close(self):
                pass

        assert scheduler._sync_benchmark_baseline(
            conn, NotReallyABroker()) is False

    def test_an_unmigrated_database_is_reported_not_raised(self, tmp_path,
                                                           caplog):
        """The benchmark table arrives with an upgrade. A database that
        predates it must log the problem, not kill the pass."""
        bare = sqlite3.connect(str(tmp_path / "old.db"))
        try:
            with caplog.at_level(logging.INFO):
                assert scheduler._sync_benchmark_baseline(
                    bare, FakeBroker(account(ACCOUNT_ONE, "1000"))) is False
            assert any(r.levelno >= logging.ERROR for r in caplog.records), (
                "a baseline that could not be written said nothing at all")
        finally:
            bare.close()


# ==========================================================================
# 2. Saying so, in English, to somebody who is not a developer
# ==========================================================================


class TestTheChangeIsAnnouncedInPlainEnglish:
    def _swap_and_capture(self, conn, caplog) -> str:
        scheduler._sync_benchmark_baseline(
            conn, FakeBroker(account(ACCOUNT_ONE, "1000")))
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="catalyst.scheduler"):
            scheduler._sync_benchmark_baseline(
                conn, FakeBroker(account(ACCOUNT_TWO, "2000")))
        return "\n".join(r.getMessage() for r in caplog.records
                         if r.levelno == logging.INFO)

    def test_it_is_logged_at_info_with_both_fingerprints(self, conn, caplog):
        message = self._swap_and_capture(conn, caplog)

        assert benchmark.fingerprint_account(ACCOUNT_ONE) in message, (
            "the old account fingerprint is missing, so nobody can tell "
            "which comparison was replaced")
        assert benchmark.fingerprint_account(ACCOUNT_TWO) in message

    def test_it_reads_as_an_explanation_rather_than_an_event_code(
            self, conn, caplog):
        message = self._swap_and_capture(conn, caplog)

        for phrase in ("your broker account has changed",
                       "what it means", "started again", "not lost"):
            assert phrase in message.lower(), (
                f"the announcement never says {phrase!r}. The owner is not "
                "a developer; 'source=account_changed' is an event code, "
                "not an explanation")
        assert "$2,000.00" in message, (
            "the new baseline is not stated as money")

    def test_it_quotes_the_reason_stored_on_the_row(self, conn, caplog):
        """The log and the page must agree, so the log quotes the row
        rather than paraphrasing it."""
        message = self._swap_and_capture(conn, caplog)
        assert benchmark.current(conn).reason in message

    def test_the_first_ever_strike_is_not_announced_as_a_change(
            self, conn, caplog):
        with caplog.at_level(logging.INFO, logger="catalyst.scheduler"):
            scheduler._sync_benchmark_baseline(
                conn, FakeBroker(account(ACCOUNT_ONE, "1000")))
        message = "\n".join(r.getMessage() for r in caplog.records)

        assert "has changed" not in message.lower(), (
            "a first connection is not a swapped account, and telling the "
            "owner their account changed on day one is a false alarm")
        assert "first time" in message.lower()

    def test_nothing_secret_is_ever_in_the_announcement_or_the_row(
            self, conn, caplog):
        with caplog.at_level(logging.INFO, logger="catalyst.scheduler"):
            scheduler._sync_benchmark_baseline(
                conn, FakeBroker(account(ACCOUNT_ONE, "1000")))
            scheduler._sync_benchmark_baseline(
                conn, FakeBroker(account(ACCOUNT_TWO, "2000")))
        message = "\n".join(r.getMessage() for r in caplog.records)
        stored = json.dumps(rows(conn))

        for secret in (FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                       FAKE_ANTHROPIC_KEY, ACCOUNT_ONE, ACCOUNT_TWO):
            assert secret not in message, f"{secret!r} reached the log"
            assert secret not in stored, (
                f"{secret!r} reached the database. The fingerprint is a hash "
                "precisely so no identifier has to be stored - and this row "
                "goes into diagnostic bundles")


# ==========================================================================
# 3. Restarting the SPY tracker - and not destroying it
# ==========================================================================


class TestTheSpyTrackerRestarts:
    def test_a_new_account_refreshes_the_series_the_same_day(
            self, conn, _no_real_refresh):
        """The daily guard must not swallow the restart. The new baseline
        indexes from TODAY, and a cache that last updated on Friday has
        nothing in today's window to index against."""
        today_marked = {"benchmark_day": date.today()}

        scheduler._sync_benchmark_baseline(
            conn, FakeBroker(account(ACCOUNT_ONE, "1000")), today_marked)

        assert len(_no_real_refresh) == 1, (
            "the SPY series was not refreshed when the comparison "
            "restarted, so the restarted window has stale bars in it")
        assert _no_real_refresh[0]["force"] is True, (
            "the refresh was requested without force, so today's marker "
            "silently skips it")
        assert _no_real_refresh[0]["state"] is today_marked

    def test_an_unchanged_account_does_not_refetch_anything(
            self, conn, _no_real_refresh):
        broker = FakeBroker(account(ACCOUNT_ONE, "1000"))
        scheduler._sync_benchmark_baseline(conn, broker)
        _no_real_refresh.clear()

        scheduler._sync_benchmark_baseline(conn, broker)

        assert _no_real_refresh == [], (
            "an ordinary cycle re-fetched the whole benchmark series")

    def test_the_bar_history_is_kept_across_a_restart(self, conn, tmp_path,
                                                      monkeypatch):
        """THE FINDING, made mechanical: the bar cache needs no
        invalidation. The bars are raw SPY closes on a pinned basis, true
        whichever account is connected - only the day the comparison is
        INDEXED from moves. Wiping them to restart the comparison would
        destroy history an unentitled account cannot re-fetch.
        """
        from decimal import Decimal

        from catalyst.backtest.data import Bar, BarCache
        from catalyst.data.benchmark import BENCHMARK_SYMBOL

        root = tmp_path / "bars"
        cache = BarCache(str(root))
        history = [
            Bar(day=date(2026, 8, 3) + timedelta(days=i),
                open=Decimal("500"), high=Decimal("505"), low=Decimal("495"),
                close=Decimal(500 + i), volume=Decimal("1000"))
            for i in range(5)]
        cache.write_bars(BENCHMARK_SYMBOL, history)
        monkeypatch.setenv("CATALYST_BARS", str(root))

        scheduler._sync_benchmark_baseline(
            conn, FakeBroker(account(ACCOUNT_TWO, "2000")))

        kept = list(BarCache(str(root)).load_bars(BENCHMARK_SYMBOL))
        assert [b.day for b in kept] == [b.day for b in history], (
            "the SPY price history was discarded when the account changed. "
            "A new baseline moves the index start; it does not make "
            "yesterday's SPY close untrue")


# ==========================================================================
# 4. The cycle actually calls it, and the setup page shortcuts the wait
# ==========================================================================


class TestTheWiring:
    def test_the_trading_cycle_syncs_the_baseline(self, tmp_path, monkeypatch):
        """Wired at the SCHEDULER, off its own broker read - cycle.py is
        risk code under human review and is deliberately untouched."""
        import catalyst.execution.broker as broker_mod
        import catalyst.orchestrator.cycle as cycle_mod
        from catalyst.setup import credentials as creds

        monkeypatch.setenv("CATALYST_CREDENTIALS", str(tmp_path / "creds.json"))
        monkeypatch.setenv("CATALYST_SERVICE_USER",
                           "catalyst-does-not-exist-in-tests")
        creds.save_credentials(FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                               FAKE_ANTHROPIC_KEY, "fake-token-0000000",
                               settings={"account_mode": "paper"})

        class WiredBroker:
            def __init__(self, *a, **kw):
                pass

            def get_account(self):
                return account(ACCOUNT_TWO, "2000")

            def close(self):
                pass

        monkeypatch.setattr(broker_mod, "Broker", WiredBroker)
        monkeypatch.setattr(cycle_mod, "run_cycle",
                            lambda *a, **kw: "report")

        db_file = str(tmp_path / "cycle.db")
        init_db(db_file).close()
        scheduler._run_one_cycle(db_file, {})

        checked = sqlite3.connect(db_file)
        try:
            stored = benchmark.current(checked)
        finally:
            checked.close()
        assert stored.account_fingerprint == \
            benchmark.fingerprint_account(ACCOUNT_TWO), (
            "a whole cycle ran without ever asking which account it was "
            "trading - a key swap would go unnoticed indefinitely")
        assert stored.capital_cents == 200000

    def test_saving_credentials_wakes_the_scheduler(self):
        scheduler._wake.clear()
        scheduler._credentials_changed("alpaca")
        assert scheduler._wake.is_set(), (
            "the loop still sleeps out its full cycle after a key change, "
            "so the owner sees the old account for up to fifteen minutes")
        scheduler._wake.clear()

    def test_the_setup_page_is_built_with_the_listener_attached(self):
        """A source check, because the alternative is binding port 8000."""
        from pathlib import Path

        src = Path(scheduler.__file__).read_text()
        assert "setup_app=SetupApp(" in src
        assert "on_credentials_changed=_credentials_changed" in src, (
            "the setup page is mounted without the listener, so saving new "
            "keys tells the running bot nothing")


class TestTheSetupPageAnnouncesChanges:
    def _app(self, tmp_path, seen, **kw):
        from catalyst.setup.first_run import SetupApp

        defaults = dict(
            credentials_path=str(tmp_path / "creds.json"),
            alpaca_tester=lambda k, s, **kwargs: (True, "Connected."),
            anthropic_tester=lambda k: (True, "Connected."),
            admin_tester=lambda k: (True, "Connected."),
            # the installer's access code is a separate concern, covered
            # in test_install.py; this file is about the listener
            require_token=False,
            on_credentials_changed=seen.append)
        defaults.update(kw)
        return SetupApp(**defaults)

    def _post(self, app, route, payload):
        return app.handle("POST", route, json.dumps(payload).encode(),
                          {"content-type": "application/json"})

    def test_the_first_save_announces_a_change(self, tmp_path):
        seen: list[str] = []
        response = self._post(self._app(tmp_path, seen), "/save", {
            "alpaca_key": FAKE_ALPACA_KEY,
            "alpaca_secret": FAKE_ALPACA_SECRET,
            "anthropic_key": FAKE_ANTHROPIC_KEY,
            "monthly_budget_usd": "5", "account_mode": "paper"})

        assert response.json()["ok"] is True
        assert seen == ["all"]

    def test_replacing_only_the_broker_keys_announces_a_change(self, tmp_path):
        """THE OWNER'S ACTUAL ROUTE. On a configured machine the page
        offers "Replace the broker keys", which never fires on_saved -
        so a $1,000 to $2,000 swap arrived with nothing listening."""
        seen: list[str] = []
        app = self._app(tmp_path, seen)
        self._post(app, "/save", {
            "alpaca_key": FAKE_ALPACA_KEY,
            "alpaca_secret": FAKE_ALPACA_SECRET,
            "anthropic_key": FAKE_ANTHROPIC_KEY,
            "monthly_budget_usd": "5", "account_mode": "paper"})
        seen.clear()

        response = self._post(app, "/replace-key", {
            "which": "alpaca",
            "alpaca_key": "PKFAKE987654321TEST",
            "alpaca_secret": "fakealpacasecret1111111111111111TESTONLY"})

        assert response.json()["ok"] is True, response.json()["message"]
        assert seen == ["alpaca"]

    def test_a_refused_replacement_announces_nothing(self, tmp_path):
        seen: list[str] = []
        app = self._app(tmp_path, seen)
        self._post(app, "/save", {
            "alpaca_key": FAKE_ALPACA_KEY,
            "alpaca_secret": FAKE_ALPACA_SECRET,
            "anthropic_key": FAKE_ANTHROPIC_KEY,
            "monthly_budget_usd": "5", "account_mode": "paper"})
        seen.clear()
        app.alpaca_tester = lambda k, s, **kw: (False, 'Alpaca said: "no".')

        response = self._post(app, "/replace-key", {
            "which": "alpaca", "alpaca_key": "PKNOPE", "alpaca_secret": "x"})

        assert response.json()["ok"] is False
        assert seen == [], (
            "nothing was saved, but the bot was told the account changed")

    def test_a_listener_that_explodes_never_breaks_the_save(self, tmp_path):
        def boom(_which):
            raise RuntimeError("the listener is on fire")

        app = self._app(tmp_path, [], on_credentials_changed=boom)
        response = self._post(app, "/save", {
            "alpaca_key": FAKE_ALPACA_KEY,
            "alpaca_secret": FAKE_ALPACA_SECRET,
            "anthropic_key": FAKE_ANTHROPIC_KEY,
            "monthly_budget_usd": "5", "account_mode": "paper"})

        assert response.json()["ok"] is True, (
            "a broken listener turned a successful save into a failure on "
            "the owner's screen - the keys ARE saved at that point")

    def test_the_listener_is_never_handed_a_credential(self, tmp_path):
        seen: list[str] = []
        self._post(self._app(tmp_path, seen), "/save", {
            "alpaca_key": FAKE_ALPACA_KEY,
            "alpaca_secret": FAKE_ALPACA_SECRET,
            "anthropic_key": FAKE_ANTHROPIC_KEY,
            "monthly_budget_usd": "5", "account_mode": "paper"})

        for value in seen:
            for secret in (FAKE_ALPACA_KEY, FAKE_ALPACA_SECRET,
                           FAKE_ANTHROPIC_KEY):
                assert secret not in value, (
                    "the callback carries a secret into a stack frame, and "
                    "from there into any traceback")


# ==========================================================================
# Sabotage log - house rule 4: a test that cannot fail is not a test.
# Each check below was verified by breaking a copy and confirming the
# named test failed, then restoring it. See SABOTAGE-LOG.md.
# ==========================================================================
