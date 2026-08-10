"""Production Cost API adapter + nightly reconciliation wiring.

The fixture shapes below are captured VERBATIM from the real Cost API
(2026-08-10, live admin key): buckets of results whose "amount" is a
decimal string in CENTS ("525.64452" beside token volumes pricing to
~$5-6 - the dollars reading would be 100x off). TRAPS.md holds.

Sabotage log (house rule 4):
- adapter flattening changed to sum buckets instead of results (records
  = data): caught by test_flattens_buckets_for_reconcile (KeyError
  'amount' surfaced as CostApiError refusal). Restored, green.
- missing-amount refusal removed (unreadable record treated as zero):
  caught by test_record_without_amount_refused. Restored, green.
"""

import json
import sqlite3
from datetime import date

import httpx
import pytest

from catalyst.cost.cost_api import (
    PAGE_LIMIT, CostApiError, fetch_cost_api_day,
)
from catalyst.cost.tracker import reconcile_day

REAL_SHAPE = {
    "data": [
        {"starting_at": "2026-08-04T00:00:00Z",
         "ending_at": "2026-08-05T00:00:00Z",
         "results": [
             {"currency": "USD", "amount": "525.64452", "workspace_id": None,
              "description": None, "cost_type": None, "context_window": None,
              "model": None, "service_tier": None, "token_type": None,
              "inference_geo": None}]},
    ],
    "has_more": False,
    "next_page": None,
}


def http_get_returning(payload, status=200, capture=None):
    def get(url, headers=None, params=None, timeout=None):
        if capture is not None:
            capture.update({"url": url, "params": params,
                            "header_names": sorted(headers or {})})
        return httpx.Response(
            status, json=payload if status == 200 else None,
            text=None if status == 200 else "denied",
            request=httpx.Request("GET", url))
    return get


class TestAdapter:
    def test_flattens_buckets_for_reconcile(self, tmp_path):
        page = fetch_cost_api_day(date(2026, 8, 4), admin_key="k",
                                  http_get=http_get_returning(REAL_SHAPE))
        assert len(page.records) == 1
        assert page.records[0]["amount"] == "525.64452"
        assert page.has_more is False
        # and the reconciler consumes it as CENTS end to end
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        r = reconcile_day(date(2026, 8, 4), conn, lambda d: page)
        assert str(r.cost_api_total_cents) == "525.64452"
        assert r.action_taken != "none"      # empty local ledger disagrees
        conn.close()

    def test_explicit_limit_and_no_key_leak_in_url(self):
        seen = {}
        fetch_cost_api_day(date(2026, 8, 4), admin_key="SECRETKEY",
                           http_get=http_get_returning(REAL_SHAPE,
                                                       capture=seen))
        assert seen["params"]["limit"] == PAGE_LIMIT   # TRAPS.md: explicit
        assert "SECRETKEY" not in seen["url"]
        assert seen["params"]["starting_at"] == "2026-08-04T00:00:00Z"
        assert seen["params"]["ending_at"] == "2026-08-05T00:00:00Z"

    def test_no_admin_key_refuses_plainly(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_ADMIN_KEY", raising=False)
        with pytest.raises(CostApiError, match="admin key"):
            fetch_cost_api_day(date(2026, 8, 4),
                               http_get=http_get_returning(REAL_SHAPE))

    def test_non_200_raises_with_raw_body(self):
        with pytest.raises(CostApiError) as e:
            fetch_cost_api_day(date(2026, 8, 4), admin_key="k",
                               http_get=http_get_returning(None, status=403))
        assert e.value.status_code == 403
        assert "denied" in e.value.body

    def test_record_without_amount_refused(self):
        bad = {"data": [{"results": [{"currency": "USD"}]}],
               "has_more": False}
        with pytest.raises(CostApiError, match="amount"):
            fetch_cost_api_day(date(2026, 8, 4), admin_key="k",
                               http_get=http_get_returning(bad))

    def test_empty_day_is_a_clean_zero(self, tmp_path):
        empty = {"data": [{"starting_at": "x", "ending_at": "y",
                           "results": []}], "has_more": False}
        page = fetch_cost_api_day(date(2026, 8, 9), admin_key="k",
                                  http_get=http_get_returning(empty))
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        r = reconcile_day(date(2026, 8, 9), conn, lambda d: page)
        assert r.action_taken == "none" and r.cost_api_total_cents == 0
        conn.close()


class TestReadOnlyGuarantee:
    def test_adapter_module_can_only_get(self):
        """The owner's instruction: never touch spend limits. The module
        must contain no POST/PUT/PATCH/DELETE call at all."""
        src = open("catalyst/cost/cost_api.py").read()
        for verb in ("httpx.post", "httpx.put", "httpx.patch",
                     "httpx.delete", '"POST"', '"PUT"', '"PATCH"',
                     '"DELETE"'):
            assert verb not in src, f"read-only module contains {verb}"


class TestAdminKeyInSetup:
    def _app(self, tmp_path, admin_result=(True, "ok")):
        from catalyst.setup.first_run import SetupApp
        return SetupApp(
            credentials_path=str(tmp_path / "c.json"),
            alpaca_tester=lambda k, s, **kw: (True, "ok"),
            anthropic_tester=lambda k: (True, "ok"),
            admin_tester=lambda k: admin_result,
            require_token=False)

    def _save(self, app, admin=""):
        form = {"alpaca_key": "PKFAKE", "alpaca_secret": "SECFAKE",
                "anthropic_key": "sk-ant-fake",
                "anthropic_admin_key": admin,
                "monthly_budget_usd": "5", "account_mode": "paper"}
        body = "&".join(f"{k}={v}" for k, v in form.items()).encode()
        return app.handle("POST", "/save", body,
                          {"content-type": "application/x-www-form-urlencoded"})

    def test_blank_admin_key_is_fine(self, tmp_path):
        from catalyst.setup import credentials as creds
        resp = self._save(self._app(tmp_path))
        assert json.loads(resp.body)["ok"] is True
        assert creds.load_credentials(
            str(tmp_path / "c.json")).anthropic_admin_key == ""

    def test_admin_key_saved_when_it_tests_ok(self, tmp_path):
        from catalyst.setup import credentials as creds
        resp = self._save(self._app(tmp_path), admin="sk-ant-admin-fake")
        assert json.loads(resp.body)["ok"] is True
        assert creds.load_credentials(
            str(tmp_path / "c.json")).anthropic_admin_key == "sk-ant-admin-fake"

    def test_bad_admin_key_saves_nothing(self, tmp_path):
        from catalyst.setup import credentials as creds
        resp = self._save(self._app(tmp_path, admin_result=(False, "nope")),
                          admin="sk-ant-admin-bad")
        assert json.loads(resp.body)["ok"] is False
        assert not creds.credentials_exist(str(tmp_path / "c.json"))


class TestLiveUsageShape2026_08_10:
    """The VERBATIM usage object the real Messages API returned on the
    first live call (2026-08-10). The tracker refused to price it until
    output_tokens_details was reviewed - which is the record-first
    discipline doing its job - and prices it now. thinking_tokens is a
    breakdown of output_tokens (already billed there), never additional."""

    REAL_USAGE = {
        "input_tokens": 11, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 0,
                           "ephemeral_1h_input_tokens": 0},
        "output_tokens": 4,
        "output_tokens_details": {"thinking_tokens": 0},
        "service_tier": "standard", "inference_geo": "global",
    }

    def test_real_shape_prices_cleanly(self, tmp_path):
        from decimal import Decimal

        from catalyst.cost.tracker import record_usage
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        ev = record_usage(dict(self.REAL_USAGE), "claude-sonnet-5",
                          "manual", "verify", conn)
        assert ev.priced_cents is not None
        # 11 in @ 300c/MTok + 4 out @ 1500c/MTok
        expected = (Decimal(11) * 300 + Decimal(4) * 1500) / 1_000_000
        assert ev.priced_cents == expected
        conn.close()

    # VERBATIM from the first live web-search research turn (2026-08-10):
    # server_tool_use carries web_fetch_requests beside web_search_requests.
    # Web fetch is not metered per-request (informational counter); web
    # search bills at WEB_SEARCH_CENTS_PER_QUERY. The tracker refused this
    # row until reviewed - record-first discipline, again.
    REAL_SEARCH_USAGE = {
        "cache_creation": {"ephemeral_1h_input_tokens": 0,
                           "ephemeral_5m_input_tokens": 0},
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "inference_geo": "global", "input_tokens": 24094,
        "output_tokens": 2271,
        "output_tokens_details": {"thinking_tokens": 2155},
        "server_tool_use": {"web_fetch_requests": 0,
                            "web_search_requests": 2},
        "service_tier": "standard",
    }

    def test_web_fetch_counter_is_informational_zero_cost(self, tmp_path):
        from decimal import Decimal

        from catalyst.cost.pricing import WEB_SEARCH_CENTS_PER_QUERY
        from catalyst.cost.tracker import record_usage
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        ev = record_usage(dict(self.REAL_SEARCH_USAGE), "claude-sonnet-5",
                          "manual", "verify", conn)
        expected = ((Decimal(24094) * 300 + Decimal(2271) * 1500)
                    / 1_000_000) + 2 * WEB_SEARCH_CENTS_PER_QUERY
        assert ev.priced_cents == expected
        # a NONZERO fetch count must still price identically - fetch is
        # free; only the searches are charged
        fetched = dict(self.REAL_SEARCH_USAGE)
        fetched["server_tool_use"] = {"web_fetch_requests": 7,
                                      "web_search_requests": 2}
        ev2 = record_usage(fetched, "claude-sonnet-5", "manual", "v2", conn)
        assert ev2.priced_cents == expected
        conn.close()

    def test_new_key_inside_details_still_refuses(self, tmp_path):
        import pytest as _pytest

        from catalyst.cost.tracker import (
            UnrecognizedUsageFieldError, record_usage,
        )
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        mutated = dict(self.REAL_USAGE)
        mutated["output_tokens_details"] = {"thinking_tokens": 0,
                                            "mystery_tokens": 9}
        with _pytest.raises(UnrecognizedUsageFieldError):
            record_usage(mutated, "claude-sonnet-5", "manual", "v", conn)
        # recorded anyway, unpriced - the governor blocks from here
        assert conn.execute("SELECT priced_cents FROM cost_events"
                            ).fetchone()[0] is None
        conn.close()
