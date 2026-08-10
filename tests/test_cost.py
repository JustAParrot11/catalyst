"""Stage-3 tests: pricing arithmetic against TRAPS.md's rules, governor
caps, ledger separation, and reconciliation behavior. Fully offline -
the Cost API transport is a fixture function, never a network call.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.cost import CostEstimate, CostEvent
from catalyst.cost.governor import (
    BASE_CAP_CENTS,
    MANUAL_SPEND_CAP_CENTS_PER_MONTH,
    authorize,
)
from catalyst.cost.ledger import month_to_date_cents, net_realized_profit_cents_this_month
from catalyst.cost.pricing import UnknownModelError
from catalyst.cost.tracker import (
    RECONCILE_THRESHOLD_CENTS,
    make_usage_components,
    price,
    reconcile_day,
    record_usage,
)

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)


def usage_fixture(**overrides):
    base = dict(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_creation_input_tokens=500_000,
        cache_read_input_tokens=2_000_000,
    )
    base.update(overrides)
    raw = dict(base)
    raw["server_tool_use"] = {"web_search_requests": overrides.get("web_search_requests", 0)}
    raw.pop("web_search_requests", None)
    return make_usage_components(raw)


class TestPricing:
    def test_known_arithmetic_sonnet(self):
        # 1M in @ 300c + 0.1M out @ 1500c/M + 0.5M cache-write @ 300*1.25
        # + 2M cache-read @ 300*0.10 + 3 searches @ 1c
        usage = usage_fixture(web_search_requests=3)
        expected = (
            Decimal("300")
            + Decimal("150")
            + Decimal("500000") * Decimal("300") * Decimal("1.25") / Decimal("1000000")
            + Decimal("2000000") * Decimal("300") * Decimal("0.10") / Decimal("1000000")
            + Decimal("3")
        )
        assert price(usage, "claude-sonnet-4-6") == expected

    def test_every_component_moves_the_price(self):
        """The TRAPS.md regression: a pricing path that ignores cache or
        search fields understates the bill by half. Each field must
        independently change the total."""
        base = price(usage_fixture(), "claude-sonnet-4-6")
        for field in ("input_tokens", "output_tokens",
                      "cache_creation_input_tokens", "cache_read_input_tokens"):
            bumped = price(usage_fixture(**{field: 5_000_000}), "claude-sonnet-4-6")
            assert bumped != base, f"{field} does not affect price - TRAPS.md violation"
        with_search = price(usage_fixture(web_search_requests=10), "claude-sonnet-4-6")
        assert with_search == base + Decimal("10")

    def test_unknown_model_is_loud_never_zero(self):
        with pytest.raises(UnknownModelError):
            price(usage_fixture(), "claude-renamed-model-v9")

    def test_cache_fields_parsed_from_raw_usage_object(self):
        raw = {
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_input_tokens": 7, "cache_read_input_tokens": 9,
            "server_tool_use": {"web_search_requests": 2},
        }
        u = make_usage_components(raw)
        assert (u.cache_creation_input_tokens, u.cache_read_input_tokens,
                u.web_search_requests) == (7, 9, 2)
        assert u.raw == raw  # verbatim, not reconstructed


class TestGovernor:
    def estimate(self, cents, kind="scheduled"):
        return CostEstimate(estimated_cents=Decimal(cents), basis="test",
                            kind=kind, component="research")

    def test_scheduled_allows_under_base_cap(self, tmp_db):
        d = authorize(self.estimate(400), tmp_db, as_of=TODAY)
        assert d.authorized and d.cap_cents == BASE_CAP_CENTS

    def test_scheduled_denies_over_cap_with_shortfall(self, tmp_db):
        record_usage({"input_tokens": 0, "output_tokens": 0}, "claude-sonnet-4-6",
                     "scheduled", "research", tmp_db)
        tmp_db.execute("UPDATE cost_events SET priced_cents = '450'")
        tmp_db.commit()
        d = authorize(self.estimate(100), tmp_db, as_of=TODAY)
        assert not d.authorized
        assert d.shortfall_cents == Decimal("50")
        assert d.reason == "cap_exceeded"

    def test_denial_is_logged_never_silent(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO cost_events (id, raw_usage_json, kind, component, priced_cents, priced_at, api_call_id) "
            "VALUES (?, '{}', 'scheduled', 'research', '600', ?, NULL)",
            (str(uuid.uuid4()), NOW.isoformat()),
        )
        authorize(self.estimate(10), tmp_db, as_of=TODAY)
        rows = tmp_db.execute(
            "SELECT decision, reason FROM cost_governor_events"
        ).fetchall()
        assert ("deny", "cap_exceeded") in rows

    def test_manual_has_its_own_cap_not_unbounded(self, tmp_db):
        d = authorize(self.estimate(int(MANUAL_SPEND_CAP_CENTS_PER_MONTH) + 1, kind="manual"),
                      tmp_db, as_of=TODAY)
        assert not d.authorized
        assert d.cap_cents == MANUAL_SPEND_CAP_CENTS_PER_MONTH

    def test_manual_spend_does_not_consume_scheduled_cap(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO cost_events (id, raw_usage_json, kind, component, priced_cents, priced_at, api_call_id) "
            "VALUES (?, '{}', 'manual', 'backtest_judgement', '1900', ?, NULL)",
            (str(uuid.uuid4()), NOW.isoformat()),
        )
        d = authorize(self.estimate(400), tmp_db, as_of=TODAY)
        assert d.authorized, "manual spend leaked into the scheduled cap"
        assert d.period_to_date_cents == Decimal("0")

    def test_cap_grows_only_on_net_positive_month(self, tmp_db):
        # one $50 winner and one $80 loser: net -$30 -> no cap growth
        # (ARCHITECTURE section 9.13)
        tmp_db.execute(
            "INSERT INTO positions (id, ticker, entry_order_ids, stop_order_id, opened_at, planned_exit_date, status) "
            "VALUES ('p1','X','[]',NULL,?,?,'closed'), ('p2','Y','[]',NULL,?,?,'closed')",
            (NOW.isoformat(), TODAY.isoformat(), NOW.isoformat(), TODAY.isoformat()),
        )
        tmp_db.execute(
            "INSERT INTO closed_trades (position_id, entry_price, exit_price, exit_reason, "
            "realized_pnl_cents, expected_holding_days, actual_holding_days, closed_at) "
            "VALUES ('p1','10','15','stop',5000,5,5,?), ('p2','20','12','stop',-8000,5,5,?)",
            (NOW.isoformat(), NOW.isoformat()),
        )
        tmp_db.commit()
        assert net_realized_profit_cents_this_month(tmp_db, TODAY) == Decimal("0")
        d = authorize(self.estimate(10), tmp_db, as_of=TODAY)
        assert d.cap_cents == BASE_CAP_CENTS

    def test_cap_grows_by_share_of_net_profit(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO positions (id, ticker, entry_order_ids, stop_order_id, opened_at, planned_exit_date, status) "
            "VALUES ('p1','X','[]',NULL,?,?,'closed')",
            (NOW.isoformat(), TODAY.isoformat()),
        )
        tmp_db.execute(
            "INSERT INTO closed_trades (position_id, entry_price, exit_price, exit_reason, "
            "realized_pnl_cents, expected_holding_days, actual_holding_days, closed_at) "
            "VALUES ('p1','10','15','planned_exit',10000,5,5,?)",
            (NOW.isoformat(),),
        )
        tmp_db.commit()
        d = authorize(self.estimate(10), tmp_db, as_of=TODAY,
                      governor_profit_share=Decimal("0.10"))
        assert d.cap_cents == BASE_CAP_CENTS + Decimal("1000")  # $5 + 10% of $100


class TestLedger:
    def test_kinds_never_pooled(self, tmp_db):
        for kind, cents in (("scheduled", "100"), ("manual", "700")):
            tmp_db.execute(
                "INSERT INTO cost_events (id, raw_usage_json, kind, component, priced_cents, priced_at, api_call_id) "
                "VALUES (?, '{}', ?, 'x', ?, ?, NULL)",
                (str(uuid.uuid4()), kind, cents, NOW.isoformat()),
            )
        tmp_db.commit()
        assert month_to_date_cents("scheduled", tmp_db, TODAY) == Decimal("100")
        assert month_to_date_cents("manual", tmp_db, TODAY) == Decimal("700")


class TestReconciliation:
    def seed_local(self, conn, cents="100"):
        conn.execute(
            "INSERT INTO cost_events (id, raw_usage_json, kind, component, priced_cents, priced_at, api_call_id) "
            "VALUES (?, '{}', 'scheduled', 'research', ?, ?, NULL)",
            (str(uuid.uuid4()), cents,
             datetime.combine(YESTERDAY, datetime.min.time(), timezone.utc).isoformat()),
        )
        conn.commit()

    def test_refuses_unclosed_day(self, tmp_db):
        with pytest.raises(ValueError, match="whole days"):
            reconcile_day(TODAY, "scheduled", "research", tmp_db, lambda d: [])

    def test_amounts_parsed_as_decimal_string_cents(self, tmp_db):
        self.seed_local(tmp_db, "100")
        result = reconcile_day(
            YESTERDAY, "scheduled", "research", tmp_db,
            lambda d: [{"kind": "scheduled", "component": "research", "amount": "100.00"}],
        )
        assert result.discrepancy_cents == Decimal("0")
        assert result.action_taken == "none"

    def test_large_discrepancy_pauses_scheduled_spend(self, tmp_db):
        self.seed_local(tmp_db, "100")
        result = reconcile_day(
            YESTERDAY, "scheduled", "research", tmp_db,
            lambda d: [{"kind": "scheduled", "component": "research",
                        "amount": str(Decimal("100") + RECONCILE_THRESHOLD_CENTS + 1)}],
        )
        assert result.action_taken == "scheduled_paused"
        d = authorize(
            CostEstimate(estimated_cents=Decimal("1"), basis="t",
                         kind="scheduled", component="research"),
            tmp_db, as_of=TODAY,
        )
        assert not d.authorized
        assert d.reason == "reconciliation_discrepancy_unacknowledged"
        # manual spend is NOT paused by a scheduled discrepancy
        m = authorize(
            CostEstimate(estimated_cents=Decimal("1"), basis="t",
                         kind="manual", component="backtest_judgement"),
            tmp_db, as_of=TODAY,
        )
        assert m.authorized

    def test_per_kind_comparison_catches_cancelling_errors(self, tmp_db):
        """A scheduled underpricing and a manual overpricing that cancel
        in aggregate must NOT reconcile clean (ARCHITECTURE section 7.1)."""
        self.seed_local(tmp_db, "100")  # scheduled/research local=100
        api_day = [
            {"kind": "scheduled", "component": "research", "amount": "200"},
            {"kind": "manual", "component": "research", "amount": "0"},
        ]
        result = reconcile_day(YESTERDAY, "scheduled", "research", tmp_db, lambda d: api_day)
        assert result.discrepancy_cents == Decimal("100")
        assert result.action_taken == "scheduled_paused"
