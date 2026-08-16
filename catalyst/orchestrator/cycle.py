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
import sqlite3
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from catalyst.data.bar_history import ensure_history
from catalyst.discovery import Candidate
from catalyst.execution.broker import Broker, BrokerError
from catalyst.execution.exits import _neutralize_stop, manage_exits, reopen_stops
from catalyst.execution.orders import (
    confirm_stops_resting, place, place_stop, replace_stop,
)
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

# Broker statuses that mean "this order will never fill". An entry in
# one of these with nothing filled bought nothing, so it opens nothing.
_TERMINAL_UNFILLED = {"canceled", "expired", "done_for_day", "rejected",
                      "suspended", "stopped"}


@dataclass
class CycleReport:
    cycle_id: str
    started_at: datetime
    kill_switch: KillSwitchState
    funnel: dict = field(default_factory=dict)        # stage -> count
    drop_reasons: dict = field(default_factory=dict)  # stage -> [reasons]
    errors: list = field(default_factory=list)


def _finite(value) -> Decimal:
    """Decimal(value) that refuses NaN/Infinity. Python's json parses the
    non-standard NaN/Infinity literals, and Decimal builds happily from
    them - the failure only surfaces at the FIRST comparison, which for
    account equity is inside the kill switch. The one code path that
    exists to fail closed must not be the one that raises (stress-tester
    defects 3 and 4)."""
    dec = Decimal(str(value))
    if not dec.is_finite():
        raise ValueError(f"non-finite number from upstream: {value!r}")
    return dec


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
        equity = _finite(acct["equity"])
        # cash account settled funds: neither `cash` nor
        # `non_marginable_buying_power` is documented as exactly
        # "settled" (risk review F4), and `cash` can include unsettled
        # sale proceeds. Take the SMALLER of the two until the live
        # same-day-sale verification pins the semantics - sizing too
        # small is an opportunity cost, sizing on unsettled funds is a
        # good-faith violation.
        cash = _finite(acct["cash"])
        nmbp = acct.get("non_marginable_buying_power")
        settled = min(cash, _finite(nmbp)) if nmbp else cash
        day_pnl = equity - _finite(acct.get("last_equity", equity))
    except (KeyError, ArithmeticError, TypeError, ValueError):
        return None

    rows = conn.execute(
        """SELECT p.id, p.ticker, p.opened_at, p.planned_exit_date,
                  d.notional_usd, d.adaptive_params_snapshot
           FROM positions p
           LEFT JOIN orders o ON o.id = json_extract(
                CASE WHEN json_valid(p.entry_order_ids)
                     THEN p.entry_order_ids ELSE '[]' END, '$[0]')
           LEFT JOIN risk_decisions d ON d.candidate_id = o.decision_id
           WHERE p.status = 'open'""").fetchall()
    try:
        open_positions = tuple(
            OpenPosition(
                position_id=r[0], ticker=r[1],
                notional_usd=Decimal(r[4]) if r[4] else Decimal("0"),
                cluster_key=_cluster_key_of(r[5]),
                opened_at_date=datetime.fromisoformat(r[2]).date(),
                planned_exit_date=datetime.fromisoformat(r[3]).date())
            for r in rows)
    except (ArithmeticError, TypeError, ValueError):
        # A position row we cannot parse means exposure we cannot count.
        # Dropping it would understate exposure and let sizing
        # over-allocate, so the whole read is declared unreliable and the
        # kill switch stands the cycle down (stress-tester defect 22).
        return None

    return PortfolioState(
        equity_usd=equity, settled_cash_usd=settled,
        open_positions=open_positions, day_pnl_usd=day_pnl,
        peak_equity_usd=_peak_equity(conn, equity),
        consecutive_losses=_consecutive_losses(conn),
        as_of=now, reliable=True)


def _cluster_key_of(snapshot_json) -> str:
    """The stored cluster key, or '' if the snapshot is unreadable. An
    empty key is safe: cycle._fallback_cluster_key re-derives one, and an
    unkeyed position never silently bypasses the cluster bound."""
    if not snapshot_json:
        return ""
    try:
        loaded = json.loads(snapshot_json)
    except (TypeError, ValueError):
        return ""
    return loaded.get("_cluster_key", "") if isinstance(loaded, dict) else ""


def _peak_equity(conn, current_equity: Decimal) -> Decimal:
    """High-water mark: the max of every OBSERVED equity mark
    (equity_snapshots, written each healthy cycle), the realized P&L
    path at trade closes, and current equity. Persisted observations are
    what make the drawdown kill honest about unrealized peaks (risk
    review F7: a peak rebuilt from realized P&L alone forgets a $1,300
    week that fell back to $1,100 and shows zero drawdown)."""
    peak = current_equity
    row = conn.execute(
        "SELECT MAX(CAST(equity_usd AS REAL)) FROM equity_snapshots"
    ).fetchone()
    if row and row[0] is not None:
        peak = max(peak, Decimal(str(row[0])))
    running = Decimal("1000")
    for (pnl_cents,) in conn.execute(
            "SELECT realized_pnl_cents FROM closed_trades ORDER BY closed_at"):
        running += Decimal(pnl_cents) / 100
        peak = max(peak, running)
    return peak


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


def _protective_duties(conn, broker: Broker, report: "CycleReport",
                       now: datetime,
                       account_mode: str = "paper") -> tuple[bool, list[dict]]:
    """Reconcile fills, close finished positions, re-arm expired DAY
    stops, run hard-date exits. Runs on EVERY cycle including loss-rule
    kill trips - these duties only ever reduce risk. A reconcile failure
    degrades (state may be stale) but never skips stop re-arming
    (risk review F6: one flaky read must not leave the book unprotected
    for a session).

    Returns (all_positions_protected, open_position_dicts)."""
    try:
        reconcile(broker, conn)
    except BrokerError as exc:
        report.errors.append(f"reconcile: {exc}")
    _adopt_orphan_entries(conn, report, now)
    close_filled_positions(conn, account_mode=account_mode, now=now)
    _void_dead_entries(conn, report)

    open_rows = _open_position_dicts(conn, now)

    # WHAT DOES THE BROKER ACTUALLY HOLD? Read once, used by everything
    # below. An entry that never filled holds nothing, and a position
    # holding nothing needs no stop, cannot be sold, and must not block
    # the whole bot from trading (ESCALATION-7).
    broker_held = _broker_held(broker, report)
    held_syms = ({p.get("symbol") for p in broker_held if p.get("symbol")}
                 if broker_held is not None else None)
    pending = _pending_entry_ids(open_rows, held_syms)
    _void_stale_pending(conn, report, [p for p in open_rows
                                       if p["id"] in pending and p["due"]])
    # A voided phantom is no longer an open position, so it drops out of
    # every list below rather than being carried as a special case.
    open_rows = [p for p in open_rows
                 if not (p["id"] in pending and p["due"])]
    pending &= {p["id"] for p in open_rows}

    # Hard-date exits FIRST: they neutralize their own stops and must
    # not wait on the confirmation pass (re-review NEW-5: one flaky
    # get_open_orders delayed every exit a full cycle).
    due = [p for p in open_rows if p["due"]]
    # A due position whose entry order or quantity cannot be resolved
    # used to send a market sell with qty "None" and then die on a NOT
    # NULL constraint - a garbage order at the broker and an unfinished
    # cycle. It needs a human, not an order (stress-tester defect 17).
    unsellable = [p for p in due if not p["qty"] or not p["decision_id"]]
    for p in unsellable:
        report.errors.append(
            f"position {p['id']} ({p['ticker']}) is due but has no "
            f"resolvable entry order (qty={p['qty']!r}, "
            f"decision_id={p['decision_id']!r}) - not exited, needs review")
    due = [p for p in due if p not in unsellable]
    if due:
        try:
            manage_exits(due, now, broker, conn)
        except BrokerError as exc:
            report.errors.append(f"manage_exits: {exc}")

    if not open_rows:
        return (_broker_positions_agree(broker, conn, report, broker_held),
                open_rows)
    try:
        open_orders = broker.get_open_orders()
        confirmations = confirm_stops_resting(open_rows, broker, conn,
                                              open_orders=open_orders)
    except BrokerError as exc:
        report.errors.append(f"confirm_stops: {exc}")
        return False, open_rows

    status_by_id = {c.position_id: c.status for c in confirmations}
    live_ids_by_pos = {c.position_id: c.live_stop_order_ids
                       for c in confirmations}
    stop_qty_by_id = {o.get("id"): o.get("qty") for o in open_orders}

    for p in open_rows:
        if p["due"] or p["id"] in pending:
            # A position holding nothing gets no stop. There is no
            # quantity to protect, and a sell stop for shares that were
            # never bought is a short waiting to happen (ESCALATION-7).
            continue
        status = status_by_id.get(p["id"])
        live = live_ids_by_pos.get(p["id"], ())

        if status == "duplicate_stops":
            # Two live stops sell one position twice (re-review NEW-3).
            # Keep the recorded id when it is among the live set, else
            # the first live one; neutralize the rest.
            keep = p["stop_order_id"] if p["stop_order_id"] in live else live[0]
            extras_gone = True
            for extra in (s for s in live if s != keep):
                outcome = _neutralize_stop(broker, extra, poll_attempts=3,
                                           poll_interval_s=1.0)
                if outcome == "live":
                    extras_gone = False
                    report.errors.append(
                        f"duplicate stop {extra} on {p['id']} could not "
                        "be confirmed cancelled - entries stay blocked")
            if extras_gone:
                conn.execute(
                    "UPDATE positions SET stop_order_id = ? WHERE id = ?",
                    (keep, p["id"]))
                p["stop_order_id"] = keep
                status_by_id[p["id"]] = status = "ok"
                # the live set is now exactly the kept stop; without this
                # the backfill below reads the STALE tuple and re-points
                # the row at the id just cancelled (risk round 3 #2,
                # reproduced by test_stage5_gaps)
                live = (keep,)
                live_ids_by_pos[p["id"]] = live

        if status == "ok":
            # Backfill/repair the recorded id from the broker's truth
            # (re-review NEW-4: an orphan live stop with a NULL local id
            # means the exit path later market-sells into it).
            if live and p["stop_order_id"] != live[0]:
                conn.execute(
                    "UPDATE positions SET stop_order_id = ? WHERE id = ?",
                    (live[0], p["id"]))
                p["stop_order_id"] = live[0]
            # Re-arm when the resting stop under-covers what is held
            # (re-review B4 residual A: a partial that grew after arming
            # leaves the grown sleeve unprotected all session).
            held = Decimal(str(p["qty"])) if p["qty"] else None
            resting = stop_qty_by_id.get(p["stop_order_id"])
            if (held and resting is not None and p.get("stop_price")
                    and Decimal(str(resting)) < held):
                res = replace_stop(p, Decimal(str(p["stop_price"])),
                                   broker, conn)
                if res.status == "replaced" and res.new_stop_order_id:
                    conn.execute(
                        "UPDATE positions SET stop_order_id = ? WHERE id = ?",
                        (res.new_stop_order_id, p["id"]))
                else:
                    status_by_id[p["id"]] = "unprotected"

    conn.commit()
    unprotected = [
        p for p in open_rows
        if status_by_id.get(p["id"]) == "unprotected"
        and p.get("stop_price") is not None and p.get("qty")
        and not p["due"] and p["id"] not in pending]
    if unprotected:
        results = reopen_stops(unprotected, broker, conn)
        for pos, res in zip(unprotected, results):
            if res.status not in ("rejected", "submit_unconfirmed") \
                    and res.broker_order_id:
                conn.execute(
                    "UPDATE positions SET stop_order_id = ? WHERE id = ?",
                    (res.broker_order_id, pos["id"]))
                status_by_id[pos["id"]] = "ok"
        conn.commit()

    # A position holding nothing is not "unprotected" - there is nothing
    # to protect. Counting it as such is how ONE entry that silently
    # never filled blocks every future trade the bot would ever make
    # (ESCALATION-7), which is a far larger loss than the risk the block
    # was written to avoid.
    all_protected = all(
        status_by_id.get(p["id"]) == "ok" for p in open_rows
        if not p["due"] and p["id"] not in pending)
    return (all_protected
            and _broker_positions_agree(broker, conn, report,
                                        broker_held)), open_rows


def _broker_held(broker: Broker, report: "CycleReport") -> list | None:
    """What the broker says it actually holds, or None when it will not
    say. Read ONCE per cycle and passed to everything that needs it:
    two reads a cycle can disagree with each other, and a rule that
    fires on the difference between them is a rule that fires at random.
    """
    try:
        return broker.get_positions()
    except BrokerError as exc:
        report.errors.append(f"get_positions: {exc}")
        return None


def _pending_entry_ids(open_rows: list[dict], held_syms: set | None) -> set:
    """Open positions that hold NOTHING, confirmed from both sides.

    ESCALATION-7. An entry that is accepted but never fills leaves a
    positions row with no fill. The qty then falls back to the ORDERED
    quantity, and every session re-arms a protective sell stop for
    shares the account does not hold - which, if it ever triggered,
    sells stock that was never bought and leaves a short in a cash
    account. Worse in practice: the phantom can never be protected, and
    an unprotected position blocks EVERY new entry, so one order that
    silently failed to fill stops the bot trading indefinitely.

    THE TEST IS ZERO SHARES, AND BOTH SIDES MUST AGREE. No local fill
    row is not enough on its own - if `reconcile` failed, the shares can
    be real and simply unrecorded, and skipping the stop there would
    leave a genuine holding naked. So the broker must also report
    holding none of that ticker before the position is treated as empty.

    A BROKER THAT WILL NOT ANSWER CHANGES NOTHING. `held_syms is None`
    returns the empty set, so every position keeps exactly today's
    behaviour. The unsafe direction is assuming emptiness we cannot
    confirm, and that is the one case this refuses to guess in.
    """
    if held_syms is None:
        return set()
    return {p["id"] for p in open_rows
            if not p.get("fill_confirmed") and p["ticker"] not in held_syms}


def _broker_positions_agree(broker: Broker, conn,
                            report: "CycleReport",
                            held: list | None = None) -> bool:
    """Positions-level reconciliation (re-review NEW-1 / stress
    ESCALATION-8): a ticker held at the broker with no local open
    position is invisible to every kill switch, limit and exit. Detect
    the divergence and block entries until a human resolves it. Fails
    open-eyed: a broker error here reads as 'cannot confirm agreement'."""
    if held is None:
        held = _broker_held(broker, report)
    if held is None:
        return False
    local = {r[0] for r in conn.execute(
        "SELECT ticker FROM positions WHERE status = 'open'")}
    held_syms = {p.get("symbol") for p in held if p.get("symbol")}
    unaccounted = sorted(held_syms - local)
    for sym in unaccounted:
        report.errors.append(
            f"broker holds {sym} with no local open position - "
            "unaccounted exposure, entries blocked pending review")
    # The reverse direction (stress stage-8 E3): a local open position
    # with a RECORDED ENTRY FILL that the broker does not hold means the
    # book is counting exposure that does not exist and will re-arm
    # stops for shares it does not own. Only fill-confirmed positions
    # count - a freshly placed, not-yet-filled entry is legitimately
    # local-open/broker-flat and must not false-trip this.
    ghosts = [r[0] for r in conn.execute(
        """SELECT DISTINCT p.ticker FROM positions p
           JOIN orders o ON o.id = json_extract(
                CASE WHEN json_valid(p.entry_order_ids)
                     THEN p.entry_order_ids ELSE '[]' END, '$[0]')
           JOIN fills f ON f.order_id = o.id
           WHERE p.status = 'open'""") if r[0] not in held_syms]
    for sym in ghosts:
        report.errors.append(
            f"local open position in {sym} has a recorded entry fill but "
            "the broker holds none - phantom exposure, entries blocked "
            "pending review")
    return not unaccounted and not ghosts


def _adopt_orphan_entries(conn, report: "CycleReport", now: datetime) -> None:
    """A filled buy whose cycle died before the positions INSERT is a
    real holding with no stop, no exit date and no exposure accounting
    (re-review NEW-1b / stress ESCALATION-2). Adopt it: create the
    position row from the decision so every protective duty sees it."""
    rows = conn.execute(
        """SELECT o.id, o.decision_id, d.planned_exit_date
           FROM orders o
           JOIN fills f ON f.order_id = o.id
           JOIN risk_decisions d ON d.candidate_id = o.decision_id
                AND d.action = 'trade'
           WHERE o.side = 'buy'
             AND NOT EXISTS (
                 SELECT 1 FROM positions p
                 WHERE json_valid(p.entry_order_ids)
                   AND EXISTS (SELECT 1 FROM json_each(p.entry_order_ids)
                               WHERE json_each.value = o.id))""").fetchall()
    for order_id, decision_id, planned_exit in rows:
        ticker_row = conn.execute(
            "SELECT ticker FROM candidates WHERE id = ?",
            (decision_id,)).fetchone()
        if ticker_row is None or not planned_exit:
            report.errors.append(
                f"filled orphan entry {order_id} cannot be adopted "
                "(missing candidate/exit date) - needs review")
            continue
        conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), ticker_row[0], json.dumps([order_id]),
             None, now.isoformat(), planned_exit, "open"))
        report.errors.append(
            f"adopted orphan filled entry {order_id} ({ticker_row[0]}) - "
            "stop arms on this cycle's confirmation pass")
    conn.commit()


def _void_dead_entries(conn, report: "CycleReport") -> None:
    """An entry that went terminal with ZERO fill can never become a
    holding: void the position instead of re-arming a naked sell stop
    for shares never bought, forever (re-review NEW-2 / stress
    ESCALATION-7)."""
    rows = conn.execute(
        """SELECT p.id, p.ticker, o.status FROM positions p
           JOIN orders o ON o.id = json_extract(
                CASE WHEN json_valid(p.entry_order_ids)
                     THEN p.entry_order_ids ELSE '[]' END, '$[0]')
           WHERE p.status = 'open'
             AND o.status IN ('canceled', 'expired', 'rejected',
                              'done_for_day', 'suspended', 'stopped')
             AND NOT EXISTS (SELECT 1 FROM fills f
                             WHERE f.order_id = o.id)""").fetchall()
    for pos_id, ticker, order_status in rows:
        conn.execute("UPDATE positions SET status = 'void' WHERE id = ?",
                     (pos_id,))
        report.errors.append(
            f"position {pos_id} ({ticker}) voided: entry order terminal "
            f"({order_status}) with zero fill")
    conn.commit()


def _void_stale_pending(conn, report: "CycleReport",
                        stale: list[dict]) -> None:
    """Close the book on an entry that never filled and never will.

    ESCALATION-7, the long tail. A position whose entry is still live at
    the broker holds nothing, so it is left alone while there is any
    chance of a fill. Once it reaches its planned exit date that chance
    is gone: the thesis had a deadline and the deadline has passed.

    IT IS VOIDED, NEVER SOLD. The alternative path for a due position is
    a market sell, and selling shares that were never bought is the
    exact naked short this whole fix exists to prevent. `void` is also
    not `closed`: nothing was ever held, so there is no P&L, and it must
    not reach `closed_trades` and pollute the record the adaptive
    parameters learn from.
    """
    for p in stale:
        conn.execute("UPDATE positions SET status = 'void' WHERE id = ?",
                     (p["id"],))
        report.errors.append(
            f"position {p['id']} ({p['ticker']}) voided: its entry order "
            f"never filled and it has reached its planned exit date. "
            f"Nothing was bought, nothing was sold, and no stop was ever "
            f"armed for it. The broker confirms holding no {p['ticker']}.")
    if stale:
        conn.commit()


def _poll_entry_fill(broker: Broker, broker_order_id: str | None, *,
                     attempts: int, interval_s: float) -> Decimal:
    """Filled qty of a just-placed market order, polled briefly."""
    import time as _time
    if not broker_order_id:
        return Decimal("0")
    for attempt in range(attempts):
        try:
            state = broker.get_order(broker_order_id)
        except BrokerError:
            return Decimal("0")
        try:
            qty = _finite(state.get("filled_qty") or "0")
        except (ArithmeticError, TypeError, ValueError):
            # An unreadable filled_qty used to raise here - after the
            # entry was live at the broker and before the positions row
            # was written, leaving a real position with no local record,
            # no stop and no exit date. Treat it as "not yet filled":
            # the position is recorded unprotected and the next cycle's
            # confirm_stops_resting arms it (stress-tester defect 10).
            return Decimal("0")
        if qty > 0:
            return qty
        if attempt < attempts - 1:
            _time.sleep(interval_s)
    return Decimal("0")


def _fallback_cluster_key(c: Candidate) -> str:
    """cluster_fn returning nothing must fail CLOSED, not open (risk
    review N2): an unkeyed position would silently bypass the correlated-
    cluster bound. Same shape as correlation.cluster's keys."""
    year, week, _ = c.catalyst_date.isocalendar()
    return f"{c.sector or 'unknown'}|{c.catalyst_type}|{year}-W{week:02d}"


def run_cycle(conn, broker: Broker, transport, feed_fetch, build_candidates_fn,
              cluster_fn, *, now: datetime | None = None,
              kind: str = "scheduled",
              max_research: int = MAX_RESEARCH_PER_CYCLE,
              entry_poll_attempts: int = 5,
              entry_poll_interval_s: float = 1.0,
              account_mode: str = "paper",
              owner_monthly_cap_cents=None,
              bars_dir: str | None = None) -> CycleReport:
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
        # A trip BLOCKS NEW ENTRIES; it must never abandon what is
        # already open (ARCHITECTURE 3.2; risk review B2). When the trip
        # is a loss rule the portfolio read was good, so the protective
        # duties - reconcile, stop re-arm, hard exits - still run. Only
        # an unreliable/stale portfolio justifies full stand-down: acting
        # on state we cannot trust is how a bad day becomes a worse one.
        if ks.reason not in ("portfolio_state_unreliable",
                             "portfolio_state_stale"):
            _protective_duties(conn, broker, report, now, account_mode=account_mode)
        return report

    # daily equity mark from the confirmed broker read (dashboard's
    # performance-vs-SPY panel needs real marks; also feeds the
    # drawdown kill's high-water mark - risk review F7)
    positions_notional = str(sum(
        (p.notional_usd for p in portfolio.open_positions), Decimal("0")))
    prior = conn.execute(
        "SELECT equity_usd FROM equity_snapshots WHERE day=? AND source=?",
        (now.date().isoformat(), "broker_read")).fetchone()
    if prior is not None and Decimal(prior[0]) > portfolio.equity_usd:
        # same-day replace would forget the intraday high the drawdown
        # peak needs (re-review F7 note): preserve it under its own
        # source, keeping the MAX ever seen today - a REPLACE with a
        # later, lower prior would understate the peak (risk round 3 #5)
        existing_high = conn.execute(
            "SELECT equity_usd FROM equity_snapshots WHERE day=? AND source=?",
            (now.date().isoformat(), "intraday_high")).fetchone()
        high = (max(Decimal(existing_high[0]), Decimal(prior[0]))
                if existing_high else Decimal(prior[0]))
        conn.execute(
            """INSERT OR REPLACE INTO equity_snapshots
               (day, taken_at, equity_usd, settled_cash_usd,
                positions_notional, source)
               VALUES (?,?,?,?,?,?)""",
            (now.date().isoformat(), now.isoformat(), str(high),
             str(portfolio.settled_cash_usd), positions_notional,
             "intraday_high"))
    conn.execute(
        """INSERT OR REPLACE INTO equity_snapshots
           (day, taken_at, equity_usd, settled_cash_usd,
            positions_notional, source)
           VALUES (?,?,?,?,?,?)""",
        (now.date().isoformat(), now.isoformat(), str(portfolio.equity_usd),
         str(portfolio.settled_cash_usd), positions_notional,
         "broker_read"))
    conn.commit()

    # ---- 2. does anything we hold still deserve to be held?
    #
    # BEFORE the hard-exit sweep, so a review that brings an exit forward
    # to today is acted on by this pass rather than the next one. The
    # review cannot close anything itself - it only ever moves a date
    # EARLIER, and _protective_duties below does the closing. It is
    # deliberately skipped on a kill-switch trip: that path returns
    # above after running the protective duties, and a tripped account
    # should not be buying opinions.
    _review_open_positions(
        conn, broker, transport, report, now,
        CostContext(conn=conn,
                    governor_profit_share=Decimal(
                        str(params["governor_profit_share"])),
                    cycle_id=cycle_id, kind=kind,
                    owner_monthly_cap_cents=owner_monthly_cap_cents))

    # ---- 3 + 4. reconcile, then stop duties and hard exits
    stops_ok, open_rows = _protective_duties(conn, broker, report, now,
                                             account_mode=account_mode)

    # score refusals whose counterfactual window has elapsed (the
    # feedback loop; failures leave rows unscored for the next cycle)
    try:
        from catalyst.risk.refusal_tracker import score_due_refusals
        score_due_refusals(broker, conn, now)
    except BrokerError as exc:
        report.errors.append(f"refusal_scoring: {exc}")

    # New entries require: every open position protected (risk review
    # B3), and the market open - an off-hours "latest" quote is stale,
    # and a queued market order fills at an open price unrelated to the
    # mid that sized it (risk review F5).
    block_entries: str | None = None
    if not stops_ok:
        block_entries = "unprotected_position_blocks_entries"
    else:
        try:
            clock = broker.get_clock()
            # Only a real boolean True counts. The STRING "false" is
            # truthy, and a truthiness test on it traded with the market
            # shut (stress-tester defect 20).
            if clock.get("is_open") is not True:
                block_entries = "market_closed"
        except BrokerError:
            block_entries = "market_clock_unavailable"

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
             # default=str: a Decimal or datetime left in a payload by a
             # parser used to raise TypeError here, AFTER a successful
             # fetch (stress-tester defect 25)
             json.dumps(ev.payload_raw, default=str)))
    conn.commit()

    candidates = build_candidates_fn(events, now)
    report.funnel["candidates"] = len(candidates)

    fresh: list[Candidate] = []
    screen_reasons: list[str] = []
    open_tickers = {p["ticker"] for p in open_rows}
    for c in candidates:
        existing = conn.execute(
            "SELECT ticker, catalyst_date FROM candidates WHERE id=?",
            (c.id,)).fetchone()
        if conn.execute("SELECT 1 FROM research_views WHERE candidate_id=?",
                        (c.id,)).fetchone():
            screen_reasons.append(f"{c.id}: already_researched")
        elif _failed_attempts(conn, c.id) >= MAX_RESEARCH_ATTEMPTS:
            # A PAID CALL THAT FAILED LEFT NO TRACE THE SCREEN COULD SEE.
            # research_views is written only when a view PARSES
            # (boundary.py), so an invalid view, a truncated extraction
            # or a transport error made the candidate fresh again fifteen
            # minutes later - forever. Reproduced: 6 paid calls for one
            # stuck candidate over 6 cycles, ~51c a cycle, which spends
            # the whole $5 monthly cap in under an hour on a candidate
            # that never produces anything.
            #
            # The bound is on REPEATED failure, not on any failure: one
            # transient 529 must not permanently discard a good
            # candidate.
            screen_reasons.append(
                f"{c.id}: research_failed_{MAX_RESEARCH_ATTEMPTS}_times: "
                + str(_last_skip_reason(conn, c.id) or "no reason recorded"))
        elif c.ticker in open_tickers:
            screen_reasons.append(f"{c.id}: position_already_open")
        elif existing and (existing[0] != c.ticker
                           or existing[1] != c.catalyst_date.isoformat()):
            # Candidate ids are content hashes: a collision means two
            # different clusters share an id. INSERT OR IGNORE kept the
            # first row while the second candidate was traded, so the
            # audit trail described the wrong company (stress-tester
            # defect 19). Every trade must be explainable after the fact.
            screen_reasons.append(
                f"{c.id}: id_collision_with_different_candidate "
                f"(stored {existing[0]} {existing[1]}, "
                f"got {c.ticker} {c.catalyst_date.isoformat()})")
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
    entered_this_cycle: set = set()
    for c in fresh[:max_research]:
        if c.ticker in entered_this_cycle:
            # the open-ticker screen ran before the loop; a second
            # candidate on the SAME ticker in one cycle is double
            # exposure plus a guaranteed duplicate-stops deadlock
            # (stress ESCALATION-9)
            report.drop_reasons.setdefault("researched", []).append(
                f"{c.id}: ticker_already_entered_this_cycle")
            _note_not_attempted(conn, c.id,
                                "ticker_already_entered_this_cycle", now)
            continue
        if block_entries is not None:
            report.drop_reasons.setdefault("researched", []).append(
                f"{c.id}: {block_entries}")
            _note_not_attempted(conn, c.id, str(block_entries), now)
            continue
        if transport is None:
            report.drop_reasons.setdefault("researched", []).append(
                f"{c.id}: no_model_transport_configured")
            _note_not_attempted(conn, c.id,
                                "no_model_transport_configured", now)
            continue
        market = build_market_snapshot(broker, c.ticker, now)
        if market is None:
            report.drop_reasons.setdefault("researched", []).append(
                f"{c.id}: no_market_quote")
            _note_not_attempted(conn, c.id, "no_market_quote", now)
            continue
        log = investigate(
            c, CostContext(conn=conn,
                           governor_profit_share=Decimal(
                               str(params["governor_profit_share"])),
                           cycle_id=cycle_id, kind=kind,
                           owner_monthly_cap_cents=owner_monthly_cap_cents),
            transport, graph_context=_graph_context(c, conn),
            signals=_signals_for(c, events),
            # THE SNAPSHOT WAS ALREADY HERE, three lines up, and went
            # only to the risk engine. The model was being asked what
            # price and volume had done and shown neither.
            market=market)
        if log.parsed_view is None:
            report.drop_reasons.setdefault("researched", []).append(
                f"{c.id}: {log.skipped_reason}")
            continue
        researched += 1

        cluster_key = cluster_keys.get(c.id) or _fallback_cluster_key(c)
        # THE HISTORY HAS TO BE THERE BEFORE IT CAN BE READ. `data/` is
        # gitignored, so nothing on the owner's machine had ever cached a
        # candidate's bars and per-stock sizing fell back to the catalyst
        # category for every single name - correct, tested and inert.
        # One request per researched candidate fixes that; bars are
        # inside the Alpaca subscription and cost nothing.
        #
        # It cannot fail the cycle: ensure_history never raises, and a
        # ticker without history simply keeps its category value, which
        # is the conservative direction.
        if bars_dir:
            ensure_history(broker, bars_dir, c.ticker, now=now)
            # AND PUT IT IN FRONT OF THE MODEL. The bars were being
            # fetched for sizing and nothing else, while the research
            # brief asked "what has the price done since the filing"
            # with only today's close attached. The move since the
            # catalyst date is the evidence for whether the opportunity
            # is still open, and it is already on disk by this line.
            try:
                from catalyst.data.price_action import price_action

                market = replace(
                    market,
                    price_action=price_action(bars_dir, c.ticker,
                                              c.catalyst_date))
            except Exception:  # noqa: BLE001 - research must not die here
                pass

            # A SECOND OPINION ON THE ONE NUMBER EVERYTHING DESCENDS
            # FROM. Every traded figure comes from this single live
            # quote; if it is wrong - wrong symbol, misplaced decimal,
            # an unadjusted corporate action - nothing would notice,
            # because there is one source and it is believed. The cached
            # bars cannot confirm today's price but can refuse to
            # believe a hundredfold one.
            try:
                from catalyst.data.quote_check import cross_check

                check = cross_check(market.last_close, bars_dir, c.ticker)
                market = replace(market, quote_check=check)
                # Recorded whatever the verdict, including "not checked".
                # A flagged quote is passed through DELIBERATELY, and a
                # pass-through that lives only in a process which has
                # since exited cannot be told apart from never having
                # looked - which is the failure this whole check exists
                # to remove, not to reproduce one layer up.
                _record_quote_check(conn, c, market.last_close, check, now)
                if check.refused:
                    report.drop_reasons.setdefault(
                        "researched", []).append(
                        f"{c.id}: quote_failed_cross_check")
                    report.errors.append(
                        f"{c.ticker}: {check.sentence}")
                    _note_not_attempted(conn, c.id,
                                        "quote_failed_cross_check", now)
                    continue
                if check.flagged:
                    report.errors.append(f"{c.ticker}: {check.sentence}")
            except Exception:  # noqa: BLE001 - a check must not stop a cycle
                pass
        # bars_dir lets sizing read the CANDIDATE'S OWN gap and volatility
        # history instead of only its catalyst category's assumption. It
        # reads the local bar cache, so it costs no API call, and it
        # falls back to the category value for any ticker not cached -
        # which is the conservative direction, since the category value
        # is the ceiling.
        decision = evaluate(c, log.parsed_view, portfolio, params, market,
                            cluster_key=cluster_key, bars_dir=bars_dir)
        decision_id = _persist_decision(conn, decision, cluster_key)
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
        if entry.status in ("rejected", "submit_unconfirmed"):
            # submit_unconfirmed: the order row exists (it carries the
            # client_order_id) and reconcile will resolve it, but no
            # position is invented from an order we cannot confirm - a
            # phantom position would have a stop placed against shares
            # the account may not hold (stress-tester defect 8).
            report.drop_reasons.setdefault("orders_placed", []).append(
                f"{c.id}: entry_{entry.status}")
            if entry.status == "submit_unconfirmed":
                report.errors.append(
                    f"entry submit unconfirmed for {c.id}: reconcile must "
                    f"resolve order {entry.raw_response.get('submit_error')}")
                # we may be holding shares with no stop and no position
                # row: take no further entries until that is resolved
                block_entries = "unconfirmed_submit_blocks_entries"
            continue

        # The protective stop covers what actually FILLED, never the
        # ordered qty (risk review B3/B4): a pre-fill sell stop is
        # rejected by a cash account, and its silent rejection left the
        # position unprotected. Poll the market order briefly; if it has
        # not filled yet, the position row carries stop_order_id NULL and
        # the next cycle's confirm_stops_resting arms it (and blocks new
        # entries until then).
        filled_qty = _poll_entry_fill(broker, entry.broker_order_id,
                                      attempts=entry_poll_attempts,
                                      interval_s=entry_poll_interval_s)
        if filled_qty == 0 and entry.status in _TERMINAL_UNFILLED:
            # The broker answered 200 but the order is already dead
            # (Alpaca cancels unfilled orders at the close). Nothing was
            # bought, so a positions row here would be a phantom that
            # every later cycle tries to protect with a sell stop for
            # shares the account does not hold (stress-tester defect 26).
            report.drop_reasons.setdefault("orders_placed", []).append(
                f"{c.id}: entry_{entry.status}_unfilled")
            continue
        if filled_qty > decision.qty:
            # a broker-reported fill larger than the order is upstream
            # garbage; sizing a stop from it would sell shares we never
            # ordered (stress ESCALATION-1). Protect the ordered qty and
            # flag the discrepancy for review.
            report.errors.append(
                f"broker reports filled_qty {filled_qty} > ordered "
                f"{decision.qty} on {c.id} - clamped, needs review")
            filled_qty = decision.qty
        stop_broker_id = None
        if filled_qty > 0:
            stop = place_stop(decision_id=c.id, ticker=c.ticker,
                              qty=filled_qty,
                              stop_price=decision.stop_price,
                              broker=broker, conn=conn)
            if stop.status != "rejected" and stop.broker_order_id:
                stop_broker_id = stop.broker_order_id
        entry_order_row = conn.execute(
            "SELECT id FROM orders WHERE decision_id=? AND side='buy' "
            "ORDER BY submitted_at DESC LIMIT 1", (c.id,)).fetchone()
        if entry_order_row:
            # THE SPREAD THE PAPER ACCOUNT DID NOT PAY (TRAPS.md).
            # Recorded here because this is the only moment the measured
            # NBBO for this entry exists; reconcile writes the fill on a
            # later cycle, by which time the book has moved on. It is
            # stored BESIDE the broker's price and never instead of it -
            # reconciliation still compares against the real fill.
            conn.execute(
                "INSERT OR REPLACE INTO entry_market_context "
                "(order_id, half_spread_bp, last_close, recorded_at) "
                "VALUES (?,?,?,?)",
                (entry_order_row[0], str(market.half_spread_bp),
                 str(market.last_close), now.isoformat()))
        conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), c.ticker,
             json.dumps([entry_order_row[0]] if entry_order_row else []),
             stop_broker_id, now.isoformat(),
             decision.planned_exit_date.isoformat(), "open"))
        conn.commit()
        placed += 1
        entered_this_cycle.add(c.ticker)
        # keep in-cycle exposure honest for the NEXT candidate
        portfolio = _with_position(portfolio, c, decision, cluster_key, now)
        if stop_broker_id is None:
            # an unprotected position blocks further entries this cycle
            # exactly as it will next cycle
            block_entries = "unprotected_position_blocks_entries"
            report.drop_reasons.setdefault("orders_placed", []).append(
                f"{c.id}: entry_open_but_stop_not_armed")

    for c in fresh[max_research:]:
        report.drop_reasons.setdefault("researched", []).append(
            f"{c.id}: deferred_max_research_per_cycle")
        _note_not_attempted(conn, c.id, "deferred_max_research_per_cycle", now)
    report.funnel["researched"] = researched
    report.funnel["proposed"] = proposed
    report.funnel["orders_placed"] = placed
    return report


MAX_QUOTE_AGE = timedelta(minutes=10)


#: How many paid attempts one candidate gets before the pipeline stops
#: buying it. Two, because the failure modes worth retrying (a transient
#: overload, a one-off malformed tool call) clear on the second try, and
#: anything that fails twice is structural.
MAX_RESEARCH_ATTEMPTS = 2


#: Marks a research_calls row that records a candidate the cycle never
#: ATTEMPTED - no prompt, no API call, no spend. Distinct from
#: budget_denied (the governor refused a real attempt) and from a failed
#: call (the API was reached and something went wrong).
NOT_ATTEMPTED = "not_attempted: "


def _record_quote_check(conn, candidate, live_price, check,
                        now: datetime) -> None:
    """Persist what the cached history said about the live quote.

    INSERT OR REPLACE: a candidate re-checked every cycle keeps one
    current row rather than growing a log nobody reads.

    Never raises. A failure to record an observation must not stop a
    trade, and must not stop a refusal either - the refusal itself is
    decided above this call, not by it.
    """
    try:
        conn.execute(
            "INSERT OR REPLACE INTO quote_cross_checks "
            "(candidate_id, ticker, live_price, reference_close, "
            " reference_day, deviation, checked, flagged, refused, note, "
            " checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (candidate.id, candidate.ticker, str(live_price),
             str(check.reference_close) if check.reference_close is not None
             else None,
             str(check.reference_day) if check.reference_day is not None
             else None,
             str(check.deviation) if check.deviation is not None else None,
             int(bool(check.checked)), int(bool(check.flagged)),
             int(bool(check.refused)), check.sentence, now.isoformat()))
        conn.commit()
    except sqlite3.Error:
        pass          # observability must never break the cycle


def _note_not_attempted(conn, candidate_id: str, reason: str,
                        now: datetime) -> None:
    """Record WHY a candidate went unresearched this cycle.

    OWNER-REPORTED, looking at six candidates: "why does it appear to
    have never researched?" The decisions table said "not researched
    yet" for all of them, which sounds like a queue. It was covering six
    different situations, one of which - no model transport configured -
    means no API key and is not a queue at all.

    Every one of those skips existed only in the in-memory cycle report,
    which is gone the moment the process moves on. Nothing per-candidate
    reached the database, so neither the funnel nor the decisions table
    could name the gate. They both already read skipped_reason, so
    writing the reason there makes it visible in both without either
    knowing this function exists.

    The id is deterministic, so a candidate deferred for twenty cycles
    keeps ONE row with the latest timestamp rather than twenty. Cost and
    latency are zero because nothing was spent, and that is the point:
    these rows must never be mistaken for attempts.
    """
    try:
        conn.execute(
            "INSERT OR REPLACE INTO research_calls "
            "(id, candidate_id, model, prompt_rendered, tools_offered, "
            "cost_cents, latency_ms, skipped_reason, called_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"na:{candidate_id}:{reason[:40]}", candidate_id, "",
             "", "[]", "0", 0, NOT_ATTEMPTED + reason, now.isoformat()))
        conn.commit()
    except sqlite3.Error:
        pass          # observability must never break the cycle


def _failed_attempts(conn, candidate_id: str) -> int:
    """Paid research calls for this candidate that produced no view.

    Counts research_calls rows that actually reached the API - a row
    with a skipped_reason set by the governor never spent anything and
    must not count against the candidate. Neither must a not_attempted
    row: a candidate deferred twice for being over the per-cycle cap
    would otherwise be discarded forever, having never been tried once.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM research_calls WHERE candidate_id = ? "
            "AND (skipped_reason IS NULL OR (skipped_reason NOT LIKE "
            "'budget_denied%' AND skipped_reason NOT LIKE ?))",
            (candidate_id, NOT_ATTEMPTED + "%")).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _last_skip_reason(conn, candidate_id: str) -> str:
    """The last reason an ATTEMPT failed. A not_attempted row is not an
    attempt, so it must not be quoted as why the attempts failed."""
    try:
        row = conn.execute(
            "SELECT skipped_reason FROM research_calls WHERE candidate_id = ? "
            "AND skipped_reason IS NOT NULL AND skipped_reason NOT LIKE ? "
            "ORDER BY called_at DESC LIMIT 1",
            (candidate_id, NOT_ATTEMPTED + "%")).fetchone()
        return str(row[0]) if row and row[0] else ""
    except sqlite3.Error:
        return ""


def build_market_snapshot(broker: Broker, ticker: str,
                          now: datetime | None = None) -> MarketSnapshot | None:
    """Live NBBO at decision time - independent of anything Claude said.
    A stale quote (>10 min, e.g. off-hours 'latest') is refused: sizing
    and the spread gate off Friday's book is not a decision, it's a
    guess (risk review F5)."""
    now = now or datetime.now(timezone.utc)
    try:
        q = broker.get_latest_quote(ticker)
    except BrokerError:
        return None
    quote = q.get("quote") or {}
    if not isinstance(quote, dict):
        return None
    ts = quote.get("t")
    if not ts:
        return None    # an undatable quote cannot pass the freshness gate
    if ts:
        try:
            quote_at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if quote_at.tzinfo is None:
                # A feed that drops the trailing Z used to raise TypeError
                # here ("can't subtract offset-naive and offset-aware") -
                # only ValueError was caught (stress-tester defect 21).
                # Alpaca timestamps are UTC; if a feed ever sent local
                # time instead it would read as HOURS old and be refused,
                # which is the safe direction.
                quote_at = quote_at.replace(tzinfo=timezone.utc)
            if now - quote_at > MAX_QUOTE_AGE:
                return None
        except (TypeError, ValueError):
            return None
    bid, ask = quote.get("bp"), quote.get("ap")
    try:
        bid, ask = _finite(bid), _finite(ask)
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
        if getattr(lim, "note", ""):
            conn.execute(
                "INSERT OR REPLACE INTO limit_application_notes "
                "(decision_id, rule_name, note) VALUES (?,?,?)",
                (decision_id, lim.rule_name, lim.note))
    conn.commit()
    return decision_id


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, Decimal):
        return str(v)
    return v


def _review_open_positions(conn, broker: Broker, transport,
                           report: "CycleReport", now: datetime,
                           cost_context) -> None:
    """Ask Claude whether each open position's thesis still holds.

    Runs BEFORE the hard-exit sweep, so a review that brings an exit
    forward to today is acted on by the SAME pass rather than waiting
    fifteen minutes. The review itself closes nothing: it can only move
    a date earlier, and the existing time-exit machinery does the rest.

    Never raises. A review that fails is one position unreviewed; an
    exception here would abandon the sweep and leave the rest of the
    book unexamined.
    """
    from catalyst.research.position_review import (
        bring_exit_forward,
        due_for_review,
        review_position,
    )

    try:
        positions = _reviewable_positions(conn)
    except sqlite3.Error as exc:
        report.errors.append(f"position_review: cannot read positions: {exc}")
        return
    if not positions:
        return

    to_review, skipped = due_for_review(conn, positions, now)
    report.funnel["positions_reviewed"] = len(to_review)
    for position, why in skipped:
        report.drop_reasons.setdefault("positions_reviewed", []).append(
            f"{position.get('ticker')}: {why}")

    for position in to_review:
        # WHY THIS ONE, NOW. A review that fired early because news
        # broke is a different event from one the clock came round to,
        # and the funnel should not make them look identical.
        if position.get("review_trigger"):
            report.drop_reasons.setdefault("positions_reviewed", []).append(
                f"{position.get('ticker')}: read early - "
                f"{position['review_trigger']}")
        try:
            snapshot = build_market_snapshot(broker, position["ticker"], now)
            entry = position.get("entry_price")
            last = snapshot.last_close if snapshot is not None else None
            market = {"entry_price": entry, "last_price": last,
                      "move_pct": (
                          f"{((last - entry) / entry * 100):.1f}"
                          if entry and last and entry > 0 else "?")}
            view = {"thesis": position.get("thesis"),
                    "invalidation": position.get("invalidation")}
            review = review_position(conn, position, view, market,
                                     transport, cost_context, now=now)
            if review.skipped_reason:
                report.drop_reasons.setdefault(
                    "positions_reviewed", []).append(
                    f"{position.get('ticker')}: {review.skipped_reason}")
                continue
            moved, why = bring_exit_forward(conn, position, review, now)
            if moved:
                report.drop_reasons.setdefault(
                    "positions_reviewed", []).append(
                    f"{position.get('ticker')}: {why}")
        except Exception as exc:  # noqa: BLE001 - one position, not the book
            report.errors.append(
                f"position_review {position.get('ticker')}: "
                f"{type(exc).__name__}: {exc}")


def _reviewable_positions(conn) -> list[dict]:
    """Open positions with the thesis and entry price the review needs.

    A position whose thesis cannot be found is still returned, with the
    fields empty: render_prompt says "(none recorded)" and the model can
    answer no_opinion. Dropping it silently would make an unreviewable
    position indistinguishable from a reviewed one.
    """
    rows = conn.execute(
        """SELECT p.id, p.ticker, p.opened_at, p.planned_exit_date,
                  v.thesis, v.invalidation, f.price
           FROM positions p
           LEFT JOIN orders o ON o.id = json_extract(
                CASE WHEN json_valid(p.entry_order_ids)
                     THEN p.entry_order_ids ELSE '[]' END, '$[0]')
           LEFT JOIN research_views v ON v.candidate_id = o.decision_id
           LEFT JOIN fills f ON f.order_id = o.id
           WHERE p.status = 'open'""").fetchall()
    out = []
    for r in rows:
        try:
            opened = datetime.fromisoformat(r[2]).date()
            exit_date = datetime.fromisoformat(r[3]).date()
        except (TypeError, ValueError):
            continue
        try:
            entry = Decimal(str(r[6])) if r[6] is not None else None
        except ArithmeticError:
            entry = None
        out.append({"id": r[0], "ticker": r[1], "opened_at_date": opened,
                    "planned_exit_date": exit_date, "thesis": r[4],
                    "invalidation": r[5], "entry_price": entry})
    return out


def _open_position_dicts(conn, now: datetime) -> list[dict]:
    today = now.date().isoformat()
    rows = conn.execute(
        """SELECT p.id, p.ticker, p.stop_order_id, p.planned_exit_date,
                  o.decision_id, o.qty, d.stop_price, f.qty
           FROM positions p
           LEFT JOIN orders o ON o.id = json_extract(
                CASE WHEN json_valid(p.entry_order_ids)
                     THEN p.entry_order_ids ELSE '[]' END, '$[0]')
           LEFT JOIN risk_decisions d ON d.candidate_id = o.decision_id
           LEFT JOIN fills f ON f.order_id = o.id
           WHERE p.status = 'open'""").fetchall()
    out = []
    for r in rows:
        # qty: what is actually HELD - entry fills NET of sell fills
        # (risk review B4 residual B: after a partial stop fire, selling
        # the gross entry qty is an oversized order the cash account
        # rejects forever). No fill row yet -> fall back to ordered qty
        # only for stop/exit sizing of a possibly-filled entry.
        # The netting sums in DECIMAL, in Python: SQLite's REAL cast
        # turned 1 - 3x0.1 into 0.69999999999999996, a qty Alpaca
        # rejects (risk round 3 #3, reproduced by test_stage5_gaps).
        if r[7] is not None:
            sold = sum(
                (Decimal(str(q)) for (q,) in conn.execute(
                    """SELECT sf.qty FROM fills sf
                       JOIN orders so ON so.id = sf.order_id
                       WHERE so.decision_id = ? AND so.side = 'sell'""",
                    (r[4],))),
                Decimal("0"))
            held = (Decimal(str(r[7])) - sold).quantize(Decimal("0.0001"))
            # trailing zeros stripped so "4" stays "4", not "4.0000"
            qty = (format(held, "f").rstrip("0").rstrip(".")
                   if held > 0 else None)
        else:
            qty = r[5]
        out.append(
            {"id": r[0], "ticker": r[1], "stop_order_id": r[2],
             "decision_id": r[4], "qty": qty,
             "stop_price": r[6], "due": r[3] <= today,
             # Whether a fill was actually RECORDED, kept separate from
             # qty because qty falls back to the ordered quantity when
             # it was not. Without this the two cases - "we hold four
             # shares" and "we asked for four shares and heard nothing
             # back" - are the same value with the same type, and the
             # second one arms a stop for stock nobody owns
             # (ESCALATION-7).
             "fill_confirmed": r[7] is not None})
    return out


def _signals_for(candidate, raw_events) -> list | None:
    """What each independent feed said about THIS candidate's ticker.

    Returns None for a candidate no feed agreed about, which keeps the
    ordinary single-feed path byte-identical - the research prompt only
    changes shape where there is genuinely a link to weigh.
    """
    try:
        from catalyst.discovery.links import signals_from_events

        found = signals_from_events(raw_events or []).get(candidate.ticker) or []
        if len({s.source for s in found}) > 1:
            return found
    except Exception:  # noqa: BLE001 - research must not die on enrichment
        pass
    return None


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
