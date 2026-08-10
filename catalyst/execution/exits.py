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
from catalyst.execution.broker import Broker
from catalyst.execution.orders import _TERMINAL_CANCEL, _submit, place_stop


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
            broker.cancel_order(stop_id)
            confirmed = False
            state: dict = {}
            for attempt in range(poll_attempts):
                state = broker.get_order(stop_id)
                if state.get("status") in _TERMINAL_CANCEL:
                    confirmed = True
                    break
                if attempt < poll_attempts - 1:
                    time.sleep(poll_interval_s)
            if not confirmed or state.get("status") == "filled":
                # Stop either still live (unsafe to sell) or already did
                # the exit for us (nothing left to sell). Reconcile will
                # pick up the fill; skip.
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
