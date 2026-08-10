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
- top-level allowlist reverted to the old token/cache/search substring
  heuristic (cost-audit F1: "code_execution_requests" then priced itself
  at zero): caught by test_novel_top_level_billing_key_refuses.
  Restored, green.
"""

import json
import sqlite3
from datetime import date
from decimal import Decimal

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

    def test_default_http_path_issues_only_get(self, monkeypatch):
        """Behavioral, not textual (cost-audit F5): with every mutating
        httpx entry point poisoned, the adapter's DEFAULT path (no
        injected http_get) must still work - proving it never reaches
        for anything but GET."""
        import httpx as _httpx
        calls = []

        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append(url)
            return _httpx.Response(200, json=REAL_SHAPE,
                                   request=_httpx.Request("GET", url))

        def poisoned(*a, **k):
            raise AssertionError("read-only adapter used a mutating verb")

        monkeypatch.setattr(_httpx, "get", fake_get)
        for verb in ("post", "put", "patch", "delete", "request", "stream"):
            monkeypatch.setattr(_httpx, verb, poisoned)
        page = fetch_cost_api_day(date(2026, 8, 4), admin_key="k")
        assert len(page.records) == 1 and len(calls) == 1


class TestSonnet5IntroPricing:
    """Found empirically 2026-08-10: Anthropic bills Sonnet 5 at INTRO
    rates ($2/$10 per MTok) through 2026-08-31 - the owner's real-time
    console showed ~$0.21 where standard rates predicted $0.326 for the
    same verbatim usage, and intro rates predict $0.231. Rates are
    date-effective: the spend date decides, never the reprice date.

    Sabotage (house rule 4): SONNET5_INTRO_ENDS moved to 2026-07-31 in
    a copy - test_rates_flip_on_september_first failed on the August
    side. Restored, green."""

    def test_rates_flip_on_september_first(self):
        from datetime import date as _date

        from catalyst.cost.pricing import rates_for
        assert rates_for("claude-sonnet-5", _date(2026, 8, 10)) == \
            (Decimal("200"), Decimal("1000"))
        assert rates_for("claude-sonnet-5", _date(2026, 8, 31)) == \
            (Decimal("200"), Decimal("1000"))     # inclusive last day
        assert rates_for("claude-sonnet-5", _date(2026, 9, 1)) == \
            (Decimal("300"), Decimal("1500"))
        # only sonnet-5 has an intro window
        assert rates_for("claude-sonnet-4-6", _date(2026, 8, 10)) == \
            (Decimal("300"), Decimal("1500"))

    def test_reprice_uses_the_spend_date_not_the_reprice_date(self, tmp_path):
        """A September reprice of an August row must keep the August
        intro rate - otherwise every historical row silently inflates
        by 50% the day the intro window closes."""
        import json as _json
        import uuid as _uuid

        from catalyst.cost.tracker import reprice_all
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        usage = {"input_tokens": 1_000_000, "output_tokens": 0,
                 "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": 0}
        conn.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at, api_call_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(_uuid.uuid4()), _json.dumps(usage), "claude-sonnet-5",
             "manual", "v", None, "2026-08-10T12:00:00+00:00", None))
        conn.commit()
        out = reprice_all(conn)   # runs "today", whatever today is
        assert out.still_unpriced == []
        (_, _, new), = out.changes
        assert new == Decimal("200")   # 1M input tokens at the AUGUST rate
        conn.close()


class TestNightlyReconcileWiring:
    """cost-audit F2/F3/F6: the scheduler hook was untested. These pin
    closed-days-only, idempotency, the check_failed record on failure,
    backfill of missed days, and the clean no-admin-key skip."""

    def _run(self, tmp_path, monkeypatch, fetch, admin_key="sk-admin"):
        from types import SimpleNamespace

        import catalyst.cost.cost_api as cost_api_mod
        import catalyst.setup.credentials as creds_mod
        from catalyst.orchestrator.scheduler import _maybe_reconcile_yesterday
        monkeypatch.setattr(
            creds_mod, "load_credentials",
            lambda *a, **k: SimpleNamespace(anthropic_admin_key=admin_key))
        monkeypatch.setattr(cost_api_mod, "fetch_cost_api_day", fetch)
        db_file = str(tmp_path / "sched.db")
        conn = sqlite3.connect(db_file)
        conn.executescript(open("catalyst/storage/schema.sql").read())
        conn.close()
        _maybe_reconcile_yesterday(db_file)
        return db_file

    @staticmethod
    def _empty_page(target_date, admin_key=None):
        from catalyst.cost.tracker import CostApiPage
        return CostApiPage(records=[], has_more=False,
                           raw_response={"data": [], "has_more": False})

    def _rows(self, db_file):
        conn = sqlite3.connect(db_file)
        rows = conn.execute(
            "SELECT target_date, action_taken FROM cost_reconciliation_events "
            "ORDER BY target_date").fetchall()
        conn.close()
        return rows

    def test_no_admin_key_skips_cleanly(self, tmp_path, monkeypatch):
        def explode(*a, **k):
            raise AssertionError("fetched without an admin key")
        db_file = self._run(tmp_path, monkeypatch, explode, admin_key="")
        assert self._rows(db_file) == []

    def test_backfills_oldest_missing_day_one_per_cycle(
            self, tmp_path, monkeypatch):
        from datetime import datetime, timedelta, timezone

        from catalyst.orchestrator.scheduler import (
            RECONCILE_BACKFILL_DAYS, _maybe_reconcile_yesterday,
        )
        calls = []

        def fetch(target_date, admin_key=None):
            calls.append(target_date)
            return self._empty_page(target_date)

        db_file = self._run(tmp_path, monkeypatch, fetch)
        today = datetime.now(timezone.utc).date()
        oldest = today - timedelta(days=RECONCILE_BACKFILL_DAYS)
        assert calls == [oldest]              # one day per cycle, oldest first
        for _ in range(RECONCILE_BACKFILL_DAYS * 2):
            _maybe_reconcile_yesterday(db_file)
        rows = self._rows(db_file)
        # fully caught up: every closed day in the window, NEVER today
        assert len(rows) == RECONCILE_BACKFILL_DAYS
        assert rows[-1][0] == (today - timedelta(days=1)).isoformat()
        assert all(d < today.isoformat() for d, _ in rows)
        # idempotent: reconciled days are not re-fetched
        n = len(calls)
        _maybe_reconcile_yesterday(db_file)
        assert len(calls) == n

    def test_failure_lands_a_check_failed_row_and_retries(
            self, tmp_path, monkeypatch):
        from catalyst.orchestrator.scheduler import _maybe_reconcile_yesterday
        calls = []

        def broken(target_date, admin_key=None):
            calls.append(target_date)
            raise CostApiError("cost_report answered HTTP 403",
                               status_code=403, body="key revoked")

        db_file = self._run(tmp_path, monkeypatch, broken)   # never raises
        rows = self._rows(db_file)
        assert len(rows) == 1 and rows[0][1] == "check_failed"
        conn = sqlite3.connect(db_file)
        raw = conn.execute(
            "SELECT api_raw_response FROM cost_reconciliation_events"
        ).fetchone()[0]
        conn.close()
        assert "key revoked" in raw           # house rule 3: raw error beside it
        # a failed day is retried, not marked done - and not row-spammed
        _maybe_reconcile_yesterday(db_file)
        assert len(calls) == 2
        assert len(self._rows(db_file)) == 1


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
        from catalyst.cost.tracker import (
            make_usage_components, price, record_usage,
        )
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        ev = record_usage(dict(self.REAL_USAGE), "claude-sonnet-5",
                          "manual", "verify", conn)
        assert ev.priced_cents is not None and ev.priced_cents > 0
        # record_usage must price at the rate in force on ITS OWN
        # priced_at date; the rate values per date are pinned in
        # TestSonnet5IntroPricing (hardcoding cents here would flip when
        # the intro window ends on 2026-09-01)
        assert ev.priced_cents == price(
            make_usage_components(dict(self.REAL_USAGE)),
            "claude-sonnet-5", on_date=ev.priced_at.date())
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

    def test_web_fetch_counter_is_informational_zero_cost(self):
        from datetime import date as _date
        from decimal import Decimal

        from catalyst.cost.pricing import WEB_SEARCH_CENTS_PER_QUERY
        from catalyst.cost.tracker import make_usage_components, price
        # date-pinned to the day the spend actually happened (intro
        # window): 24094 in @ 200c/MTok + 2271 out @ 1000c/MTok + 2
        # searches. This is the row Anthropic really billed.
        spend_day = _date(2026, 8, 10)
        expected = ((Decimal(24094) * 200 + Decimal(2271) * 1000)
                    / 1_000_000) + 2 * WEB_SEARCH_CENTS_PER_QUERY
        got = price(make_usage_components(dict(self.REAL_SEARCH_USAGE)),
                    "claude-sonnet-5", on_date=spend_day)
        assert got == expected
        # a NONZERO fetch count must still price identically - fetch is
        # free; only the searches are charged
        fetched = dict(self.REAL_SEARCH_USAGE)
        fetched["server_tool_use"] = {"web_fetch_requests": 7,
                                      "web_search_requests": 2}
        assert price(make_usage_components(fetched), "claude-sonnet-5",
                     on_date=spend_day) == expected

    def test_novel_top_level_billing_key_refuses(self, tmp_path):
        """cost-audit F1: the old substring heuristic let a hypothetical
        new billed key ("code_execution_requests") price itself at ZERO.
        The top level is an allowlist now - any unrecognized key refuses,
        after the row is recorded."""
        import pytest as _pytest

        from catalyst.cost.tracker import (
            UnrecognizedUsageFieldError, record_usage,
        )
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        novel = {"input_tokens": 10, "output_tokens": 5,
                 "code_execution_requests": 400}
        with _pytest.raises(UnrecognizedUsageFieldError,
                            match="code_execution_requests"):
            record_usage(novel, "claude-sonnet-5", "manual", "v", conn)
        assert conn.execute("SELECT priced_cents FROM cost_events"
                            ).fetchone()[0] is None
        conn.close()

    def test_non_standard_service_tier_refuses_to_price(self, tmp_path):
        """cost-audit F7: batch is discounted and priority is a premium;
        a non-standard tier must never bill at the standard rate
        silently. Nothing in this codebase requests those tiers today."""
        import pytest as _pytest

        from catalyst.cost.tracker import (
            ServiceTierUnpricedError, record_usage,
        )
        conn = sqlite3.connect(tmp_path / "t.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        for tier in ("batch", "priority"):
            usage = dict(self.REAL_USAGE)
            usage["service_tier"] = tier
            with _pytest.raises(ServiceTierUnpricedError):
                record_usage(usage, "claude-sonnet-5", "manual", "v", conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM cost_events WHERE priced_cents IS NULL"
        ).fetchone()[0] == 2
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
