"""The maintenance page: is everything communicating?

Owner request 2026-08-10: one place that says whether each moving part
is online - the bot itself and each outside service.

Two invariants matter more than the layout:

1. NO PROBE MAY COST MONEY. The ordinary Anthropic key can only be
   proved live by sending a message, and that is charged against a
   ceiling of a few dollars a month. A health page that quietly spends
   the trading budget every time it is opened would be a defect, not a
   feature - so that key is reported from the ledger instead.
2. NO PROBE MAY RAISE. A maintenance page that dies when a service is
   down is the opposite of useful; the page must render the outage.

Sabotage log (house rule 4) at the bottom.
"""

import sqlite3

import pytest

from catalyst.dashboard import maintenance, panels
from catalyst.dashboard.db import Db
from catalyst.storage import init_db


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "m.db")
    init_db(path).close()
    d = Db(path)
    yield d
    d.close()


class Creds:
    def __init__(self, **kw):
        self.alpaca_key = kw.get("alpaca_key", "PKFAKE")
        self.alpaca_secret = kw.get("alpaca_secret", "SEC")
        self.anthropic_key = kw.get("anthropic_key", "sk-ant-fake")
        self.anthropic_admin_key = kw.get("anthropic_admin_key", "sk-ant-admin")
        self.settings = {"account_mode": "paper"}


class TestNoProbeCostsMoney:
    def test_the_research_key_is_never_probed_live(self):
        """Sending a message is the only live proof, and it is billed."""
        checks = maintenance.active_checks(
            Creds(),
            alpaca_probe=lambda: (True, "ok"),
            market_data_probe=lambda: (True, "ok"),
            edgar_probe=lambda: (True, "ok"),
            admin_probe=lambda: (True, "ok"))
        research = next(c for c in checks if c.name == "Anthropic research key")
        assert research.latency_ms is None, (
            "a latency means a request was made - that request is billed")
        assert "costs real money" in research.detail

    def test_the_module_never_reaches_for_the_messages_api(self):
        src = open("catalyst/dashboard/maintenance.py").read()
        assert "v1/messages" not in src
        assert "anthropic.com/v1/messages" not in src

    def test_outside_services_are_not_contacted_unless_asked(self, db):
        """Opening the page must not fire four network requests, or a
        refresh becomes a rate-limit problem on a public API."""
        def explode():
            raise AssertionError("probed without being asked")

        report = maintenance.build_report(db, Creds(), run_active=False,
                                          )
        assert report.ran_active is False
        assert all(c.group == "The bot itself" for c in report.checks)


class TestProbesNeverRaise:
    def test_a_failing_probe_becomes_a_rendered_row(self):
        def boom():
            raise ConnectionError("name resolution failed")

        checks = maintenance.active_checks(
            Creds(), alpaca_probe=boom, market_data_probe=boom,
            edgar_probe=boom, admin_probe=boom)
        broker = next(c for c in checks if c.name == "Alpaca (your broker)")
        assert broker.state == "fail"
        assert "name resolution failed" in broker.summary
        assert broker.raw, "house rule 3: the raw error belongs beside it"

    def test_missing_credentials_read_as_not_set_up_not_broken(self):
        checks = maintenance.active_checks(
            Creds(alpaca_key="", alpaca_secret="", anthropic_admin_key=""),
            edgar_probe=lambda: (True, "reachable"))
        broker = next(c for c in checks if c.name == "Alpaca (your broker)")
        assert broker.state == "unknown"
        assert "fail" != broker.state
        # EDGAR needs no key at all, so it is still checked
        edgar = next(c for c in checks if "EDGAR" in c.name)
        assert edgar.state == "ok"

    def test_a_broken_database_is_reported_not_raised(self, tmp_path):
        bad = Db(str(tmp_path / "does-not-exist" / "x.db"))
        report = maintenance.build_report(bad, None, run_active=False)
        assert report.worst in ("fail", "warn", "unknown")
        html = panels.maintenance_panel(report)
        assert "Database" in html


class TestPassiveChecks:
    def test_a_stale_feed_is_flagged(self, db):
        conn = sqlite3.connect(db.path)
        conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                     ("edgar", "acc-1", "2026-01-01T00:00:00+00:00", "{}"))
        conn.commit(); conn.close()
        checks = maintenance.passive_checks(db)
        feed = next(c for c in checks if "Filing feed" in c.name)
        assert feed.state == "warn"
        assert "ago" in feed.summary

    def test_unpriced_cost_rows_are_a_hard_problem(self, db):
        conn = sqlite3.connect(db.path)
        conn.execute("INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
                     ("e1", "{}", "claude-sonnet-5", "scheduled", "research",
                      None, "2026-08-10T12:00:00+00:00", None))
        conn.commit(); conn.close()
        checks = maintenance.passive_checks(db)
        ledger = next(c for c in checks if c.name == "Cost ledger complete")
        assert ledger.state == "fail"
        assert "not priced" in ledger.summary

    def test_a_clean_new_install_is_not_reported_as_broken(self, db):
        """Nothing has run yet. That is 'not set up', never 'problem' -
        crying wolf on a fresh install teaches the owner to ignore it."""
        checks = maintenance.passive_checks(db)
        assert not any(c.state == "fail" for c in checks), \
            [c.name for c in checks if c.state == "fail"]


class TestRendering:
    def test_page_groups_the_bot_and_the_outside_world(self, db):
        report = maintenance.build_report(
            db, Creds(), run_active=True,
            alpaca_probe=lambda: (True, "account reachable"),
            market_data_probe=lambda: (True, "SPY bar returned"),
            edgar_probe=lambda: (True, "reachable"),
            admin_probe=lambda: (True, "bill readable"))
        html = panels.maintenance_panel(report)
        assert "The bot itself" in html and "Outside services" in html
        assert "account reachable" in html
        assert "Check outside services now" in html
        # states carry a word, never colour alone
        assert "online" in html

    def test_every_check_says_what_it_means(self, db):
        report = maintenance.build_report(db, None, run_active=False)
        for c in report.checks:
            assert c.detail, f"{c.name} has no plain-English explanation"


@pytest.mark.sabotage
class TestSabotage:
    """Verified by breaking a copy:
    - the research-key check given a live probe (latency set): caught by
      test_the_research_key_is_never_probed_live.
    - _timed's except clause removed so a probe raises: caught by
      test_a_failing_probe_becomes_a_rendered_row.
    """

    def test_timed_swallows_and_reports(self):
        def boom():
            raise RuntimeError("kaboom")
        ok, message, raw, ms = maintenance._timed(boom)
        assert ok is False and "kaboom" in message and ms >= 0


class TestQueriesActuallyRun:
    """Found by rendering the page: two checks were querying columns
    that do not exist (research_calls.at, kill_switch_events.at). The
    page honestly showed the SQL error - house rule 3 working - but the
    checks were useless. Nothing here may report a query error."""

    def test_no_passive_check_reports_a_query_error(self, db):
        """Only a genuine fault may carry a raw error. A fresh install
        with nothing fetched yet must produce none - this assertion
        originally treated 'no bars downloaded yet' as a failure, which
        passed on a machine that had run fetch_history and failed on a
        real server, rolling back the owner's upgrade."""
        for c in maintenance.passive_checks(db):
            assert not c.raw, f"{c.name} reported an error: {c.raw}"

    def test_a_missing_bar_cache_is_not_reported_as_a_fault(
            self, db, tmp_path, monkeypatch):
        """data/ is gitignored, so EVERY fresh install starts with no
        benchmark file. That is 'not set up yet', never a problem."""
        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "definitely-absent"))
        check = next(c for c in maintenance.passive_checks(db)
                     if c.name == "S&P benchmark data")
        assert check.state == "unknown"
        assert not check.raw
        assert "not fetched yet" in check.summary

    def test_a_populated_bar_cache_reads_as_online(
            self, db, tmp_path, monkeypatch):
        from datetime import date, timedelta
        from decimal import Decimal

        from catalyst.backtest.data import Bar, BarCache

        root = tmp_path / "bars"
        today = date.today()
        BarCache(str(root)).write_bars("SPY", [
            Bar(day=today - timedelta(days=i), open=Decimal("1"),
                high=Decimal("1"), low=Decimal("1"), close=Decimal("1"),
                volume=Decimal("1"))
            for i in range(3)])
        monkeypatch.setenv("CATALYST_BARS", str(root))
        check = next(c for c in maintenance.passive_checks(db)
                     if c.name == "S&P benchmark data")
        assert check.state == "ok"
        assert "3 daily bars" in check.summary

    def test_element_ids_are_unique_across_both_tables(self, db):
        """Both tables started their row numbering at zero, so the raw
        disclosures collided - the exact failure duplicate_ids() exists
        to catch."""
        from catalyst.dashboard.render import duplicate_ids

        checks = maintenance.passive_checks(db)
        for c in checks:
            c.raw = "boom"                      # force every disclosure open
            c.group = "Outside services" if checks.index(c) % 2 else "The bot itself"
        report = maintenance.MaintenanceReport(checks=checks, ran_active=True,
                                               generated_at="now")
        assert duplicate_ids(panels.maintenance_panel(report)) == []


class TestDiagnosticExport:
    """Owner asked where to export logs to send back. The bundle existed
    from stage 6 but had NO way in from the UI and no route at all when
    the page itself is down - which is exactly when it is wanted."""

    def test_the_page_offers_a_download(self, db):
        report = maintenance.build_report(db, None, run_active=False)
        html = panels.maintenance_panel(report)
        assert 'href="/diagnostics.json"' in html
        assert 'download="catalyst-diagnostics.json"' in html
        assert "keys and secrets are stripped" in html.replace("\n", " ")

    def test_the_page_names_the_offline_command(self, db):
        report = maintenance.build_report(db, None, run_active=False)
        html = panels.maintenance_panel(report)
        assert "--diagnostics" in html, (
            "the bundle must be obtainable when the page is the broken "
            "thing, and the page should say how")

    def test_cli_prints_a_redacted_bundle_without_a_server(self, db, capsys):
        import json as _json

        from catalyst.dashboard.server import main

        rc = main(["--diagnostics", "--db", db.path])
        assert rc == 0
        payload = _json.loads(capsys.readouterr().out)
        assert payload["build_hash"]
        assert "maintenance_checks" in payload
        assert payload["note"]

    def test_bundle_carries_the_maintenance_summary(self, db):
        from catalyst.dashboard.server import diagnostics_bundle

        bundle = diagnostics_bundle(db)
        names = [c.get("name") for c in bundle["maintenance_checks"]]
        assert "Database" in names
        assert "Cost ledger complete" in names

    def test_a_planted_credential_never_survives_the_bundle(self, db):
        """The bundle is meant to be sent to a stranger."""
        import sqlite3

        from catalyst.dashboard.server import diagnostics_bundle

        planted = "sk-ant-FAKE-0000000000000000"
        conn = sqlite3.connect(db.path)
        conn.executescript(
            open("catalyst/dashboard/schema_logs.sql").read())
        conn.execute(
            "INSERT INTO logs (ts, level, component, message) VALUES (?,?,?,?)",
            ("2026-08-10T12:00:00+00:00", "ERROR", "research",
             f"call failed with {planted}"))
        conn.commit(); conn.close()
        import json as _json
        assert planted not in _json.dumps(diagnostics_bundle(Db(db.path)),
                                          default=str)


# ==========================================================================
# "Saved OK" and "not found" at the same time.
#
# Owner-reported 2026-08-10: "I added the admin API key and it said ok.
# But in maintenance it says not found, I need a definitive way and
# evidence it has correctly accepted it after I enter it."
#
# A boolean cannot settle that - both screens can render a boolean from
# different files or different moments, and neither is checkable. A
# fingerprint can.
# ==========================================================================


class TestStoredCredentialEvidence:
    def _configured(self, tmp_path, monkeypatch, admin="sk-ant-admin01-xyz"):
        from catalyst.setup import credentials as creds
        path = tmp_path / "creds.json"
        monkeypatch.setenv("CATALYST_CREDENTIALS", str(path))
        creds.save_credentials("PKAAAAAAAAAAAAAAAAAA", "sssssssssssssssssss",
                               "sk-ant-aaaaaaaaaaaaaaaa", "tok",
                               anthropic_admin_key=admin, path=str(path))
        return path

    def test_a_stored_key_reports_a_fingerprint_without_any_network(
            self, tmp_path, monkeypatch):
        from catalyst.dashboard.maintenance import stored_credentials_checks
        from catalyst.setup.credentials import fingerprint

        self._configured(tmp_path, monkeypatch)
        checks = {c.name: c for c in stored_credentials_checks()}
        admin = checks["Anthropic billing key (admin) - stored?"]
        assert admin.state == "ok"
        assert fingerprint("sk-ant-admin01-xyz") in admin.summary

    def test_the_fingerprint_never_contains_the_key(self, tmp_path, monkeypatch):
        from catalyst.dashboard.maintenance import stored_credentials_checks

        from catalyst.setup.credentials import fingerprint

        secret = "sk-ant-admin01-SUPERSECRETVALUE99"
        self._configured(tmp_path, monkeypatch, admin=secret)
        blob = " ".join(f"{c.summary} {c.detail} {c.raw}"
                        for c in stored_credentials_checks())
        assert secret not in blob

        # THE INVARIANT, stated precisely: the fingerprint must not be a
        # PIECE of the key. Checking fixed-length chunks missed a
        # fingerprint defined as key[:8], because 8 < the chunk size -
        # the first version of this test passed against that sabotage.
        fp = fingerprint(secret)
        assert fp, "a stored key must produce a fingerprint"
        assert fp not in secret, (
            f"the fingerprint {fp!r} is a substring of the key itself")
        assert len(fp) == 8 and all(ch in "0123456789abcdef" for ch in fp), (
            "a hex digest, not an excerpt")

    def test_the_save_echoes_the_same_fingerprint_the_page_will_show(
            self, tmp_path, monkeypatch):
        """This is the whole point: two short strings the owner compares.
        If they match, the key typed is the key the bot has."""
        import json as _json

        from catalyst.dashboard.maintenance import stored_credentials_checks
        from catalyst.setup.first_run import SetupApp

        path = tmp_path / "creds.json"
        monkeypatch.setenv("CATALYST_CREDENTIALS", str(path))
        app = SetupApp(credentials_path=str(path),
                       alpaca_tester=lambda k, s, **kw: (True, "ok"),
                       anthropic_tester=lambda k: (True, "ok"),
                       admin_tester=lambda k: (True, "ok"),
                       require_token=False)
        admin = "sk-ant-admin01-" + "m" * 30
        body = ("alpaca_key=PKAAAAAAAAAAAAAAAAAA&alpaca_secret=ssssssssssssssssss"
                "&anthropic_key=sk-ant-aaaaaaaaaaaaaaaa"
                f"&anthropic_admin_key={admin}&monthly_budget_usd=5").encode()
        resp = _json.loads(app.handle(
            "POST", "/save", body,
            {"content-type": "application/x-www-form-urlencoded"}).body)
        assert resp["ok"], resp
        echoed = resp["fingerprints"]["anthropic_admin_key"]
        assert echoed and echoed in resp["message"]

        shown = {c.name: c for c in stored_credentials_checks()}
        assert echoed in shown["Anthropic billing key (admin) - stored?"].summary

    def test_a_missing_key_says_not_stored_rather_than_nothing(
            self, tmp_path, monkeypatch):
        from catalyst.dashboard.maintenance import stored_credentials_checks

        self._configured(tmp_path, monkeypatch, admin="")
        checks = {c.name: c for c in stored_credentials_checks()}
        admin = checks["Anthropic billing key (admin) - stored?"]
        assert admin.state == "unknown"
        assert "not stored" in admin.summary

    def test_an_unreadable_file_is_never_reported_as_nothing_entered(
            self, tmp_path, monkeypatch):
        """The defect that made the owner's two screens disagree without
        either being obviously wrong."""
        from catalyst.dashboard.maintenance import stored_credentials_checks

        path = self._configured(tmp_path, monkeypatch)
        path.write_text("{ this is not json")
        checks = stored_credentials_checks()
        assert checks[0].state == "fail"
        assert "could not be read" in checks[0].summary
        assert "NOT the same as having entered nothing" in checks[0].detail
        assert not any("not stored" in c.summary for c in checks)

    def test_a_fresh_machine_is_not_reported_as_broken(
            self, tmp_path, monkeypatch):
        from catalyst.dashboard.maintenance import stored_credentials_checks

        monkeypatch.setenv("CATALYST_CREDENTIALS", str(tmp_path / "nope.json"))
        checks = stored_credentials_checks()
        assert checks[0].state == "unknown"
        assert "not been completed" in checks[0].summary

    def test_the_evidence_needs_no_probe_and_appears_on_a_plain_load(
            self, tmp_path, monkeypatch):
        """It used to appear only if you clicked "check now", which runs
        live probes. "Did it save" must not require a network call."""
        from catalyst.dashboard import maintenance
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        self._configured(tmp_path, monkeypatch)
        dbf = str(tmp_path / "m.db")
        init_db(dbf).close()
        report = maintenance.build_report(Db(dbf), None, run_active=False)
        names = [c.name for c in report.checks]
        assert "Anthropic billing key (admin) - stored?" in names
