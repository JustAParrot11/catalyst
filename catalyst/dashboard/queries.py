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
        return [], f"local bar cache {root}/SPY.csv", f"{type(exc).__name__}: {exc}", 0

    window = [b for b in bars if start <= b.day <= end]
    if not window:
        span = f"{bars[0].day}..{bars[-1].day}" if bars else "empty file"
        return (
            [], f"local bar cache {root}/SPY.csv",
            f"cache holds {len(bars)} bars ({span}) but none inside the bot's "
            f"window {start}..{end}",
            len(bars),
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
    basis, meta = "basis unrecorded", ""
    try:
        raw_meta = cache.read_meta() or {}
        if raw_meta.get("feed") or raw_meta.get("adjustment"):
            basis = (f"feed={raw_meta.get('feed', 'unrecorded')}, "
                     f"adjustment={raw_meta.get('adjustment', 'unrecorded')}")
        meta = f", fetched_at={raw_meta.get('fetched_at', 'unknown')}"
    except Exception:
        meta = ", cache_meta unreadable"
    return points, f"local bar cache {root}/SPY.csv ({basis}{meta})", None, len(window)


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
        _, source, error, rows = _load_spy(today - timedelta(days=30), today)
        perf.spy_source, perf.spy_rows = source, rows
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

    spy_points, source, error, rows = _load_spy(perf.start_day, perf.end_day)
    perf.spy_points, perf.spy_source, perf.spy_error, perf.spy_rows = (
        spy_points, source, error, rows,
    )
    # A cache full of bars with none in a two-day window that happens to
    # be a weekend is not a broken benchmark. Distinguishing the two is
    # the whole point: one needs fixing, the other needs Tuesday.
    perf.spy_window_too_short = bool(
        not spy_points and rows > 0 and error and "none inside" in error)
    return perf


# --------------------------------------------------------------------------
# 2. The candidate funnel
# --------------------------------------------------------------------------


@dataclass
class Stage:
    key: str
    label: str
    count: int
    query: QueryResult
    drops: list = field(default_factory=list)   # [(reason, n, detail)]
    note: str = ""


@dataclass
class Funnel:
    stages: list
    blame: str = ""
    blame_stage: str = ""


def _grouped(db: Db, sql: str, params: tuple = ()) -> QueryResult:
    return db.q(sql, params)


def funnel(db: Db) -> Funnel:
    stages: list[Stage] = []

    raw_q = db.count("raw_events")
    err_q = db.q(
        "SELECT source, attempted_at, error_text FROM raw_events_errors "
        "ORDER BY attempted_at DESC LIMIT 20"
    )
    stages.append(Stage(
        "raw_events", "raw events fetched", int(raw_q.scalar(0) or 0), raw_q,
        drops=[(f"feed error: {r['source']}", 1, r["error_text"]) for r in err_q.rows],
        note="Sources that failed are listed with their raw upstream error text.",
    ))

    cand_q = db.count("candidates")
    stages.append(Stage(
        "candidates", "candidates built", int(cand_q.scalar(0) or 0), cand_q,
        note="Raw events that were not dated, tradeable events are dropped by "
             "discovery/candidates.py and leave no row here.",
    ))

    researched_q = db.q(
        "SELECT COUNT(DISTINCT candidate_id) FROM research_calls WHERE skipped_reason IS NULL"
    )
    skip_q = _grouped(db,
        "SELECT skipped_reason, COUNT(*) n FROM research_calls "
        "WHERE skipped_reason IS NOT NULL GROUP BY skipped_reason ORDER BY n DESC")
    gov_q = _grouped(db,
        "SELECT reason, COUNT(*) n, MIN(at) first_at, MAX(at) last_at "
        "FROM cost_governor_events WHERE decision = 'deny' "
        "GROUP BY reason ORDER BY n DESC")
    drops = [(f"research skipped: {r['skipped_reason']}", r["n"], "") for r in skip_q.rows]
    drops += [
        (f"cost governor denied: {r['reason']}", r["n"],
         f"first {r['first_at']}, last {r['last_at']}")
        for r in gov_q.rows
    ]
    stages.append(Stage(
        "researched", "researched by the model", int(researched_q.scalar(0) or 0),
        researched_q, drops=drops,
        note="A governor denial is a skip with a reason, never a silent no-op "
             "(ARCHITECTURE section 7.2).",
    ))

    view_q = db.count("research_views", "direction != 'no_trade'")
    no_trade_q = db.count("research_views", "direction = 'no_trade'")
    priced_in_q = db.count("research_views", "priced_in = 1")
    stages.append(Stage(
        "views", "model returned a directional view",
        int(view_q.scalar(0) or 0), view_q,
        drops=[
            ("model said no_trade", int(no_trade_q.scalar(0) or 0), ""),
            ("model judged it already priced in", int(priced_in_q.scalar(0) or 0),
             "priced_in=1 rows, which may overlap the directional views above"),
        ],
    ))

    proposed_q = db.count("risk_decisions", "action = 'trade'")
    skip_rows = db.q("SELECT skip_reasons FROM risk_decisions WHERE action = 'skip'")
    reason_counts: dict[str, int] = {}
    for row in skip_rows.rows:
        for reason in jload(row["skip_reasons"], []) or ["(unparseable skip_reasons)"]:
            reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + 1
    bind_q = _grouped(db,
        "SELECT rule_name, bound_type, COUNT(*) n FROM limit_applications "
        "WHERE binding = 1 GROUP BY rule_name, bound_type ORDER BY n DESC")
    drops = [(f"risk skip: {k}", v, "") for k, v in
             sorted(reason_counts.items(), key=lambda kv: -kv[1])]
    drops += [
        (f"limit bound: {r['rule_name']} ({r['bound_type']})", r["n"],
         "binding=1 in limit_applications")
        for r in bind_q.rows
    ]
    stages.append(Stage(
        "proposed", "risk engine proposed a trade", int(proposed_q.scalar(0) or 0),
        proposed_q, drops=drops,
        note="Deterministic code decides here; the model only gated entry.",
    ))

    orders_q = db.count("orders")
    unfilled_q = db.q(
        "SELECT COUNT(*) FROM risk_decisions d WHERE d.action = 'trade' "
        "AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.decision_id = d.candidate_id)"
    )
    rejected_q = _grouped(db,
        "SELECT status, COUNT(*) n FROM orders GROUP BY status ORDER BY n DESC")
    drops = []
    stranded = int(unfilled_q.scalar(0) or 0)
    if stranded:
        drops.append(("trade decision with no order row", stranded,
                      "execution never submitted, or crashed before recording"))
    drops += [(f"order status: {r['status']}", r["n"], "") for r in rejected_q.rows]
    stages.append(Stage(
        "orders", "orders placed", int(orders_q.scalar(0) or 0), orders_q, drops=drops,
    ))

    blame, blame_stage = "", ""
    if stages[-1].count == 0:
        # Where does it STOP? The last stage that produced anything, plus
        # one. Blaming the first empty stage is wrong when a later stage
        # clearly has rows (e.g. raw_events pruned but candidates present)
        # - that reads as a fault where there is none.
        last_nonzero = max((i for i, s in enumerate(stages) if s.count > 0),
                           default=None)
        idx = 0 if last_nonzero is None else last_nonzero + 1
        upstream = 0 if last_nonzero is None else stages[last_nonzero].count
        st = stages[idx]
        top = max(st.drops, key=lambda d: d[1], default=None)
        blame_stage = st.key
        blame = (
            f"No orders have been placed. The pipeline stops at \"{st.label}\": "
            f"{upstream} in, 0 out."
        )
        if top:
            blame += f" Largest drop reason at that stage: {top[0]} (n={top[1]})."
        else:
            blame += (
                " No drop reason was recorded at that stage either - that is itself "
                "the finding: the stage produced nothing and explained nothing."
            )
    return Funnel(stages=stages, blame=blame, blame_stage=blame_stage)


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
    try:
        from catalyst.setup.credentials import load_credentials
        _creds = load_credentials()
        admin_key_present = bool(_creds.anthropic_admin_key)
        raw_budget = (_creds.settings or {}).get("monthly_budget_usd")
        if raw_budget is not None:
            owner_budget_usd = Decimal(str(raw_budget))
    except Exception:  # noqa: BLE001 - display only, never fatal
        pass

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
        base_cap_cents=gov.BASE_CAP_CENTS, max_cap_cents=gov.GOVERNOR_MAX_CAP_CENTS,
        manual_month_cap_cents=gov.MANUAL_SPEND_CAP_CENTS_PER_MONTH,
        check_failed_q=check_failed_q, last_reconciled_ok=last_reconciled_ok,
        reconcile_gap_days=reconcile_gap_days,
        admin_key_present=admin_key_present,
        owner_budget_usd=owner_budget_usd,
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
        "       (SELECT COUNT(*) FROM research_calls rc WHERE rc.candidate_id = c.id) AS n_calls "
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

    return Trace(
        candidate_id=candidate_id, candidate_q=candidate_q, raw_events_q=raw_events_q,
        source_event_ids=source_ids, calls_q=calls_q, turns_by_call=turns_by_call,
        view_q=view_q, decisions_q=decisions_q, limits_by_decision=limits_by_decision,
        orders_q=orders_q, fills_by_order=fills_by_order, refusal_q=refusal_q,
        positions=positions, closed_q=closed_q, stops_q=stops_q,
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
        ("Sources", nodes(source_hits, "src:", lambda s: s.replace("_", " "))),
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
