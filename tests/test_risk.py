"""Risk engine tests: sizing clamps and gates, evaluate gating,
kill-switch fail-closed behavior, adaptive parameter rules.

Fully offline (conftest blocks sockets and strips credentials).
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.discovery import Candidate
from catalyst.research.schema import ResearchView
from catalyst.risk import (
    KillSwitchState, MarketSnapshot, OpenPosition, PortfolioState,
)
from catalyst.risk import adaptive_params as ap
from catalyst.risk.evaluate import evaluate
from catalyst.risk.hard_bounds import HARD_BOUNDS, HardBounds
from catalyst.risk.kill_switches import MAX_CONSECUTIVE_LOSSES, check
from catalyst.risk.sizing import size

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


def portfolio(equity="1000", settled="1000", positions=(), day_pnl="0",
              peak="1000", losses=0, as_of=NOW, reliable=True):
    return PortfolioState(
        equity_usd=Decimal(equity), settled_cash_usd=Decimal(settled),
        open_positions=tuple(positions), day_pnl_usd=Decimal(day_pnl),
        peak_equity_usd=Decimal(peak), consecutive_losses=losses,
        as_of=as_of, reliable=reliable)


def market(half_spread="8", close="50.00"):
    return MarketSnapshot(ticker="TEST", last_close=Decimal(close),
                          half_spread_bp=Decimal(half_spread),
                          median_daily_dollar_volume=Decimal("5000000"))


def open_pos(notional, cluster="x", pid="p1"):
    return OpenPosition(position_id=pid, ticker="AAA",
                        notional_usd=Decimal(notional), cluster_key=cluster,
                        opened_at_date=date(2026, 8, 1),
                        planned_exit_date=date(2026, 8, 20))


PARAMS = {
    "conviction_floor": Decimal("0.60"),
    "adverse_gap_assumption": {"insider_cluster": Decimal("0.08")},
    "stop_width": {"insider_cluster": Decimal("0.10")},
    "holding_period_estimate": {"insider_cluster": Decimal("12")},
    "search_budget_allocation": {"insider_cluster": Decimal("1.0")},
    "governor_profit_share": Decimal("0.10"),
}


def candidate():
    return Candidate(
        id="cand-1", ticker="TEST", catalyst_type="insider_cluster",
        catalyst_date=date(2026, 8, 20), catalyst_date_confidence="estimated",
        source_event_ids=("e1",), discovered_at=NOW, sector="tech",
        correlation_tags=("tech",))


def view(direction="long", conviction=0.8, priced_in=False):
    return ResearchView(
        candidate_id="cand-1", direction=direction, conviction=conviction,
        thesis="t", invalidation="i", expected_holding_days=12,
        priced_in=priced_in, priced_in_reasoning="r")


# ---------------------------------------------------------------- sizing

class TestSizing:
    def test_gate_not_passed_skips_with_no_numbers(self):
        r = size(False, "insider_cluster", portfolio(), PARAMS,
                 HARD_BOUNDS, market())
        assert r.action == "skip"
        assert r.skip_reasons == ("gate_not_passed",)
        assert r.notional_usd is None and r.qty is None and r.stop_price is None

    def test_spread_gate_is_hard_and_binding(self):
        r = size(True, "insider_cluster", portfolio(), PARAMS,
                 HARD_BOUNDS, market(half_spread="21"))
        assert r.action == "skip"
        assert r.skip_reasons == ("spread_gate",)
        [lim] = r.limits_applied
        assert lim.rule_name == "max_entry_half_spread_bp"
        assert lim.bound_type == "hard" and lim.binding

    def test_spread_exactly_at_bound_passes(self):
        r = size(True, "insider_cluster", portfolio(), PARAMS,
                 HARD_BOUNDS, market(half_spread="20"))
        assert r.action == "trade"

    def test_no_free_slot(self):
        pos = [open_pos("100", pid=f"p{i}") for i in range(HARD_BOUNDS.max_open_positions)]
        r = size(True, "insider_cluster", portfolio(positions=pos), PARAMS,
                 HARD_BOUNDS, market())
        assert r.skip_reasons == ("no_free_slot",)

    def test_worst_case_uses_max_of_gap_and_stop(self):
        # gap 0.30 > stop 0.10: notional = 1000*0.02/0.30 = 66.67 - below
        # the slot ceiling, so the gap governs size.
        params = {**PARAMS,
                  "adverse_gap_assumption": {"insider_cluster": Decimal("0.30")}}
        r = size(True, "insider_cluster", portfolio(), params,
                 HARD_BOUNDS, market())
        assert r.action == "trade"
        assert r.notional_usd == Decimal("66.67")

    def test_equal_weight_slot_clamp(self):
        # worst_case 0.10 -> raw notional 1000*0.02/0.10 = 200 = slot
        # ceiling exactly; drop gap so raw = 200 > ceiling? equal here.
        # Use stop 0.04/gap 0.04 -> raw 500, clamped to 200.
        params = {**PARAMS,
                  "adverse_gap_assumption": {"insider_cluster": Decimal("0.04")},
                  "stop_width": {"insider_cluster": Decimal("0.04")}}
        r = size(True, "insider_cluster", portfolio(), params,
                 HARD_BOUNDS, market())
        assert r.notional_usd == Decimal("200.00")
        assert any(l.rule_name == "equal_weight_slot" and l.binding
                   for l in r.limits_applied)

    def test_total_exposure_clamp(self):
        # 850 deployed of 900 allowed -> only 50 of room.
        pos = [open_pos("425", pid="p1"), open_pos("425", pid="p2", cluster="y")]
        r = size(True, "insider_cluster", portfolio(settled="500", positions=pos),
                 PARAMS, HARD_BOUNDS, market())
        assert r.action == "trade"
        assert r.notional_usd == Decimal("50.00")
        assert any(l.rule_name == "max_total_exposure" and l.binding
                   for l in r.limits_applied)

    def test_correlated_cluster_clamp(self):
        # same cluster already holds 300; cluster cap 0.35*1000=350.
        pos = [open_pos("300", cluster="biotech-aug")]
        r = size(True, "insider_cluster", portfolio(positions=pos), PARAMS,
                 HARD_BOUNDS, market(), cluster_key="biotech-aug")
        assert r.notional_usd == Decimal("50.00")
        assert any(l.rule_name == "max_correlated_cluster" and l.binding
                   for l in r.limits_applied)

    def test_settled_cash_clamp(self):
        r = size(True, "insider_cluster", portfolio(settled="120"), PARAMS,
                 HARD_BOUNDS, market())
        assert r.notional_usd == Decimal("120.00")
        assert any(l.rule_name == "settled_cash" and l.binding
                   for l in r.limits_applied)

    def test_dust_notional_skips(self):
        r = size(True, "insider_cluster", portfolio(settled="0.50"), PARAMS,
                 HARD_BOUNDS, market())
        assert r.skip_reasons == ("notional_below_minimum",)

    def test_qty_rounds_down_and_stop_below_close(self):
        r = size(True, "insider_cluster", portfolio(), PARAMS,
                 HARD_BOUNDS, market(close="49.99"))
        assert r.action == "trade"
        # qty truncated to 4dp, never rounded up past notional
        assert r.qty * Decimal("49.99") <= r.notional_usd + Decimal("0.01")
        assert r.stop_price == Decimal("44.99")  # 49.99 * 0.9

    def test_no_research_view_parameter_exists(self):
        # The boundary is structural: size() must not accept anything
        # shaped like a ResearchView. Signature inspection, not trust.
        import inspect
        sig = inspect.signature(size)
        assert "view" not in sig.parameters
        assert sig.parameters["passed_gate"].annotation in (bool, "bool")


# -------------------------------------------------------------- evaluate

class TestEvaluate:
    def test_long_above_floor_trades(self):
        d = evaluate(candidate(), view(), portfolio(), PARAMS, market())
        assert d.action == "trade" and d.side == "long"
        assert d.planned_exit_date == NOW.date() + timedelta(days=12)
        assert d.adaptive_params_snapshot["conviction_floor"] == Decimal("0.60")

    def test_below_conviction_floor_skips(self):
        d = evaluate(candidate(), view(conviction=0.59), portfolio(),
                     PARAMS, market())
        assert d.action == "skip"
        assert "below_conviction_floor" in d.skip_reasons

    def test_short_view_skips_with_named_reason(self):
        d = evaluate(candidate(), view(direction="short"), portfolio(),
                     PARAMS, market())
        assert d.action == "skip"
        assert "short_unavailable_cash_account" in d.skip_reasons

    def test_priced_in_skips(self):
        d = evaluate(candidate(), view(priced_in=True), portfolio(),
                     PARAMS, market())
        assert d.action == "skip"
        assert "model_judged_priced_in" in d.skip_reasons

    def test_no_trade_direction_skips(self):
        d = evaluate(candidate(), view(direction="no_trade"), portfolio(),
                     PARAMS, market())
        assert "model_no_trade" in d.skip_reasons

    def test_conviction_cannot_scale_size(self):
        # Same portfolio, conviction 0.61 vs 0.99: identical size. The
        # model's confidence gates, it never sizes.
        lo = evaluate(candidate(), view(conviction=0.61), portfolio(),
                      PARAMS, market())
        hi = evaluate(candidate(), view(conviction=0.99), portfolio(),
                      PARAMS, market())
        assert lo.notional_usd == hi.notional_usd
        assert lo.qty == hi.qty


# --------------------------------------------------------- kill switches

class TestKillSwitches:
    def test_none_portfolio_fails_closed(self):
        s = check(None, HARD_BOUNDS)
        assert s.tripped and s.reason == "portfolio_state_unreliable"

    def test_unreliable_portfolio_fails_closed(self):
        s = check(portfolio(reliable=False), HARD_BOUNDS)
        assert s.tripped and s.reason == "portfolio_state_unreliable"

    def test_stale_snapshot_fails_closed(self):
        stale = portfolio(as_of=datetime.now(timezone.utc) - timedelta(minutes=11))
        s = check(stale, HARD_BOUNDS)
        assert s.tripped and s.reason == "portfolio_state_stale"

    def test_daily_loss_kill(self):
        p = portfolio(day_pnl="-41", as_of=datetime.now(timezone.utc))
        s = check(p, HARD_BOUNDS)  # 4% of 1000 = 40
        assert s.tripped and s.reason == "daily_loss_kill"

    def test_drawdown_kill(self):
        p = portfolio(equity="870", peak="1000",
                      as_of=datetime.now(timezone.utc))
        s = check(p, HARD_BOUNDS)  # 13% > 12%
        assert s.tripped and s.reason == "drawdown_kill"

    def test_consecutive_losses_kill(self):
        p = portfolio(losses=MAX_CONSECUTIVE_LOSSES,
                      as_of=datetime.now(timezone.utc))
        s = check(p, HARD_BOUNDS)
        assert s.tripped and s.reason == "consecutive_losses_kill"

    def test_healthy_portfolio_passes(self):
        s = check(portfolio(as_of=datetime.now(timezone.utc)), HARD_BOUNDS)
        assert s == KillSwitchState(tripped=False, reason=None)


# ----------------------------------------------------- adaptive params

def evidence(param="conviction_floor", n=30, effect="-0.5", sig="0.95",
             strength="1.0",
             start=NOW - timedelta(days=60), end=NOW - timedelta(days=1)):
    return ap.EvidenceSample(
        parameter=param, trade_ids=tuple(f"t{i}" for i in range(n)),
        window_start=start, window_end=end,
        effect_size=Decimal(effect), significance=Decimal(sig),
        evidence_strength=Decimal(strength))


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.executescript(
        open("catalyst/storage/schema.sql").read())
    # apply() enforces closed-scored-outcome provenance (risk review F2):
    # the synthetic evidence ids t0..t59 must exist as SCORED refusals
    for i in range(60):
        conn.execute(
            "INSERT INTO refusals (decision_id, candidate_id, "
            "price_at_refusal, refused_at, scored_at, outcome_price, "
            "outcome_return) VALUES (?,?,?,?,?,?,?)",
            (f"d{i}", f"t{i}", "50", NOW.isoformat(), NOW.isoformat(),
             "55", "0.1"))
    conn.commit()
    yield conn
    conn.close()


class TestAdaptiveParams:
    def test_insufficient_sample_refused(self):
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                  evidence(n=29))
        assert not p.applicable
        assert p.reason.startswith("insufficient_sample")

    def test_insufficient_significance_refused(self):
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                  evidence(sig="0.89"))
        assert not p.applicable

    def test_loosen_step_is_a_third_of_tighten(self):
        # conviction_floor: raising = tighten. Full strength both ways.
        t = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                  evidence(effect="1"))
        l = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                  evidence(effect="-1"))
        assert t.direction == "tighten" and l.direction == "loosen"
        t_step = t.proposed_value - t.old_value
        l_step = l.old_value - l.proposed_value
        assert t_step == Decimal("0.03")
        assert l_step == Decimal("0.01")

    def test_apply_writes_log_and_current_values_reads_it(self, db):
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                  evidence(effect="1"))
        out = ap.apply(p, HARD_BOUNDS, ap.current_values(db), db)
        assert out.applied, out.refusal_reason
        assert ap.current_values(db)["conviction_floor"] == Decimal("0.63")
        row = db.execute("SELECT reverses_to, sample_ids FROM adaptive_param_log").fetchone()
        assert row[0] == "0.60"
        assert len(json.loads(row[1])) == 30

    def test_overlapping_evidence_window_refused(self, db):
        p1 = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                   evidence(effect="1"))
        assert ap.apply(p1, HARD_BOUNDS, ap.current_values(db), db).applied
        # second proposal's window starts inside the first's
        p2 = ap.propose_adjustment("conviction_floor", Decimal("0.63"),
                                   evidence(effect="1",
                                            start=NOW - timedelta(days=30),
                                            end=NOW))
        out = ap.apply(p2, HARD_BOUNDS, ap.current_values(db), db)
        assert not out.applied
        assert out.refusal_reason.startswith("evidence_window_overlaps_previous")

    def test_disjoint_second_window_accepted(self, db):
        p1 = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                   evidence(effect="1"))
        assert ap.apply(p1, HARD_BOUNDS, ap.current_values(db), db).applied
        p2 = ap.propose_adjustment("conviction_floor", Decimal("0.63"),
                                   evidence(effect="1", start=NOW,
                                            end=NOW + timedelta(days=30)))
        assert ap.apply(p2, HARD_BOUNDS, ap.current_values(db), db).applied

    def test_stale_proposal_refused(self, db):
        p = ap.propose_adjustment("conviction_floor", Decimal("0.55"),
                                  evidence(effect="1"))
        out = ap.apply(p, HARD_BOUNDS, ap.current_values(db), db)
        assert not out.applied
        assert out.refusal_reason.startswith("stale_proposal")

    def test_range_ceiling_refusal_names_bound_and_margin(self, db):
        snapshot = ap.current_values(db)
        snapshot["conviction_floor"] = Decimal("0.94")
        p = ap.propose_adjustment("conviction_floor", Decimal("0.94"),
                                  evidence(effect="1"))
        out = ap.apply(p, HARD_BOUNDS, snapshot, db)
        assert not out.applied
        assert "range_ceiling" in out.refusal_reason
        assert "0.02" in out.refusal_reason  # 0.97 is 0.02 above 0.95

    def test_oversized_step_refused_even_if_proposal_lies(self, db):
        p = ap.AdjustmentProposal(
            parameter="conviction_floor", direction="tighten",
            old_value=Decimal("0.60"), proposed_value=Decimal("0.70"),
            evidence=evidence(), applicable=True, reason=None)
        out = ap.apply(p, HARD_BOUNDS, ap.current_values(db), db)
        assert not out.applied
        assert out.refusal_reason.startswith("step_exceeds_bound")

    def test_hard_bounds_have_no_adaptive_path(self):
        # No function in adaptive_params writes to HardBounds; the
        # dataclass is frozen and the module exposes no setter.
        from dataclasses import FrozenInstanceError
        with pytest.raises(FrozenInstanceError):
            object.__getattribute__(HARD_BOUNDS, "__class__")
            HARD_BOUNDS.max_loss_per_position_pct = Decimal("0.5")

    def test_per_catalyst_param_requires_leaf(self):
        with pytest.raises(ValueError):
            ap.propose_adjustment("stop_width", Decimal("0.10"), evidence())

    def test_scalar_param_rejects_leaf(self):
        with pytest.raises(ValueError):
            ap.propose_adjustment("conviction_floor.x", Decimal("0.6"),
                                  evidence())

    def test_auto_revert_on_opposing_post_sample(self, db):
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                  evidence(effect="-1"))  # loosen to 0.59
        assert ap.apply(p, HARD_BOUNDS, ap.current_values(db), db).applied
        assert ap.current_values(db)["conviction_floor"] == Decimal("0.59")
        # post-change evidence says raise it (opposes the loosening);
        # loosen reverts at a third of the minimum sample = 10.
        post = evidence(n=10, effect="1", start=NOW + timedelta(days=1),
                        end=NOW + timedelta(days=40))
        out = ap.maybe_auto_revert("conviction_floor", post, db)
        assert out.reverted
        assert out.restored_value == Decimal("0.60")
        assert ap.current_values(db)["conviction_floor"] == Decimal("0.60")

    def test_revert_refuses_pre_change_evidence(self, db):
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                  evidence(effect="-1"))
        assert ap.apply(p, HARD_BOUNDS, ap.current_values(db), db).applied
        pre = evidence(n=30, effect="1", start=NOW - timedelta(days=90),
                       end=NOW - timedelta(days=61))
        out = ap.maybe_auto_revert("conviction_floor", pre, db)
        assert not out.reverted
        assert "predates" in out.reason

    def test_revert_of_tighten_needs_full_sample(self, db):
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                  evidence(effect="1"))  # tighten
        assert ap.apply(p, HARD_BOUNDS, ap.current_values(db), db).applied
        post = evidence(n=10, effect="-1", start=NOW + timedelta(days=1),
                        end=NOW + timedelta(days=40))
        out = ap.maybe_auto_revert("conviction_floor", post, db)
        assert not out.reverted
        assert out.reason.startswith("insufficient_post_sample")


class TestAdaptiveHardening:
    """Risk review F1-F3: apply() must not trust its caller."""

    def test_hand_built_proposal_with_tiny_sample_refused(self, db):
        p = ap.AdjustmentProposal(
            parameter="conviction_floor", direction="tighten",
            old_value=Decimal("0.60"), proposed_value=Decimal("0.61"),
            evidence=evidence(n=1), applicable=True, reason=None)
        out = ap.apply(p, HARD_BOUNDS, ap.current_values(db), db)
        assert not out.applied
        assert out.refusal_reason.startswith("insufficient_sample")

    def test_hand_built_low_significance_refused(self, db):
        p = ap.AdjustmentProposal(
            parameter="conviction_floor", direction="tighten",
            old_value=Decimal("0.60"), proposed_value=Decimal("0.61"),
            evidence=evidence(sig="0.50"), applicable=True, reason=None)
        out = ap.apply(p, HARD_BOUNDS, ap.current_values(db), db)
        assert not out.applied
        assert out.refusal_reason.startswith("insufficient_significance")

    def test_unknown_evidence_ids_refused(self, db):
        ev = ap.EvidenceSample(
            parameter="conviction_floor",
            trade_ids=tuple(f"ghost{i}" for i in range(30)),
            window_start=NOW - timedelta(days=60),
            window_end=NOW - timedelta(days=1),
            effect_size=Decimal("1"), significance=Decimal("0.95"),
            evidence_strength=Decimal("1"))
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"), ev)
        out = ap.apply(p, HARD_BOUNDS, ap.current_values(db), db)
        assert not out.applied
        assert "evidence_not_closed_scored_outcome" in out.refusal_reason

    def test_unscored_refusal_id_refused(self, db):
        db.execute(
            "INSERT INTO refusals (decision_id, candidate_id, "
            "price_at_refusal, refused_at) VALUES (?,?,?,?)",
            ("dx", "unscored-1", "50", NOW.isoformat()))
        db.commit()
        ids = tuple(f"t{i}" for i in range(29)) + ("unscored-1",)
        ev = ap.EvidenceSample(
            parameter="conviction_floor", trade_ids=ids,
            window_start=NOW - timedelta(days=60),
            window_end=NOW - timedelta(days=1),
            effect_size=Decimal("1"), significance=Decimal("0.95"),
            evidence_strength=Decimal("1"))
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"), ev)
        out = ap.apply(p, HARD_BOUNDS, ap.current_values(db), db)
        assert not out.applied

    def test_reverted_adjustments_window_still_blocks_reuse(self, db):
        """F3: revert-then-reapply with the same evidence window must be
        refused - the window was spent, reverted or not."""
        p = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                  evidence(effect="-1"))  # loosen
        assert ap.apply(p, HARD_BOUNDS, ap.current_values(db), db).applied
        post = evidence(n=10, effect="1", start=NOW + timedelta(days=1),
                        end=NOW + timedelta(days=40))
        assert ap.maybe_auto_revert("conviction_floor", post, db).reverted
        # same original window again, value is back at 0.60
        p2 = ap.propose_adjustment("conviction_floor", Decimal("0.60"),
                                   evidence(effect="-1"))
        out = ap.apply(p2, HARD_BOUNDS, ap.current_values(db), db)
        assert not out.applied
        assert out.refusal_reason.startswith("evidence_window_overlaps_previous")
