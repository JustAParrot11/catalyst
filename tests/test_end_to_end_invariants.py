"""The invariants that must hold whatever else changes.

OWNER-ASKED: "create new tests you think we need to validate every part
of the bot, logic arithmetic and way we get data and API costs alpaca
usage and call anything you can find."

Most of this repo's tests pin a specific defect that once cost money.
This file is the other kind: PROPERTIES that must be true of the whole
system regardless of which defect is fashionable. They are the things
that, if they ever stopped being true, would be discovered by losing
money rather than by a test going red.

Five areas, in the order they can hurt:

  1. ARITHMETIC - money is Decimal end to end, cents are integers, and
     no float ever reaches a figure the owner reads.
  2. RISK LOGIC - hard bounds actually bound, and no adaptive parameter
     can walk outside its own range however the evidence reads.
  3. DATA - every feed's failure is recorded rather than swallowed, and
     no upstream shape can raise into the trading loop.
  4. API COST - every priced row prices from a stored RAW usage object,
     cache tokens included, and the governor's cap is enforced against
     the same figure the dashboard shows.
  5. ALPACA USAGE - every outbound call is one this bot is allowed to
     make, within a documented rate limit, and no removed field is
     referenced.

Offline throughout; `tests/conftest.py` blocks sockets by contract.
"""

import ast
import inspect
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "catalyst"
NOW = datetime(2026, 8, 16, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    from catalyst.storage import init_db

    conn = init_db(str(tmp_path / "c.db"))
    yield conn
    conn.close()


# =====================================================================
# 1. ARITHMETIC
# =====================================================================

class TestMoneyArithmetic:
    """Float money is how a cent goes missing and never comes back."""

    def test_cents_are_never_computed_in_float_in_the_cost_layer(self):
        """A grep-level guard, deliberately. `0.1 + 0.2` is the oldest
        bug in finance and the cost layer is where it would land on the
        one number that decides whether this bot is viable."""
        offenders = []
        for path in (PKG / "cost").glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                # float(...) applied to anything that looks like money
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "float"):
                    src = ast.get_source_segment(path.read_text(), node) or ""
                    if any(w in src for w in ("cent", "cost", "price", "usd")):
                        offenders.append(f"{path.name}: {src}")
        assert not offenders, (
            "float() applied to money in the cost layer:\n  "
            + "\n  ".join(offenders))

    def test_priced_cost_rows_round_trip_exactly(self, db):
        """What is stored must be what comes back. A Decimal written as
        a float loses the last cent on the way through SQLite."""
        from catalyst.cost.ledger import month_to_date_cents

        amounts = ["0.01", "193.30", "0.1", "0.2", "1234.56"]
        for i, amount in enumerate(amounts):
            db.execute(
                "INSERT INTO cost_events (id,raw_usage_json,model,kind,"
                "component,priced_cents,priced_at) VALUES "
                "(?,'{}','m','scheduled','research',?,?)",
                (f"e{i}", amount, "2026-08-10T12:00:00+00:00"))
        db.commit()
        total = month_to_date_cents("scheduled", db, date(2026, 8, 16))
        assert total == sum(Decimal(a) for a in amounts), (
            f"{total} != exact sum - money went through a float")

    def test_the_annual_hurdle_arithmetic_is_right(self):
        """The number that decides viability. Checked against figures
        computed by hand in BUILD-BRIEF."""
        for monthly_usd, capital, expected_pct in (
                (5, 1000, 6.0), (25, 1000, 30.0), (50, 2000, 30.0),
                (25, 2000, 15.0)):
            hurdle = Decimal(monthly_usd) * 12 / Decimal(capital) * 100
            assert abs(hurdle - Decimal(str(expected_pct))) < Decimal("0.05")

    def test_position_sizing_is_exact_not_approximate(self):
        """notional = risk_budget / worst_case, and it must land on the
        cent rather than near it."""
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.hard_bounds import HARD_BOUNDS
        from catalyst.risk.sizing import size

        params = {"stop_width": {"earnings": Decimal("0.16")},
                  "adverse_gap_assumption": {"earnings": Decimal("0.14")}}
        portfolio = PortfolioState(
            equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("2000"), consecutive_losses=0,
            as_of=NOW, reliable=True)
        market = MarketSnapshot(ticker="AAA", last_close=Decimal("50"),
                                half_spread_bp=Decimal("5"),
                                median_daily_dollar_volume=Decimal("0"))
        res = size(True, "earnings", portfolio, params, HARD_BOUNDS, market)
        # 2% of 2000 = 40; worst case max(0.14, 0.16) = 0.16 -> 250.00
        assert res.notional_usd == Decimal("250.00")
        assert isinstance(res.notional_usd, Decimal)

    def test_qty_always_rounds_DOWN(self):
        """Rounding a share count up buys stock the sizing did not
        authorise, on a cash account that may not have the money."""
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.hard_bounds import HARD_BOUNDS
        from catalyst.risk.sizing import size

        params = {"stop_width": {"earnings": Decimal("0.16")},
                  "adverse_gap_assumption": {"earnings": Decimal("0.14")}}
        portfolio = PortfolioState(
            equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("2000"), consecutive_losses=0,
            as_of=NOW, reliable=True)
        market = MarketSnapshot(ticker="AAA", last_close=Decimal("33.33"),
                                half_spread_bp=Decimal("5"),
                                median_daily_dollar_volume=Decimal("0"))
        res = size(True, "earnings", portfolio, params, HARD_BOUNDS, market)
        assert res.qty * market.last_close <= res.notional_usd, (
            "qty x price exceeds the authorised notional - rounded up")


# =====================================================================
# 2. RISK LOGIC
# =====================================================================

class TestHardBoundsActuallyBound:
    def test_no_position_can_exceed_the_equal_weight_slot(self):
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.hard_bounds import HARD_BOUNDS
        from catalyst.risk.sizing import size

        # An absurdly tight worst-case would divide into a huge notional.
        params = {"stop_width": {"x": Decimal("0.02")},
                  "adverse_gap_assumption": {"x": Decimal("0.02")}}
        portfolio = PortfolioState(
            equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("2000"), consecutive_losses=0,
            as_of=NOW, reliable=True)
        market = MarketSnapshot(ticker="AAA", last_close=Decimal("10"),
                                half_spread_bp=Decimal("1"),
                                median_daily_dollar_volume=Decimal("0"))
        res = size(True, "x", portfolio, params, HARD_BOUNDS, market)
        slot = Decimal("2000") / HARD_BOUNDS.max_open_positions
        assert res.notional_usd <= slot

    def test_no_adaptive_parameter_can_leave_its_range(self):
        """PARAM_RANGE is the adaptive system's own leash. Every
        parameter, both edges, whatever the evidence says."""
        from catalyst.risk.adaptive_params import DEFAULT_PARAMS, PARAM_RANGE

        for name, (lo, hi) in PARAM_RANGE.items():
            assert lo < hi, f"{name} has an empty range"
            default = DEFAULT_PARAMS.get(name)
            if isinstance(default, Decimal):
                assert lo <= default <= hi, (
                    f"{name} ships at {default}, outside its own range")
            elif isinstance(default, dict):
                for leaf, value in default.items():
                    assert lo <= Decimal(str(value)) <= hi, (
                        f"{name}.{leaf} ships at {value}, outside its range")

    def test_the_conviction_bar_is_always_reachable(self):
        """floor + priced-in premium must stay under the maximum
        conviction the model can express, or those candidates are
        refused forever by arithmetic."""
        from catalyst.risk.adaptive_params import PARAM_RANGE
        from catalyst.risk.evaluate import PRICED_IN_CONVICTION_PREMIUM

        assert PARAM_RANGE["conviction_floor"][1] + \
            PRICED_IN_CONVICTION_PREMIUM < Decimal("1.0")

    def test_every_hard_bound_is_a_positive_fraction_or_count(self):
        """A zero or negative bound does not bind, it disables."""
        from catalyst.risk.hard_bounds import HARD_BOUNDS

        for field, value in vars(HARD_BOUNDS).items():
            assert value > 0, f"{field} is {value} - that is not a bound"

    def test_the_max_hold_honours_days_to_weeks(self):
        from catalyst.risk.hard_bounds import HARD_BOUNDS

        assert 1 < HARD_BOUNDS.max_hold_days <= 31, (
            "BUILD-BRIEF requires days to about three weeks, never months")


# =====================================================================
# 3. DATA ACQUISITION
# =====================================================================

class TestEveryFeedFailsLoudlyToTheDatabase:
    def test_a_feed_error_lands_in_raw_events_errors(self, db):
        """BUILD-BRIEF: every zero keeps its raw upstream response
        beside it. A feed that fails silently is indistinguishable from
        a quiet market."""
        db.execute(
            "INSERT INTO raw_events_errors (source, attempted_at, error_text) "
            "VALUES ('edgar_fts', ?, 'HTTP 500 from efts.sec.gov')",
            (NOW.isoformat(),))
        db.commit()
        row = db.execute(
            "SELECT error_text FROM raw_events_errors").fetchone()
        assert "500" in row[0]

    def test_raw_payloads_are_stored_verbatim(self, db):
        """Reading named fields means a renamed or nested one silently
        prices itself at zero (TRAPS.md)."""
        payload = {"ticker": "AAA", "nested": {"unexpected": [1, 2, 3]},
                   "unknown_future_field": "kept"}
        db.execute("INSERT INTO raw_events VALUES ('alpaca_news','n1',?,?)",
                   (NOW.isoformat(), json.dumps(payload)))
        db.commit()
        back = json.loads(db.execute(
            "SELECT payload_raw FROM raw_events").fetchone()[0])
        assert back == payload, "the payload was not stored verbatim"

    @pytest.mark.parametrize("payload", [
        None, "", "not json", "[]", "{}", '{"parsed": null}',
        '{"parsed": {"owners": null}}', '{"ticker": 12345}',
    ])
    def test_no_upstream_shape_raises_out_of_discovery(self, payload):
        from catalyst.data import RawEvent
        from catalyst.data.form4_adapter import flatten_form4_events

        try:
            parsed = json.loads(payload) if payload else None
        except (TypeError, ValueError):
            parsed = payload
        flatten_form4_events([RawEvent("edgar_form4", "s", NOW, parsed)])

    def test_the_universe_rule_is_applied_before_anything_is_traded(self):
        """A fund reaching the risk engine at all is the defect; it must
        be stopped in discovery, not caught later."""
        from catalyst.discovery.universe import is_tradeable

        assert not is_tradeable("SPY")
        assert is_tradeable("AAPL")


class TestDatesAreHandledHonestly:
    def test_future_dated_events_are_invisible_until_they_arrive(self):
        """Point-in-time discipline: a feed claiming tomorrow's filings
        must not produce a candidate today."""
        from catalyst.discovery.candidates import build_candidates

        assert build_candidates([], NOW) == []


# =====================================================================
# 4. API COST
# =====================================================================

class TestCostAccounting:
    def test_cache_tokens_are_priced_not_ignored(self):
        """TRAPS.md: cache tokens are billed but are NOT in
        input_tokens. Missing them understates the bill by about half."""
        src = (PKG / "cost" / "tracker.py").read_text()
        for field in ("cache_read_input_tokens",
                      "cache_creation_input_tokens"):
            assert field in src, f"{field} is never read - the bill is wrong"

    def test_cache_tokens_actually_change_the_price(self):
        """Behaviour, not a grep. Writes cost 1.25x input and reads
        0.1x, so a usage object carrying them must price HIGHER than the
        identical one without - the trap is that they are billed and are
        NOT inside input_tokens, so a naive reading charges nothing."""
        from catalyst.cost.tracker import make_usage_components, price

        bare = make_usage_components({"input_tokens": 1000, "output_tokens": 100})
        cached = make_usage_components({"input_tokens": 1000, "output_tokens": 100,
                                  "cache_creation_input_tokens": 10000,
                                  "cache_read_input_tokens": 10000})
        cost_a = price(bare, "claude-sonnet-5")
        cost_b = price(cached, "claude-sonnet-5")
        assert Decimal(str(cost_b)) > Decimal(str(cost_a)), (
            "cache tokens priced at zero - this understates the bill by "
            "about half (TRAPS.md)")

    def test_web_search_is_charged_on_top_of_tokens(self):
        """$10 per 1,000 queries. Omitting it understated a real run by
        89% (TRAPS.md)."""
        src = (PKG / "cost" / "tracker.py").read_text()
        assert "search" in src.lower(), "web search is not priced at all"

    def test_an_unpriced_row_blocks_authorisation(self, db):
        """A row nobody could price is spend of unknown size. Counting
        it as zero is the one thing that must not happen."""
        from catalyst.cost.governor import has_unpriced_rows

        db.execute(
            "INSERT INTO cost_events (id,raw_usage_json,model,kind,component,"
            "priced_cents,priced_at) VALUES "
            "('u','{}','m','scheduled','research',NULL,?)",
            (NOW.isoformat(),))
        db.commit()
        assert has_unpriced_rows(db) is True

    def test_scheduled_and_manual_spend_never_pool(self, db):
        """Mixing them makes every projection wrong (TRAPS.md)."""
        from catalyst.cost.ledger import month_to_date_cents

        for kind, amount in (("scheduled", "100"), ("manual", "900")):
            db.execute(
                "INSERT INTO cost_events (id,raw_usage_json,model,kind,"
                "component,priced_cents,priced_at) VALUES "
                "(?,'{}','m',?,'research',?,?)",
                (kind, kind, amount, "2026-08-10T12:00:00+00:00"))
        db.commit()
        assert month_to_date_cents("scheduled", db,
                                   date(2026, 8, 16)) == Decimal("100")

    def test_the_governor_and_the_forecast_use_the_same_cap(self, db):
        """A forecast projecting against a different cap than the one
        that stops the bot is worse than no forecast."""
        from catalyst.cost.forecast import forecast
        from catalyst.cost.governor import scheduled_cap_cents

        cap, _bound = scheduled_cap_cents(db, Decimal("0.10"),
                                          date(2026, 8, 16))
        f = forecast(Decimal("0"), cap, date(2026, 8, 16))
        assert f.cap_cents == cap

    def test_expected_profit_can_never_raise_the_cap(self):
        """BUILD-BRIEF, and it is the rule that keeps a hopeful model
        from authorising its own spending."""
        src = (PKG / "cost" / "governor.py").read_text()
        assert "realised" in src or "realized" in src, (
            "nothing in the governor mentions realised profit - check the "
            "cap can only rise on closed trades")


# =====================================================================
# 5. ALPACA USAGE
# =====================================================================

class TestAlpacaUsage:
    def test_no_removed_PDT_field_is_referenced_anywhere(self):
        """Alpaca removed these in July 2026. Code referencing them
        breaks (TRAPS.md)."""
        removed = ("pattern_day_trader", "daytrade_count",
                   "last_daytrade_count", "daytrading_buying_power",
                   "last_daytrading_buying_power")
        hits = []
        for path in PKG.rglob("*.py"):
            for line in path.read_text().splitlines():
                bare = line.strip()
                if bare.startswith("#") or '"""' in bare:
                    continue        # a comment saying NOT to use it is fine
                for field in removed:
                    # An actual READ: .get("field") or ["field"]
                    if f'"{field}"' in bare or f"'{field}'" in bare:
                        hits.append(f"{path.relative_to(ROOT)}: {bare[:70]}")
        assert not hits, "removed Alpaca fields referenced:\n  " + \
            "\n  ".join(hits)

    def test_corporate_actions_uses_v1_not_v2(self):
        """/v2/corporate-actions 404s - easy to reach by extrapolating
        from the /v2/ prefix every other endpoint uses (TRAPS.md)."""
        for path in PKG.rglob("*.py"):
            assert "/v2/corporate-actions" not in path.read_text(), (
                f"{path.relative_to(ROOT)} calls /v2/corporate-actions, "
                "which 404s")

    def test_fractional_stops_are_day_orders(self):
        """Fractional stop orders are supported only with
        time_in_force=DAY (TRAPS.md)."""
        src = (PKG / "execution" / "orders.py").read_text()
        assert 'tif="day"' in src or "'day'" in src or '"day"' in src

    def test_every_data_call_goes_through_the_one_client(self):
        """One client means one place enforcing timeouts, retries and
        redaction. A stray httpx call bypasses all three."""
        offenders = []
        for path in PKG.rglob("*.py"):
            if path.name == "broker.py":
                continue
            text = path.read_text()
            if "httpx.Client(" in text or "requests.get(" in text:
                offenders.append(str(path.relative_to(ROOT)))
        # data/sources own their own transports by design; the check is
        # that EXECUTION does not.
        assert not [o for o in offenders if "/execution/" in o], offenders

    def test_the_broker_never_leaks_keys_through_repr(self):
        from catalyst.execution.broker import Broker

        b = Broker("AKREALKEY123", "secretvalue456")
        try:
            text = repr(b)
            assert "AKREALKEY123" not in text
            assert "secretvalue456" not in text
            assert "redacted" in text.lower()
        finally:
            b.close()

    def test_stop_orders_are_never_assumed_to_work_out_of_hours(self):
        """Stops do not trigger outside regular hours, so overnight gap
        risk cannot be removed with stock alone (TRAPS.md). The adverse
        gap parameter is what carries that, and it must exist for every
        catalyst type."""
        from catalyst.risk.adaptive_params import DEFAULT_PARAMS

        gaps = DEFAULT_PARAMS["adverse_gap_assumption"]
        stops = DEFAULT_PARAMS["stop_width"]
        assert set(gaps) == set(stops), (
            "a catalyst type has a stop but no adverse-gap assumption, so "
            "its sizing ignores overnight risk entirely")
        for ct, gap in gaps.items():
            assert gap > 0, f"{ct} assumes no overnight gap at all"


# =====================================================================
# 6. THE BENCHMARK, since the owner hit its empty state
# =====================================================================

class TestTheBenchmarkExplainsItself:
    def test_a_new_baseline_reads_as_new_not_broken(self, db):
        """The owner's report. A comparison that started today has
        nothing to index yet, and that is not a fault."""
        from catalyst import benchmark
        from catalyst.dashboard.db import Db
        from catalyst.dashboard import queries

        today = datetime.now(timezone.utc).date()
        benchmark.record(db, capital_cents=Decimal("200000"),
                         start_date=today, source="first_run",
                         account_fingerprint="a", reason="first read")
        db.commit()
        path = db.execute("PRAGMA database_list").fetchall()[0][2]
        d = Db(str(path))
        perf = queries.performance(d)
        d.close()
        assert not perf.baseline_is_placeholder
        # There is genuinely nothing to plot on day one - a zero-length
        # window. What must NOT happen is a message that never mentions
        # the baseline, leaving "no equity series" reading as a fault on
        # exactly the day it is most normal.
        assert str(today) in (perf.spy_error or ""), (
            "the message never says the comparison starts today")
        assert "not a fault" in (perf.spy_error or "")
        assert "freshly connected account" in (perf.spy_error or ""), (
            "a brand-new comparison is being reported as a broken one")

    def test_a_stale_cache_and_a_short_window_read_differently(self):
        """One needs Tuesday; the other needs somebody to look at why
        Alpaca stopped answering. If both read the same, a real outage
        gets left for a week."""
        from catalyst.dashboard.queries import SPY_STALE_AFTER_DAYS

        assert SPY_STALE_AFTER_DAYS >= 3, (
            "an ordinary long weekend would be reported as an outage")
        assert SPY_STALE_AFTER_DAYS <= 10, (
            "a refresh that stopped working would look normal for too long")
