"""Stage-3 tests: pricing arithmetic against TRAPS.md's rules, governor
caps, ledger separation, reconciliation behavior - and a regression test
for every finding in cost-auditor's stage-3 audit (F1-F12). Fully
offline; the Cost API transport is a fixture, never a network call.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.cost import CostEstimate
from catalyst.cost.governor import (
    BASE_CAP_CENTS,
    DEFAULT_GOVERNOR_PROFIT_SHARE,
    GOVERNOR_MAX_CAP_CENTS,
    MANUAL_LIFETIME_BUDGET_CENTS,
    MANUAL_SPEND_CAP_CENTS_PER_MONTH,
    authorize,
)
from catalyst.cost.ledger import (
    lifetime_cents,
    month_to_date_cents,
    net_realized_profit_cents_prior_month,
)
from catalyst.cost.pricing import (
    RATES_MAX_AGE_DAYS,
    RATES_VERIFIED_ON,
    UnknownModelError,
)
from catalyst.cost.tracker import (
    CostApiPage,
    TruncatedCostPageError,
    UnrecognizedUsageFieldError,
    has_unpriced_rows,
    make_usage_components,
    price,
    reconcile_day,
    record_usage,
    reprice_all,
)

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)
SHARE = DEFAULT_GOVERNOR_PROFIT_SHARE


def usage_fixture(**overrides):
    base = dict(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_creation_input_tokens=500_000,
        cache_read_input_tokens=2_000_000,
    )
    web = overrides.pop("web_search_requests", 0)
    base.update(overrides)
    raw = dict(base)
    raw["server_tool_use"] = {"web_search_requests": web}
    return make_usage_components(raw)


def insert_cost_row(conn, kind="scheduled", component="research", cents="100",
                    at=None, model="claude-sonnet-4-6", priced=True):
    conn.execute(
        "INSERT INTO cost_events (id, raw_usage_json, model, kind, component, priced_cents, priced_at, api_call_id) "
        "VALUES (?, '{}', ?, ?, ?, ?, ?, NULL)",
        (str(uuid.uuid4()), model, kind, component,
         cents if priced else None, (at or NOW).isoformat()),
    )
    conn.commit()


def insert_closed_trade(conn, pnl_cents, closed_at, mode="paper", pid=None):
    pid = pid or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO positions (id, ticker, entry_order_ids, stop_order_id, opened_at, planned_exit_date, status) "
        "VALUES (?, 'X', '[]', NULL, ?, ?, 'closed')",
        (pid, closed_at.isoformat(), closed_at.date().isoformat()),
    )
    conn.execute(
        "INSERT INTO closed_trades (position_id, account_mode, entry_price, exit_price, exit_reason, "
        "realized_pnl_cents, expected_holding_days, actual_holding_days, closed_at) "
        "VALUES (?, ?, '10', '11', 'planned_exit', ?, 5, 5, ?)",
        (pid, mode, pnl_cents, closed_at.isoformat()),
    )
    conn.commit()


def clean_page(records):
    return CostApiPage(records=records, has_more=False,
                       raw_response={"data": records})


class TestPricing:
    def test_known_arithmetic_sonnet_hand_computed_literal(self):
        # Audit suggestion: a literal a reader can eyeball, computed by
        # hand: $3 + $1.50 + $1.875 + $0.60 + $0.03 = $7.005 = 700.5c
        usage = usage_fixture(web_search_requests=3)
        assert price(usage, "claude-sonnet-4-6") == Decimal("700.50")

    def test_every_component_moves_the_price(self):
        base = price(usage_fixture(), "claude-sonnet-4-6")
        for field in ("input_tokens", "output_tokens",
                      "cache_creation_input_tokens", "cache_read_input_tokens"):
            bumped = price(usage_fixture(**{field: 5_000_000}), "claude-sonnet-4-6")
            assert bumped != base, f"{field} does not affect price - TRAPS.md violation"
        assert price(usage_fixture(web_search_requests=10), "claude-sonnet-4-6") == base + Decimal("10")

    def test_unknown_model_is_loud(self):
        with pytest.raises(UnknownModelError):
            price(usage_fixture(), "claude-renamed-model-v9")

    def test_1h_cache_writes_bill_at_2x(self):
        # Audit F3 follow-up: 1h-TTL writes at 2x, not 1.25x. 500k 1h
        # tokens at $3/M input: 500000*300*2/1M = 300c vs 187.5c at 5m.
        raw = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 500_000,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_1h_input_tokens": 500_000,
                                "ephemeral_5m_input_tokens": 0},
        }
        u = make_usage_components(raw)
        assert price(u, "claude-sonnet-4-6") == Decimal("300")

    def test_unrecognized_billing_field_is_loud(self):
        # The renamed-field trap must raise, never price at zero.
        with pytest.raises(UnrecognizedUsageFieldError):
            make_usage_components({"input_tokens": 5, "output_megatokens": 1})

    def test_rates_provenance_not_stale(self):
        verified = date.fromisoformat(RATES_VERIFIED_ON)
        age = (TODAY - verified).days
        assert age <= RATES_MAX_AGE_DAYS, (
            f"pricing table last verified {age} days ago (max {RATES_MAX_AGE_DAYS}); "
            "re-verify against the pricing page and update RATES_VERIFIED_ON"
        )


class TestRecordFirstPriceSecond:
    def test_unknown_model_still_lands_a_row(self, tmp_db):
        """Audit F2: the money is spent before we price it. A dated model
        ID missing from the table must still be recorded."""
        with pytest.raises(UnknownModelError):
            record_usage({"input_tokens": 100, "output_tokens": 50},
                         "claude-sonnet-4-5-20250929", "scheduled", "research", tmp_db)
        rows = tmp_db.execute(
            "SELECT model, priced_cents FROM cost_events"
        ).fetchall()
        assert rows == [("claude-sonnet-4-5-20250929", None)]

    def test_unpriced_rows_block_all_authorization(self, tmp_db):
        insert_cost_row(tmp_db, priced=False)
        for kind in ("scheduled", "manual"):
            d = authorize(
                CostEstimate(estimated_cents=Decimal("1"), basis="t",
                             kind=kind, component="research"),
                tmp_db, SHARE, as_of=TODAY,
            )
            assert not d.authorized
            assert d.reason == "unpriced_cost_rows"

    def test_reprice_all_fills_holes_and_reports_changes(self, tmp_db):
        """Audit F3: the verbatim-storage recovery path must be real."""
        with pytest.raises(UnknownModelError):
            record_usage({"input_tokens": 1_000_000, "output_tokens": 0},
                         "claude-newmodel-x", "scheduled", "research", tmp_db)
        import catalyst.cost.pricing as pricing
        pricing.MODEL_RATES_CENTS_PER_MTOK["claude-newmodel-x"] = (
            Decimal("100"), Decimal("500"))
        try:
            changes = reprice_all(tmp_db)
        finally:
            del pricing.MODEL_RATES_CENTS_PER_MTOK["claude-newmodel-x"]
        assert len(changes) == 1
        row_id, old, new = changes[0]
        assert old is None and new == Decimal("100")
        assert not has_unpriced_rows(tmp_db)


class TestGovernor:
    def estimate(self, cents, kind="scheduled"):
        return CostEstimate(estimated_cents=Decimal(cents), basis="test",
                            kind=kind, component="research")

    def test_scheduled_allows_under_base_cap(self, tmp_db):
        d = authorize(self.estimate(400), tmp_db, SHARE, as_of=TODAY)
        assert d.authorized and d.cap_cents == BASE_CAP_CENTS

    def test_scheduled_denies_over_cap_with_shortfall(self, tmp_db):
        insert_cost_row(tmp_db, cents="450")
        d = authorize(self.estimate(100), tmp_db, SHARE, as_of=TODAY)
        assert not d.authorized and d.shortfall_cents == Decimal("50")

    def test_paper_profit_never_raises_the_cap(self, tmp_db):
        """Audit F5: fictional paper P&L must not authorize real spend."""
        last_month = (TODAY.replace(day=1) - timedelta(days=1))
        insert_closed_trade(tmp_db, 30000, datetime.combine(last_month, datetime.min.time(), timezone.utc), mode="paper")
        d = authorize(self.estimate(10), tmp_db, SHARE, as_of=TODAY)
        assert d.cap_cents == BASE_CAP_CENTS, "paper profit raised the real-money cap"

    def test_live_profit_raises_cap_but_never_past_hard_bound(self, tmp_db):
        """Audit F5: even a huge live month clamps at GOVERNOR_MAX_CAP."""
        last_month = (TODAY.replace(day=1) - timedelta(days=1))
        insert_closed_trade(tmp_db, 30000, datetime.combine(last_month, datetime.min.time(), timezone.utc), mode="live")
        d = authorize(self.estimate(10), tmp_db, SHARE, as_of=TODAY)
        # uncapped would be 500 + 30000*0.10 = 3500; hard bound is 800
        assert d.cap_cents == GOVERNOR_MAX_CAP_CENTS

    def test_profit_basis_is_prior_month_not_current(self, tmp_db):
        """Audit F5 secondary: this month's profit cannot retroactively
        legitimize this month's spend."""
        insert_closed_trade(tmp_db, 30000, NOW, mode="live")
        assert net_realized_profit_cents_prior_month(tmp_db, TODAY) == Decimal("0")
        d = authorize(self.estimate(10), tmp_db, SHARE, as_of=TODAY)
        assert d.cap_cents == BASE_CAP_CENTS

    def test_net_floor_is_monthly_not_per_trade(self, tmp_db):
        last_month = (TODAY.replace(day=1) - timedelta(days=1))
        at = datetime.combine(last_month, datetime.min.time(), timezone.utc)
        insert_closed_trade(tmp_db, 5000, at, mode="live")
        insert_closed_trade(tmp_db, -8000, at, mode="live")
        assert net_realized_profit_cents_prior_month(tmp_db, TODAY) == Decimal("0")

    def test_manual_monthly_cap_binds(self, tmp_db):
        d = authorize(self.estimate(int(MANUAL_SPEND_CAP_CENTS_PER_MONTH) + 1, kind="manual"),
                      tmp_db, SHARE, as_of=TODAY)
        assert not d.authorized and d.reason == "cap_exceeded"

    def test_manual_lifetime_budget_binds_across_months(self, tmp_db):
        """Audit F7: $200 is one-off, not $20 every month forever."""
        for months_ago in range(1, 11):  # $19.90 x 10 prior months = $199
            past = (TODAY.replace(day=1) - timedelta(days=1)).replace(day=15)
            for _ in range(months_ago):
                past = (past.replace(day=1) - timedelta(days=1)).replace(day=15)
            insert_cost_row(tmp_db, kind="manual", component="backtest_judgement",
                            cents="1990",
                            at=datetime.combine(past, datetime.min.time(), timezone.utc))
        assert lifetime_cents("manual", tmp_db) == Decimal("19900")
        d = authorize(self.estimate(200, kind="manual"), tmp_db, SHARE, as_of=TODAY)
        assert not d.authorized
        assert d.reason == "lifetime_build_budget_exceeded"
        assert d.cap_cents == MANUAL_LIFETIME_BUDGET_CENTS

    def test_manual_does_not_consume_scheduled_cap(self, tmp_db):
        insert_cost_row(tmp_db, kind="manual", component="backtest_judgement", cents="1900")
        d = authorize(self.estimate(400), tmp_db, SHARE, as_of=TODAY)
        assert d.authorized and d.period_to_date_cents == Decimal("0")

    def test_cycle_id_recorded(self, tmp_db):
        """Audit F12: skips must be attributable to their cycle."""
        authorize(self.estimate(1), tmp_db, SHARE, as_of=TODAY, cycle_id="cyc-42")
        row = tmp_db.execute("SELECT cycle_id FROM cost_governor_events").fetchone()
        assert row[0] == "cyc-42"


class TestReconciliation:
    def seed_local(self, conn, cents="100"):
        insert_cost_row(
            conn, cents=cents,
            at=datetime.combine(YESTERDAY, datetime.min.time(), timezone.utc),
        )

    def test_refuses_unclosed_day(self, tmp_db):
        with pytest.raises(ValueError, match="whole days"):
            reconcile_day(TODAY, "scheduled", "research", tmp_db,
                          lambda d: clean_page([]))

    def test_threshold_fires_at_cap_scale_spend(self, tmp_db):
        """Audit F1: at ~17c/day (the $5 cap's scale), a total ledger
        failure must trip the pause - the old fixed 50c threshold
        mathematically never could."""
        self.seed_local(tmp_db, "17")
        result = reconcile_day(YESTERDAY, "scheduled", "research", tmp_db,
                               lambda d: clean_page([]))
        assert result.action_taken == "scheduled_paused"

    def test_empty_api_day_with_local_spend_never_auto_acks(self, tmp_db):
        """Audit F1/F6: 'the adapter returned nothing' is not agreement."""
        self.seed_local(tmp_db, "3")  # even below the 5c floor
        result = reconcile_day(YESTERDAY, "scheduled", "research", tmp_db,
                               lambda d: clean_page([]))
        assert result.action_taken == "scheduled_paused"
        row = tmp_db.execute(
            "SELECT acknowledged_by, api_record_count, api_raw_response "
            "FROM cost_reconciliation_events"
        ).fetchone()
        assert row[0] is None            # no self-sign-off
        assert row[1] == 0
        assert row[2] is not None        # raw payload beside the zero

    def test_truncated_page_refused(self, tmp_db):
        """Audit F4: never compare against a truncated reference."""
        self.seed_local(tmp_db)
        page = CostApiPage(records=[{"kind": "scheduled", "component": "research",
                                     "amount": "50"}],
                           has_more=True, raw_response={})
        with pytest.raises(TruncatedCostPageError):
            reconcile_day(YESTERDAY, "scheduled", "research", tmp_db, lambda d: page)

    def test_clean_match_reconciles_and_records_payload(self, tmp_db):
        self.seed_local(tmp_db, "100")
        result = reconcile_day(
            YESTERDAY, "scheduled", "research", tmp_db,
            lambda d: clean_page([{"kind": "scheduled", "component": "research",
                                   "amount": "100.00"}]),
        )
        assert result.discrepancy_cents == Decimal("0")
        assert result.action_taken == "none"
        count = tmp_db.execute(
            "SELECT api_record_count FROM cost_reconciliation_events"
        ).fetchone()[0]
        assert count == 1

    def test_discrepancy_pauses_both_kinds(self, tmp_db):
        """Audit F11, made explicit and deliberate: a mispriced table
        poisons both ledgers, so the pause is global."""
        self.seed_local(tmp_db, "100")
        reconcile_day(YESTERDAY, "scheduled", "research", tmp_db,
                      lambda d: clean_page([{"kind": "scheduled",
                                             "component": "research",
                                             "amount": "200"}]))
        for kind in ("scheduled", "manual"):
            d = authorize(
                CostEstimate(estimated_cents=Decimal("1"), basis="t",
                             kind=kind, component="research"),
                tmp_db, SHARE, as_of=TODAY,
            )
            assert not d.authorized
            assert d.reason == "reconciliation_discrepancy_unacknowledged"

    def test_per_kind_comparison_catches_cancelling_errors(self, tmp_db):
        self.seed_local(tmp_db, "100")
        api_day = [
            {"kind": "scheduled", "component": "research", "amount": "200"},
            {"kind": "manual", "component": "research", "amount": "0"},
        ]
        result = reconcile_day(YESTERDAY, "scheduled", "research", tmp_db,
                               lambda d: clean_page(api_day))
        assert result.discrepancy_cents == Decimal("100")
        assert result.action_taken == "scheduled_paused"


class TestLedger:
    def test_kinds_never_pooled(self, tmp_db):
        insert_cost_row(tmp_db, kind="scheduled", cents="100")
        insert_cost_row(tmp_db, kind="manual", cents="700")
        assert month_to_date_cents("scheduled", tmp_db, TODAY) == Decimal("100")
        assert month_to_date_cents("manual", tmp_db, TODAY) == Decimal("700")
