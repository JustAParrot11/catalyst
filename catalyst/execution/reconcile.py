"""Fill reconciliation against broker state. HUMAN REVIEW REQUIRED.

broker_reported_price is recorded beside any modeled price, never
instead of it (TRAPS.md: paper fills pay no spread).

Lookup is by client_order_id (our local order id), so an order whose
submit response was lost to a network failure is still reconcilable -
the broker either knows the id or it does not, and both answers are
recorded.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal

from catalyst.execution import Fill
from catalyst.execution.broker import Broker, BrokerError

_TERMINAL = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def reconcile(broker: Broker, conn) -> list[Fill]:
    """Walk every locally-recorded order not yet terminal, ask the broker
    what actually happened, update local status, and record any fill at
    the broker's reported price. Returns the fills discovered this pass."""
    rows = conn.execute(
        """SELECT id, broker_order_id, status FROM orders
           WHERE status NOT IN ({})""".format(
            ",".join("?" * len(_TERMINAL))),
        tuple(_TERMINAL)).fetchall()

    fills: list[Fill] = []
    for order_id, broker_order_id, local_status in rows:
        try:
            remote = broker.get_order_by_client_id(order_id)
        except BrokerError as exc:
            if exc.status_code == 404:
                # The broker has never heard of this order: the submit
                # never landed. Terminal, and the raw answer is recorded
                # beside it (house rule 3).
                conn.execute(
                    "UPDATE orders SET status = 'rejected', raw_response = ? "
                    "WHERE id = ?",
                    (json.dumps({"reconcile_404": True,
                                 "body": exc.body,
                                 "checked_at": _now().isoformat()}),
                     order_id))
                continue
            raise

        remote_status = remote.get("status", "unknown")
        conn.execute(
            "UPDATE orders SET status = ?, broker_order_id = ? WHERE id = ?",
            (remote_status, remote.get("id", broker_order_id), order_id))

        filled_qty = Decimal(str(remote.get("filled_qty") or "0"))
        avg_price = remote.get("filled_avg_price")
        if filled_qty > 0 and avg_price is not None:
            already = conn.execute(
                "SELECT 1 FROM fills WHERE order_id = ?",
                (order_id,)).fetchone()
            if not already:
                filled_at = datetime.fromisoformat(
                    remote.get("filled_at").replace("Z", "+00:00")
                ) if remote.get("filled_at") else _now()
                fill = Fill(order_id=order_id,
                            price=Decimal(str(avg_price)),
                            qty=filled_qty, filled_at=filled_at,
                            broker_reported_price=Decimal(str(avg_price)))
                conn.execute(
                    """INSERT INTO fills (order_id, price, qty, filled_at,
                                          broker_reported_price,
                                          modeled_slippage)
                       VALUES (?,?,?,?,?,NULL)""",
                    (order_id, str(fill.price), str(fill.qty),
                     fill.filled_at.isoformat(),
                     str(fill.broker_reported_price)))
                fills.append(fill)
    conn.commit()
    return fills


def close_filled_positions(conn, account_mode: str = "paper",
                           now: datetime | None = None) -> int:
    """Transition open positions whose exit sell has fully filled into
    closed_trades. Entry/exit prices are the broker's reported fill
    prices. exit_reason comes from the sell order's type: 'stop' for a
    stop fill, 'hard_exit' for the time-based market sell. Returns how
    many positions were closed."""
    now = now or _now()
    closed = 0
    for pos_id, ticker, entry_ids_json, opened_at, planned_exit in \
            conn.execute("SELECT id, ticker, entry_order_ids, opened_at, "
                         "planned_exit_date FROM positions "
                         "WHERE status = 'open'").fetchall():
        entry_ids = json.loads(entry_ids_json)
        if not entry_ids:
            continue
        entry = conn.execute(
            """SELECT f.price, f.qty, o.decision_id FROM fills f
               JOIN orders o ON o.id = f.order_id
               WHERE f.order_id = ?""", (entry_ids[0],)).fetchone()
        if entry is None:
            continue           # entry not filled yet; nothing to close
        entry_price, entry_qty, decision_id = entry

        sells = conn.execute(
            """SELECT f.price, f.qty, o.order_type, f.filled_at
               FROM fills f JOIN orders o ON o.id = f.order_id
               WHERE o.decision_id = ? AND o.side = 'sell'
               ORDER BY f.filled_at""", (decision_id,)).fetchall()
        sold_qty = sum(Decimal(s[1]) for s in sells)
        if sold_qty < Decimal(entry_qty):
            continue           # partial or no exit yet; stays open

        # qty-weighted exit price across sell fills
        exit_price = sum(Decimal(s[0]) * Decimal(s[1]) for s in sells) / sold_qty
        exit_reason = ("stop" if any(s[2] in ("stop", "stop_limit")
                                     for s in sells) else "hard_exit")
        pnl_cents = int(((exit_price - Decimal(entry_price))
                         * Decimal(entry_qty) * 100).quantize(Decimal("1")))
        opened_date = datetime.fromisoformat(opened_at).date()
        last_fill = datetime.fromisoformat(
            sells[-1][3].replace("Z", "+00:00"))
        decision_row = conn.execute(
            "SELECT planned_exit_date, decided_at FROM risk_decisions "
            "WHERE candidate_id = ? AND action='trade'", (decision_id,)).fetchone()
        expected_days = 0
        if decision_row and decision_row[0]:
            expected_days = (datetime.fromisoformat(decision_row[0]).date()
                             - datetime.fromisoformat(decision_row[1]).date()).days
        conn.execute(
            """INSERT OR REPLACE INTO closed_trades
               (position_id, account_mode, entry_price, exit_price,
                exit_reason, realized_pnl_cents, expected_holding_days,
                actual_holding_days, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (pos_id, account_mode, str(entry_price),
             str(exit_price.quantize(Decimal("0.0001"))), exit_reason,
             pnl_cents, expected_days,
             (last_fill.date() - opened_date).days, last_fill.isoformat()))
        conn.execute("UPDATE positions SET status = 'closed' WHERE id = ?",
                     (pos_id,))
        closed += 1
    conn.commit()
    return closed


def _now() -> datetime:
    return datetime.now(timezone.utc)
