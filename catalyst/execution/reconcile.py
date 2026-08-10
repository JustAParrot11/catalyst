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


def _number(value) -> Decimal | None:
    """A broker number, or None if it is unreadable or not finite.
    Decimal('NaN') and Decimal('Infinity') both construct successfully
    and then raise on the first comparison, so is_finite is part of
    parsing, not a separate check (stress-tester defect 5)."""
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return dec if dec.is_finite() else None


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

        filled_qty = _number(remote.get("filled_qty") or "0")
        avg_price = _number(remote.get("filled_avg_price"))
        if filled_qty is None or (remote.get("filled_avg_price") is not None
                                  and avg_price is None):
            # A quantity or price we cannot read is not a fill and is not
            # a zero either: record the broker's verbatim answer beside
            # the order and leave the order non-terminal so the next pass
            # asks again (house rule 3; stress-tester defects 5 and 6).
            conn.execute(
                "UPDATE orders SET raw_response = ? WHERE id = ?",
                (json.dumps({"unreadable_fill_fields": True,
                             "remote": remote,
                             "checked_at": _now().isoformat()}), order_id))
            continue
        if avg_price is not None and avg_price <= 0:
            # A non-positive fill price is impossible. Recorded as a fill
            # it would flow into realized P&L, which feeds the drawdown
            # kill switch's high-water mark AND the cost governor's cap.
            conn.execute(
                "UPDATE orders SET raw_response = ? WHERE id = ?",
                (json.dumps({"nonpositive_fill_price": str(
                    remote.get("filled_avg_price")),
                    "remote": remote,
                    "checked_at": _now().isoformat()}), order_id))
            continue
        if filled_qty > 0 and avg_price is not None:
            filled_at = _timestamp(remote.get("filled_at"))
            fill = Fill(order_id=order_id,
                        price=avg_price,
                        qty=filled_qty, filled_at=filled_at,
                        broker_reported_price=avg_price)
            prev = conn.execute(
                "SELECT qty FROM fills WHERE order_id = ?",
                (order_id,)).fetchone()
            if prev is None:
                conn.execute(
                    """INSERT INTO fills (order_id, price, qty, filled_at,
                                          broker_reported_price,
                                          modeled_slippage)
                       VALUES (?,?,?,?,?,NULL)""",
                    (order_id, str(fill.price), str(fill.qty),
                     fill.filled_at.isoformat(),
                     str(fill.broker_reported_price)))
                fills.append(fill)
            elif Decimal(prev[0]) < filled_qty:
                # a partial fill grew: the broker's cumulative avg price
                # and qty replace the earlier observation (risk review B4
                # - a fill frozen at first sight understates the position)
                conn.execute(
                    """UPDATE fills SET price = ?, qty = ?,
                       broker_reported_price = ?, filled_at = ?
                       WHERE order_id = ?""",
                    (str(fill.price), str(fill.qty),
                     str(fill.broker_reported_price),
                     fill.filled_at.isoformat(), order_id))
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
        try:
            entry_ids = json.loads(entry_ids_json)
        except (TypeError, ValueError):
            continue      # corrupt row: it needs a human, not a guess
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
        if sold_qty <= 0 or sold_qty < Decimal(entry_qty):
            continue           # partial or no exit yet; stays open
                               # (sold_qty 0 also guards the division
                               # below - stress-tester defect 27)

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


def _timestamp(value) -> datetime:
    """A broker timestamp, falling back to observation time. A malformed
    or absent filled_at must not lose the fill itself - a fill we cannot
    date is still a position we hold (stress-tester)."""
    if not isinstance(value, str) or not value:
        return _now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _now()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
