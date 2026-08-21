"""Page bodies. Each panel takes a Db and returns HTML.

Panels take an `p` (id prefix) argument wherever they can appear beside
another instance of themselves, so element ids stay unique on a page —
duplicated ids once meant one panel silently received another's data and
both rendered blank.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from catalyst.dashboard import charts, queries
from catalyst.dashboard.db import Db, jload
from catalyst.discovery.conjunctions import sector_band
from catalyst.dashboard.render import (
    BAKEOFF_CAVEAT,
    MIN_TRADES_FOR_MEANING,
    PAPER_PNL_CAVEAT,
    SURVIVORSHIP_CAVEAT,
    alarm,
    caveat,
    caveat_fold,
    caveat_html,
    details,
    dollars,
    empty_block,
    zero_block,
    esc,
    figcap,
    json_pretty,
    meter,
    note,
    ok,
    pill,
    pre,
    prov,
    prov_html,
    raw,
    section,
    signed_pct,
    table,
    tiles,
)

# --------------------------------------------------------------------------
# Performance vs the S&P 500 — the top element on the page
# --------------------------------------------------------------------------


#: The em dash, as a LITERAL character. Passing "&mdash;" through esc()
#: renders "&AMP;MDASH;" as visible text - a real owner-reported bug.
DASH = "\u2014"


#: The refusal that never fixes itself. refresh_benchmark names it
#: exactly so this page can tell it apart from an outage.
_FEED_REFUSED = ("feed_no_longer_available", "feeds_refused_http")


def _spy_rebuild_offer(perf, p: str) -> str:
    """The one fault on this page with a button, and only when it is
    that fault.

    OWNER-REPORTED: "the SPY comparison line has disappeared", days
    after replacing the Alpaca keys. A cache built on `sip` keeps asking
    for `sip`; a key without that subscription is refused every time and
    the comparison never returns. Waiting cannot fix it, so telling the
    owner to wait is the wrong answer.

    OFFERED, NEVER TAKEN AUTOMATICALLY. The rebuild discards real
    history, and the pin it removes exists for a good reason - a series
    half consolidated tape and half one exchange's prints makes every
    comparison against it quietly wrong. So it needs a typed
    confirmation, and it appears only when the stored reason says the
    feed itself was refused. A generic outage gets no button, because
    for that one waiting IS the answer.
    """
    reason = str(perf.spy_error or "")
    if not any(marker in reason for marker in _FEED_REFUSED):
        return ""
    return (
        '<form class="danger-form" method="post" '
        f'action="/rebuild-benchmark" id="{p}-spy-rebuild">'
        "<p><b>Your market-data key can no longer read the feed this "
        "series was built on.</b> That is why it stopped updating, and "
        "it will not recover by itself - every refresh asks for the same "
        "refused feed.</p>"
        "<p>Rebuilding fetches the history again on a feed your current "
        "keys can reach. It <b>discards the stored series</b> rather "
        "than splicing a second feed onto it, because a benchmark on two "
        "different bases makes every comparison against it quietly "
        "wrong. The page will name the feed it lands on.</p>"
        '<label for="' + p + '-spy-confirm">Type <code>REBUILD</code> '
        "to confirm</label> "
        f'<input id="{p}-spy-confirm" name="confirm" autocomplete="off" '
        'placeholder="REBUILD" required>'
        '<button type="submit">Rebuild the SPY series</button>'
        "</form>")


def _baseline_words(base) -> str:
    """Where the comparison's money and start date came from, in one
    phrase. Never "$1,000" with no attribution: on this page a figure
    that cannot say where it came from is not allowed to exist."""
    return {
        "owner_set": "you set this on the Maintenance page",
        "first_run": "struck from the broker account's own equity the "
                     "first time it was read",
        "account_changed": "the connected Alpaca account changed, so the "
                           "comparison restarted from its equity",
        "unset": "PLACEHOLDER - nothing has been recorded, so this is the "
                 "documented fallback rather than a decision",
    }.get(base.source, f"recorded with source {base.source}")


def _baseline_block(perf, p: str) -> str:
    """What SPY is bought with, from when, and why - stated, not assumed.

    This used to be a constant (START_CAPITAL_CENTS = 100_000) behind
    every figure on this panel. A $2,000 account against a $1,000 base is
    wrong by 100% in whichever direction flatters or damns at random, and
    nothing on the page would have said so.
    """
    base = perf.baseline
    if base is None:
        return ""
    body = []
    if base.is_placeholder:
        # Not an alarm: on a fresh install this is the correct state and
        # it fixes itself the first time the broker is read. It is a
        # note, because presenting a placeholder as a decision is the
        # thing the brief forbids.
        body.append(note(
            f'<b id="{p}-baseline-placeholder">Every figure here is measured '
            f"against a PLACEHOLDER of {dollars(base.capital_cents)}, not "
            "against a real baseline.</b> No broker account has been read "
            "and none has been set by hand, so the dashboard is using "
            "<code>benchmark.FALLBACK_CAPITAL_CENTS</code>. It is replaced "
            "automatically the first time the bot reads the Alpaca account, "
            "or immediately if you set one on the "
            '<a href="/maintenance#bench-section">Maintenance page</a>. '
            f"Reason recorded: <code>{esc(base.reason)}</code>"))
    else:
        body.append(prov(
            f"Baseline: {dollars(base.capital_cents)} from "
            f"{base.start_date}, source '{base.source}' ({_baseline_words(base)}), "
            f"recorded {base.set_at or 'time not recorded'}"
            + (f", account fingerprint {base.account_fingerprint}"
               if base.account_fingerprint else "")
            + f". Why: {base.reason}"))
    if perf.excluded_trades or perf.excluded_cost_cents:
        # Money that was really made and really spent does not get to
        # vanish because a new baseline was struck. It is outside the
        # window, and the page says so with the numbers.
        body.append(note(
            f'<b id="{p}-baseline-excluded">'
            f"{perf.excluded_trades} closed trade(s) and "
            f"{dollars(perf.excluded_cost_cents)} of API spend are dated "
            f"before {base.start_date} and are OUTSIDE this comparison.</b> "
            f"Their realised P&amp;L was {dollars(perf.excluded_pnl_cents)}. "
            "A baseline struck from a broker read already contains the "
            "profit that account has banked, so counting those trades again "
            "would book the same money twice. They are still on the "
            '<a href="/chain">decisions</a> page, and the older baseline '
            'is still in the history on the <a href="/maintenance'
            '#bench-section">Maintenance page</a>.'))
    return "".join(body)


def performance_panel(db: Db, p: str = "perf") -> str:
    perf = queries.performance(db)
    base = perf.baseline
    start_dollars = float(perf.start_capital_cents) / 100.0
    start_text = f"${start_dollars:,.0f}"
    out = []

    if perf.bot_points:
        excess = perf.excess_pp
        cls = "pos" if (excess or 0) >= 0 else "neg"
        if excess is not None:
            # signed_pct, not signed_pp. The "%" fix landed on the TILE
            # and missed this sentence, so the headline still said "pp"
            # - the exact jargon the owner asked to be rid of.
            excess_text = f'<span class="{cls}">{esc(signed_pct(excess))}</span>'
        elif perf.spy_window_too_short:
            # The tile beside this already says "too early to compare".
            # The headline shouted "unavailable" in alarm red at the same
            # time, so the page contradicted itself about whether
            # anything was wrong (found by stress-testing the rendered
            # pages, not by reading the code).
            excess_text = '<span class="muted-fig">not yet</span>'
        else:
            excess_text = '<span class="neg">unavailable</span>' 
        # BOTH PERCENTAGES, THEN THE GAP, THEN THE MONEY. Owner-
        # reported: "still dont understand what beating it by 0.89pp
        # means, can you just show a percentage symbol equivalent".
        #
        # A gap on its own is only readable if you can see what it is a
        # gap BETWEEN - and the dollar line is the one nobody has to
        # translate at all.
        sides = ""
        money = ""
        if perf.bot_index is not None and perf.spy_index is not None:
            you, spy = perf.bot_index - 100.0, perf.spy_index - 100.0
            sides = (f' <b>You {you:+.2f}%</b>, '
                     f"<b>SPY {spy:+.2f}%</b>.")
            try:
                # `excess`, NOT `excess_v`. excess_v is bound further
                # down in the tiles block, so naming it here raised
                # UnboundLocalError and took out BOTH /performance and
                # the Overview on the owner's machine.
                #
                # It shipped because no test rendered this panel with a
                # bot series AND a SPY series present - the only state
                # in which this branch runs at all.
                start = Decimal(perf.start_capital_cents or 0)
                if start > 0 and excess is not None:
                    diff = start * Decimal(str(excess)) / 100
                    money = (" In money, that is "
                             f"<b>{dollars(diff.copy_abs())}</b> "
                             + ("more" if excess >= 0 else "less")
                             + " than the same cash in SPY would have been.")
            except (ArithmeticError, TypeError, ValueError):
                money = ""
        out.append(
            f'<p id="{p}-headline"><span class="big">{excess_text}</span> '
            f"against SPY since {esc(perf.start_day)}, net of all API "
            f"spend.{sides}{money}</p>"
        )
        bot_text = (f"bot index {perf.bot_index:.2f} "
                    f"(= {dollars(perf.net_equity_cents)} on a {start_text} "
                    f"start)")
        if perf.spy_index is not None:
            spy_text = f"SPY index {perf.spy_index:.2f}"
        elif perf.spy_window_too_short:
            spy_text = ("no SPY comparison yet - the account is younger than "
                        "one trading day")
        else:
            spy_text = "SPY index unavailable, see the benchmark note below"
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
            state, word = "idle", ("too early to compare - needs a trading day"
                                   if perf.spy_window_too_short
                                   else "no benchmark to compare against")
        elif excess_v >= 0:
            state, word = "good", "ahead of SPY"
        else:
            state, word = "crit", "behind SPY"
        # PERCENT, NOT "pp". Owner-reported: "still dont understand what
        # beating it by 0.89pp means, can you just show a percentage
        # symbol equivalent". Percentage POINTS is the technically
        # correct unit for the gap between two percentages, and it is
        # jargon - a figure nobody can read is not a figure. The two
        # underlying percentages sit beside it so the gap is checkable
        # rather than taken on trust.
        headline_tile = (f'<span class="{"pos" if (excess_v or 0) >= 0 else "neg"}">'
                         f"{esc(signed_pct(excess_v))}</span>")
        # NOT "exposure-matched". It never was, and the correction was
        # sitting in a provenance line that section() sweeps into a fold
        # at the foot of the page - so the headline made a claim the
        # page quietly contradicted where nobody would look.
        #
        # Owner-reported: "i dont want the false idea we are beating
        # SPY". This is exactly how that idea forms, and the numbers
        # make it concrete: an account holding one position and ~80%
        # cash falls less than a fully invested index in every down
        # market. That is not skill, it is not being in the market.
        # The pointer only exists when the thing it points at does.
        # _exposure_warning renders only where there IS a SPY series,
        # so promising it unconditionally sends the reader looking for
        # a paragraph that is not on the page.
        headline_sub = f"{pill(state, word)} net of API spend"
        if perf.spy_points:
            headline_sub += " &mdash; but see the exposure warning below"
        equity_tile = dollars(perf.net_equity_cents)
        equity_sub = (
            (f"{pill('idle', 'placeholder baseline')} " if perf.baseline_is_placeholder
             else f"{pill('good', 'baseline ' + esc(base.source).replace('_', ' '))} ")
            + f"from {start_text} at {esc(perf.start_day)}")
    else:
        headline_tile = "&mdash;"
        headline_sub = f"{pill('idle', 'no closed trades yet')} nothing to compare"
        equity_tile = dollars(perf.net_equity_cents)
        equity_sub = (
            (f"{pill('idle', 'placeholder baseline')} " if perf.baseline_is_placeholder
             else f"{pill('good', 'baseline ' + esc(base.source).replace('_', ' '))} ")
            + f"{start_text} baseline, no equity series yet")
    sample_state = ("good" if perf.n_closed >= MIN_TRADES_FOR_MEANING
                    else ("idle" if perf.n_closed == 0 else "warn"))
    out.append(tiles(f"{p}-tiles", [
        ("Excess vs SPY", headline_tile, headline_sub),
        ("Account value", equity_tile, equity_sub),
        ("Closed trades", str(perf.n_closed),
         f"{pill(sample_state, f'{perf.n_closed} of {MIN_TRADES_FOR_MEANING}')} "
         "needed before any number here means anything"),
    ]))

    # THE ACCOUNT-VALUE TILE, TAKEN APART. One figure stood for the
    # money you started with, paper trading profit and a real API bill;
    # only one of those has actually left a card.
    out.append(_equity_bridge(perf, p))

    # WHAT THE COMPARISON IS AGAINST, before any number is read.
    out.append(_baseline_block(perf, p))

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
        out.append(charts.index_chart(
            series, chart_id=f"{p}-chart", x_labels=x_labels,
            start_capital_dollars=start_dollars,
            y_axis_title=("Index (start = 100)  |  % move  |  "
                          f"$ on a {start_text} account")))
        out.append(prov(
            "Y axis reads three ways on every tick: index (start=100), the same move "
            f"in per cent, and the dollar value on the {start_text} baseline. "
            f"100 on this chart is {start_text}, not a bug. The dollar column "
            "follows the baseline, so it changes when the baseline does."
        ))
        if perf.flat_since_baseline:
            out.append(note(
                f'<b id="{p}-flat">The bot line is flat on purpose.</b> '
                f"Nothing has closed and nothing has been billed since the "
                f"baseline was struck on {esc(base.start_date)}, so realised "
                "equity has not moved. SPY has. That gap is the comparison "
                "working, not a missing series - open positions are not "
                "marked into this line, which is why the broker's own figure "
                "below can differ."))
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
    baseline_label = (
        "starting capital (PLACEHOLDER - no baseline recorded)"
        if perf.baseline_is_placeholder else
        f"starting capital, baseline of {esc(base.start_date)} "
        f"({esc(base.source).replace('_', ' ')})")
    rows = [
        [baseline_label, dollars(perf.start_capital_cents)],
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
        f"({perf.closed_q.row_count} rows read, {perf.n_closed} of them inside "
        f"the baseline window). API spend is the LOCAL "
        f"ledger, priced by cost.tracker.price() from stored raw usage objects "
        f"({perf.costs_q.row_count} priced cost_events rows) - locally priced, not "
        "billed; the billed figure appears on the Cost page for closed days only."
    ))

    # Benchmark provenance, and its absence made loud.
    if perf.spy_points and perf.spy_feed and perf.spy_feed != "sip":
        out.append(caveat(
            f"This SPY line comes from the {perf.spy_feed.upper()} feed, not "
            "the consolidated tape. Alpaca only sells the full tape on a paid "
            "data plan, and this account does not have one, so the bot uses "
            "the best feed it can read. For a daily close on something as "
            "liquid as SPY the difference is small - but it is a different "
            "basis, and the comparison is only as good as that."))
    if perf.spy_points:
        out.append(_exposure_warning(perf, p))
        out.append(prov(
            f"Benchmark: SPY, {len(perf.spy_points)} daily closes from "
            f"{perf.spy_source}, indexed to 100 on the same day as the bot "
            "line, total return (adjustment=all) so dividends are included."
        ))
    elif perf.spy_stale:
        # A DIFFERENT PROBLEM, and the only one of the two anybody can
        # act on: the once-a-day Alpaca refresh has stopped working, so
        # the cache is behind the comparison window and no amount of
        # waiting fixes it. Rendered as an alarm precisely because the
        # short-window case below is deliberately NOT one - if both read
        # the same, a real outage gets left for a week.
        out.append(alarm(
            f'<b id="{p}-spy-stale">The SPY benchmark cache is out of '
            "date, and this one does need looking at.</b> "
            f"{perf.spy_error or ''} Until it refreshes there is no S&amp;P "
            "comparison, though nothing about trading is affected - the "
            "benchmark is reporting only."))
        out.append(_spy_rebuild_offer(perf, p))
    elif perf.spy_window_too_short:
        # Healthy cache, window shorter than one trading day. An alarm
        # here taught the owner to distrust a working benchmark.
        out.append(note(
            f'<b id="{p}-spy-early">No SPY comparison yet, and nothing is '
            "wrong.</b> The benchmark cache is healthy - "
            f"{perf.spy_rows} daily closes are loaded from "
            f"<code>{esc(perf.spy_source or 'the local bar cache')}</code> - but "
            f"the bot's own history so far ({esc(perf.start_day)} to "
            f"{esc(perf.end_day)}) does not yet contain a completed trading "
            "day to index against. A weekend or a first Monday looks exactly "
            "like this. It fills in on its own once the market has closed on "
            "a day the bot was running. Raw reason: "
            f"<code>{esc(perf.spy_error or 'unknown')}</code>."
        ))
    else:
        out.append(alarm(
            f'<b id="{p}-spy-missing">SPY benchmark unavailable.</b> source tried: '
            f"<code>{esc(perf.spy_source or 'local bar cache')}</code>; rows usable: "
            f"{perf.spy_rows}. Raw reason: <code>{esc(perf.spy_error or 'unknown')}</code>. "
            "This is why the excess figure above reads unavailable rather than 0. "
            "The bot refreshes this cache once a day from Alpaca; if this "
            "persists, the Maintenance page shows whether Alpaca is reachable."
        ))
        out.append(_spy_rebuild_offer(perf, p))
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


#: Upstream failures the owner should read as sentences, not as markup.
_FAULT_GISTS = (
    ("Request Rate Threshold Exceeded",
     "sec.gov rate-limited this machine. The bot stops calling SEC "
     "endpoints for 15 minutes on purpose - requesting during the "
     "timeout extends it. It resumes on its own; nothing to do."),
    ("Undeclared Automated Tool",
     "sec.gov rejected the request as an undeclared automated tool - "
     "the contact User-Agent is missing or malformed."),
    ("AccessDenied",
     "the file is not published (a weekend, a holiday, or before the "
     "evening publish). Not a fault."),
    ("timeout", "the source did not answer in time."),
)


def _fault_gist(detail) -> str:
    """One readable sentence for an upstream failure.

    A raw body is evidence, not an explanation. sec.gov's block page is
    a 4KB HTML document; printing it where a sentence belongs is how
    the panel became unreadable.
    """
    text = str(detail or "")
    for marker, gist in _FAULT_GISTS:
        if marker.lower() in text.lower():
            return gist
    stripped = " ".join(re.sub(r"<[^>]+>", " ", text).split())
    return (stripped[:200] + "...") if len(stripped) > 200 else (
        stripped or "no detail was returned")


def funnel_panel(db: Db, p: str = "funnel") -> str:
    """Where every candidate ended up, as one narrowing population.

    Rebuilt 2026-08-11 on the owner's report that it was "very confusing
    on what its actually doing and it is still error 400". See
    queries.funnel for the three defects behind that. This side of it:

    - each step reads "N arrived -> M continued", so the arithmetic is
      visible rather than expressed as a percentage that could exceed
      100;
    - a candidate stopping because the model or the risk engine said no
      is the system WORKING, and is drawn in neutral text. Only genuine
      faults - a feed that would not read, spending blocked, an approved
      trade with no order - are drawn as faults;
    - anything the recorded reasons do not account for is stated as an
      unexplained residual instead of quietly not adding up.
    """
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

    out.append(note(
        "Read this top to bottom: it follows <b>one</b> group of candidates "
        "and shows how many were still in the running after each step. A "
        "step can only ever remove candidates, never add them, so each "
        "number is smaller than the one above it. <b>Candidates stopping is "
        "normal and is most of what this page shows</b> &mdash; a few "
        "trades a month out of many candidates is the design, not a fault. "
        "Only the items marked <span class=\"fault-chip\">NEEDS ATTENTION</span> "
        "are things going wrong."))

    first = data.stages[0].count if data.stages else 0
    last = data.stages[-1].count if data.stages else 0
    rate = f"{(100.0 * last / first):.0f}%" if first else "&mdash;"
    out.append(tiles(f"{p}-tiles", [
        ("Candidates built", str(first), "every dated, tradeable event found"),
        ("Reached an order", str(last), "what survived all four steps below"),
        ("Of those, traded", rate,
         f"{last} of {first} - each step's losses are itemised below"),
    ]))

    widest = max((s.entered for s in data.stages), default=0) or 1
    starved_shown = False
    for i, stage in enumerate(data.stages):
        pct = 100.0 * stage.count / widest
        width = max(0.0, min(100.0, pct))
        # "N arrived, M continued" beats a percentage: it is checkable by
        # eye and it cannot read as 200%.
        if i == 0:
            flow = f'<span class="funnel-flow">{stage.count} to start</span>'
        elif stage.entered == 0:
            flow = ('<span class="funnel-flow quiet">nothing reached this '
                    "step</span>")
        else:
            lost = stage.left
            cls = " lost" if lost else ""
            flow = (f'<span class="funnel-flow{cls}">{stage.entered} arrived '
                    f'&rarr; <b>{stage.count}</b> continued'
                    + (f" &middot; {lost} stopped here" if lost else "")
                    + "</span>")
        bar = ("" if stage.count == 0 else
               f'<span class="funnel-bar" style="width:{width:.1f}%" '
               f'title="{esc(stage.label)}: {stage.count} of {widest}"></span>')
        out.append(
            f'<div class="funnel-step" id="{p}-row-{esc(stage.key)}">'
            f'<div class="funnel-head">'
            f'<span class="funnel-num">{i + 1}</span>'
            f'<span class="funnel-label">{esc(stage.label)}</span>'
            f'<span class="funnel-n">{stage.count}</span></div>'
            f'<div class="funnel-track">{bar}</div>'
            f"{flow}"
            f'<p class="funnel-plain">{esc(stage.plain)}</p>')

        if stage.drops or getattr(stage, "stale_drops", None):
            # COLOUR STILL FOLLOWS "IS THIS STILL HAPPENING". A reason not
            # seen for days is history and must stop wearing the colour
            # that means something is wrong right now - the owner read a
            # wall of 400s as a live fault days after the bug was fixed.
            # ONE wrapper around the reason AND its provenance. The <li>
            # is a two-column grid (2.4em 1fr); a bare text node becomes
            # an anonymous THIRD grid item, which pushed the provenance
            # onto a second row and into the 2.4em count column - it
            # rendered one word per line (owner-reported, with a
            # screenshot). Wrapping is structural, so the next element
            # added here cannot fall into the same trap.
            # THE KIND DECIDES THE STYLING, not a substring of the
            # date text. "The market was closed" and an HTTP 400 are not
            # the same news and must not look the same; matching on
            # wording also broke the moment the wording changed.
            def _chip(reason, _key=stage.key):
                # STAGE-AWARE. "Unknown means broken" is true of the
                # research stage's machine codes and false of every
                # judgement stage, where an unknown reason is the bot
                # deciding not to trade.
                kind = queries.skip_kind(reason, _key)
                if kind == "ROUTINE":
                    return ("drop-routine",
                            '<span class="drop-tag">routine</span> ')
                if kind == "LIMIT":
                    return ("drop-limit",
                            '<span class="drop-tag">a limit</span> ')
                return ("drop-live",
                        '<span class="drop-tag drop-tag-fault">fault</span> ')

            items = ""
            for reason, n, detail in stage.drops:
                cls, tag = _chip(reason)
                items += (
                    f'<li class="{cls}">'
                    f'<span class="funnel-why-n">{esc(n)}</span>'
                    '<span class="funnel-why-text">' + tag + esc(reason)
                    + (f' <span class="prov">{raw(detail)}</span>'
                       if detail else "")
                    + "</span></li>")
            if stage.drops:
                out.append(
                    f'<div class="funnel-why" id="{p}-drops-{esc(stage.key)}">'
                    f"<h3>Why they stopped here</h3>"
                    "<p class='prov'>Tagged <b>routine</b> where nothing went "
                    "wrong and the bot was working as designed - declining "
                    "a candidate is the commonest correct thing it does - "
                    "<b>a limit</b> where a bound did its job, and "
                    "<b>fault</b> where something actually broke. Only the "
                    "last kind is worth your attention.</p>"
                    f"<ul>{items}</ul>")
            else:
                # NOTHING CURRENT IS NOT NOTHING AT ALL. Everything here
                # has settled, and saying so is the reassuring half of
                # this panel - a silent gap reads as a broken query.
                out.append(
                    f'<div class="funnel-why" id="{p}-drops-{esc(stage.key)}">'
                    f"<h3>Why they stopped here</h3>"
                    "<p class='prov'>Nothing is currently stopping candidates "
                    "here. Everything recorded at this step has settled and "
                    "is filed below.</p><ul></ul>")
            # LEGACY, OUT OF THE WAY BUT NOT GONE. Owner-reported: "If
            # these errors are legacy why are they still visible taking
            # space? I want them if relevant not legacy." Settled
            # reasons outside the fault window collapse into a
            # disclosure: the page shows what is current, and the record
            # stays one click away.
            stale = getattr(stage, "stale_drops", None) or []
            if stale:
                older = "".join(
                    f'<li class="drop-routine">'
                    f'<span class="funnel-why-n">{esc(n)}</span>'
                    '<span class="funnel-why-text">' + esc(reason)
                    + (f' <span class="prov">{raw(detail)}</span>'
                       if detail else "")
                    + "</span></li>"
                    for reason, n, detail in stale)
                out.append(
                    f'<details id="{p}-drops-old-{esc(stage.key)}">'
                    f"<summary>{len(stale)} older reason(s), settled and "
                    f"not seen for over {queries.FEED_FAULT_WINDOW_DAYS} "
                    "day(s) - kept for the record</summary>"
                    f"<ul class='funnel-why-list'>{older}</ul>"
                    "<p class='prov'>Each of these stopped happening and "
                    "the bot has worked past it. They are here because a "
                    "fault that disappears silently cannot be told apart "
                    "from one that never happened.</p></details>")
            explained = 0
            for _r, n, _d in stage.drops:
                try:
                    explained += int(n)
                except (TypeError, ValueError):
                    pass
            residual = stage.left - explained
            if residual > 0:
                # SAY WHAT DOES NOT ADD UP. The old panel listed reasons
                # summing to four beside a drop of one and left the reader
                # to notice.
                out.append(
                    f'<p class="prov" id="{p}-residual-{esc(stage.key)}">'
                    f"{residual} of the {stage.left} that stopped here have no "
                    "recorded reason. That is a gap in the record, not a "
                    "category.</p>")
            elif residual < 0:
                out.append(
                    f'<p class="prov" id="{p}-residual-{esc(stage.key)}">'
                    f"These reasons add up to {explained} against {stage.left} "
                    "that stopped: one candidate can be refused for several "
                    "reasons at once, so this list explains why rather than "
                    "dividing them up.</p>")
            out.append("</div>")
        elif stage.left > 0:
            out.append(
                f'<p class="prov" id="{p}-nodrops-{esc(stage.key)}">'
                f"{stage.left} stopped here with no reason recorded &mdash; "
                "that is a gap in the record.</p>")

        if stage.faults:
            # A fault is (reason, n, detail) and MAY carry a fourth
            # element, (href, label): the page that can actually clear
            # it. The advice text itself goes through raw() and so is
            # escaped - the link has to be built here, from code
            # constants, or it renders as literal &lt;a&gt; on screen.
            items = "".join(
                f'<li><span class="funnel-why-n">{esc(f[1])}</span>'
                + '<span class="funnel-why-text">' + esc(f[0])
                + (f' <span class="prov">{raw(f[2])}</span>' if f[2] else "")
                + (f' <a class="fault-fix" href="{esc(f[3][0])}">'
                   f'{esc(f[3][1])}</a>'
                   if len(f) > 3 and f[3] else "")
                + "</span></li>"
                for f in stage.faults)
            out.append(
                f'<div class="funnel-fault" id="{p}-faults-{esc(stage.key)}">'
                '<h3><span class="fault-chip">NEEDS ATTENTION</span> '
                f"Things that went wrong here</h3><ul>{items}</ul>"
                "<p class='prov'>Only blocks whose cause is still in "
                "force. One that has since cleared is listed as history "
                "below, not here.</p></div>")

        # RESOLVED, and said so. A block already lifted must not wear the
        # same orange as a live one - the owner went to acknowledge a
        # pause that had already been cleared and found nothing to click.
        if getattr(stage, "healed", None):
            done = "".join(
                f'<li><span class="funnel-why-n">{esc(h[1])}</span>'
                + '<span class="funnel-why-text">' + esc(h[0])
                + (f' <span class="prov">{raw(h[2])}</span>' if h[2] else "")
                + "</span></li>"
                for h in stage.healed)
            out.append(
                f'<div class="funnel-why" id="{p}-healed-{esc(stage.key)}">'
                "<h3>Blocked earlier, running again now</h3>"
                f"<ul>{done}</ul>"
                "<p class='prov'>Nothing to do. Kept visible because a "
                "fault that vanishes silently is indistinguishable from "
                "one that never happened.</p></div>")

        # A step starved by the one above it is a CONSEQUENCE, not a
        # finding. Only the first starved step gets the full empty-state
        # with its query; the rest get one quiet line, with the query
        # still one click away so nothing is actually hidden. Six
        # identical SQL dumps is what buried this page before.
        if stage.entered == 0 and stage.count == 0:
            if not starved_shown:
                starved_shown = True
                out.append(zero_block(
                    f"{p}-empty-{esc(stage.key)}", stage.query,
                    meaning=stage.plain or "",
                ))
            else:
                err = (f' <span class="funnel-drop">query FAILED: '
                       f"{esc(stage.query.error)}</span>"
                       if stage.query.error else "")
                out.append(
                    f'<div class="quiet" id="{p}-starved-{esc(stage.key)}">'
                    "nothing reached this stage &mdash; the shortfall is "
                    f"above, not here.{err}"
                    f'<details id="{p}-starved-q-{esc(stage.key)}">'
                    "<summary>its query anyway</summary>"
                    f"<code>{esc(stage.query.sql)}</code> &mdash; returned "
                    f"{stage.query.row_count} row(s)</details></div>")
        if stage.note:
            out.append(prov(stage.note))
        out.append("</div>")

    funnel_html = section(f"{p}-section",
                          "Where every candidate ended up", "".join(out))

    # --- feed health, as its own section. Raw events are not candidates:
    # dividing one by the other is what produced "200% kept".
    feed: list[str] = [note(
        "This is about whether the data sources answered, which is a "
        "different question from what happened to candidates above. A feed "
        "that fails produces no candidates at all, so it never shows up as "
        "a drop reason &mdash; it shows up here.")]
    feed.append(tiles(f"{p}-feed-tiles", [
        ("Raw items fetched", f"{data.feed_events:,}",
         "everything every feed has returned"),
        ("Feeds that failed", str(len(data.feed_faults)),
         "each with the upstream error text below"),
    ]))
    if data.feed_faults:
        # THE UPSTREAM BODY CAN BE A 4KB HTML PAGE. sec.gov's rate-limit
        # notice is a full document, and rendering it inline turned the
        # panel into a wall of markup (owner-reported 2026-08-11: "this
        # error is squished in dashboard"). House rule 3 still holds -
        # the raw text is kept verbatim - it just moves one click away,
        # with a readable summary in front of it.
        # Everything after the count goes in ONE wrapper. This row had
        # FOUR grid children - count, reason, gist, fold - so the last
        # two wrapped into the 2.4em count column and rendered a word
        # per line. That is the same "squished" report from 2026-08-11,
        # still squished for a second reason after the raw body was
        # folded away.
        items = "".join(
            f'<li><span class="funnel-why-n">{esc(n)}</span>'
            + '<span class="funnel-why-text">' + esc(reason)
            + (f'<span class="prov">{esc(_fault_gist(detail))}</span>'
               f'<details class="raw-fold"><summary>the exact response '
               f'from the server</summary><pre>{esc(str(detail)[:4000])}'
               "</pre></details>" if detail else "")
            + "</span></li>"
            for reason, n, detail in data.feed_faults)
        feed.append(
            f'<div class="funnel-fault" id="{p}-feed-faults">'
            '<h3><span class="fault-chip">NEEDS ATTENTION</span> '
            f"Feeds that could not be read</h3><ul>{items}</ul>"
            f"<p class='prov'>Only failures from the last "
            f"{queries.FEED_FAULT_WINDOW_DAYS} day(s) that the feed has NOT "
            "read successfully since. A failure it recovered from is listed "
            "separately below, not here.</p></div>")
    if getattr(data, "feed_healed", None):
        healed = "".join(
            f'<li><span class="funnel-why-n">{esc(n)}</span>'
            + '<span class="funnel-why-text">' + esc(reason)
            + f'<span class="prov">{esc(detail)}</span></span></li>'
            for reason, n, detail in data.feed_healed)
        feed.append(
            f'<div class="funnel-why" id="{p}-feed-healed">'
            "<h3>Feeds that failed and recovered</h3>"
            f"<ul>{healed}</ul>"
            "<p class='prov'>These read successfully after the error, so "
            "they are history rather than something to act on. Kept "
            "visible because a fault that vanishes silently is "
            "indistinguishable from one that never happened.</p></div>")
    elif data.feed_events == 0 and data.feed_query is not None:
        feed.append(zero_block(
            f"{p}-feed-empty", data.feed_query,
            meaning="no feed has returned anything yet"))
    else:
        feed.append(ok(f'<span id="{p}-feed-ok">Every feed answered; nothing '
                       "upstream is failing.</span>"))
    return funnel_html + section(f"{p}-feed-section", "Feed health", "".join(feed))


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


def cost_panel(db: Db, p: str = "cost", compact: bool = False) -> str:
    c = queries.cost_panel(db)
    out = []

    # THE HURDLE IS A FRACTION OF THE ACCOUNT, so it moves when the
    # account does. £20/month is 30% a year on $1,000 and 15% on $2,000 -
    # the same bill against a different account is a different bar, and
    # the page has to say which account it divided by.
    base = queries.baseline(db)
    account_cents = float(base.capital_cents) or 1.0
    account_text = f"${account_cents / 100:,.0f}"
    account_basis = (
        f"{account_text} account (PLACEHOLDER baseline - no account has been "
        "read and none has been set)" if base.is_placeholder else
        f"{account_text} account (baseline of {base.start_date}, "
        f"{base.source.replace('_', ' ')})")
    base_hurdle = float(c.base_cap_cents) * 12 / account_cents * 100
    max_hurdle = float(c.max_cap_cents) * 12 / account_cents * 100

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
         f"{pill('idle', 'on the ' + account_text + ' baseline')} what the "
         "strategy must beat before a trade counts as good"),
        _daily_ceiling_tile(db, c),
        ("Spend this month, all kinds", dollars(total_mtd),
         f"scheduled {dollars(c.scheduled_mtd_cents)} + manual "
         f"{dollars(c.manual_mtd_cents)}, never pooled"),
    ]))

    # WHICH BOUND IS IN FORCE, named. Owner report 2026-08-10: entered
    # 20 on the setup page and saw "$5" everywhere afterwards, because
    # this page printed the base constant while the governor spent
    # against the owner's figure. Both now come from
    # governor.scheduled_cap_cents(), so they cannot disagree - and the
    # page says which one set the number rather than leaving the reader
    # to work out whether their setting took.
    if c.creds_error:
        out.append(alarm(
            f'<b id="{p}-creds-unreadable">Your saved settings could not be '
            "read, so the cap above is the built-in default and may not be "
            "the one you chose.</b> This is NOT the same as having entered "
            f"nothing. Raw error: <code>{esc(c.creds_error)}</code>"))
    elif c.cap_source == "_owner_set":
        out.append(ok(
            f'<span id="{p}-cap-source">The cap above is <b>your</b> figure: '
            f"{dollars(c.base_cap_cents)} a month, saved on the settings page "
            f"and enforced by the governor. That is {base_hurdle:.1f}% a year "
            f"on a {account_text} account - the return the strategy has to "
            "beat before a trade counts as good. Separately, the bot may add to its own "
            f"budget out of banked profit, never past {dollars(c.max_cap_cents)} "
            "on its own.</span>"))
    elif c.cap_source == "_hard_capped":
        out.append(prov(
            f"The cap above is the hard ceiling of {dollars(c.max_cap_cents)}. "
            "Banked profit would otherwise have raised it further; that bound "
            "never moves without a human editing it."))
    else:
        out.append(prov(
            f"The cap above is the built-in default of {dollars(c.base_cap_cents)} "
            "a month. You have not set a budget of your own - the settings page "
            "takes one, and the bot obeys it either way."))

    # WHAT A SEARCH ACTUALLY COSTS, beside what it was assumed to cost.
    # The pre-call estimate that guards the cap is built from this
    # number, and its seed came from a single measured call. The estimate
    # raises itself on evidence but never lowers itself - lowering the
    # seed is a human decision, and this is the number it needs.
    if c.search_tokens_observed is None:
        out.append(prov_html(
            f'<span id="{p}-search-tokens">A web search is estimated to add '
            f"<b>{c.search_tokens_seed:,}</b> input tokens, from one measured "
            f"call. Only {c.search_tokens_sample} searching turn(s) have been "
            "recorded so far - too few to measure against, so the estimate is "
            "still the seed. Nothing is being adapted on this yet.</span>"))
    else:
        direction = ("ABOVE the seed, so the estimate has raised itself"
                     if c.search_tokens_observed > c.search_tokens_seed
                     else "below the seed, and the estimate is deliberately "
                          "NOT lowered by itself - that is a change for you "
                          "to make, not for the bot to make on a quiet spell")
        out.append(prov_html(
            f'<span id="{p}-search-tokens">A web search was seeded at '
            f"<b>{c.search_tokens_seed:,}</b> input tokens from a single call. "
            f"Measured across {c.search_tokens_sample} searching turns, the "
            f"75th percentile is <b>{c.search_tokens_observed:,}</b> - "
            f"{direction}.</span>"))

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

    # WHEN DOES IT STOP? The pace marker answers "am I ahead of pace"
    # for someone reading the page. This answers the question that
    # decides whether the month produces anything: on this burn rate,
    # what DATE does research stop, and is that before the month ends.
    # On the shipped $5/month default and the owner's own measured
    # $1.93/day it is day three - the funnel then empties with nothing
    # anywhere saying why.
    from catalyst.cost.forecast import forecast as _forecast

    _f = _forecast(c.scheduled_mtd_cents, c.base_cap_cents, c.as_of)
    out.append((alarm if _f.will_stop_early else note)(
        f'<b id="{p}-forecast">' + esc(_f.sentence()) + "</b>"
        + (' <a href="/setup">Change the monthly budget</a>.'
           if _f.will_stop_early else "")))
    out.append(prov(
        "A straight-line projection from month-to-date scheduled spend, "
        "not a promise: cost per day follows how many candidates appear, "
        "which nothing can know in advance. It forecasts spending only "
        "and never authorises any."))

    # Daily billed spend, charted. Same rows as the table below - this
    # is the same data drawn, not a second source of truth.
    # DOLLARS ON THE AXIS, cents kept in the tooltip. The Cost API
    # reports cents (TRAPS.md) and the ledger stores cents, but the
    # owner's budget, the cap and the account are all in dollars, and a
    # chart whose axis is in different units from the figure beside it
    # is a chart that gets misread. The stored value is unchanged; only
    # the presentation divides by 100.
    daily = [(str(r["target_date"])[5:],
              float(r["cost_api_total_cents"] or 0) / 100.0,
              f"{r['target_date']}: "
              f"${float(r['cost_api_total_cents'] or 0) / 100.0:,.4f} "
              f"({r['cost_api_total_cents']} cents billed)")
             for r in reversed(c.billed_q.rows)][-30:]
    if daily:
        out.append('<div class="chart-wrap">' + charts.bar_chart(
            daily, chart_id=f"{p}-daily-chart",
            title="Billed spend per closed day (Anthropic Cost API)",
            # PRECISION MATCHED TO THE DATA, not fixed at four decimals.
            # Four was chosen because a day can genuinely cost under a
            # cent and "$0.00" on every bar would look like a broken
            # feed - true, and it also formats the Y AXIS, which came
            # out reading "$1.0005 / $0.7504 / $0.5002". Rendered and
            # looked at.
            #
            # So: keep four decimals only while the biggest day really
            # is sub-cent, which is the case the original note was
            # about, and use ordinary money everywhere else.
            value_fmt=_money_fmt(v for _l, v, _t in daily),
            reference=(float(c.base_cap_cents) / 100.0 / 30.0,
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
            title="Billed spend per closed day (Anthropic Cost API)",
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
        f"Annual hurdle on the {account_basis}, computed from the CAP (a constant, "
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
            "<p>WHAT THE TWO NUMBERS ARE. <b>Local</b> is what THIS BOT "
            "recorded spending: every Claude call it made, priced from the "
            "stored token counts. <b>Cost API</b> is what Anthropic billed "
            "your whole ORGANISATION that day - which includes anything "
            "else the same account was used for, by you or by any other "
            "tool.</p>"
            "<p>SO A GAP IS NOT AUTOMATICALLY AN ERROR. If local reads "
            "$0.00 and the Cost API reads a few dollars on a day this bot "
            "was not running, that is your own use of the API, not drift in "
            "the bot's ledger. Days before the bot's first recorded spend "
            "are no longer compared at all, because there is nothing of the "
            "bot's to compare. Acknowledging one of these changes no "
            "figure anywhere: it records that a human looked, and lets "
            "scheduled spending resume. It cannot break anything.</p>"
            "<p>WHAT WOULD BE A REAL PROBLEM is local and Cost API "
            "disagreeing on a day the bot DID run, because then the bot's "
            "own arithmetic has drifted from the real bill - and that "
            "number is what decides whether this project is viable.</p>"
            "<p>WHAT TO DO: read the two figures, decide whether the gap is "
            "explainable, and type your name to acknowledge it. That records "
            "who accepted it and restarts spending on the next cycle. It is NOT a daily "
            "chore - it appears only when the figures disagree on a day the "
            "bot ran. If that keeps happening, the cost tracking is wrong "
            "and it should be reported rather than clicked through.</p>"
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
         # WHY IT PAUSED, in the row that paused. Owner-reported: seven
         # consecutive "scheduled_paused" rows and nothing saying why
         # any of them stopped the bot. The governor table one section
         # below has had a reason column all along.
         (f'<span class="prov-inline">{esc(r["pause_reason"])}</span>'
          if r["pause_reason"] else DASH),
         esc(r["acknowledged_by"] or "-"),
         details(f"{p}-recon-raw-{i}", "raw payload", pre(json_pretty(r["api_raw_response"])))]
        for i, r in enumerate(c.reconciliation_q.rows)
    ]
    out.append(_billed_breakdown(c.reconciliation_q, p))

    out.append("<h3>Reconciliation history (local ledger vs Cost API, one closed day each)</h3>")
    if recon_rows:
        out.append(table(
            f"{p}-recon",
            ["day", "kind", "local", "billed", "discrepancy", "API records",
             "action", "why it paused", "acknowledged by", "raw"],
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

    out.append(_token_price_editor(db, p, c.as_of))
    return section(f"{p}-section", "Cost, with provenance on every number", "".join(out))


def _billed_breakdown(recon_q, p: str) -> str:
    """Where the money actually went, from Anthropic's own itemisation.

    The Cost API is asked to group by description, so the raw response
    already stored beside each reconciliation carries the bill line by
    line. That answers a question the totals cannot: on 2026-08-07 cache
    WRITES were 54% of the bill, which is a tuning target and is
    invisible in a single "$0.50" figure.

    Read from the stored payload, never re-fetched - this panel cannot
    spend money or disagree with what was reconciled.
    """
    if not recon_q or not recon_q.rows:
        return ""
    from collections import OrderedDict
    lines: "OrderedDict[str, Decimal]" = OrderedDict()
    total = Decimal("0")
    days = 0
    unreadable = []
    for r in recon_q.rows:
        payload = jload(r["api_raw_response"], None)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            unreadable.append(r["target_date"])
            continue
        days += 1
        for bucket in payload["data"]:
            for rec in (bucket or {}).get("results") or []:
                label = str(rec.get("description") or "(not itemised)")
                try:
                    amount = Decimal(str(rec.get("amount")))
                except Exception:  # noqa: BLE001 - shown, never silently zeroed
                    unreadable.append(f"{r['target_date']}:{label}")
                    continue
                lines[label] = lines.get(label, Decimal("0")) + amount
                total += amount
    if not lines or total <= 0:
        return ""
    ordered = sorted(lines.items(), key=lambda kv: kv[1], reverse=True)
    rows = [[esc(label), dollars(amount),
             f"{(amount / total * 100):.1f}%",
             meter(f"{p}-bill-{i}", float(amount), float(total))]
            for i, (label, amount) in enumerate(ordered)]
    out = [f"<h3>Where the billed money went, across {days} reconciled "
           "day(s)</h3>",
           table(f"{p}-bill-breakdown",
                 ["line, as Anthropic names it", "billed", "share", ""],
                 rows, numeric_cols={1, 2})]
    out.append(prov(
        f"BILLED figures, itemised by Anthropic and totalling "
        f"{dollars(total)} across {days} closed day(s). Read from the raw "
        "payload stored at reconciliation time, not re-fetched - this table "
        "cannot disagree with the reconciliation above it. Cache WRITES are "
        "charged at 1.25x the input rate and cache READS at 0.1x, so a large "
        "cache-write share means context is being rebuilt rather than reused."))
    if unreadable:
        out.append(alarm(
            f'<b id="{p}-bill-unreadable">{len(unreadable)} bill line(s) '
            "could not be read and are NOT in the total above:</b> "
            + esc(", ".join(str(u) for u in unreadable[:10]))))
    return "".join(out)


def _token_price_editor(db: Db, p: str, as_of) -> str:
    """Owner-entered token rates, date-effective and append-only.

    Published rates change - Sonnet 5's introductory pricing ends
    2026-08-31 - and the alternative to this form is editing pricing.py
    and redeploying, which the owner should never be asked to do. The
    three properties that matter are enforced in cost/overrides.py, not
    here: effective-from rather than retroactive, append-only, and a
    refusal of any zero or negative rate.
    """
    from catalyst.cost import overrides as _ovr
    from catalyst.cost.pricing import MODEL_RATES_CENTS_PER_MTOK

    out: list[str] = [
        "<h3>Token prices &mdash; what the ledger prices at</h3>",
        # Owner-asked 2026-08-11: "Surely the API pulling API costs is
        # more accurate than our fixed cost. Ensure that if I do manually
        # update cost it'll still prioritize API pulling API costs unless
        # drastically wrong." It does, and the reason is worth stating
        # where the box is rather than in a doc nobody opens.
        note("A price you type here is only an <b>estimate for calls not "
             "yet billed</b>. It can never overwrite what Anthropic "
             "actually charged: the Cost API's figure is the authority, "
             "spend already recorded keeps the rate it was priced at, and "
             "nothing here reprices history. If a price you enter is "
             "drastically wrong, the nightly comparison against the real "
             "bill is what catches it &mdash; it pauses scheduled spending "
             "rather than quietly believing you."),
    ]

    # What is in force today, per model, so the form has a baseline to
    # correct rather than a blank box.
    live_rows = []
    for m in sorted(MODEL_RATES_CENTS_PER_MTOK):
        try:
            if db.conn is not None:
                inp, outp = _ovr.rates_for_on(db.conn, m, as_of)
            else:
                from catalyst.cost.pricing import rates_for
                inp, outp = rates_for(m, as_of)
            source = "built-in table"
            if db.conn is not None:
                hit = db.q(
                    "SELECT effective_from, set_by FROM pricing_overrides "
                    "WHERE model = ? AND effective_from <= ? "
                    "ORDER BY effective_from DESC, set_at DESC LIMIT 1",
                    (m, as_of.isoformat()))
                if hit.rows:
                    source = (f"set by {hit.rows[0]['set_by']}, effective "
                              f"{hit.rows[0]['effective_from']}")
            live_rows.append([esc(m), f"{inp}", f"{outp}",
                              f"${Decimal(inp) / 100:.2f}",
                              f"${Decimal(outp) / 100:.2f}", esc(source)])
        except Exception as exc:      # a pricing failure must be visible
            live_rows.append([esc(m), "-", "-", "-", "-",
                              esc(f"{type(exc).__name__}: {exc}")])
    out.append(table(
        f"{p}-price-live",
        ["model", "input c/MTok", "output c/MTok", "input $/M", "output $/M",
         "where this rate came from"],
        live_rows, numeric_cols={1, 2, 3, 4}))

    today = as_of.isoformat()
    model_opts = "".join(
        f'<option value="{esc(m)}"'
        + (" selected" if m == "claude-sonnet-5" else "")
        + f">{esc(m)}</option>"
        for m in sorted(MODEL_RATES_CENTS_PER_MTOK))
    out.append(
        f'<form class="inline" id="{p}-price-form" method="post" '
        'action="/set-token-price">'
        f'<label class="prov">model <select name="model">{model_opts}</select>'
        "</label> "
        '<label class="prov">in force from <input type="date" '
        f'name="effective_from" value="{esc(today)}" required></label> '
        '<label class="prov">input cents per million tokens '
        '<input type="number" step="0.01" min="0.01" '
        'name="input_cents_per_mtok" required></label> '
        '<label class="prov">output cents per million tokens '
        '<input type="number" step="0.01" min="0.01" '
        'name="output_cents_per_mtok" required></label> '
        '<label class="prov">your name <input type="text" name="set_by" '
        'size="26" required placeholder="who is making this change">'
        "</label> "
        '<label class="prov"><input type="checkbox" name="allow_large_change" '
        'value="1"> yes, the rate really did move more than 20x</label> '
        '<button type="submit">Record new rate</button></form>')
    out.append(prov(
        "Rates are in CENTS per million tokens, so $3.00 per million is "
        "300. A new rate applies from its date FORWARD only: spend already "
        "recorded keeps the rate it was priced at, which is what keeps the "
        "nightly comparison against the real Anthropic bill reconstructable. "
        "A correction is a new row, never an edit, so the record of what was "
        "believed when survives. A zero is refused at entry. Sonnet 5 is on "
        "introductory pricing until 2026-08-31 and the built-in table "
        "already knows that, so this form is only needed when published "
        "rates change again."))

    hist = db.q("SELECT model, effective_from, input_cents_per_mtok, "
                "output_cents_per_mtok, set_by, set_at, note "
                "FROM pricing_overrides ORDER BY set_at DESC LIMIT 50")
    if hist.rows:
        out.append(table(
            f"{p}-price-history",
            ["model", "in force from", "input c/MTok", "output c/MTok",
             "set by", "recorded at", "note"],
            [[esc(r["model"]), esc(r["effective_from"]),
              esc(r["input_cents_per_mtok"]), esc(r["output_cents_per_mtok"]),
              esc(r["set_by"]), esc(r["set_at"]), esc(r["note"] or "-")]
             for r in hist.rows], numeric_cols={2, 3}))
    else:
        out.append(empty_block(
            f"{p}-empty-price-history", hist,
            meaning="no rate has ever been overridden by hand; every figure "
                    "above is priced from the built-in table in "
                    "catalyst/cost/pricing.py",
        ))
    return "".join(out)


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

    out.append(adaptation_block(db, p))
    out.append(catalyst_coverage_block(p))
    return section(f"{p}-section", "Operational alerts and adaptation",
                   "".join(out))


def adaptation_block(db: Db, p: str = "adapt") -> str:
    """What has moved, and on what evidence.

    Its own function since the adaptation loop actually started running:
    it was only ever rendered inside the Overview digest, which is the
    one place a reader skims. The thresholds this bot trades on now
    change by themselves, and "which number moved, when, and why" is a
    thing the owner should be able to go and look at rather than happen
    across.
    """
    a = queries.alerts(db)
    out = []
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
    return "".join(out)


def _daily_ceiling_tile(db: Db, c) -> tuple:
    """Today's spend against the rate ceiling, and WHICH limit binds first.

    The owner set two numbers that can disagree: "$5 a day usage is ok"
    and a $25/month budget. $5 a day for 30 days is $150, so on a busy
    month the MONTHLY cap runs out long before the daily one ever fires.
    Saying which one will stop the bot, and roughly when, is the
    difference between a limit the owner chose and a limit that
    surprises them mid-month.
    """
    from datetime import datetime, timezone

    from catalyst.cost.governor import DAILY_CAP_CENTS

    today = datetime.now(timezone.utc).date()
    res = db.q("SELECT COALESCE(SUM(CAST(priced_cents AS REAL)), 0) FROM "
               "cost_events WHERE kind = 'scheduled' AND date(priced_at) = ? "
               "AND priced_cents IS NOT NULL", (today.isoformat(),))
    spent_today = Decimal(str(res.rows[0][0])) if res.rows else Decimal(0)
    used = (spent_today / DAILY_CAP_CENTS * 100) if DAILY_CAP_CENTS else 0
    state = "crit" if used >= 100 else "warn" if used >= 75 else "good"

    # WHICH ONE BINDS FIRST, at today's rate.
    left_this_month = c.base_cap_cents - c.scheduled_mtd_cents
    note = ""
    if spent_today > 0 and left_this_month > 0:
        days_left = left_this_month / spent_today
        if days_left < 20:
            note = (f" At today's rate the MONTHLY budget runs out in about "
                    f"{days_left:.0f} day(s) - that is the limit that will "
                    "stop the bot, not this one.")
        else:
            note = " The monthly budget is not close at this rate."
    label = f"{used:.0f}% of the ${DAILY_CAP_CENTS / 100:.0f} daily ceiling"
    return ("Spent today", dollars(spent_today),
            pill(state, label)
            + " a guard against a runaway, not a throttle - it resets at "
              "midnight UTC and needs no acknowledgement." + note)


def catalyst_coverage_block(p: str) -> str:
    """What the bot can trade, and which of it rests on evidence.

    THE TABLE THAT WAS NEVER FILLED IN. Discovery produced eleven kinds
    of catalyst and the risk engine had parameters for one, so 23 of the
    owner's 36 candidates - 64% - died on `unknown_catalyst_type` before
    conviction was read. Some had already been paid for. Nothing on the
    dashboard said so: the funnel showed candidates arriving and leaving,
    and the reason looked like ordinary attrition.

    So the coverage is now stated, and every row says whether its numbers
    were GRADED or are an ESTIMATE. That distinction is the whole lesson
    of the previous build - the defect was not wrong numbers, it was
    wrong numbers that looked measured.
    """
    from catalyst.risk.adaptive_params import (
        DEFAULT_PARAMS, GRADED_CATALYST_TYPES, catalyst_shape_reason,
    )

    gaps = DEFAULT_PARAMS["adverse_gap_assumption"]
    stops = DEFAULT_PARAMS["stop_width"]
    holds = DEFAULT_PARAMS["holding_period_estimate"]
    rows = []
    for ct in sorted(gaps, key=lambda c: (c not in GRADED_CATALYST_TYPES, c)):
        graded = ct in GRADED_CATALYST_TYPES
        rows.append([
            esc(ct.replace("_", " ")),
            pill("good", "graded") if graded else pill("warn", "estimate"),
            f"{float(gaps[ct]) * 100:.0f}%",
            f"{float(stops[ct]) * 100:.0f}%",
            f"{int(holds[ct])}d",
            f'<span class="prov">{esc(catalyst_shape_reason(ct))}</span>',
        ])
    n_graded = len(GRADED_CATALYST_TYPES & set(gaps))
    # FOLDED, not removed. Eighteen rows of reasoning is exactly the
    # "essay with numbers in it" the owner objected to and a test now
    # guards against - it fired on this table. The headline count stays
    # visible because it is the fact worth seeing at a glance; the
    # per-type reasoning is one click away for when it matters.
    return (
        f'<h3 id="{p}-coverage">What it is allowed to trade</h3>'
        + f'<p id="{p}-coverage-line"><b>{len(gaps)}</b> catalyst type(s) '
        f'can reach the risk engine &mdash; <b>{n_graded}</b> backtested, '
        f'<b>{len(gaps) - n_graded}</b> estimated.</p>'
        + details(f"{p}-coverage-detail",
                  "the assumptions behind each one",
                  table(f"{p}-coverage-table",
                ["catalyst type", "basis", "assumed gap", "stop", "hold",
                 "why this shape"], rows)
        + prov(
            "A type NOT on this list is discovered, possibly "
            "researched at cost, and then discarded as "
            "unknown_catalyst_type before conviction is read - which is "
            "what 64% of candidates hit until 2026-08-14. The estimates "
            "are deliberately generous: a larger assumed gap means a "
            "smaller position, so being wrong this way costs opportunity "
            "rather than money, and the refusal tracker is what moves "
            "them.")))


# --------------------------------------------------------------------------
# Decisions: index and single-candidate narrative
# --------------------------------------------------------------------------


def _why_not_researched(db: Db, candidate_id: str) -> str:
    """Which gate stopped this candidate, in words.

    A research_call row with a skipped_reason is the bot's own record of
    why it did not spend; without one, nothing has reached the model yet
    at all, which usually means the cycle has not got to it.
    """
    res = db.q("SELECT skipped_reason FROM research_calls "
               "WHERE candidate_id = ? AND skipped_reason IS NOT NULL "
               "ORDER BY called_at DESC LIMIT 1", (candidate_id,))
    if not res.rows:
        return "not researched yet"
    reason = str(res.rows[0]["skipped_reason"] or "")
    friendly = {
        "market_closed": "not researched - market shut",
        "market_clock_unavailable": "not researched - broker clock unreadable",
        "unprotected_position_blocks_entries":
            "not researched - an unprotected position blocks new entries",
        "unconfirmed_submit_blocks_entries":
            "not researched - an unconfirmed order blocks new entries",
        # The five the cycle used to leave unrecorded, so every one of
        # them read as the benign "not researched yet". One of them means
        # there is no API key at all, which is not a queue.
        "deferred_max_research_per_cycle":
            "queued - over this cycle's research limit, next cycle",
        "no_model_transport_configured":
            "not researched - NO ANTHROPIC KEY configured, so nothing can "
            "be researched at all",
        "no_market_quote":
            "not researched - no live quote for this ticker",
        "ticker_already_entered_this_cycle":
            "not researched - already entered this ticker this cycle",
    }
    for key, text in friendly.items():
        if key in reason:
            return text
    if "cap_exceeded" in reason or "governor" in reason:
        return "not researched - spending cap reached"
    if "transport_error" in reason or "HTTPStatusError" in reason:
        return "not researched - the model call failed"
    return f"not researched - {reason[:48]}"


def decisions_index(db: Db, p: str = "dec") -> str:
    res = queries.decision_list(db)
    if res.is_empty:
        return section(f"{p}-section", "Decisions (taken and declined)",
                       empty_block(f"{p}-empty", res,
                                   meaning="no candidate has ever been discovered"))
    rows = []
    for r in res.rows:
        # NAME THE GATE. "not researched" is true and useless: it does
        # not say whether the market was shut, the governor refused, or
        # the model call failed. The skip reason is already recorded -
        # it just was not being read here (owner-reported 2026-08-11).
        status = r["action"] or ("researched" if r["n_calls"]
                                 else _why_not_researched(db, r["id"]))
        if r["n_orders"]:
            status = f"traded ({r['n_orders']} order(s))"
        elif r["action"] == "skip":
            status = "declined"
        rows.append([
            f'<a href="/decision?candidate_id={esc(r["id"])}">{esc(r["ticker"])}</a>',
            esc(r["catalyst_type"]), esc(r["catalyst_date"]),
            # THE BAND, WITH THE CODE BESIDE IT. "2870" in a column
            # headed SECTOR is a SIC code and means nothing to a reader;
            # the mapping already existed for the correlation logic.
            f'{esc(sector_band(r["sector"]))} '
            f'<span class="prov">SIC {esc(r["sector"])}</span>',
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
            f"({esc(c['catalyst_date_confidence'])}), sector "
            f"{esc(sector_band(c['sector']))} (SIC {esc(c['sector'])}), "
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


def _price_provenance(t: queries.Trace, p: str) -> str:
    """WHERE THE PRICE CAME FROM, on the page that has to explain the
    trade afterwards.

    Every figure in the section below - the size, the stop, the notional
    - is that price times something. BUILD-BRIEF's test is that someone
    who was not there can read one trade and understand it, and a size
    is not explained by a number whose origin is not stated.
    """
    q = t.quote_check_q
    lead = ("<p class='prov'>The price under everything below is the "
            "<b>mid of the live Alpaca bid and ask</b> at decision time, "
            "refused outright if it was over ten minutes old, non-positive "
            "or crossed. The model supplies no price: it has no field to "
            "return one in.</p>")
    if q is None or not q.rows:
        return lead + ("<p class='prov'>No cross-check against cached bars "
                       "is recorded for this candidate. That means the "
                       "check did not run or predates this record, "
                       "<b>not</b> that it passed.</p>")
    r = dict(q.rows[0])
    if r.get("refused"):
        state, word = "crit", "REFUSED"
    elif r.get("flagged"):
        state, word = "warn", "flagged, traded anyway"
    elif r.get("checked"):
        state, word = "good", "consistent"
    else:
        state, word = "idle", "not checked"
    return lead + (f"<p class='prov' id='{p}-quotecheck'>"
                   f"{pill(state, word)} {esc(str(r.get('note') or ''))}</p>")


def _narrative_what_risk_did(t: queries.Trace, p: str) -> str:
    out = ["<h3>3. What the deterministic risk engine did with it</h3>",
           _price_provenance(t, p)]
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
    out.append(_narrative_reviews(t, p))
    return "".join(out)


def _narrative_reviews(t: queries.Trace, p: str) -> str:
    """Every time the model was asked whether the thesis still held.

    Reviews that said HOLD are shown alongside the ones that acted. A
    view listing only the reviews that closed something would make the
    model look decisive in hindsight, and hide the far more common
    answer - that nothing had changed.
    """
    out = ["<h4>Was it still worth holding?</h4>"]
    if not t.positions:
        return ""
    if not t.reviews_q.rows:
        out.append(empty_block(
            f"{p}-empty-reviews", t.reviews_q,
            meaning="the thesis was never re-checked while this position was "
                    "open. Expected for a position opened today or closing "
                    "tomorrow - both skip the review deliberately - and a "
                    "defect for anything held longer.",
        ))
        return "".join(out)
    rows = []
    for r in t.reviews_q.rows:
        r = dict(r)
        changed = jload(r.get("what_changed_json"), []) or []
        rows.append([
            esc(r["reviewed_at"]),
            esc(r["skipped_reason"] and "not obtained" or r["action"]),
            "yes" if r["invalidation_triggered"] else "no",
            esc(r["skipped_reason"] or r["reasoning"]),
            esc("; ".join(str(c) for c in changed) or "-"),
        ])
    out.append(table(
        f"{p}-reviews",
        ["reviewed at", "answer", "invalidation triggered?", "reasoning",
         "what changed since entry"],
        rows))
    out.append(prov(
        "A review can only ever bring the exit date FORWARD, never push it "
        "out - not by a day. 'hold' is not an instruction the system acts "
        "on; it is the absence of a reason to leave early, and the exit "
        "date set at entry stands either way."))
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
        # ONE BOX PER THING, whatever it is linked by.
        #
        # The key used to be (predicate, node), so the same entity linked
        # twice was drawn twice: "PDUFA decision" appeared at the top
        # left as `likely_beneficiary_of` and again at the bottom right
        # as `schedules`, reading as two different events. Rendered and
        # looked at. Keyed on the NODE, the second link joins the box it
        # belongs to and its predicate rides along on the same line.
        if node in seen:
            continue
        seen.add(node)
        branches.append((
            # Predicates are stored as they came from the graph, in
            # snake_case: "likely_beneficiary_of" is not a phrase anyone
            # reads. The underlying value is untouched; only the drawn
            # label loses its underscores.
            str(predicate or "linked to").replace("_", " "),
            node, kind or "entity",
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


def _decision_floor(decision: dict) -> tuple[float, str]:
    """The conviction floor THIS decision actually faced, and where the
    number came from.

    Read from the decision's own `adaptive_params_snapshot`, which is
    written when the decision is made. It used to be hardcoded at 0.60,
    which was harmless only for as long as the floor never moved - the
    adaptation loop had never been wired up, so it never did. Now that
    it runs, a fixed marker would put the threshold line in the wrong
    place on the one chart whose entire job is showing whether the model
    cleared it.
    """
    try:
        snap = jload(decision.get("adaptive_params_snapshot"), {}) or {}
        raw = snap.get("conviction_floor")
        if raw is not None:
            return float(raw), "the value in force when this was decided"
    except (TypeError, ValueError, ArithmeticError):
        pass
    from catalyst.risk.adaptive_params import DEFAULT_PARAMS

    return (float(DEFAULT_PARAMS["conviction_floor"]),
            "the shipped default - this decision recorded no snapshot")


def _conviction_gauge(value: float, p: str, decision: dict | None = None) -> str:
    """Where the model's conviction sat, and where the floor was.

    The number alone ("0.71") means nothing without the threshold it had
    to clear, which is the entire reason a trade happened or did not.
    """
    floor, floor_source = _decision_floor(decision or {})
    pct = max(0.0, min(1.0, value)) * 100
    return (
        f'<div class="gauge" id="{p}-conviction">'
        f'<p class="gauge-title">Conviction {value:.2f}</p>'
        f'<div class="gauge-track">'
        f'<span class="gauge-fill" style="width:{pct:.1f}%"></span>'
        f'<span class="gauge-mark" style="left:{floor * 100:.0f}%"></span>'
        "</div>"
        f'<p class="prov">The marker is the conviction floor this candidate '
        f"had to clear: {floor:.2f}, {esc(floor_source)}. It is an adaptive "
        "parameter and moves on scored outcomes, so older decisions can show "
        "a different marker. Left of the marker means the model was not "
        "confident enough for the code to size anything.</p>"
        "</div>")


def _spider_groups(t, c, db, ticker: str) -> list:
    """The three arms of the decision, each built from what is actually
    recorded. An arm with nothing in it is dropped rather than drawn
    empty - a branch to nowhere reads as a fact the bot had and did not
    use, which is the opposite of true.
    """
    view = dict(t.view_q.rows[0]) if t.view_q.rows else {}
    decision = dict(t.decisions_q.rows[0]) if t.decisions_q.rows else {}

    # 1. WHAT IT SAW - sources, plus the evidence graph if it has one.
    seen = []
    for r in t.raw_events_q.rows[:4]:
        d = dict(r)
        # The feed in WORDS. "edgar" names the machine, not the thing.
        seen.append((queries.source_label(d.get("source")),
                     f"filing {d.get('source_id') or 'unknown'}, fetched "
                     f"{d.get('fetched_at') or 'unknown'}"))
    ev = queries.evidence_graph(db, ticker) if ticker else None
    for r in (ev.rows if ev else [])[:4]:
        d = dict(r)
        # Whichever END IS NOT THE COMPANY is the interesting one. An
        # assertion can point either way - "the CFO bought shares of
        # GBFH" has the company as the OBJECT - so reading object_label
        # first drops exactly the assertions that name a person.
        subj = str(d.get("subject_label") or "").strip()
        obj = str(d.get("object_label") or "").strip()
        label = subj if obj == ticker else obj
        if not label:
            label = str(d.get("object_date") or "").strip()
        if label and label != ticker:
            seen.append((label, str(d.get("predicate") or "linked to")))
    if c.get("catalyst_type"):
        seen.append((str(c["catalyst_type"]).replace("_", " "), "catalyst type"))

    # 2. WHAT IT CONCLUDED - the model's view, in the model's terms.
    concluded = []
    if view:
        if view.get("direction"):
            concluded.append((str(view["direction"]), "direction"))
        if view.get("conviction") is not None:
            concluded.append((f"conviction {view['conviction']}", "0 to 1"))
        if view.get("expected_holding_days") is not None:
            concluded.append(
                (f"hold {view['expected_holding_days']} days", "expected"))
        priced = view.get("priced_in")
        if priced is not None:
            concluded.append(("already priced in" if int(priced or 0)
                              else "not priced in", "model's judgement"))

    # 3. WHAT THE CODE DID - the risk engine, and every limit that bound.
    did = []
    if decision:
        did.append((str(decision.get("action") or "no action"), "risk engine"))
        if decision.get("notional_usd"):
            did.append((f"${decision['notional_usd']}", "size the code chose"))
        if decision.get("stop_price"):
            did.append((f"stop {decision['stop_price']}", "resting at the broker"))
        if decision.get("planned_exit_date"):
            did.append(
                (f"exit by {decision['planned_exit_date']}", "hard date"))
    limits = t.limits_by_decision.get(decision.get("id"))
    for r in (limits.rows if limits else []):
        d = dict(r)
        if int(d.get("binding") or 0):
            did.append((str(d.get("rule_name") or "limit"), "this one bound"))
    for reason in (jload(decision.get("skip_reasons"), []) or [])[:3]:
        did.append((str(reason).replace("_", " "), "why it stopped"))

    return [("What it saw", seen[:6]),
            ("What it concluded", concluded[:5]),
            ("What the code did", did[:6])]


def trace_simple(db: Db, candidate_id: str, p: str = "trs") -> str:
    """The decision in one picture and one paragraph.

    The full dossier is the record; this is the read. Someone opening a
    trade for the first time should get the shape of it - what was seen,
    what was concluded, what the code then did - before meeting a single
    table.
    """
    t = queries.decision_trace(db, candidate_id)
    if not t.candidate_q.rows:
        return section(f"{p}-section", "Decision",
                       empty_block(f"{p}-empty", t.candidate_q,
                                   meaning=f"no candidate with id {candidate_id!r}"))
    c = dict(t.candidate_q.rows[0])
    ticker = str(c.get("ticker") or "")
    view = dict(t.view_q.rows[0]) if t.view_q.rows else {}
    decision = dict(t.decisions_q.rows[0]) if t.decisions_q.rows else {}
    closed = dict(t.closed_q.rows[0]) if t.closed_q.rows else {}
    action = str(decision.get("action") or "").lower()

    verdict = {"trade": "TRADED", "skip": "DECLINED"}.get(
        action, "NO RISK DECISION YET")
    out = [_view_switch(candidate_id, "simple", p)]

    # The sentence first. If a reader takes one thing from this page,
    # it should be a sentence, not a diagram.
    who = ticker or candidate_id
    if action == "trade":
        story = (f"The bot traded {who}. The model read it as "
                 f"{view.get('direction') or 'a directional bet'} with conviction "
                 f"{view.get('conviction', 'unrecorded')}, and the risk engine - "
                 f"not the model - chose a size of "
                 f"{dollars(_cents(decision.get('notional_usd'))) if decision.get('notional_usd') else 'an unrecorded amount'}.")
    elif action == "skip":
        reasons = jload(decision.get("skip_reasons"), []) or []
        story = (f"The bot declined {who}. "
                 + ("It stopped on: " + ", ".join(
                     str(r).replace("_", " ") for r in reasons[:3]) + "."
                    if reasons else
                    "No skip reason was recorded, which is itself a gap worth "
                    "chasing - a refusal should always carry its reason."))
    else:
        story = (f"{who} reached the risk engine but no decision is recorded "
                 "against it yet.")
    if closed:
        story += (f" It closed for {dollars(closed.get('realized_pnl_cents'))} "
                  f"({closed.get('exit_reason') or 'no exit reason recorded'}).")
    out.append(f'<p class="lede-line" id="{p}-story">{esc(story)}</p>')

    groups = _spider_groups(t, c, db, ticker)
    if any(leaves for _, leaves in groups):
        out.append('<div class="chart-wrap">' + charts.decision_spider(
            esc(who), esc(verdict),
            [(esc(label), [(esc(a), esc(b)) for a, b in leaves])
             for label, leaves in groups],
            chart_id=f"{p}-spider") + "</div>")
        out.append('<p class="prov"><span class="key key-1"></span>what it saw '
                   '<span class="key key-2"></span>what it concluded '
                   '<span class="key key-3"></span>what the code did</p>')
        out.append(prov(
            "Every box is something actually recorded against this candidate - "
            "nothing here is inferred for the picture. Hover a line for the "
            "detail behind it. The three arms are the whole architecture: the "
            "model proposes on the middle arm, deterministic code disposes on "
            "the right one, and it can only ever narrow what the model asked "
            "for."))
    else:
        out.append(note(
            f'<b id="{p}-nothing">Nothing is recorded against this candidate '
            "yet</b> beyond its own row - no sources, no model view, no risk "
            "decision. The full view below shows each of those queries and "
            "what it returned."))
    out.append(f'<p class="prov"><a href="/decision?candidate_id='
               f'{esc(candidate_id)}&amp;view=full">Open the full record</a> for '
               "the prompt, every tool call, each limit that bound, and the "
               "fills.</p>")
    return section(f"{p}-section", f"Decision: {esc(who)}", "".join(out))


def _view_switch(candidate_id: str, active: str, p: str) -> str:
    """Simple and full, as a visible pair - not a hidden preference.

    Both links carry the candidate id, so a bookmarked or shared URL
    lands on the same view its sender was looking at.
    """
    def one(key: str, label: str, hint: str) -> str:
        on = " active" if key == active else ""
        return (f'<a class="switch-opt{on}" href="/decision?candidate_id='
                f'{esc(candidate_id)}&amp;view={key}"'
                + (' aria-current="page"' if key == active else "")
                + f"><b>{esc(label)}</b><span>{esc(hint)}</span></a>")
    return (f'<div class="switch" id="{p}-switch" role="navigation" '
            'aria-label="Level of detail">'
            + one("simple", "Simple", "the decision in one picture")
            + one("full", "Full record", "every query, prompt and limit")
            + "</div>")


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
    # THE LITERAL CHARACTER, NEVER THE ENTITY. A placeholder that passes
    # through esc() a second time becomes &amp;mdash;, and .upper() makes
    # that &AMP;MDASH; - which the browser then renders as the visible
    # text "&MDASH;". Owner-reported, on the verdict tile of a decision
    # with no risk row. esc() leaves a real em dash alone.
    DASH = "—"
    direction = str(view.get("direction") or DASH)
    closed = dict(t.closed_q.rows[0]) if t.closed_q.rows else {}

    header = [tiles(f"{p}-tiles", [
        ("Verdict", esc(action.upper() if action else DASH),
         f"{pill(verdict_state, verdict_word)} "
         + esc(str(decision.get("at") or "no timestamp"))),
        ("Model view", esc(direction),
         (f"conviction {conviction_f:.2f}" if conviction_f is not None
          else "no view recorded") + " - the model proposes, code disposes"),
        ("Size the code chose",
         dollars(_cents(decision.get("notional_usd"))) if decision.get("notional_usd")
         else DASH,
         "set by the risk engine, never by the model"),
        ("Outcome",
         dollars(closed.get("realized_pnl_cents")) if closed else "open / none",
         esc(str(closed.get("exit_reason") or "no exit recorded"))),
    ])]
    if conviction_f is not None:
        header.append(_conviction_gauge(conviction_f, p, decision))

    body = [
        _view_switch(candidate_id, "full", p),
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


def _refusal_switch(active: str, p: str) -> str:
    def one(key, label, hint):
        on = " active" if key == active else ""
        return (f'<a class="switch-opt{on}" href="/refusals?view={key}"'
                + (' aria-current="page"' if key == active else "")
                + f"><b>{esc(label)}</b><span>{esc(hint)}</span></a>")
    return (f'<div class="switch" id="{p}-switch" role="navigation" '
            'aria-label="Level of detail">'
            + one("simple", "Simple", "which reasons refuse, and were they right")
            + one("full", "Full record", "every refusal, with prices")
            + "</div>")


def refusals_simple(db: Db, p: str = "refs") -> str:
    """The refusal tracker as a map: reason -> candidate -> what happened.

    The brief calls this the single most important feedback loop, and a
    table makes you compute the loop in your head. Drawn, the question
    "is a particular reason refusing things that then went up?" is
    answered by following one strand instead of reading a column.
    """
    r = queries.refusals(db)
    out = [_refusal_switch("simple", p)]
    rows = [dict(x) for x in r.query.rows]
    if not rows:
        out.append(empty_block(f"{p}-empty", r.query,
                               meaning="no candidate has been declined and "
                                       "recorded, so there is nothing to score"))
        return section(f"{p}-section", "Refusals", "".join(out))

    if r.n_scored:
        share = 100.0 * r.n_positive / r.n_scored
        out.append(
            f"<p id='{p}-headline'><span class='big'>{r.mean_outcome_return:+.4f}"
            f"</span> mean outcome return across {r.n_scored} scored "
            f"refusal(s); {r.n_positive} of {r.n_scored} ({share:.0f}%) moved "
            "the way the system declined to go.</p>")
    else:
        out.append(
            f"<p id='{p}-headline'>{len(rows)} refusal(s) recorded, "
            "<b>none scored yet</b>. Until they are scored there is no answer "
            "to \"is it too strict\" - only the question.</p>")

    reason_hits, cand_hits, outcome_hits = {}, {}, {}
    edges = []
    for x in rows:
        cid = f"c:{x['candidate_id']}"
        cand_hits[cid] = (x["ticker"] or x["candidate_id"], 0)
        reasons = jload(x["skip_reasons"], []) or ["no reason recorded"]
        for reason in reasons:
            rk = f"r:{reason}"
            reason_hits[rk] = reason_hits.get(rk, 0) + 1
            edges.append((rk, cid, 1,
                          f"{x['ticker'] or x['candidate_id']} was refused on "
                          f"{reason}"))
        # The outcome column is the point of the whole tracker.
        if not x["scored_at"]:
            ok = "o:not scored yet"
            detail = "refused, outcome not yet measured"
        else:
            ret = _dec_or_none(x["outcome_return"])
            if ret is None:
                ok, detail = "o:scored, no return recorded", "scored but no return"
            elif ret > 0:
                ok = "o:went UP after refusal"
                detail = f"outcome return {ret:+}"
            else:
                ok = "o:did not go up"
                detail = f"outcome return {ret:+}"
        outcome_hits[ok] = outcome_hits.get(ok, 0) + 1
        edges.append((cid, ok, 1, detail))

    degree = {}
    for a, b_, _, _ in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b_] = degree.get(b_, 0) + 1
    layers = [
        ("Why it refused", [(k, k[2:].replace("_", " "), v)
                            for k, v in sorted(reason_hits.items(),
                                               key=lambda kv: -kv[1])]),
        ("Which candidate", [(k, label, degree.get(k, 0))
                             for k, (label, _) in cand_hits.items()]),
        ("What it then did", [(k, k[2:], v)
                              for k, v in sorted(outcome_hits.items(),
                                                 key=lambda kv: -kv[1])]),
    ]
    links = {esc(k): f"/decision?candidate_id={esc(k[2:])}" for k in cand_hits}
    out.append('<div class="chart-wrap">' + charts.neural_map(
        [(esc(lbl), [(esc(a), esc(b_), w) for a, b_, w in ns])
         for lbl, ns in layers],
        [(esc(a), esc(b_), w, esc(t)) for a, b_, w, t in edges],
        chart_id=f"{p}-map", links=links,
        # Same reason as BRAIN_COLUMN_NOTES: the outer columns hold
        # REASONS and RESULTS, not things, so several candidates share
        # one node and the picture reads as a merge unless it says so.
        column_notes={
            "Why it refused": ("the reason given - every candidate "
                               "refused for it runs into this node"),
            "Which candidate": "one dated, tradeable event",
            "What it then did": ("what the price did afterwards, "
                                 "scored later"),
        }) + "</div>")
    out.append(prov(
        "Left is the rule that declined it, middle is the candidate, right is "
        "what the price did afterwards. A reason with most of its strands "
        "landing on \"went UP after refusal\" is a reason refusing money - "
        "which is the number that is allowed to move the conviction floor, "
        "and only once the sample is big enough."))
    if r.n_scored < MIN_TRADES_FOR_MEANING:
        out.append(alarm(
            f"<b id='{p}-small-sample'>Too small to act on.</b> {r.n_scored} "
            f"scored refusal(s) against a {MIN_TRADES_FOR_MEANING} minimum. "
            "Read the shape, not the verdict."))
    return section(f"{p}-section", "Refusals", "".join(out))


def _dec_or_none(value):
    from decimal import Decimal, InvalidOperation
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def refusals_panel(db: Db, p: str = "ref") -> str:
    r = queries.refusals(db)
    out = [_refusal_switch("full", p)]
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


def bundle_buttons(p: str) -> str:
    """The log-collection buttons, ON EVERY PAGE SOMEONE WOULD LOOK.

    ONE BUTTON PER QUESTION, plus the master. Owner-asked: "different
    type of log collection buttons for different issues e.g. pricing or
    logic etc. Then one master log that is all."

    A scoped bundle is not a smaller master bundle for tidiness: it is
    the difference between sending a whole-database dump and sending the
    rows that bear on the question. Each one says what it covers, so
    choosing between them does not require knowing the schema.

    These lived only on /maintenance and the owner reported not seeing
    them - having gone, reasonably, to Logs. A control that exists on a
    page nobody thinks to open has not shipped.
    """
    from catalyst.dashboard.server import (
        DEFAULT_WINDOW_DAYS, DIAGNOSTIC_SCOPES, LOG_WINDOW_DAYS,
    )

    out = [f'<h3 id="{p}-bundle">Collect logs to send on</h3>',
           "<p>Pick what went wrong and how far back to look, then "
           "download one file. <b>Safe to send on</b>: keys and secrets "
           "are stripped twice &mdash; once where each value is "
           "captured, and again over the whole file before it is "
           "written.</p>",
           f'<form id="{p}-bundle-form" method="get" '
           'action="/diagnostics.json">']
    for key in ("everything", "all", "pricing", "logic", "data",
                "execution"):
        spec = DIAGNOSTIC_SCOPES[key]
        master = " master" if key == "everything" else ""
        out.append(
            f'<p class="bundlerow"><label class="bundlebtn{master}">'
            f'<input type="radio" name="scope" value="{key}"'
            + (" checked" if key == "everything" else "")
            + f'> {esc(spec["label"])}</label>'
            f'<span class="bundlewhy">{esc(spec["why"])}</span></p>')
    # HOW FAR BACK. Owner-asked: "When i click download log i want it to
    # ask me how many days of logs so im not getting a massive file."
    # A default of everything is how a diagnostic export becomes a file
    # nobody can open.
    opts = "".join(
        f'<option value="{d or 0}"'
        + (" selected" if d == DEFAULT_WINDOW_DAYS else "") + ">"
        + (f"last {d} day{'s' if d != 1 else ''}" if d else "everything, "
           "however old")
        + "</option>"
        for d in LOG_WINDOW_DAYS)
    out.append(
        f'<p class="bundlerow"><label class="bundlebtn" for="{p}-days">'
        f'How far back</label><span class="bundlewhy">'
        f'<select id="{p}-days" name="days">{opts}</select> '
        "&mdash; anything with a timestamp is cut to this window. Rows "
        "with no timestamp at all (the current positions, the price "
        "table) always come out whole, and the file says which those "
        "were.</span></p>"
        f'<p class="bundlerow"><button class="bundlebtn master" '
        f'type="submit" id="{p}-bundle-go">Download</button>'
        '<span class="bundlewhy">One file, named for what you picked.'
        "</span></p></form>")
    out.append(prov(
        "If the page itself will not load, the same file can be produced "
        "on the server with:  sudo -u catalyst /opt/catalyst/venv/bin/python "
        "-m catalyst.dashboard --diagnostics > catalyst-diagnostics.json"))
    return "".join(out)


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
        f'<label>text <input id="{p}-q" name="q" size="32" '
        f'value="{esc(lg.filters["q"])}" '
        # SHORT ENOUGH TO FIT ITS OWN BOX. The old text needed 288px in a
        # 172px field, so the reader saw "substring of message, traceb".
        # What it searches is spelled out in the caption below instead.
        'placeholder="message, traceback or context"></label> '
        f'<label>since <input id="{p}-since" name="since" '
        f'value="{esc(lg.filters["since"])}" placeholder="2026-08-01"></label> '
        f'<label>until <input id="{p}-until" name="until" '
        f'value="{esc(lg.filters["until"])}" placeholder="2026-08-31"></label> '
        f'<label>limit <input id="{p}-limit" name="limit" size="4" '
        f'value="{esc(lg.filters["limit"])}"></label> '
        '<button type="submit">search</button>'
        "</form>"
    )
    out = [form]
    if not lg.available:
        out.append(f'<div class="empty" id="{p}-missing">{esc(lg.reason)}</div>')
        out.append(empty_block(f"{p}-empty-nolabel", lg.query,
                               meaning="the logs table itself is absent"))
        # ...and the buttons especially here: a missing logs table is
        # precisely when someone needs to send the evidence on.
        out.append(bundle_buttons(p))
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
    out.append(bundle_buttons(p))
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
    # ONE BUTTON PER QUESTION, plus the master. Owner-asked: "different
    # type of log collection buttons for different issues e.g. pricing
    # or logic etc. Then one master log that is all."
    #
    # A scoped bundle is not a smaller master bundle for tidiness: it is
    # the difference between sending a whole-database dump and sending
    # the rows that bear on the question. Each one says what it covers,
    # so choosing between them does not require knowing the schema.
    out.append(bundle_buttons(p))

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
# The benchmark baseline: what "the same money in SPY" means, and setting it
# --------------------------------------------------------------------------


#: Bounds on an owner-entered baseline. Not risk limits - this figure
#: places no order and sizes nothing. They exist so a slip of the
#: keyboard produces a sentence instead of a comparison against $0 or
#: against a hundred million dollars.
MIN_BASELINE_CENTS = 100                    # $1
MAX_BASELINE_CENTS = 1_000_000_000          # $10,000,000
#: SPY has not existed for the whole of history. A date before this
#: cannot be answered by any bar cache, so it is refused with a reason
#: rather than silently returning an empty window.
EARLIEST_BASELINE_DATE = "1993-01-01"


def _baseline_source_pill(base) -> str:
    return {
        "owner_set": pill("good", "you set this"),
        "first_run": pill("good", "from the broker account"),
        "account_changed": pill("warn", "account changed, restarted"),
        "unset": pill("idle", "placeholder, never set"),
    }.get(base.source, pill("idle", esc(base.source)))


def benchmark_panel(db: Db, p: str = "bench", message: str = "",
                    failed: bool = False) -> str:
    """The SPY comparison, and the form that sets it.

    Owner-asked, 2026-08-14: "a section so I can emulate the SPY with a
    custom field, I want to be able to in maintainence state when I
    wouldve invested in SPY and track from then e.g. I can say track SPY
    if i were to invest $2000 on a set date and calculate that against
    our bot."

    THE HISTORY IS SHOWN BESIDE THE FORM on purpose. The table is
    append-only and every row carries the reason it was written, so a
    comparison that changed under the owner's feet - a new Alpaca
    account restarts it automatically - is answered on screen instead of
    looking like the numbers moved by themselves.
    """
    v = queries.benchmark_view(db)
    base = v.baseline
    out = []

    if message:
        out.append(alarm(f'<b id="{p}-result">{esc(message)}</b>') if failed
                   else ok(f'<span id="{p}-result">{esc(message)}</span>'))

    spy_value = v.spy_value_cents
    diff = v.difference_cents
    out.append(tiles(f"{p}-tiles", [
        ("The comparison money", dollars(base.capital_cents),
         f"{_baseline_source_pill(base)} bought as SPY on "
         f"{esc(base.start_date)}"),
        ("That money in SPY today",
         dollars(spy_value) if spy_value is not None else "&mdash;",
         (f"{pill('idle', 'last close ' + esc(v.spy_last_day))} from the "
          "local bar cache" if spy_value is not None else
          f"{pill('idle', 'no SPY series')} the raw reason is printed below")),
        ("The bot, net of the API bill", dollars(v.bot_net_cents),
         f"{pill('idle', f'{v.n_closed} closed trade(s)')} banked profit "
         "only - open positions are not marked in"),
        ("Bot minus SPY",
         (f'<span class="{"pos" if diff >= 0 else "neg"}">{dollars(diff)}</span>'
          if diff is not None else "&mdash;"),
         ("same money, same start date" if diff is not None
          else "needs a SPY series to subtract")),
    ]))

    if v.n_closed < MIN_TRADES_FOR_MEANING:
        out.append(alarm(
            f'<b id="{p}-small-sample">Too small a sample to read as a '
            f"verdict.</b> {v.n_closed} closed trade(s) against the "
            f"{MIN_TRADES_FOR_MEANING} this dashboard requires before any "
            "comparison here counts as evidence. The difference above is a "
            "description of what has happened, not a measurement of edge."))

    if spy_value is None:
        # House rule 3: the zero prints its raw upstream response. Here
        # the "upstream" is the local bar cache, so its coverage and the
        # exact failure both go on screen.
        out.append(alarm(
            f'<b id="{p}-spy-missing">No SPY series for this baseline.</b> '
            f"Window asked for: {esc(base.start_date)} onwards. Source tried: "
            f"<code>{esc(v.spy_source or 'local bar cache')}</code>. Raw "
            f"reason: <code>{esc(v.spy_error or 'none recorded')}</code>. "
            + (f"The cache holds {v.cache_bars} daily closes covering "
               f"{esc(v.cache_first)} to {esc(v.cache_last)} - a start date "
               "outside that range cannot be answered until the cache is "
               "refreshed."
               if v.cache_bars else
               f"The cache could not be read at all: "
               f"<code>{esc(v.cache_error or 'no detail')}</code>.")))
    else:
        out.append(prov(
            f"SPY: {len(v.spy_points)} daily closes from {v.spy_source}. The "
            f"baseline money is bought at the first close on or after "
            f"{base.start_date} ({v.spy_points[0][0]}) and marked at "
            f"{v.spy_last_day}. Cache coverage: {v.cache_first} to "
            f"{v.cache_last}, {v.cache_bars} bars. Exposure is NOT matched - "
            "SPY is fully invested throughout and the bot is not."
            + (f" Feed: {v.spy_feed} rather than the consolidated tape."
               if v.spy_feed and v.spy_feed != "sip" else "")))

    out.append(prov(
        f"Why this baseline: {base.reason}"
        + (f" Recorded {base.set_at}." if base.set_at else "")
        + (f" Account fingerprint {base.account_fingerprint} (a hash of the "
           "broker account id, never a key)." if base.account_fingerprint
           else " No account fingerprint recorded.")))

    # --- the form.
    out.append(f'<h3 id="{p}-form-heading">Track SPY from a date and amount '
               "of your choosing</h3>")
    out.append(
        f'<form class="inline" id="{p}-form" method="post" '
        'action="/set-benchmark">'
        '<label class="prov">dollars into SPY '
        f'<input id="{p}-amount" name="amount_usd" type="text" '
        'inputmode="decimal" placeholder="2000" '
        f'value="{float(base.capital_cents) / 100:.2f}"></label> '
        '<label class="prov">bought on '
        f'<input id="{p}-date" name="start_date" type="date" '
        f'value="{esc(base.start_date)}" min="{EARLIEST_BASELINE_DATE}">'
        "</label> "
        '<label class="prov">why (optional) '
        f'<input id="{p}-why" name="reason" type="text" maxlength="500" '
        'size="30" placeholder="moving to the $2,000 account"></label> '
        f'<button id="{p}-submit" type="submit">Set the comparison</button>'
        "</form>")
    out.append(prov(
        f"The amount must be between $1 and $10,000,000 and the date must "
        f"not be in the future or before {EARLIEST_BASELINE_DATE}; anything "
        "else comes back as a sentence saying what was wrong, and nothing is "
        "written. This figure changes what the bot is COMPARED against - it "
        "never changes what the bot may spend, size or trade. Closed trades "
        "and API spend dated before the start date fall outside the "
        "comparison and the Performance page says how many. Nothing is "
        "overwritten: this appends a row and the previous ones stay below "
        "forever. A later change of Alpaca account overrides an owner-set "
        "baseline - a new account is a new experiment - and that override "
        "appears in the history with its own reason."))

    # --- the history.
    rows = []
    for r in v.history_q.rows:
        d = dict(r)
        rows.append([
            dollars(d.get("capital_cents")), esc(d.get("start_date")),
            esc(str(d.get("source") or "").replace("_", " ")),
            esc(d.get("set_at")),
            esc(d.get("account_fingerprint") or "none"),
            f'<span class="prov">{raw(d.get("reason"))}</span>',
        ])
    if rows:
        out.append(table(
            f"{p}-history",
            ["amount", "from", "set by", "recorded at", "account", "why"],
            rows, numeric_cols={0}))
    else:
        out.append(empty_block(
            f"{p}-empty-history", v.history_q,
            meaning="no baseline has ever been recorded. The figures above "
                    "are the documented fallback, and the first broker read "
                    "or the form above replaces them.",
        ))
    return section(f"{p}-section",
                   "The SPY comparison: how much, from when, and why",
                   "".join(out))


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
        banked = perf.start_capital_cents + perf.gross_pnl_cents
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


# --------------------------------------------------------------------------
# The brain
# --------------------------------------------------------------------------


#: What runs when. Every row is a fact from the code, not a plan:
#: DEFAULT_CYCLE_SECONDS is 900, discovery is step 4 of run_cycle and is
#: never gated on market hours, entries are gated on the broker clock,
#: and the benchmark and the bill check each run once a day.
SCHEDULE = [
    ("Every 15 minutes, all day and night", "same",
     "One cycle: check the kill switches, reconcile with the broker, "
     "move stops and take time-based exits, fetch new filings, build "
     "candidates."),
    ("14:30-21:00 UK", "09:30-16:00 New York",
     "US market hours. This is the ONLY window in which the bot may "
     "open a position, and the only window in which it pays Claude to "
     "research one - research is gated on the same clock, so nothing is "
     "spent while the market is shut."),
    ("11:00-03:00 UK", "06:00-22:00 New York",
     "EDGAR's own publishing window, business days. The bot fetches "
     "every cycle regardless; outside this window there is simply "
     "nothing new to fetch."),
    ("Shortly after 00:00 UK", "shortly after 19:00 New York",
     "The nightly bill check: yesterday's spend is read from the "
     "Anthropic Cost API and compared with the bot's own ledger. "
     "Anthropic reports whole days only, so it can never check today."),
    ("Once a day, first cycle after midnight UTC", "same",
     "The SPY benchmark series is topped up from Alpaca, so the "
     "performance comparison stays current."),
    ("Weekends and US holidays", "same",
     "Cycles keep running - stops, exits and reconciliation still "
     "matter - but the market is shut, so no position is opened and no "
     "research is paid for."),
]


def schedule_panel(db: Db, p: str = "sched") -> str:
    """What the bot does, and when, in both clocks.

    Owner asked for "a logic run down e.g. what it does at each time US
    and UK time". UK first because that is where the owner is; New York
    beside it because that is the clock the market keeps. The two drift
    by an hour twice a year when the daylight-saving changes land on
    different dates - the market hours are the fixed ones, so those are
    the anchor and the UK column is what moves.
    """
    rows = [[esc(uk), esc(us if us != "same" else uk), esc(what)]
            for uk, us, what in SCHEDULE]
    out = [
        f'<p class="lede-line" id="{p}-lede">A cycle every 15 minutes, '
        "around the clock. What changes through the day is what a cycle "
        "is <b>allowed</b> to do.</p>",
        table(f"{p}-table", ["UK time", "New York time", "What happens"], rows),
        prov("Every row is read from the code rather than written down "
             "beside it: the cycle interval is DEFAULT_CYCLE_SECONDS, "
             "discovery is step 4 of run_cycle and is never gated on "
             "market hours, and entries and research are both gated on "
             "the broker's own clock. UK times shift by an hour when "
             "British Summer Time and US daylight saving fall out of "
             "step; the New York column is the anchor."),
    ]
    return section(f"{p}-section", "What the bot does, and when",
                   "".join(out))


def state_line(db: Db, p: str = "state") -> str:
    """What is happening, in one sentence, before anything else.

    The owner checks in twice a day and asked for something that reads
    like a trading desk. A desk answers "is anything wrong, is anything
    open, did anything happen" in a glance; this page answered it only
    after four panels and a thousand words. This is that glance, and
    every clause of it is a figure already computed elsewhere - it
    reads, it never decides.
    """
    bits = []
    try:
        open_q = db.q("SELECT COUNT(*) n FROM positions WHERE status = 'open'")
        n_open = int(open_q.rows[0]["n"]) if open_q.rows else 0
    except Exception:  # noqa: BLE001
        n_open = 0
    bits.append(f"holding <b>{n_open}</b> of 5 positions")

    try:
        perf = queries.performance(db)
        bits.append(f"account <b>{dollars(perf.net_equity_cents)}</b> "
                    f"after costs")
        if perf.n_closed:
            bits.append(f"<b>{perf.n_closed}</b> closed trade(s)")
        else:
            bits.append("nothing closed yet")
    except Exception:  # noqa: BLE001
        bits.append("account value unavailable")

    try:
        c = queries.cost_panel(db)
        bits.append(f"spent <b>{dollars(c.scheduled_mtd_cents)}</b> of "
                    f"{dollars(c.base_cap_cents)} this month")
    except Exception:  # noqa: BLE001
        pass

    # The one thing that should interrupt a glance.
    try:
        blocked = db.q("SELECT COUNT(*) n FROM cost_reconciliation_events "
                       "WHERE action_taken = 'scheduled_paused' "
                       "AND acknowledged_by IS NULL")
        if blocked.rows and int(blocked.rows[0]["n"]):
            bits.append('<b class="neg">spending is PAUSED pending a '
                        "reconciliation you need to acknowledge</b>")
    except Exception:  # noqa: BLE001
        pass

    return (f'<p class="state-line" id="{p}-line">'
            + " &middot; ".join(bits) + "</p>")


def brain_view_controls(p: str, zoom: float, nodes: int,
                        focus: str = "") -> str:
    """Zoom and expand, as LINKS. No JavaScript, by design - the map is
    documented as deterministic, and a JS pan/zoom draws a different
    picture per browser. Each control is a URL, so a view the owner
    finds useful can be bookmarked or pasted into a bug report.
    """
    tail = f"&amp;focus={esc(focus)}" if focus else ""

    def opt(label, key, value, current):
        on = abs(float(current) - float(value)) < 1e-9
        other = "nodes" if key == "zoom" else "zoom"
        other_v = nodes if key == "zoom" else zoom
        href = f"/brain?{key}={value}&amp;{other}={other_v:g}{tail}"
        cls = "viewopt on" if on else "viewopt"
        return (f'<span class="{cls}">{label}</span>' if on else
                f'<a class="{cls}" href="{href}">{label}</a>')

    # TWO KINDS OF CONTROL, AND THEY ARE NOT THE SAME KIND.
    #
    # "Show per layer" changes WHAT IS DRAWN, so it has to be a request
    # to the server and it stays a link always.
    #
    # Zoom only moves the CAMERA. Where the script runs you scroll to
    # zoom and drag to move, so a row of magnification links is a worse
    # version of something already in your hand - it hides itself
    # (.viewbar-camera, hidden by .map-live) rather than sitting there
    # as clutter. With scripting off it is the only zoom there is, so it
    # stays. Owner-asked: "cant we move the mouse ourself instead of
    # zooming etc."
    return (
        f'<p class="viewbar viewbar-camera" id="{p}-controls-camera">'
        "<span class=\"viewbar-label\">Zoom</span> "
        + " ".join(opt(t, "zoom", v, zoom) for t, v in
                   (("fit", 1), ("1.5x", 1.5), ("2x", 2), ("3x", 3)))
        + "</p>"
        + f'<p class="viewbar" id="{p}-controls">'
        + '<span class="viewbar-label">Show per layer</span> '
        + " ".join(opt(t, "nodes", v, nodes) for t, v in
                   (("8", 8), ("14", 14), ("30", 30), ("all", 999)))
        + "</p>"
        + prov("How many nodes are drawn is a question for the server, "
               "so it reloads the page and the answer is in the URL. "
               "Moving and zooming is not - it never redraws the graph "
               "differently. Node labels are trimmed only to fit their "
               "column, and the full name is always on hover."))


#: The one rule the interaction layer lives under. Stated here because
#: it is the reason a dashboard that handles money is allowed a script
#: at all, and it is what the tests check.
CAMERA_RULE = (
    "This script may move the camera and change what is emphasised. It "
    "never decides what is drawn: the nodes, the lines and the numbers "
    "are the server's, and turning JavaScript off gives back exactly the "
    "same picture with the links still working.")


def brain_interaction(chart_id: str, p: str) -> str:
    """Drag to move, wheel to zoom, click to follow a thread.

    OWNER-ASKED: "cant we move the mouse ourself instead of zooming etc,
    think about how it can display useful info but also be intuitive."

    Zoom-by-link was defensible and it was not intuitive. Nobody reads a
    map by picking a magnification from a list; they grab it and move
    it. So the map now behaves like a map.

    WHY A SCRIPT IS ALLOWED HERE, when the rest of this dashboard has
    none. The objection to JavaScript was never the mouse - it was that
    a client-side graph draws a different picture per browser and cannot
    be pasted into a bug report. That objection is answered by
    CAMERA_RULE rather than by refusing the mouse: the SVG is still
    rendered by the server, still deterministic, still identical with
    scripting off. What the layer adds is a viewport and a highlight.
    Every link, every control and every number keeps working without it.

    The whole thing is deliberately small and dependency-free. Nothing
    is fetched, nothing is computed from the database, nothing is
    stored.
    """
    return f"""<script>
/* {CAMERA_RULE} */
(function () {{
  var svg = document.getElementById({chart_id!r});
  var cam = document.getElementById({chart_id!r} + '-camera');
  if (!svg || !cam) return;               /* no map on this page */
  var box = svg.viewBox.baseVal;
  var view = {{x: 0, y: 0, k: 1}};
  var MIN = 0.4, MAX = 8;
  var stage = svg.parentNode;
  stage.classList.add('map-live');
  /* Reveal the mouse controls only where they work. The strip sits
     BEFORE the chart in the document, so no sibling selector can reach
     it - the script has to turn it on itself. */
  var tools = document.getElementById({p!r} + '-tools');
  if (tools) tools.classList.add('on');
  document.body.classList.add('map-tools-live');

  function apply() {{
    cam.setAttribute('transform',
      'translate(' + view.x.toFixed(2) + ',' + view.y.toFixed(2) + ') ' +
      'scale(' + view.k.toFixed(4) + ')');
    var pct = document.getElementById({p!r} + '-zoomnow');
    if (pct) pct.textContent = Math.round(view.k * 100) + '%';
  }}
  function reset() {{ view = {{x: 0, y: 0, k: 1}}; apply(); }}

  /* Client coordinates -> the SVG's own units, so zooming keeps the
     point under the cursor where it is rather than drifting. */
  function at(evt) {{
    var r = svg.getBoundingClientRect();
    return {{x: (evt.clientX - r.left) / r.width * box.width,
             y: (evt.clientY - r.top) / r.height * box.height}};
  }}
  function zoomAbout(pt, k) {{
    k = Math.max(MIN, Math.min(MAX, k));
    view.x = pt.x - (pt.x - view.x) * (k / view.k);
    view.y = pt.y - (pt.y - view.y) * (k / view.k);
    view.k = k;
    apply();
  }}

  /* --- drag to move ------------------------------------------------ */
  var drag = null, moved = 0;
  svg.addEventListener('pointerdown', function (e) {{
    if (e.button !== 0) return;
    drag = {{x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, held: false}};
    moved = 0;
  }});
  svg.addEventListener('pointermove', function (e) {{
    if (!drag) return;
    var r = svg.getBoundingClientRect();
    var dx = (e.clientX - drag.x) / r.width * box.width;
    var dy = (e.clientY - drag.y) / r.height * box.height;
    moved = Math.max(moved, Math.abs(e.clientX - drag.x)
                          + Math.abs(e.clientY - drag.y));
    /* CAPTURE ONLY ONCE A DRAG REALLY BEGINS. Capturing on pointerdown
       retargets the click that follows to the SVG itself, so clicking a
       node landed on the background instead and silently did nothing.
       Found in a browser, not in the markup: pan, zoom, find and the
       keyboard all worked, and only the node click was dead. */
    if (moved <= 3) return;
    if (!drag.held) {{
      drag.held = true;
      stage.classList.add('map-grabbing');
      try {{ svg.setPointerCapture(e.pointerId); }} catch (err) {{}}
    }}
    view.x = drag.vx + dx; view.y = drag.vy + dy;
    apply();
  }});
  function endDrag(e) {{
    if (!drag) return;
    var held = drag.held;
    drag = null;
    stage.classList.remove('map-grabbing');
    if (held) {{ try {{ svg.releasePointerCapture(e.pointerId); }} catch (err) {{}} }}
  }}
  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);

  /* --- wheel to zoom ----------------------------------------------- */
  svg.addEventListener('wheel', function (e) {{
    e.preventDefault();
    zoomAbout(at(e), view.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12));
  }}, {{passive: false}});

  /* --- click a node to follow its thread --------------------------- */
  var nodes = [].slice.call(svg.querySelectorAll('[data-node]'));
  var edges = [].slice.call(svg.querySelectorAll('[data-src]'));
  var card = document.getElementById({p!r} + '-card');
  var picked = null;

  function clear() {{
    picked = null;
    stage.classList.remove('map-picked');
    nodes.forEach(function (n) {{ n.classList.remove('on', 'near'); }});
    edges.forEach(function (l) {{ l.classList.remove('on'); }});
    if (card) card.hidden = true;
  }}
  function pick(g) {{
    var id = g.getAttribute('data-node');
    if (picked === id) {{ clear(); return; }}
    picked = id;
    stage.classList.add('map-picked');
    var near = {{}};
    edges.forEach(function (l) {{
      var s = l.getAttribute('data-src'), d = l.getAttribute('data-dst');
      var hit = (s === id || d === id);
      l.classList.toggle('on', hit);
      if (hit) {{ near[s] = 1; near[d] = 1; }}
    }});
    nodes.forEach(function (n) {{
      var nid = n.getAttribute('data-node');
      n.classList.toggle('on', nid === id);
      n.classList.toggle('near', nid !== id && !!near[nid]);
    }});
    if (!card) return;
    var links = g.getAttribute('data-links') || '0';
    var q = encodeURIComponent(id);
    card.innerHTML =
      '<b>' + g.getAttribute('data-label') + '</b>'
      + '<span class="cardsub">' + g.getAttribute('data-layer')
      + ' &middot; ' + links + ' link(s) &middot; '
      + Object.keys(near).length + ' shown connected</span>'
      + '<a href="/node?id=' + q + '">What is this?</a>'
      + '<a href="/brain?focus=' + q + '">Draw just this</a>';
    card.hidden = false;
  }}
  nodes.forEach(function (g) {{
    g.addEventListener('click', function (e) {{
      if (moved > 4) return;              /* that was a drag, not a click */
      e.preventDefault(); e.stopPropagation();
      pick(g);
    }});
    g.addEventListener('keydown', function (e) {{
      if (e.key === 'Enter' || e.key === ' ') {{ e.preventDefault(); pick(g); }}
    }});
  }});
  svg.addEventListener('click', function () {{ if (moved <= 4) clear(); }});
  svg.addEventListener('dblclick', function (e) {{ e.preventDefault(); reset(); }});

  /* --- keyboard ----------------------------------------------------- */
  svg.setAttribute('tabindex', '0');
  svg.addEventListener('keydown', function (e) {{
    var step = box.width / 12;
    var was = {{x: view.x, y: view.y, k: view.k}};
    if (e.key === 'ArrowLeft') view.x += step;
    else if (e.key === 'ArrowRight') view.x -= step;
    else if (e.key === 'ArrowUp') view.y += step;
    else if (e.key === 'ArrowDown') view.y -= step;
    else if (e.key === '+' || e.key === '=') view.k = Math.min(MAX, view.k * 1.2);
    else if (e.key === '-' || e.key === '_') view.k = Math.max(MIN, view.k / 1.2);
    else if (e.key === '0') {{ reset(); e.preventDefault(); return; }}
    else if (e.key === 'Escape') {{ clear(); return; }}
    else return;
    e.preventDefault();
    if (was.k !== view.k || was.x !== view.x) apply(); else apply();
  }});

  var btn = document.getElementById({p!r} + '-reset');
  if (btn) btn.addEventListener('click', function (e) {{
    e.preventDefault(); reset(); clear();
  }});

  /* Type to find. Highlights matches; it never removes a node, because
     a map that quietly drops what you did not search for is a different
     picture rather than the same one with your answer marked. */
  var find = document.getElementById({p!r} + '-find');
  if (find) find.addEventListener('input', function () {{
    var q = find.value.trim().toLowerCase();
    stage.classList.toggle('map-finding', q.length > 0);
    nodes.forEach(function (n) {{
      var hit = q && (n.getAttribute('data-label') || '')
        .toLowerCase().indexOf(q) >= 0;
      n.classList.toggle('found', !!hit);
    }});
  }});

  apply();
}})();
</script>"""


def brain_ways_in(b, p: str) -> str:
    """The handful of nodes worth opening first.

    OWNER-REPORTED: "its got too much data all at once and isnt easy to
    navigate." A whole-graph picture has no entry point - every node
    looks like every other node, so there is nowhere obvious to click
    and the reader is left to scan a texture. These are the busiest
    nodes, named, in order, each opening its own neighbourhood.
    """
    top = queries.busiest_nodes(b, limit=8)
    if not top:
        return ""
    chips = "".join(
        f'<a class="waychip" href="/brain?focus={esc(nid)}">'
        f'{esc(nlabel)}<span class="waychip-n">{weight}</span></a>'
        for nid, nlabel, _layer, weight in top)
    return (f'<div class="waysin" id="{p}-waysin">'
            "<h3>Start with one thing</h3>"
            f"<p>{chips}</p>"
            + prov("The most connected nodes, with how many links each "
                   "carries. Opening one draws just that node and what "
                   "it touches - the same recorded links, a picture you "
                   "can read.") + "</div>")


#: WHAT A NODE IN EACH COLUMN ACTUALLY IS, in one short line.
#:
#: The middle columns hold ANSWERS, not things - and that is the single
#: most misread part of this drawing. Three tickers all run into one
#: node labelled "long", which a reader takes as three candidates being
#: merged into one when it means all three were judged the same way.
#: Nothing on the page said otherwise, so the picture was quietly
#: teaching the wrong model of how the bot works.
BRAIN_COLUMN_NOTES = {
    "Sources": "a filing or headline the bot read",
    "Candidates": "one dated, tradeable event",
    "What it linked": "a person or company named in it",
    "Model view": ("Claude's answer - every candidate it gave "
                   "the same answer to runs into it"),
    "Risk engine": ("what the code then decided - again, one node "
                    "per answer"),
    "Outcome": "how the trade actually ended",
}


def brain_panel(db: Db, p: str = "brain", zoom: float = 1.0,
                nodes: int = None, focus: str = "") -> str:
    """The whole system's wiring in one picture.

    Not a metaphor and not an illustration: every node is a row and
    every line is a foreign key or a recorded assertion. A picture of a
    machine that draws links the machine does not have would be worse
    than no picture, because it would be believed.
    """
    cap = int(nodes) if nodes else charts.MAX_NODES_PER_LAYER
    b = queries.brain(db)
    out = []
    if not b.edge_count:
        out.append(note(
            f'<b id="{p}-quiet">Nothing is wired up yet.</b> The bot has not '
            "yet linked a source to a candidate, so there is nothing to draw. "
            "This fills in on its own as discovery runs - the queries behind "
            "it are printed below, so an empty brain and a broken query are "
            "never the same picture."))
        for i, q in enumerate(b.queries):
            out.append(empty_block(f"{p}-q-{i}", q,
                                   meaning="no rows behind this layer yet"))
        return section(f"{p}-section", "The brain", "".join(out))

    # FOCUS FIRST. A whole-graph picture answers "is anything connected"
    # and little else; past a few dozen nodes the lines are a texture.
    # Owner-reported: "its got too much data all at once and isnt easy
    # to navigate." The focused view is a SUBSET of the same edges, so a
    # line here is the same recorded row it was on the whole map.
    whole = b
    focus_label = ""
    if focus:
        b = queries.brain_focus(whole, focus)
        focus_label = next(
            (nlabel for _lbl, ns in whole.layers for nid, nlabel, _w in ns
             if nid == focus), focus)
        if not b.edge_count:
            out.append(note(
                f'<b id="{p}-nofocus">Nothing is recorded as connecting to '
                f"{esc(str(focus_label))}.</b> That is a fact about the "
                "data rather than a gap in the page - every line on this "
                "map is a stored row. "
                f'<a href="/brain">Show the whole map</a>.'))
            return section(f"{p}-section", "The brain", "".join(out))
        out.append(
            f'<p class="crumb" id="{p}-crumb">'
            f'<a href="/brain">The whole map</a> &rsaquo; '
            f"<b>{esc(str(focus_label))}</b></p>")
        out.append(
            f'<p id="{p}-headline"><span class="big">{b.edge_count}</span> '
            f"link(s) touching <b>{esc(str(focus_label))}</b>, across "
            f"{b.node_count} node(s), out to "
            f"{queries.FOCUS_HOPS} step(s). This is a slice of the "
            f"{whole.edge_count} link(s) on the whole map, not a "
            f"different picture. "
            f'<a href="/node?id={esc(focus)}">What is this node?</a></p>')
    else:
        out.append(
            f'<p id="{p}-headline"><span class="big">{b.edge_count}</span> '
            f"recorded link(s) across {b.node_count} node(s). Left to right "
            "is the path a filing takes to become a trade: what the bot "
            "read, what it built from it, what it linked, what the model "
            "made of it, what the code decided, and what actually "
            "happened.</p>")
        out.append(brain_ways_in(b, p))
    # Candidate nodes go somewhere: clicking one opens its decision.
    # EVERY node that HAS a record links to it, not just candidates.
    # The owner asked to "click to see the news"; only Candidate nodes
    # were clickable, so the sources and entities - the half of the map
    # that says WHAT was read - went nowhere.
    #
    # A node with no page to open is deliberately left unlinked rather
    # than pointed at a search that may return nothing: a link that goes
    # somewhere useless is worse than no link, because it costs a click
    # to discover.
    tickers = {str(r["ticker"]).strip().upper()
               for r in db.q("SELECT DISTINCT ticker FROM candidates "
                             "WHERE ticker IS NOT NULL").rows}
    links = {}
    for label, nodes in b.layers:
        for nid, nlabel, _ in nodes:
            key = esc(nid)
            # EVERY node opens its own page now. Previously only some
            # nodes linked anywhere, so clicking the map was a lottery -
            # the owner asked to "click in and it opens another page
            # with the runbook". /node works for any id in the graph and
            # says so plainly when an id is not in it.
            links[key] = f"/node?id={esc(nid)}"
            if label == "Candidates":
                links[key] = f"/decision?candidate_id={esc(nid[5:])}"
            elif str(nlabel).strip().upper() in tickers:
                # A node whose label IS a ticker opens the news map
                # filtered to it - where the headlines actually are.
                # Checked against tickers that exist rather than guessed:
                # /newsmap filters on `ticker`, and a link built from a
                # feed name ("SEC filings (EDGAR)") would resolve to an
                # empty page. A link that goes somewhere useless is worse
                # than no link, because it costs a click to find out.
                links[key] = f"/newsmap?ticker={esc(str(nlabel).strip())}"
    out.append(brain_view_controls(p, zoom, cap, focus=focus))
    # THE MOUSE CONTROLS, and a plain statement of what they do. They are
    # shown only where they work: the .map-live class is added by the
    # script, so with scripting off this strip stays hidden rather than
    # advertising a drag that does nothing.
    out.append(
        f'<p class="maptools" id="{p}-tools">'
        f'<span class="maphint">Drag to move &middot; scroll to zoom '
        f'&middot; click a node to follow its links &middot; '
        f'double-click to reset</span>'
        f'<label class="mapfind">find '
        f'<input id="{p}-find" type="search" placeholder="a ticker, a feed"'
        ' autocomplete="off"></label>'
        f'<span class="mapzoom">zoom <b id="{p}-zoomnow">100%</b></span>'
        f'<button type="button" class="viewopt" id="{p}-reset">'
        "Reset view</button></p>"
        # BESIDE THE MAP, NOT OVER IT. Overlaid on the drawing it
        # covered the node it was describing - seen in a screenshot,
        # which is the only way that kind of fault shows up.
        f'<div class="mapcard" id="{p}-card" hidden></div>')
    out.append('<div class="chart-wrap chart-scroll">' + charts.neural_map(
        [(esc(label), [(esc(nid), esc(nlabel), w) for nid, nlabel, w in nodes])
         for label, nodes in b.layers],
        [(esc(s), esc(d), w, esc(t))
         for s, d, w, t in queries.collapse_edges(b.edges)],
        chart_id=f"{p}-map", links=links,
        max_per_layer=cap, zoom=zoom, column_notes=BRAIN_COLUMN_NOTES)
        + "</div>")
    out.append(brain_interaction(f"{p}-map", p))
    out.append(prov(
        "Every line is one recorded relationship - a source event named by a "
        "candidate, an assertion in the evidence graph, a view against a "
        "candidate, a decision against a view. Nothing is added to make the "
        "picture denser. Hover any line for the row behind it, and any node "
        "for how many links it carries. Brightness is how heavily a link is "
        "used; dot size is how connected a node is. Colour is depth only - "
        "left is early, right is late - not category."))
    out.append(caveat(
        "This draws what the database HAS, which is not the same as what the "
        "bot considered. A source that returned nothing, or a candidate "
        "dropped before it was stored, leaves no node here. Read the Pipeline "
        "page for the drop reasons at each stage."))

    rows = []
    for label, nodes in b.layers:
        for _, nlabel, weight in nodes:
            rows.append([esc(label), esc(nlabel), str(weight)])
    if rows:
        out.append(details(
            f"{p}-node-table", f"the same {len(rows)} nodes as a table",
            table(f"{p}-nodes", ["layer", "node", "links"], rows,
                  numeric_cols={2})))
    return section(f"{p}-section", "The brain", "".join(out))


def news_map_panel(db: Db, params: dict | None = None, p: str = "newsmap") -> str:
    """What the news said, about whom, and what the bot did about it.

    Owner-asked: "I also wanted a second neural network for new linking
    news feeds e.g. CEO appointed or something like that to see links
    and connections, filters so the network doesnt get hug etc."

    EVERY LINE IS A ROW. A story joins a ticker because the stored
    payload named it; a ticker joins an outcome because a candidate row
    carries that ticker. Nothing is inferred to thicken the picture - a
    connector nobody can trace back to a row is decoration that looks
    like evidence, and this dashboard's whole claim is that it never
    draws one.
    """
    params = params or {}

    def one(key, default=""):
        got = params.get(key)
        return (got[0] if isinstance(got, list) else got) or default

    try:
        days = max(1, min(30, int(one("days", "3"))))
    except (TypeError, ValueError):
        days = 3
    kind = str(one("kind"))[:40]
    ticker = str(one("ticker"))[:12]
    only_linked = one("linked") in ("1", "on", "true")

    m = queries.news_map(db, days=days, kind=kind, ticker=ticker,
                         only_linked=only_linked)
    out: list[str] = [note(
        "Read left to right: <b>a story</b> was published, it was "
        "<b>about a company</b>, and the bot <b>did something or "
        "nothing</b> about it. A ticker marked <b>*</b> is one where a "
        "FILING feed said something too &mdash; those are the "
        "cross-feed links worth looking at, because a story and a filing "
        "agreeing is two independent observations rather than one "
        "newsroom. Hover any line for the headline behind it.")]

    kinds = sorted({str((n[1] or "")) for _lbl, nodes in m.layers[:1]
                    for n in nodes}) if m.layers else []
    out.append(
        f'<form class="inline" id="{p}-filters" method="get" action="/newsmap">'
        f'<label class="prov">days <input type="number" name="days" min="1" '
        f'max="30" value="{days}" style="width:5em"></label> '
        f'<label class="prov">ticker <input type="text" name="ticker" '
        f'value="{esc(ticker)}" placeholder="any" style="width:7em"></label> '
        f'<label class="prov">kind <input type="text" name="kind" '
        f'value="{esc(kind)}" placeholder="e.g. dilution" '
        f'style="width:10em"></label> '
        f'<label class="prov"><input type="checkbox" name="linked" value="1"'
        + (" checked" if only_linked else "")
        + "> only tickers a filing feed also mentioned</label> "
        '<button type="submit">Redraw</button></form>')

    out.append(tiles(f"{p}-tiles", [
        ("Stories", f"{m.story_count:,}",
         f"news items stored in the last {days} day(s)"),
        ("Companies", f"{m.ticker_count:,}", "distinct tickers mentioned"),
        ("Cross-feed", str(len(m.cross_feed_tickers)),
         "also named by a filing feed - the links worth reading"),
    ]))

    story_nodes = m.layers[0][1] if m.layers else []
    if story_nodes:
        out.append(charts.neural_map(
            m.layers, m.edges, chart_id=f"{p}-map", links=m.node_links))
        out.append(prov(
            f"Drawn from {len(m.edges)} recorded link(s). The story column "
            f"is capped at {queries.MAP_MAX_STORIES} and the ticker column "
            f"at {queries.MAP_MAX_TICKERS}, ordered so cross-feed tickers "
            "come first - a firehose day carries 450+ symbols and drawing "
            "them all is a smear, not a map. Narrow the window or filter by "
            "ticker to see the rest."))
    else:
        out.append(zero_block(
            f"{p}-empty", m.query,
            meaning=("no news has been stored in this window yet. The news "
                     "feed reaches discovery from build a244e894 onward, so "
                     "an install upgraded before that has nothing here until "
                     "the next cycle runs.")))

    if m.cross_feed_tickers:
        out.append(note(
            "<b>Cross-feed right now:</b> "
            + ", ".join(esc(t) for t in m.cross_feed_tickers[:24])
            + ". These are the tickers where news and filings independently "
            "landed on the same company. That is what earns a candidate a "
            "bigger research budget, and it is the only kind of link the "
            "bot treats as more than coincidence."))
    return section(f"{p}-section", "News map: what was said, about whom",
                   "".join(out))


def data_integrity_panel(db: Db, p: str = "integ") -> str:
    """Where every number came from, and whether anything disagreed.

    OWNER-ASKED: "I want to ensure all data is correct and validated so
    we arent trading under false pretenses."

    Three things nothing on this dashboard showed before:

      - FILL AGAINST INTENDED. BUILD-BRIEF requires it on every trade.
        Both halves were being stored - the live mid the order was sized
        from, and the broker's own fill - and never compared anywhere a
        person could see. It is the only measurement of what this bot
        actually pays to trade.
      - THE MODELLED SPREAD, beside the real fill rather than instead of
        it, because a paper account pays no spread and paper P&L is
        optimistic by exactly that amount.
      - HOW MUCH EVIDENCE THERE IS. Per-stock sizing and the price-action
        block both read cached daily bars; with none cached both fall
        back to the catalyst category, correctly and silently. The count
        is the difference between a bot sizing on this stock's own
        history and one sizing on a category average.
    """
    d = queries.data_integrity(db)
    out: list[str] = []

    out.append(note(
        "<b>Every number that touches money comes from Alpaca, never from "
        "the model.</b> The research model has no price, target or "
        "quantity field it could return &mdash; a figure it reads in an "
        "article can only reach the thesis text, which no arithmetic "
        "reads. The price below is the mid of the live bid and ask at "
        "decision time, refused outright if it is more than ten minutes "
        "old, non-positive or crossed."))

    med = (f"{d.median_slippage_pct:+.3f}%"
           if d.median_slippage_pct is not None else DASH)
    worst = (f"{d.worst_slippage_pct:+.3f}%"
             if d.worst_slippage_pct is not None else DASH)
    out.append(tiles(f"{p}-tiles", [
        ("Fills measured", f"{d.n_fills}",
         "buy fills with both an intended and a filled price"),
        ("Median slippage", med, "filled against the mid it was sized from"),
        ("Worst slippage", worst, "the largest gap either way"),
        ("Tickers with history", f"{d.cached_tickers:,}",
         "cached daily bars, which per-stock sizing reads"),
    ]))

    if d.n_fills:
        rows = [[esc(t or DASH),
                 dollars(_cents(intended)) if intended is not None else DASH,
                 dollars(_cents(filled)) if filled is not None else DASH,
                 f"{pct:+.3f}%" if pct is not None else DASH,
                 esc(modelled or DASH), esc(when)]
                for _oid, t, intended, filled, pct, modelled, when in d.fills]
        out.append(table(
            f"{p}-fills",
            ["ticker", "intended (live mid)", "filled (broker)",
             "slippage", "modelled spread $", "when"],
            rows, numeric_cols={1, 2, 3, 4}))
        out.append(caveat(
            "On a PAPER account slippage reads near zero because paper "
            "fills pay no spread. That is not a good result, it is the "
            # DASH, not "&mdash;": caveat() escapes its argument, so the
            # entity would reach the browser as a visible "&mdash;".
            f"absence of a measurement {DASH} the modelled column is what "
            "the same trade would have cost in real money, which is why it "
            "is recorded beside the broker's price and never instead of "
            "it."))
    else:
        out.append(zero_block(
            f"{p}-nofills", d.fills_q,
            meaning=("no buy fill has both an intended and a filled price "
                     "yet. The intended price is recorded when an order is "
                     "sent, so this fills in with the first trade.")))

    if not d.cached_tickers:
        out.append(note(
            f'<b id="{p}-nobars">No cached price history yet.</b> '
            "Per-stock sizing and the price-action evidence both read "
            f"daily bars from <code>{esc(d.cache_dir)}</code>. With none "
            "cached, every position is sized against its catalyst "
            "category's assumption instead of against what that stock has "
            "actually done &mdash; correct and conservative, but it is the "
            "weaker of the two. The bot fetches a candidate's history when "
            "it researches it, so this fills in on its own."))

    out.append(_quote_cross_check_block(d, p))

    return section(f"{p}-section",
                   "Data integrity: where every number came from",
                   "".join(out))


def _plain_conviction(c) -> str:
    """The number, said in words. Conviction is defined as a frequency,
    so the English is a translation and not a gloss."""
    if c is None:
        return "no conviction recorded"
    pct = int(round(float(c) * 100))
    return (f"{float(c):.2f} &mdash; Claude expected this to be right "
            f"about {pct} times in 100 similar setups")


def _money_fmt(values):
    """A dollar formatter whose precision suits the numbers it is given.

    A fixed four decimals was chosen so a sub-cent day would not render
    as "$0.00" and look like a broken feed. That reasoning holds, but
    the same formatter also labels the Y AXIS, which came out reading
    "$1.0005 / $0.7504 / $0.5002 / $0.2501". Rendered and looked at.

    So the rule, not a list of charts: while the largest value really is
    under a cent, keep the extra places; otherwise show money the way
    money is written.
    """
    try:
        top = max((abs(float(v)) for v in values if v is not None),
                  default=0.0)
    except (TypeError, ValueError):
        top = 0.0
    if 0 < top < 0.01:
        return lambda v: f"${v:,.4f}"
    return lambda v: f"${v:,.2f}"


def _money(cents) -> str:
    if cents is None:
        return DASH
    return dollars(cents)


def _num(value):
    """A Decimal, or None. Nothing on this page may raise on bad data:
    a trade whose stop price arrived as an empty string still has to
    render its thesis."""
    try:
        d = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return d if d.is_finite() else None


def _price_rail(st, p: str, index: int) -> str:
    """Where the stop sits relative to the entry, drawn rather than said.

    OWNER-ASKED: "Simplify data maybe with prediction graphs, it feels
    word heavy". The single most useful geometric fact about a position
    is how far the price must fall before the stop rescues it - it is
    what sets the size, it is what the owner is exposed to, and it took
    three sentences to say in prose.

    NOTHING HERE IS PREDICTED. It draws only prices that exist: the
    stop, the fill, and the exit if there was one. A "where it might
    go" line would be a forecast the bot does not make, drawn with the
    same authority as a measured number - which is the one thing this
    dashboard refuses to do anywhere else.
    """
    entry, stop = _num(st.entry_price), _num(st.stop_price)
    if entry is None or stop is None or entry <= 0 or stop <= 0:
        return ""
    exit_px = _num(st.exit_price)
    marks = [("stop", stop), ("bought", entry)]
    if exit_px is not None and exit_px > 0:
        marks.append(("sold", exit_px))
    lo, hi = min(m[1] for m in marks), max(m[1] for m in marks)
    pad = (hi - lo) * Decimal("0.18") or hi * Decimal("0.02")
    lo, hi = lo - pad, hi + pad
    span = hi - lo
    if span <= 0:
        return ""

    W, H = 640, 84
    def x(v):
        return float((Decimal(v) - lo) / span) * (W - 80) + 40

    parts = [
        f'<svg id="{p}-t{index}-rail" class="rail-chart" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="where the stop sits relative to the price '
        f'paid for {esc(st.ticker)}">',
        f'<line x1="40" y1="46" x2="{W - 40}" y2="46" class="rail-axis"/>',
    ]
    # The band between the stop and the fill IS the exposure. Drawing it
    # as an area rather than two ticks is the whole point: the width of
    # that band is what the position size was divided by.
    parts.append(
        f'<rect x="{x(stop):.1f}" y="38" width="{x(entry) - x(stop):.1f}" '
        f'height="16" class="rail-risk"/>')
    for label, value in marks:
        px = x(value)
        cls = {"stop": "rail-stop", "bought": "rail-entry",
               "sold": "rail-exit"}[label]
        parts.append(f'<line x1="{px:.1f}" y1="28" x2="{px:.1f}" y2="64" '
                     f'class="{cls}"/>')
        anchor = ("start" if px < 70 else "end" if px > W - 70 else "middle")
        parts.append(
            f'<text x="{px:.1f}" y="20" text-anchor="{anchor}" '
            f'class="rail-label">{esc(label)} ${esc(f"{value:.2f}")}</text>')
    parts.append("</svg>")

    drop = (entry - stop) / entry * 100
    words = (f"The stop sits <b>{drop:.1f}% below</b> what the bot paid. "
             "That gap is the divisor in the sizing sum: a wider one buys "
             "fewer shares for the same dollars of risk.")
    if exit_px is not None and exit_px > 0:
        moved = (exit_px - entry) / entry * 100
        words += (f" It was sold at ${exit_px:.2f}, "
                  f"<b>{moved:+.1f}%</b> against the fill.")
    return "".join(parts) + figcap(words)


#: Broker rejection codes seen in this account's own record, translated.
#: A LOOKUP, never a filter: an unrecognised code falls through to the
#: broker's own message, which is the rule that matters (house rule 7).
#: The list existing at all is a convenience, not the classifier.
_BROKER_CODES = {
    "40310000": ("The broker refused this as a possible wash trade - it "
                 "will not accept a sell order while a buy for the same "
                 "stock is still working."),
    "40010001": "The broker could not read the order as sent.",
    "40110000": "Not enough buying power for this order.",
}


def _broker_said(raw_text) -> tuple[str, str]:
    """(what happened in English, the exact response).

    OWNER-REPORTED: "the data at the bottom appears to just be raw json
    not easily understandable". It was - a truncated JSON blob in a
    table cell, which is the one thing this dashboard tells everyone
    else not to do.

    The JSON is not thrown away; house rule 3 wants the raw response
    beside the answer, not instead of it. It folds.
    """
    text = str(raw_text or "")
    if not text.strip():
        return "", ""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return "", text
    if not isinstance(data, dict):
        return "", text
    if not data:
        # "{}" is not a response worth folding away for. Offering to
        # reveal an empty object is the noise this change removes.
        return "", ""

    # A REFUSAL. The broker names its own reason; that message is the
    # answer, and the code lookup only adds plain English on top of it.
    body = data.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            body = {"message": body}
    if not isinstance(body, dict):
        body = {}
    code = str(body.get("code") or data.get("code") or "")
    message = str(body.get("message") or data.get("message")
                  or data.get("submit_error") or "")
    if code or message:
        said = _BROKER_CODES.get(code, "")
        if said and message:
            return f"{said} It said: &ldquo;{esc(message)}&rdquo;", text
        if message:
            return f"The broker said: &ldquo;{esc(message)}&rdquo;", text
        return f"The broker refused it with code {esc(code)}.", text

    # AN ACCEPTED ORDER. Say what actually moved, not the whole object.
    filled = _num(data.get("filled_qty"))
    price = _num(data.get("filled_avg_price"))
    status = str(data.get("status") or "")
    if filled and filled > 0 and price:
        return (f"Filled <b>{esc(f'{filled}')}</b> shares at an average of "
                f"<b>${esc(f'{price:.4f}'.rstrip('0').rstrip('.'))}</b>."), text
    if status:
        plain = {
            "new": "Accepted and resting at the broker, not yet filled.",
            "accepted": "Accepted by the broker, not yet working.",
            "partially_filled": "Partly filled; the rest is still working.",
            "canceled": "Cancelled before it filled.",
            "expired": "Expired at the close without filling.",
        }.get(status, f"The broker reported status &ldquo;{esc(status)}&rdquo;.")
        return plain, text
    return "", text


#: Section icons for the trade story. OWNER-ASKED: "less text more
#: graphs and icons, make the UI more friendly, its text heavy".
#:
#: The icon is never the only signal - every heading keeps its words and
#: every icon is aria-hidden, so a screen reader hears the heading and
#: nothing is carried by the picture alone. That is the same rule the
#: status pills follow, and for the same reason.
_STEP_ICON = {
    "why": "\U0001F50D",       # magnifier - how it was found
    "view": "\U0001F9E0",      # brain - what Claude concluded
    "size": "⚖️",    # scales - what code sized
    "guard": "\U0001F6E1️",  # shield - protection
    "orders": "\U0001F4CB",    # clipboard - what was sent
    "next": "\U0001F504",      # cycle - reviews and what happens next
}


def _step(key: str, title: str) -> str:
    return (f'<h4><span class="step-ico" aria-hidden="true">'
            f'{_STEP_ICON[key]}</span>{esc(title)}</h4>')


def _why_fold(fid: str, text: str) -> str:
    """The explanation, available but out of the way.

    The owner reported the page "text heavy" twice. None of this prose
    is wrong - it is the provenance and the reasoning the brief demands
    - so it is folded rather than deleted. Someone who wants to know why
    a number is what it is can still find out in one click; someone
    reading the page to see how a trade went is no longer wading.
    """
    return (f'<details class="why-fold" id="{fid}">'
            "<summary>why this matters</summary>"
            f"<div>{text}</div></details>")


def _hold_progress(st, p: str, index: int) -> str:
    """How far through its allowed life this position is.

    A bar, because "opened 2026-08-17, closes 2026-08-29" is two dates a
    reader has to subtract; a fill level is the answer already worked
    out.
    """
    start, end = _as_date(st.opened_at), _as_date(st.planned_exit_date)
    if not (start and end and end > start):
        return ""
    today = (_as_date(st.closed_at) if st.status != "open" else None) \
        or datetime.now(timezone.utc).date()
    total = (end - start).days
    gone = max(0, min((today - start).days, total))
    left = (end - today).days
    pct = gone / total * 100
    word = (f"{left} day(s) left of a {total}-day hold" if left >= 0
            else f"{-left} day(s) past its exit date")
    if st.status != "open":
        word = f"held {gone} of an allowed {total} days"
    return (f'<div class="hold" id="{p}-t{index}-hold">'
            f'<div class="hold-track"><span class="hold-fill" '
            f'style="width:{pct:.1f}%"></span></div>'
            + figcap(esc(word)) + "</div>")


def _as_date(text):
    """A date from an ISO string of any precision, or None."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _hold_timeline(stories, p: str) -> str:
    """Every position's window on one axis, with today marked.

    OWNER-ASKED: "for the trades tab its auto expanded, can we add more
    detail, simplify into some other graphs".

    Folding every trade shut means the page opens showing nothing, so
    the shut state has to earn its place. This is the view that answers
    the question the owner actually opens this tab with - what am I
    holding, and how long until it closes - without opening anything.

    The hard exit date is the whole design of this bot ("hold days to
    weeks, never months"), and until now it was a string buried in each
    story. A bar running out of room is that rule made visible.

    TODAY IS THE REAL CLOCK, never a fixture date - house rule 6. The
    marker is where it actually is; a pinned one would drift out of the
    window a day at a time and quietly stop meaning anything.
    """
    rows = []
    for st in stories:
        start, end = _as_date(st.opened_at), _as_date(st.planned_exit_date)
        if start and end and end >= start:
            rows.append((st, start, end))
    if not rows:
        return ""

    today = datetime.now(timezone.utc).date()
    lo = min(r[1] for r in rows)
    hi = max(max(r[2] for r in rows), today)
    span = max((hi - lo).days, 1)

    W, row_h, top = 640, 26, 22
    H = top + row_h * len(rows) + 22
    lab_w = 58

    def x(d):
        return lab_w + (d - lo).days / span * (W - lab_w - 14)

    parts = [
        f'<svg id="{p}-timeline" class="tl-chart" viewBox="0 0 {W} {H}" '
        'role="img" aria-label="how long each position runs, against '
        'today">']
    for i, (st, start, end) in enumerate(rows):
        y = top + i * row_h
        x0, x1 = x(start), x(end)
        held = st.status == "open"
        parts.append(
            f'<text x="0" y="{y + 11:.0f}" class="tl-name">'
            f"{esc(st.ticker)}</text>")
        parts.append(
            f'<rect x="{x0:.1f}" y="{y:.0f}" '
            f'width="{max(x1 - x0, 2):.1f}" height="13" rx="2" '
            f'class="{"tl-open" if held else "tl-done"}">'
            f"<title>{esc(st.ticker)}: {esc(str(start))} to "
            f"{esc(str(end))}</title></rect>")
        left = (end - today).days
        if held:
            parts.append(
                f'<text x="{min(x1 + 6, W - 12):.1f}" y="{y + 11:.0f}" '
                f'text-anchor="end" class="tl-note">'
                + esc(f"{left}d left" if left >= 0 else "past due")
                + "</text>")
    # TODAY, drawn last so nothing overpaints it.
    tx = x(today)
    parts.append(f'<line x1="{tx:.1f}" y1="{top - 8:.0f}" x2="{tx:.1f}" '
                 f'y2="{H - 20:.0f}" class="tl-today"/>')
    parts.append(f'<text x="{tx:.1f}" y="{H - 8:.0f}" text-anchor="middle" '
                 'class="tl-note">today</text>')
    parts.append("</svg>")
    return ('<div class="tl-wrap">'
            "<h3>How long each position has left</h3>"
            + "".join(parts)
            + figcap("Every position carries a hard exit date set when it "
                     "opened, and a review can only bring it FORWARD. A bar "
                     "that ends left of today has overrun and should have "
                     "closed.")
            + "</div>")


def _trade_headline(st) -> str:
    """The one line that shows while a trade is folded shut.

    It has to answer "do I need to open this?" on its own, so it carries
    the four facts that decide that: which stock, is it live, how much,
    and how it went.
    """
    bits = [f"<b>{esc(st.ticker)}</b>"]
    if st.status == "open":
        bits.append("open")
    elif st.realized_pnl_cents is not None:
        bits.append(("made " if st.realized_pnl_cents >= 0 else "lost ")
                    + _money(abs(st.realized_pnl_cents)))
    else:
        bits.append("closed")
    if st.notional_usd:
        bits.append(f"${esc(st.notional_usd)}")
    if st.opened_at:
        bits.append(esc(str(st.opened_at)[:10]))
    if st.conviction is not None:
        bits.append(f"conviction {float(st.conviction):.2f}")
    return " &middot; ".join(bits)


def _trade_story(st, p: str, index: int, folded: bool = True) -> str:
    """One trade told as a story, in the order a person asks.

    OWNER-ASKED: "I also want it breaking into english, chat responses
    claude gives, I want to understand in plain text."

    So every machine value is followed by what it MEANS, and Claude's
    own words are quoted rather than summarised - a summary of a thesis
    is just another opinion, and the point of keeping the text is that
    the owner can judge the reasoning themselves.

    OWNER-ASKED, second pass: "its already uncollapsed which will get
    messy as there are many open and closed trades" and "it feels word
    heavy". So the whole story folds behind a one-line summary, the
    numbers that were spelled out in sentences are now tiles and a
    drawing, and the prose that survived is the part no figure can say -
    Claude's own reasoning.
    """
    pid = esc(st.position_id[:8])
    out: list[str] = []

    # THE FOUR NUMBERS FIRST, in a row, before any sentence. They used
    # to be a paragraph; a paragraph is not scannable and there will be
    # dozens of these.
    facts = [
        ("Bought", (f"{esc(st.qty)} @ ${esc(st.entry_price)}"
                    if st.entry_price and st.qty else "not filled yet"),
         (f"{esc(st.notional_usd)} dollars committed"
          if st.notional_usd else "no size on record")),
        ("Stop", f"${esc(st.stop_price)}" if st.stop_price else DASH,
         "sells automatically if reached"),
        ("Closes", esc(st.planned_exit_date or "?"),
         "hard exit date, set at entry"),
    ]
    if st.realized_pnl_cents is not None:
        won = st.realized_pnl_cents >= 0
        facts = [
            ("Result", _money(st.realized_pnl_cents),
             ("a profit" if won else "a loss") + " after the exit"),
            ("Sold", f"${esc(st.exit_price or '?')}",
             esc(st.exit_reason or "no reason recorded")),
            ("Held", f"{st.actual_holding_days}d"
             if st.actual_holding_days is not None else DASH,
             f"expected {st.expected_holding_days}d"
             if st.expected_holding_days else "no expectation recorded"),
        ] + facts[:1]
    out.append(tiles(f"{p}-t{index}-tiles", facts))
    # THE POSITION, ON ONE AXIS. Owner-asked for "a graph with multiple
    # points of info" - price, what it cost, what it sells for, and
    # every call to Claude - because the separate small bars answered
    # none of "how is this trade actually going". The rail and hold bar
    # remain as the FALLBACK for a position the full chart cannot draw.
    chart = _position_chart(st, p, index)
    out.append(chart)
    out.append(_technicals(st, p, index))
    if not chart:
        out.append(_hold_progress(st, p, index))
        out.append(_price_rail(st, p, index))
    if not st.entry_price:
        out.append(caveat(
            "The fill has not been reconciled yet, so the price paid is "
            "not on record. Reconciliation runs every cycle."))

    # ---- 2. why this company at all
    out.append(_step("why", f"Why {st.ticker}"))
    if st.origin == "hunt":
        who = pill("good", "Claude found it") + (
            f' &ldquo;{esc(st.nomination_why)}&rdquo;'
            if st.nomination_why else "")
        why_more = ("Claude read the raw feed and nominated this itself. "
                    "The mechanical screen had no rule for it, so nothing "
                    "about this route has been backtested.")
    elif st.origin == "screen":
        who = pill("good", "Mechanical screen")
        why_more = ("The screen is line-for-line the arm the backtest "
                    "graded, so its edge is a measured one rather than a "
                    "judgement.")
    else:
        who = pill("idle", "origin not recorded")
        why_more = ""
    if st.catalyst_type:
        who += " " + pill("idle", str(st.catalyst_type))
        if st.catalyst_date:
            who += " " + pill("idle", f"resolves {st.catalyst_date}")
    out.append(f"<p>{who}</p>")
    if why_more:
        out.append(_why_fold(
            f"{p}-t{index}-whyfold",
            f"<p>{why_more}</p><p>The catalyst type also sets how hard the "
            "risk engine sizes it: a binary event gets a smaller position "
            "than a slow re-rating.</p>"))

    # ---- 3. what Claude concluded, in its own words
    out.append(_step("view", "Claude's view"))
    if not st.thesis:
        out.append(caveat("No research view is on record for this position."))
    else:
        verdict = {"long": "buy it", "short": "short it",
                   "no_trade": "leave it alone"}.get(st.direction,
                                                     st.direction or "?")
        out.append(f"<p>{pill('good', verdict)}</p>")
        if st.conviction is not None:
            # THE GAUGE AND THE TRANSLATION, not one or the other. The
            # picture shows where 0.60 sat against the floor it had to
            # clear; only the sentence says what 0.60 MEANS, and
            # conviction is defined as a frequency precisely because the
            # bare number meant two different things to the model and to
            # the reader for weeks. Trimming text is not a reason to
            # drop the definition.
            out.append(_conviction_gauge(float(st.conviction),
                                         f"{p}-t{index}"))
            out.append(figcap(_plain_conviction(st.conviction)))
        out.append(
            '<blockquote class="said"><b>Its reasoning:</b><br>'
            f"{esc(st.thesis)}</blockquote>")
        out.append(
            '<blockquote class="said"><b>What would prove it wrong:</b><br>'
            f"{esc(st.invalidation)}</blockquote>")
        if st.priced_in_reasoning:
            already = ("already priced in" if st.priced_in
                       else "not yet priced in")
            out.append(
                f'<blockquote class="said"><b>Move {esc(already)}:</b><br>'
                f"{esc(st.priced_in_reasoning)}</blockquote>")
            # "Priced in" is jargon. The gloss is folded rather than cut:
            # a reader who knows the term never has to read it, and one
            # who does not is still one click from the answer.
            out.append(_why_fold(
                f"{p}-t{index}-pricedfold",
                "<p>&ldquo;Priced in&rdquo; is whether the market had "
                "already reacted to this news before the bot could. If it "
                "had, the move is gone and there is nothing left to "
                "trade.</p>"))
        if st.expected_holding_days:
            out.append(f"<p>{pill('idle', f'expected {st.expected_holding_days} trading days')}</p>")

    # ---- 4. what the code then did with that
    out.append(_step("size", "Size and stop"))
    out.append("<p><b>Claude never chooses the amount.</b></p>")
    out.append(_why_fold(
        f"{p}-t{index}-sizefold",
        "<p>Deterministic code works the size out from the account "
        "balance, the most it may lose on one position, and how far this "
        "stock has gapped overnight before. The model has no parameter "
        "through which a number of its own could arrive, and a test holds "
        "that shape. A persuasive thesis and a correct one are different "
        "properties, and a model that sizes its own positions converts "
        "the first into money.</p>"))
    if not st.notional_usd:
        out.append(caveat("No risk decision is on record for this position."))
    # WHY THAT AMOUNT AND NOT MORE. Owner-asked: "will the dashboard
    # explain why it decided to for example spend 15% of account value
    # instead of 30%". The arithmetic is short and completely
    # explainable, and the figures behind it were already being stored -
    # they just were not being shown anywhere a person would look.
    binding = [l for l in st.limits if l[4]]
    if st.limits:
        out.append(f'<h5 id="{p}-t{index}-size">Why that amount, and not '
                   "more</h5>")
        pct = ""
        try:
            if st.equity_at_entry and st.notional_usd:
                share = (Decimal(st.notional_usd)
                         / Decimal(st.equity_at_entry) * 100)
                pct = (f" &mdash; about <b>{share:.0f}% of the "
                       f"{dollars(Decimal(st.equity_at_entry) * 100)} "
                       "account</b>")
        except (ArithmeticError, TypeError, ValueError):
            pct = ""
        if st.equity_at_entry and binding:
            out.append(
                f"<p><b>{esc(st.notional_usd)} dollars</b>{pct}.</p>")
        out.append(_why_fold(
            f"{p}-t{index}-sumfold",
            "<p>One sum: <b>the most it may lose on a single position</b>, "
            "divided by <b>how far this stock could fall before the stop "
            "rescues it</b>. A stop that must sit far away gets a SMALLER "
            "position, because the same dollars of risk buy fewer shares. "
            "Widen the stop and this number falls; a bigger account raises "
            "it proportionally.</p>"))
        rows = []
        # `why` not `note`: `note` is the module-level renderer, and
        # binding it as a loop variable shadowed the function for the
        # whole of _trade_story - which raised UnboundLocalError on the
        # FIRST line of the story, before any of this ran. Exactly the
        # shadowing that produced the `held` / `broker_held` bug in
        # cycle.py.
        for rule, btype, requested, bound, binds, why in st.limits:
            plain = {
                "max_loss_per_position": "most it may lose on one position",
                "per_stock_adverse_gap": "how far this stock has gapped "
                                         "overnight before",
                "per_stock_stop_width": "how far the stop must sit outside "
                                        "this stock's normal noise",
                "max_hold_days": "longest it may hold anything",
                "max_total_exposure": "most that may be invested at once",
                "max_correlated_cluster": "most in one correlated bet",
                "max_open_positions": "how many positions at once",
            }.get(str(rule), str(rule))
            rows.append([esc(plain), esc(str(requested)), esc(str(bound)),
                         "<b>THIS ONE DECIDED IT</b>" if binds
                         else "did not bind", esc(str(why or ""))])
        out.append(table(
            f"{p}-t{index}-limits",
            ["what was checked", "wanted", "allowed", "effect", "why"],
            rows))
        if not binding:
            out.append(prov(
                "Nothing bound: the size came straight out of the sum "
                "above with no limit reducing it."))

    if st.entry_intended and st.entry_price:
        out.append(
            f"<p>{pill('idle', f'quoted ${st.entry_intended}')} "
            f"{pill('idle', f'filled ${st.entry_price}')} "
            f"{pill('idle', f'modelled spread {st.modeled_slippage or chr(63)}c')}</p>")
        out.append(_why_fold(
            f"{p}-t{index}-fillfold",
            "<p>Paper fills pay no spread, so the modelled cost is recorded "
            "BESIDE the broker's price and never instead of it &mdash; "
            "reconciliation still compares against the real fill.</p>"))

    # ---- 5. protection, told as a timeline
    out.append(_step("guard", "Protection"))
    if not st.stop_events:
        out.append(caveat(
            "No stop check has run yet. Checks run every cycle, so this "
            "fills in within about fifteen minutes."))
    else:
        gaps = [e for e in st.stop_events if e[1] != "ok"]
        latest = st.stop_events[-1]
        if latest[1] == "ok":
            out.append(ok(f"<b>Protected.</b> Checked {esc(latest[0])}."))
        else:
            out.append(alarm(
                f"<b>NOT protected.</b> Checked {esc(latest[0])}: "
                f"{esc(str(latest[1]))}."))
        if gaps and latest[1] == "ok":
            out.append(caveat(
                f"{len(gaps)} earlier check(s) found no resting stop, the "
                f"first at {gaps[0][0]} {DASH} resolved, and kept "
                "because a position that was briefly unprotected is worth "
                "knowing about even once it is fixed."))
        rows = [[esc(when), esc(status),
                 esc(str(ids) if ids not in ("[]", None) else "none")]
                for when, status, ids in st.stop_events[-12:]]
        out.append(details(
            f"{p}-t{index}-stopfold", f"every check ({len(st.stop_events)})",
            table(f"{p}-t{index}-stops",
                  ["checked", "status", "resting stop order"], rows)))

    # ---- 6. every order, including the ones that failed
    #
    # OWNER-REPORTED: "the data at the bottom appears to just be raw
    # json not easily understandable". It was: this column used to hold
    # the broker's response object, truncated mid-object at 220
    # characters, which is the exact thing every other panel here is
    # forbidden from doing. It is translated now, and the object folds
    # underneath - house rule 3 asks for the raw response BESIDE the
    # answer, not instead of it.
    if st.orders:
        out.append(_step("orders", "Orders sent"))
        out.append(
            "<p class='prov'>Rejections included &mdash; hiding one is how "
            "a gap goes unnoticed.</p>")
        rows = []
        for j, (when, side, otype, qty, status, raw_text) in enumerate(
                st.orders):
            plain = {"filled": "filled", "rejected": "REJECTED",
                     "new": "resting at the broker",
                     "accepted": "accepted"}.get(str(status), str(status))
            said, exact = _broker_said(raw_text)
            cell = said or "<span class='prov'>nothing recorded</span>"
            if exact:
                cell += (
                    f'<details class="raw-fold" id="{p}-t{index}-o{j}">'
                    "<summary>the broker's exact response</summary>"
                    f"<pre>{esc(json_pretty(exact)[:4000])}</pre></details>")
            rows.append([esc(when), esc(f"{side} {otype}"), esc(qty),
                         esc(plain), cell])
        out.append(table(f"{p}-t{index}-orders",
                         ["sent", "order", "qty", "status",
                          "what that meant"], rows))

    # ---- 7. re-reads, and what it will do next
    out.append(_step("next", "Re-reads, and what happens next"))
    if not st.reviews:
        out.append(
            "<p class='prov'>Nothing re-read yet. Claude re-reads each open "
            "thesis about once a day, sooner if news names the company.</p>")
    else:
        for when, action, triggered, reasoning, changed, skipped in st.reviews:
            if skipped:
                out.append(
                    f"<p class='prov'>{esc(when)} &mdash; review skipped: "
                    f"{esc(str(skipped))}</p>")
                continue
            said = {"hold": ("good", "kept holding"),
                    "exit_now": ("crit", "closed it now"),
                    "no_opinion": ("idle", "had no view")}.get(
                        str(action), ("idle", str(action)))
            out.append(
                f'<blockquote class="said"><b>{esc(when)}</b> '
                + pill(said[0], said[1])
                + (" " + pill("crit", "invalidation triggered")
                   if triggered else "")
                + f"<br>{esc(reasoning)}"
                + ("<br><span class='prov'>What changed: "
                   + esc("; ".join(str(c) for c in changed)) + "</span>"
                   if changed else "")
                + "</blockquote>")
    if st.status == "open" and st.planned_exit_date:
        out.append(note(
            f"<b>Next.</b> The stop at ${esc(st.stop_price or '?')} sells "
            "automatically if reached; otherwise it closes on "
            f"<b>{esc(st.planned_exit_date)}</b>. A review can only bring "
            "that date FORWARD, never push it out."))

    open_or_closed = ("still open" if st.status == "open"
                      else f"closed {esc(st.closed_at or '')}")
    return (
        f'<details class="trade" id="{p}-t{index}"'
        + ("" if folded else " open") + ">"
        f'<summary id="{p}-t{index}-h">{_trade_headline(st)}'
        f'<span class="prov"> &middot; position {pid} &middot; '
        f"{open_or_closed}</span></summary>"
        + "".join(out) + "</details>")


def trades_panel(db: Db, params: dict | None = None, p: str = "tr") -> str:
    """Every trade, past and present, explained in English.

    OWNER-ASKED after the first ever trade: "I want a tab to be actually
    getting data about past and present trades like every thing, if i
    traded i want to know why, the decisions its taking and will take,
    for complete trades an entire breakdown. I also want it breaking
    into english."
    """
    params = params or {}
    wanted = (params.get("id") or [None])[0] if isinstance(
        params.get("id"), list) else params.get("id")
    d = queries.trades(db, wanted)
    out: list[str] = []

    if not d.stories:
        out.append(zero_block(
            f"{p}-none", d.positions_q,
            meaning=("no position has been opened yet. This page fills in "
                     "the moment the first order fills - it reads the "
                     "positions table, which is written at entry.")))
        return section(f"{p}-section", "Trades: what was bought and why",
                       "".join(out))

    # THE BOOK FIRST, then the blotter, then the dossiers. Owner-
    # reported the page "doesnt feel professional enough" and wanted
    # "better metrics": counting open and closed positions is not a
    # metric, it is an inventory. What is at stake, how much of the
    # account is working, and what a unit of risk has actually returned
    # are the three a book is judged on.
    out.append(_book_strip(d.stories, p))
    out.append(_blotter(d.stories, p))
    # ALWAYS FOLDED. Owner-reported twice: "its already uncollapsed which
    # will get messy as there are many open and closed trades", and then
    # "for the trades tab its auto expanded". The first version kept an
    # exception for a lone trade, on the reasoning that folding buys
    # nothing when there is nothing to scroll past. The owner disagreed,
    # and they are the one reading it - a page whose behaviour changes
    # depending on how many rows it has is a page you cannot learn.
    #
    # The one remaining exception is a trade asked for BY ID: following a
    # link to one specific trade must not land on a shut box.
    #
    # What that costs is a page that opens showing nothing, so the shut
    # state has to carry real information - hence the timeline and the
    # summary line below, which is where the answer to "add more detail"
    # went.
    out.append(_hold_timeline(d.stories, p))
    out.append(note("<b>Click any trade for the full story.</b>"))
    for i, st in enumerate(d.stories):
        out.append(_trade_story(st, p, i, folded=st.position_id != wanted))
    return section(f"{p}-section", "Trades: what was bought and why",
                   "".join(out))


def origin_panel(db: Db, p: str = "origin") -> str:
    """Where candidates came from, and whether the model's own picks
    are any better than the screen's.

    OWNER-ASKED: "surely to make this properly agentic we want claude go
    out and finds its own trades".

    It now does - and this page exists because that is the change most
    likely to make the backtest quietly stop describing the running
    system. The graded arm is the mechanical screen; nothing about a
    hunted candidate has ever been measured. Keeping the two tellable
    apart is what turns "are the model's own picks good?" from an
    opinion into a number, and the number needs months, so the counting
    starts now.
    """
    d = queries.origin_split(db)
    out: list[str] = []

    out.append(note(
        "<b>Two things now produce candidates.</b> The <b>screen</b> is "
        "mechanical - Form 4 clusters and cross-feed agreement - and is "
        "line-for-line the arm that was backtested, so its measured edge "
        "means something. The <b>hunt</b> is Claude reading the raw feed "
        "once a day and nominating what the screen has no rule for. Both "
        "go through the identical research, pricing and risk path; "
        "nothing downstream knows which is which. They are counted "
        f"separately here so the record can eventually say which is "
        "worth the money."))

    if not d.rows:
        out.append(zero_block(
            f"{p}-none", d.origins_q,
            meaning=("no candidate has been stamped with an origin yet. "
                     "Stamping happens as candidates are built, so this "
                     "fills in on the next discovery pass.")))
        return section(f"{p}-section", "Who found it: screen or Claude",
                       "".join(out))

    out.append(tiles(f"{p}-tiles", [
        ("From the screen", f"{d.n_screened:,}",
         "mechanical, and the arm the backtest graded"),
        ("From Claude's hunt", f"{d.n_hunted:,}",
         "nominated from the raw feed, then validated against it"),
    ]))

    rows = []
    for origin, cands, researched, directional, traded in d.rows:
        label = {"screen": "screen (mechanical)",
                 "hunt": "hunt (Claude)"}.get(str(origin), str(origin))
        rows.append([esc(label), f"{cands:,}", f"{researched:,}",
                     f"{directional:,}", f"{traded:,}"])
    out.append(table(
        f"{p}-funnel",
        ["origin", "candidates", "researched", "directional view", "traded"],
        rows, numeric_cols={1, 2, 3, 4}))
    out.append(caveat(
        "These counts are NOT a verdict and will not be one for months. "
        "A source that produced three candidates and one trade has told "
        f"you nothing yet {DASH} the refusals page is where this "
        "eventually gets settled, by scoring what each source's declined "
        "candidates went on to do."))

    if d.recent:
        rows = [[esc(when), esc(ticker or DASH), esc(ctype or DASH),
                 esc(str(cdate or DASH)),
                 esc(direction or "not researched yet"),
                 (f"{float(conv):.2f}" if conv is not None else DASH),
                 esc((rationale or "")[:400])]
                for when, ticker, ctype, cdate, rationale, direction, conv
                in d.recent]
        out.append(f'<h3 id="{p}-recent-h">What Claude nominated, and why</h3>')
        out.append(table(
            f"{p}-recent",
            ["nominated", "ticker", "type", "resolves", "view", "conviction",
             "why it was worth researching"],
            rows, numeric_cols={5}))
        out.append(prov(
            "The reason is the model's own, written before any research "
            "happened. It is not the trade thesis - a full research pass "
            "writes that afterwards, and it is on the decision page."))
    return section(f"{p}-section", "Who found it: screen or Claude",
                   "".join(out))


def _quote_cross_check_block(d, p: str) -> str:
    """Did anything DISAGREE with the live quote?

    This sits below the fill table because it is upstream of it: every
    figure in that table descends from one live Alpaca quote, and a
    refused quote never becomes an order at all, so it appears nowhere
    else on this page.

    THE THREE VERDICTS MEAN DIFFERENT THINGS and the page has to keep
    them apart:

      consistent   the cached close agrees; nothing to see
      flagged      a large move, PASSED THROUGH on purpose - large moves
                   are what this bot trades, and refusing them would
                   throw away its whole reason for existing
      REFUSED      a deviation no session produces; the shape of a
                   decimal error, a wrong symbol or an unadjusted
                   corporate action. No order was placed.
      not checked  there was no cached history to compare against. NOT
                   the same as passing, and shown as its own count for
                   exactly that reason.
    """
    out = [
        f'<h3 id="{p}-quotes-h">Did anything disagree with the price?</h3>',
        note("Every figure above descends from <b>one</b> live Alpaca "
             "quote. Before the risk engine runs, that quote is compared "
             "against the newest daily close already cached for the same "
             "ticker. Yesterday's close cannot confirm today's price, but "
             "it can refuse to believe a hundredfold one."),
    ]
    if not d.n_quote_checks:
        out.append(zero_block(
            f"{p}-noquotes", d.quotes_q,
            meaning=("no quote has been cross-checked yet. A row is "
                     "written every time a researched candidate is priced, "
                     "so this fills in with the first research pass.")))
        return "".join(out)

    out.append(tiles(f"{p}-quote-tiles", [
        ("Quotes cross-checked", f"{d.n_quote_checks:,}",
         "researched candidates priced since records began"),
        ("Refused", f"{d.n_quote_refused:,}",
         "no order placed - the quote failed its own check"),
        ("Flagged, traded anyway", f"{d.n_quote_flagged:,}",
         "a large but believable move, passed through on purpose"),
        ("Not checked", f"{d.n_quote_unchecked:,}",
         "no cached history to compare against - not the same as passing"),
    ]))
    rows = [[esc(t or DASH), esc(live or DASH), esc(ref or DASH),
             esc(day or DASH),
             (f"{Decimal(dev) * 100:+.1f}%" if dev else DASH),
             esc(verdict), esc(when)]
            for t, live, ref, day, dev, verdict, _note, when
            in d.quote_checks]
    out.append(table(
        f"{p}-quotes",
        ["ticker", "live quote", "cached close", "as of", "deviation",
         "verdict", "when"],
        rows, numeric_cols={1, 2, 4}))
    # THE SENTENCE, for the rows that need one. A verdict with no
    # figures behind it is the thing this project refuses to accept
    # anywhere else, and "REFUSED" in a table cell is exactly that.
    said = [(t, sentence) for t, _l, _r, _d, _dev, verdict, sentence, _w
            in d.quote_checks if verdict in ("REFUSED", "flagged")]
    if said:
        out.append(f'<div class="note" id="{p}-quote-why"><b>Why:</b><ul>' +
                   "".join(f"<li><b>{esc(t or DASH)}</b> {esc(s)}</li>"
                           for t, s in said) + "</ul></div>")
    out.append(caveat(
        "A flag is an observation, not a fault. A stock can genuinely "
        f"gap 40% on a readout {DASH} that is the trade, and refusing it "
        "would quietly discard exactly the candidates this bot exists to "
        "take. Only a deviation beyond fivefold is refused, because no "
        "single session produces one."))
    return "".join(out)


def story_panel(db: Db, params: dict | None = None, p: str = "story") -> str:
    """One headline, and what the bot made of it.

    OWNER-ASKED: "I want to be able to click the news and see what the
    bot thought of each and connectiosn".

    Written as a NARRATIVE, in the order a person would ask the
    questions: what was published, was it about a company the bot
    follows, did anything else say the same thing, did it become a
    candidate, what did the model conclude, and what did the code then
    do. BUILD-BRIEF's test is that "someone who was not there can read a
    single trade and understand why it was made".

    THE COMMON CASE IS THAT A STORY LED NOWHERE, and that is stated
    plainly rather than rendered as an empty panel. "Nothing happened"
    and "this page is broken" look identical otherwise, and telling them
    apart is repeatedly the whole diagnosis.
    """
    params = params or {}
    got = params.get("id")
    sid = (got[0] if isinstance(got, list) else got) or ""
    if not sid:
        return section(f"{p}-section", "A news story",
                       "<p>Open a story from the "
                       "<a href='/newsmap'>news map</a> &mdash; click any "
                       "headline in the left-hand column.</p>")

    d = queries.story_detail(db, sid)
    if not d.found:
        return section(
            f"{p}-section", "A news story",
            zero_block(f"{p}-missing", d.story_q,
                       meaning=(
                           f"no stored news story has the id "
                           f"{esc(sid)}. Stories are kept as they arrive "
                           "from the feed; one from before this install, or "
                           "from a window that has since been pruned, will "
                           "not be here.")))

    out: list[str] = []
    arrow = " &rarr; "
    hint = {1: "read as good news", -1: "read as bad news",
            0: "no direction read from it"}.get(d.hint, "no direction")

    out.append(tiles(f"{p}-tiles", [
        ("Company", esc(d.ticker or "&mdash;"),
         "the ticker the feed attached to this story"),
        ("Kind", esc(d.catalyst.replace("_", " ")), hint),
        ("Became a candidate", "yes" if d.candidates else "no",
         f"{len(d.candidates)} candidate row(s) for this company"),
    ]))

    # 1. What was said.
    out.append(f'<h3 id="{p}-said">1. What was said</h3>')
    headline_text = d.headline or "(the stored story carries no headline)"
    out.append(
        f'<p class="lead" id="{p}-headline">{esc(headline_text)}</p>')
    # prov_html, not prov: the parts are already escaped and there is a
    # <code> tag here. prov() would escape it all a second time and
    # print the tag on the page.
    out.append(prov_html(
        f"{esc(d.publisher or 'publisher not recorded')}, "
        f"{esc(d.when or 'date not recorded')}. Stored id "
        f"<code>{esc(d.source_id)}</code>. This is the feed's own text, "
        "kept verbatim."))

    # 2. Did anything else say it too?
    out.append(f'<h3 id="{p}-corrob">2. Did anything else say the same?</h3>')
    if d.corroboration:
        out.append(note(
            "<b>Yes &mdash; another feed named the same company.</b> That is "
            "two independent observations rather than one newsroom, which "
            "is the only kind of link this bot treats as more than "
            "coincidence, and it is what earns a candidate a bigger "
            "research budget."))
        out.append(table(
            f"{p}-corrob-table", ["feed", "what it said"],
            [[esc(src), esc(text or sid2)]
             for src, sid2, text in d.corroboration[:12]]))
    else:
        out.append(note(
            f"<b>No.</b> Only the news feed mentioned {esc(d.ticker)} in the "
            "stored window. A single newsroom saying something is one "
            "observation, and the bot weights it as such &mdash; this is "
            "normal, not a fault."))

    # 3. What the bot thought, and did.
    out.append(f'<h3 id="{p}-thought">3. What the bot thought of it</h3>')
    if not d.candidates:
        out.append(note(
            f"<b>Nothing was built from this story.</b> No candidate row "
            f"exists for {esc(d.ticker)}, so the model was never asked "
            "about it and no decision was made. That is the ordinary "
            "outcome for most stories: the news feed is used for "
            "corroboration and sentiment, and on its own it does not "
            "manufacture a candidate. The "
            f'<a href="/funnel">funnel</a> shows how many reach each '
            "stage and why the rest stop."))
    for item in d.candidates:
        cid = str(item.get("id") or "")
        made_it = item.get("from_this_story")
        head = (f'<b>{esc(item.get("catalyst_type") or "candidate")}</b> '
                f'&mdash; discovered {esc(str(item.get("discovered_at"))[:19])}'
                + (' <span class="pill good">built from THIS story</span>'
                   if made_it else
                   ' <span class="pill idle">same company, different '
                   'evidence</span>'))
        body = [f"<p>{head}</p>"]

        if item.get("direction"):
            body.append(
                f'<p><b>The model read it as {esc(item["direction"])}</b>, '
                f'conviction {esc(item.get("conviction"))}. '
                f'{esc(item.get("thesis") or "")}</p>')
            if item.get("invalidation"):
                body.append(prov("What would prove it wrong: "
                                 + esc(item["invalidation"])))
            if item.get("priced_in"):
                body.append(note(
                    "<b>The model judged this already priced in.</b> "
                    + esc(item.get("priced_in_reasoning") or "")
                    + " That does not veto the trade &mdash; it raises the "
                    "conviction bar the candidate has to clear."))
        else:
            body.append(note(
                "<b>The model was never asked about this one.</b> It was "
                "discovered but not researched, which the "
                '<a href="/funnel">funnel</a> explains by stage.'))

        action = item.get("action")
        if action == "trade":
            body.append(
                f'<p><b>The risk engine traded it:</b> '
                f'{dollars(_cents(item.get("notional_usd")))}, stop at '
                f'{esc(item.get("stop_price"))}, exit by '
                f'{esc(item.get("planned_exit_date"))}.</p>')
        elif action == "skip":
            reasons = jload(item.get("skip_reasons"), []) or []
            body.append(
                "<p><b>The risk engine declined it.</b> "
                + ", ".join(f"<code>{esc(r)}</code>" for r in reasons)
                + "</p>")
        else:
            body.append(prov("No risk decision is recorded for this "
                             "candidate yet."))

        # WHY THE SIZE WAS THE SIZE. These sentences come from the
        # per-stock sizing bounds and answer the question a number
        # cannot.
        for rule, why in (item.get("notes") or []):
            body.append(prov(f"{esc(rule.replace('_', ' '))}: {esc(why)}"))

        if cid:
            body.append(
                f'<p><a href="/decision?candidate_id={esc(cid)}">Read the '
                f"whole decision for this candidate</a>{arrow}every query "
                "behind it, in order.</p>")
        out.append(details(
            f"{p}-cand-{esc(cid)}",
            f'{esc(item.get("catalyst_type") or "candidate")} '
            f'&mdash; {esc(cid or "no id")}',
            "".join(body)))

    out.append(prov(
        f"Provenance: the story is one row of raw_events "
        f"({d.story_q.row_count if d.story_q else 0} matched); the "
        f"candidates are "
        f"{d.cand_q.row_count if d.cand_q else 0} row(s) joined to "
        "research_views and risk_decisions on candidate_id. Nothing here "
        "is inferred."))
    return section(f"{p}-section",
                   f"News: {d.ticker or 'story'} &mdash; what the bot made "
                   "of it", "".join(out))


# --------------------------------------------------------------------------
# The chain: every decision, in order, with its justification
# --------------------------------------------------------------------------


def chain_panel(db: Db, p: str = "chain") -> str:
    """What happened, then what, and why - in order.

    The brain map answers "what is connected to what". It cannot answer
    "what happened next", because a picture of a graph has no order in
    it. The owner asked for the other thing in these words: "I want
    every decision with justification in order from what is researched
    to find to placing a trade."

    Each step expands to its evidence rather than linking away, so the
    story can be read top to bottom without losing your place. <details>
    does that with no JavaScript, which keeps the page reproducible and
    printable.
    """
    data = queries.decision_chains(db)
    out = []
    if not data.chains:
        out.append(note(
            f'<b id="{p}-quiet">No candidates yet.</b> This fills in as '
            "discovery runs. Each one becomes a chain: what was found, what "
            "linked to it, what the model concluded, what the risk engine "
            "did with that, and what happened at the broker."))
        if data.query is not None:
            out.append(empty_block(f"{p}-q", data.query,
                                   meaning="no candidate rows yet"))
        return section(f"{p}-section", "Every decision, in order", "".join(out))

    out.append(
        f'<p id="{p}-intro">The newest {len(data.chains)} candidates, each '
        "read top to bottom. <b>Every step says why it moved on, or why it "
        "stopped.</b> Open a step to see the evidence it rested on.</p>")

    for ci, chain in enumerate(data.chains):
        pill_cls = {"traded": "good", "declined": "quiet",
                    "in progress": "warn"}.get(chain.verdict, "quiet")
        out.append(
            f'<div class="chain" id="{p}-{ci}">'
            f'<h3 class="chain-head">{esc(chain.ticker)} '
            f'{pill(pill_cls, esc(chain.verdict))}</h3>')
        for step in chain.steps:
            cls = "chain-step stopped" if step.stopped else "chain-step"
            body = "".join(
                f'<div class="chain-fact"><span class="chain-k">{esc(k)}</span>'
                f'<span class="chain-v">{esc(v)}</span></div>'
                for k, v in step.detail) or "<p class='prov'>nothing recorded</p>"
            link = (f'<a class="chain-link" href="{step.href}">the full '
                    "record for this step &rarr;</a>" if step.href else "")
            out.append(
                f'<details class="{cls}" id="{p}-{ci}-{step.n}">'
                f"<summary><span class='chain-n'>{step.n}</span>"
                f"<span class='chain-stage'>{esc(step.stage)}</span>"
                f"<span class='chain-text'><b>{esc(step.headline)}</b>"
                f"<span class='chain-why'>{esc(step.why)}</span></span>"
                "</summary>"
                f'<div class="chain-body">{body}{link}</div></details>')
        out.append("</div>")
    return section(f"{p}-section", "Every decision, in order", "".join(out))


def open_positions_panel(db: Db, p: str = "reviews") -> str:
    """What we are holding, and what the bot has said about it since.

    Owner-asked: "need a section for the bot to re-evaluate every now
    and again for current trades". The re-evaluation runs on every
    cycle, but until now it was only visible buried inside one trade's
    dossier - so a feature that was working looked like one that was
    not.

    A review that said HOLD is shown beside one that acted. Listing only
    the reviews that changed something would make the model look
    decisive in hindsight and hide the far more common answer.
    """
    from catalyst.research.position_review import REVIEW_INTERVAL_HOURS

    rows = db.q(
        "SELECT p.id, p.ticker, p.opened_at, p.planned_exit_date "
        "FROM positions p WHERE p.status = 'open' ORDER BY p.opened_at")
    out = []
    if not rows.rows:
        out.append(note(
            f'<b id="{p}-none">Nothing is open right now.</b> When a '
            "position is open, the bot re-reads its thesis about every "
            f"{REVIEW_INTERVAL_HOURS} hours and says whether it still "
            "holds. A review can only ever bring the exit date FORWARD, "
            "never push it out."))
        out.append(empty_block(f"{p}-q", rows,
                               meaning="no open positions"))
        return section(f"{p}-section", "Open positions, re-checked",
                       "".join(out))

    out.append(
        f'<p id="{p}-intro">The bot re-reads each open thesis about every '
        f"{REVIEW_INTERVAL_HOURS} hours. <b>A review can only bring the exit "
        "date forward, never push it out</b> - 'hold' is the absence of a "
        "reason to leave early, not permission to stay longer.</p>")
    for i, pos in enumerate(rows.rows):
        revs = db.q(
            "SELECT action, invalidation_triggered, reasoning, "
            "       what_changed_json, skipped_reason, reviewed_at "
            "FROM position_reviews WHERE position_id = ? "
            "ORDER BY reviewed_at DESC", (pos["id"],))
        out.append(
            f'<div class="chain" id="{p}-{i}">'
            f'<h3 class="chain-head">{esc(pos["ticker"])} '
            f'<span class="prov">opened {esc(pos["opened_at"])[:10]}, '
            f'closes {esc(pos["planned_exit_date"])}</span></h3>')
        if not revs.rows:
            out.append(empty_block(
                f"{p}-{i}-none", revs,
                meaning="not re-checked yet. Expected for a position opened "
                        "today or closing tomorrow - both skip the review "
                        "deliberately - and a defect for anything held longer"))
        for r in revs.rows:
            changed = jload(r["what_changed_json"], []) or []
            answer = r["skipped_reason"] and "not obtained" or str(r["action"])
            cls = ("chain-step stopped" if answer == "exit_now"
                   else "chain-step")
            out.append(
                f'<details class="{cls}">'
                f"<summary><span class='chain-stage'>{esc(answer)}</span>"
                f"<span class='chain-text'><b>{esc(str(r['reviewed_at'])[:16])}"
                "</b><span class='chain-why'>"
                f"{esc(str(r['skipped_reason'] or r['reasoning'])[:200])}"
                "</span></span></summary>"
                "<div class='chain-body'>"
                f'<div class="chain-fact"><span class="chain-k">invalidation '
                f'triggered</span><span class="chain-v">'
                f'{"yes" if r["invalidation_triggered"] else "no"}</span></div>'
                + "".join(
                    f'<div class="chain-fact"><span class="chain-k">changed'
                    f'</span><span class="chain-v">{esc(str(c))}</span></div>'
                    for c in changed)
                + "</div></details>")
        out.append("</div>")
    return section(f"{p}-section", "Open positions, re-checked", "".join(out))


def node_panel(db: Db, node_id: str, p: str = "node") -> str:
    """One node of the map, opened, with its runbook.

    Owner-asked: "or i can click in and it opens another page with the
    runbook". A map tells you a thing is connected to another thing and
    then leaves you to work out what either of them means. This says
    what THIS node is, everything recorded as connecting to it, and what
    to do when you land here.
    """
    d = queries.node_detail(db, node_id)
    out = []
    if not d.found:
        out.append(note(
            f'<b id="{p}-missing">Nothing in the map has the id '
            f"{esc(d.node_id)}.</b> The graph is rebuilt from rows that "
            "exist, so a node vanishes when the rows behind it are "
            "removed or fall outside the window the map draws. That is "
            "not an error - but nothing can be shown for it."))
        out.append(f'<p><a href="/brain">Back to the map</a></p>')
        return section(f"{p}-section", "Node not in the map", "".join(out))

    out.append(
        f'<p id="{p}-what"><span class="chain-stage">{esc(d.kind_label)}'
        f"</span></p><p><b>{esc(d.label)}</b></p>")
    out.append(note(f'<b id="{p}-runbook">What this is, and what to do.</b> '
                    + esc(d.runbook)))

    for title, rows, direction in (
            ("What led here", d.incoming, "from"),
            ("What it led to", d.outgoing, "to")):
        out.append(f"<h3>{title}</h3>")
        if not rows:
            out.append(prov(
                f"Nothing recorded {direction} this node. Every line on "
                "the map is a stored row, so an empty side means no row "
                "joins them - not that the link is missing from the "
                "picture."))
            continue
        out.append(table(
            f"{p}-{direction}", ["node", "what the record says"],
            [[esc(label), esc(why)] for label, why in rows[:60]]))
        if len(rows) > 60:
            out.append(prov(f"{len(rows) - 60} more not shown."))

    out.append("<h3>Where to next</h3>")
    out.append("<p>" + " &middot; ".join(
        f'<a href="{esc(href)}">{esc(text)}</a>' for text, href in d.links)
        + "</p>")
    return section(f"{p}-section", f"Node: {esc(d.label)}", "".join(out))


#: What each queued action is, at a glance. Beside the words, never
#: instead of them, and aria-hidden - the same rule as the trade steps.
_ACTION_ICON = {"review": "\U0001F9E0", "exit": "\U0001F6AA",
                "hunt": "\U0001F50D", "blocked": "⏸"}


def next_actions_panel(db: Db, p: str = "na") -> str:
    """What the bot will do next, and when.

    OWNER-ASKED: "can we add a next actions tab e.g. when will claude
    next evaluate the choice and say sell or keep".

    Everything on this page is asked of the code that actually decides -
    position_review.should_review, last_reviewed_at and news_since, the
    same three the live cycle calls. Restating the schedule here would
    make the page a second source of truth, and a dashboard that
    confidently names the wrong next action is worse than one that says
    nothing, because the owner plans around it.

    WHAT IT CANNOT PROMISE, and says so: that any of this fires. A
    tripped kill switch, an exhausted budget or a stopped service each
    stop the cycle, and none of them are visible from a schedule.
    """
    d = queries.next_actions(db)
    out: list[str] = []

    if d.error:
        out.append(alarm(f"The schedule could not be read: {esc(d.error)}"))
        out.append(empty_block(f"{p}-err", d.positions_q,
                               meaning="the positions table could not be read"))
        return section(f"{p}-section", "What happens next", "".join(out))

    if not d.actions:
        out.append(zero_block(
            f"{p}-none", d.positions_q,
            meaning=("nothing is queued because nothing is open. Reviews "
                     "and exits are both properties of a held position, so "
                     "this page fills in when the next order fills.")))
        return section(f"{p}-section", "What happens next", "".join(out))

    due = [a for a in d.actions if a.due_now]
    reviews = [a for a in d.actions if a.kind == "review"]
    out.append(tiles(f"{p}-tiles", [
        ("Due now", f"{len(due)}", "on the next cycle, budget allowing"),
        ("Open positions", f"{d.n_open}", "each re-read on its own clock"),
        ("Re-read every", f"{d.interval_hours}h",
         f"sooner on news, never inside {d.min_gap_hours}h"),
    ]))

    rows = []
    for i, a in enumerate(d.actions):
        ico = _ACTION_ICON.get(a.kind, _ACTION_ICON["review"])
        state = ("crit" if a.kind == "exit" and a.due_now
                 else "good" if a.due_now
                 else "idle" if a.kind != "blocked" else "warn")
        rows.append([
            f'<span class="step-ico" aria-hidden="true">{ico}</span>'
            f"<b>{esc(a.what)}</b>",
            pill(state, a.when_words),
            esc(a.when[:16].replace("T", " ") if a.when else "—"),
            f'<span class="prov-inline">{esc(a.detail)}</span>',
        ])
    out.append(table(f"{p}-table",
                     ["what", "when", "at", "why then"], rows))

    out.append(note(
        "<b>Claude answers one of three things at a review:</b> keep "
        "holding, close it now, or no opinion. It can bring an exit date "
        "<b>forward</b> and never push it out."))
    out.append(_why_fold(f"{p}-why", (
        "<p>Reviews are gated so a position is not paid for repeatedly to "
        f"be told nothing changed: at most one every {d.min_gap_hours} "
        f"hours, otherwise on a {d.interval_hours}-hour clock, brought "
        "forward the moment a feed publishes something naming the "
        "company. A position opened today is skipped - there is nothing "
        "new to find yet - and so is one closing tomorrow, because an "
        "early exit would not settle any sooner.</p>"
        "<p>These times are what the schedule ALLOWS, not a promise the "
        "cycle runs. A tripped kill switch, an exhausted daily budget or "
        "a stopped service each stop all of it, and none of them are "
        "visible from a schedule. The Overview and Cost pages are where "
        "those live.</p>")))
    return section(f"{p}-section", "What happens next", "".join(out))


def _exposure_warning(perf, p: str) -> str:
    """The condition the excess figure may only be read under.

    OWNER-REPORTED: "ensure it is 100% accurate, i dont want the false
    idea we are beating SPY".

    THE COMPARISON IS NOT LIKE FOR LIKE AND CANNOT BE MADE SO with what
    the schema records. SPY is 100% invested every day of the window.
    This account holds a handful of positions and is mostly cash, so in
    any falling market it loses less than the index - and that is not
    skill, it is not being in the market. The reverse is just as true:
    in a rising market it will look worse than it is.

    A caveat, not a correction: exposure-matching properly needs a daily
    position-value series, which nothing writes yet. Inventing one from
    today's exposure would be worse than saying so, because it would
    look like a measurement.

    It sits BESIDE the number rather than in the provenance fold, which
    is where it used to be - while the tile above it claimed
    "exposure-matched". The page contradicted itself, and the half that
    was easy to see was the wrong half.
    """
    deployed = ""
    try:
        equity = Decimal(perf.net_equity_cents or 0)
        invested = Decimal(getattr(perf, "open_notional_cents", 0) or 0)
        if equity > 0 and invested > 0:
            deployed = (f" Right now about <b>{invested / equity * 100:.0f}%"
                        "</b> of the account is invested; SPY is 100% "
                        "invested every day of this window.")
    except (ArithmeticError, TypeError, ValueError):
        deployed = ""
    return caveat_html(
        "Read the excess figure with this in mind: <b>the two lines are "
        "not like for like.</b> SPY is fully invested throughout. This "
        "account is mostly cash between trades, so it falls less than "
        "the index in a down market and rises less in an up one."
        + deployed
        + " Matching them properly needs a daily record of what the "
        "positions were worth, which nothing writes yet - so this is "
        "stated rather than silently corrected.")


def _equity_bridge(perf, p: str) -> str:
    """What actually moved the account value, as parts rather than a sum.

    OWNER-ASKED: the account-value tile is one figure standing for four
    different things - the money you started with, trading profit that
    is currently FICTIONAL because the account is paper, and an API bill
    that is real money leaving a real card. The brief is explicit about
    this ("Paper P&L is fictional; the API bill is real money") and the
    tile was quietly averaging the two into a single number that read as
    one kind of thing.

    A bridge, because the question is never "what is the total" - it is
    "what moved it, and which of those do I actually care about".

    WHAT IT DELIBERATELY DOES NOT CONTAIN: unrealised profit on open
    positions. net_equity_cents is built from CLOSED trades only, so an
    open winner is invisible here. Said on the page rather than left for
    the owner to discover by arithmetic.
    """
    start = Decimal(perf.start_capital_cents or 0)
    pnl = Decimal(perf.gross_pnl_cents or 0)
    sched = Decimal(perf.scheduled_cost_cents or 0)
    manual = Decimal(perf.manual_cost_cents or 0)
    net = Decimal(perf.net_equity_cents or 0)
    api = sched + manual
    if start <= 0:
        return ""

    # Widths are proportional to the START, so the bar reads as "how
    # much of the account did this move" rather than being rescaled to
    # make a tiny cost look large.
    def pct(v):
        return float(abs(v) / start * 100) if start else 0.0

    segs = []
    if pnl:
        segs.append(("pnl", pnl, "trading" + (" profit" if pnl > 0 else " loss")))
    if api:
        segs.append(("api", -api, "API spend"))
    bars = "".join(
        f'<span class="bridge-seg bridge-{kind}" '
        f'style="width:{min(pct(v), 100):.2f}%" '
        f'title="{esc(label)}: {dollars(abs(v))}"></span>'
        for kind, v, label in segs)

    rows = [
        ["Started with", dollars(start), "the baseline this is measured from"],
        ["Trading profit or loss", dollars(pnl),
         (f"{pill('idle', 'paper')} closed trades only &mdash; fictional "
          "until the account is live")],
        ["API spend", "-" + dollars(api),
         (f"{pill('crit', 'real money')} {dollars(sched)} scheduled, "
          f"{dollars(manual)} manual")],
        ["<b>Account value</b>", f"<b>{dollars(net)}</b>",
         "what the two above leave"],
    ]
    return (
        f'<div class="bridge" id="{p}-bridge">'
        "<h3>What moved the account value</h3>"
        f'<div class="bridge-bar"><span class="bridge-seg bridge-start" '
        f'style="width:100%"></span>{bars}</div>'
        + table(f"{p}-bridge-t", ["", "amount", "what it is"], rows,
                numeric_cols={1})
        + figcap(
            "<b>These are not the same kind of number.</b> The trading "
            "line is paper and settles nothing; the API line has already "
            "left a real card. Open positions are NOT in this - it is "
            "built from closed trades, so an open winner is invisible "
            "here until it closes.")
        + "</div>")


def _pct(value, places=1):
    return DASH if value is None else f"{value:.{places}f}%"


def _r(value):
    """An R multiple. Signed always: +1.8R and -1.0R are the two things
    a reader is scanning for, and an unsigned 1.8 hides which."""
    return DASH if value is None else f"{value:+.2f}R"


def _blotter(stories, p: str) -> str:
    """Every position on one dense line, the way a book is actually read.

    OWNER-REPORTED: "The trades part doesnt feel professional enough, i
    want better metrics, i feels like a robot made it".

    The page printed what was STORED - price, quantity, dollars - and
    left every derived number to the reader. A blotter is defined by the
    derived ones, and the two that matter were both sitting in the
    database already:

      RISK. qty x (entry - stop) is the money actually at stake. Showing
      notional without it says how much was SPENT, not how much can be
      LOST, and here those differ by a factor of ten. It is also the
      number the position was sized from, so without it the sizing
      cannot be checked.

      R MULTIPLE. Result divided by that initial risk. It is how a
      discretionary book is graded, because it makes a $40 win on $10 of
      risk and a $400 win on $100 of risk the same result - which they
      are. Raw P&L flatters whichever trade happened to be biggest.

    Figures are right-aligned and tabular so columns can be compared
    down the page rather than read across it.
    """
    if not stories:
        return ""
    rows = []
    for i, st in enumerate(stories):
        m = queries.trade_metrics(st)
        live = st.status == "open"
        rows.append([
            f'<a href="/trades?id={esc(st.position_id)}">'
            f"<b>{esc(st.ticker)}</b></a>",
            pill("good" if live else "idle", "open" if live else "closed"),
            esc(str(st.opened_at)[:10]),
            esc(st.qty or DASH),
            f"${esc(st.entry_price)}" if st.entry_price else DASH,
            f"${esc(st.stop_price)}" if st.stop_price else DASH,
            _pct(m.stop_pct),
            dollars(m.risk_usd * 100) if m.risk_usd is not None else DASH,
            _pct(m.exposure_pct, 0),
            (dollars(m.pnl_usd * 100) if m.pnl_usd is not None else DASH),
            _r(m.r_multiple),
            f"{float(st.conviction):.2f}" if st.conviction is not None else DASH,
        ])
    return (
        "<h3>Positions</h3>"
        + table(f"{p}-blotter",
                ["ticker", "state", "opened", "qty", "entry", "stop",
                 "stop dist", "risk $", "exposure", "result", "R", "conv"],
                rows, numeric_cols={3, 4, 5, 6, 7, 8, 9, 10, 11})
        + figcap(
            "<b>Risk $</b> is what this position can lose if the stop "
            "does its job &mdash; quantity times the distance from the "
            "fill to the stop, and the number the size was worked out "
            "from. <b>R</b> is the result as a multiple of that risk, "
            "which is how a book is graded: it makes a small win on a "
            "small risk and a large win on a large risk read as the same "
            "result, because they are."))


def _book_strip(stories, p: str) -> str:
    """The book, not the trades: what is at stake now and how the
    finished ones actually went - with the sample size attached to every
    average, because at this many trades none of them mean anything yet
    and a page that implies otherwise is worse than a blank one."""
    b = queries.book_metrics(stories)
    at_risk = (dollars(b.open_risk_usd * 100)
               if b.open_risk_usd is not None else DASH)
    deployed = _pct(b.deployed_pct, 0)
    tiles_ = [
        ("At risk now", at_risk,
         f"{b.n_open} open &mdash; the most the stops should let go"),
        ("Deployed", deployed,
         (dollars(b.open_exposure_usd * 100)
          if b.open_exposure_usd is not None else DASH) + " of the account"),
    ]
    if b.graded:
        state = "good" if b.enough_sample else "warn"
        tiles_.append((
            "Expectancy", _r(b.expectancy_r),
            f"{pill(state, f'{b.graded} of {MIN_TRADES_FOR_MEANING}')} "
            "graded trades &mdash; average result per unit of risk"))
        tiles_.append((
            "Win rate", _pct(b.win_rate, 0),
            f"{b.wins} won, {b.losses} lost"))
    else:
        tiles_.append((
            "Expectancy", DASH,
            f"{pill('idle', 'no closed trades')} needs a finished trade "
            "with a recorded stop to grade"))
    out = [tiles(f"{p}-book", tiles_)]
    if b.graded and not b.enough_sample:
        out.append(caveat(
            f"Expectancy and win rate rest on {b.graded} trade(s). "
            f"{MIN_TRADES_FOR_MEANING} is the floor this dashboard uses "
            "before a number like that is allowed to mean anything - "
            "until then they describe what happened, not what to expect."))
    if b.best_r is not None and b.graded > 1:
        out.append(figcap(
            f"Best {_r(b.best_r)}, worst {_r(b.worst_r)}."))
    return "".join(out)


#: Calendar days of price history drawn BEFORE the entry, so the chart
#: shows what the stock was doing when the bot decided to buy it. Long
#: enough that a 20-day average exists; short enough that the position
#: itself is still the subject of the picture.
CONTEXT_DAYS_BEFORE_ENTRY = 60


def _load_position_bars(ticker: str):
    """Daily closes for a held ticker, or (). Never raises: a missing
    cache is a chart that does not draw, not a page that does not."""
    try:
        from catalyst.backtest.data import BarCache
        from catalyst.dashboard.db import bars_path

        return BarCache(bars_path()).load_bars(ticker)
    except Exception:            # noqa: BLE001 - absence is normal
        return ()


def _position_chart(st, p: str, index: int) -> str:
    """One position, everything that has happened to it, on one axis.

    OWNER-ASKED: "i cant accurately see how well my current trades are
    going i want a graph with multiple points of info e.g. when is it
    calling to claude for a tech, current costs, original cost, sell
    cost etc".

    So the axis is TIME, from the day it was bought to the day it must
    close, and everything that matters is plotted against it:

      - the price line, from this ticker's own cached daily closes;
      - what it cost to buy and what it sells for if the thesis fails,
        as horizontal rules, because the gap between them is the money
        at stake;
      - a marker every time Claude was called to re-check the thesis,
        and what it said;
      - today, and the hard exit date where it closes regardless.

    NOTHING IS PROJECTED. The price line stops where the data stops. The
    only marks in the future are DATES the bot has already committed to,
    never a price it might reach.
    """
    entry = _num(st.entry_price)
    stop = _num(st.stop_price)
    start = _as_date(st.opened_at)
    end = _as_date(st.planned_exit_date)
    if not (entry and stop and start and end and end > start):
        return ""
    # THE RUN-UP INTO THE TRADE, not just the hold. A days-to-weeks
    # position is too short to read on its own - and a 20-day average
    # over a 12-day hold does not exist at all, so the trend line never
    # drew. Starting the window before the entry gives both: whether the
    # stock was already extended when it was bought, and enough bars for
    # the average to mean something.
    chart_start = start - timedelta(days=CONTEXT_DAYS_BEFORE_ENTRY)
    bars = [b for b in _load_position_bars(st.ticker)
            if chart_start <= b.day <= end]
    today = datetime.now(timezone.utc).date()

    lo_p = min([float(stop), float(entry)] + [float(b.low) for b in bars])
    hi_p = max([float(stop), float(entry)] + [float(b.high) for b in bars])
    pad = (hi_p - lo_p) * 0.12 or hi_p * 0.02
    lo_p, hi_p = lo_p - pad, hi_p + pad
    span_d = max((end - chart_start).days, 1)

    W, H, L, R, T, B = 660, 210, 88, 16, 18, 40

    def x(d):
        return L + (d - chart_start).days / span_d * (W - L - R)

    def y(v):
        return T + (hi_p - float(v)) / (hi_p - lo_p) * (H - T - B)

    out = [f'<svg id="{p}-t{index}-pos" class="pos-chart" '
           f'viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="price, stop and every review for {esc(st.ticker)}">']
    out.append(f'<rect x="{L}" y="{y(entry):.1f}" width="{W - L - R}" '
               f'height="{abs(y(stop) - y(entry)):.1f}" class="pos-risk"/>')
    for value, cls, label in ((entry, "pos-entry", f"bought ${entry:.2f}"),
                              (stop, "pos-stop", f"stop ${stop:.2f}")):
        out.append(f'<line x1="{L}" y1="{y(value):.1f}" x2="{W - R}" '
                   f'y2="{y(value):.1f}" class="{cls}"/>')
        out.append(f'<text x="{L - 6}" y="{y(value) + 3:.1f}" '
                   f'text-anchor="end" class="pos-label">{esc(label)}</text>')
    if bars:
        pts = " ".join(f"{x(b.day):.1f},{y(b.close):.1f}" for b in bars)
        out.append(f'<polyline points="{pts}" class="pos-price"/>')
        # THE TREND UNDER THE PRICE. A 20-day average is the plainest
        # technical read there is: above it the stock has been rising
        # into this, below it falling. Drawn only where it EXISTS - the
        # first nineteen days have no twenty-day average, and drawing
        # one for them would be inventing the very thing being read.
        closes = [float(b.close) for b in bars]
        sma = _sma(closes, 20)
        if len(sma) >= 2:
            line = " ".join(f"{x(bars[i].day):.1f},{y(v):.1f}"
                            for i, v in sma)
            out.append(f'<polyline points="{line}" class="pos-sma"/>')
        last = bars[-1]
        out.append(f'<circle cx="{x(last.day):.1f}" cy="{y(last.close):.1f}" '
                   f'r="3.5" class="pos-now"/>')
        out.append(f'<text x="{x(last.day):.1f}" y="{y(last.close) - 8:.1f}" '
                   f'text-anchor="middle" class="pos-label">'
                   f"now ${float(last.close):.2f}</text>")
    # ONE MARKER PER DAY, and only for reviews that DECIDED something.
    #
    # OWNER-REPORTED with a screenshot: several rules landed on the same
    # pixel and their labels overprinted into "skipp/jjjgted". Two
    # causes, both fixed here - same-day reviews are collapsed, and
    # skipped ones are not drawn at all, because a skipped review cost
    # nothing and decided nothing. It is still counted in the caption
    # and still listed in full further down the page.
    by_day = {}
    for when, action, _trig, _why, _changed, skipped in (st.reviews or []):
        day = _as_date(when)
        if not day or not (chart_start <= day <= end) or skipped:
            continue
        # An exit outranks a hold on a day that saw both: the decision
        # that changed something is the one worth a mark.
        if by_day.get(day, "hold") == "hold":
            by_day[day] = str(action)
    placed = []
    for day in sorted(by_day):
        px = x(day)
        word = {"hold": "held", "exit_now": "EXIT",
                "no_opinion": "no view"}.get(by_day[day], by_day[day])
        cls = "pos-review" if by_day[day] == "hold" else "pos-review-exit"
        out.append(f'<line x1="{px:.1f}" y1="{T}" x2="{px:.1f}" '
                   f'y2="{H - B}" class="{cls}">'
                   f"<title>{esc(str(day))}: {esc(word)}</title></line>")
        # Label only where one FITS. Below ~34px apart the words
        # overprint, and an unreadable label is worse than none - the
        # rule and its tooltip still carry the fact.
        if all(abs(px - q) > 34 for q in placed):
            out.append(f'<text x="{px:.1f}" y="{T + 9:.0f}" '
                       f'text-anchor="middle" class="pos-label">{esc(word)}'
                       "</text>")
            placed.append(px)
    out.append(f'<line x1="{x(start):.1f}" y1="{T}" x2="{x(start):.1f}" '
               f'y2="{H - B}" class="pos-bought"/>')
    out.append(f'<text x="{x(start):.1f}" y="{H - 20:.0f}" '
               'text-anchor="middle" class="pos-label">bought</text>')
    if chart_start <= today <= end:
        out.append(f'<line x1="{x(today):.1f}" y1="{T - 4}" '
                   f'x2="{x(today):.1f}" y2="{H - B + 4}" class="pos-today"/>')
    for day, label in ((chart_start, str(chart_start)),
                       (end, f"closes {end}")):
        out.append(f'<text x="{x(day):.1f}" y="{H - 8}" '
                   f'text-anchor="{"start" if day == chart_start else "end"}" '
                   f'class="pos-label">{esc(label)}</text>')
    out.append("</svg>")

    left = (end - today).days
    words = f"Bought at <b>${entry:.2f}</b>, stop at <b>${stop:.2f}</b>. "
    if bars:
        move = (float(bars[-1].close) - float(entry)) / float(entry) * 100
        words += (f"Last close <b>${float(bars[-1].close):.2f}</b>, "
                  f"<b>{move:+.1f}%</b> against the fill. ")
    else:
        words += ("No cached daily closes for this ticker yet, so the price "
                  "line is empty rather than guessed. ")
    n_reviews = len([r for r in (st.reviews or []) if not r[5]])
    words += (f"Claude has re-read the thesis <b>{n_reviews}</b> time(s); "
              + (f"it closes in <b>{left}</b> day(s) whatever happens."
                 if left >= 0 else "its exit date has passed."))
    return "".join(out) + figcap(words)


def _sma(values, window):
    """Simple moving average, aligned to the END of each window. Returns
    (index, value) pairs so a caller can plot only where it exists -
    the first `window-1` days genuinely have no average and drawing one
    for them would invent data."""
    out = []
    if len(values) < window:
        return out
    running = sum(values[:window])
    out.append((window - 1, running / window))
    for i in range(window, len(values)):
        running += values[i] - values[i - window]
        out.append((i, running / window))
    return out


def _technicals(st, p: str, index: int) -> str:
    """A brief read of the tape, from the same numbers Claude was given.

    OWNER-ASKED: "maybe another graph or two, it feels very wordy to
    give it a brief technical analysis".

    DELIBERATELY NOT A SECOND OPINION. Every figure here comes from
    data/price_action.py - the exact module that fills the research
    prompt - so this page shows what the model SAW rather than a fresh
    calculation that might disagree with it. A dashboard quietly
    computing its own version of the evidence is how two numbers with
    one name start contradicting each other.

    Descriptive, never predictive: where the price sits in its own
    year, how far it has moved, whether it is being traded more than
    usual. No signal, no rating, no target.
    """
    try:
        from catalyst.data.price_action import price_action
        from catalyst.dashboard.db import bars_path

        pa = price_action(bars_path(), st.ticker,
                          since=_as_date(st.catalyst_date))
    except Exception:            # noqa: BLE001 - absence is normal
        return ""
    if not pa.measured:
        return ""

    def move(v):
        return DASH if v is None else f"{float(v):+.1f}%"

    vol = pa.recent_volume_ratio
    vol_word = ("no volume history" if vol is None
                else "traded far more than usual" if float(vol) >= 2
                else "busier than usual" if float(vol) >= 1.2
                else "quieter than usual" if float(vol) < 0.8
                else "about its usual volume")
    rows = [
        ("5 days", move(pa.move_5d_pct), "what the last week did"),
        ("20 days", move(pa.move_20d_pct), "the month behind it"),
        ("Since the catalyst", move(pa.move_since_catalyst_pct),
         f"over {pa.sessions_since_catalyst or 0} session(s)"),
        ("Volume", (DASH if vol is None else f"{float(vol):.1f}x"),
         esc(vol_word)),
    ]
    out = [tiles(f"{p}-t{index}-ta", rows), _range_bar(st, pa, p, index)]
    return "".join(x for x in out if x)


def _range_bar(st, pa, p: str, index: int) -> str:
    """Where the price sits between its own 52-week low and high.

    One of the few things about a stock that is a fact rather than a
    judgement, and it takes a sentence to say and a glance to see.
    """
    pos = pa.range_position_pct
    if pos is None:
        return ""
    pct = max(0.0, min(100.0, float(pos)))
    entry = _num(st.entry_price)
    # MARGINS WIDE ENOUGH FOR THE WORDS THAT GO IN THEM. "52w low" is
    # ~41px at this size and was anchored end-at-36, so it started at
    # x=-5 and the browser clipped the "5" off every one of them.
    # Measured, not guessed: the SVG box check reported it on every
    # trade on the page.
    W, H, PAD = 640, 40, 4
    L, R = 58, W - 44                     # where the track begins and ends
    parts = [f'<svg id="{p}-t{index}-range" class="range-chart" '
             f'viewBox="0 0 {W} {H}" role="img" aria-label="where '
             f'{esc(st.ticker)} sits in its own 52-week range">',
             f'<rect x="{L}" y="16" width="{R - L}" height="8" rx="4" '
             'class="range-track"/>']
    x = L + pct / 100 * (R - L)
    parts.append(f'<circle cx="{x:.1f}" cy="20" r="5" class="range-now"/>')
    parts.append(f'<text x="{PAD}" y="24" text-anchor="start" '
                 'class="pos-label">52w low</text>')
    parts.append(f'<text x="{W - PAD}" y="24" text-anchor="end" '
                 'class="pos-label">high</text>')
    # THE FLOATING LABEL RIDES THE MARKER, so at 0% and 100% it hangs
    # off both ends. Clamped to a middle it can be centred in: half of
    # "100% up the range" at this size is about 48px, so 56 clears it
    # with room, and the marker itself still sits at the true position.
    label_x = min(max(x, 56.0), float(W - 56))
    parts.append(f'<text x="{label_x:.1f}" y="12" text-anchor="middle" '
                 f'class="pos-label">{pct:.0f}% up the range</text>')
    parts.append("</svg>")
    words = (f"<b>{pct:.0f}%</b> of the way from this stock's own "
             "52-week low to its high"
             + (f", bought at ${entry:.2f}." if entry else ".")
             + " Near the low is not cheap and near the high is not "
             "expensive - it is context for the thesis, not a verdict.")
    return "".join(parts) + figcap(words)


def _sparkline(values, entry=None, sid=""):
    """Sixty closes in the width of a table cell.

    No axis, no labels, no ticks - a sparkline earns its place by being
    read in the same glance as the number beside it. The entry price is
    the only reference drawn, because "above or below what I paid" is
    the one question the shape has to answer.
    """
    vals = [float(v) for v in (values or ()) if v is not None]
    if len(vals) < 3:
        return ""
    lo, hi = min(vals), max(vals)
    if entry is not None:
        lo, hi = min(lo, float(entry)), max(hi, float(entry))
    rng = (hi - lo) or (hi or 1.0) * 0.02
    W, H = 96, 22

    def y(v):
        return 2 + (hi - v) / rng * (H - 4)

    step = (W - 2) / max(len(vals) - 1, 1)
    pts = " ".join(f"{1 + i * step:.1f},{y(v):.1f}" for i, v in enumerate(vals))
    up = vals[-1] >= (float(entry) if entry is not None else vals[0])
    out = [f'<svg class="spark" viewBox="0 0 {W} {H}" aria-hidden="true">']
    if entry is not None:
        out.append(f'<line x1="0" y1="{y(float(entry)):.1f}" x2="{W}" '
                   f'y2="{y(float(entry)):.1f}" class="spark-entry"/>')
    out.append(f'<polyline points="{pts}" class="spark-line '
               f'{"spark-up" if up else "spark-down"}"/>')
    out.append(f'<circle cx="{1 + (len(vals) - 1) * step:.1f}" '
               f'cy="{y(vals[-1]):.1f}" r="1.8" class="spark-dot"/>')
    out.append("</svg>")
    return "".join(out)


def _signed(value, places=2, money=False, unit=""):
    """A signed figure that carries its own colour class.

    The UNIT goes inside the span, not after it. Outside, the figure
    renders as "+0.28</span>R" - two things where the reader sees one,
    and a copy that loses the unit.
    """
    if value is None:
        return DASH
    cls = "pos" if value >= 0 else "neg"
    text = (dollars(abs(Decimal(str(value))) * 100) if money
            else f"{float(value):+.{places}f}")
    if money and value < 0:
        text = "-" + text
    return f'<span class="{cls}">{text}{unit}</span>'


def detailed_overview(db: Db, p: str = "pro") -> str:
    """The book as a desk would want it: every position marked, every
    metric derived, nothing hidden behind prose.

    OWNER-ASKED: "a detailed toggle ... current price of each trade,
    price tracking, live graphs ... this needs no data missed,
    understandable to a proper pro trader with loads of metrics".

    WHAT "AS MUCH AS WE CAN" HONESTLY MEANS, and it is stated on the
    page rather than implied away. This dashboard reads a database and a
    bar cache. It holds no broker session and takes no quote. The
    freshest price it can show is the last DAILY CLOSE the bot cached,
    and every mark carries the day it came from.

    A mark-to-market that looks like a tick and is a day old is how
    somebody believes they are flat when they are not - so the age is a
    column, not a footnote, and a stale one is called out.
    """
    # LIVE QUOTES WHERE THEY CAN BE HAD. dashboard/live.py validates
    # each one exactly as the trading path does - fresh, positive, not
    # crossed - so anything shown here is a price the risk engine would
    # also have accepted. A failure is never fatal: the mark falls back
    # to the cached close and the page says which it is.
    tickers = [r["ticker"] for r in db.q(
        "SELECT ticker FROM positions WHERE status = 'open'").rows]
    try:
        from catalyst.dashboard.live import quotes_for

        quotes = quotes_for(tickers)
    except Exception:            # noqa: BLE001 - a nicety, not the page
        quotes = {}
    b = queries.live_book(db, quotes=quotes)
    out: list[str] = []

    if not b.positions:
        out.append(zero_block(
            f"{p}-none", b.positions_q,
            meaning=("nothing is open, so there is nothing to mark. This "
                     "view fills the moment a position exists.")))
        # THE DESK IS NOT EMPTY JUST BECAUSE THE BOOK IS. Cost and API
        # activity are live whether or not anything is held, and they
        # are half of what was asked for here - an empty book must not
        # take them off the page.
        out.append(_cost_desk(db, p))
        out.append(_api_desk(db, p))
        return section(f"{p}-section", "The desk", "".join(out))

    worst = max((x.stale_days for x in b.positions
                 if x.stale_days is not None), default=None)
    # `worst or 99` read a ZERO-day-old mark - the freshest possible -
    # as 99 days stale, because 0 is falsy. It flagged today's close as
    # critical. Explicit None check, because the difference between "no
    # mark" and "marked today" is the whole point of the column.
    fresh_state = ("crit" if worst is None
                   else "good" if worst <= 1
                   else "warn" if worst <= 4 else "crit")

    # WHICH KIND OF PRICE IS THIS. The single most important thing on
    # the page: a live quote and a day-old close look identical as
    # numbers and mean completely different things.
    if b.positions and b.n_live == len(b.positions):
        mark_value, mark_state = "LIVE", "good"
        mark_sub = (f"all {b.n_live} quoted just now, validated the same "
                    "way the risk engine validates one")
    elif b.n_live:
        mark_value, mark_state = f"{b.n_live}/{len(b.positions)}", "warn"
        mark_sub = ("live; the rest fall back to their last cached close "
                    "- the MARKED column says which is which")
    else:
        mark_value, mark_state = esc(b.freshest or DASH), fresh_state
        # HOW OLD, in words. The pill's colour alone cannot say "nine
        # days", and nine days is the difference between a mark worth
        # reading and one that is fiction.
        age = ("" if worst is None
               else " (today)" if worst == 0
               else f" ({worst} day(s) old)")
        mark_sub = (esc(b.quote_note) + age if b.quote_note
                    else "last cached daily close, not a live quote" + age)
    pnl_sub = ("marked to live quotes" if b.n_live == len(b.positions)
               and b.positions else
               "marked to the last cached close, not a live quote")

    out.append(tiles(f"{p}-tiles", [
        ("Open P&L", _signed(b.unrealised_usd, money=True), pnl_sub),
        ("Deployed", _pct(b.deployed_pct, 0),
         (dollars(b.deployed_usd * 100) if b.deployed_usd is not None
          else DASH) + " at market"),
        ("At risk", (dollars(b.open_risk_usd * 100)
                     if b.open_risk_usd is not None else DASH),
         f"{_pct(b.open_risk_pct, 1)} of the account if every stop fills"),
        ("Marks", mark_value,
         f"{pill(mark_state, 'live quotes' if b.n_live else 'cached closes')} "
         + mark_sub),
    ]))

    rows = []
    for x in b.positions:
        rows.append([
            f'<a href="/trades?id={esc(x.position_id)}">'
            f"<b>{esc(x.ticker)}</b></a>",
            _sparkline(x.spark, x.entry),
            f"${esc(f'{x.entry:.2f}')}" if x.entry else DASH,
            f"${esc(f'{x.last:.2f}')}" if x.last is not None else DASH,
            _signed(x.unrealised_pct, 1, unit="%"),
            _signed(x.unrealised_usd, money=True),
            _signed(x.r_now, unit="R"),
            f"${esc(f'{x.stop:.2f}')}" if x.stop else DASH,
            _pct(x.to_stop_pct, 1),
            (dollars(x.risk_usd * 100) if x.risk_usd is not None else DASH),
            f"{x.days_held}d" if x.days_held is not None else DASH,
            f"{x.days_left}d" if x.days_left is not None else DASH,
            f"{float(x.conviction):.2f}" if x.conviction is not None else DASH,
            (f'<span class="live-dot" aria-hidden="true">&#9679;</span>'
             f'<span class="mono">{esc(x.as_of)}</span>'
             if x.source == "live"
             else esc(x.as_of or "no cached bars")),
        ])
    out.append(table(
        f"{p}-book",
        ["ticker", "60 sessions", "entry", "last", "move", "open P&L",
         "R now", "stop", "to stop", "risk $", "held", "left", "conv",
         "marked"],
        rows, numeric_cols={2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}))
    if b.quote_note and not b.n_live:
        out.append(caveat(
            "No live quote could be taken, so every mark below is the "
            f"last cached daily close. Reason: {b.quote_note}"))
    out.append(figcap(
        "<b>R now</b> is the open result as a multiple of the money at "
        "risk on that position - the unit a book is read in, because it "
        "makes a small win on a small risk and a large win on a large "
        "risk the same result. <b>To stop</b> is how far the price must "
        "fall before the stop fills. <b>Marked</b> is the day the price "
        "came from: a green dot and a time is a live quote taken just "
        "now, a date is that day's cached close. A mark that looks "
        "like a tick and is a day old is worse than no mark at all, so "
        "the two are never dressed the same."))
    out.append(_market_strip(b, p))
    out.append(_position_bars(b, p))
    out.append(_cost_desk(db, p))
    out.append(_api_desk(db, p))
    return section(f"{p}-section", "The desk", "".join(out))


def overview_switch(detailed: bool) -> str:
    """Summary or the full book. A link, not a script: it survives a
    refresh, it can be bookmarked, and it works with no JavaScript -
    which this page has deliberately never needed."""
    opts = [("", "Summary", "the handful of figures worth a glance"),
            ("?view=detailed", "Detailed",
             "every position marked, every metric derived")]
    cells = []
    for href, label, why in opts:
        on = (label == "Detailed") == detailed
        cells.append(
            f'<a class="switch-opt{" on" if on else ""}" href="/{href}" '
            f'title="{esc(why)}">{esc(label)}</a>')
    return f'<div class="switch" id="ov-switch">{"".join(cells)}</div>'


#: How often the desk view reloads itself. Short enough that a quote
#: moves, long enough that the quote cache absorbs most of it and the
#: broker is not re-asked on every tick.
DESK_REFRESH_SECONDS = 15


def _f(value, scale=1, default=None):
    """A float for drawing, or `default`. Every chart on this page has
    to survive a column that arrived as an empty string - a bad datum
    hides one bar, it never takes the desk down."""
    try:
        d = Decimal(str(value)) / Decimal(str(scale))
    except (ArithmeticError, TypeError, ValueError):
        return default
    return float(d) if d.is_finite() else default


#: Below this many pixels per column there is no room for a name under
#: a bar, and overlapping labels are worse than none. Classified by the
#: RULE - the width actually available - rather than by a list of which
#: charts are allowed labels (house rule 7), so a chart that later gains
#: or loses bars gets the right answer without anyone remembering to
#: come back here.
MIN_LABEL_STEP_PX = 30


def _bar_row(pairs, sid, unit="$", height=54, width=300):
    """A small column chart from (label, value) pairs. Values may be
    negative; the zero line is drawn where zero actually is.

    NAMES UNDER THE BARS WHEREVER THEY FIT. Rendered and looked at:
    "Open P&L by position" came out as three unlabelled rectangles, and
    a bar chart nobody can attach a ticker to is decoration rather than
    data. The <title> tooltip is not an answer - there is no hover on a
    phone, and needing one to read a three-bar chart is a defect.
    """
    vals = [float(v) for _l, v in pairs if v is not None]
    if not vals:
        return ""
    step = width / max(len(pairs), 1)
    label_band = 11 if step >= MIN_LABEL_STEP_PX else 0
    plot = height - label_band
    hi, lo = max(vals + [0.0]), min(vals + [0.0])
    span = (hi - lo) or 1.0
    zero_y = 2 + (hi - 0.0) / span * (plot - 4)
    out = [f'<svg id="{sid}" class="minibar" viewBox="0 0 {width} {height}" '
           f'role="img" aria-hidden="true">',
           f'<line x1="0" y1="{zero_y:.1f}" x2="{width}" y2="{zero_y:.1f}" '
           'class="minibar-zero"/>']
    for i, (_label, v) in enumerate(pairs):
        if v is None:
            continue
        y = 2 + (hi - float(v)) / span * (plot - 4)
        top, bot = min(y, zero_y), max(y, zero_y)
        out.append(
            f'<rect x="{i * step + step * 0.18:.1f}" y="{top:.1f}" '
            f'width="{step * 0.64:.1f}" height="{max(bot - top, 1):.1f}" '
            f'class="{"minibar-pos" if float(v) >= 0 else "minibar-neg"}">'
            f"<title>{esc(str(_label))}: {unit}{float(v):,.2f}</title></rect>")
        if label_band:
            out.append(
                f'<text x="{i * step + step * 0.5:.1f}" y="{height - 2}" '
                f'text-anchor="middle" class="minibar-label">'
                f"{esc(str(_label))}</text>")
    out.append("</svg>")
    return "".join(out)


def _gauge_row(rows, sid):
    """used / limit bars, one per line. The bar is the fraction; the
    words are the two numbers, because a fraction alone cannot say
    whether it is close to something that matters."""
    out = [f'<div class="dg" id="{sid}">']
    drawn = 0
    for label, used, limit, note in rows:
        u, lim = _f(used), _f(limit)
        if not lim or lim <= 0 or u is None:
            continue
        pct = max(0.0, u / lim * 100)
        # OVER THE LINE IS RED AND STAYS ON THE PAGE. A bar that simply
        # stops at 100% cannot say whether it is at the limit or twice
        # past it, so the number beside it always carries the truth and
        # the bar is only ever the picture.
        state = ("crit" if pct >= 100 else "warn" if pct >= 80 else "ok")
        out.append(
            f'<div class="dg-line"><span class="dg-name">{esc(label)}</span>'
            f'<span class="dg-track"><span class="dg-bar dg-{state}" '
            f'style="width:{min(pct, 100):.1f}%"></span></span>'
            f'<span class="dg-num mono">{esc(note)}</span></div>')
        drawn += 1
    out.append("</div>")
    return "".join(out) if drawn else ""


def _market_strip(b, p: str) -> str:
    """Bid, ask, spread and mid for everything held - the top-of-book
    line a desk keeps in the corner of its eye.

    EVERY POSITION APPEARS, quoted or not. Showing only the live ones
    would let a symbol drop off the strip at the exact moment its quote
    stopped arriving, which is the moment somebody most needs to see it.
    A row with no quote carries the reason instead.
    """
    if not b.positions:
        return ""
    rows = []
    for x in b.positions:
        live = x.source == "live"
        rows.append([
            f"<b>{esc(x.ticker)}</b>",
            f"${esc(f'{x.bid:.2f}')}" if x.bid else DASH,
            f"${esc(f'{x.ask:.2f}')}" if x.ask else DASH,
            f"${esc(f'{x.last:.2f}')}" if x.last is not None else DASH,
            (f"{float(x.spread_bp):.1f}" if x.spread_bp is not None
             else DASH),
            ((x.entry and x.last is not None
              and _signed((x.last - x.entry) / x.entry * 100, 2, unit="%"))
             or DASH),
            (f'<span class="live-dot" aria-hidden="true">&#9679;</span>'
             f'<span class="mono">{esc(x.as_of)}</span>' if live else
             f'<span class="muted-fig">{esc(x.quote_error)}</span>'
             if x.quote_error else
             f'<span class="muted-fig">cached close '
             f'{esc(x.as_of or DASH)}</span>'),
        ])
    return ("<h3>Top of book</h3>" + table(
        f"{p}-tob",
        ["ticker", "bid", "ask", "mid", "spread bp", "vs entry", "quoted"],
        rows, numeric_cols={1, 2, 3, 4, 5}) + figcap(
        "<b>Spread bp</b> is half the bid-ask gap in basis points - what "
        "crossing it costs, one way. A wide one is the difference "
        "between a paper fill and a real one, so it sits beside the "
        "price rather than behind a fold. A row with no bid and ask is "
        "one no live quote could be had for; the reason is in the last "
        "column."))


def _position_bars(b, p: str) -> str:
    """The book as two charts: money made, and money made per unit of
    money risked. They rank differently, and the difference is the
    whole reason R exists."""
    if not b.positions:
        return ""
    pnl = [(x.ticker, _f(x.unrealised_usd)) for x in b.positions]
    rmul = [(x.ticker, _f(x.r_now)) for x in b.positions]
    if not any(v is not None for _t, v in pnl):
        return ""
    out = ['<div class="deskgrid">',
           '<div class="deskcell"><h4>Open P&amp;L by position ($)</h4>',
           _bar_row(pnl, f"{p}-pnl-bars"), "</div>",
           '<div class="deskcell"><h4>R multiple by position</h4>',
           _bar_row(rmul, f"{p}-r-bars", unit=""), "</div></div>"]
    out.append(figcap(
        "Left is dollars, right is the same result divided by what was "
        "risked to get it. A position can be the biggest winner on the "
        "left and a middling one on the right - that is a large bet "
        "working, not a good one. Hover any bar for its number."))
    return "".join(out)


def _today_verdict(db: Db, p: str, now=None) -> str:
    """One line saying why today has cost what it has cost.

    OWNER-ASKED 2026-08-21: "should claude be spending daily? it failed
    to spend anything today? Does that mean its failing to research".

    A $0 day had exactly the same appearance whether the market was
    shut, the screen was quiet, the budget had refused, or the service
    was dead - and the owner correctly could not tell which. The brief
    calls that out twice ("a zero is never left unexplained", "routine
    attrition must not look like damage"), so the reason now sits
    beside the number rather than in a log nobody reads.
    """
    # `now` is injectable ONLY so a test can pin the clock. House rule
    # 6: this classifier measures against datetime.now(), and a test
    # that fixes the clock in one place but not the other goes red at
    # UTC midnight for a reason unrelated to what it tests.
    s = queries.spend_today(db, now=now)
    # ROUTINE IS "idle", NOT A WARNING. A quiet day is the system
    # working, and painting it amber is how a working bot gets read as
    # a broken one - which this project has already paid for twice.
    state = {"spent": "good", "routine": "idle",
             "limit": "warn", "fault": "crit"}.get(s.kind, "idle")
    word = {"spent": "spending", "routine": "quiet",
            "limit": "held back", "fault": "not running"}.get(s.kind, s.kind)
    return (f'<p class="verdict">{pill(state, word)} '
            f"<b>{dollars(s.cents)} spent today</b> &mdash; "
            f"{esc(s.headline)}. {esc(s.detail)}</p>")


def _api_desk(db: Db, p: str) -> str:
    """What the API has actually been asked to do, and what it charged.

    Owner-asked for "api and costing data all live". The costing half is
    _cost_desk; this is the workload behind it - calls, latency, tokens
    and searches. Every figure is measured from the bot's own audit
    trail of model calls, not estimated.
    """
    d = queries.api_desk(db)
    head = "<h3>The API, at work</h3>" + _today_verdict(db, p)
    if not d.calls_q.rows:
        return head + zero_block(
            f"{p}-api-none", d.calls_q,
            meaning=("no model call has been made in the last fortnight. "
                     "The line above says why today spent what it did; "
                     "the funnel names the stage that stopped the rest."))
    hit = d.cache_hit_pct
    out = [head, tiles(f"{p}-api", [
        ("Calls today", str(d.calls_today),
         f"{d.calls_7d} in the last 7 days, {dollars(d.cost_7d_cents)} "
         "of billing"),
        ("Cost per call", (dollars(d.avg_cost_cents)
                           if d.avg_cost_cents is not None else DASH),
         "mean over the last 7 days"),
        ("Latency", (f"{d.latency_median_ms / 1000:.1f}s"
                     if d.latency_median_ms is not None else DASH),
         (f"median; worst {d.latency_worst_ms / 1000:.1f}s"
          if d.latency_worst_ms is not None else "not recorded")),
        ("Web searches", f"{d.web_searches:,}",
         f"{dollars(Decimal(d.web_searches))} at 1c each, billed on top "
         "of tokens"),
    ])]
    out.append(_gauge_row([
        ("Cache hit rate", float(hit or 0), 100.0,
         f"{float(hit):.0f}% of billed input" if hit is not None
         else "no input recorded"),
    ], f"{p}-api-gauges"))
    out.append(table(
        f"{p}-tokens",
        ["", "tokens", "what it is"],
        [["fresh input", f"{d.input_tokens:,}",
          "charged at the full input rate"],
         ["cache reads", f"{d.cache_read_tokens:,}",
          "charged at 0.1x input - the cheap ones"],
         ["cache writes", f"{d.cache_write_tokens:,}",
          "charged at 1.25x input - the expensive ones"],
         ["<b>billed input</b>", f"<b>{d.billed_input_tokens:,}</b>",
          "<b>the three above, which is what the bill is built on</b>"],
         ["output", f"{d.output_tokens:,}",
          "charged at the output rate, roughly 5x input"]],
        numeric_cols={1}))
    if d.unparseable_turns:
        out.append(caveat(
            f"{d.unparseable_turns} recorded turn(s) had a usage object "
            "this page could not read, so their tokens are missing from "
            "the counts above. The ledger prices from the same raw "
            "records, so the money figures are unaffected."))
    if len(d.by_day) > 1:
        out.append('<div class="deskgrid">'
                   '<div class="deskcell"><h4>Calls per day</h4>')
        out.append(_bar_row([(str(day), float(n)) for day, n, _c in d.by_day],
                            f"{p}-api-calls", unit=""))
        out.append('</div><div class="deskcell"><h4>Cost per call ($)</h4>')
        out.append(_bar_row([(str(day), _f(c, scale=100))
                             for day, c in d.cost_per_call],
                            f"{p}-api-cpc"))
        out.append("</div></div>")
    out.append(figcap(
        "<b>Cache tokens are billed and are not inside the input "
        "count</b> - reading input alone understates the bill by about "
        "half, which is exactly the mistake this table exists to stop. "
        "A high cache hit rate is the cheapest thing on this page: it "
        "is the same context charged at a tenth."))
    return "".join(out)


def _cost_desk(db: Db, p: str) -> str:
    """Every number about what this bot costs to run, and the only
    forecast in the building that is a real one.

    THE PROJECTION IS OF SPEND, NEVER OF PRICE. Owner-asked for
    "predictions". Month-end spend from the current burn rate is
    arithmetic on a measured series and is offered here. A price target
    is not, and never will be from this page - the bot does not forecast
    prices, and a dashboard inventing one would be putting a number in
    front of the owner that nothing in the system stands behind.
    """
    from catalyst.cost.forecast import forecast

    c = queries.cost_panel(db)
    # THE CAP IN FORCE, WHICH IS base_cap_cents DESPITE THE NAME.
    #
    # OWNER-REPORTED: "where has the month against cap come from, im
    # unsure what it means, my API montly is 100" - against a panel
    # reading "$19.77 / $8.00" and a full red bar, while the status
    # strip on the same page correctly read "$19.77 of $100.00".
    #
    # max_cap_cents is GOVERNOR_MAX_CAP_CENTS: a hard bound on the
    # PROFIT-SHARE mechanism, capping how far realised profit may walk
    # the budget up on its own. It has nothing to do with the figure the
    # owner sets, and reading it here made four numbers wrong at once -
    # the cap, the gauge, the derived daily ceiling ($5 instead of $10),
    # and the forecast, which saw spend above cap, returned
    # already_exhausted, and left burn rate and projected month blank.
    #
    # This is the same defect cost_panel itself carries a comment
    # about: a dashboard that shows a smaller cap than the one being
    # spent against teaches the owner their setting did nothing.
    cap = Decimal(c.base_cap_cents or 0)
    mtd = Decimal(c.scheduled_mtd_cents or 0) + Decimal(c.manual_mtd_cents or 0)
    f = forecast(mtd, cap, c.as_of)
    per_day = f.daily_rate_cents

    projected = (per_day * f.days_in_month) if per_day is not None else None
    hurdle = ""
    try:
        capital = Decimal(queries.baseline(db).capital_cents or 0)
        if capital > 0 and projected is not None:
            hurdle = f"{projected * 12 / capital * 100:.0f}% a year to break even"
    except Exception:            # noqa: BLE001
        hurdle = ""

    out = ["<h3>What it costs to run</h3>", tiles(f"{p}-cost", [
        ("Month to date", dollars(mtd),
         f"day {c.days_elapsed} of {f.days_in_month}"),
        ("Burn rate", (dollars(per_day) if per_day is not None else DASH),
         "a day, measured over this month"),
        ("Projected month", (dollars(projected) if projected is not None
                             else DASH),
         esc(hurdle) if hurdle else "at the current rate"),
        ("Cap", dollars(cap),
         # THREE STATES, NOT TWO. will_stop_early is true when the cap
         # is ALREADY gone as well as when a date is projected, and in
         # the first case exhausted_on is None - so this printed the
         # words "runs out None" at the owner. Seen in their screenshot.
         ("already spent" if f.already_exhausted
          else f"runs out {f.exhausted_on}" if f.exhausted_on
          else "not expected to run out this month")),
    ])]
    # THE DAILY CEILING IS DERIVED FROM THE MONTHLY CAP, and read from
    # the governor rather than restated here. A second copy of that
    # arithmetic is how the dashboard once printed a $5 ceiling while
    # the bot spent against $10.
    from catalyst.cost.governor import daily_cap_cents

    today = queries.spend_today_cents(db)
    day_cap = daily_cap_cents(cap if cap > 0 else None)
    out.append(_gauge_row([
        ("Month against the cap", mtd, cap,
         f"{dollars(mtd)} / {dollars(cap)}"),
        ("Today against the daily ceiling", today, day_cap,
         f"{dollars(today)} / {dollars(day_cap)}"),
    ], f"{p}-cost-gauges"))
    if c.billed_days:
        # billed_q comes back NEWEST FIRST. Charted in that order the
        # time axis runs backwards, which reads as a burn rate falling
        # when it is rising - so it is reversed before it is drawn.
        bars = [(r["target_date"], _f(r["cost_api_total_cents"], scale=100))
                for r in reversed(list(c.billed_q.rows))][-30:]
        out.append(_bar_row(bars, f"{p}-billed-bars"))
        out.append(figcap(
            f"Daily billed spend, oldest left. {c.billed_days} day(s) "
            "reconciled against Anthropic's own records - this is what "
            "was actually charged, not what we estimated."))
    out.append(figcap(
        "<b>The only forecast here is of SPEND.</b> Month-end is "
        "arithmetic on a measured burn rate. No price target appears on "
        "this page and none ever will: the bot does not forecast prices, "
        "so a number like that would be one nothing in the system stands "
        "behind."))
    return "".join(out)
