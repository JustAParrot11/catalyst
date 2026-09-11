"""The refusal tracker: score what declined candidates went on to do.

BUILD-BRIEF: "Record the price whenever a candidate is declined, then
score what it went on to do. If refused candidates are systematically
profitable, the threshold that refused them is too strict - and now that
is a number rather than an argument. This is the single most important
feedback loop in the system."

cycle.py records the refusal price at decision time (NBBO mid). This
module closes the loop: after the strategy's holding horizon has passed,
the refusal is scored at the then-current price, and scored refusals are
aggregated into an EvidenceSample for the conviction floor - the input
adaptive_params.propose_adjustment demands.

Scored refusals are CLOSED outcomes in the sense the adaptation rules
require: the counterfactual trade's window has fully elapsed before the
score is taken. Nothing here reads unrealised anything.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from catalyst.execution.broker import Broker, BrokerError
from catalyst.risk.adaptive_params import EvidenceSample

# Score at the strategy's measured hold (insider_cluster HOLD_DAYS=12);
# a refusal younger than this has no complete counterfactual yet.
SCORING_HORIZON_DAYS = 12

# Refusals scored later than this after coming due are still recorded,
# but the staleness is visible in scored_at vs refused_at.


#: Why a due refusal was NOT scored this pass, keyed by rowid. Read by
#: the dashboard so "6 of 223 scored" can say what the other 217 are
#: waiting on - a quote that keeps failing is a delisted or renamed
#: ticker, which is a different fact from "not due yet" (house rule 3).
#: In-process only; `refusal_scoring_skips` is the durable copy.
LAST_UNSCORED_REASONS: dict = {}


def _remember_skip(conn, candidate_id, ticker, reason, now) -> None:
    """Persist WHY this refusal is still unscored.

    IN-PROCESS WAS NOT ENOUGH. The dict above was added on 2026-09-05 to
    answer exactly this question and it cannot: a diagnostic bundle is
    written by a different process from the cycle, so it only ever saw
    an empty dict. The owner's 2026-09-11 bundle then showed 291
    refusals and ~none scored, with nothing anywhere saying why.

    `attempts` counts, so "the quote failed once" and "this ticker has
    refused a quote forty times" - a delisting, a rename - are
    different facts rather than the same line. Never raises: a
    diagnostic must not be able to break the loop it describes.
    """
    try:
        conn.execute(
            "INSERT INTO refusal_scoring_skips "
            "(candidate_id, ticker, reason, attempted_at, attempts) "
            "VALUES (?,?,?,?,1) "
            "ON CONFLICT(candidate_id) DO UPDATE SET "
            "  ticker=excluded.ticker, reason=excluded.reason, "
            "  attempted_at=excluded.attempted_at, "
            "  attempts=refusal_scoring_skips.attempts + 1",
            (str(candidate_id), str(ticker), str(reason)[:500],
             now.isoformat()))
    except Exception:  # noqa: BLE001 - an older database has no table yet
        pass


def _forget_skip(conn, candidate_id) -> None:
    """A refusal that scored is no longer waiting on anything."""
    try:
        conn.execute("DELETE FROM refusal_scoring_skips WHERE candidate_id = ?",
                     (str(candidate_id),))
    except Exception:  # noqa: BLE001
        pass


def score_due_refusals(broker: Broker, conn,
                       now: datetime | None = None) -> int:
    """Score every unscored refusal whose horizon has elapsed, at the
    current NBBO mid. Returns how many were scored. Broker failures skip
    the refusal (it stays unscored and is retried next cycle) - a
    missing quote never fabricates an outcome. Every skip records WHY
    in LAST_UNSCORED_REASONS."""
    now = now or datetime.now(timezone.utc)
    due_before = (now - timedelta(days=SCORING_HORIZON_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT r.rowid, r.price_at_refusal, c.ticker, r.candidate_id
           FROM refusals r JOIN candidates c ON c.id = r.candidate_id
           WHERE r.scored_at IS NULL AND r.refused_at <= ?""",
        (due_before,)).fetchall()
    scored = 0
    LAST_UNSCORED_REASONS.clear()

    def skip(rowid, ticker, why, candidate_id):
        LAST_UNSCORED_REASONS[rowid] = f"{ticker}: {why}"
        _remember_skip(conn, candidate_id, ticker, why, now)

    for rowid, price_at_refusal, ticker, candidate_id in rows:
        try:
            q = broker.get_latest_quote(ticker)
        except BrokerError as exc:
            skip(rowid, ticker, f"quote refused ({exc})", candidate_id)
            continue
        quote = q.get("quote") or {}
        if not isinstance(quote, dict):
            skip(rowid, ticker, f"no quote object in {str(q)[:120]!r}", candidate_id)
            continue
        try:
            bid = Decimal(str(quote.get("bp")))
            ask = Decimal(str(quote.get("ap")))
        except (ArithmeticError, TypeError, ValueError):
            skip(rowid, ticker, f"unreadable bid/ask {quote.get('bp')!r}/"
                                f"{quote.get('ap')!r}", candidate_id)
            continue
        # NaN/Infinity survive Decimal() and raise on the FIRST
        # comparison instead (stress-tester defect 3).
        if not (bid.is_finite() and ask.is_finite()):
            skip(rowid, ticker, "non-finite bid/ask", candidate_id)
            continue
        if bid <= 0 or ask <= 0 or ask < bid:
            skip(rowid, ticker, f"unusable NBBO bid {bid} ask {ask} - "
                                "an off-hours or halted quote", candidate_id)
            continue
        outcome = (bid + ask) / 2
        entry = Decimal(price_at_refusal)
        if entry <= 0:
            skip(rowid, ticker, f"refusal price {entry} is not a price", candidate_id)
            continue
        ret = (outcome - entry) / entry
        conn.execute(
            """UPDATE refusals SET scored_at = ?, outcome_price = ?,
               outcome_return = ? WHERE rowid = ?""",
            (now.isoformat(), str(outcome), str(ret), rowid))
        _forget_skip(conn, candidate_id)
        scored += 1
    conn.commit()
    return scored


def conviction_floor_evidence(conn,
                              now: datetime | None = None,
                              window_start: datetime | None = None,
                              ) -> EvidenceSample | None:
    """Aggregate refusals refused for below_conviction_floor (and scored)
    into an EvidenceSample for the conviction_floor parameter.

    Sign convention (adaptive_params): effect_size > 0 means "raise the
    value". Systematically PROFITABLE refusals mean the floor refused
    good trades -> evidence to LOWER it -> negative effect_size.

    Statistics are a mean/standard-error normal approximation - crude,
    and crude is fine: the significance floor and minimum sample in
    adaptive_params are the real guards. Returns None when there is
    nothing scored in the window."""
    now = now or datetime.now(timezone.utc)
    clauses = ["r.scored_at IS NOT NULL",
               "d.skip_reasons LIKE '%below_conviction_floor%'"]
    params: list = []
    if window_start is not None:
        clauses.append("r.refused_at >= ?")
        params.append(window_start.isoformat())
    rows = conn.execute(
        f"""SELECT r.candidate_id, r.outcome_return, r.refused_at
            FROM refusals r JOIN risk_decisions d ON d.id = r.decision_id
            WHERE {' AND '.join(clauses)}
            ORDER BY r.refused_at""", params).fetchall()
    if not rows:
        return None

    returns = [Decimal(r[1]) for r in rows]
    n = len(returns)
    mean = sum(returns) / n
    if n > 1:
        var = sum((x - mean) ** 2 for x in returns) / (n - 1)
        se = (var / n).sqrt() if var > 0 else Decimal("0")
    else:
        se = Decimal("0")

    # |t| mapped to a coarse two-sided confidence bucket; deliberately
    # conservative (normal approximation overstates small-n confidence,
    # so bucket boundaries are set above the usual z values).
    t = abs(mean) / se if se > 0 else Decimal("0")
    if t >= 3:
        significance = Decimal("0.99")
    elif t >= Decimal("2.2"):
        significance = Decimal("0.95")
    elif t >= Decimal("1.8"):
        significance = Decimal("0.90")
    else:
        significance = Decimal("0.50")

    strength = min(Decimal("1"), t / 4)
    return EvidenceSample(
        parameter="conviction_floor",
        trade_ids=tuple(r[0] for r in rows),
        window_start=datetime.fromisoformat(rows[0][2]),
        window_end=datetime.fromisoformat(rows[-1][2]),
        # profitable refusals => floor too strict => lower it
        effect_size=-mean,
        significance=significance,
        evidence_strength=strength)
