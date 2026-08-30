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


def _blocking_order_ids(raw) -> list[str]:
    """The order ids the BROKER itself says are holding the shares.

    Alpaca refuses a sell it cannot cover with the reason attached:

        {"code": 40310000, "available": "0", "held_for_orders": "79.1295",
         "message": "insufficient qty available for order (requested:
                     79.1295, available: 0)",
         "related_orders": ["68fc7415-042c-490d-88cf-80ca2b1cc743"]}

    `related_orders` is the answer to the only question that matters,
    from the one party that cannot be wrong about it. Reading it is what
    makes this independent of whether our own view of the account is
    complete - and on 2026-08-30 it was not: `GET /v2/orders?status=open`
    did not list 68fc7415 at all, so every pre-emptive cancel looked
    successful and the sell was refused anyway, 93 times in a day.

    Classified by the rule, not by the error code (house rule 7): if a
    rejection names orders as related to it, those orders are why it
    failed, whatever code sits beside them.
    """
    if not isinstance(raw, dict):
        return []
    # _submit nests the original body under "rejection" when it resolved
    # the submission afterwards; look in both shapes.
    bodies = [raw]
    nested = raw.get("rejection")
    if isinstance(nested, dict):
        bodies.append(nested)
    out: list[str] = []
    for body in bodies:
        related = body.get("related_orders")
        if isinstance(related, (list, tuple)):
            out.extend(str(x) for x in related if x)
    return list(dict.fromkeys(out))


def _is_a_sell_on(broker: Broker, order_id: str, symbol: str) -> bool:
    """Only ever cancel an order that is a sell of the symbol being
    exited. A blocker id comes from the broker's own response, but
    cancelling on a name alone would make a malformed body able to
    cancel anything in the account; this bounds it to orders that
    could actually be holding these shares."""
    try:
        o = broker.get_order(order_id)
    except BrokerError:
        return False
    return (str(o.get("symbol") or "").upper() == symbol.upper()
            and str(o.get("side") or "").lower() == "sell")


def _our_working_exit(conn, order: dict, decision_id: str) -> bool:
    """Is this resting order the market sell WE submitted for this exit?

    Every order this module places carries its local `orders.id` as the
    broker's client_order_id, so the account can be read back against
    our own record without guessing.

    It matters because a resting sell is otherwise cancelled before the
    exit goes out: an exit order accepted outside market hours rests
    until the open, and cancel-and-resubmit every fifteen minutes would
    keep replacing it with a fresh one that never reaches an opening
    auction. Requiring order_type='market' is what separates the exit
    from the protective stop - a resting STOP is exactly what must be
    cancelled, and it is a sell on the same decision_id too.
    """
    client_id = order.get("client_order_id")
    if not client_id:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM orders WHERE id=? AND decision_id=? "
            "AND side='sell' AND order_type='market'",
            (str(client_id), str(decision_id))).fetchone()
    except Exception:  # noqa: BLE001 - an unreadable row is not a licence
        return False   # to skip the exit; fall through and sell
    return row is not None


def manage_exits(due_positions: list[dict], as_of: datetime, broker: Broker,
                 conn, *, poll_attempts: int = 5,
                 poll_interval_s: float = 1.0) -> list[OrderResult]:
    """Close every position whose hard exit date has arrived.

    due_positions: dicts with id, ticker, qty, decision_id,
    stop_order_id (may be None). The caller (orchestrator) selects which
    positions are due; this function only executes, it never decides.
    Positions whose stop cannot be confirmed cancelled are SKIPPED this
    pass and reported by the returned results being absent - never sold
    into a possibly-live stop. The caller is expected to notice that
    absence and say so: it went unreported for 25 hours on 2026-08-29.

    EVERY stop resting against the symbol is neutralized, not only the
    one the position row remembers - see the note in the body."""
    # THE BROKER'S OPEN ORDERS ARE THE TRUTH; THE RECORDED ID IS A CACHE.
    #
    # OWNER'S LOG, 2026-08-29/30: 99 exit attempts for EMBC, every sell
    # answered HTTP 403 "insufficient qty available (requested 79.1295,
    # available 0)" - the shares were reserved by a resting stop this
    # function never cancelled.
    #
    # It cancelled the one id on the position row. Fractional stops are
    # DAY-only and expire nightly (TRAPS.md), so reopen_stops re-places
    # them each session under a NEW broker id - and the loop in cycle.py
    # that writes that id back begins `if p["due"] ... continue`, so a
    # DUE position is skipped by it. On the single day the id matters
    # most, it is the stale one. _neutralize_stop then 404s and
    # correctly answers 'gone' (risk review B1: a purged id must not
    # brick the exit forever), the sell goes out, and the live stop is
    # still holding every share.
    #
    # Asking the broker costs one request a pass and removes the class
    # rather than this instance of it: any stop resting against this
    # symbol blocks the sell, whatever placed it and whatever the row
    # remembers.
    try:
        open_orders = broker.get_open_orders()
    except BrokerError:
        # Fail CLOSED, the same way an unconfirmable cancel does.
        # Without the list there is no way to know what is resting, and
        # selling into a possibly-live stop is the double-sell this
        # module exists to prevent.
        return []

    results: list[OrderResult] = []
    for pos in due_positions:
        symbol = str(pos.get("ticker") or "").upper()
        # ANY RESTING SELL BLOCKS THE EXIT, NOT ONLY A STOP.
        #
        # This filter used to be `"stop" in type`, and the owner's
        # 2026-08-30 bundle is what that cost: 93 more rejected sells in
        # a single day, all "insufficient qty available (requested
        # 79.1295, available 0)", while both this function AND
        # confirm_stops_resting reported the position UNPROTECTED - no
        # stop resting, nothing to cancel - and the shares were held all
        # the same. Alpaca reserves the quantity for whatever sell is
        # working, whatever its type; a stale limit or an accepted
        # market sell holds them exactly as a stop does.
        #
        # So the class is "a resting sell on this symbol", which is the
        # property that actually blocks (house rule 7), and the one
        # exception is carved out by name below.
        resting = [o for o in open_orders
                   if str(o.get("symbol") or "").upper() == symbol
                   and str(o.get("side") or "").lower() == "sell"
                   and o.get("id")]
        working = [o for o in resting
                   if _our_working_exit(conn, o, pos["decision_id"])]
        if working:
            # Our own exit is already at the broker, waiting for a
            # session. Cancelling and re-submitting it every cycle would
            # mean it never reaches an opening auction.
            results.append(OrderResult(
                decision_id=pos["decision_id"],
                broker_order_id=str(working[0].get("id")),
                status="exit_already_working", submitted_at=as_of,
                raw_response={"reason": "a market sell for this position is "
                                        "already resting at the broker",
                              "order": working[0]}))
            continue

        stop_ids = [str(o.get("id")) for o in resting]
        # The recorded id too: it may be live and simply absent from a
        # truncated or stale listing, and cancelling a dead id is free.
        recorded = pos.get("stop_order_id")
        if recorded is not None and str(recorded) not in stop_ids:
            stop_ids.append(str(recorded))

        blocked = False
        for stop_id in stop_ids:
            outcome = _neutralize_stop(broker, stop_id,
                                       poll_attempts=poll_attempts,
                                       poll_interval_s=poll_interval_s)
            if outcome != "gone":
                # 'live': unsafe to sell. 'filled': the stop already did
                # the exit; reconcile will close the position. Skip.
                blocked = True
                break
        if blocked:
            continue
        result = _submit(
            broker, conn, decision_id=pos["decision_id"],
            symbol=pos["ticker"], qty=str(pos["qty"]), side="sell",
            order_type="market", tif="day")

        # THE BROKER NAMES WHAT IS HOLDING THE SHARES; ASK IT ONCE.
        #
        # Everything above is a guess about what is resting, built from
        # a listing that on 2026-08-30 did not contain the order that
        # was actually holding EMBC. The rejection did contain it. One
        # retry, only against orders the broker itself named, only after
        # confirming each is a sell of this symbol, and only when every
        # one of them is confirmed gone - so a partial cancel can never
        # turn into a sell racing a live order.
        if result.status == "rejected":
            blockers = [b for b in _blocking_order_ids(result.raw_response)
                        if b not in stop_ids]
            if blockers and all(
                    _is_a_sell_on(broker, b, symbol)
                    and _neutralize_stop(broker, b,
                                         poll_attempts=poll_attempts,
                                         poll_interval_s=poll_interval_s)
                    == "gone"
                    for b in blockers):
                result = _submit(
                    broker, conn, decision_id=pos["decision_id"],
                    symbol=pos["ticker"], qty=str(pos["qty"]), side="sell",
                    order_type="market", tif="day")
        results.append(result)
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
