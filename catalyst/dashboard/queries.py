"""Every read the dashboard performs, as pure-ish functions of a Db.

Each function returns a dataclass that carries BOTH the computed numbers
and the QueryResult objects behind them, so the renderer can always
print provenance and, on a zero, the exact query that produced it.

Nothing here writes. The two write paths live in server.py.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from catalyst.dashboard.db import Db, QueryResult, START_CAPITAL_CENTS, bars_path, jload


def _as_date(text) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _dec(text, default=Decimal("0")) -> Decimal:
    if text is None:
        return default
    try:
        return Decimal(str(text))
    except (InvalidOperation, ValueError):
        return default


# --------------------------------------------------------------------------
# 1. Performance against the S&P 500, net of all costs
# --------------------------------------------------------------------------


@dataclass
class Performance:
    closed_q: QueryResult
    costs_q: QueryResult
    n_closed: int = 0
    n_closed_live: int = 0
    n_closed_paper: int = 0
    gross_pnl_cents: Decimal = Decimal("0")
    scheduled_cost_cents: Decimal = Decimal("0")
    manual_cost_cents: Decimal = Decimal("0")
    bot_points: list = field(default_factory=list)      # [(date, index, equity_cents)]
    spy_points: list = field(default_factory=list)      # [(date, index, equity_cents)]
    spy_source: str = ""
    spy_error: str | None = None
    spy_rows: int = 0
    #: True when the cache is HEALTHY and the bot's own window is simply
    #: shorter than one trading day - a new account over a weekend, not a
    #: fault. Kept as a flag rather than sniffed out of spy_error, so the
    #: page can stop calling a normal Monday morning "unavailable".
    spy_window_too_short: bool = False
    #: Which Alpaca feed the cached bars came from. "iex" is one
    #: exchange's prints rather than the consolidated tape - a fine daily
    #: benchmark for an instrument as liquid as SPY, but the page has to
    #: say so rather than let a reader assume the tape.
    spy_feed: str = ""
    start_day: date | None = None
    end_day: date | None = None

    @property
    def net_equity_cents(self) -> Decimal:
        return (Decimal(START_CAPITAL_CENTS) + self.gross_pnl_cents
                - self.scheduled_cost_cents - self.manual_cost_cents)

    @property
    def bot_index(self) -> float | None:
        return self.bot_points[-1][1] if self.bot_points else None

    @property
    def spy_index(self) -> float | None:
        return self.spy_points[-1][1] if self.spy_points else None

    @property
    def excess_pp(self) -> float | None:
        if self.bot_index is None or self.spy_index is None:
            return None
        return self.bot_index - self.spy_index

    @property
    def sample_is_meaningful(self) -> bool:
        from catalyst.dashboard.render import MIN_TRADES_FOR_MEANING

        return self.n_closed >= MIN_TRADES_FOR_MEANING


def _load_spy(start: date, end: date):
    """SPY closes from the local bar cache. Returns (points, source, error,
    row_count). Never touches the network — the dashboard reads what
    scripts/fetch_history.py already cached."""
    root = bars_path()
    try:
        from catalyst.backtest.data import BarCache

        cache = BarCache(root)
        bars = cache.load_bars("SPY")
    except Exception as exc:
        return [], f"local bar cache {root}/SPY.csv", f"{type(exc).__name__}: {exc}", 0, ""

    window = [b for b in bars if start <= b.day <= end]
    if not window:
        span = f"{bars[0].day}..{bars[-1].day}" if bars else "empty file"
        return (
            [], f"local bar cache {root}/SPY.csv",
            f"cache holds {len(bars)} bars ({span}) but none inside the bot's "
            f"window {start}..{end}",
            len(bars), "",
        )
    base = window[0].close
    points = [
        (b.day, float(b.close / base * 100), int(b.close / base * START_CAPITAL_CENTS))
        for b in window
    ]
    # Read the basis from the cache metadata rather than asserting it.
    # The label used to hardcode "feed=sip, adjustment=all"; if a refresh
    # ever wrote a different basis, the page would have gone on claiming
    # the old one - a caption that cannot be wrong is not provenance.
    basis, meta, feed = "basis unrecorded", "", ""
    try:
        raw_meta = cache.read_meta() or {}
        feed = str(raw_meta.get("feed") or "")
        if raw_meta.get("feed") or raw_meta.get("adjustment"):
            basis = (f"feed={raw_meta.get('feed', 'unrecorded')}, "
                     f"adjustment={raw_meta.get('adjustment', 'unrecorded')}")
        meta = f", fetched_at={raw_meta.get('fetched_at', 'unknown')}"
    except Exception:
        meta = ", cache_meta unreadable"
    return (points, f"local bar cache {root}/SPY.csv ({basis}{meta})", None,
            len(window), feed)


def performance(db: Db) -> Performance:
    closed_q = db.q(
        "SELECT position_id, account_mode, realized_pnl_cents, closed_at, exit_reason "
        "FROM closed_trades ORDER BY closed_at"
    )
    costs_q = db.q(
        "SELECT kind, priced_cents, priced_at FROM cost_events "
        "WHERE priced_cents IS NOT NULL ORDER BY priced_at"
    )
    perf = Performance(closed_q=closed_q, costs_q=costs_q)

    trades = [
        (_as_date(r["closed_at"]), _dec(r["realized_pnl_cents"]), r["account_mode"])
        for r in closed_q.rows
    ]
    trades = [t for t in trades if t[0] is not None]
    perf.n_closed = len(trades)
    perf.n_closed_live = sum(1 for t in trades if t[2] == "live")
    perf.n_closed_paper = perf.n_closed - perf.n_closed_live
    perf.gross_pnl_cents = sum((t[1] for t in trades), Decimal("0"))

    costs = [
        (_as_date(r["priced_at"]), _dec(r["priced_cents"]), r["kind"])
        for r in costs_q.rows
    ]
    costs = [c for c in costs if c[0] is not None]
    perf.scheduled_cost_cents = sum((c[1] for c in costs if c[2] == "scheduled"), Decimal("0"))
    perf.manual_cost_cents = sum((c[1] for c in costs if c[2] == "manual"), Decimal("0"))

    days = sorted({t[0] for t in trades} | {c[0] for c in costs})
    if not days:
        # No equity series to index against - but still SAY what state the
        # benchmark cache is in, so "the bot has done nothing" and "the SPY
        # cache is missing" are two different sentences on the page.
        today = datetime.now(timezone.utc).date()
        _, source, error, rows, feed = _load_spy(today - timedelta(days=30), today)
        perf.spy_source, perf.spy_rows, perf.spy_feed = source, rows, feed
        perf.spy_error = error or (
            f"SPY cache is readable ({rows} bars in a 30-day probe window), but the "
            "bot has no closed trades and no priced cost rows, so there is no equity "
            "series to index the benchmark against yet"
        )
        return perf

    perf.start_day = days[0] - timedelta(days=1)
    perf.end_day = days[-1]
    points = [(perf.start_day, 100.0, START_CAPITAL_CENTS)]
    running = Decimal(START_CAPITAL_CENTS)
    for day in days:
        running += sum((t[1] for t in trades if t[0] == day), Decimal("0"))
        running -= sum((c[1] for c in costs if c[0] == day), Decimal("0"))
        index = float(running / Decimal(START_CAPITAL_CENTS) * 100)
        points.append((day, index, int(running)))
    perf.bot_points = points

    spy_points, source, error, rows, feed = _load_spy(perf.start_day, perf.end_day)
    (perf.spy_points, perf.spy_source, perf.spy_error, perf.spy_rows,
     perf.spy_feed) = (spy_points, source, error, rows, feed)
    # A cache full of bars with none in a two-day window that happens to
    # be a weekend is not a broken benchmark. Distinguishing the two is
    # the whole point: one needs fixing, the other needs Tuesday.
    perf.spy_window_too_short = bool(
        not spy_points and rows > 0 and error and "none inside" in error)
    return perf


# --------------------------------------------------------------------------
# 2. The candidate funnel
# --------------------------------------------------------------------------


#: What each feed IS, in words. The map keys are the machine names
#: stored in raw_events.source; a diagram node reading "edgar" tells a
#: reader who already knows what EDGAR is precisely nothing they did not
#: know, and tells everyone else nothing at all (owner-reported
#: 2026-08-11: "it just says edgar4 doesnt say what that means").
SOURCE_LABELS = {
    "edgar": "SEC filings (EDGAR)",
    "edgar_form4": "Insider trades (SEC Form 4)",
    "federal_register": "Government notices (Federal Register)",
    "clinicaltrials": "Drug trials (ClinicalTrials.gov)",
    "openfda": "FDA decisions (openFDA)",
    "alpaca_news": "Market news (Alpaca)",
    "alpaca": "Broker data (Alpaca)",
    "news": "News",
}


def source_label(name) -> str:
    """A feed's name in words, falling back to the machine name so a new
    feed appears rather than disappearing."""
    key = str(name or "").strip()
    return SOURCE_LABELS.get(key, key.replace("_", " ") or "unknown source")


def _last_seen(last_at, first_at=None) -> str:
    """When a drop reason last happened, in words that age.

    A bare count cannot say "this stopped". The owner read a wall of
    400 Bad Request errors as a live fault days after the bug behind
    them was fixed, because the page showed only how many there had
    ever been.
    """
    day = _as_date(last_at)
    if day is None:
        return ""
    age = (datetime.now(timezone.utc).date() - day).days
    when = ("last seen today" if age <= 0 else
            "last seen yesterday" if age == 1 else
            f"last seen {age} days ago")
    span = ""
    first = _as_date(first_at)
    if first is not None and first != day:
        span = f", first {first}"
    if age >= 2:
        when += " - NOT since, so this may be history rather than a live fault"
    return f"{when} ({day}{span})"


@dataclass
class Stage:
    key: str
    label: str
    count: int
    query: QueryResult
    drops: list = field(default_factory=list)   # [(reason, n, detail)]
    note: str = ""
    #: What this step DOES, in a sentence a non-developer can read. The
    #: stage labels are pipeline nouns; on their own they told the owner
    #: nothing about what was happening to their money.
    plain: str = ""
    #: How many candidates arrived here. `count` is how many left. Both
    #: are needed for the arithmetic to be checkable by eye, which is
    #: what "200% kept" destroyed.
    entered: int = 0
    #: Reasons split by whether they need attention. Painting normal
    #: attrition in error orange - and "order status: filled" with it -
    #: is what made this panel read as a wall of faults
    #: (owner-reported: "Funnel still has this is the orange errors?").
    #: [(reason, n, detail)] and optionally a 4th element, (href, label):
    #: the page that can actually clear this fault.
    faults: list = field(default_factory=list)
    #: Faults that have since RESOLVED. Same shape, shown as history so
    #: a cleared block cannot masquerade as a live one - nor vanish
    #: silently, which would make it indistinguishable from one that
    #: never happened.
    healed: list = field(default_factory=list)

    @property
    def left(self) -> int:
        return max(0, self.entered - self.count)


#: How far back the feed-health panel looks. Older than this is
#: history, not an alert - the panel had no window at all and so
#: alarmed about resolved failures indefinitely.
FEED_FAULT_WINDOW_DAYS = 3


@dataclass
class Funnel:
    stages: list
    blame: str = ""
    blame_stage: str = ""
    #: Feed health is a SEPARATE question from candidate attrition. Raw
    #: events and candidates are different populations from different
    #: tables, so dividing one by the other produced "200% kept".
    feed_events: int = 0
    feed_query: QueryResult | None = None
    feed_faults: list = field(default_factory=list)
    #: Faults the feed has since recovered from. Shown for the
    #: record, never under NEEDS ATTENTION.
    feed_healed: list = field(default_factory=list)


#: Risk-engine skip codes in English. The codes are what the risk engine
#: writes and what a developer needs; on their own they told the owner
#: nothing - "adverse_gap_assumption_exceeds_max_loss_per_position" is a
#: sentence with the verb removed. The code is still shown beside the
#: sentence, so nothing is lost and grep still works.
SKIP_LABELS = {
    "adverse_gap_assumption_exceeds_max_loss_per_position":
        "the overnight gap it could suffer was bigger than this account "
        "is allowed to lose on one position",
    "sector_cluster_would_exceed_max_correlated_cluster_pct":
        "too much of the account would have been riding on one sector",
    "conviction_below_floor":
        "the model was not confident enough to clear the current floor",
    "max_positions_reached":
        "the account already holds as many positions as it is allowed",
    "insufficient_buying_power":
        "there was not enough cash to take a position worth taking",
    "spread_too_wide":
        "the bid-ask spread would have eaten the expected move",
    "no_stop_price":
        "no stop could be placed, so the downside was unbounded",
    "catalyst_date_passed":
        "the catalyst had already happened by the time it was sized",
}


def _plain_skip(code: str) -> str:
    """A risk-engine skip code as a sentence, falling back to the code
    itself so a NEW reason appears rather than silently reading as
    blank."""
    key = str(code or "").strip()
    if key in SKIP_LABELS:
        return SKIP_LABELS[key]
    return key.replace("_", " ") or "no reason recorded"


def _grouped(db: Db, sql: str, params: tuple = ()) -> QueryResult:
    return db.q(sql, params)


def _ids(db: Db, sql: str, params: tuple = ()) -> tuple[set, QueryResult]:
    """(ids, result) - the first column of a query as a set of strings,
    plus the QueryResult so the panel can still print the exact SQL
    beside an empty stage (house rule 3).

    Attribution is done in Python over candidate ids rather than as a
    COUNT(*) per table, because a count cannot say WHICH candidate
    stopped where - and without that the stages are five unrelated
    numbers rather than a funnel."""
    res = db.q(sql, params)
    out = set()
    for row in res.rows:
        val = row[list(row.keys())[0]] if hasattr(row, "keys") else row[0]
        if val is not None:
            out.add(str(val))
    return out, res


#: A governor reason in plain English, with what to DO about it. The
#: raw values are code identifiers - "reconciliation_discrepancy_
#: unacknowledged" is exact and tells the owner nothing they can act on
#: ("Unsure what the reason for block actually is it needs to be
#: clearer"). The identifier is kept BESIDE the sentence, never instead
#: of it, so it stays greppable and the two can never disagree.
# (plain sentence, what to do, where to go).
#
# THE LINK IS A THIRD FIELD, NOT MARKUP IN THE SENTENCE. The advice text
# reaches the page through raw(), which escapes and redacts - stored text
# must never become markup, and that is right. So an anchor written into
# the sentence renders as literal &lt;a href=...&gt; on screen. Verified
# by rendering; it looked exactly like a bug in the page.
#
# A FOURTH FIELD CARRIES THE PAST TENSE. The sentence is written for a
# live block, so reusing it for a cleared one produces "spending was
# blocked, then resumed - a cost cross-check IS HOLDING spending", which
# contradicts itself in one line. Caught by reading the render.
GOVERNOR_REASONS = {
    "cap_exceeded": (
        "the monthly budget ran out",
        "Raise it on the Settings page, or wait for the month to roll "
        "over. The Cost page shows the cap in force and what has been "
        "spent against it.",
        ("/costs#cost-section", "Open the Cost page"),
        "the monthly budget had run out"),
    "reconciliation_discrepancy_unacknowledged": (
        "a cost cross-check is holding spending until someone confirms it",
        "This is NOT out of money. The daily check compared what the bot "
        "recorded against what Anthropic billed, they differed, and "
        "nothing may be spent until that is acknowledged. It takes one "
        "click, changes no figure anywhere, and only records that a "
        "human looked.",
        # THE BUTTON IS ON THE COST PAGE, NOT MAINTENANCE. This used to
        # say "Open Maintenance and acknowledge it", and /maintenance has
        # no acknowledge form on it - verified by rendering both. The
        # owner followed the instruction, found nothing to click, and
        # reported the block as unfixed. It was: the advice was wrong.
        ("/costs#cost-unacked", "Acknowledge it on the Cost page"),
        "a cost cross-check was holding spending until someone confirmed it"),
    "unpriced_cost_rows": (
        "a cost row could not be priced, so spending stopped",
        "This is a code fault, not a budget one: an API response arrived "
        "with a shape the pricing table does not know. Send the Cost & "
        "pricing bundle.",
        ("/logs#log-bundle", "Collect the Cost & pricing log"),
        "a cost row could not be priced"),
}

_UNKNOWN_REASON = (
    "This reason has no plain-English explanation recorded yet - send "
    "the Everything bundle so it can be diagnosed.",
    ("/logs#log-bundle", "Collect the Everything log"),
    None)


def governor_reason_past(reason: str) -> str:
    """The same block described as something that HAPPENED. See the note
    above GOVERNOR_REASONS."""
    entry = GOVERNOR_REASONS.get(str(reason))
    return (entry[3] if entry and entry[3] else str(reason))


def explain_governor_reason(reason: str):
    """(plain sentence, what to do). Unknown reasons pass through
    unchanged rather than being guessed at - a confident wrong
    explanation is worse than the identifier."""
    entry = GOVERNOR_REASONS.get(str(reason))
    if entry is None:
        return str(reason), _UNKNOWN_REASON[0]
    return entry[0], entry[1]


def governor_gate_still_closed(db, reason: str, last_deny_at, last_allow_at) -> bool:
    """Is the gate that produced this denial STILL shut?

    OWNER-REPORTED: the funnel said "125 spending was blocked ... last
    seen yesterday", the advice said acknowledge it, and the Cost page
    had nothing to acknowledge - because the pause had already been
    cleared. Reproduced:

        reconciliation acknowledged: True
          governor actually blocking right now : False
          funnel shows a NEEDS ATTENTION block : True
          Cost page offers an acknowledge form : False

    A count of every denial ever recorded is history. Painting it as
    current state sends the owner to fix something already fixed, and -
    far worse - trains them to ignore the block, which is the one panel
    that must be believed when it IS live.

    Each gate is asked the question it can actually answer, from rows
    that exist. Nothing is inferred from the age of the denial.
    """
    if reason == "reconciliation_discrepancy_unacknowledged":
        # Authoritative: the same condition cost.tracker gates on.
        q = db.q("SELECT COUNT(*) AS n FROM cost_reconciliation_events "
                 "WHERE action_taken = 'scheduled_paused' "
                 "AND acknowledged_at IS NULL")
        return bool(q.rows and int(q.rows[0]["n"]))
    if reason == "unpriced_cost_rows":
        q = db.q("SELECT COUNT(*) AS n FROM cost_events "
                 "WHERE priced_cents IS NULL")
        return bool(q.rows and int(q.rows[0]["n"]))
    # Everything else, including cap_exceeded and any gate added later:
    # the governor authorising ANY spend after the last denial means the
    # gate opened. Where there has been no authorisation at all, the
    # denial stands - silence is not evidence of recovery.
    if not last_allow_at:
        return True
    return str(last_deny_at) >= str(last_allow_at)


def governor_reason_link(reason: str):
    """(href, label) for the page that can actually resolve this block,
    or None. Kept apart from the sentence so the sentence can stay
    escaped text - see the note above GOVERNOR_REASONS."""
    entry = GOVERNOR_REASONS.get(str(reason))
    return entry[2] if entry else _UNKNOWN_REASON[1]


def funnel(db: Db) -> Funnel:
    """Where every candidate the bot has ever built ended up.

    REBUILT 2026-08-11. Owner-reported: "Recreate the funnel section as
    its very confusing on what its actually doing and it is still error
    400." Three separate defects were behind that, and none of them was
    wording:

    ONE POPULATION, NARROWING. Stages used to be independent COUNT(*)s
    over different tables, so "raw events fetched 1" was followed by
    "candidates built 2 - 200% kept". Every stage is now a SUBSET of the
    one above it, computed over candidate ids, so a count can never
    exceed the stage above and the arithmetic checks by eye.

    REASONS ATTRIBUTED TO THE CANDIDATES THAT ACTUALLY LEFT. A stage
    reading "100% kept" listed a governor denial underneath it, and a
    stage that lost one candidate listed reasons summing to four.
    Reasons are now counted only over the candidates that left AT THAT
    STEP, and anything left over is shown as an explicit unexplained
    residual rather than hidden.

    FAULTS ARE NOT ATTRITION. Feed errors and governor denials mean
    something is wrong; a model declining a trade means the system is
    working. They were the same shade of orange, and "order status:
    filled" - an outright success - was orange too. They are now
    different lists with different colours, and feed health moved out of
    the funnel entirely because raw events are not candidates.
    """
    stages: list[Stage] = []

    # --- feed health: upstream of the funnel, and a different question
    raw_q = db.count("raw_events")
    # HAS IT RECOVERED? That is the question the panel was not asking.
    # It listed the last 20 errors EVER, with no window and no check for
    # whether the feed had read successfully since - so a Form 4 failure
    # from a rate-limit episode days ago sat under "NEEDS ATTENTION"
    # permanently, and the owner could not tell a live fault from a
    # healed one ("are these all old errors not removing from the screen
    # or a genuine issue").
    #
    # A fault is only ATTENTION-worthy while the feed has not read
    # anything since it. That is checkable, from rows that exist.
    err_q = db.q(
        "SELECT source, attempted_at, error_text FROM raw_events_errors "
        "WHERE attempted_at >= ? ORDER BY attempted_at DESC LIMIT 20",
        ((datetime.now(timezone.utc) - timedelta(days=FEED_FAULT_WINDOW_DAYS)
          ).isoformat(),))
    feed_faults, feed_healed = [], []
    for r in err_q.rows:
        source, when = str(r["source"]), str(r["attempted_at"])
        ok_since = db.q(
            "SELECT COUNT(*) AS n FROM raw_events WHERE source = ? "
            "AND fetched_at > ?", (source, when))
        n_ok = int(ok_since.rows[0]["n"]) if ok_since.rows else 0
        entry = (f"{source_label(source)} could not be read", 1,
                 str(r["error_text"] or ""))
        if n_ok > 0:
            feed_healed.append(
                (f"{source_label(source)} failed, then recovered", 1,
                 f"{n_ok} successful read(s) since {when[:16]} - "
                 "resolved, shown for the record"))
        else:
            feed_faults.append(entry)

    # --- the funnel proper, over candidate ids
    cand_ids, cand_q = _ids(db, "SELECT id FROM candidates")
    researched_ids, researched_q = _ids(
        db, "SELECT DISTINCT candidate_id FROM research_calls "
            "WHERE skipped_reason IS NULL")
    view_ids, view_q = _ids(
        db, "SELECT candidate_id FROM research_views WHERE direction != 'no_trade'")
    trade_ids, trade_q = _ids(
        db, "SELECT candidate_id FROM risk_decisions WHERE action = 'trade'")
    order_ids, order_q = _ids(db, "SELECT DISTINCT decision_id FROM orders")

    # Each stage intersects the one above, so the chain cannot widen even
    # if a downstream table holds a row for a candidate that never
    # reached it (a stale row, a hand-edited database, a bug).
    s_cand = cand_ids
    s_res = s_cand & researched_ids
    s_view = s_res & view_ids
    s_trade = s_view & trade_ids
    s_order = s_trade & order_ids

    stages.append(Stage(
        "candidates", "Candidates built", len(s_cand), cand_q,
        entered=len(s_cand),
        plain="A dated, tradeable event with a ticker attached. Raw feed "
              "items that were not one of these never became a candidate "
              "and are not counted anywhere below.",
    ))

    # --- researched: reasons drawn only from candidates that left here
    left_res = s_cand - s_res
    skip_q = _grouped(db,
        "SELECT candidate_id, skipped_reason, called_at FROM research_calls "
        "WHERE skipped_reason IS NOT NULL")
    res_reasons: dict[str, list] = {}
    for r in skip_q.rows:
        if str(r["candidate_id"]) not in left_res:
            continue
        key = f"research skipped: {r['skipped_reason']}"
        res_reasons.setdefault(key, []).append(r["called_at"])
    drops = [(k, len(v), _last_seen(max(v), min(v)))
             for k, v in sorted(res_reasons.items(), key=lambda kv: -len(kv[1]))]
    # A governor denial is a FAULT, not attrition: the bot wanted to
    # research and was not allowed to spend. It is also not attributable
    # to a candidate, so it can never sit in the drop column.
    gov_q = _grouped(db,
        "SELECT reason, COUNT(*) n, MIN(at) first_at, MAX(at) last_at "
        "FROM cost_governor_events WHERE decision = 'deny' "
        "GROUP BY reason ORDER BY n DESC")
    allow_q = db.q("SELECT MAX(at) AS last_allow FROM cost_governor_events "
                   "WHERE decision != 'deny'")
    last_allow_at = allow_q.rows[0]["last_allow"] if allow_q.rows else None
    gov_faults, gov_healed = [], []
    for r in gov_q.rows:
        plain, todo = explain_governor_reason(r["reason"])
        seen = _last_seen(r["last_at"], r["first_at"])
        if governor_gate_still_closed(db, r["reason"], r["last_at"],
                                      last_allow_at):
            gov_faults.append((
                f"spending was blocked \u2014 {plain}", r["n"],
                f"{todo}  [{r['reason']}]  " + seen,
                governor_reason_link(r["reason"])))
        else:
            # RESOLVED. Kept visible, because a fault that vanishes
            # silently is indistinguishable from one that never
            # happened - but out of the block that demands action.
            gov_healed.append((
                "spending was blocked, then resumed \u2014 "
                + governor_reason_past(r["reason"]), r["n"],
                f"This is history, not something to act on: the check "
                f"that stopped spending has since cleared"
                + (f" and the governor has authorised spend since "
                   f"{str(last_allow_at)[:16]}" if last_allow_at else "")
                + f".  [{r['reason']}]  " + seen))
    stages.append(Stage(
        "researched", "Researched by the model", len(s_res), researched_q,
        drops=drops, faults=gov_faults, healed=gov_healed,
        entered=len(s_cand),
        plain="Claude read the candidate and everything the feeds hold on "
              "it. This is the only step that costs money, so it is also "
              "the step the cost governor can block.",
    ))

    # --- directional view
    left_view = s_res - s_view
    nt_q = _grouped(db,
        "SELECT candidate_id, priced_in FROM research_views "
        "WHERE direction = 'no_trade'")
    n_priced_in = sum(1 for r in nt_q.rows
                      if str(r["candidate_id"]) in left_view and r["priced_in"])
    n_no_trade = sum(1 for r in nt_q.rows
                     if str(r["candidate_id"]) in left_view and not r["priced_in"])
    drops = []
    if n_priced_in:
        drops.append(("the model judged the move already priced in", n_priced_in,
                      "the event is real but the market has had it already"))
    if n_no_trade:
        drops.append(("the model saw no tradeable edge", n_no_trade, ""))
    stages.append(Stage(
        "views", "Model saw a trade worth making", len(s_view), view_q,
        drops=drops, entered=len(s_res),
        plain="Claude returned a direction, a conviction and what would "
              "prove it wrong. A candidate it declined stops here - and "
              "the refusals page then scores what it went on to do.",
    ))

    # --- risk engine
    left_trade = s_view - s_trade
    skip_rows = db.q(
        "SELECT candidate_id, skip_reasons FROM risk_decisions WHERE action = 'skip'")
    reason_counts: dict[str, int] = {}
    for row in skip_rows.rows:
        if str(row["candidate_id"]) not in left_trade:
            continue
        for reason in jload(row["skip_reasons"], []) or ["(unparseable skip_reasons)"]:
            reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + 1
    drops = [(_plain_skip(k), v, k) for k, v in
             sorted(reason_counts.items(), key=lambda kv: -kv[1])]
    bind_q = _grouped(db,
        "SELECT rule_name, bound_type, COUNT(*) n FROM limit_applications "
        "WHERE binding = 1 GROUP BY rule_name, bound_type ORDER BY n DESC")
    stages.append(Stage(
        "proposed", "Risk engine approved a trade", len(s_trade), trade_q,
        drops=drops, entered=len(s_view),
        plain="Deterministic code, never the model. It decides whether to "
              "trade at all, how large, and where the stop sits. It can "
              "only ever shrink what the model asked for.",
        note="Limits that bound at least once: " + (", ".join(
            f"{r['rule_name']} ({r['bound_type']}) x{r['n']}"
            for r in bind_q.rows) or "none recorded"),
    ))

    # --- orders
    stranded = sorted(s_trade - s_order)
    drops = []
    if stranded:
        drops.append(("approved but no order was recorded", len(stranded),
                      "execution never submitted it, or crashed before "
                      "writing the row - this one IS a fault"))
    stages.append(Stage(
        "orders", "Order placed at the broker", len(s_order), order_q,
        drops=drops, faults=[d for d in drops], entered=len(s_trade),
        plain="An order actually sent to Alpaca, with its stop resting at "
              "the broker. What happened to it afterwards is on the "
              "Decisions page.",
    ))

    blame, blame_stage = "", ""
    if stages[-1].count == 0:
        # WHERE DOES IT STOP? The first stage that lost everything it was
        # given. Now that the chain narrows by construction, that is
        # simply the first stage with entered > 0 and count == 0 - and if
        # nothing ever entered at all, the feeds are the answer.
        if not s_cand:
            blame_stage = "candidates"
            blame = ("No orders have been placed, and no candidate has been "
                     "built yet: nothing has reached the pipeline at all. "
                     "That is a question about the feeds, not the strategy.")
            if feed_faults:
                blame += (f" {len(feed_faults)} feed(s) failed to read - "
                          "listed under Feed health.")
        else:
            st = next((s for s in stages if s.entered > 0 and s.count == 0),
                      stages[-1])
            top = max(st.drops, key=lambda d: d[1], default=None)
            blame_stage = st.key
            blame = (
                f'No orders have been placed. Every candidate stops at '
                f'"{st.label}": {st.entered} arrived, none got through.')
            if top:
                blame += f" Most common reason: {top[0]} ({top[1]} of them)."
            else:
                blame += (
                    " Nothing was recorded about why, and that is itself the "
                    "finding: the step rejected everything and explained "
                    "nothing.")
    return Funnel(stages=stages, blame=blame, blame_stage=blame_stage,
                  feed_events=int(raw_q.scalar(0) or 0), feed_query=raw_q,
                  feed_faults=feed_faults)


# --------------------------------------------------------------------------
# 3. Cost
# --------------------------------------------------------------------------


@dataclass
class CostPanel:
    as_of: date
    month_prefix: str
    days_elapsed: int
    scheduled_mtd_q: QueryResult
    manual_mtd_q: QueryResult
    scheduled_mtd_cents: Decimal
    manual_mtd_cents: Decimal
    scheduled_samples: int
    manual_samples: int
    lifetime_manual_cents: Decimal
    lifetime_manual_budget_cents: Decimal
    lifetime_scheduled_cents: Decimal
    ledger_crosscheck: str
    unpriced_q: QueryResult
    reconciliation_q: QueryResult
    unacked_q: QueryResult
    governor_q: QueryResult
    billed_q: QueryResult
    billed_days: int
    billed_total_cents: Decimal
    rates_stale: bool
    rates_verified_on: str
    base_cap_cents: Decimal
    max_cap_cents: Decimal
    manual_month_cap_cents: Decimal
    check_failed_q: QueryResult
    last_reconciled_ok: str | None
    reconcile_gap_days: int | None
    admin_key_present: bool
    owner_budget_usd: Decimal | None = None
    #: (seed, observed, sample) tokens per web search. The seed
    #: is a constant from ONE call; observed is measured from the
    #: raw usage of every turn that actually searched.
    search_tokens_seed: int = 0
    search_tokens_observed: int | None = None
    search_tokens_sample: int = 0
    #: Which bound set the cap above: "_owner_set", "_hard_capped", or
    #: "" for the base. The page must name the bound, not just the number.
    cap_source: str = ""
    #: Non-None when the credentials file could not be READ. A read
    #: failure must never be displayed as "nothing was entered".
    creds_error: str | None = None


def cost_panel(db: Db, as_of: date | None = None) -> CostPanel:
    from catalyst.cost import governor as gov
    from catalyst.cost import ledger as led
    from catalyst.cost.pricing import RATES_VERIFIED_ON, rates_stale

    as_of = as_of or datetime.now(timezone.utc).date()
    month = as_of.strftime("%Y-%m")

    mtd_sql = (
        "SELECT COUNT(*) n, COALESCE(SUM(CAST(priced_cents AS REAL)), 0) cents "
        "FROM cost_events WHERE kind = ? AND strftime('%Y-%m', priced_at) = ? "
        "AND priced_cents IS NOT NULL"
    )
    sched_q = db.q(mtd_sql, ("scheduled", month))
    manual_q = db.q(mtd_sql, ("manual", month))

    def _sum(kind: str, month_only: bool) -> tuple[Decimal, int]:
        sql = ("SELECT priced_cents FROM cost_events WHERE kind = ? "
               "AND priced_cents IS NOT NULL")
        params: tuple = (kind,)
        if month_only:
            sql += " AND strftime('%Y-%m', priced_at) = ?"
            params = (kind, month)
        res = db.q(sql, params)
        return sum((_dec(r[0]) for r in res.rows), Decimal("0")), res.row_count

    sched_cents, sched_n = _sum("scheduled", True)
    manual_cents, manual_n = _sum("manual", True)
    life_manual, _ = _sum("manual", False)
    life_sched, _ = _sum("scheduled", False)

    # Cross-check the panel's own arithmetic against cost/ledger.py, the
    # module that owns the number. Disagreement is displayed, not hidden.
    crosscheck = "not run (no readable connection)"
    if db.conn is not None:
        try:
            ledger_value = led.month_to_date_cents("scheduled", db.conn, as_of)
            crosscheck = (
                f"agrees with cost.ledger.month_to_date_cents('scheduled') = "
                f"{ledger_value} cents"
                if ledger_value == sched_cents else
                f"DISAGREES with cost.ledger.month_to_date_cents('scheduled') = "
                f"{ledger_value} cents vs panel {sched_cents} cents"
            )
        except Exception as exc:
            crosscheck = f"cross-check failed: {type(exc).__name__}: {exc}"

    unpriced_q = db.q(
        "SELECT id, model, kind, component, priced_at, raw_usage_json FROM cost_events "
        "WHERE priced_cents IS NULL ORDER BY priced_at DESC LIMIT 25"
    )
    recon_q = db.q(
        "SELECT id, target_date, kind, component, local_total_cents, "
        "cost_api_total_cents, discrepancy_cents, threshold_cents, api_record_count, "
        "action_taken, acknowledged_by, acknowledged_at, api_raw_response "
        "FROM cost_reconciliation_events ORDER BY target_date DESC LIMIT 30"
    )
    unacked_q = db.q(
        "SELECT id, target_date, kind, component, local_total_cents, "
        "cost_api_total_cents, discrepancy_cents, threshold_cents, api_record_count, "
        "api_raw_response FROM cost_reconciliation_events "
        "WHERE action_taken = 'scheduled_paused' AND acknowledged_at IS NULL "
        "ORDER BY target_date DESC"
    )
    governor_q = db.q(
        "SELECT at, requested_kind, decision, reason, estimate_cents, cap_cents, cycle_id "
        "FROM cost_governor_events ORDER BY at DESC LIMIT 25"
    )
    billed_q = db.q(
        "SELECT target_date, cost_api_total_cents FROM cost_reconciliation_events "
        "WHERE action_taken != 'check_failed' "
        "AND (action_taken != 'scheduled_paused' OR acknowledged_at IS NOT NULL) "
        "ORDER BY target_date DESC LIMIT 60"
    )
    billed_total = sum((_dec(r["cost_api_total_cents"]) for r in billed_q.rows), Decimal("0"))

    # Staleness of the nightly bill check (cost-audit F2): a dark
    # instrument must be visibly dark. check_failed rows carry the raw
    # error; the gap is measured against the most recent SUCCESSFUL day.
    check_failed_q = db.q(
        "SELECT target_date, api_raw_response FROM cost_reconciliation_events "
        "WHERE action_taken = 'check_failed' ORDER BY target_date DESC LIMIT 10"
    )
    last_ok_q = db.q(
        "SELECT MAX(target_date) d FROM cost_reconciliation_events "
        "WHERE action_taken != 'check_failed'"
    )
    last_reconciled_ok = last_ok_q.rows[0]["d"] if last_ok_q.rows else None
    reconcile_gap_days = None
    if last_reconciled_ok:
        yesterday = as_of - timedelta(days=1)
        reconcile_gap_days = (yesterday - date.fromisoformat(last_reconciled_ok)).days
    admin_key_present = False
    owner_budget_usd = None
    creds_error = None
    try:
        from catalyst.setup.credentials import load_credentials
        _creds = load_credentials()
        admin_key_present = bool(_creds.anthropic_admin_key)
        raw_budget = (_creds.settings or {}).get("monthly_budget_usd")
        if raw_budget is not None:
            owner_budget_usd = Decimal(str(raw_budget))
    except Exception as exc:  # noqa: BLE001 - display only, never fatal
        creds_error = f"{type(exc).__name__}: {exc}"

    # THE CAP THE GOVERNOR ACTUALLY USES, from the governor itself. This
    # used to be gov.BASE_CAP_CENTS, so an owner who raised their budget
    # to $20 still read "$0.00 of $5.00" everywhere and concluded the
    # setting did nothing. It had in fact taken effect - only the screen
    # was wrong, which is the worse of the two failures.
    owner_cap_cents = (owner_budget_usd * 100) if owner_budget_usd is not None else None
    try:
        effective_cap, cap_source = gov.scheduled_cap_cents(
            db.conn, gov.DEFAULT_GOVERNOR_PROFIT_SHARE, as_of, owner_cap_cents)
    except Exception:  # noqa: BLE001 - fall back to the documented base
        effective_cap, cap_source = gov.BASE_CAP_CENTS, "_unreadable"

    # What a web search ACTUALLY costs in input tokens, beside the
    # seed constant it was estimated at. The estimate only ever
    # raises itself; lowering the seed is a human decision, and
    # this is the number that decision needs.
    from catalyst.research.boundary import (
        INPUT_TOKENS_PER_SEARCH as _seed_tokens,
        observed_tokens_per_search,
    )
    try:
        _observed_tokens, _observed_sample = \
            observed_tokens_per_search(db.conn)
    except Exception:  # noqa: BLE001 - provenance never gates a page
        _observed_tokens, _observed_sample = None, 0

    return CostPanel(
        as_of=as_of, month_prefix=month, days_elapsed=as_of.day,
        scheduled_mtd_q=sched_q, manual_mtd_q=manual_q,
        scheduled_mtd_cents=sched_cents, manual_mtd_cents=manual_cents,
        scheduled_samples=sched_n, manual_samples=manual_n,
        lifetime_manual_cents=life_manual,
        lifetime_manual_budget_cents=gov.MANUAL_LIFETIME_BUDGET_CENTS,
        lifetime_scheduled_cents=life_sched,
        ledger_crosscheck=crosscheck,
        unpriced_q=unpriced_q, reconciliation_q=recon_q, unacked_q=unacked_q,
        governor_q=governor_q, billed_q=billed_q,
        billed_days=billed_q.row_count, billed_total_cents=billed_total,
        rates_stale=rates_stale(as_of), rates_verified_on=RATES_VERIFIED_ON,
        base_cap_cents=effective_cap, cap_source=cap_source,
        max_cap_cents=gov.GOVERNOR_MAX_CAP_CENTS,
        manual_month_cap_cents=gov.MANUAL_SPEND_CAP_CENTS_PER_MONTH,
        check_failed_q=check_failed_q, last_reconciled_ok=last_reconciled_ok,
        reconcile_gap_days=reconcile_gap_days,
        admin_key_present=admin_key_present,
        owner_budget_usd=owner_budget_usd,
        search_tokens_seed=_seed_tokens,
        search_tokens_observed=_observed_tokens,
        search_tokens_sample=_observed_sample,
        creds_error=creds_error,
    )


# --------------------------------------------------------------------------
# 4. Decision traces
# --------------------------------------------------------------------------


def decision_list(db: Db, limit: int = 200) -> QueryResult:
    return db.q(
        "SELECT c.id, c.ticker, c.catalyst_type, c.catalyst_date, c.sector, "
        "       c.discovered_at, "
        "       v.direction, v.conviction, v.priced_in, "
        "       (SELECT d.action FROM risk_decisions d WHERE d.candidate_id = c.id "
        "        ORDER BY d.decided_at DESC LIMIT 1) AS action, "
        "       (SELECT COUNT(*) FROM orders o "
        "        WHERE o.decision_id = c.id) AS n_orders, "
        "       (SELECT COUNT(*) FROM research_calls rc WHERE rc.candidate_id = c.id "
        "        AND rc.skipped_reason IS NULL) AS n_calls "
        "FROM candidates c LEFT JOIN research_views v ON v.candidate_id = c.id "
        "ORDER BY c.discovered_at DESC LIMIT ?",
        (limit,),
    )


@dataclass
class Trace:
    candidate_id: str
    candidate_q: QueryResult
    raw_events_q: QueryResult
    source_event_ids: list
    calls_q: QueryResult
    turns_by_call: dict
    view_q: QueryResult
    decisions_q: QueryResult
    limits_by_decision: dict
    orders_q: QueryResult
    fills_by_order: dict
    refusal_q: QueryResult
    positions: list
    closed_q: QueryResult
    stops_q: QueryResult
    reviews_q: QueryResult
    evidence: "EvidenceChain"


@dataclass
class EvidenceChain:
    available: bool
    table: str
    columns: list
    query: QueryResult | None
    reason: str = ""


def evidence_chain(db: Db, candidate_id: str) -> EvidenceChain:
    """Stage 5a's graph_assertions may or may not be merged when this
    runs, so the table is feature-detected rather than assumed. Columns
    are read from PRAGMA table_info and rendered generically — guessing
    a column name that does not exist is how a panel silently blanks."""
    table = "graph_assertions"
    if not db.table_exists(table):
        return EvidenceChain(
            False, table, [], None,
            reason=f"table {table!r} is not in this database - stage 5a's evidence "
                   "graph is either not merged or has never written a row. "
                   "Feature-detected, not assumed.",
        )
    cols = db.columns(table)
    key = next((c for c in ("candidate_id", "subject_id", "entity_id") if c in cols), None)
    if key:
        res = db.q(f"SELECT * FROM {table} WHERE {key} = ? LIMIT 200", (candidate_id,))
    else:
        res = db.q(f"SELECT * FROM {table} LIMIT 200")
    return EvidenceChain(True, table, cols, res)


def broker_equity(db: Db) -> QueryResult:
    """The most recent equity Alpaca itself reported. Display only.

    Kept separate from the dashboard's own net-value arithmetic on
    purpose: one is what the broker says the account is worth, the
    other is what it is worth after the API bill and counting only
    banked profit. Showing one without the other invites the reader to
    assume they should match.
    """
    return db.q(
        "SELECT day, taken_at, equity_usd, settled_cash_usd, positions_notional "
        "FROM equity_snapshots WHERE source = 'broker_read' "
        "ORDER BY day DESC, taken_at DESC LIMIT 1")


def evidence_graph(db: Db, ticker: str) -> QueryResult:
    """Assertions around one company, with entity NAMES resolved.

    Display-only. graph_assertions stores entity ids; rendering those
    raw shows the reader "company:GBFH" where a name belongs, which is
    not evidence anybody can check. Both graph tables are
    feature-detected exactly as evidence_chain does - stage 5a may not
    be present in a given database, and guessing is how a panel blanks.
    """
    if not (db.table_exists("graph_assertions")
            and db.table_exists("graph_entities")):
        return QueryResult("graph tables absent", (), [],
                           "graph_entities/graph_assertions not in this database")
    key = f"company:{(ticker or '').strip().upper()}"
    return db.q(
        """SELECT a.predicate, a.object_date, a.source_class, a.source_ref,
                  a.reliability, a.asserted_at,
                  s.display_name AS subject_label, s.kind AS subject_kind,
                  o.display_name AS object_label, o.kind AS object_kind
           FROM graph_assertions a
           JOIN graph_entities s ON s.id = a.subject_entity_id
           LEFT JOIN graph_entities o ON o.id = a.object_entity_id
           WHERE s.canonical_key = ? OR o.canonical_key = ?
           ORDER BY a.asserted_at, a.id LIMIT 40""",
        (key, key))


def decision_trace(db: Db, candidate_id: str) -> Trace:
    candidate_q = db.q("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
    source_ids = []
    if candidate_q.rows:
        source_ids = jload(candidate_q.rows[0]["source_event_ids"], []) or []

    if source_ids:
        marks = ",".join("?" * len(source_ids))
        raw_events_q = db.q(
            f"SELECT source, source_id, fetched_at, payload_raw FROM raw_events "
            f"WHERE source_id IN ({marks})",
            tuple(str(s) for s in source_ids),
        )
    else:
        raw_events_q = QueryResult(
            "SELECT ... FROM raw_events WHERE source_id IN (<candidates.source_event_ids>)",
            (), [], None,
        )

    calls_q = db.q(
        "SELECT * FROM research_calls WHERE candidate_id = ? ORDER BY called_at",
        (candidate_id,),
    )
    turns_by_call = {}
    for row in calls_q.rows:
        turns_by_call[row["id"]] = db.q(
            "SELECT turn_index, stop_reason, raw_response, usage_raw FROM research_call_turns "
            "WHERE call_id = ? ORDER BY turn_index",
            (row["id"],),
        )

    view_q = db.q("SELECT * FROM research_views WHERE candidate_id = ?", (candidate_id,))
    decisions_q = db.q(
        "SELECT * FROM risk_decisions WHERE candidate_id = ? ORDER BY decided_at",
        (candidate_id,),
    )
    limits_by_decision = {}
    for row in decisions_q.rows:
        limits_by_decision[row["id"]] = db.q(
            "SELECT rule_name, bound_type, requested_value, bound_value, binding "
            "FROM limit_applications WHERE decision_id = ? "
            "ORDER BY binding DESC, rule_name",
            (row["id"],),
        )

    # orders.decision_id holds the candidate id (production semantics
    # since the stage-5 FK correction), so the trace fetches directly
    orders_q = db.q(
        "SELECT * FROM orders WHERE decision_id = ? ORDER BY submitted_at",
        (candidate_id,),
    )

    fills_by_order = {}
    for row in orders_q.rows:
        fills_by_order[row["id"]] = db.q(
            "SELECT price, qty, filled_at, broker_reported_price, modeled_slippage "
            "FROM fills WHERE order_id = ? ORDER BY filled_at",
            (row["id"],),
        )

    refusal_q = db.q(
        "SELECT * FROM refusals WHERE candidate_id = ? ORDER BY refused_at",
        (candidate_id,),
    )

    order_ids = {r["id"] for r in orders_q.rows}
    pos_q = db.q("SELECT * FROM positions")
    positions = [
        dict(r) for r in pos_q.rows
        if order_ids & set(str(x) for x in (jload(r["entry_order_ids"], []) or []))
    ]
    position_ids = [p["id"] for p in positions]
    if position_ids:
        marks = ",".join("?" * len(position_ids))
        closed_q = db.q(
            f"SELECT * FROM closed_trades WHERE position_id IN ({marks})",
            tuple(position_ids),
        )
        stops_q = db.q(
            f"SELECT * FROM stop_confirmations WHERE position_id IN ({marks}) "
            "ORDER BY checked_at DESC LIMIT 20",
            tuple(position_ids),
        )
    else:
        closed_q = QueryResult(
            "SELECT * FROM closed_trades WHERE position_id IN "
            "(<positions whose entry_order_ids include this candidate's orders>)",
            (), [], None,
        )
        stops_q = QueryResult(
            "SELECT * FROM stop_confirmations WHERE position_id IN (<same>)", (), [], None,
        )

    # Every time the model was asked "does this thesis still hold?", in
    # order. A review that said hold is as much a part of the story as
    # one that closed the position - showing only the acting reviews
    # would make the model look decisive in hindsight.
    if position_ids:
        marks = ",".join("?" * len(position_ids))
        reviews_q = db.q(
            f"SELECT * FROM position_reviews WHERE position_id IN ({marks}) "
            "ORDER BY reviewed_at",
            tuple(position_ids),
        )
    else:
        reviews_q = QueryResult(
            "SELECT * FROM position_reviews WHERE position_id IN "
            "(<positions whose entry_order_ids include this candidate's orders>)",
            (), [], None,
        )

    return Trace(
        candidate_id=candidate_id, candidate_q=candidate_q, raw_events_q=raw_events_q,
        source_event_ids=source_ids, calls_q=calls_q, turns_by_call=turns_by_call,
        view_q=view_q, decisions_q=decisions_q, limits_by_decision=limits_by_decision,
        orders_q=orders_q, fills_by_order=fills_by_order, refusal_q=refusal_q,
        positions=positions, closed_q=closed_q, stops_q=stops_q,
        reviews_q=reviews_q,
        evidence=evidence_chain(db, candidate_id),
    )


# --------------------------------------------------------------------------
# 5. Refusals
# --------------------------------------------------------------------------


@dataclass
class Refusals:
    query: QueryResult
    n_total: int
    n_scored: int
    mean_outcome_return: float | None
    n_positive: int


def refusals(db: Db, limit: int = 200) -> Refusals:
    res = db.q(
        "SELECT r.decision_id, r.candidate_id, r.price_at_refusal, r.refused_at, "
        "       r.scored_at, r.outcome_price, r.outcome_return, "
        "       c.ticker, c.catalyst_type, d.skip_reasons "
        "FROM refusals r "
        "LEFT JOIN candidates c ON c.id = r.candidate_id "
        "LEFT JOIN risk_decisions d ON d.id = r.decision_id "
        "ORDER BY r.refused_at DESC LIMIT ?",
        (limit,),
    )
    scored = [_dec(r["outcome_return"], None) for r in res.rows if r["scored_at"]]
    scored = [s for s in scored if s is not None]
    mean = float(sum(scored) / len(scored)) if scored else None
    return Refusals(
        query=res, n_total=res.row_count, n_scored=len(scored),
        mean_outcome_return=mean, n_positive=sum(1 for s in scored if s > 0),
    )


# --------------------------------------------------------------------------
# 6. Logs
# --------------------------------------------------------------------------


LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


@dataclass
class Logs:
    available: bool
    query: QueryResult
    components: list
    levels: list
    filters: dict
    reason: str = ""


#: A page of logs, and the most a single page may ever ask sqlite for.
#: LIMIT -1 means "no limit" in sqlite, so an unclamped negative from the
#: query string would try to render the whole table into one page.
DEFAULT_LOG_LIMIT = 200
MAX_LOG_LIMIT = 2000


def _log_limit(value) -> int:
    """The limit box is a free-text query parameter: ?limit=abc reached
    int() and 500ed the whole page (stage-8 stress). A filter value that
    makes no sense falls back to the default rather than taking the
    dashboard down with it."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_LOG_LIMIT
    return max(1, min(parsed, MAX_LOG_LIMIT))


def logs(db: Db, level: str = "", component: str = "", q: str = "",
         since: str = "", until: str = "", limit=DEFAULT_LOG_LIMIT) -> Logs:
    limit = _log_limit(limit)
    filters = {"level": level, "component": component, "q": q,
               "since": since, "until": until, "limit": limit}
    if not db.table_exists("logs"):
        return Logs(
            False,
            QueryResult("SELECT ... FROM logs", (), [], "table 'logs' does not exist"),
            [], [], filters,
            reason="The logs table is not in this database. Its DDL lives in "
                   "catalyst/dashboard/schema_logs.sql and is folded into "
                   "storage/schema.sql by the coordinating session - "
                   "ui-designer does not edit the shared schema.",
        )
    where, params = [], []
    if level:
        where.append("level = ?")
        params.append(level)
    if component:
        where.append("component = ?")
        params.append(component)
    if q:
        where.append("(message LIKE ? OR COALESCE(traceback_text,'') LIKE ? "
                     "OR COALESCE(context_json,'') LIKE ?)")
        params += [f"%{q}%"] * 3
    if since:
        where.append("ts >= ?")
        params.append(since)
    if until:
        where.append("ts <= ?")
        params.append(until)
    sql = (
        "SELECT id, ts, level, component, message, cycle_id, candidate_id, "
        "traceback_text, context_json FROM logs"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY ts DESC, id DESC LIMIT ?"
    )
    params.append(int(limit))
    res = db.q(sql, tuple(params))
    comps = [r[0] for r in db.q(
        "SELECT DISTINCT component FROM logs ORDER BY component").rows]
    lvls = [r[0] for r in db.q("SELECT DISTINCT level FROM logs ORDER BY level").rows]
    return Logs(True, res, comps, lvls, filters)


# --------------------------------------------------------------------------
# 7. Alerts / operational state for the overview
# --------------------------------------------------------------------------


@dataclass
class Alerts:
    items: list           # [(severity, text, detail)]
    kill_q: QueryResult
    unprotected_q: QueryResult
    adaptive_q: QueryResult


def alerts(db: Db) -> Alerts:
    items = []
    kill_q = db.q(
        "SELECT triggered_at, switch_name, cleared_at, portfolio_state_snapshot "
        "FROM kill_switch_events ORDER BY triggered_at DESC LIMIT 10"
    )
    for row in kill_q.rows:
        if not row["cleared_at"]:
            items.append(("alarm",
                          f"kill switch ACTIVE: {row['switch_name']} since "
                          f"{row['triggered_at']}",
                          row["portfolio_state_snapshot"]))
    unprotected_q = db.q(
        "SELECT position_id, checked_at, status, live_stop_order_ids "
        "FROM stop_confirmations WHERE status != 'ok' ORDER BY checked_at DESC LIMIT 10"
    )
    for row in unprotected_q.rows:
        items.append(("alarm",
                      f"position {row['position_id']} is {row['status']} "
                      f"(checked {row['checked_at']})",
                      row["live_stop_order_ids"]))
    adaptive_q = db.q(
        "SELECT parameter, old_value, new_value, changed_at, reverted_at, "
        "evidence_summary, sample_ids, evidence_window_start, evidence_window_end, "
        "reverses_to FROM adaptive_param_log ORDER BY changed_at DESC LIMIT 20"
    )
    return Alerts(items=items, kill_q=kill_q, unprotected_q=unprotected_q,
                  adaptive_q=adaptive_q)


# --------------------------------------------------------------------------
# 8. The brain: the whole system's wiring, as recorded
# --------------------------------------------------------------------------


@dataclass
class Brain:
    layers: list = field(default_factory=list)   # [(label, [(id,label,weight)])]
    edges: list = field(default_factory=list)    # [(src,dst,weight,title)]
    queries: list = field(default_factory=list)  # every query behind it
    node_count: int = 0
    edge_count: int = 0


def brain(db: Db, limit: int = 60) -> Brain:
    """Sources -> candidates -> entities -> views -> decisions -> outcomes.

    Built ONLY from rows that exist. Every edge here is a foreign key or
    a recorded assertion; none is inferred to make the picture denser. A
    connector nobody can trace to a row would be a decoration that looks
    like evidence.
    """
    b = Brain()
    cand_q = db.q(
        "SELECT id, ticker, catalyst_type, source_event_ids, sector "
        "FROM candidates ORDER BY discovered_at DESC LIMIT ?", (limit,))
    b.queries.append(cand_q)
    cands = [dict(r) for r in cand_q.rows]
    cand_ids = {c["id"] for c in cands}

    # 1. Sources -> candidates, via the event ids the candidate names.
    src_q = db.q("SELECT source_id, source FROM raw_events")
    b.queries.append(src_q)
    source_of = {r["source_id"]: r["source"] for r in src_q.rows}
    source_hits: dict = {}
    for c in cands:
        for sid in (jload(c["source_event_ids"], []) or []):
            src = source_of.get(sid)
            if not src:
                continue
            key = f"src:{src}"
            source_hits[key] = source_hits.get(key, 0) + 1
            b.edges.append((key, f"cand:{c['id']}", 1,
                            f"{src} filing {sid} became candidate "
                            f"{c['ticker'] or c['id']}"))

    # 2. Candidates -> entities, from the evidence graph (if present).
    entity_hits: dict = {}
    if db.table_exists("graph_assertions") and db.table_exists("graph_entities"):
        ev_q = db.q(
            """SELECT s.canonical_key AS sk, s.display_name AS sn, s.kind AS skind,
                      o.canonical_key AS ok, o.display_name AS onm, o.kind AS okind,
                      a.predicate
               FROM graph_assertions a
               JOIN graph_entities s ON s.id = a.subject_entity_id
               LEFT JOIN graph_entities o ON o.id = a.object_entity_id
               LIMIT 400""")
        b.queries.append(ev_q)
        by_ticker = {(c["ticker"] or "").upper(): c["id"] for c in cands}
        for r in ev_q.rows:
            d = dict(r)
            for near, far, far_name, far_kind in (
                    (d["sk"], d["ok"], d["onm"], d["okind"]),
                    (d["ok"], d["sk"], d["sn"], d["skind"])):
                if not near or not str(near).startswith("company:"):
                    continue
                cid = by_ticker.get(str(near).split(":", 1)[1].upper())
                if not cid or not far or not far_name:
                    continue
                # Keyed by canonical_key, LABELLED by display name. The
                # key is "person:restrepo" and the name is "J. Restrepo,
                # CFO"; drawing the key shows the reader an id where a
                # name belongs, which is not evidence anyone can check.
                key = f"ent:{far}"
                name, count = entity_hits.get(key, (far_name, 0))
                entity_hits[key] = (name, count + 1)
                b.edges.append((f"cand:{cid}", key, 1,
                                f"{d['predicate']} ({far_kind or 'entity'})"))

    # 3. Candidates -> the model's view.
    view_q = db.q("SELECT candidate_id, direction, conviction FROM research_views")
    b.queries.append(view_q)
    view_hits: dict = {}
    for r in view_q.rows:
        d = dict(r)
        if d["candidate_id"] not in cand_ids:
            continue
        key = f"view:{d['direction'] or 'no direction'}"
        view_hits[key] = view_hits.get(key, 0) + 1
        b.edges.append((f"cand:{d['candidate_id']}", key, 1,
                        f"the model read this as {d['direction']} "
                        f"(conviction {d['conviction']})"))

    # 4. Views -> what the risk engine did with them.
    dec_q = db.q("SELECT candidate_id, action, skip_reasons FROM risk_decisions")
    b.queries.append(dec_q)
    act_hits: dict = {}
    for r in dec_q.rows:
        d = dict(r)
        if d["candidate_id"] not in cand_ids:
            continue
        view = next((dict(v)["direction"] for v in view_q.rows
                     if dict(v)["candidate_id"] == d["candidate_id"]), None)
        act = f"act:{d['action'] or 'no action'}"
        act_hits[act] = act_hits.get(act, 0) + 1
        if view is not None:
            b.edges.append((f"view:{view or 'no direction'}", act, 1,
                            f"the risk engine chose to {d['action']}"))
        for reason in (jload(d["skip_reasons"], []) or []):
            key = f"why:{reason}"
            act_hits.setdefault(act, 0)
            b.edges.append((act, key, 1, f"stopped on {reason}"))

    # 5. What actually happened.
    out_q = db.q("SELECT exit_reason, COUNT(*) n FROM closed_trades "
                 "GROUP BY exit_reason")
    b.queries.append(out_q)
    outcome_hits = {f"out:{r['exit_reason'] or 'unrecorded'}": r["n"]
                    for r in out_q.rows}
    for key in outcome_hits:
        b.edges.append(("act:trade", key, outcome_hits[key],
                        f"closed: {key.split(':', 1)[1]}"))
    reason_hits: dict = {}
    for src, dst, _, _ in b.edges:
        if dst.startswith("why:"):
            reason_hits[dst] = reason_hits.get(dst, 0) + 1

    def nodes(hits: dict, strip: str, pretty=lambda s: s):
        return [(k, pretty(k[len(strip):]), v)
                for k, v in sorted(hits.items(), key=lambda kv: -kv[1])]

    degree: dict = {}
    for src, dst, _, _ in b.edges:
        degree[src] = degree.get(src, 0) + 1
        degree[dst] = degree.get(dst, 0) + 1

    b.layers = [
        ("Sources", nodes(source_hits, "src:", source_label)),
        ("Candidates", [(f"cand:{c['id']}", c["ticker"] or c["id"],
                         degree.get(f"cand:{c['id']}", 0)) for c in cands
                        if degree.get(f"cand:{c['id']}", 0)]),
        ("What it linked",
         [(k, name, count) for k, (name, count) in
          sorted(entity_hits.items(), key=lambda kv: -kv[1][1])]),
        ("Model view", nodes(view_hits, "view:")),
        ("Risk engine", nodes(act_hits, "act:")
         + nodes(reason_hits, "why:", lambda s: s.replace("_", " "))),
        ("Outcome", nodes(outcome_hits, "out:", lambda s: s.replace("_", " "))),
    ]
    b.node_count = sum(len(n) for _, n in b.layers)
    b.edge_count = len(b.edges)
    return b


#: How many hops out from a focused node the map draws. ONE, measured:
#: this graph is hub-and-spoke, so two hops from a busy node reaches
#: most of it and gives back the wall the focus existed to escape.
#:
#:     focus                     hops=1     hops=2
#:     skip                      4n/40e    24n/60e
#:     no_trade                 12n/20e    19n/60e
#:     Insider trades (Form 4)    9n/8e    11n/12e
FOCUS_HOPS = 1


def collapse_edges(edges: list) -> list:
    """One line per relationship, however many rows recorded it.

    Twenty decisions all citing conviction_below_floor are twenty rows
    and twenty edges, drawn as twenty identical lines on top of each
    other: forty SVG elements the reader sees as one, with no hint that
    the relationship is heavy rather than singular.

    The COUNT IS NOT LOST - it becomes the line's weight, and the hover
    text says how many rows are behind it. The headline still reports
    every recorded link, because that is what the database holds.
    """
    merged: dict = {}
    for src, dst, weight, title in edges:
        key = (src, dst)
        if key in merged:
            w, t, n = merged[key]
            merged[key] = (w + (weight or 1), t, n + 1)
        else:
            merged[key] = (weight or 1, title, 1)
    return [(src, dst, w, (f"{t}  (and {n - 1} more like it - "
                           f"{n} recorded links)" if n > 1 else t))
            for (src, dst), (w, t, n) in merged.items()]


def brain_focus(whole: Brain, node_id: str, hops: int = FOCUS_HOPS) -> Brain:
    """The neighbourhood of ONE node, as its own small map.

    OWNER-REPORTED: "its got too much data all at once and isnt easy to
    navigate."

    A whole-graph picture answers "is anything connected" and almost
    nothing else - past a few dozen nodes the lines are a texture rather
    than information. What a reader actually wants is one thing and what
    it touches, which is small, legible, and a question the graph can
    answer exactly.

    Nothing is invented here: this is a SUBSET of the same edges, so a
    line on a focused map is the same recorded row it was on the whole
    one. The counts are recomputed over the subset, because reporting
    the whole graph's totals beside a fragment of it would misdescribe
    the picture on screen.
    """
    focused = Brain(queries=list(whole.queries))
    if not node_id:
        return focused
    reached = {node_id}
    for _ in range(max(1, hops)):
        nxt = set(reached)
        for src, dst, _w, _t in whole.edges:
            if src in reached:
                nxt.add(dst)
            if dst in reached:
                nxt.add(src)
        if nxt == reached:
            break                       # nothing further to reach
        reached = nxt
    focused.edges = [e for e in whole.edges
                     if e[0] in reached and e[1] in reached]
    kept = {n for e in focused.edges for n in (e[0], e[1])} | {node_id}
    focused.layers = [
        (label, [n for n in nodes if n[0] in kept])
        for label, nodes in whole.layers]
    focused.layers = [(lbl, ns) for lbl, ns in focused.layers if ns]
    focused.node_count = sum(len(n) for _, n in focused.layers)
    focused.edge_count = len(focused.edges)
    return focused


def busiest_nodes(b: Brain, limit: int = 8) -> list:
    """The handful worth focusing on first, most connected first.

    A map with no way in is a map nobody uses. These are the entry
    points: [(id, label, layer, links)].
    """
    ranked = [(nid, nlabel, layer, weight)
              for layer, nodes in b.layers for nid, nlabel, weight in nodes]
    ranked.sort(key=lambda r: -r[3])
    return ranked[:limit]


# --------------------------------------------------------------------------
# The news map: what was said, about whom, and what the bot did about it
# --------------------------------------------------------------------------


@dataclass
class NewsMap:
    layers: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    node_links: dict = field(default_factory=dict)
    story_count: int = 0
    ticker_count: int = 0
    query: QueryResult | None = None
    filters: dict = field(default_factory=dict)
    #: Tickers where news agreed with a FILING feed. These are the ones
    #: worth looking at, and they are rare on purpose.
    cross_feed_tickers: tuple = ()


#: Ticker column cap. A firehose day carries 450+ symbols and drawing
#: them is not a map, it is a smear - the owner asked for "filters so the
#: network doesnt get hug etc". Ordered by how much evidence each ticker
#: carries, so the cap keeps the interesting end.
MAP_MAX_TICKERS = 14
MAP_MAX_STORIES = 26


def news_map(db: Db, *, days: int = 3, kind: str = "",
             ticker: str = "", only_linked: bool = False) -> NewsMap:
    """News stories -> tickers -> what happened next, as recorded rows.

    EVERY EDGE IS A ROW. A story connects to a ticker because
    raw_events.payload_raw named that ticker; a ticker connects to a
    candidate because the candidate's source_event_ids contains that
    event's id. Nothing is inferred to make the picture denser - a
    connector nobody can trace back to a row is decoration that looks
    like evidence.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    res = db.q(
        "SELECT source_id, fetched_at, payload_raw FROM raw_events "
        "WHERE source = 'alpaca_news' AND fetched_at >= ? "
        "ORDER BY fetched_at DESC LIMIT 4000", (since,))

    stories: list = []
    by_ticker: dict = {}
    for row in res.rows:
        payload = jload(row["payload_raw"], {}) or {}
        tick = str(payload.get("ticker") or "").strip().upper()
        if not tick:
            continue
        catalyst = str(payload.get("catalyst_type") or "news")
        if kind and catalyst != kind:
            continue
        if ticker and tick != ticker.strip().upper():
            continue
        stories.append({
            "id": str(row["source_id"]),
            "ticker": tick,
            "headline": str(payload.get("headline") or "")[:110],
            "catalyst": catalyst,
            "hint": int(payload.get("direction_hint") or 0),
            "publisher": str(payload.get("publisher") or ""),
            "when": str(payload.get("filed_date") or "")[:10],
        })
        by_ticker.setdefault(tick, []).append(stories[-1])

    # Which tickers ALSO have evidence from a filing feed? Those are the
    # cross-feed conjunctions - the whole point - so they are marked and
    # sorted first, and `only_linked` reduces the map to just them.
    linked: set = set()
    if by_ticker:
        marks = ",".join("?" * len(by_ticker))
        others = db.q(
            f"SELECT payload_raw FROM raw_events "
            f"WHERE source != 'alpaca_news' AND fetched_at >= ? LIMIT 8000",
            (since,))
        for row in others.rows:
            payload = jload(row["payload_raw"], {}) or {}
            other = str(payload.get("ticker") or payload.get("symbol")
                        or "").strip().upper()
            if other in by_ticker:
                linked.add(other)

    if only_linked:
        by_ticker = {t: v for t, v in by_ticker.items() if t in linked}
        stories = [s for s in stories if s["ticker"] in by_ticker]

    # Which tickers became candidates, and what happened to them.
    outcomes: dict = {}
    if by_ticker:
        marks = ",".join("?" * len(by_ticker))
        got = db.q(
            "SELECT c.ticker, c.id, "
            "  COALESCE(d.action, CASE WHEN v.candidate_id IS NOT NULL "
            "                          THEN 'researched' ELSE 'seen' END) act "
            "FROM candidates c "
            "LEFT JOIN research_views v ON v.candidate_id = c.id "
            "LEFT JOIN risk_decisions d ON d.candidate_id = c.id "
            f"WHERE c.ticker IN ({marks})", tuple(by_ticker))
        for row in got.rows:
            outcomes.setdefault(str(row["ticker"]).upper(), set()).add(
                str(row["act"]))

    ordered = sorted(
        by_ticker.items(),
        key=lambda kv: (kv[0] not in linked, -len(kv[1]), kv[0]))[:MAP_MAX_TICKERS]
    keep_tickers = {t for t, _ in ordered}
    shown_stories = [s for s in stories
                     if s["ticker"] in keep_tickers][:MAP_MAX_STORIES]

    story_nodes = [(f"st-{s['id']}",
                    (s["headline"] or s["catalyst"])[:58],
                    1.0 + abs(s["hint"])) for s in shown_stories]
    ticker_nodes = [(f"tk-{t}", t + (" *" if t in linked else ""),
                     1.0 + min(3, len(rows)))
                    for t, rows in ordered]
    act_labels = {"trade": "traded", "skip": "declined by the risk engine",
                  "researched": "researched, no decision yet",
                  "seen": "seen, not researched"}
    acts: dict = {}
    for t in keep_tickers:
        for act in (outcomes.get(t) or {"seen"}):
            acts.setdefault(act, []).append(t)
    act_nodes = [(f"ac-{a}", act_labels.get(a, a), 1.0 + len(ts))
                 for a, ts in sorted(acts.items())]

    edges = [(f"st-{s['id']}", f"tk-{s['ticker']}", 1.0 + abs(s["hint"]),
              f"{s['when']} {s['publisher']}: {s['headline']}")
             for s in shown_stories]
    for act, ts in acts.items():
        for t in ts:
            edges.append((f"tk-{t}", f"ac-{act}", 1.0,
                          f"{t} -> {act_labels.get(act, act)}"))

    return NewsMap(
        layers=[("What was said", story_nodes),
                ("About whom", ticker_nodes),
                ("What the bot did", act_nodes)],
        edges=edges,
        node_links={f"tk-{t}": f"/logs?q={t}" for t in keep_tickers},
        story_count=len(stories), ticker_count=len(by_ticker),
        query=res,
        filters={"days": days, "kind": kind, "ticker": ticker,
                 "only_linked": only_linked},
        cross_feed_tickers=tuple(sorted(linked)),
    )


# --------------------------------------------------------------------------
# Decision chains: every step, in order, with its justification
# --------------------------------------------------------------------------


@dataclass
class ChainStep:
    """One link in the chain from raw filing to placed order."""

    n: int
    stage: str          # short label: "Found", "Linked", "Judged", ...
    headline: str       # one line: WHAT happened
    why: str            # one line: WHY it moved on, or why it stopped
    detail: list        # [(label, value)] shown when expanded
    href: str = ""      # click-through to the underlying record
    stopped: bool = False


@dataclass
class Chain:
    candidate_id: str
    ticker: str
    verdict: str        # "traded", "declined", "in progress"
    steps: list = field(default_factory=list)


@dataclass
class Chains:
    chains: list = field(default_factory=list)
    query: QueryResult | None = None


def decision_chains(db: Db, limit: int = 12) -> Chains:
    """The ordered story of each recent candidate.

    THE OWNER ASKED FOR THIS IN THESE WORDS: "I want every decision with
    justification in order from what is researched to find to placing a
    trade."

    The brain map answers "what is connected to what". It cannot answer
    "what happened, then what, and why" - a picture of a graph has no
    order in it. This does, one row per step, and every step carries the
    reason it moved on or the reason it stopped.

    Nothing here is inferred. Each step is built from rows that exist,
    and a step with no row says so rather than being quietly skipped -
    a chain that hides its gaps reads as a decision that was never made.
    """
    cand_q = db.q(
        "SELECT id, ticker, catalyst_type, source_event_ids, discovered_at, "
        "       sector FROM candidates ORDER BY discovered_at DESC LIMIT ?",
        (limit,))
    out: list = []
    for c in cand_q.rows:
        cid, ticker = str(c["id"]), str(c["ticker"])
        steps: list = []

        # 1. FOUND - the raw evidence, with the actual headline where the
        #    feed carried one. "a news event" is not evidence; the words
        #    are.
        ids = jload(c["source_event_ids"], []) or []
        sources = []
        if ids:
            marks = ",".join("?" * len(ids))
            for r in db.q(
                f"SELECT source, source_id, payload_raw FROM raw_events "
                f"WHERE source_id IN ({marks})",
                tuple(str(i) for i in ids)).rows:
                p = jload(r["payload_raw"], {}) or {}
                sources.append((
                    str(r["source"]),
                    str(p.get("headline") or p.get("matched_phrase")
                        or p.get("catalyst_type") or r["source_id"])[:140],
                    str(p.get("filed_date") or p.get("filing_date") or "")[:10]))
        steps.append(ChainStep(
            n=1, stage="Found",
            headline=(f"{len(sources)} piece(s) of evidence on {ticker}"
                      if sources else f"{ticker} surfaced with no readable source rows"),
            why=(f"grouped as {c['catalyst_type']}"
                 + (f" in {c['sector']}" if c["sector"] else "")),
            detail=[(f"{s} ({when or 'undated'})", text) for s, text, when in sources]
                   or [("no raw_events row matched", str(ids))],
            stopped=not sources))

        # 2. LINKED - only for a conjunction, and it says which feeds.
        feeds = sorted({s for s, _, _ in sources})
        if len(feeds) > 1:
            steps.append(ChainStep(
                n=2, stage="Linked",
                headline=f"{len(feeds)} independent feeds landed on {ticker}",
                why="two unrelated sources agreeing is the whole reason this "
                    "one was researched rather than skipped",
                detail=[("feeds", ", ".join(feeds))]))

        # 3. JUDGED - what the model concluded, in its own words.
        view = db.q(
            "SELECT direction, conviction, thesis, invalidation, priced_in, "
            "       priced_in_reasoning, expected_holding_days "
            "FROM research_views WHERE candidate_id = ?", (cid,))
        calls = db.q(
            "SELECT skipped_reason, cost_cents FROM research_calls "
            "WHERE candidate_id = ? ORDER BY called_at DESC LIMIT 1", (cid,))
        if view.rows:
            v = view.rows[0]
            steps.append(ChainStep(
                n=len(steps) + 1, stage="Judged",
                headline=f"{v['direction']} at {float(v['conviction']):.2f} conviction",
                why=str(v["thesis"])[:400],
                detail=[("what would prove it wrong", str(v["invalidation"])),
                        ("already priced in?",
                         ("yes - " if v["priced_in"] else "no - ")
                         + str(v["priced_in_reasoning"])),
                        ("expected hold", f"{v['expected_holding_days']} days")],
                href=f"/decision?candidate_id={cid}&view=full",
                stopped=str(v["direction"]) == "no_trade"))
        else:
            reason = (str(calls.rows[0]["skipped_reason"]) if calls.rows
                      and calls.rows[0]["skipped_reason"] else "not researched yet")
            steps.append(ChainStep(
                n=len(steps) + 1, stage="Judged",
                headline="no view was obtained",
                why=reason, detail=[("skip reason", reason)], stopped=True))

        # 4. SIZED - what deterministic code did with that view, and
        #    which limit bound. This is where the model stops mattering.
        dec = db.q(
            "SELECT action, side, notional_usd, qty, stop_price, "
            "       planned_exit_date, skip_reasons FROM risk_decisions "
            "WHERE candidate_id = ? ORDER BY decided_at DESC LIMIT 1", (cid,))
        if dec.rows:
            d = dec.rows[0]
            reasons = jload(d["skip_reasons"], []) or []
            traded = str(d["action"]) == "trade"
            steps.append(ChainStep(
                n=len(steps) + 1, stage="Sized",
                headline=(f"trade {d['side']} ${d['notional_usd']}" if traded
                          else "declined by the risk engine"),
                why=("stop at " + str(d["stop_price"]) + ", hard exit "
                     + str(d["planned_exit_date"]) if traded
                     else "; ".join(str(r) for r in reasons[:3]) or "no reason recorded"),
                detail=([("quantity", str(d["qty"])),
                         ("stop", str(d["stop_price"])),
                         ("hard exit date", str(d["planned_exit_date"]))] if traded
                        else [("limit that bound", str(r)) for r in reasons]),
                stopped=not traded))

        # 5. PLACED / 6. HAPPENED
        orders = db.q(
            "SELECT id, side, qty, status, submitted_at FROM orders "
            "WHERE decision_id = ? ORDER BY submitted_at", (cid,))
        if orders.rows:
            o = orders.rows[0]
            fills = db.q("SELECT price, qty, filled_at FROM fills "
                         "WHERE order_id = ? ORDER BY filled_at", (o["id"],))
            steps.append(ChainStep(
                n=len(steps) + 1, stage="Placed",
                headline=f"{o['side']} {o['qty']} - {o['status']}",
                why=(f"filled at {fills.rows[0]['price']}" if fills.rows
                     else "no fill recorded against this order"),
                detail=[("submitted", str(o["submitted_at"]))]
                       + [("fill", f"{f['qty']} @ {f['price']} on {f['filled_at']}")
                          for f in fills.rows],
                stopped=not fills.rows))
            closed = db.q(
                "SELECT realized_pnl_cents, exit_reason, actual_holding_days "
                "FROM closed_trades WHERE position_id IN "
                "(SELECT id FROM positions WHERE entry_order_ids LIKE ?)",
                (f'%{o["id"]}%',))
            if closed.rows:
                ct = closed.rows[0]
                steps.append(ChainStep(
                    n=len(steps) + 1, stage="Closed",
                    headline=f"{int(ct['realized_pnl_cents'])/100:+.2f} realised",
                    why=f"exited on {ct['exit_reason']} after "
                        f"{ct['actual_holding_days']} day(s)",
                    detail=[("trigger", str(ct["exit_reason"]))]))

        verdict = ("traded" if any(s.stage == "Placed" for s in steps)
                   else "declined" if any(s.stopped for s in steps)
                   else "in progress")
        out.append(Chain(candidate_id=cid, ticker=ticker, verdict=verdict,
                         steps=steps))
    return Chains(chains=out, query=cand_q)


# --------------------------------------------------------------------------
# One node of the brain map, opened
# --------------------------------------------------------------------------

#: WHAT EACH KIND OF NODE IS, and what to do when you land on one. The
#: owner asked to "click in and it opens another page with the runbook":
#: a map tells you a thing is connected to another thing, and then leaves
#: you to work out what either of them means.
NODE_RUNBOOK = {
    "src": ("A data feed",
            "One of the free sources the bot reads. Its links point at "
            "the candidates it produced. If this node has no links, the "
            "feed read something that never became a candidate - normal "
            "for most filings, a problem only if it NEVER produces any. "
            "Feed failures are on the Pipeline page."),
    "cand": ("A candidate the bot built",
             "A ticker something was noticed about. Its links point back "
             "to the evidence and forward to what the model and the risk "
             "engine did. Open its decision for the whole audit trail."),
    "ent": ("A named entity",
            "A person, company or filing the evidence graph recorded "
            "alongside a candidate - an insider, a counterparty, an "
            "agency. Useful for spotting the same name across unrelated "
            "candidates."),
    "view": ("What the model concluded",
             "Candidates group here by direction. A pile on 'no_trade' "
             "is normal and healthy; a pile on 'long' with nothing "
             "traded means the risk engine is refusing them, and the "
             "Pipeline page names which limit."),
    "why": ("A reason something stopped",
            "Every candidate that ended here stopped for this reason. If "
            "one reason dominates, that is the single thing to change - "
            "the Pipeline page shows the same counts with the drop "
            "reasons in full."),
    "company": ("A company in the news map",
                "Stories and filings that named this ticker. The news "
                "map filtered to it shows what was said and when."),
}


@dataclass
class NodeDetail:
    node_id: str
    label: str
    kind: str
    kind_label: str
    runbook: str
    incoming: list = field(default_factory=list)   # [(other_label, why)]
    outgoing: list = field(default_factory=list)
    links: list = field(default_factory=list)      # [(text, href)]
    found: bool = True


def node_detail(db: Db, node_id: str) -> NodeDetail:
    """Everything recorded about ONE node of the map.

    Built from the same brain() query the picture is, so the page and
    the drawing can never disagree about what connects to what. A node
    id that is not in the graph returns found=False rather than an empty
    page pretending to be a real one.
    """
    kind = str(node_id).split(":", 1)[0]
    kind_label, runbook = NODE_RUNBOOK.get(
        kind, ("A node", "No runbook is recorded for this kind of node "
                         "yet - send the Everything bundle from "
                         "Maintenance if it matters."))
    b = brain(db)
    labels = {nid: lbl for _, nodes in b.layers for nid, lbl, _ in nodes}
    detail = NodeDetail(
        node_id=str(node_id), label=labels.get(str(node_id), str(node_id)),
        kind=kind, kind_label=kind_label, runbook=runbook,
        found=str(node_id) in labels)
    for src, dst, _w, title in b.edges:
        if dst == node_id:
            detail.incoming.append((labels.get(src, src), str(title)))
        elif src == node_id:
            detail.outgoing.append((labels.get(dst, dst), str(title)))
    if kind == "cand":
        detail.links.append(("Open the full decision record",
                             f"/decision?candidate_id={node_id[5:]}&view=full"))
    if detail.label and kind in ("cand", "company", "ent"):
        detail.links.append(("See what the news said about it",
                             f"/newsmap?ticker={detail.label}"))
    # THE NAVIGATION LOOP. Map -> this runbook -> just this node's
    # corner of the map -> the next runbook. Without the middle step the
    # only way back into the picture is the whole picture, which is the
    # thing that was hard to read in the first place.
    if detail.found:
        detail.links.append(("Draw just this node and what it touches",
                             f"/brain?focus={node_id}"))
    detail.links.append(("Back to the whole map", "/brain"))
    return detail
