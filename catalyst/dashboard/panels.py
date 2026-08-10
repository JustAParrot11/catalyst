"""Page bodies. Each panel takes a Db and returns HTML.

Panels take an `p` (id prefix) argument wherever they can appear beside
another instance of themselves, so element ids stay unique on a page —
duplicated ids once meant one panel silently received another's data and
both rendered blank.
"""

from decimal import Decimal

from catalyst.dashboard import charts, queries
from catalyst.dashboard.db import Db, START_CAPITAL_CENTS, jload
from catalyst.dashboard.render import (
    BAKEOFF_CAVEAT,
    MIN_TRADES_FOR_MEANING,
    PAPER_PNL_CAVEAT,
    SURVIVORSHIP_CAVEAT,
    alarm,
    caveat,
    caveat_fold,
    details,
    dollars,
    empty_block,
    esc,
    json_pretty,
    meter,
    ok,
    pill,
    pre,
    prov,
    raw,
    section,
    signed_pp,
    table,
    tiles,
)

# --------------------------------------------------------------------------
# Performance vs the S&P 500 — the top element on the page
# --------------------------------------------------------------------------


def performance_panel(db: Db, p: str = "perf") -> str:
    perf = queries.performance(db)
    out = []

    if perf.bot_points:
        excess = perf.excess_pp
        cls = "pos" if (excess or 0) >= 0 else "neg"
        excess_text = (
            f'<span class="{cls}">{esc(signed_pp(excess))}</span>' if excess is not None
            else '<span class="neg">unavailable</span>'
        )
        out.append(
            f'<p id="{p}-headline"><span class="big">{excess_text}</span> '
            f"excess return against SPY, both series indexed to 100 at "
            f"{esc(perf.start_day)}, bot line net of all API spend.</p>"
        )
        bot_text = (f"bot index {perf.bot_index:.2f} "
                    f"(= {dollars(perf.net_equity_cents)} on a $1,000 start)")
        spy_text = (f"SPY index {perf.spy_index:.2f}" if perf.spy_index is not None
                    else "SPY index unavailable, see the benchmark note below")
        out.append(prov(f"{bot_text} vs {spy_text}."))
    else:
        out.append(
            f'<p id="{p}-headline"><span class="big">no equity series yet</span> '
            "— nothing has closed and nothing has been billed, so there is no line "
            "to draw. The two queries behind that emptiness are printed below.</p>"
        )

    # The at-a-glance row. Every tile carries where its number came from,
    # because a bare figure on this page is not allowed to exist.
    if perf.bot_points:
        excess_v = perf.excess_pp
        # None means the BENCHMARK is missing, not that we are level.
        # `(None or 0) >= 0` read as "ahead of SPY" and wore a green
        # badge beside the word "n/a" (caught by rendering it).
        if excess_v is None:
            state, word = "idle", "no benchmark to compare against"
        elif excess_v >= 0:
            state, word = "good", "ahead of SPY"
        else:
            state, word = "crit", "behind SPY"
        headline_tile = (f'<span class="{"pos" if (excess_v or 0) >= 0 else "neg"}">'
                         f"{esc(signed_pp(excess_v))}</span>")
        headline_sub = f"{pill(state, word)} exposure-matched, net of API spend"
        equity_tile = dollars(perf.net_equity_cents)
        equity_sub = f"from $1,000 at {esc(perf.start_day)}"
    else:
        headline_tile = "&mdash;"
        headline_sub = f"{pill('idle', 'no closed trades yet')} nothing to compare"
        equity_tile = "&mdash;"
        equity_sub = "no equity series recorded yet"
    sample_state = ("good" if perf.n_closed >= MIN_TRADES_FOR_MEANING
                    else ("idle" if perf.n_closed == 0 else "warn"))
    out.append(tiles(f"{p}-tiles", [
        ("Excess vs SPY", headline_tile, headline_sub),
        ("Account value", equity_tile, equity_sub),
        ("Closed trades", str(perf.n_closed),
         f"{pill(sample_state, f'{perf.n_closed} of {MIN_TRADES_FOR_MEANING}')} "
         "needed before any number here means anything"),
    ]))

    # Sample-size honesty, first, before any number is read as a verdict.
    if perf.n_closed < MIN_TRADES_FOR_MEANING:
        out.append(alarm(
            f'<b id="{p}-small-sample">The sample is too small to mean anything.</b> '
            f"{perf.n_closed} closed trade(s) against a minimum of "
            f"{MIN_TRADES_FOR_MEANING} before any number here is allowed to be read "
            "as evidence (ARCHITECTURE.md section 6.1, MIN_SAMPLE_SIZE, itself a "
            "provisional placeholder rather than a power analysis). Treat every "
            "figure on this panel as a description of what happened, not as a "
            "measurement of edge."
        ))
    else:
        out.append(ok(
            f'<b id="{p}-small-sample">{perf.n_closed} closed trades</b> — at or above '
            f"the {MIN_TRADES_FOR_MEANING}-trade floor, so these numbers are readable "
            "as weak evidence. They are still one draw from a wide distribution; see "
            "the caveats below."
        ))

    out.append(caveat_fold(
        f"{p}-caveats",
        "Three standing caveats on every number here: the bake-off beat "
        "SPY on a lucky subsample, the graded universes are not "
        "delisting-complete, and paper P&L is fictional while the API "
        "bill is real. Open for the full wording.",
        [BAKEOFF_CAVEAT, SURVIVORSHIP_CAVEAT, PAPER_PNL_CAVEAT]))

    # The chart, or an explained absence.
    if perf.bot_points:
        series = [charts.Series(
            "catalyst, net of all API spend",
            [(pt[0].toordinal(), pt[1]) for pt in perf.bot_points],
            "var(--series-1)",
        )]
        if perf.spy_points:
            series.append(charts.Series(
                "SPY (total return, same start)",
                [(pt[0].toordinal(), pt[1]) for pt in perf.spy_points],
                "var(--series-2)", dash="5 3",
            ))
        xs = [pt[0] for pt in perf.bot_points]
        mid = xs[len(xs) // 2]
        x_labels = [(xs[0].toordinal(), str(xs[0])),
                    (mid.toordinal(), str(mid)),
                    (xs[-1].toordinal(), str(xs[-1]))]
        out.append(charts.index_chart(series, chart_id=f"{p}-chart", x_labels=x_labels))
        out.append(prov(
            "Y axis reads three ways on every tick: index (start=100), the same move "
            "in per cent, and the dollar value on the fixed $1,000 account. "
            "100 on this chart is $1,000, not a bug."
        ))
    else:
        # Draw the empty chart AS a chart. A blank gap where a graph
        # belongs reads as a broken page; the frame plus a plain-English
        # line keeps "nothing has happened yet" visibly different from
        # "this is broken" - the raw queries below settle which it is.
        out.append(charts.placeholder(
            chart_id=f"{p}-chart-placeholder",
            title="Account value vs SPY, indexed to 100, net of API spend",
            explanation="The line starts the day the first trade closes. "
                        "Nothing has closed yet.",
        ))
        out.append(empty_block(
            f"{p}-empty-closed", perf.closed_q,
            meaning="closed_trades is what the bot's equity line is built from.",
        ))
        out.append(empty_block(
            f"{p}-empty-costs", perf.costs_q,
            meaning="cost_events is what makes the line 'net of costs'.",
        ))

    # The arithmetic, spelled out.
    rows = [
        ["starting capital (fixed, CLAUDE.md)", dollars(START_CAPITAL_CENTS)],
        [f"realised P&amp;L, {perf.n_closed} closed trades "
         f"({perf.n_closed_live} live / {perf.n_closed_paper} paper)",
         dollars(perf.gross_pnl_cents)],
        ["less scheduled (runtime) API spend", "-" + dollars(perf.scheduled_cost_cents)],
        ["less manual (build/testing) API spend", "-" + dollars(perf.manual_cost_cents)],
        ["<b>= net equity, the blue line</b>", "<b>" + dollars(perf.net_equity_cents) + "</b>"],
    ]
    out.append(table(f"{p}-arithmetic", ["component", "amount"], rows, numeric_cols={1}))
    out.append(prov(
        "Provenance: realised P&L is from closed_trades.realized_pnl_cents "
        f"({perf.closed_q.row_count} rows, whole history). API spend is the LOCAL "
        f"ledger, priced by cost.tracker.price() from stored raw usage objects "
        f"({perf.costs_q.row_count} priced cost_events rows) - locally priced, not "
        "billed; the billed figure appears on the Cost page for closed days only."
    ))

    # Benchmark provenance, and its absence made loud.
    if perf.spy_points:
        out.append(prov(
            f"Benchmark: SPY, {len(perf.spy_points)} daily closes from "
            f"{perf.spy_source}, indexed to 100 on the same day as the bot line. "
            "Exposure is NOT matched: SPY is fully invested throughout, the bot is "
            "not - a like-for-like exposure-matched comparison needs a daily "
            "position-value series the schema does not record yet."
        ))
    else:
        out.append(alarm(
            f'<b id="{p}-spy-missing">SPY benchmark unavailable.</b> source tried: '
            f"<code>{esc(perf.spy_source or 'local bar cache')}</code>; rows usable: "
            f"{perf.spy_rows}. Raw reason: <code>{esc(perf.spy_error or 'unknown')}</code>. "
            "This is why the excess figure above reads unavailable rather than 0."
        ))
    out.append(prov(
        "Missing on purpose rather than invented: the T-bill comparison the brief "
        "also asks for. No risk-free rate series exists in the database or the bar "
        "cache, so there is nothing to draw; naming the gap beats drawing a made-up "
        "line. It needs a rate series (a DGS3MO-style column, or a BIL/SHV bar "
        "cache) before it can appear."
    ))
    return section(f"{p}-section", "Performance against the S&P 500, net of all costs",
                   "".join(out))


# --------------------------------------------------------------------------
# The funnel
# --------------------------------------------------------------------------


def funnel_panel(db: Db, p: str = "funnel") -> str:
    data = queries.funnel(db)
    out = []
    if data.blame:
        out.append(
            f'<div class="blame" id="{p}-blame"><b>Why it has not traded:</b> '
            f"{esc(data.blame)} (stage key: <code>{esc(data.blame_stage)}</code>)</div>"
        )
    else:
        out.append(ok(f'<span id="{p}-blame">Orders have been placed; no stage is '
                      "currently blocking the pipeline end to end.</span>"))

    # Survival rate at a glance: how many of the raw events reached the
    # far end. The count alone hides whether 3 orders came from 5
    # candidates or from 5,000.
    if data.stages:
        first, last = data.stages[0].count, data.stages[-1].count
        rate = f"{(100.0 * last / first):.1f}%" if first else "&mdash;"
        out.append(tiles(f"{p}-tiles", [
            (data.stages[0].label.strip().capitalize(), str(first),
             "everything the feeds produced in the window"),
            (data.stages[-1].label.strip().capitalize(), str(last),
             "what survived every stage below"),
            ("Survival rate", rate,
             f"{last} of {first} - each stage's losses are itemised below"),
        ]))

    widest = max((s.count for s in data.stages), default=0) or 1
    # Ordinal ramp, lightest at the widest stage and darkening as
    # candidates survive - the funnel's own direction is the encoding.
    # Steps are 100 apart because the validator FAILED the obvious
    # 6-step version on adjacent lightness (0.047 against a 0.06 floor).
    ramp = ["var(--step-1)", "var(--step-2)", "var(--step-3)",
            "var(--step-4)", "var(--step-5)"]
    for i, stage in enumerate(data.stages):
        width = max(3, int(380 * stage.count / widest))
        shade = ramp[min(i, len(ramp) - 1)]
        pct = (100.0 * stage.count / widest) if widest else 0.0
        # Stage-to-stage conversion is the number that turns a list of
        # counts into a diagnosis: it names WHICH step lost the
        # candidates, which "3 in, 0 out" never does on its own.
        prev = data.stages[i - 1].count if i else None
        if prev is None:
            conv = '<span class="funnel-conv">&mdash;</span>'
        elif prev == 0:
            conv = '<span class="funnel-conv">&mdash;</span>'
        else:
            kept = 100.0 * stage.count / prev
            lost_cls = " lost" if kept < 100.0 else ""
            conv = (f'<span class="funnel-conv{lost_cls}" title="{stage.count} '
                    f'of the {prev} that reached the stage above">'
                    f"{kept:.0f}% kept</span>")
        # A zero draws NO bar. A minimum-width stub reads as a stray mark
        # rather than as "none", and the count column already says 0.
        bar = ("" if stage.count == 0 else
               f'<span class="funnel-bar" style="width:{width}px;'
               f'background:{shade}" title="{esc(stage.label)}: {stage.count} '
               f'({pct:.0f}% of the widest stage)"></span>')
        out.append(
            f'<div class="funnel-row" id="{p}-row-{esc(stage.key)}">'
            f'<span class="funnel-label">{esc(stage.label)}</span>'
            f'<span class="funnel-n">{stage.count}</span>'
            f"{conv}<span>{bar}</span></div>"
        )
        # A stage starved by the one above it is not its own finding.
        # Repeating a full empty-state for every downstream stage was
        # what buried this panel; the query stays reachable either way,
        # so nothing is lost - only the repetition.
        starved = stage.count == 0 and prev == 0
        if stage.drops:
            drops = "".join(
                f"<li>{esc(reason)} &mdash; <b>{esc(n)}</b>"
                + (f" <span class='prov'>{raw(detail)}</span>" if detail else "")
                + "</li>"
                for reason, n, detail in stage.drops
            )
            out.append(f'<ul class="funnel-drop" id="{p}-drops-{esc(stage.key)}">{drops}</ul>')
        elif not starved:
            out.append(
                f'<p class="prov" id="{p}-nodrops-{esc(stage.key)}">'
                "no recorded drop reasons at this stage</p>"
            )
        if starved:
            err = (f' <span class="funnel-drop">query FAILED: '
                   f"{esc(stage.query.error)}</span>"
                   if stage.query.error else "")
            out.append(
                f'<div class="quiet" id="{p}-starved-{esc(stage.key)}">'
                "nothing reached this stage &mdash; the shortfall is above, "
                f"not here.{err}"
                f'<details id="{p}-starved-q-{esc(stage.key)}">'
                "<summary>its query anyway</summary>"
                f"<code>{esc(stage.query.sql)}</code> &mdash; returned "
                f"{stage.query.row_count} row(s)</details></div>"
            )
        elif stage.count == 0:
            out.append(empty_block(
                f"{p}-empty-{esc(stage.key)}", stage.query,
                meaning=stage.note or f"stage {stage.key} produced nothing",
            ))
        elif stage.note:
            out.append(prov(stage.note))
    return section(f"{p}-section", "Candidate funnel: raw events to orders", "".join(out))


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


def cost_panel(db: Db, p: str = "cost", compact: bool = False) -> str:
    c = queries.cost_panel(db)
    out = []

    base_hurdle = float(c.base_cap_cents) * 12 / START_CAPITAL_CENTS * 100
    max_hurdle = float(c.max_cap_cents) * 12 / START_CAPITAL_CENTS * 100

    # Tiles first: the three numbers that decide whether this is viable.
    total_mtd = c.scheduled_mtd_cents + c.manual_mtd_cents
    cap_used = ((c.scheduled_mtd_cents / c.base_cap_cents * 100)
                if c.base_cap_cents else Decimal(0))
    cap_state = ("crit" if cap_used >= 100 else
                 "warn" if cap_used >= 75 else "good")
    out.append(tiles(f"{p}-tiles", [
        ("Scheduled spend, this month", dollars(c.scheduled_mtd_cents),
         f"{pill(cap_state, f'{cap_used:.0f}% of the ${c.base_cap_cents / 100:.0f} cap')} "
         "locally priced from stored raw usage"),
        ("Annual hurdle at the cap", f"{base_hurdle:.1f}%",
         "what the strategy must beat before a trade counts as good"),
        ("Spend this month, all kinds", dollars(total_mtd),
         f"scheduled {dollars(c.scheduled_mtd_cents)} + manual "
         f"{dollars(c.manual_mtd_cents)}, never pooled"),
    ]))

    # Reconcile the owner's typed figure with the cap actually enforced.
    # Owner report 2026-08-10: entered 20 on the setup page, saw $5
    # everywhere afterwards, and nothing on this page explained why.
    # The setting can only ever TIGHTEN the cap (governor.authorize).
    if c.owner_budget_usd is not None:
        owner_cents = c.owner_budget_usd * 100
        hurdle = float(owner_cents) * 12 / 100 / 1000 * 100
        if owner_cents != c.base_cap_cents:
            out.append(prov(
                f"Your budget of ${c.owner_budget_usd:g}/month is the figure "
                f"being enforced, {'above' if owner_cents > c.base_cap_cents else 'below'} "
                f"the {dollars(c.base_cap_cents)} default. That is "
                f"{hurdle:.1f}% a year on a $1,000 account - the return the "
                "strategy has to beat before a trade counts as good. Separately, "
                "the bot may add to its own budget out of banked profit, never "
                f"past {dollars(c.max_cap_cents)} on its own."))

    # Pace against the cap. days_elapsed is already on the panel; the
    # marker is where an evenly-spent month would sit today, so under
    # or over pace is a glance rather than mental arithmetic.
    import calendar as _cal
    days_in_month = _cal.monthrange(c.as_of.year, c.as_of.month)[1]
    pace_pct = 100.0 * c.days_elapsed / days_in_month
    out.append(meter(
        f"{p}-meter", float(c.scheduled_mtd_cents), float(c.base_cap_cents),
        pace=pace_pct,
        legend=(f"Scheduled spend against the ${c.base_cap_cents / 100:.0f}/month "
                f"cap. The upright marker is an even-pace month at day "
                f"{c.days_elapsed} of {days_in_month} - fill left of it is "
                f"under pace, right of it is over.")))

    # Daily billed spend, charted. Same rows as the table below - this
    # is the same data drawn, not a second source of truth.
    daily = [(str(r["target_date"])[5:], float(r["cost_api_total_cents"] or 0),
              f"{r['target_date']}: {r['cost_api_total_cents']} cents billed")
             for r in reversed(c.billed_q.rows)][-30:]
    if daily:
        out.append('<div class="chart-wrap">' + charts.bar_chart(
            daily, chart_id=f"{p}-daily-chart",
            title="Billed spend per closed day, cents (Anthropic Cost API)",
            value_fmt=lambda v: f"{v:,.1f}",
            reference=(float(c.base_cap_cents) / 30.0,
                       "cap, pro-rata per day"),
        ) + "</div>")
        out.append(prov(
            "Bars are BILLED whole closed days from the Cost API - today is "
            "never among them, because the Cost API reports whole days only "
            "(TRAPS.md). The dashed line is the monthly cap divided by 30, "
            "shown as a pace guide only: the cap is enforced on the month's "
            "total, never per day."
        ))
    else:
        out.append('<div class="chart-wrap">' + charts.placeholder(
            chart_id=f"{p}-daily-placeholder",
            title="Billed spend per closed day, cents (Anthropic Cost API)",
            explanation="Bars appear once a day has closed and been "
                        "reconciled against the real bill.",
        ) + "</div>")

    out.append(table(
        f"{p}-summary",
        ["figure", "amount", "billed or estimated", "window", "samples"],
        [
            ["scheduled (runtime) spend, month to date",
             dollars(c.scheduled_mtd_cents),
             "ESTIMATED locally (priced by cost.tracker.price from stored raw usage)",
             f"{esc(c.month_prefix)}, {c.days_elapsed} day(s) elapsed",
             str(c.scheduled_samples)],
            ["manual (build/testing) spend, month to date",
             dollars(c.manual_mtd_cents),
             "ESTIMATED locally", f"{esc(c.month_prefix)}", str(c.manual_samples)],
            ["billed total across reconciled closed days",
             dollars(c.billed_total_cents),
             "BILLED (Anthropic Cost API, whole closed days only)",
             f"last {c.billed_days} reconciled day(s)", str(c.billed_days)],
            ["lifetime build budget used (manual)",
             f"{dollars(c.lifetime_manual_cents)} of "
             f"{dollars(c.lifetime_manual_budget_cents)}",
             "ESTIMATED locally, lifetime", "all time", "-"],
            ["lifetime scheduled spend", dollars(c.lifetime_scheduled_cents),
             "ESTIMATED locally, lifetime", "all time", "-"],
        ],
        numeric_cols={1},
    ))
    out.append(prov(
        "Today's spend is never billed-queryable: the Anthropic Cost API reports "
        "whole days only (TRAPS.md), so the month-to-date figures above are the "
        "local ledger's own pricing and the billed row covers closed days only. "
        f"Panel arithmetic cross-check: {c.ledger_crosscheck}."
    ))
    out.append(prov(
        f"Annual hurdle on the $1,000 account, computed from the CAP (a constant, "
        f"not a projection): base scheduled cap {dollars(c.base_cap_cents)}/month = "
        f"{base_hurdle:.1f}%/yr; hard ceiling {dollars(c.max_cap_cents)}/month = "
        f"{max_hurdle:.1f}%/yr. Observed spend is deliberately NOT annualised from "
        f"{c.days_elapsed} day(s) - cost/ledger.py exposes no function that "
        "multiplies a partial month into a year (ARCHITECTURE section 7.4)."
    ))

    # cost-audit F2: the nightly bill check going dark must be as loud as
    # the check failing. Alarms when the admin key is configured but the
    # most recent SUCCESSFUL reconciliation is missing or > 2 days behind
    # yesterday, and prints the raw error of any failed check beside it.
    check_is_stale = c.admin_key_present and (
        c.reconcile_gap_days is None or c.reconcile_gap_days > 2)
    if check_is_stale or c.check_failed_q.rows:
        last = (f"most recent successful check covered "
                f"{esc(c.last_reconciled_ok)}" if c.last_reconciled_ok
                else "no day has EVER been successfully reconciled")
        out.append(alarm(
            f'<b id="{p}-recon-stale">The nightly bill check is not '
            f"current.</b> An admin key is "
            f"{'configured' if c.admin_key_present else 'NOT configured'}; "
            f"{last}. Until it runs, every figure above is the local "
            "estimate with no cross-check against the real bill."
        ))
        for i, r in enumerate(c.check_failed_q.rows):
            out.append(
                f'<div id="{p}-recon-failed-{i}">'
                f"<p>Bill check for {esc(r['target_date'])} FAILED; raw "
                f"error below.</p>"
                + details(f"{p}-recon-failed-raw-{i}", "raw failure",
                          pre(json_pretty(r["api_raw_response"])))
                + "</div>")

    if c.scheduled_samples == 0:
        upstream = None
        if c.reconciliation_q.rows:
            upstream = c.reconciliation_q.rows[0]["api_raw_response"]
        out.append(empty_block(
            f"{p}-empty-scheduled", c.scheduled_mtd_q, upstream=upstream,
            meaning="zero scheduled spend this month. Either nothing ran, or the "
                    "recording path is broken - the raw Cost API payload from the "
                    "most recent reconciliation is printed beside it so those two "
                    "can be told apart.",
        ))

    if c.rates_stale:
        out.append(alarm(
            f'<b id="{p}-rates-stale">Pricing table is stale.</b> '
            f"catalyst/cost/pricing.py was last verified against the published rates "
            f"on {esc(c.rates_verified_on)}; rates_stale() is True as of "
            f"{esc(c.as_of)}. Every cost number on this page is priced from that "
            "table, so treat them as provenance-suspect until it is re-verified."
        ))
    else:
        out.append(prov(
            f"Pricing table provenance: verified {c.rates_verified_on}, "
            f"rates_stale() = False as of {c.as_of}."
        ))

    if c.unpriced_q.rows:
        rows = [[esc(r["id"]), esc(r["model"]), esc(r["kind"]), esc(r["component"]),
                 esc(r["priced_at"]),
                 details(f"{p}-unpriced-raw-{i}", "raw usage object",
                         pre(json_pretty(r["raw_usage_json"])))]
                for i, r in enumerate(c.unpriced_q.rows)]
        out.append(alarm(
            f'<b id="{p}-unpriced">{len(rows)} cost row(s) recorded but NOT priced.</b> '
            "The governor blocks all spend while any unpriced row exists "
            "(cost/tracker.py). The verbatim usage payload is beside each one."
        ))
        out.append(table(f"{p}-unpriced-table",
                         ["id", "model", "kind", "component", "priced_at", "raw usage"],
                         rows))

    # Reconciliation discrepancies and the acknowledge form (a WRITE path).
    if c.unacked_q.rows:
        out.append(alarm(
            f'<b id="{p}-unacked">{c.unacked_q.row_count} unacknowledged '
            "reconciliation discrepancy(ies). Scheduled spend is PAUSED until a "
            "human acknowledges each one.</b>"
        ))
        for i, r in enumerate(c.unacked_q.rows):
            zero_note = ""
            if int(r["api_record_count"] or 0) == 0:
                zero_note = (
                    "<p class='funnel-drop'>The Cost API returned <b>0 records</b> for "
                    "this day. The verbatim payload is printed below so an empty day "
                    "and a broken query are distinguishable.</p>"
                )
            out.append(
                f'<div id="{p}-unacked-{i}">'
                f"<p>{esc(r['target_date'])} &mdash; local "
                f"{dollars(r['local_total_cents'])} vs Cost API "
                f"{dollars(r['cost_api_total_cents'])}, discrepancy "
                f"{dollars(r['discrepancy_cents'])} against a threshold of "
                f"{dollars(r['threshold_cents'])}; API records: "
                f"{esc(r['api_record_count'])}.</p>"
                + zero_note
                + details(f"{p}-unacked-raw-{i}", "raw Cost API payload for this day",
                          pre(json_pretty(r["api_raw_response"])))
                + f'<form class="inline" id="{p}-ack-form-{i}" method="post" '
                  'action="/acknowledge-reconciliation">'
                  f'<input type="hidden" name="event_id" value="{esc(r["id"])}">'
                  '<label>acknowledged by (a human name, required): '
                  f'<input id="{p}-ack-who-{i}" name="acknowledged_by" required '
                  'placeholder="your name"></label> '
                  '<button type="submit">acknowledge and resume scheduled spend</button>'
                  "</form></div>"
            )
    else:
        out.append(ok(f'<span id="{p}-unacked">No unacknowledged reconciliation '
                      "discrepancies. Scheduled spend is not paused on this ground.</span>"))

    if compact:
        return section(f"{p}-section", "Cost (summary)", "".join(out))

    recon_rows = [
        [esc(r["target_date"]), esc(r["kind"]), dollars(r["local_total_cents"]),
         dollars(r["cost_api_total_cents"]), dollars(r["discrepancy_cents"]),
         esc(r["api_record_count"]), esc(r["action_taken"]),
         esc(r["acknowledged_by"] or "-"),
         details(f"{p}-recon-raw-{i}", "raw payload", pre(json_pretty(r["api_raw_response"])))]
        for i, r in enumerate(c.reconciliation_q.rows)
    ]
    out.append("<h3>Reconciliation history (local ledger vs Cost API, one closed day each)</h3>")
    if recon_rows:
        out.append(table(
            f"{p}-recon",
            ["day", "kind", "local", "billed", "discrepancy", "API records",
             "action", "acknowledged by", "raw"],
            recon_rows, numeric_cols={2, 3, 4, 5},
        ))
    else:
        out.append(empty_block(
            f"{p}-empty-recon", c.reconciliation_q,
            meaning="no day has been reconciled against the Cost API yet",
        ))

    out.append("<h3>Governor decisions (every skip carries its reason)</h3>")
    gov_rows = [
        [esc(r["at"]), esc(r["requested_kind"]), esc(r["decision"]),
         esc(r["reason"] or "-"), dollars(r["estimate_cents"]), dollars(r["cap_cents"]),
         esc(r["cycle_id"] or "-")]
        for r in c.governor_q.rows
    ]
    if gov_rows:
        out.append(table(
            f"{p}-governor",
            ["at", "kind", "decision", "reason", "estimate", "cap", "cycle"],
            gov_rows, numeric_cols={4, 5},
        ))
    else:
        out.append(empty_block(
            f"{p}-empty-governor", c.governor_q,
            meaning="the governor has never been asked to authorize anything",
        ))
    return section(f"{p}-section", "Cost, with provenance on every number", "".join(out))


# --------------------------------------------------------------------------
# Alerts / adaptive log
# --------------------------------------------------------------------------


def alerts_panel(db: Db, p: str = "alerts") -> str:
    a = queries.alerts(db)
    out = []
    if a.items:
        for i, (severity, text, detail) in enumerate(a.items):
            body = f"<b>{esc(text)}</b>" + (f"<br>{pre(detail)}" if detail else "")
            out.append(f'<div class="{esc(severity)}" id="{p}-item-{i}">{body}</div>')
    else:
        out.append(ok(f'<span id="{p}-none">No active kill switch and no unprotected '
                      "position recorded.</span>"))
        out.append(prov(
            f"Sources: kill_switch_events ({a.kill_q.row_count} rows read), "
            f"stop_confirmations non-ok ({a.unprotected_q.row_count} rows read). "
            "Zero rows here means no check has ever recorded a problem - it does not "
            "prove the checks ran; the Logs page is where you confirm they did."
        ))

    out.append("<h3>Adaptive parameter changes, with the evidence behind each</h3>")
    rows = [
        [esc(r["parameter"]), esc(r["old_value"]), esc(r["new_value"]),
         esc(r["changed_at"]), esc(r["reverted_at"] or "-"),
         esc(r["reverses_to"]),
         f"{esc(r['evidence_window_start'])}..{esc(r['evidence_window_end'])}",
         raw(r["evidence_summary"]),
         esc(len(jload(r["sample_ids"], []) or []))]
        for r in a.adaptive_q.rows
    ]
    if rows:
        out.append(table(
            f"{p}-adaptive",
            ["parameter", "old", "new", "changed at", "reverted at", "reverses to",
             "evidence window", "evidence", "sample n"],
            rows, numeric_cols={8},
        ))
    else:
        out.append(empty_block(
            f"{p}-empty-adaptive", a.adaptive_q,
            meaning="no adaptive parameter has moved. At a few trades a month this "
                    "is the expected state for months - adaptation needs closed, "
                    "scored outcomes and a minimum sample per parameter "
                    "(ARCHITECTURE section 6.1).",
        ))
    return section(f"{p}-section", "Operational alerts and adaptation", "".join(out))


# --------------------------------------------------------------------------
# Decisions: index and single-candidate narrative
# --------------------------------------------------------------------------


def decisions_index(db: Db, p: str = "dec") -> str:
    res = queries.decision_list(db)
    if res.is_empty:
        return section(f"{p}-section", "Decisions (taken and declined)",
                       empty_block(f"{p}-empty", res,
                                   meaning="no candidate has ever been discovered"))
    rows = []
    for r in res.rows:
        status = r["action"] or ("researched" if r["n_calls"] else "not researched")
        if r["n_orders"]:
            status = f"traded ({r['n_orders']} order(s))"
        elif r["action"] == "skip":
            status = "declined"
        rows.append([
            f'<a href="/decision?candidate_id={esc(r["id"])}">{esc(r["ticker"])}</a>',
            esc(r["catalyst_type"]), esc(r["catalyst_date"]), esc(r["sector"]),
            esc(r["direction"] or "-"),
            esc(f"{r['conviction']:.2f}" if r["conviction"] is not None else "-"),
            esc("yes" if r["priced_in"] else ("no" if r["priced_in"] is not None else "-")),
            esc(status), esc(r["discovered_at"]),
        ])
    body = table(
        f"{p}-table",
        ["ticker", "catalyst", "catalyst date", "sector", "model direction",
         "conviction", "priced in", "outcome", "discovered"],
        rows, numeric_cols={5},
    )
    body += prov(
        f"{res.row_count} candidate(s), newest first. Declined candidates are listed "
        "beside taken ones on purpose: a decision to skip is a decision, and its "
        "trace is reconstructable the same way."
    )
    return section(f"{p}-section", "Decisions (taken and declined)", body)


def _narrative_what_was_seen(t: queries.Trace, p: str) -> str:
    out = ["<h3>1. What the model was given, and what it looked at</h3>"]
    if t.candidate_q.rows:
        c = dict(t.candidate_q.rows[0])
        out.append(
            f"<p id='{p}-seen-prose'>On {esc(c['discovered_at'])} discovery built a "
            f"candidate from {len(t.source_event_ids)} raw source event(s): "
            f"<b>{esc(c['ticker'])}</b>, catalyst type <b>{esc(c['catalyst_type'])}</b>, "
            f"resolving {esc(c['catalyst_date'])} "
            f"({esc(c['catalyst_date_confidence'])}), sector {esc(c['sector'])}, "
            f"correlation tags {esc(c['correlation_tags'])}.</p>"
        )
    else:
        out.append(empty_block(f"{p}-empty-candidate", t.candidate_q,
                               meaning="no candidate row with this id"))
    if t.raw_events_q.rows:
        for i, r in enumerate(t.raw_events_q.rows):
            out.append(details(
                f"{p}-rawevent-{i}",
                f"source event {r['source']}:{r['source_id']} fetched {r['fetched_at']}",
                pre(json_pretty(r["payload_raw"])),
            ))
    else:
        out.append(empty_block(
            f"{p}-empty-rawevents", t.raw_events_q,
            meaning="the candidate names source_event_ids "
                    f"({t.source_event_ids}) but no raw_events row matched them",
        ))

    if t.calls_q.rows:
        for i, call in enumerate(t.calls_q.rows):
            tools = jload(call["tools_offered"], []) or []
            head = (
                f"<p id='{p}-call-{i}'>Model call {esc(call['id'])} to "
                f"<code>{esc(call['model'])}</code> at {esc(call['called_at'])}, "
                f"{esc(call['latency_ms'])} ms, cost {dollars(call['cost_cents'])}, "
                f"tools offered: {esc(', '.join(map(str, tools)) or 'none')}"
                + (f", <b>skipped: {esc(call['skipped_reason'])}</b>"
                   if call["skipped_reason"] else "")
                + ".</p>"
            )
            out.append(head)
            out.append(details(f"{p}-prompt-{i}", "the exact prompt sent",
                               pre(call["prompt_rendered"])))
            turns = t.turns_by_call.get(call["id"])
            if turns is not None and turns.rows:
                for turn in turns.rows:
                    out.append(details(
                        f"{p}-turn-{i}-{turn['turn_index']}",
                        f"turn {turn['turn_index']} (stop_reason "
                        f"{turn['stop_reason']}) - verbatim API response and usage",
                        pre(json_pretty(turn["raw_response"]))
                        + pre(json_pretty(turn["usage_raw"])),
                    ))
            else:
                out.append(empty_block(
                    f"{p}-empty-turns-{i}",
                    turns or t.calls_q,
                    meaning="the call recorded no API turns - a call that cost money "
                            "with no turn rows is a recording bug, a call skipped "
                            "before spending is not",
                ))
    else:
        out.append(empty_block(
            f"{p}-empty-calls", t.calls_q,
            meaning="the model was never asked about this candidate",
        ))
    return "".join(out)


def _narrative_what_it_concluded(t: queries.Trace, p: str) -> str:
    out = ["<h3>2. What the model concluded, in its own words</h3>"]
    if not t.view_q.rows:
        out.append(empty_block(
            f"{p}-empty-view", t.view_q,
            meaning="no research_views row: the model produced no structured view "
                    "(skipped, denied by the governor, or the extraction turn failed)",
        ))
        return "".join(out)
    v = dict(t.view_q.rows[0])
    out.append(
        f"<p id='{p}-view-prose'>The model returned direction "
        f"<b>{esc(v['direction'])}</b> at conviction <b>{v['conviction']:.2f}</b>, "
        f"expected holding {esc(v['expected_holding_days'])} day(s), and judged the "
        f"move <b>{'already priced in' if v['priced_in'] else 'not yet priced in'}</b>."
        "</p>"
    )
    out.append(table(
        f"{p}-view",
        ["field", "verbatim"],
        [["thesis", raw(v["thesis"])],
         ["what would invalidate it", raw(v["invalidation"])],
         ["priced-in reasoning", raw(v["priced_in_reasoning"])]],
    ))
    out.append(prov(
        "Verbatim from research_views. Conviction is a GATE, never a size input: "
        "risk/sizing.py cannot receive this object at all (ARCHITECTURE section 4.3)."
    ))
    return "".join(out)


def _narrative_what_risk_did(t: queries.Trace, p: str) -> str:
    out = ["<h3>3. What the deterministic risk engine did with it</h3>"]
    if not t.decisions_q.rows:
        out.append(empty_block(
            f"{p}-empty-decisions", t.decisions_q,
            meaning="no risk_decisions row: the candidate never reached the risk gate",
        ))
        return "".join(out)
    view = dict(t.view_q.rows[0]) if t.view_q.rows else None
    for i, d in enumerate(t.decisions_q.rows):
        d = dict(d)
        reasons = jload(d["skip_reasons"], []) or []
        if d["action"] == "trade":
            notional = d["notional_usd"]
            notional_text = (f"${Decimal(str(notional)):,.2f}"
                             if notional is not None else "not recorded")
            prose = (
                f"Code decided to TRADE: {esc(d['side'])} "
                f"{esc(d['qty'])} shares, notional {esc(notional_text)}"
                f", stop at {esc(d['stop_price'])}, hard exit "
                f"{esc(d['planned_exit_date'])}."
            )
        else:
            prose = (
                "Code decided to SKIP. Reasons recorded: "
                f"{esc(', '.join(map(str, reasons)) or 'none recorded')}."
            )
        if view and view["direction"] != "no_trade" and d["action"] == "skip":
            prose += (
                " <b>Code overruled the model here</b>: the model returned a "
                f"directional view ({esc(view['direction'])}, conviction "
                f"{view['conviction']:.2f}) and the risk engine declined it anyway."
            )
        out.append(f"<p id='{p}-decision-{i}'>{prose}</p>")

        limits = t.limits_by_decision.get(d["id"])
        if limits is not None and limits.rows:
            rows = [
                [esc(r["rule_name"]), esc(r["bound_type"]), esc(r["requested_value"]),
                 esc(r["bound_value"]),
                 "<b>BOUND</b>" if r["binding"] else "did not bind"]
                for r in limits.rows
            ]
            out.append(table(
                f"{p}-limits-{i}",
                ["rule", "type", "requested", "bound to", "did it bind?"],
                rows, numeric_cols={2, 3},
            ))
            binding = [r["rule_name"] for r in limits.rows if r["binding"]]
            if binding:
                out.append(prov(
                    "Binding rules on this decision: " + ", ".join(map(str, binding))
                    + ". A hard bound never moves by itself; an adaptive one moves "
                    "only on closed, scored outcomes."
                ))
        else:
            out.append(empty_block(
                f"{p}-empty-limits-{i}", limits or t.decisions_q,
                meaning="no limit_applications rows: no rule was recorded as even "
                        "considered for this decision",
            ))
        out.append(details(
            f"{p}-snapshot-{i}", "adaptive parameter values in effect at decision time",
            pre(json_pretty(d["adaptive_params_snapshot"])),
        ))
    return "".join(out)


def _narrative_what_happened(t: queries.Trace, p: str) -> str:
    out = ["<h3>4. What actually happened at the broker</h3>"]
    if not t.orders_q.rows:
        out.append(empty_block(
            f"{p}-empty-orders", t.orders_q,
            meaning="no orders row for this candidate's decisions. If a decision "
                    "above says TRADE, this emptiness is the bug; if it says SKIP, "
                    "it is the expected state.",
        ))
    for i, o in enumerate(t.orders_q.rows):
        o = dict(o)
        out.append(
            f"<p id='{p}-order-{i}'>Order {esc(o['id'])} "
            f"(broker id {esc(o['broker_order_id'] or 'none assigned')}): "
            f"{esc(o['side'])} {esc(o['qty'])} as {esc(o['order_type'])} "
            f"{esc(o['time_in_force'])}, submitted {esc(o['submitted_at'])}, "
            f"status <b>{esc(o['status'])}</b>.</p>"
        )
        out.append(details(f"{p}-order-raw-{i}", "broker response, verbatim",
                           pre(json_pretty(o["raw_response"]))))
        fills = t.fills_by_order.get(o["id"])
        if fills is not None and fills.rows:
            rows = [[esc(f["filled_at"]), esc(f["qty"]), esc(f["price"]),
                     esc(f["broker_reported_price"]),
                     esc(f["modeled_slippage"] if f["modeled_slippage"] is not None else "-")]
                    for f in fills.rows]
            out.append(table(
                f"{p}-fills-{i}",
                ["filled at", "qty", "recorded price", "broker reported price",
                 "modeled slippage"],
                rows, numeric_cols={1, 2, 3, 4},
            ))
            out.append(prov(
                "Paper fills pay no spread. The modeled slippage sits BESIDE the "
                "broker's price, never instead of it - reconciliation compares "
                "against the real fill (TRAPS.md)."
            ))
        else:
            out.append(empty_block(
                f"{p}-empty-fills-{i}", fills or t.orders_q,
                upstream=o["raw_response"],
                meaning="no fills for this order; the broker's raw response for the "
                        "order is printed beside the zero",
            ))

    if t.positions:
        for i, pos in enumerate(t.positions):
            out.append(
                f"<p id='{p}-position-{i}'>Position {esc(pos['id'])} in "
                f"{esc(pos['ticker'])}, opened {esc(pos['opened_at'])}, hard exit date "
                f"{esc(pos['planned_exit_date'])}, status {esc(pos['status'])}, stop "
                f"order {esc(pos['stop_order_id'] or 'NONE RECORDED')}.</p>"
            )
    if t.closed_q.rows:
        for i, ct in enumerate(t.closed_q.rows):
            ct = dict(ct)
            out.append(
                f"<p id='{p}-closed-{i}'>Closed for "
                f"<b>{dollars(ct['realized_pnl_cents'])}</b> realised "
                f"({esc(ct['account_mode'])} account): entry {esc(ct['entry_price'])}, "
                f"exit {esc(ct['exit_price'])}, trigger "
                f"<b>{esc(ct['exit_reason'])}</b>, held "
                f"{esc(ct['actual_holding_days'])} day(s) against an expected "
                f"{esc(ct['expected_holding_days'])}.</p>"
            )
    elif t.positions:
        out.append(empty_block(f"{p}-empty-closed", t.closed_q,
                               meaning="position is still open, or never closed"))
    if t.stops_q.rows:
        rows = [[esc(r["checked_at"]), esc(r["status"]), esc(r["live_stop_order_ids"])]
                for r in t.stops_q.rows]
        out.append(table(f"{p}-stops", ["checked at", "status", "live stop order ids"], rows))
    return "".join(out)


def _narrative_evidence(t: queries.Trace, p: str, db_for_graph=None,
                        ticker: str = "") -> str:
    out = ["<h3>5. Evidence the decision was built on</h3>"]
    ev = t.evidence
    if not ev.available:
        out.append(f'<div class="empty" id="{p}-evidence-missing">{esc(ev.reason)}</div>')
        return "".join(out)
    res = ev.query
    if res is None or res.is_empty:
        out.append(empty_block(
            f"{p}-evidence-empty", res,
            meaning=f"{ev.table} exists (columns: {', '.join(ev.columns)}) but holds "
                    "no assertion for this candidate",
        ))
        return "".join(out)
    cols = ev.columns
    # The mindmap FIRST, then the verbatim table beneath it. The picture
    # answers "what is this built on"; the table is what you read when
    # you need the exact wording of a claim. Columns are still
    # feature-detected - the diagram degrades to the table alone rather
    # than guessing a column that is not there.
    label_of = lambda d, keys: next(  # noqa: E731
        (str(d[k]) for k in keys if d.get(k) not in (None, "")), "")
    # Named rows where the graph tables allow it; the generic scan is
    # the fallback, so a database without stage 5a still renders.
    named = queries.evidence_graph(db_for_graph, ticker) if db_for_graph else None
    graph_rows = named.rows if (named and named.rows) else res.rows
    # THE COMPANY IS THE CENTRE. An assertion may point either way -
    # "CEO bought shares of GBFH" has the company as the OBJECT - so the
    # branch is whichever endpoint is not the company. Reading the
    # object blindly put a random LLC in the middle and drew the company
    # four times around the rim (caught by rendering it).
    centre = ticker or t.candidate_id
    for r in graph_rows:
        d = dict(r)
        if str(d.get("subject_kind") or "") == "company":
            centre = label_of(d, ("subject_label",)) or centre
            break
        if str(d.get("object_kind") or "") == "company":
            centre = label_of(d, ("object_label",)) or centre
            break

    branches, seen = [], set()
    for r in graph_rows:
        d = dict(r)
        subj = label_of(d, ("subject_label", "subject", "subject_entity_id"))
        obj = label_of(d, ("object_label", "object", "object_entity_id"))
        predicate = label_of(d, ("predicate", "relation", "edge", "kind"))
        if obj == centre and subj:
            node, kind = subj, label_of(d, ("subject_kind",))
        elif subj == centre and obj:
            node, kind = obj, label_of(d, ("object_kind",))
        elif d.get("object_date") and subj == centre:
            node, kind = str(d["object_date"]), "event"
        else:
            node, kind = obj or subj, label_of(d, ("object_kind", "subject_kind"))
        if not node or node == centre:
            continue
        key = (predicate, node)
        if key in seen:
            continue
        seen.add(key)
        branches.append((
            predicate or "linked to", node, kind or "entity",
            label_of(d, ("reliability", "confidence", "strength")),
            label_of(d, ("source_ref", "source_class", "source")),
        ))
        if len(branches) >= 12:
            break
    if branches:
        out.append('<div class="chart-wrap">' + charts.mindmap(
            esc(centre), [(esc(a), esc(b), esc(c), esc(dd), esc(e))
                          for a, b, c, dd, e in branches],
            chart_id=f"{p}-mindmap") + "</div>")
        out.append(prov(
            "Every box is a fact the bot linked to this candidate, and every "
            "line is one recorded assertion - hover a line for its source and "
            "how it was established. A solid line was filed with a regulator; "
            "dashed was reported; dotted was inferred. Layout is fixed by the "
            "order the assertions were recorded, so the same evidence always "
            "draws the same picture."))
    source_col = next((c for c in cols if "source" in c or "class" in c), None)
    rel_col = next((c for c in cols if "reliab" in c or "confid" in c or "strength" in c), None)
    rows = []
    for r in res.rows:
        d = dict(r)
        marker = ""
        if rel_col and d.get(rel_col) is not None:
            marker = f'<span class="tag">reliability: {esc(d[rel_col])}</span>'
        if source_col and d.get(source_col) is not None:
            marker = f'<span class="tag">source class: {esc(d[source_col])}</span>' + marker
        body = "<br>".join(f"<code>{esc(k)}</code>: {raw(v)}" for k, v in d.items())
        rows.append([marker or "(no source/reliability column found)", body])
    # The diagram above now carries the meaning; this is the exact
    # wording, for when that is what you need. Folded, not dropped -
    # same rule as the empty-state queries.
    out.append(details(
        f"{p}-evidence-verbatim",
        f"every assertion behind that diagram, verbatim ({len(rows)})",
        table(f"{p}-evidence",
              ["hop marker", "assertion (all columns, verbatim)"], rows)))
    out.append(prov(
        f"Rendered generically from {ev.table} - columns were read with "
        f"PRAGMA table_info at request time ({', '.join(cols)}) rather than assumed, "
        "because stage 5a may or may not be merged in this database."
    ))
    return "".join(out)


def _cents(dollars_value) -> Decimal | None:
    """notional_usd is stored in DOLLARS; dollars() takes CENTS."""
    try:
        return Decimal(str(dollars_value)) * 100
    except Exception:  # noqa: BLE001
        return None


def _conviction_gauge(value: float, p: str) -> str:
    """Where the model's conviction sat, and where the floor was.

    The number alone ("0.71") means nothing without the threshold it had
    to clear, which is the entire reason a trade happened or did not.
    """
    floor = 0.60          # display only - the live floor is adaptive
    pct = max(0.0, min(1.0, value)) * 100
    return (
        f'<div class="gauge" id="{p}-conviction">'
        f'<p class="gauge-title">Conviction {value:.2f}</p>'
        f'<div class="gauge-track">'
        f'<span class="gauge-fill" style="width:{pct:.1f}%"></span>'
        f'<span class="gauge-mark" style="left:{floor * 100:.0f}%"></span>'
        "</div>"
        f'<p class="prov">The marker is the conviction floor the candidate had '
        f"to clear ({floor:.2f} at the time of writing; it is an adaptive "
        "parameter and moves on scored outcomes). Left of the marker means "
        "the model was not confident enough for the code to size anything.</p>"
        "</div>")


def trace_page(db: Db, candidate_id: str, p: str = "tr") -> str:
    t = queries.decision_trace(db, candidate_id)
    if not t.candidate_q.rows:
        return section(f"{p}-section", "Decision trace",
                       empty_block(f"{p}-empty", t.candidate_q,
                                   meaning=f"no candidate with id {candidate_id!r}"))
    c = dict(t.candidate_q.rows[0])

    # --- dossier header: the verdict, before the reasoning behind it.
    view = dict(t.view_q.rows[0]) if t.view_q.rows else {}
    decision = dict(t.decisions_q.rows[0]) if t.decisions_q.rows else {}
    action = str(decision.get("action") or "").lower()
    verdict_state, verdict_word = {
        "trade": ("good", "traded"),
        "skip": ("idle", "declined"),
    }.get(action, ("idle", "no risk decision recorded"))
    conviction = view.get("conviction")
    try:
        conviction_f = float(conviction) if conviction is not None else None
    except (TypeError, ValueError):
        conviction_f = None
    direction = str(view.get("direction") or "&mdash;")
    closed = dict(t.closed_q.rows[0]) if t.closed_q.rows else {}

    header = [tiles(f"{p}-tiles", [
        ("Verdict", esc(action or "&mdash;").upper() or "&mdash;",
         f"{pill(verdict_state, verdict_word)} "
         + esc(str(decision.get("at") or "no timestamp"))),
        ("Model view", esc(direction),
         (f"conviction {conviction_f:.2f}" if conviction_f is not None
          else "no view recorded") + " - the model proposes, code disposes"),
        ("Size the code chose",
         dollars(_cents(decision.get("notional_usd"))) if decision.get("notional_usd")
         else "&mdash;",
         "set by the risk engine, never by the model"),
        ("Outcome",
         dollars(closed.get("realized_pnl_cents")) if closed else "open / none",
         esc(str(closed.get("exit_reason") or "no exit recorded"))),
    ])]
    if conviction_f is not None:
        header.append(_conviction_gauge(conviction_f, p))

    body = [
        "".join(header),
        f"<p class='prov' id='{p}-intro'>A single decision, start to finish. Someone "
        "who was not there should be able to read this page and understand why the "
        "trade was made or declined.</p>"
    ]
    body.append(_narrative_what_was_seen(t, p))
    body.append(_narrative_what_it_concluded(t, p))
    body.append(_narrative_what_risk_did(t, p))
    body.append(_narrative_what_happened(t, p))
    body.append(_narrative_evidence(t, p, db_for_graph=db,
                                   ticker=str(c.get('ticker') or '')))
    if t.refusal_q.rows:
        rows = [[esc(r["refused_at"]), esc(r["price_at_refusal"]),
                 esc(r["scored_at"] or "not scored yet"),
                 esc(r["outcome_price"] or "-"), esc(r["outcome_return"] or "-")]
                for r in t.refusal_q.rows]
        body.append("<h3>6. Refusal tracking - what this declined candidate went on to do</h3>")
        body.append(table(f"{p}-refusal",
                          ["refused at", "price at refusal", "scored at",
                           "outcome price", "outcome return"], rows,
                          numeric_cols={1, 3, 4}))
    return section(f"{p}-section",
                   f"Decision trace: {c['ticker']} ({c['catalyst_type']})", "".join(body))


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def refusals_panel(db: Db, p: str = "ref") -> str:
    r = queries.refusals(db)
    out = []
    if r.n_scored:
        share = 100.0 * r.n_positive / r.n_scored
        out.append(
            f"<p id='{p}-headline'><span class='big'>{r.mean_outcome_return:+.4f}</span> "
            f"mean outcome return across {r.n_scored} scored refusal(s); "
            f"{r.n_positive} of {r.n_scored} ({share:.0f}%) went on to move in the "
            "direction the system declined to take.</p>"
        )
        if r.n_scored < MIN_TRADES_FOR_MEANING:
            out.append(alarm(
                f"<b id='{p}-small-sample'>Too small to act on.</b> {r.n_scored} scored "
                f"refusal(s) against a {MIN_TRADES_FOR_MEANING} minimum. This number "
                "cannot yet move the conviction floor and must not be read as "
                "evidence the system is too strict."
            ))
    else:
        out.append(
            f"<p id='{p}-headline'>No refusal has been scored yet, so there is no "
            "answer to 'is it too strict' - only the question.</p>"
        )
    if r.query.is_empty:
        out.append(empty_block(f"{p}-empty", r.query,
                               meaning="no candidate has been declined and recorded"))
    else:
        rows = [[
            f'<a href="/decision?candidate_id={esc(x["candidate_id"])}">'
            f'{esc(x["ticker"] or x["candidate_id"])}</a>',
            esc(x["catalyst_type"] or "-"), esc(x["refused_at"]),
            esc(x["price_at_refusal"]),
            esc(x["scored_at"] or "not scored yet"),
            esc(x["outcome_price"] or "-"), esc(x["outcome_return"] or "-"),
            raw(", ".join(map(str, jload(x["skip_reasons"], []) or []))),
        ] for x in r.query.rows]
        out.append(table(
            f"{p}-table",
            ["candidate", "catalyst", "refused at", "price at refusal", "scored at",
             "outcome price", "outcome return", "why it was refused"],
            rows, numeric_cols={3, 5, 6},
        ))
        unscored = r.n_total - r.n_scored
        out.append(prov(
            f"{r.n_total} refusal(s) recorded, {r.n_scored} scored, {unscored} "
            "awaiting an outcome. Scoring is an async job that runs days or weeks "
            "later; 'not scored yet' is normal, not a fault."
        ))
    return section(f"{p}-section",
                   "Refusals, and what the declined candidates went on to do",
                   "".join(out))


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def logs_panel(db: Db, params: dict, p: str = "log") -> str:
    lg = queries.logs(
        db,
        level=params.get("level", ""), component=params.get("component", ""),
        q=params.get("q", ""), since=params.get("since", ""),
        until=params.get("until", ""),
        # NOT int() here: queries._log_limit owns the coercion, because a
        # hostile ?limit= must fall back, not raise (stage-8 stress).
        limit=params.get("limit", queries.DEFAULT_LOG_LIMIT),
    )
    level_opts = "".join(
        f'<option value="{esc(v)}"{" selected" if v == lg.filters["level"] else ""}>'
        f'{esc(v or "any level")}</option>'
        for v in [""] + (lg.levels or queries.LOG_LEVELS)
    )
    comp_opts = "".join(
        f'<option value="{esc(v)}"{" selected" if v == lg.filters["component"] else ""}>'
        f'{esc(v or "any component")}</option>'
        for v in [""] + lg.components
    )
    form = (
        f'<form id="{p}-form" method="get" action="/logs">'
        f'<label>level <select id="{p}-level" name="level">{level_opts}</select></label> '
        f'<label>component <select id="{p}-component" name="component">{comp_opts}'
        "</select></label> "
        f'<label>text <input id="{p}-q" name="q" value="{esc(lg.filters["q"])}" '
        'placeholder="substring of message, traceback or context"></label> '
        f'<label>since <input id="{p}-since" name="since" '
        f'value="{esc(lg.filters["since"])}" placeholder="2026-08-01"></label> '
        f'<label>until <input id="{p}-until" name="until" '
        f'value="{esc(lg.filters["until"])}" placeholder="2026-08-31"></label> '
        f'<label>limit <input id="{p}-limit" name="limit" size="4" '
        f'value="{esc(lg.filters["limit"])}"></label> '
        '<button type="submit">search</button> '
        '<a href="/diagnostics.json">download diagnostic bundle (redacted)</a>'
        "</form>"
    )
    out = [form]
    if not lg.available:
        out.append(f'<div class="empty" id="{p}-missing">{esc(lg.reason)}</div>')
        out.append(empty_block(f"{p}-empty-nolabel", lg.query,
                               meaning="the logs table itself is absent"))
        return section(f"{p}-section", "Logs", "".join(out))
    if lg.query.is_empty:
        out.append(empty_block(
            f"{p}-empty", lg.query,
            meaning="no log line matched these filters. Widen the window or clear "
                    "the text box; an empty result under a filter is not evidence "
                    "the component is silent.",
        ))
    else:
        rows = []
        for i, r in enumerate(lg.query.rows):
            extra = ""
            if r["traceback_text"]:
                extra += details(f"{p}-tb-{i}", "traceback", pre(r["traceback_text"]))
            if r["context_json"]:
                extra += details(f"{p}-ctx-{i}", "state at the time",
                                 pre(json_pretty(r["context_json"])))
            link = (f'<a href="/decision?candidate_id={esc(r["candidate_id"])}">trace</a>'
                    if r["candidate_id"] else "-")
            rows.append([esc(r["ts"]), esc(r["level"]), esc(r["component"]),
                         raw(r["message"]) + extra, esc(r["cycle_id"] or "-"), link])
        out.append(table(f"{p}-table",
                         ["time", "level", "component", "message", "cycle", "trace"], rows))
        out.append(prov(
            f"{lg.query.row_count} line(s), newest first, capped at "
            f"{lg.filters['limit']}. Every message, traceback and context blob on "
            "this page passes through the same redactor the diagnostic bundle uses."
        ))
    return section(f"{p}-section", "Logs", "".join(out))


# --------------------------------------------------------------------------
# Setup — STUB. Stage 7 owns the real credential flow.
# --------------------------------------------------------------------------


SETUP_MOUNT_POINT = "catalyst.dashboard.server: _route_setup()"


def setup_stub(p: str = "setup") -> str:
    body = (
        f'<div class="caveat" id="{p}-stub">'
        "<b>MOUNT POINT - NOT IMPLEMENTED HERE.</b> Stage 7 (integration-engineer) "
        "owns the credential setup flow: the form fields, the plain-English "
        "explanations, the per-field test-connection buttons, and writing the "
        "credentials file readable only by the service user. This page is the agreed "
        "place for it to attach."
        "</div>"
        f'<ul id="{p}-contract"><li>GET <code>/setup</code> renders the form '
        "(this stub).</li>"
        "<li>POST <code>/setup</code> accepts it and currently returns 501.</li>"
        "<li>Credentials are redacted at capture and never re-displayed; the "
        "dashboard already refuses to render any string matching a key pattern "
        "(catalyst/dashboard/redact.py), so a mistakenly stored key does not leak "
        "through the log view or the diagnostic bundle.</li>"
        f"<li>Code hook: <code>{esc(SETUP_MOUNT_POINT)}</code></li></ul>"
    )
    return section(f"{p}-section", "Setup and credentials (stage 7 mount point)", body)


# --------------------------------------------------------------------------
# Maintenance — is everything talking to everything?
# --------------------------------------------------------------------------


def _slug(text: str) -> str:
    """Stable per-check id. Row INDEX is not enough: the two tables both
    start at zero, which duplicated element ids across the page - the
    exact failure duplicate_ids() exists to catch."""
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in text).strip("-")


_STATE_PILL = {"ok": ("good", "online"), "warn": ("warn", "attention"),
               "fail": ("crit", "problem"), "unknown": ("idle", "not set up")}


def maintenance_panel(report, p: str = "maint") -> str:
    """Render a MaintenanceReport. Pure: it does no I/O of its own, so
    the route decides whether outside services get contacted."""
    out = []
    counts = {k: sum(1 for c in report.checks if c.state == k)
              for k in ("ok", "warn", "fail", "unknown")}
    worst_state, worst_word = _STATE_PILL[report.worst]
    headline = {"ok": "Everything is talking",
                "warn": "Working, with something to look at",
                "fail": "Something is broken",
                "unknown": "Not fully set up yet"}[report.worst]
    out.append(tiles(f"{p}-tiles", [
        ("Overall", esc(headline),
         f"{pill(worst_state, worst_word)} {len(report.checks)} checks run"),
        ("Online", str(counts["ok"]),
         "parts that answered normally"),
        ("Need attention", str(counts["fail"] + counts["warn"]),
         f"{counts['fail']} problem(s), {counts['warn']} warning(s), "
         f"{counts['unknown']} not set up"),
    ]))

    if not report.ran_active:
        out.append(prov(
            "Showing what the bot has recorded. Outside services were NOT "
            "contacted for this view - press the button below to check them "
            "live. Every one of those checks is free: Alpaca and EDGAR cost "
            "nothing, and the Anthropic check reads your bill rather than "
            "using the model."))
    out.append(
        f'<form class="inline" id="{p}-run" method="get" action="/maintenance">'
        '<input type="hidden" name="check" value="now">'
        "<button type=\"submit\">Check outside services now</button></form>")

    # Sending the evidence somewhere. The bundle existed from stage 6 but
    # had no way in from the UI, so in practice it did not exist (owner
    # asked where to export logs). `download` makes the browser save it
    # as a file rather than render a wall of JSON.
    out.append(
        f'<p id="{p}-bundle"><a href="/diagnostics.json" download='
        '"catalyst-diagnostics.json"><b>Download diagnostic bundle</b></a> '
        "&mdash; one file with the recent logs, every error, the funnel, "
        "the cost ledger and this page's checks. Safe to send on: keys and "
        "secrets are stripped out of it twice, once where each value is "
        "captured and again over the whole file before it is written.</p>")
    out.append(prov(
        "If the page itself will not load, the same file can be produced "
        "on the server with:  sudo -u catalyst /opt/catalyst/venv/bin/python "
        "-m catalyst.dashboard --diagnostics > catalyst-diagnostics.json"))

    for group in ("The bot itself", "Outside services"):
        checks = report.by_group(group)
        if not checks:
            continue
        out.append(f"<h3>{esc(group)}</h3>")
        rows = []
        for c in checks:
            state, word = _STATE_PILL[c.state]
            latency = f"{c.latency_ms} ms" if c.latency_ms is not None else "-"
            rows.append([
                esc(c.name), pill(state, word), esc(c.summary), latency,
                f'<span class="prov">{esc(c.detail)}</span>'
                + (details(f"{p}-raw-{_slug(c.name)}", "raw response",
                            pre(c.raw)) if c.raw else ""),
            ])
        out.append(table(f"{p}-table-{group.split()[-1]}",
                         ["part", "state", "what it says", "took",
                          "what it means"], rows))

    out.append(prov(
        f"Generated {esc(report.generated_at)}. Nothing on this page changes "
        "any setting or places any order; it only looks."))
    return section(f"{p}-section",
                   "Maintenance: is everything communicating?", "".join(out))


# --------------------------------------------------------------------------
# Broker value vs net value - two different numbers, on purpose
# --------------------------------------------------------------------------


def value_reconciliation_panel(db: Db, p: str = "val") -> str:
    """What Alpaca says the account is worth, what it is worth after the
    API bill, and every line between them.

    Owner asked to see the difference clearly. These two figures are
    SUPPOSED to differ and the page must say why, or the reader assumes
    one of them is wrong:
      - Alpaca marks OPEN positions to market; the dashboard's net value
        counts only profit actually banked on closed trades.
      - Alpaca has never heard of the Anthropic bill; the dashboard
        deducts it, because that half is real money even on paper.
    """
    perf = queries.performance(db)
    brok = queries.broker_equity(db)
    out = []

    net_cents = perf.net_equity_cents
    if brok.rows:
        r = dict(brok.rows[0])
        broker_cents = (Decimal(str(r["equity_usd"])) * 100).quantize(Decimal("1"))
        as_of = str(r["taken_at"])
        banked = Decimal(START_CAPITAL_CENTS) + perf.gross_pnl_cents
        unrealised = broker_cents - banked      # broker marks minus banked
        costs = perf.scheduled_cost_cents + perf.manual_cost_cents
        gap = broker_cents - net_cents
        out.append(tiles(f"{p}-tiles", [
            ("Alpaca account value", dollars(broker_cents),
             f"{pill('idle', 'broker read')} as of {esc(as_of)}"),
            ("Net value after costs", dollars(net_cents),
             f"{pill('idle', 'this dashboard')} banked profit less the API bill"),
            ("Difference", dollars(gap),
             "unrealised marks plus API spend - explained line by line below"),
        ]))
        out.append(table(f"{p}-bridge", ["line", "amount", "why it differs"], [
            ["Alpaca account value", dollars(broker_cents),
             "what the broker says the account is worth right now, including "
             "open positions marked to market"],
            ["less profit not yet banked", "-" + dollars(unrealised),
             "open positions can still move; this dashboard counts a trade "
             "only once it has closed"],
            ["less API spend to date", "-" + dollars(costs),
             "Alpaca has never heard of the Anthropic bill. Paper P&amp;L is "
             "fictional; this half is real money"],
            ["<b>= net value, the line on the chart</b>",
             "<b>" + dollars(net_cents) + "</b>",
             "<b>the only figure that can honestly be compared with the "
             "S&amp;P</b>"],
        ], numeric_cols={1}))
        out.append(prov(
            "Broker figure from equity_snapshots where source='broker_read' "
            f"({brok.row_count} row read, newest first). Net value from "
            "closed_trades and cost_events, priced locally. The two are "
            "expected to differ; a gap is not an error."))
    else:
        out.append(tiles(f"{p}-tiles", [
            ("Alpaca account value", "&mdash;",
             f"{pill('idle', 'not read yet')} no broker snapshot recorded"),
            ("Net value after costs", dollars(net_cents),
             f"{pill('idle', 'this dashboard')} banked profit less the API bill"),
            ("Difference", "&mdash;", "needs a broker read to compare against"),
        ]))
        out.append(empty_block(
            f"{p}-empty", brok,
            meaning="equity_snapshots is written once per cycle from a "
                    "confirmed broker read; until the first cycle runs with "
                    "credentials there is nothing to compare against.",
        ))
    return section(f"{p}-section",
                   "Broker value vs net value", "".join(out))
