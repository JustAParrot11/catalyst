"""Order construction and stop management. HUMAN REVIEW REQUIRED.

replace_stop() is cancel-then-confirm-then-place: the new stop is never
placed unless the old one's cancellation is confirmed - two live stops
on one position is the failure this sequencing exists to prevent.
confirm_stops_resting() runs once per session at the open and queries
the broker directly (not positions.stop_order_id, which can be stale).
Both per ARCHITECTURE.md section 3.2.

Every order row is written with the broker's verbatim response, INCLUDING
rejections (house rule 3). The local order id doubles as Alpaca's
client_order_id, so a network-ambiguous submit can be resolved by
get_order_by_client_id instead of resubmitting blind.

Fractional stop orders only support time_in_force=DAY (TRAPS.md): they
expire at the close and MUST be re-placed each session - that is
confirm_stops_resting's "unprotected" finding at the next open, fed to
exits.reopen_stops().
"""

import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from catalyst.execution import OrderResult, StopConfirmation, StopReplacementResult
from catalyst.execution.broker import Broker, BrokerError, OrderRejected
from catalyst.risk import RiskDecision

_TERMINAL_CANCEL = {"canceled", "filled", "expired", "rejected", "done_for_day"}
_STOP_TYPES = {"stop", "stop_limit"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record_order(conn, *, order_id: str, decision_id: str, side: str,
                  qty: str, order_type: str, tif: str, submitted_at: datetime,
                  status: str, broker_order_id: str | None, raw: dict | str):
    conn.execute(
        """INSERT INTO orders (id, decision_id, broker_order_id, side, qty,
                               order_type, time_in_force, submitted_at,
                               status, raw_response)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (order_id, decision_id, broker_order_id, side, qty, order_type, tif,
         submitted_at.isoformat(), status, json.dumps(raw)))
    conn.commit()


def _submit(broker: Broker, conn, *, decision_id: str, symbol: str, qty: str,
            side: str, order_type: str, tif: str,
            stop_price: str | None = None) -> OrderResult:
    order_id = str(uuid.uuid4())
    submitted_at = _now()
    try:
        raw = broker.submit_order(
            symbol=symbol, qty=qty, side=side, order_type=order_type,
            time_in_force=tif, client_order_id=order_id,
            stop_price=stop_price)
        status = raw.get("status", "accepted")
        broker_order_id = raw.get("id")
    except OrderRejected as exc:
        raw = exc.body if isinstance(exc.body, dict) else {"body": exc.body}
        raw = {**raw, "_http_status": exc.status_code}
        status = "rejected"
        broker_order_id = raw.get("id")
    _record_order(conn, order_id=order_id, decision_id=decision_id,
                  side=side, qty=qty, order_type=order_type, tif=tif,
                  submitted_at=submitted_at, status=status,
                  broker_order_id=broker_order_id, raw=raw)
    return OrderResult(decision_id=decision_id, broker_order_id=broker_order_id,
                       status=status, submitted_at=submitted_at,
                       raw_response=raw)


def place(decision: RiskDecision, ticker: str, broker: Broker,
          conn) -> OrderResult:
    """Entry order for a trade decision: market DAY buy of decision.qty.

    Refuses anything that is not a sized long trade - the risk engine is
    the only source of qty, and this function will not invent one."""
    if decision.action != "trade" or decision.side != "long":
        raise ValueError("place() only accepts a long trade decision")
    if decision.qty is None or decision.qty <= 0:
        raise ValueError("trade decision arrived without a positive qty")
    return _submit(broker, conn, decision_id=decision.candidate_id,
                   symbol=ticker, qty=str(decision.qty), side="buy",
                   order_type="market", tif="day")


def place_stop(*, decision_id: str, ticker: str, qty: Decimal,
               stop_price: Decimal, broker: Broker, conn) -> OrderResult:
    """Protective sell stop. DAY only (fractional constraint, TRAPS.md)."""
    return _submit(broker, conn, decision_id=decision_id, symbol=ticker,
                   qty=str(qty), side="sell", order_type="stop", tif="day",
                   stop_price=str(stop_price))


def replace_stop(position: dict, new_stop_price: Decimal, broker: Broker,
                 conn, *, poll_attempts: int = 5,
                 poll_interval_s: float = 1.0) -> StopReplacementResult:
    """Cancel-confirm-place. position needs: id, ticker, qty, decision_id,
    stop_order_id (broker id of the resting stop, may be None)."""
    old_id = position.get("stop_order_id")
    replaced_at = _now()

    if old_id is not None:
        # Tolerate an already-terminal or broker-unknown old stop (DAY
        # stops expire nightly - TRAPS.md; risk review B1): the goal is
        # confirming it can no longer fire, not that the cancel verb
        # succeeded.
        try:
            broker.cancel_order(old_id)
        except BrokerError as exc:
            if exc.status_code is None or exc.status_code >= 500:
                result = StopReplacementResult(
                    position_id=position["id"], old_stop_order_id=old_id,
                    new_stop_order_id=None,
                    status="failed_cancel_unconfirmed",
                    raw_response={"cancel_error": str(exc),
                                  "status_code": exc.status_code})
                _record_replacement(conn, result, replaced_at)
                return result
            # 4xx: already terminal or unknown; the status read decides.
        confirmed = False
        last_state: dict = {}
        for attempt in range(poll_attempts):
            try:
                last_state = broker.get_order(old_id)
            except BrokerError as exc:
                if exc.status_code == 404:
                    last_state = {"status": "canceled",
                                  "_note": "unknown_to_broker_404"}
                    confirmed = True
                    break
                last_state = {"cancel_error": str(exc)}
                break
            if last_state.get("status") in _TERMINAL_CANCEL:
                confirmed = True
                break
            if attempt < poll_attempts - 1:
                time.sleep(poll_interval_s)
        if not confirmed:
            result = StopReplacementResult(
                position_id=position["id"], old_stop_order_id=old_id,
                new_stop_order_id=None, status="failed_cancel_unconfirmed",
                raw_response=last_state)
            _record_replacement(conn, result, replaced_at)
            return result
        if last_state.get("status") == "filled":
            # The old stop fired while we were cancelling: the position is
            # already (being) closed. Placing a new sell stop now would
            # open a short. Report unconfirmed so the caller re-reconciles.
            result = StopReplacementResult(
                position_id=position["id"], old_stop_order_id=old_id,
                new_stop_order_id=None, status="failed_cancel_unconfirmed",
                raw_response=last_state)
            _record_replacement(conn, result, replaced_at)
            return result

    placed = place_stop(decision_id=position["decision_id"],
                        ticker=position["ticker"],
                        qty=Decimal(str(position["qty"])),
                        stop_price=new_stop_price, broker=broker, conn=conn)
    ok = placed.status not in ("rejected",) and placed.broker_order_id
    result = StopReplacementResult(
        position_id=position["id"], old_stop_order_id=old_id,
        new_stop_order_id=placed.broker_order_id if ok else None,
        status="replaced" if ok else "failed_cancel_unconfirmed",
        raw_response=placed.raw_response)
    _record_replacement(conn, result, replaced_at)
    return result


def _record_replacement(conn, result: StopReplacementResult,
                        replaced_at: datetime):
    conn.execute(
        """INSERT INTO stop_replacements
           (position_id, old_stop_order_id, new_stop_order_id, status,
            raw_response, replaced_at)
           VALUES (?,?,?,?,?,?)""",
        (result.position_id, result.old_stop_order_id,
         result.new_stop_order_id, result.status,
         json.dumps(result.raw_response), replaced_at.isoformat()))
    conn.commit()


def confirm_stops_resting(positions: list[dict], broker: Broker,
                          conn) -> list[StopConfirmation]:
    """Once per session at the open: query the broker's open orders FRESH
    and classify each position ok / unprotected / duplicate_stops.
    positions need: id, ticker."""
    open_orders = broker.get_open_orders()
    checked_at = _now().isoformat()
    out: list[StopConfirmation] = []
    for pos in positions:
        stops = tuple(
            o["id"] for o in open_orders
            if o.get("symbol") == pos["ticker"]
            and o.get("side") == "sell"
            and o.get("type") in _STOP_TYPES)
        status = ("unprotected" if len(stops) == 0
                  else "ok" if len(stops) == 1
                  else "duplicate_stops")
        conf = StopConfirmation(position_id=pos["id"],
                                live_stop_order_ids=stops, status=status)
        conn.execute(
            """INSERT INTO stop_confirmations
               (position_id, checked_at, live_stop_order_ids, status)
               VALUES (?,?,?,?)""",
            (conf.position_id, checked_at, json.dumps(list(stops)),
             conf.status))
        out.append(conf)
    conn.commit()
    return out
