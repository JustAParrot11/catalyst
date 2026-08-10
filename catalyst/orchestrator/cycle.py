"""One cycle: kill switches -> reconcile -> stops/exits -> discovery ->
research -> risk -> execution, in that order, always.

The funnel is recorded stage by stage with drop reasons (BUILD-BRIEF
dashboard requirement: when it has not traded, name the stage
responsible). Every zero keeps its raw upstream response beside it -
a feed failure lands in raw_events_errors verbatim, never as a silent
empty list.

Dependency injection everywhere (broker, transport, feed, clock) so the
whole cycle runs offline under test; the systemd entry point (stage 7)
supplies the live pieces.
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from catalyst.discovery import Candidate
from catalyst.execution.broker import Broker, BrokerError
from catalyst.execution.exits import manage_exits, reopen_stops
from catalyst.execution.orders import confirm_stops_resting, place, place_stop
from catalyst.execution.reconcile import close_filled_positions, reconcile
from catalyst.research.boundary import CostContext, investigate
from catalyst.risk import (
    KillSwitchState, MarketSnapshot, OpenPosition, PortfolioState,
)
from catalyst.risk.adaptive_params import current_values
from catalyst.risk.evaluate import evaluate
from catalyst.risk.hard_bounds import HARD_BOUNDS
from catalyst.risk.kill_switches import check as kill_check

MAX_RESEARCH_PER_CYCLE = 3     # bounds worst-case spend per cycle; the
                               # governor is the real cap, this is belt


@dataclass
class CycleReport:
    cycle_id: str
    started_at: datetime
    kill_switch: KillSwitchState
    funnel: dict = field(default_factory=dict)        # stage -> count
    drop_reasons: dict = field(default_factory=dict)  # stage -> [reasons]
    errors: list = field(default_factory=list)


def build_portfolio_state(broker: Broker, conn,
                          now: datetime | None = None) -> PortfolioState | None:
    """ONLY from a confirmed broker read plus local position rows.
    Any broker failure returns None -> kill switches fail closed."""
    now = now or datetime.now(timezone.utc)
    try:
        acct = broker.get_account()
    except BrokerError:
        return None
    try:
        equity = Decimal(str(acct["equity"]))
        # cash account: non_marginable_buying_power is the settled figure
        # when Alpaca provides it; plain cash otherwise.
        settled = Decimal(str(acct.get("non_marginable_buying_power")
                              or acct["cash"]))
        day_pnl = equity - Decimal(str(acct.get("last_equity", equity)))
    except (KeyError, ArithmeticError, TypeError):
        return None

    rows = conn.execute(
        """SELECT p.id, p.ticker, p.opened_at, p.planned_exit_date,
                  d.notional_usd, d.adaptive_params_snapshot
           FROM positions p
           LEFT JOIN orders o ON o.id = json_extract(p.entry_order_ids, '$[0]')
           LEFT JOIN risk_decisions d ON d.candidate_id = o.decision_id
           WHERE p.status = 'open'""").fetchall()
    open_positions = tuple(
        OpenPosition(
            position_id=r[0], ticker=r[1],
            notional_usd=Decimal(r[4]) if r[4] else Decimal("0"),
            cluster_key=(json.loads(r[5]).get("_cluster_key", "")
                         if r[5] else ""),
            opened_at_date=datetime.fromisoformat(r[2]).date(),
            planned_exit_date=datetime.fromisoformat(r[3]).date())
        for r in rows)

    return PortfolioState(
        equity_usd=equity, settled_cash_usd=settled,
        open_positions=open_positions, day_pnl_usd=day_pnl,
        peak_equity_usd=_peak_equity(conn, equity),
        consecutive_losses=_consecutive_losses(conn),
        as_of=now, reliable=True)


def _peak_equity(conn, current_equity: Decimal) -> Decimal:
    """High-water mark approximated from the realized P&L path at trade
    closes (capital base $1,000) plus current equity. Slightly
    conservative between closes; never below current equity."""
    peak = Decimal("1000")
    running = Decimal("1000")
    for (pnl_cents,) in conn.execute(
            "SELECT realized_pnl_cents FROM closed_trades ORDER BY closed_at"):
        running += Decimal(pnl_cents) / 100
        peak = max(peak, running)
    return max(peak, current_equity)


def _consecutive_losses(conn) -> int:
    n = 0
    for (pnl,) in conn.execute(
            "SELECT realized_pnl_cents FROM closed_trades "
            "ORDER BY closed_at DESC"):
        if pnl < 0:
            n += 1
        else:
            break
    return n


def run_cycle(conn, broker: Broker, transport, feed_fetch, build_candidates_fn,
              cluster_fn, *, now: datetime | None = None,
              kind: str = "scheduled",
              max_research: int = MAX_RESEARCH_PER_CYCLE) -> CycleReport:
    now = now or datetime.now(timezone.utc)
    cycle_id = str(uuid.uuid4())
    params = current_values(conn)
    report = CycleReport(cycle_id=cycle_id, started_at=now,
                         kill_switch=KillSwitchState(False, None))

    # ---- 1. kill switches, before anything else
    portfolio = build_portfolio_state(broker, conn, now)
    ks = kill_check(portfolio, HARD_BOUNDS)
    report.kill_switch = ks
    if ks.tripped:
        conn.execute(
            """INSERT INTO kill_switch_events
               (triggered_at, switch_name, portfolio_state_snapshot)
               VALUES (?,?,?)""",
            (now.isoformat(), ks.reason,
             json.dumps(_portfolio_snapshot(portfolio))))
        conn.commit()
        report.funnel["kill_switch"] = 1
        return report

    # daily equity mark from the confirmed broker read (dashboard's
    # performance-vs-SPY panel needs real marks, not reconstructions)
    conn.execute(
        """INSERT OR REPLACE INTO equity_snapshots
           (day, taken_at, equity_usd, settled_cash_usd,
            positions_notional, source)
           VALUES (?,?,?,?,?,?)""",
        (now.date().isoformat(), now.isoformat(), str(portfolio.equity_usd),
         str(portfolio.settled_cash_usd),
         str(sum((p.notional_usd for p in portfolio.open_positions),
                 Decimal("0"))),
         "broker_read"))
    conn.commit()

    # ---- 2. reconcile what already happened before deciding anything new
    try:
        reconcile(broker, conn)
    except BrokerError as exc:
        report.errors.append(f"reconcile: {exc}")
        return report          # cannot trust local state; stop the cycle
    close_filled_positions(conn, now=now)

    # score refusals whose counterfactual window has elapsed (the
    # feedback loop; failures leave rows unscored for the next cycle)
    try:
        from catalyst.risk.refusal_tracker import score_due_refusals
        score_due_refusals(broker, conn, now)
    except BrokerError as exc:
        report.errors.append(f"refusal_scoring: {exc}")

    # ---- 3. session stop duties + hard-date exits
    open_rows = _open_position_dicts(conn, now)
    if open_rows:
        confirmations = confirm_stops_resting(open_rows, broker, conn)
        unprotected = [
            p for p, c in zip(open_rows, confirmations)
            if c.status == "unprotected" and p.get("stop_price") is not None
            and not p["due"]]
        if unprotected:
            reopen_stops(unprotected, broker, conn)
        due = [p for p in open_rows if p["due"]]
        if due:
            manage_exits(due, now, broker, conn)

    # ---- 4. discovery
    since = now - timedelta(days=5)
    try:
        events = feed_fetch(since, now)
    except Exception as exc:   # FeedError and anything transport-shaped
        raw_text = getattr(exc, "raw_text", None) or repr(exc)
        conn.execute(
            "INSERT INTO raw_events_errors (source, attempted_at, error_text) "
            "VALUES (?,?,?)",
            ("edgar_form4", now.isoformat(), str(raw_text)))
        conn.commit()
        report.errors.append(f"feed: {type(exc).__name__}")
        # unreachable is NOT empty (build brief): name the stage and stop
        report.funnel["raw_events"] = 0
        report.drop_reasons["raw_events"] = [
            "feed_unreachable_see_raw_events_errors"]
        return report

    report.funnel["raw_events"] = len(events)
    for ev in events:
        conn.execute(
            "INSERT OR IGNORE INTO raw_events VALUES (?,?,?,?)",
            (ev.source, ev.source_id, ev.fetched_at.isoformat(),
             json.dumps(ev.payload_raw)))
    conn.commit()

    candidates = build_candidates_fn(events, now)
    report.funnel["candidates"] = len(candidates)

    fresh: list[Candidate] = []
    screen_reasons: list[str] = []
    open_tickers = {p["ticker"] for p in open_rows}
    for c in candidates:
        if conn.execute("SELECT 1 FROM research_views WHERE candidate_id=?",
                        (c.id,)).fetchone():
            screen_reasons.append(f"{c.id}: already_researched")
        elif c.ticker in open_tickers:
            screen_reasons.append(f"{c.id}: position_already_open")
        else:
            fresh.append(c)
            conn.execute(
                "INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                (c.id, c.ticker, c.catalyst_type, c.catalyst_date.isoformat(),
                 c.catalyst_date_confidence,
                 json.dumps(list(c.source_event_ids)),
                 c.discovered_at.isoformat(), c.sector,
                 json.dumps(list(c.correlation_tags))))
    conn.commit()
    report.drop_reasons["screened"] = screen_reasons
    report.funnel["screened"] = len(fresh)

    cluster_keys = cluster_fn(fresh, list(portfolio.open_positions))

    # ---- 5. research -> risk -> execution per candidate
    researched = proposed = placed = 0
    for c in fresh[:max_research]:
        if transport is None:
            report.drop_reasons.setdefault("researched", []).append(
                f"{c.id}: no_model_transport_configured")
            continue
        market = build_market_snapshot(broker, c.ticker)
        if market is None:
            report.drop_reasons.setdefault("researched", []).append(
                f"{c.id}: no_market_quote")
            continue
        log = investigate(
            c, CostContext(conn=conn,
                           governor_profit_share=Decimal(
                               str(params["governor_profit_share"])),
                           cycle_id=cycle_id, kind=kind),
            transport, graph_context=_graph_context(c, conn))
        if log.parsed_view is None:
            report.drop_reasons.setdefault("researched", []).append(
                f"{c.id}: {log.skipped_reason}")
            continue
        researched += 1

        decision = evaluate(c, log.parsed_view, portfolio, params, market,
                            cluster_key=cluster_keys.get(c.id, ""))
        decision_id = _persist_decision(conn, decision,
                                        cluster_keys.get(c.id, ""))
        if decision.action == "skip":
            conn.execute(
                """INSERT INTO refusals
                   (decision_id, candidate_id, price_at_refusal, refused_at)
                   VALUES (?,?,?,?)""",
                (decision_id, c.id, str(market.last_close), now.isoformat()))
            conn.commit()
            report.drop_reasons.setdefault("proposed", []).append(
                f"{c.id}: {','.join(decision.skip_reasons)}")
            continue
        proposed += 1

        entry = place(decision, c.ticker, broker, conn)
        if entry.status == "rejected":
            report.drop_reasons.setdefault("orders_placed", []).append(
                f"{c.id}: entry_rejected")
            continue
        stop = place_stop(decision_id=c.id, ticker=c.ticker,
                          qty=decision.qty, stop_price=decision.stop_price,
                          broker=broker, conn=conn)
        entry_order_row = conn.execute(
            "SELECT id FROM orders WHERE decision_id=? AND side='buy' "
            "ORDER BY submitted_at DESC LIMIT 1", (c.id,)).fetchone()
        conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), c.ticker,
             json.dumps([entry_order_row[0]] if entry_order_row else []),
             stop.broker_order_id, now.isoformat(),
             decision.planned_exit_date.isoformat(), "open"))
        conn.commit()
        placed += 1
        # keep in-cycle exposure honest for the NEXT candidate
        portfolio = _with_position(portfolio, c, decision,
                                   cluster_keys.get(c.id, ""), now)

    report.funnel["researched"] = researched
    report.funnel["proposed"] = proposed
    report.funnel["orders_placed"] = placed
    return report


def build_market_snapshot(broker: Broker, ticker: str) -> MarketSnapshot | None:
    """Live NBBO at decision time - independent of anything Claude said."""
    try:
        q = broker.get_latest_quote(ticker)
    except BrokerError:
        return None
    quote = q.get("quote") or {}
    bid, ask = quote.get("bp"), quote.get("ap")
    try:
        bid, ask = Decimal(str(bid)), Decimal(str(ask))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    half_spread_bp = (ask - bid) / 2 / mid * Decimal("10000")
    return MarketSnapshot(
        ticker=ticker, last_close=mid,
        half_spread_bp=half_spread_bp.quantize(Decimal("0.1")),
        # not consumed by any current sizing rule; populated when one is
        median_daily_dollar_volume=Decimal("0"))


def _graph_context(candidate: Candidate, conn) -> str | None:
    try:
        from catalyst.graph.hooks import graph_context_for_candidate
        return graph_context_for_candidate(candidate, conn)
    except Exception:
        return None            # graph informs, it never blocks a cycle


def _persist_decision(conn, decision, cluster_key: str) -> str:
    decision_id = str(uuid.uuid4())
    snapshot = {**{k: _jsonable(v) for k, v in
                   decision.adaptive_params_snapshot.items()},
                "_cluster_key": cluster_key}
    conn.execute(
        """INSERT INTO risk_decisions
           (id, candidate_id, action, side, notional_usd, qty, stop_price,
            planned_exit_date, skip_reasons, adaptive_params_snapshot,
            decided_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (decision_id, decision.candidate_id, decision.action, decision.side,
         str(decision.notional_usd) if decision.notional_usd is not None else None,
         str(decision.qty) if decision.qty is not None else None,
         str(decision.stop_price) if decision.stop_price is not None else None,
         decision.planned_exit_date.isoformat()
         if decision.planned_exit_date else None,
         json.dumps(list(decision.skip_reasons)), json.dumps(snapshot),
         datetime.now(timezone.utc).isoformat()))
    for lim in decision.limits_applied:
        conn.execute(
            "INSERT INTO limit_applications VALUES (?,?,?,?,?,?)",
            (decision_id, lim.rule_name, str(lim.bound_value),
             str(lim.requested_value), lim.bound_type, int(lim.binding)))
    conn.commit()
    return decision_id


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, Decimal):
        return str(v)
    return v


def _open_position_dicts(conn, now: datetime) -> list[dict]:
    today = now.date().isoformat()
    rows = conn.execute(
        """SELECT p.id, p.ticker, p.stop_order_id, p.planned_exit_date,
                  o.decision_id, o.qty, d.stop_price
           FROM positions p
           LEFT JOIN orders o ON o.id = json_extract(p.entry_order_ids, '$[0]')
           LEFT JOIN risk_decisions d ON d.candidate_id = o.decision_id
           WHERE p.status = 'open'""").fetchall()
    return [
        {"id": r[0], "ticker": r[1], "stop_order_id": r[2],
         "decision_id": r[4], "qty": r[5], "stop_price": r[6],
         "due": r[3] <= today}
        for r in rows]


def _with_position(portfolio: PortfolioState, candidate, decision,
                   cluster_key: str, now: datetime) -> PortfolioState:
    new_pos = OpenPosition(
        position_id=f"pending-{candidate.id}", ticker=candidate.ticker,
        notional_usd=decision.notional_usd, cluster_key=cluster_key,
        opened_at_date=now.date(),
        planned_exit_date=decision.planned_exit_date)
    return PortfolioState(
        equity_usd=portfolio.equity_usd,
        settled_cash_usd=portfolio.settled_cash_usd - decision.notional_usd,
        open_positions=portfolio.open_positions + (new_pos,),
        day_pnl_usd=portfolio.day_pnl_usd,
        peak_equity_usd=portfolio.peak_equity_usd,
        consecutive_losses=portfolio.consecutive_losses,
        as_of=portfolio.as_of, reliable=portfolio.reliable)


def _portfolio_snapshot(p: PortfolioState | None) -> dict:
    if p is None:
        return {"portfolio": None, "reason": "broker_read_failed"}
    return {"equity_usd": str(p.equity_usd),
            "settled_cash_usd": str(p.settled_cash_usd),
            "open_positions": len(p.open_positions),
            "day_pnl_usd": str(p.day_pnl_usd),
            "peak_equity_usd": str(p.peak_equity_usd),
            "consecutive_losses": p.consecutive_losses,
            "as_of": p.as_of.isoformat(), "reliable": p.reliable}
