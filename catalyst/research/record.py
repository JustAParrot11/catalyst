"""The bot's own recent outcomes, rendered for the research prompt.

WHY. CLAUDE.md, "What is not proven": "Claude learns nothing between
calls. No past outcome reaches the research prompt. It judges every
candidate as if it were the first. Closing that loop is the
highest-value next change."

OWNER-ASKED 2026-09-05: "optimize how agentic and self sufficient it is
... I need claude to be looking daily". A model that never sees what
its last twenty calls did cannot calibrate anything, however carefully
the conviction scale is defined. This is the loop closed at its
cheapest: the outcomes already exist in two tables, and rendering them
is a few hundred tokens against a prompt measured at tens of thousands.

WHAT IT IS NOT. It is informational text, like the market section: it
sizes nothing, it decides nothing, and code never reads it back. The
model is told its record and asked to weigh it. The refusal tracker and
the adaptive parameters remain the mechanisms that MOVE anything, on
closed outcomes, with sample minimums - this only lets the model see
the same evidence they see.

Never raises. A database without the tables, or with nothing scored
yet, produces None and the prompt simply has no such section.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

#: How many of each the prompt carries. Enough to show a pattern, few
#: enough that a bad week cannot become the whole prompt.
MAX_TRADES = 8
MAX_REFUSALS = 8

RECORD_HEADER = "YOUR RECENT RECORD (informational only)"


def _pct(x) -> str:
    try:
        return f"{Decimal(str(x)) * 100:+.1f}%"
    except (ArithmeticError, TypeError, ValueError):
        return "?"


def recent_trades(conn: sqlite3.Connection, limit: int = MAX_TRADES) -> list[dict]:
    """Closed trades, newest first, with what the model said going in."""
    rows = conn.execute(
        """SELECT p.ticker, t.exit_reason, t.realized_pnl_cents,
                  t.entry_price, t.exit_price, t.actual_holding_days,
                  t.expected_holding_days, t.closed_at,
                  v.direction, v.conviction, c.catalyst_type
           FROM closed_trades t
           JOIN positions p ON p.id = t.position_id
           LEFT JOIN orders o ON o.id = json_extract(
                CASE WHEN json_valid(p.entry_order_ids)
                     THEN p.entry_order_ids ELSE '[]' END, '$[0]')
           LEFT JOIN research_views v ON v.candidate_id = o.decision_id
           LEFT JOIN candidates c ON c.id = o.decision_id
           ORDER BY t.closed_at DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        try:
            entry, exit_ = Decimal(str(r[3])), Decimal(str(r[4]))
            ret = (exit_ - entry) / entry if entry > 0 else None
        except (ArithmeticError, TypeError, ValueError):
            ret = None
        out.append({"ticker": r[0], "exit_reason": r[1], "pnl_cents": r[2],
                    "ret": ret, "held": r[5], "expected": r[6],
                    "closed_at": str(r[7])[:10], "direction": r[8],
                    "conviction": r[9], "catalyst_type": r[10]})
    return out


def recent_scored_refusals(conn: sqlite3.Connection,
                           limit: int = MAX_REFUSALS) -> list[dict]:
    """Declined candidates whose counterfactual window has closed."""
    rows = conn.execute(
        """SELECT c.ticker, c.catalyst_type, v.direction, v.conviction,
                  v.priced_in, r.outcome_return, r.refused_at,
                  d.skip_reasons
           FROM refusals r
           JOIN candidates c ON c.id = r.candidate_id
           LEFT JOIN research_views v ON v.candidate_id = r.candidate_id
           LEFT JOIN risk_decisions d ON d.id = r.decision_id
           WHERE r.scored_at IS NOT NULL
           ORDER BY r.refused_at DESC LIMIT ?""", (limit,)).fetchall()
    return [{"ticker": r[0], "catalyst_type": r[1], "direction": r[2],
             "conviction": r[3], "priced_in": bool(r[4]),
             "ret": r[5], "refused_at": str(r[6])[:10],
             "why": str(r[7] or "")} for r in rows]


def render_record(trades: list[dict], refusals: list[dict]) -> str | None:
    if not trades and not refusals:
        return None
    lines = [RECORD_HEADER,
             "What this system's earlier calls went on to do. It sizes "
             "nothing and decides nothing; weigh it as you would weigh "
             "your own track record on setups like this one."]
    if trades:
        lines.append("")
        lines.append(f"Closed trades, newest first ({len(trades)}):")
        for t in trades:
            conv = (f"{float(t['conviction']):.2f}"
                    if t["conviction"] is not None else "?")
            lines.append(
                f"  - {t['ticker']} ({t['catalyst_type'] or '?'}), "
                f"{t['direction'] or '?'} at conviction {conv}: "
                f"{_pct(t['ret']) if t['ret'] is not None else '?'} over "
                f"{t['held']} day(s) (planned {t['expected']}), closed "
                f"{t['closed_at']} by {t['exit_reason']}.")
    if refusals:
        lines.append("")
        lines.append(f"Declined candidates, scored after the holding window "
                     f"({len(refusals)}) - what the stock did WITHOUT us:")
        for r in refusals:
            conv = (f"{float(r['conviction']):.2f}"
                    if r["conviction"] is not None else "?")
            flag = " [called priced in]" if r["priced_in"] else ""
            lines.append(
                f"  - {r['ticker']} ({r['catalyst_type'] or '?'}), you said "
                f"{r['direction'] or '?'} at {conv}{flag}: the stock went "
                f"{_pct(r['ret'])} after {r['refused_at']}.")
        lines.append(
            "A declined name that went on to rise is a trade this system "
            "refused without skill; a run of them says the bar is too "
            "high. A declined name that fell says the refusal was right.")
    return "\n".join(lines)


def recent_record(conn, now: datetime | None = None) -> str | None:
    """The section, or None. Never raises."""
    try:
        return render_record(recent_trades(conn), recent_scored_refusals(conn))
    except Exception:  # noqa: BLE001 - the prompt must not die on history
        return None
