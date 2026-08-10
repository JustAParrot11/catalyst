"""HTML rendering helpers, the page shell, and the standing caveats.

Everything here is a pure function of its arguments so tests can assert
on rendered output without a server.

Two rules are enforced structurally rather than remembered:
  - empty_block() is the ONLY way this dashboard renders an empty
    result, and it cannot be called without a QueryResult, so a zero
    always arrives with its query, its row count and its raw upstream
    text attached.
  - duplicate_ids() is run over the finished page by server.py. A
    duplicated element id once meant one panel silently received another
    panel's data and both looked blank; here it becomes a visible banner.
"""

import html
import json
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser

from catalyst.dashboard.build import BUILD_HASH
from catalyst.dashboard.db import QueryResult
from catalyst.dashboard.redact import redact

#: A parameter only moves on >= 30 closed, scored outcomes
#: (ARCHITECTURE.md section 6.1, MIN_SAMPLE_SIZE["conviction_floor"], itself
#: labeled a provisional placeholder). The same floor is used here for
#: "is this performance number allowed to mean anything yet".
MIN_TRADES_FOR_MEANING = 30

BAKEOFF_CAVEAT = (
    "Bake-off caveat, carried on every performance number "
    "(docs/STRATEGY-BAKEOFF.md, verdict): nothing beat SPY out-of-sample net of all "
    "costs, robustly. The single nominal beat (arm C, +6.73pp out-of-sample excess) "
    "inverted to -15.17pp the moment the per-side spread assumption moved from 15bp "
    "to 30bp. And the portfolio took only 229 of 1,522 eligible out-of-sample "
    "signals, path-dependently by slot contention: the unconditional event study "
    "puts the population mean at +0.73%/trade against the portfolio's +1.75% - "
    "the beat rode a lucky right-tail subsample, overstating the population mean by "
    "more than 2x. Live results are one more draw from that same wide distribution."
)

SURVIVORSHIP_CAVEAT = (
    "Survivorship: the graded universes are not delisting-complete. Arm A ran on 100 "
    "cached large caps that all still exist, so its measured +0.55%/trade is "
    "\"an upper bound estimate of a lower bound phenomenon\" "
    "(docs/STRATEGY-BAKEOFF.md section 4). For arm C, 71 of 865 symbols returned "
    "quotes:null and trades:null for all of 2026 - delisted, acquired or renamed "
    "since their events - and they carry 8% of the events, so their costs and "
    "outcomes are absent from the measured distribution."
)

PAPER_PNL_CAVEAT = (
    "Paper P&L is fictional; the API bill is real money. The net line below deducts "
    "actual priced API spend from simulated trading profit - those two halves are "
    "not the same kind of number and the excess figure inherits that."
)

#: Palette provenance: the categorical, ordinal and status values below
#: are the data-viz reference palette, validated with its own script
#: against THIS dashboard's surfaces (#ffffff light, #1a1a19 dark) -
#: not eyeballed. Recorded results, so a future edit knows what it must
#: re-clear:
#:   categorical #2a78d6 + #eb6834, light/white: lightness band, chroma
#:     floor, CVD separation (worst adjacent dE 24.7 protan), normal
#:     vision (33.6), contrast >= 3:1 - ALL PASS.
#:   ordinal funnel ramp #86b6ef,#5598e7,#2a78d6,#1c5cab,#104281:
#:     monotone lightness, adjacent dL >= 0.06, light end 2.11:1 vs
#:     surface, single hue - ALL PASS. (The obvious 6-step version
#:     FAILED adjacent dL at 0.047; the steps are 100 apart for that
#:     reason, not by taste.)
#: Status colors are reserved and never used for a data series; each is
#: shipped with a glyph AND a word, never colour alone.
_CSS = """
:root {
  color-scheme: light;
  /* Off-white rather than #fff: a pure-white field beside black text is
     the main source of glare on a page somebody stares at for a while.
     The categorical palette was validated against #ffffff, the harsher
     of the two, so softening the surface only widens its margins. */
  --page:        #f2f2ef;
  --surface:     #fbfbf9;
  --surface-2:   #f0f0ec;
  --ink:         #0b0b0b;
  --ink-2:       #52514e;
  --muted:       #898781;
  --hairline:    #e1e0d9;
  --baseline:    #c3c2b7;
  --series-1:    #2a78d6;
  --series-2:    #eb6834;
  --good:        #0ca30c;
  --good-ink:    #006300;
  --warning:     #fab219;
  --serious:     #ec835a;
  --critical:    #d03b3b;
  --good-wash:   #eef7ee;
  --warn-wash:   #fdf6e6;
  --crit-wash:   #fdeeee;
  --step-1:      #86b6ef;
  --step-2:      #5598e7;
  --step-3:      #2a78d6;
  --step-4:      #1c5cab;
  --step-5:      #104281;
  --header:      #17171a;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page:      #0d0d0d;
    --surface:   #1a1a19;
    --surface-2: #232322;
    --ink:       #ffffff;
    --ink-2:     #c3c2b7;
    --muted:     #898781;
    --hairline:  #2c2c2a;
    --baseline:  #383835;
    --series-1:  #3987e5;
    --series-2:  #d95926;
    --good-ink:  #0ca30c;
    --good-wash: #12251a;
    --warn-wash: #2a2312;
    --crit-wash: #2b1615;
    --step-1:    #184f95;
    --step-2:    #256abf;
    --step-3:    #3987e5;
    --step-4:    #6da7ec;
    --step-5:    #9ec5f4;
    --header:    #000000;
  }
}
* { box-sizing: border-box; }
body { font: 14.5px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
       margin: 0; color: var(--ink); background: var(--page);
       -webkit-font-smoothing: antialiased; }
/* Measure. Prose set the full width of a 1180px page runs to ~170
   characters a line, which is roughly twice what the eye tracks
   comfortably; the return sweep is where re-reading the same line comes
   from. Tables, charts and tile rows are exempt - they are not prose. */
p, li, .prov, .caveat, .alarm, .ok, .empty, summary { max-width: 82ch; }
header { background: var(--header); color: #fff; padding: 12px 20px;
         position: sticky; top: 0; z-index: 20;
         box-shadow: 0 1px 0 rgba(0,0,0,.25); }
header h1 { font-size: 15px; margin: 0 0 8px 0; font-weight: 600;
            letter-spacing: .01em; }
nav { display: flex; flex-wrap: wrap; gap: 2px; }
nav a { color: rgba(255,255,255,.72); text-decoration: none; font-size: 13px;
        padding: 4px 9px; border-radius: 5px; }
nav a:hover { background: rgba(255,255,255,.10); color: #fff; }
nav a.active { color: #fff; background: rgba(255,255,255,.16); font-weight: 600; }
main { padding: 18px 20px 48px 20px; max-width: 1180px; }
section { background: var(--surface); border: 1px solid var(--hairline);
          border-radius: 10px; padding: 16px 18px; margin-bottom: 18px; }
section > h2 { font-size: 12px; margin: 0 0 12px 0; text-transform: uppercase;
               letter-spacing: .07em; color: var(--ink-2); font-weight: 600; }
h3 { font-size: 13px; margin: 18px 0 6px 0; }
.prov { color: var(--muted); font-size: 12px; margin: 5px 0; }
.caveat { background: var(--warn-wash); border-left: 3px solid var(--warning);
          padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0 6px 6px 0; }
.alarm { background: var(--crit-wash); border-left: 3px solid var(--critical);
         padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0 6px 6px 0; }
.ok { background: var(--good-wash); border-left: 3px solid var(--good);
      padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0 6px 6px 0; }
.empty { background: var(--surface-2); border: 1px dashed var(--baseline);
         padding: 10px 12px; margin: 9px 0; font-size: 12px; border-radius: 8px; }
.big { font-size: 30px; font-weight: 650; letter-spacing: -.02em; }
.pos { color: var(--good-ink); } .neg { color: var(--critical); }

/* KPI tiles - the at-a-glance row. Every tile carries its own
   provenance line, so a number can never be read without its source. */
.tiles { display: grid; gap: 10px; margin: 2px 0 14px 0;
         grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
.tile { background: var(--surface-2); border: 1px solid var(--hairline);
        border-radius: 9px; padding: 11px 13px; }
.tile-label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
              color: var(--muted); margin: 0 0 5px 0; }
.tile-value { font-size: 23px; font-weight: 650; letter-spacing: -.02em;
              margin: 0; line-height: 1.2; }
.tile-sub { font-size: 11.5px; color: var(--ink-2); margin: 5px 0 0 0; }
.pill { display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
        font-weight: 600; padding: 2px 9px; border-radius: 999px;
        border: 1px solid transparent; }
.pill-good { background: var(--good-wash); color: var(--good-ink);
             border-color: var(--good); }
.pill-warn { background: var(--warn-wash); color: var(--ink);
             border-color: var(--warning); }
.pill-crit { background: var(--crit-wash); color: var(--critical);
             border-color: var(--critical); }
.pill-idle { background: var(--surface-2); color: var(--ink-2);
             border-color: var(--baseline); }

table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 13px; }
th, td { border-bottom: 1px solid var(--hairline); padding: 7px 9px;
         text-align: left; vertical-align: top; }
th { background: var(--surface-2); font-weight: 600; font-size: 11px;
     text-transform: uppercase; letter-spacing: .05em; color: var(--ink-2);
     border-bottom: 1px solid var(--baseline); }
tbody tr:hover { background: var(--surface-2); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.scroll-x { overflow-x: auto; max-width: 100%; }
.scroll-x table { min-width: 520px; }
pre { background: #14140f; color: #e8e8e0; padding: 10px; overflow-x: auto;
      font-size: 12px; border-radius: 7px; white-space: pre-wrap;
      word-break: break-word; max-height: 380px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
details { margin: 7px 0; }
summary { cursor: pointer; font-size: 13px; color: var(--series-1); }
footer { color: var(--muted); font-size: 12px; padding: 12px 20px 28px 20px; }

/* Funnel: an ordinal ramp, darkening as candidates survive each stage. */
.funnel-row { display: flex; align-items: center; gap: 12px; margin: 5px 0; }
.funnel-bar { height: 22px; border-radius: 3px; min-width: 3px; }
/* No widths here: the .funnel-row grid owns the columns. Setting both
   made the count and the conversion figure collide. */
.funnel-label { font-size: 13px; }
.funnel-n { text-align: right; font-variant-numeric: tabular-nums;
            font-weight: 650; font-size: 15px; }
.funnel-drop { color: var(--serious); font-size: 12px; }
.blame { background: var(--crit-wash); border-left: 3px solid var(--critical);
         padding: 9px 11px; font-size: 13px; margin: 9px 0;
         border-radius: 0 6px 6px 0; }
form.inline { display: inline-block; margin: 7px 0; }
input, select, button { font: inherit; padding: 5px 8px; border-radius: 6px;
                        border: 1px solid var(--baseline);
                        background: var(--surface); color: var(--ink); }
button { background: var(--series-1); color: #fff; border-color: transparent;
         font-weight: 600; cursor: pointer; }
.chart { display: block; margin: 10px 0; max-width: 100%; }
.chart-wrap { overflow-x: auto; }
.tag { display: inline-block; background: var(--surface-2);
       border: 1px solid var(--hairline); border-radius: 5px; padding: 1px 7px;
       font-size: 12px; margin-right: 4px; }

/* Budget meter: spend against the cap, with a pace marker. Answers
   "am I on track to breach?" - which a month-to-date total alone
   cannot, because it says nothing about how much month is left. */
.meter { position: relative; height: 12px; border-radius: 6px;
         background: var(--surface-2); border: 1px solid var(--hairline);
         margin: 8px 0 6px 0; overflow: visible; max-width: 560px; }
.meter-fill { position: absolute; left: 0; top: 0; bottom: 0;
              border-radius: 6px 0 0 6px; background: var(--series-1); }
.meter-fill.over { background: var(--critical); }
.meter-pace { position: absolute; top: -4px; bottom: -4px; width: 2px;
              background: var(--ink-2); }
.meter-legend { font-size: 11.5px; color: var(--ink-2); margin: 0 0 2px 0; }

/* Funnel: label, count, conversion, bar. A fixed grid so the numbers
   form columns the eye can run down instead of a ragged edge. */
.funnel-row { display: grid; grid-template-columns: 220px 64px 76px 1fr;
              align-items: center; gap: 12px; margin: 0; padding: 7px 0;
              border-top: 1px solid var(--hairline); }
.funnel-row:first-of-type { border-top: none; }
.funnel-conv { font-size: 12px; color: var(--ink-2);
               font-variant-numeric: tabular-nums; }
.funnel-conv.lost { color: var(--serious); font-weight: 600; }
.quiet { color: var(--muted); font-size: 12px; margin: 0 0 6px 232px;
         max-width: 70ch; }
.quiet summary { color: var(--muted); font-size: 12px; }
.quiet code { font-size: 11.5px; }
@media (max-width: 760px) {
  .funnel-row { grid-template-columns: 1fr 54px 66px; }
  .funnel-row .funnel-bar { display: none; }
  .quiet { margin-left: 0; }
}
"""

NAV = [
    ("/", "Overview"),
    ("/performance", "Performance vs S&P"),
    ("/funnel", "Funnel"),
    ("/costs", "Cost"),
    ("/decisions", "Decisions"),
    ("/refusals", "Refusals"),
    ("/logs", "Logs"),
    ("/setup", "Setup"),
]


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def raw(value) -> str:
    """Escaped AND redacted — the only way stored text reaches a page."""
    return esc(redact(value))


def page(title: str, body: str, active: str, db_path: str, notes: str = "") -> str:
    links = "".join(
        f'<a href="{esc(href)}" class="{"active" if href == active else ""}">{esc(label)}</a>'
        for href, label in NAV
    )
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return (
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        f"<title>{esc(title)} - catalyst</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<meta name='build-hash' content='{esc(BUILD_HASH)}'>"
        f"<style>{_CSS}</style></head><body>"
        f"<header><h1>catalyst - {esc(title)}</h1><nav>{links}</nav></header>"
        f"<main>{body}</main>"
        f"<footer>build <code>{esc(BUILD_HASH)}</code> &middot; rendered "
        f"{esc(generated)} &middot; db <code>{esc(db_path)}</code> &middot; "
        "served no-store; if <code>/health</code> reports a different build hash "
        f"you are looking at a cached page. {notes}</footer>"
        "</body></html>"
    )


def section(sid: str, title: str, body: str) -> str:
    return f'<section id="{esc(sid)}"><h2>{esc(title)}</h2>{body}</section>'


def prov(text: str) -> str:
    """Provenance line. Every number on this dashboard gets one."""
    return f'<p class="prov">{esc(text)}</p>'


def caveat(text: str) -> str:
    return f'<div class="caveat">{esc(text)}</div>'


def caveat_fold(cid: str, summary: str, texts: list) -> str:
    """Several standing caveats behind ONE disclosure.

    Owner feedback 2026-08-10: three long amber blocks sat between the
    headline number and the chart, so the page opened with an
    unreadable wall. They are permanent context, not news - they say
    the same thing on every page load forever. The summary line still
    names each one and stays visible, so nothing is hidden; only the
    paragraphs fold. The sample-size alarm is deliberately NOT folded:
    that one changes, and it is the one that stops a number being read
    as a verdict.
    """
    inner = "".join(caveat(t) for t in texts)
    return (f'<details class="caveat" id="{esc(cid)}">'
            f"<summary>{esc(summary)}</summary>{inner}</details>")


def alarm(text_html: str) -> str:
    return f'<div class="alarm">{text_html}</div>'


def ok(text_html: str) -> str:
    return f'<div class="ok">{text_html}</div>'


def pre(text) -> str:
    return f"<pre>{raw(text)}</pre>"


def details(did: str, summary: str, inner_html: str) -> str:
    return (
        f'<details id="{esc(did)}"><summary>{esc(summary)}</summary>'
        f"{inner_html}</details>"
    )


def empty_block(eid: str, result: QueryResult, upstream: str | None = None,
                meaning: str = "") -> str:
    """The only empty-state renderer. Prints the exact query, its
    parameters, the row count, any driver error, and the raw upstream
    response beside it — 'no data yet' and 'the query is broken' must
    not look the same (ui-designer rule 2, CLAUDE.md house rule 3).

    Layout note (owner feedback 2026-08-10: "not very user friendly"):
    on a fresh install every funnel stage is empty, and six full SQL
    dumps stacked down the page buried the one line that actually
    matters. The VERDICT stays visible — was this an absence of data or
    a broken query — and the query text folds into a disclosure beside
    it. Nothing is removed: house rule 3 asks for the raw response
    beside the zero, not for it to be the largest thing on screen. A
    FAILED query does not fold: that one is opened by default.
    """
    parts = [f'<div class="empty" id="{esc(eid)}">']
    parts.append("<b>Empty result — here is exactly why it is empty.</b> ")
    if meaning:
        parts.append(f"{esc(meaning)} ")
    parts.append(f"rows returned: <b>{result.row_count}</b>. ")
    if result.error:
        parts.append(
            f'<span class="funnel-drop">query FAILED: {esc(result.error)} '
            "— this is a broken query, not an absence of data.</span>"
        )
    else:
        parts.append("Query ran without error — this is an absence of data, not a fault.")
    inner = f"<pre>{esc(result.sql)}\nparams: {esc(result.params)}</pre>"
    if upstream is not None:
        inner += "raw upstream response beside the zero:" + pre(upstream)
    label = ("the query that returned nothing, and its raw response"
             if upstream is not None else "the exact query that returned nothing")
    # A broken query is not a detail to go looking for.
    open_attr = " open" if result.error else ""
    parts.append(
        f'<details id="{esc(eid)}-detail"{open_attr}>'
        f"<summary>{label}</summary>{inner}</details>"
    )
    parts.append("</div>")
    return "".join(parts)


def table(tid: str, headers: list, rows: list, numeric_cols: set | None = None) -> str:
    numeric_cols = numeric_cols or set()
    head = "".join(
        f'<th class="{"num" if i in numeric_cols else ""}">{esc(h)}</th>'
        for i, h in enumerate(headers)
    )
    body = []
    for row in rows:
        cells = "".join(
            f'<td class="{"num" if i in numeric_cols else ""}">{c}</td>'
            for i, c in enumerate(row)
        )
        body.append(f"<tr>{cells}</tr>")
    # Wide tables scroll INSIDE their own box. Without this the widest
    # table on the page sets the width of the page, and the whole
    # document scrolls sideways on a phone - measured at 390px, where
    # the five-column cost table was doing exactly that.
    return (
        f'<div class="scroll-x">'
        f'<table id="{esc(tid)}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


#: Status glyph + word. A status colour NEVER carries meaning alone -
#: the glyph and the word do, and the colour only reinforces them (the
#: page is read by people who may not distinguish the hues, and printed
#: or forced-colours output drops colour entirely).
_PILL_GLYPH = {"good": "●", "warn": "▲", "crit": "■",
               "idle": "○"}


def pill(state: str, label: str) -> str:
    """A small status badge. `state` is one of good/warn/crit/idle."""
    state = state if state in _PILL_GLYPH else "idle"
    return (f'<span class="pill pill-{state}">'
            f'<span aria-hidden="true">{_PILL_GLYPH[state]}</span>'
            f"{esc(label)}</span>")


def meter(mid: str, used: float, cap: float, pace: float | None = None,
          legend: str = "") -> str:
    """Spend against a cap, with an optional pace marker.

    A month-to-date total cannot answer "am I on track to breach it?" -
    that needs the elapsed fraction of the month beside it. The marker
    is where spending would be if it were perfectly even; a fill left of
    the marker is under pace, right of it is over.
    """
    pct = (used / cap * 100.0) if cap else 0.0
    over = pct > 100.0
    width = min(max(pct, 0.0), 100.0)
    marker = ""
    if pace is not None:
        marker = (f'<span class="meter-pace" '
                  f'style="left:{min(max(pace, 0.0), 100.0):.1f}%"></span>')
    return (
        (f'<p class="meter-legend">{legend}</p>' if legend else "")
        + f'<div class="meter" id="{esc(mid)}" role="img" '
          f'aria-label="{pct:.0f}% of the cap used">'
          f'<span class="meter-fill{" over" if over else ""}" '
          f'style="width:{width:.1f}%"></span>{marker}</div>'
    )


def tiles(tid: str, items: list) -> str:
    """The at-a-glance row.

    `items` are (label, value_html, sub_html) triples. `sub` is NOT
    optional decoration - it is where the number says where it came
    from, which the brief requires of every figure on this dashboard.
    A tile with nothing to say in `sub` does not belong here.
    """
    cells = []
    for i, (label, value, sub) in enumerate(items):
        cells.append(
            f'<div class="tile" id="{esc(tid)}-{i}">'
            f'<p class="tile-label">{esc(label)}</p>'
            f'<p class="tile-value">{value}</p>'
            f'<p class="tile-sub">{sub}</p></div>'
        )
    return f'<div class="tiles" id="{esc(tid)}">{"".join(cells)}</div>'


def dollars(cents) -> str:
    if cents is None:
        return "n/a"
    value = Decimal(str(cents)) / Decimal("100")
    return f"${value:,.2f}"


def signed_pp(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}pp"


def json_pretty(text) -> str:
    if text is None:
        return ""
    if isinstance(text, (dict, list)):
        return json.dumps(text, indent=2, sort_keys=True)
    try:
        return json.dumps(json.loads(text), indent=2, sort_keys=True)
    except Exception:
        return str(text)


class _IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append(value)


def all_ids(page_html: str) -> list[str]:
    parser = _IdCollector()
    parser.feed(page_html)
    return parser.ids


def duplicate_ids(page_html: str) -> list[str]:
    """Duplicated ids meant one panel silently received data meant for
    another and both appeared blank. Checked on every response."""
    seen, dupes = set(), []
    for value in all_ids(page_html):
        if value in seen and value not in dupes:
            dupes.append(value)
        seen.add(value)
    return dupes
