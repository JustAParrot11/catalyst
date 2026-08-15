"""What "the same money in SPY instead" means, as stored facts.

OWNER-ASKED, 2026-08-14: "ensure there is no hardcode to work from the
1000, when I change the Alpaca keys i want it to register there is a new
account and restart the SPY tracker, also a section so I can emulate the
SPY with a custom field ... e.g. I can say track SPY if i were to invest
$2000 on a set date and calculate that against our bot."

THE HARDCODE WAS REAL AND IT WAS LOAD-BEARING. START_CAPITAL_CENTS =
100_000 in dashboard/db.py drove net equity, the SPY index, the whole
performance curve and the annual-hurdle arithmetic. Point the bot at a
$2,000 account without changing it and every one of those figures
compares the new account against the old base - silently, and in the
direction that flatters or damns at random.

So the baseline is DATA now, not a constant, and it carries three
things: how much, from when, and why it changed.

APPEND-ONLY, like adaptive_param_log. The current baseline is the
latest row. There is deliberately no "current" table that could drift
from the history, because the audit trail and the live state are the
same rows.

THE ACCOUNT FINGERPRINT IS A HASH OF THE BROKER'S ACCOUNT ID, never a
key and never a secret. It exists so a swapped set of Alpaca
credentials is DETECTED rather than assumed: a new account is a new
experiment, and comparing it against a baseline struck for the old one
would be arithmetic on two different things.
"""

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

#: Used only when there is no account to read and no baseline stored -
#: a fresh install that has not seen the broker yet. It is a placeholder
#: for a first render, NOT a policy, and any real account replaces it on
#: the first successful read.
FALLBACK_CAPITAL_CENTS = Decimal("100000")


@dataclass(frozen=True)
class Baseline:
    """The comparison the bot is judged against."""

    capital_cents: Decimal
    start_date: date
    source: str
    account_fingerprint: str
    reason: str
    set_at: str

    @property
    def is_placeholder(self) -> bool:
        """True when nothing has ever been stored, so the page can say
        so rather than presenting a default as a decision."""
        return self.source == "unset"


def fingerprint_account(account_id) -> str:
    """A stable, non-reversible id for a broker account.

    Hashed rather than stored, because the account id is the closest
    thing in the broker payload to an identifier for the owner, and this
    row ends up in diagnostic bundles. Truncated to 16 hex characters:
    long enough that two accounts will not collide, short enough to read
    on a page.
    """
    text = str(account_id or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def current(conn: sqlite3.Connection) -> Baseline:
    """The baseline in force. Never raises - a dashboard that cannot
    render because a table is missing is worse than one that says the
    baseline is unset."""
    try:
        row = conn.execute(
            "SELECT capital_cents, start_date, source, account_fingerprint, "
            "reason, set_at FROM benchmark_baselines "
            "ORDER BY set_at DESC, rowid DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return Baseline(
            capital_cents=FALLBACK_CAPITAL_CENTS,
            start_date=datetime.now(timezone.utc).date(),
            source="unset", account_fingerprint="", set_at="",
            reason="no baseline recorded yet - this is a placeholder, not "
                   "a decision. It is replaced the first time the broker "
                   "account is read.")
    try:
        return Baseline(
            capital_cents=Decimal(str(row[0])),
            start_date=date.fromisoformat(str(row[1])),
            source=str(row[2]), account_fingerprint=str(row[3]),
            reason=str(row[4]), set_at=str(row[5]))
    except (ValueError, ArithmeticError):
        # A row we cannot read is a fact worth showing, not a crash.
        return Baseline(
            capital_cents=FALLBACK_CAPITAL_CENTS,
            start_date=datetime.now(timezone.utc).date(),
            source="unset", account_fingerprint="", set_at="",
            reason=f"the stored baseline row could not be read: {row!r}")


def record(conn: sqlite3.Connection, *, capital_cents, start_date: date,
           source: str, account_fingerprint: str, reason: str) -> Baseline:
    """Write a new baseline. The previous ones stay, forever."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO benchmark_baselines (id, capital_cents, start_date, "
        "source, account_fingerprint, reason, set_at) VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), str(Decimal(str(capital_cents))),
         start_date.isoformat(), source, account_fingerprint, reason, now))
    conn.commit()
    return current(conn)


def sync_with_account(conn: sqlite3.Connection, account: dict,
                      today: date | None = None) -> tuple[Baseline, bool]:
    """Reconcile the baseline against the account actually connected.

    Returns (baseline, changed). Called on every healthy cycle; it does
    nothing at all unless the account is genuinely different, so it is
    cheap to call often.

    A NEW ACCOUNT RESTARTS THE COMPARISON, which is the owner's
    instruction and also the only defensible arithmetic: SPY bought with
    $1,000 in July is not the benchmark for a $2,000 account opened in
    August. The old baseline is not deleted - it stays in the history
    with the reason it was replaced.

    AN OWNER-SET BASELINE IS NOT OVERWRITTEN. If someone has said "track
    SPY as if I had put $2,000 in on 1 July", that is a deliberate
    question and a routine account read must not silently answer a
    different one. Only a genuine account CHANGE overrides it, and it
    says so.
    """
    today = today or datetime.now(timezone.utc).date()
    fp = fingerprint_account(account.get("id") or account.get("account_number"))
    if not fp:
        return current(conn), False

    now = current(conn)
    if now.account_fingerprint == fp and not now.is_placeholder:
        return now, False

    try:
        equity = Decimal(str(account["equity"])) * 100
    except (KeyError, TypeError, ValueError, ArithmeticError):
        # No readable equity means no honest baseline. Say nothing
        # rather than strike one against a number we do not have.
        return now, False

    first = now.is_placeholder
    return record(
        conn, capital_cents=equity, start_date=today,
        source="first_run" if first else "account_changed",
        account_fingerprint=fp,
        reason=(
            f"first broker account seen; SPY is bought with the account's "
            f"own opening equity on {today}"
            if first else
            f"the connected account changed (fingerprint {now.account_fingerprint} "
            f"-> {fp}), so the comparison restarts from this account's "
            f"equity on {today}. A new account is a new experiment; the "
            "previous baseline stays in the history above.")), True
