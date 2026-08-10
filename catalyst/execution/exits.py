"""Time-based and stop-triggered exits. HUMAN REVIEW REQUIRED.

Every position carries a hard exit date set when it is opened; if the
thesis has not played out by then, the position closes regardless
(BUILD-BRIEF.md trading behaviour requirements).

Sequencing on a time exit: the resting stop is cancelled AND confirmed
terminal before the market sell goes out. A market sell racing a live
stop is two sells of one position - the same double-sell hazard
replace_stop() guards, so the same confirm discipline applies.
"""

import time
from datetime import datetime
from decimal import Decimal

from catalyst.execution import OrderResult
from catalyst.execution.broker import Broker, BrokerError
from catalyst.execution.orders import _TERMINAL_CANCEL, _submit, place_stop


def _neutralize_stop(broker: Broker, stop_id: str, *, poll_attempts: int,
                     poll_interval_s: float) -> str:
    """Cancel a stop and CONFIRM it can no longer fire. Returns one of:
    'gone'   - terminal (canceled/expired/rejected) or unknown to the
               broker (404: a stale local id; DAY stops expire nightly
               per TRAPS.md, and an expired-then-purged id must not
               brick the exit path forever - risk review B1);
    'filled' - the stop already did the exit; nothing left to sell;
    'live'   - could not confirm; caller must NOT sell."""
    try:
        broker.cancel_order(stop_id)
    except BrokerError as exc:
        # 422 "order not cancelable" = already terminal; 404 = unknown.
        # Either way the truth comes from the status read below.
        if exc.status_code == 404:
            return "gone"
        if exc.status_code is None or exc.status_code >= 500:
            return "live"      # broker unreachable: fail closed
    state: dict = {}
    for attempt in range(poll_attempts):
        try:
            state = broker.get_order(stop_id)
        except BrokerError as exc:
            if exc.status_code == 404:
                return "gone"
            return "live"
        if state.get("status") in _TERMINAL_CANCEL:
            break
        if attempt < poll_attempts - 1:
            time.sleep(poll_interval_s)
    status = state.get("status")
    if status == "filled":
        return "filled"
    if status in _TERMINAL_CANCEL:
        return "gone"
    return "live"


def manage_exits(due_positions: list[dict], as_of: datetime, broker: Broker,
                 conn, *, poll_attempts: int = 5,
                 poll_interval_s: float = 1.0) -> list[OrderResult]:
    """Close every position whose hard exit date has arrived.

    due_positions: dicts with id, ticker, qty, decision_id,
    stop_order_id (may be None). The caller (orchestrator) selects which
    positions are due; this function only executes, it never decides.
    Positions whose stop cannot be confirmed cancelled are SKIPPED this
    pass and reported by the returned results being absent - never sold
    into a possibly-live stop."""
    results: list[OrderResult] = []
    for pos in due_positions:
        stop_id = pos.get("stop_order_id")
        if stop_id is not None:
            outcome = _neutralize_stop(broker, stop_id,
                                       poll_attempts=poll_attempts,
                                       poll_interval_s=poll_interval_s)
            if outcome != "gone":
                # 'live': unsafe to sell. 'filled': the stop already did
                # the exit; reconcile will close the position. Skip.
                continue
        results.append(_submit(
            broker, conn, decision_id=pos["decision_id"],
            symbol=pos["ticker"], qty=str(pos["qty"]), side="sell",
            order_type="market", tif="day"))
    return results


def reopen_stops(unprotected_positions: list[dict], broker: Broker,
                 conn) -> list[OrderResult]:
    """Re-place DAY stops at the session open (fractional stops expire at
    every close - TRAPS.md). Runs on confirm_stops_resting()'s
    'unprotected' findings. Dicts need ticker, qty, decision_id,
    stop_price."""
    return [
        place_stop(decision_id=pos["decision_id"], ticker=pos["ticker"],
                   qty=Decimal(str(pos["qty"])),
                   stop_price=Decimal(str(pos["stop_price"])),
                   broker=broker, conn=conn)
        for pos in unprotected_positions
    ]
