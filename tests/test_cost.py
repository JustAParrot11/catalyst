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
    WEB_SEARCH_CENTS_PER_QUERY,
    UnknownModelError,
    rates_for,
)
from catalyst.cost.tracker import (
    CostApiPage,
    TruncatedCostPageError,
    UnrecognizedUsageFieldError,
    acknowledge_discrepancy,
    has_unacknowledged_discrepancy,
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

    def test_unrecognized_billing_field_refuses_pricing(self):
        # The renamed-field trap must refuse to price, never price at zero.
        u = make_usage_components({"input_tokens": 5, "output_megatokens": 1})
        with pytest.raises(UnrecognizedUsageFieldError):
            price(u, "claude-sonnet-4-6")

    def test_nested_unknown_server_tool_field_refuses_pricing(self):
        """Audit N2: a new billable request class inside server_tool_use
        must be loud - web search alone understated a real run by 89%."""
        u = make_usage_components({
            "input_tokens": 5,
            "server_tool_use": {"web_search_requests": 2,
                                 "code_execution_requests": 500},
        })
        with pytest.raises(UnrecognizedUsageFieldError):
            price(u, "claude-sonnet-4-6")

    def test_rates_provenance_exists_and_is_sane(self):
        """Audit N5: staleness is a DASHBOARD warning (rates_stale()),
        not a test failure - a calendar-triggered test failure would
        block the upgrade path (upgrades roll back on red suites)."""
        verified = date.fromisoformat(RATES_VERIFIED_ON)  # parses
        assert verified <= TODAY, "RATES_VERIFIED_ON is in the future"
        assert RATES_MAX_AGE_DAYS > 0
        from catalyst.cost.pricing import rates_stale
        assert rates_stale(as_of=verified + timedelta(days=RATES_MAX_AGE_DAYS + 1))
        assert not rates_stale(as_of=verified)


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

    def test_unrecognized_field_still_lands_a_row(self, tmp_db):
        """Audit N1 regression: the unknown-field guard must never
        prevent the record. Record first, THEN loud."""
        with pytest.raises(UnrecognizedUsageFieldError):
            record_usage({"input_tokens": 100, "cache_read_tokens_v2": 999},
                         "claude-sonnet-4-6", "scheduled", "research", tmp_db)
        rows = tmp_db.execute(
            "SELECT priced_cents, raw_usage_json FROM cost_events"
        ).fetchall()
        assert len(rows) == 1 and rows[0][0] is None
        assert "cache_read_tokens_v2" in rows[0][1]  # verbatim payload kept
        assert has_unpriced_rows(tmp_db)

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
            outcome = reprice_all(tmp_db)
        finally:
            del pricing.MODEL_RATES_CENTS_PER_MTOK["claude-newmodel-x"]
        assert len(outcome.changes) == 1
        row_id, old, new = outcome.changes[0]
        assert old is None and new == Decimal("100")
        assert outcome.still_unpriced == []
        assert not has_unpriced_rows(tmp_db)
        # F3 residual b: every change logged with old and new
        log = tmp_db.execute(
            "SELECT old_cents, new_cents FROM cost_reprice_events").fetchall()
        assert len(log) == 1
        assert log[0][0] is None and Decimal(log[0][1]) == Decimal("100")

    def test_reprice_continues_past_still_unknown_models(self, tmp_db):
        """F3 residual a: one unknown model must not block repricing the
        rest of history."""
        with pytest.raises(UnknownModelError):
            record_usage({"input_tokens": 1_000_000, "output_tokens": 0},
                         "claude-mystery", "scheduled", "research", tmp_db)
        insert_cost_row(tmp_db, cents="1", model="claude-sonnet-4-6")
        outcome = reprice_all(tmp_db)
        assert [m for _, m in outcome.still_unpriced] == ["claude-mystery"]
        # the known row got (re)priced from raw '{}' -> 0
        assert any(new == Decimal("0") for _, _, new in outcome.changes)


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


class TestTheOwnerDoesNotHaveToValidatePricingEveryDay:
    """OWNER-ASKED 2026-08-20: "is it going to keep pausing spending? I
    dont want to need to validate pricing each day".

    Three things have to hold for the answer to be no, and each is a
    test here rather than a promise:

      1. the seven rows already blocking their bot clear themselves;
      2. a corrected day reconciles to zero, so no NEW pause is written;
      3. a genuine systematic fault still stops the bot, because "never
         pauses" would be a worse answer than "pauses too often".
    """

    #: The owner's real reconciliation history, cents, from the
    #: dashboard screenshot: (day, api_total, discrepancy).
    THEIR_ROWS = [("2026-08-19", "0", "0"), ("2026-08-18", "306", "0"),
                  ("2026-08-17", "364", "0"), ("2026-08-16", "0", "0"),
                  ("2026-08-15", "46", "46"), ("2026-08-14", "193", "0"),
                  ("2026-08-13", "0", "0")]

    def seed_local(self, conn, cents="100"):
        insert_cost_row(
            conn, cents=cents,
            at=datetime.combine(YESTERDAY, datetime.min.time(), timezone.utc))

    def seed_their_pauses(self, conn):
        import uuid
        for day, api, disc in self.THEIR_ROWS:
            conn.execute(
                "INSERT INTO cost_reconciliation_events "
                "(id,target_date,kind,component,local_total_cents,"
                " cost_api_total_cents,discrepancy_cents,threshold_cents,"
                " api_raw_response,api_record_count,action_taken,"
                " acknowledged_by,acknowledged_at,reconciled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), day, "all", "{}", api, api, disc, "5",
                 "{}", 3, "scheduled_paused", None, None, day))
        conn.commit()

    def test_all_seven_of_their_pauses_clear_themselves(self, tmp_db):
        """They should not have to click acknowledge seven times for
        discrepancies the current rule says are not large."""
        from catalyst.cost.tracker import clear_pauses_that_no_longer_qualify

        self.seed_their_pauses(tmp_db)
        assert has_unacknowledged_discrepancy(tmp_db)
        assert clear_pauses_that_no_longer_qualify(tmp_db) == 7
        assert not has_unacknowledged_discrepancy(tmp_db), (
            "the bot is still blocked after the upgrade, so the owner "
            "sees 'spending was blocked' unchanged and concludes the fix "
            "did nothing")

    def test_a_day_the_backfill_corrected_writes_no_new_pause(self, tmp_db):
        """The forward-looking half. The nightly backfill makes the
        ledger equal the bill BEFORE the comparison runs, so the
        discrepancy is zero and nothing pauses."""
        self.seed_local(tmp_db, "364.2052")
        result = reconcile_day(
            YESTERDAY, tmp_db,
            lambda d: clean_page([{"amount": "364.2052"}]))
        assert result.discrepancy_cents == Decimal("0.0000")
        assert result.action_taken == "none"

    def test_but_a_REAL_fault_still_stops_it(self, tmp_db):
        """The direction that matters. A rule that never pauses is worse
        than one that pauses too often: the whole point of the check is
        to catch a ledger that has stopped describing the bill."""
        self.seed_local(tmp_db, "364")
        result = reconcile_day(YESTERDAY, tmp_db,
                               lambda d: clean_page([{"amount": "50"}]))
        assert result.action_taken == "scheduled_paused"

    def test_a_still_material_old_pause_is_NOT_auto_cleared(self, tmp_db):
        """Clearing is a re-judgement, not an amnesty."""
        import uuid

        from catalyst.cost.tracker import clear_pauses_that_no_longer_qualify

        tmp_db.execute(
            "INSERT INTO cost_reconciliation_events "
            "(id,target_date,kind,component,local_total_cents,"
            " cost_api_total_cents,discrepancy_cents,threshold_cents,"
            " api_raw_response,api_record_count,action_taken,"
            " acknowledged_by,acknowledged_at,reconciled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "2026-08-01", "all", "{}", "1000", "100",
             "900", "5", "{}", 3, "scheduled_paused", None, None,
             "2026-08-01"))
        tmp_db.commit()
        assert clear_pauses_that_no_longer_qualify(tmp_db) == 0
        assert has_unacknowledged_discrepancy(tmp_db)


class TestAgainstTheRealBill:
    """GROUND TRUTH, fetched from the Anthropic Admin Cost and Usage
    APIs on 2026-08-20 for the day the owner queried.

    OWNER-REPORTED: "on 17th we spent $2.95 yet dashboard says $3.36
    around there ... Use admin API to ensure were correctly getting
    data as it feels wrong again".

    It was checked, against both endpoints, and the pricing is exact.
    What the $2.95 turned out to be is the interesting part - see
    test_the_owners_figure_was_the_token_subtotal below.

    These are real numbers off a real bill, so they are worth far more
    than a hand-made fixture: if our arithmetic ever stops reproducing
    them, it has stopped reproducing Anthropic's.
    """

    #: /v1/organizations/usage_report/messages, 2026-08-17, group_by model
    REAL_USAGE = {
        "input_tokens": 1086881,          # uncached_input_tokens
        "output_tokens": 77829,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_1h_input_tokens": 0,
                           "ephemeral_5m_input_tokens": 0},
        "server_tool_use": {"web_search_requests": 69},
    }
    #: /v1/organizations/cost_report, same day, group_by description
    REAL_INPUT_COST = Decimal("217.3762")
    REAL_OUTPUT_COST = Decimal("77.829")
    REAL_WEB_SEARCH_COST = Decimal("69")
    REAL_TOTAL = REAL_INPUT_COST + REAL_OUTPUT_COST + REAL_WEB_SEARCH_COST

    def test_our_price_reproduces_the_real_bill_to_the_cent(self):
        got = price(make_usage_components(self.REAL_USAGE),
                    "claude-sonnet-5", on_date=date(2026, 8, 17))
        assert got == self.REAL_TOTAL, (
            f"we price this day at {got}c; Anthropic charged "
            f"{self.REAL_TOTAL}c")

    def test_the_intro_rate_is_the_one_actually_billed(self):
        """1,086,881 input tokens cost 217.3762c, which is $2/MTok. At
        the standard $3 it would have been 326c. The intro rate is not
        an assumption - it is what the bill says."""
        implied = self.REAL_INPUT_COST / Decimal(
            self.REAL_USAGE["input_tokens"]) * Decimal("1000000")
        assert implied == Decimal("200"), f"{implied}c/MTok billed"
        assert rates_for("claude-sonnet-5", date(2026, 8, 17))[0] == implied

    def test_web_search_is_a_cent_a_query_on_the_real_bill(self):
        """TRAPS.md says $10/1000 queries; the bill agrees exactly.
        69 requests, 69 cents."""
        per = self.REAL_WEB_SEARCH_COST / Decimal(
            self.REAL_USAGE["server_tool_use"]["web_search_requests"])
        assert per == WEB_SEARCH_CENTS_PER_QUERY == Decimal("1")

    def test_the_owners_figure_was_the_token_subtotal(self):
        """THE ANSWER. $2.95 is the bill with WEB SEARCH LEFT OUT - the
        single trap TRAPS.md warns understated a previous run by 89%.
        The real total for that day was $3.64, so the local ledger was
        never overcharging; the figure it was being compared against was
        missing a line."""
        tokens_only = self.REAL_INPUT_COST + self.REAL_OUTPUT_COST
        assert tokens_only.quantize(Decimal("0.01")) == Decimal("295.21")
        assert self.REAL_TOTAL.quantize(Decimal("0.01")) == Decimal("364.21")
        assert self.REAL_WEB_SEARCH_COST > 0, (
            "if a real day ever bills no web search, this test stops "
            "demonstrating anything and should be re-fetched")

    def test_caching_was_not_in_use_that_day(self):
        """Recorded because it is a live cost lead, not a defect: every
        cache field on the real bill is zero, so the research prompt's
        fixed preamble is being paid for in full on every call."""
        assert self.REAL_USAGE["cache_read_input_tokens"] == 0
        assert self.REAL_USAGE["cache_creation"] == {
            "ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}


class TestReconciliation:
    def seed_local(self, conn, cents="100"):
        insert_cost_row(
            conn, cents=cents,
            at=datetime.combine(YESTERDAY, datetime.min.time(), timezone.utc),
        )

    def test_refuses_unclosed_day(self, tmp_db):
        with pytest.raises(ValueError, match="whole days"):
            reconcile_day(TODAY, tmp_db, lambda d: clean_page([]))

    def test_threshold_fires_at_cap_scale_spend(self, tmp_db):
        """Audit F1: at ~17c/day (the $5 cap's scale), a total ledger
        failure must trip the pause - the old fixed 50c threshold
        mathematically never could."""
        self.seed_local(tmp_db, "17")
        result = reconcile_day(YESTERDAY, tmp_db, lambda d: clean_page([]))
        assert result.action_taken == "scheduled_paused"

    def test_THE_OWNER_S_OWN_NUMBERS_DO_NOT_HALT_THE_BOT(self, tmp_db):
        """OWNER-REPORTED 2026-08-20: "on 17th we spent $2.95 yet
        dashboard says $3.36 around there ... its yet again paused the
        bot until we confirmed".

        41c on a 336c day. The relative test alone calls that 12.2% and
        halts; the owner's own 2026-08-14 decision - "block only if
        large", floor 50c - says it is not large. Two pause paths
        existed and the decision had only been applied to the drift one,
        so the day path quietly overrode it.

        This is the exact arithmetic, pinned, because the wrong answer
        stops the bot trading and the owner has now been stopped twice.
        """
        self.seed_local(tmp_db, "336")
        result = reconcile_day(YESTERDAY, tmp_db,
                               lambda d: clean_page([{"amount": "295"}]))
        assert result.discrepancy_cents == Decimal("41")
        assert result.action_taken == "none", (
            "a 41c difference on a $3 day halted the bot again")

    def test_but_a_SYSTEMATIC_mispricing_on_the_same_day_still_halts(
            self, tmp_db):
        """The direction that matters. Raising the floor must not buy
        quiet at the cost of missing a wrong rate table - which is the
        failure the daily check exists for. Same day, same spend, 40%
        out instead of 12%."""
        self.seed_local(tmp_db, "336")
        result = reconcile_day(YESTERDAY, tmp_db,
                               lambda d: clean_page([{"amount": "202"}]))
        assert result.action_taken == "scheduled_paused"

    def test_both_bars_must_be_cleared_not_either(self):
        """A big absolute difference that is a small proportion is a big
        day, not a broken ledger - and vice versa.

        Asserted on the threshold itself rather than end to end, because
        end to end a fresh database ALSO trips the drift rule: with no
        reconciled window behind it, _window_spend is zero and drift
        deliberately fails closed. That is right, and it would hide what
        this test is about.
        """
        from catalyst.cost.tracker import (
            RECONCILE_PAUSE_FLOOR_CENTS, RECONCILE_REL_THRESHOLD,
        )

        def day_pauses(local, api):
            local, api = Decimal(local), Decimal(api)
            threshold = max(RECONCILE_PAUSE_FLOOR_CENTS,
                            RECONCILE_REL_THRESHOLD * max(local, api))
            return (local - api).copy_abs() > threshold

        assert not day_pauses("336", "295")    # 41c, 12% - under the floor
        assert day_pauses("336", "202")        # 134c, 40% - both bars
        assert not day_pauses("10000", "9900")  # 100c but 1% - proportion
        assert not day_pauses("40", "10")      # 30c and 75% - under floor

    def test_the_day_floor_is_the_one_the_owner_chose(self):
        """Two pause paths, one decision. If they diverge again the bot
        halts on a number the owner explicitly said should not halt it."""
        from catalyst.cost.tracker import RECONCILE_PAUSE_FLOOR_CENTS
        import inspect
        from catalyst.cost import tracker

        src = inspect.getsource(tracker.reconcile_day)
        assert "RECONCILE_PAUSE_FLOOR_CENTS" in src, (
            "the daily check no longer uses the owner-chosen floor, so "
            "it can halt on a discrepancy the drift check forgives")
        assert RECONCILE_PAUSE_FLOOR_CENTS == Decimal("50")

    def test_EVERY_PAUSE_SAYS_WHICH_TEST_FIRED(self, tmp_db):
        """OWNER-REPORTED 2026-08-20: seven consecutive rows reading
        "scheduled_paused", three of them with a $0.00 discrepancy, and
        nothing anywhere saying why any of them stopped the bot.

        Three different conditions pause here and they need three
        different responses. A pause that halts trading is the last
        thing on this dashboard that should be unexplained (house rule
        3), and the governor table one section below has carried a
        reason column all along."""
        self.seed_local(tmp_db, "1000")
        result = reconcile_day(YESTERDAY, tmp_db,
                               lambda d: clean_page([{"amount": "1"}]))
        assert result.action_taken == "scheduled_paused"
        reason = tmp_db.execute(
            "SELECT pause_reason FROM cost_reconciliation_events"
        ).fetchone()[0]
        assert reason and "out by" in reason, reason
        assert "1000" in reason and "1" in reason, (
            "the reason does not carry the two numbers that caused it")

    def test_an_empty_api_answer_says_THAT_not_something_else(self, tmp_db):
        """3c, deliberately: a bigger figure is caught by the size test
        first and never reaches the empty-answer branch, which exists
        precisely for spend too small to trip anything else."""
        self.seed_local(tmp_db, "3")
        reconcile_day(YESTERDAY, tmp_db, lambda d: clean_page([]))
        reason = tmp_db.execute(
            "SELECT pause_reason FROM cost_reconciliation_events"
        ).fetchone()[0]
        assert "no records at all" in reason, reason

    def test_a_day_that_did_not_pause_carries_no_reason(self, tmp_db):
        """A reason on an unpaused row would read as a problem that is
        not there."""
        self.seed_local(tmp_db, "364")
        result = reconcile_day(YESTERDAY, tmp_db,
                               lambda d: clean_page([{"amount": "364"}]))
        assert result.action_taken == "none"
        assert tmp_db.execute(
            "SELECT pause_reason FROM cost_reconciliation_events"
        ).fetchone()[0] is None

    def test_empty_api_day_with_local_spend_never_auto_acks(self, tmp_db):
        """Audit F1/F6: 'the adapter returned nothing' is not agreement."""
        self.seed_local(tmp_db, "3")  # even below the 5c floor
        result = reconcile_day(YESTERDAY, tmp_db, lambda d: clean_page([]))
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
            reconcile_day(YESTERDAY, tmp_db, lambda d: page)

    def test_clean_match_reconciles_and_records_payload(self, tmp_db):
        self.seed_local(tmp_db, "100")
        result = reconcile_day(
            YESTERDAY, tmp_db,
            lambda d: clean_page([{"amount": "100.00"}]),
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
        reconcile_day(YESTERDAY, tmp_db,
                      lambda d: clean_page([{"amount": "200"}]))
        for kind in ("scheduled", "manual"):
            d = authorize(
                CostEstimate(estimated_cents=Decimal("1"), basis="t",
                             kind=kind, component="research"),
                tmp_db, SHARE, as_of=TODAY,
            )
            assert not d.authorized
            assert d.reason == "reconciliation_discrepancy_unacknowledged"

    def test_whole_day_totals_with_local_breakdown_recorded(self, tmp_db):
        """Audit N3: the Cost API cannot see our scheduled/manual split,
        so the comparison is whole-day totals; the per-kind LOCAL
        breakdown is recorded beside it so a kind-level error is still
        diagnosable from the row."""
        self.seed_local(tmp_db, "100")
        insert_cost_row(tmp_db, kind="manual", component="backtest_judgement",
                        cents="40",
                        at=datetime.combine(YESTERDAY, datetime.min.time(), timezone.utc))
        result = reconcile_day(YESTERDAY, tmp_db,
                               lambda d: clean_page([{"amount": "140"}]))
        assert result.discrepancy_cents == Decimal("0")
        import json as _json
        breakdown = _json.loads(tmp_db.execute(
            "SELECT component FROM cost_reconciliation_events").fetchone()[0])
        assert breakdown == {"scheduled": "100", "manual": "40"}

    def test_cumulative_drift_pauses_when_it_is_MATERIAL(self, tmp_db):
        """Audit F1 residual, re-scoped by owner decision 2026-08-14.

        The original rule paused on 6c of cumulative drift, because the
        bound was RECONCILE_FLOOR_CENTS (five cents) against a 30-day
        window. Live, that halted all spending for a day and refused 125
        candidates: whole-day billing figures settle a cent or two from
        a real-time local estimate, so a few cents of drift across a
        month is the expected state rather than a fault.

        Asked how a discrepancy should behave, the owner chose "block
        only if large". The guard still fires - a genuine billing fault
        must stop the bot - but "large" is now measured against what was
        actually spent, and never below an absolute floor. A drift of a
        few cents is no longer a halt condition; a drift that is a real
        fraction of a real bill still is.
        """
        for i in range(1, 4):
            day = TODAY - timedelta(days=i)
            insert_cost_row(tmp_db, cents="2000",
                            at=datetime.combine(day, datetime.min.time(), timezone.utc))
        results = []
        for i in (3, 2, 1):
            day = TODAY - timedelta(days=i)
            # local 2000c vs api 1600c: a 20% miss, day after day
            results.append(reconcile_day(day, tmp_db,
                                         lambda d: clean_page([{"amount": "1600"}])))
        assert any(r.action_taken == "scheduled_paused" for r in results), (
            "a sustained 20% divergence on a real bill must still pause")

    def test_a_few_cents_of_drift_does_NOT_pause(self, tmp_db):
        """The half that changed, pinned so it cannot creep back."""
        for i in range(1, 4):
            day = TODAY - timedelta(days=i)
            insert_cost_row(tmp_db, cents="2000",
                            at=datetime.combine(day, datetime.min.time(), timezone.utc))
        actions = []
        for i in (3, 2, 1):
            day = TODAY - timedelta(days=i)
            # 2c a day against $20 a day - rounding, not a fault
            actions.append(reconcile_day(
                day, tmp_db,
                lambda d: clean_page([{"amount": "1998"}])).action_taken)
        assert "scheduled_paused" not in actions, (
            f"{actions} - a few cents against $20/day halted the bot, "
            "which is the defect the owner reported")

    def test_truncated_page_writes_paused_row_before_raising(self, tmp_db):
        """Audit F4 second gap: the refusal must be on the record."""
        self.seed_local(tmp_db)
        page = CostApiPage(records=[{"amount": "50"}], has_more=True,
                           raw_response={"has_more": True})
        with pytest.raises(TruncatedCostPageError):
            reconcile_day(YESTERDAY, tmp_db, lambda d: page)
        assert has_unacknowledged_discrepancy(tmp_db)

    def test_acknowledge_requires_human_and_clears_pause(self, tmp_db):
        """Audit F11 residual: a human path out of the pause exists."""
        self.seed_local(tmp_db, "3")
        reconcile_day(YESTERDAY, tmp_db, lambda d: clean_page([]))
        assert has_unacknowledged_discrepancy(tmp_db)
        event_id = tmp_db.execute(
            "SELECT id FROM cost_reconciliation_events WHERE acknowledged_at IS NULL"
        ).fetchone()[0]
        with pytest.raises(ValueError):
            acknowledge_discrepancy(tmp_db, event_id, "auto")
        acknowledge_discrepancy(tmp_db, event_id, "owner@example")
        assert not has_unacknowledged_discrepancy(tmp_db)


class TestLedger:
    def test_kinds_never_pooled(self, tmp_db):
        insert_cost_row(tmp_db, kind="scheduled", cents="100")
        insert_cost_row(tmp_db, kind="manual", cents="700")
        assert month_to_date_cents("scheduled", tmp_db, TODAY) == Decimal("100")
        assert month_to_date_cents("manual", tmp_db, TODAY) == Decimal("700")


class TestOwnerSetsTheCap:
    """The owner asked to set the bot's monthly budget from the browser,
    upward as well as downward.

    The two-tier design is PRESERVED, not abandoned, by splitting the
    two ceilings that were previously one:

      GOVERNOR_MAX_CAP_CENTS  - the most the SYSTEM may hand itself out
                                of its own realised profit. Unchanged at
                                $8. This is the anti-ratchet: a lucky
                                month must not walk the cap upward.
      the owner's figure      - a person deciding how much of their own
                                money to spend. NO fixed ceiling, by
                                request: the typo guard lives at the
                                point of entry, which is the only place
                                that can tell a deliberate large figure
                                from a slipped keyboard.

    A human choosing to spend more is a decision; a system paying itself
    more is a ratchet. Only the second needs a wall here.
    """

    def _authorize(self, conn, estimate_cents, owner_cents=None, profit=None):
        from catalyst.cost import CostEstimate
        from catalyst.cost import governor as gov
        est = CostEstimate(estimated_cents=Decimal(str(estimate_cents)),
                           basis="test", kind="scheduled", component="research")
        return gov.authorize(est, conn, Decimal("0.10"),
                             owner_monthly_cap_cents=(
                                 Decimal(str(owner_cents))
                                 if owner_cents is not None else None))

    def test_owner_can_raise_the_cap_above_the_base(self, tmp_db):
        """The estimate is kept under DAILY_CAP_CENTS on purpose.

        This test is about the MONTHLY cap being replaced by the owner's
        figure. It used to authorise a single 600c call, which the daily
        rate ceiling now refuses first and correctly - no one research
        call should cost more than a whole day's allowance. Using a
        realistic estimate keeps the test on its own subject; the daily
        gate has its own tests below.
        """
        from catalyst.cost.governor import BASE_CAP_CENTS, DAILY_CAP_CENTS

        estimate = BASE_CAP_CENTS + 100
        assert estimate > BASE_CAP_CENTS, "must exceed the base to be a test"
        estimate = min(estimate, DAILY_CAP_CENTS - 100)
        d = self._authorize(tmp_db, estimate, owner_cents=1200)
        assert d.cap_cents == Decimal("1200")
        assert d.authorized is True, (
            "spend inside the owner's own, higher, cap must be allowed")

    def test_owner_can_still_tighten_below_the_base(self, tmp_db):
        d = self._authorize(tmp_db, 150, owner_cents=100)
        assert d.cap_cents == Decimal("100")
        assert d.authorized is False

    def test_a_large_deliberate_figure_is_honoured(self, tmp_db):
        """No ceiling here by design. The guard that stops a mistyped
        99999 becoming a budget is at the point of ENTRY, where a
        confirmation can be demanded; the governor's job is to enforce
        the number it was given, not to second-guess the owner."""
        d = self._authorize(tmp_db, 10, owner_cents=999999)
        assert d.cap_cents == Decimal("999999")

    def test_a_negative_reads_as_stop_never_as_no_limit(self, tmp_db):
        d = self._authorize(tmp_db, 1, owner_cents=-999)
        assert d.cap_cents == Decimal("0")
        assert d.authorized is False

    def test_zero_means_stop_spending_entirely(self, tmp_db):
        d = self._authorize(tmp_db, 1, owner_cents=0)
        assert d.cap_cents == Decimal("0")
        assert d.authorized is False

    def test_the_system_still_cannot_pay_itself_past_its_own_clamp(
            self, tmp_db):
        """The anti-ratchet is untouched: with NO owner figure set, a
        huge realised profit still cannot lift the cap past $8."""
        from catalyst.cost import governor as gov
        from catalyst.cost.governor import GOVERNOR_MAX_CAP_CENTS
        from datetime import datetime, timezone

        insert_closed_trade(tmp_db, 500_000,
                            datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc),
                            mode="live")
        d = self._authorize(tmp_db, 10, owner_cents=None)
        assert d.cap_cents <= GOVERNOR_MAX_CAP_CENTS

    def test_a_negative_figure_is_treated_as_zero_not_as_infinity(
            self, tmp_db):
        d = self._authorize(tmp_db, 1, owner_cents=-500)
        assert d.cap_cents == Decimal("0")


class TestOwnerEditableTokenPrices:
    """Published rates change; the alternative was editing pricing.py
    and redeploying. The owner chose a DATE-EFFECTIVE editor, and that
    choice is the safety property: history keeps the rate in force when
    the tokens were bought, so the nightly comparison against the real
    Anthropic bill cannot drift for reasons nobody can reconstruct."""

    def _set(self, conn, model, when, inp, outp, by="owner"):
        from catalyst.cost.overrides import set_override
        return set_override(conn, model, when, Decimal(inp), Decimal(outp),
                            set_by=by)

    def test_a_new_rate_applies_from_its_date_forward(self, tmp_db):
        from catalyst.cost.overrides import rates_for_on
        self._set(tmp_db, "claude-sonnet-5", date(2026, 9, 1), "300", "1500")
        assert rates_for_on(tmp_db, "claude-sonnet-5", date(2026, 9, 2)) \
            == (Decimal("300"), Decimal("1500"))

    def test_history_keeps_the_rate_that_was_in_force(self, tmp_db):
        """The whole point of date-effective. August tokens were bought
        at August prices and must stay priced that way forever."""
        from catalyst.cost.overrides import rates_for_on
        from catalyst.cost.pricing import SONNET5_INTRO_RATES
        self._set(tmp_db, "claude-sonnet-5", date(2026, 9, 1), "300", "1500")
        assert rates_for_on(tmp_db, "claude-sonnet-5", date(2026, 8, 15)) \
            == SONNET5_INTRO_RATES

    def test_the_latest_row_on_or_before_the_day_wins(self, tmp_db):
        from catalyst.cost.overrides import rates_for_on
        self._set(tmp_db, "claude-sonnet-5", date(2026, 9, 1), "300", "1500")
        self._set(tmp_db, "claude-sonnet-5", date(2026, 10, 1), "400", "2000")
        assert rates_for_on(tmp_db, "claude-sonnet-5", date(2026, 9, 20)) \
            == (Decimal("300"), Decimal("1500"))
        assert rates_for_on(tmp_db, "claude-sonnet-5", date(2026, 10, 5)) \
            == (Decimal("400"), Decimal("2000"))

    def test_a_correction_is_a_new_row_not_an_edit(self, tmp_db):
        """Append-only: what was believed when is never lost."""
        from catalyst.cost.overrides import rates_for_on
        self._set(tmp_db, "claude-sonnet-5", date(2026, 9, 1), "300", "1500")
        self._set(tmp_db, "claude-sonnet-5", date(2026, 9, 1), "310", "1550")
        assert tmp_db.execute(
            "SELECT COUNT(*) FROM pricing_overrides").fetchone()[0] == 2
        assert rates_for_on(tmp_db, "claude-sonnet-5", date(2026, 9, 5)) \
            == (Decimal("310"), Decimal("1550"))

    def test_no_override_falls_back_to_the_built_in_table(self, tmp_db):
        from catalyst.cost.overrides import rates_for_on
        from catalyst.cost.pricing import rates_for
        assert rates_for_on(tmp_db, "claude-sonnet-5", date(2026, 12, 1)) \
            == rates_for("claude-sonnet-5", date(2026, 12, 1))

    def test_an_unknown_model_still_refuses_rather_than_pricing_at_zero(
            self, tmp_db):
        from catalyst.cost.overrides import rates_for_on
        from catalyst.cost.pricing import UnknownModelError
        with pytest.raises(UnknownModelError):
            rates_for_on(tmp_db, "claude-renamed-v9", date(2026, 9, 1))

    def test_a_zero_or_negative_rate_is_refused(self, tmp_db):
        """A zero rate prices every future call at nothing - the exact
        silent-understatement failure TRAPS.md is about."""
        for bad in ("0", "-100"):
            with pytest.raises(ValueError):
                self._set(tmp_db, "claude-sonnet-5", date(2026, 9, 1), bad, "1500")
            with pytest.raises(ValueError):
                self._set(tmp_db, "claude-sonnet-5", date(2026, 9, 1), "300", bad)

    def test_recording_a_live_call_uses_the_override(self, tmp_db):
        from catalyst.cost.tracker import record_usage
        today = datetime.now(timezone.utc).date()
        self._set(tmp_db, "claude-sonnet-5", today, "1000", "1000")
        ev = record_usage({"input_tokens": 1_000_000, "output_tokens": 0,
                           "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0},
                          "claude-sonnet-5", "manual", "t", tmp_db)
        assert ev.priced_cents == Decimal("1000")
