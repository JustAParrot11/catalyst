"""Paper/live account mode: the switch to real money must be an explicit
human choice made in the setup UI, threaded end to end - never a config
edit, never a default, never something the bot flips.

Sabotage log (house rule 4):
- base_url_for_mode 'live' branch redirected to PAPER_BASE_URL: caught
  by test_live_mode_targets_the_live_endpoint. Restored, green.
- save path defaulting an invalid mode to 'live': caught by
  test_invalid_mode_refused_and_nothing_saved. Restored, green.
"""

import json

import pytest

from catalyst.execution.broker import (
    LIVE_BASE_URL, PAPER_BASE_URL, base_url_for_mode,
)
from catalyst.setup import first_run
from catalyst.setup.first_run import SetupApp, render_setup_page


class TestBaseUrl:
    def test_live_mode_targets_the_live_endpoint(self):
        assert base_url_for_mode("paper") == PAPER_BASE_URL
        assert base_url_for_mode("live") == LIVE_BASE_URL
        assert "paper" in PAPER_BASE_URL and "paper" not in LIVE_BASE_URL

    def test_unknown_mode_refuses_to_guess(self):
        with pytest.raises(ValueError):
            base_url_for_mode("prod")
        with pytest.raises(ValueError):
            base_url_for_mode("")


class TestSetupForm:
    def test_selector_renders_with_paper_default_and_real_money_warning(self):
        page = render_setup_page()
        assert 'name="account_mode"' in page
        assert 'value="paper" checked' in page        # paper is the default
        assert "REAL MONEY" in page                   # live is labelled loudly
        assert "your explicit choice" in page

    def _app(self, tmp_path, tester_log):
        def alpaca_tester(key, secret, **kwargs):
            tester_log.append(kwargs)
            return True, "ok"

        return SetupApp(
            credentials_path=str(tmp_path / "creds.json"),
            alpaca_tester=alpaca_tester,
            anthropic_tester=lambda k: (True, "ok"),
            require_token=False)

    def _save(self, app, mode=None):
        form = {"alpaca_key": "PKTESTFAKEFAKEFAKE",
                "alpaca_secret": "sekritsekritsekritsekrit",
                "anthropic_key": "sk-ant-fake-fake-fake",
                "monthly_budget_usd": "5"}
        if mode is not None:
            form["account_mode"] = mode
        body = "&".join(f"{k}={v}" for k, v in form.items()).encode()
        return app.handle(
            "POST", "/save", body,
            {"content-type": "application/x-www-form-urlencoded"})

    def test_saved_mode_persists_and_defaults_to_paper(self, tmp_path):
        from catalyst.setup import credentials as creds
        log = []
        app = self._app(tmp_path, log)
        resp = self._save(app)                        # no mode sent at all
        assert json.loads(resp.body)["ok"] is True
        saved = creds.load_credentials(str(tmp_path / "creds.json"))
        assert saved.settings["account_mode"] == "paper"
        # paper testing carries no base_url override
        assert all("base_url" not in k for k in log)

    def test_live_mode_saved_and_tested_against_live_endpoint(self, tmp_path):
        from catalyst.setup import credentials as creds
        log = []
        app = self._app(tmp_path, log)
        resp = self._save(app, mode="live")
        assert json.loads(resp.body)["ok"] is True
        saved = creds.load_credentials(str(tmp_path / "creds.json"))
        assert saved.settings["account_mode"] == "live"
        # the connection test ran against the LIVE endpoint, not paper
        assert any(k.get("base_url") == creds.ALPACA_LIVE_BASE_URL
                   for k in log)

    def test_invalid_mode_refused_and_nothing_saved(self, tmp_path):
        from catalyst.setup import credentials as creds
        app = self._app(tmp_path, [])
        resp = self._save(app, mode="margin")
        assert json.loads(resp.body)["ok"] is False
        assert not creds.credentials_exist(str(tmp_path / "creds.json"))


class TestModeReachesClosedTrades:
    def test_cycle_labels_closed_trades_with_its_mode(self, tmp_path):
        """account_mode threads run_cycle -> _protective_duties ->
        close_filled_positions, so a live trade is never recorded as
        paper (the governor only lets LIVE profit raise the spend cap)."""
        import sqlite3
        from datetime import datetime, timezone

        from catalyst.execution.reconcile import close_filled_positions

        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("ord-buy", "cand-1", "b1", "buy", "2", "market", "day",
                      "2026-08-01T14:00:00+00:00", "filled", "{}"))
        conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                     ("ord-buy", "50.00", "2", "2026-08-01T14:00:00+00:00",
                      "50.00"))
        conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("ord-sell", "cand-1", "s1", "sell", "2", "market", "day",
                      "2026-08-09T14:00:00+00:00", "filled", "{}"))
        conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                     ("ord-sell", "55.00", "2", "2026-08-09T14:30:00+00:00",
                      "55.00"))
        conn.execute(
            "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("dec-1", "cand-1", "trade", "long", "100", "2", "45.00",
             "2026-08-13", "[]", "{}", "2026-08-01T13:00:00+00:00"))
        conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                     ("pos-1", "TEST", json.dumps(["ord-buy"]), None,
                      "2026-08-01T14:00:00+00:00", "2026-08-13", "open"))
        conn.commit()
        assert close_filled_positions(conn, account_mode="live") == 1
        assert conn.execute("SELECT account_mode FROM closed_trades"
                            ).fetchone()[0] == "live"
        conn.close()
