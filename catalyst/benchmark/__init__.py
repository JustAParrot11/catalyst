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

    @property
    def fingerprints(self) -> set:
        """Every identifier this baseline's account has ever presented.

        STORED AS A SET, space-separated, because matching on one field
        is not enough. The column used to hold a single hash of
        `id or account_number`; a read that omitted `id` then hashed
        `account_number` instead, matched nothing, and the bot concluded
        the account had been swapped (measured 2026-09-12 - it
        re-baselined an unchanged account). An older row holding one hash
        parses as a one-element set, so nothing needs migrating.
        """
        return {part for part in str(self.account_fingerprint or "").split()
                if part}

    @property
    def is_unreadable(self) -> bool:
        """True when a baseline EXISTS and could not be read.

        OWNER-REPORTED 2026-09-12: *"something has gone wrong, it has
        reset my SPY track, it has randomly gone to tracking from 11/09
        when it started 14/08"*.

        "Nothing has ever been stored" and "something is stored and I
        cannot read it" were the same state, and the second one was
        treated as the first - so ONE unparseable row restarted the
        comparison at today and threw away a month of tracking.
        Distinguishing them is the whole point: absence of a baseline is
        a reason to strike one, a failure to read it never is.
        """
        return self.source == "unreadable"


#: The field a stored fingerprint is always computed from. Alpaca's
#: /v2/account always returns `id`; `account_number` is accepted when
#: COMPARING, so an older row or a payload missing `id` still matches,
#: but it is never what gets written - a stored value that depends on
#: which fields a read happened to carry is what flipped and restarted
#: the owner's SPY comparison (measured 2026-09-12).
CANONICAL_ID_FIELD = "id"


def account_fingerprints(account: dict) -> set:
    """EVERY identifier this payload carries, hashed.

    OWNER-REPORTED 2026-09-12: the SPY comparison restarted on its own.
    One cause: the fingerprint was `account.get("id") or
    account.get("account_number")`, so a single read that happened to
    omit `id` produced a DIFFERENT fingerprint for the SAME account, and
    the bot concluded the account had been swapped. Measured: it
    re-baselined on an unchanged account.

    A set, compared by intersection, so any identifier matching means
    the same account. That is the honest test - two payloads describe the
    same account when they agree on any identifier, not only when they
    happen to carry the same field.
    """
    out = set()
    for key in ("id", "account_number"):
        fp = fingerprint_account((account or {}).get(key))
        if fp:
            out.add(fp)
    return out


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
    except sqlite3.Error as exc:
        # A TABLE THAT WILL NOT READ IS NOT AN ABSENT BASELINE. Returning
        # the placeholder here told sync_with_account "nothing has ever
        # been stored", and it answered by striking a new baseline at
        # today - throwing away the owner's tracking over one failed
        # read (owner-reported 2026-09-12).
        return Baseline(
            capital_cents=FALLBACK_CAPITAL_CENTS,
            start_date=datetime.now(timezone.utc).date(),
            source="unreadable", account_fingerprint="", set_at="",
            reason=f"the baseline table could not be read ({exc}). The "
                   "stored baseline is unchanged and is NOT replaced on "
                   "the strength of a failed read.")
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
        # A row we cannot read is a fact worth showing, not a crash - and
        # it is "unreadable", NOT "unset". It used to report source
        # "unset", which sync_with_account reads as "nothing has ever
        # been stored" and answers by re-baselining at today. Measured
        # 2026-09-12: one row with an unparseable start_date restarted a
        # month of tracking.
        return Baseline(
            capital_cents=FALLBACK_CAPITAL_CENTS,
            start_date=datetime.now(timezone.utc).date(),
            source="unreadable", account_fingerprint="", set_at="",
            reason=f"the stored baseline row could not be read: {row!r}. It "
                   "is NOT replaced on the strength of a row this code "
                   "cannot parse.")


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
    # COMPARE AGAINST EVERY IDENTIFIER, WRITE ONLY THE CANONICAL ONE.
    #
    # The comparison is wide so a payload that omits one field is still
    # recognised - and so rows written by the older `id or
    # account_number` code keep matching. The WRITE is narrow so the
    # stored value does not depend on which fields a particular read
    # happened to carry, which is what made it flip.
    seen = account_fingerprints(account)
    if not seen:
        return current(conn), False
    # The canonical field for WRITING, with the long-standing fallback to
    # account_number so a payload without `id` can still strike a FIRST
    # baseline. What changed is the mismatch rule below.
    fp = (fingerprint_account((account or {}).get(CANONICAL_ID_FIELD))
          or fingerprint_account((account or {}).get("account_number")))

    now = current(conn)

    # ONLY POSITIVE EVIDENCE OF A DIFFERENT ACCOUNT MAY RESTART THE
    # COMPARISON. Owner-reported 2026-09-12: "it has reset my SPY track,
    # it has randomly gone to tracking from 11/09 when it started 14/08."
    # Three separate ways it fired, all measured, all the same mistake -
    # absence of evidence read as evidence of change:
    #
    #   1. An OWNER-SET baseline stores no fingerprint, so ""
    #      != the live hash and it was overwritten on the VERY NEXT
    #      CYCLE - with a reason claiming the account had changed, from a
    #      blank fingerprint to a real one. The docstring above has
    #      always promised the opposite.
    #   2. The fingerprint was `id or account_number`, so one read that
    #      omitted `id` looked like a different account.
    #   3. An unparseable row reported source "unset", which reads as
    #      "nothing stored" and struck a new baseline at today.
    #
    # So each of those is now its own explicit branch, and none of them
    # records anything.

    # (3) A baseline we cannot read is never replaced. There IS one; the
    # only honest thing to do is leave it alone and say so.
    if now.is_unreadable:
        return now, False

    # (2) Same account if ANY identifier matches one we have ever seen
    # for it. When this read carries an identifier the baseline has not
    # recorded yet, remember it - so the next read that presents only
    # that field is still recognised.
    known = now.fingerprints
    if known and (known & seen):
        return now, False

    # A MISMATCH IS NOT ENOUGH ON ITS OWN. If this read does not carry the
    # canonical identifier, "different account" and "same account
    # reported by a different field" are indistinguishable - and one of
    # those answers destroys a month of the owner's tracking while the
    # other costs nothing. Measured 2026-09-12: a read that omitted `id`
    # re-baselined an unchanged account.
    #
    # So a restart needs positive evidence: the canonical field present,
    # and different from the one on record.
    if known and not (account or {}).get(CANONICAL_ID_FIELD):
        return now, False

    try:
        equity = Decimal(str(account["equity"])) * 100
    except (KeyError, TypeError, ValueError, ArithmeticError):
        # No readable equity means no honest baseline. Say nothing
        # rather than strike one against a number we do not have.
        return now, False

    # (1) A baseline with NO fingerprint is not a different account - it
    # is a baseline that predates fingerprinting, or one the owner set by
    # hand from a page that cannot know the account id. Adopt the
    # fingerprint onto it and keep the owner's dates and money.
    if not now.account_fingerprint and not now.is_placeholder:
        return record(
            conn, capital_cents=now.capital_cents,
            start_date=now.start_date, source=now.source,
            account_fingerprint=fp,
            reason=(
                f"{now.reason} [Account fingerprint {fp} attached on "
                f"{today}; the comparison was NOT restarted. A baseline "
                "carrying no fingerprint is one set before fingerprinting "
                "or set by hand, not a different account.]")), False

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
