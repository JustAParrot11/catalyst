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


def _now() -> datetime:
    return datetime.now(timezone.utc)
