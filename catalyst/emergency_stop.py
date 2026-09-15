"""The owner's emergency stop. MONEY-CRITICAL on any change.

OWNER-ASKED 2026-09-15, after a day that spent $10.02: *"Add an
emergency pause button that suspends everything and just lets current
trades that are active just sit until they hit the hard exit data incase
we suddenly run out of money. The bot just suspends claude API activity
and wont trade except sell what it currently has at the date."*

WHAT IT STOPS, and the list is exactly the owner's:

  - EVERY paid Claude call. `cost.governor.authorize` refuses first,
    ahead of its own integrity gate, for scheduled and manual spend
    alike. That is the single chokepoint every billable call in this
    system passes through, so there is one place to enforce it rather
    than one per caller.
  - EVERY new entry. `orchestrator.cycle` sets `block_entries`, the
    single gate every candidate crosses on the way to being sized - so
    a view formed BEFORE the stop was engaged cannot still become a
    position after it.

WHAT IT DELIBERATELY DOES NOT STOP, because stopping it would be the
dangerous direction:

  - The hard exit date. A position still sells when its date arrives.
    Suspending that would convert "hold days to weeks" into an open-
    ended hold, which is the one thing every version of this brief has
    forbidden.
  - The stops resting at the broker. Fractional stops are DAY-only and
    expire nightly (TRAPS.md), so `reopen_stops` must keep re-placing
    them or an open position's downside becomes unbounded. A pause that
    removed the stops would INCREASE risk while reading as caution.
  - Reconciliation, feeds, the dashboard. None of them spends Claude
    money, and going blind is not the same as going quiet.

Both of those run earlier in the cycle than the entry gate, so they are
untouched by construction rather than by a second check somebody has to
remember.

WHY A TABLE AND NOT A SETTING. The other settings live in the
credentials file, which is 0600 and rewritten wholesale to change one
field - the wrong shape for a switch that has to be flipped in a hurry,
and no place for an audit trail. This is append-only, like
`benchmark_baselines`, whose append-only design is the only reason a
month of overwritten tracking was recoverable: every engage and every
release is its own row, so "when did we stop spending, and who started
it again" is a question the record answers.

AND WHAT AN UNREADABLE STATE MEANS, which is the question this project
has had to answer four times now (sections 16, 22, 27, 31) and has got
wrong by guessing:

A failed read is NOT an engaged stop. Inventing a pause from a database
hiccup would suspend the bot with nobody having asked, which is the
section 16 failure - a transient answer becoming a verdict - and the
money guard the owner actually asked for ("a hard stop to stop bot using
all the budget") is the monthly and daily cap, which does not depend on
this table at all.

But a failed read must never RELEASE a stop either. So the last state
this process read successfully is remembered, and a read that fails
falls back to it. A process that has seen the stop engaged keeps it
engaged; only a successful read of a release can lift it.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

ENGAGED = "engaged"
RELEASED = "released"

#: The governor's refusal reason, and the funnel's drop reason. One
#: string, so the page and the ledger cannot disagree about the name.
STOP_REASON = "emergency_stop_engaged"

#: What this process last read successfully, keyed by database path. A
#: failed read falls back to it rather than to "running" - see the
#: module docstring. Not a cache for speed: the read is one indexed row.
_LAST_KNOWN: dict[str, bool] = {}


@dataclass(frozen=True)
class StopState:
    """Whether spending and entering are suspended, and on what
    evidence. `read_failed` carries the raw upstream error beside the
    answer rather than instead of it (house rule 3)."""

    engaged: bool
    at: str = ""
    reason: str = ""
    set_by: str = ""
    read_failed: str = ""
    #: True when `engaged` came from this process's memory because the
    #: read failed. The dashboard says so; a stop nobody can verify is
    #: not the same fact as one read from the row.
    from_memory: bool = False


def _path_of(conn) -> str:
    """A key for the memory. A connection with no discoverable file
    still gets a stable key, so an in-memory database used by a test
    behaves like a real one."""
    try:
        for row in conn.execute("PRAGMA database_list"):
            if str(row[1]) == "main":
                return str(row[2] or "") or ":memory:"
    except (sqlite3.Error, AttributeError, IndexError, TypeError):
        pass
    return ":memory:"


def current(conn) -> StopState:
    """The newest engage/release row, or a released state if none.

    NEVER RAISES. Every caller is deciding whether to spend or to trade,
    and an exception here would abandon the cycle - which stops the hard
    exits too, and those are the half that must keep running.
    """
    key = _path_of(conn)
    try:
        rows = conn.execute(
            "SELECT state, at, reason, set_by FROM emergency_stop_events "
            "ORDER BY at DESC, rowid DESC LIMIT 1").fetchall()
    except (sqlite3.Error, AttributeError) as exc:
        remembered = _LAST_KNOWN.get(key, False)
        # LOUD, because the owner cannot see this any other way and a
        # silent fallback is how a stop that is not really enforced
        # reads as one that is.
        _log.error(
            "The emergency stop could not be read (%s). Treating it as %s, "
            "which is what this process last read. The monthly and daily "
            "budget caps are unaffected and still in force.",
            exc, "ENGAGED" if remembered else "not engaged")
        return StopState(engaged=remembered, read_failed=str(exc),
                         from_memory=True)

    if not rows:
        _LAST_KNOWN[key] = False
        return StopState(engaged=False)
    state, at, reason, set_by = rows[0]
    # AN UNRECOGNISED STATE IS NOT AN ENGAGED ONE. The CHECK constraint
    # makes it unreachable through the database; if somebody relaxes
    # that, an unreadable word must not be able to suspend the bot by
    # accident. It cannot suspend it on purpose either, which is why the
    # engage path writes the word rather than anything else.
    engaged = str(state or "").strip().lower() == ENGAGED
    _LAST_KNOWN[key] = engaged
    return StopState(engaged=engaged, at=str(at or ""),
                     reason=str(reason or ""), set_by=str(set_by or ""))


def is_engaged(conn) -> bool:
    """The one-line reading, for the gates."""
    return current(conn).engaged


def engage(conn, *, reason: str = "", set_by: str = "owner") -> StopState:
    """Suspend Claude spending and new entries. Idempotent in effect:
    engaging an already-engaged stop writes a second row, which is the
    record of it being asked for twice and changes nothing."""
    return _write(conn, ENGAGED, reason, set_by)


def release(conn, *, reason: str = "", set_by: str = "owner") -> StopState:
    """Resume. The owner's action only - nothing in this system releases
    its own stop, because a bot that can lift the switch that stops it
    is not stopped."""
    return _write(conn, RELEASED, reason, set_by)


def _write(conn, state: str, reason: str, set_by: str) -> StopState:
    at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO emergency_stop_events "
        "(id, state, reason, set_by, at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), state, str(reason or "")[:500],
         str(set_by or "")[:120], at))
    conn.commit()
    _LAST_KNOWN[_path_of(conn)] = state == ENGAGED
    _log.warning("EMERGENCY STOP %s by %s%s",
                 "ENGAGED - no Claude spending and no new entries; open "
                 "positions keep their stops and still sell on their hard "
                 "exit date" if state == ENGAGED else "RELEASED - normal "
                 "operation resumes on the next cycle",
                 set_by or "owner",
                 f" ({reason})" if reason else "")
    return StopState(engaged=state == ENGAGED, at=at, reason=str(reason or ""),
                     set_by=str(set_by or ""))


def history(conn, limit: int = 20) -> list[dict]:
    """Every engage and release, newest first. For the dashboard - the
    rows are never deleted, so "it was paused for six hours on Tuesday"
    stays answerable."""
    try:
        rows = conn.execute(
            "SELECT state, at, reason, set_by FROM emergency_stop_events "
            "ORDER BY at DESC, rowid DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
    except (sqlite3.Error, AttributeError, TypeError, ValueError):
        return []
    return [{"state": r[0], "at": r[1], "reason": r[2], "set_by": r[3]}
            for r in rows]
