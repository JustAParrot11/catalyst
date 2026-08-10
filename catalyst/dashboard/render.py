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
  --page:        #eceef1;
  --surface:     #fafbfc;
  --surface-2:   #f1f3f6;
  --accent:      #0b7f88;
  --grid-head:   #eef1f4;
  --pos:         #0f7a3d;
  --neg:         #c0342b;
  --ink:         #0b0b0b;
  --ink-2:       #52514e;
  --muted:       #898781;
  --hairline:    #dde1e7;
  --baseline:    #c2c8d1;
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
  --rail:        #14161c;
  --rail-line:   #23262f;
  --rail-ink:    #e7e8ec;
  --rail-muted:  #939aab;
  --rail-hover:  rgba(255,255,255,.06);
  --rail-active: rgba(66,133,214,.20);
  --focus-ring:  #2a78d6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page:      #08090b;
    --surface:   #101216;
    --surface-2: #161a20;
    --ink:       #e8e9ec;
    --ink-2:     #a8adb8;
    --muted:     #7d8492;
    --hairline:  #1e232b;
    --baseline:  #2b323d;
    --accent:    #3fd0d8;
    --grid-head: #0b0d10;
    --series-1:  #3987e5;
    --series-2:  #d95926;
    --good-ink:  #33d17a;
    --pos:       #33d17a;
    --neg:       #f2555a;
    --good-wash: #12251a;
    --warn-wash: #2a2312;
    --crit-wash: #2b1615;
    --step-1:    #184f95;
    --step-2:    #256abf;
    --step-3:    #3987e5;
    --step-4:    #6da7ec;
    --step-5:    #9ec5f4;
    --header:    #000000;
    --rail:      #060709;
    --rail-line: #171b22;
    --rail-ink:  #e7e8ec;
    --rail-muted: #8b91a1;
    --rail-hover: rgba(255,255,255,.07);
    --rail-active: rgba(57,135,229,.24);
    --focus-ring: #6da7ec;
  }
}
* { box-sizing: border-box; }
body { font: 13px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
       margin: 0; color: var(--ink); background: var(--page);
       -webkit-font-smoothing: antialiased; }
/* Terminal register: every FIGURE is monospaced and column-aligned, and
   every micro-label is uppercase and letterspaced. Prose stays in the
   UI sans - a trading desk sets its data in mono, not its sentences. */
.tile-value, .funnel-n, td.num, th.num, .rail-value, .big, .gauge-title,
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo,
                   "DejaVu Sans Mono", monospace; }
/* Figures align in columns everywhere. On a page whose whole job is
   comparing numbers, proportional digits make the eye do arithmetic it
   should not have to. */
.tile-value, .funnel-n, td.num, th.num, .rail-value, .big,
.mono { font-variant-numeric: tabular-nums; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }

/* --- shell: fixed sidebar, scrolling content ------------------------ */
.skip { position: absolute; left: -9999px; }
.skip:focus { left: 8px; top: 8px; z-index: 50; background: var(--surface);
              color: var(--ink); padding: 8px 12px; border-radius: 6px;
              border: 1px solid var(--focus-ring); }
.shell { display: grid; grid-template-columns: 232px minmax(0, 1fr);
         min-height: 100vh; }
.sidebar { background: var(--rail); border-right: 1px solid var(--rail-line);
           padding: 14px 10px; position: sticky; top: 0; height: 100vh;
           overflow-y: auto; display: flex; flex-direction: column; gap: 14px; }
.brand { display: flex; align-items: center; gap: 7px; color: var(--rail-ink);
         text-decoration: none; font-weight: 700; font-size: 12px;
         letter-spacing: .22em; text-transform: uppercase;
         padding: 4px 8px 0 8px; }
.brand-mark { color: var(--accent); font-size: 10px; }
.nav-group { margin-bottom: 2px; }
.nav-group-title { font-size: 9px; text-transform: uppercase;
                   letter-spacing: .18em; color: var(--rail-muted);
                   margin: 0 0 3px 8px; font-weight: 700; }
.sidebar nav a { display: block; text-decoration: none; padding: 5px 8px;
                 border-radius: 0; color: var(--rail-ink); margin-bottom: 0;
                 border-left: 2px solid transparent; }
.sidebar nav a:hover { background: var(--rail-hover); }
.sidebar nav a.active { background: var(--rail-active);
                        border-left-color: var(--accent); }
.nav-label { display: block; font-size: 13px; font-weight: 550; }
.nav-hint { display: block; font-size: 10.5px; color: var(--rail-muted);
            line-height: 1.35; }
.sidebar nav a.active .nav-hint { color: var(--rail-ink); opacity: .75; }
.sidebar-foot { margin-top: auto; font-size: 10.5px; color: var(--rail-muted);
                padding: 0 8px; line-height: 1.5; }
.sidebar-foot code { font-size: 10px; }
.content { min-width: 0; display: flex; flex-direction: column; }
.content > header { background: var(--surface);
                    border-bottom: 1px solid var(--hairline);
                    padding: 14px 22px 0 22px; position: sticky; top: 0;
                    z-index: 10; }
.titlebar h1 { font-size: 15px; margin: 0; letter-spacing: .06em;
               font-weight: 700; text-transform: uppercase; }
.subtitle { margin: 2px 0 0 0; color: var(--muted); font-size: 12.5px; }

/* --- status rail: the facts you must never navigate for -------------- */
.rail { display: flex; flex-wrap: wrap; gap: 0; margin: 10px -22px 0 -22px;
        border-top: 1px solid var(--hairline); background: var(--grid-head); }
.rail-item { display: flex; align-items: baseline; gap: 7px;
             padding: 6px 14px; border-right: 1px solid var(--hairline);
             font-size: 11.5px; }
.rail-dot { font-size: 8px; line-height: 1; }
.rail-good .rail-dot { animation: pulse 2.4s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .35 } }
@media (prefers-reduced-motion: reduce) {
  .rail-good .rail-dot { animation: none; }
}
.rail-good .rail-dot { color: var(--good); }
.rail-warn .rail-dot { color: var(--warning); }
.rail-crit .rail-dot { color: var(--critical); }
.rail-idle .rail-dot { color: var(--muted); }
.rail-label { color: var(--muted); text-transform: uppercase;
              letter-spacing: .13em; font-size: 9.5px; font-weight: 700; }
.rail-value { font-weight: 600; font-size: 12.5px; }
@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  .sidebar { position: static; height: auto; flex-direction: row;
             flex-wrap: wrap; align-items: center; gap: 6px; }
  .sidebar nav { display: flex; flex-wrap: wrap; gap: 4px; width: 100%; }
  .nav-group { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
  .nav-group-title { margin: 0 4px 0 0; }
  .nav-hint, .sidebar-foot { display: none; }
  .content > header { position: static; }
}
/* Measure. Prose set the full width of a 1180px page runs to ~170
   characters a line, which is roughly twice what the eye tracks
   comfortably; the return sweep is where re-reading the same line comes
   from. Tables, charts and tile rows are exempt - they are not prose. */
p, li, .prov, .caveat, .alarm, .ok, .empty, summary { max-width: 82ch; }
main { padding: 20px 22px 48px 22px; max-width: 1240px; }
:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; }
section { background: var(--surface); border: 1px solid var(--hairline);
          border-radius: 2px; padding: 0 0 14px 0; margin-bottom: 14px; }
section > h2 { font-size: 10.5px; margin: 0 0 12px 0; text-transform: uppercase;
               letter-spacing: .14em; color: var(--ink-2); font-weight: 700;
               padding: 8px 14px; background: var(--grid-head);
               border-bottom: 1px solid var(--hairline);
               border-left: 3px solid var(--accent); }
section > *:not(h2) { margin-left: 14px; margin-right: 14px; }
section > .scroll-x, section > .chart-wrap { margin-left: 0; margin-right: 0; }
section > .scroll-x table { border-left: none; border-right: none; }
h3 { font-size: 11px; margin: 18px 0 6px 0; text-transform: uppercase;
     letter-spacing: .11em; color: var(--ink-2); font-weight: 700; }
.prov { color: var(--muted); font-size: 12px; margin: 5px 0; }
.caveat { background: var(--warn-wash); border-left: 3px solid var(--warning);
          padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0; }
.alarm { background: var(--crit-wash); border-left: 3px solid var(--critical);
         padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0; }
.ok { background: var(--good-wash); border-left: 3px solid var(--good);
      padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0; }
.empty { background: var(--surface-2); border: 1px dashed var(--baseline);
         padding: 10px 12px; margin: 9px 0; font-size: 12px; border-radius: 2px; }
.big { font-size: 30px; font-weight: 650; letter-spacing: -.02em; }
.pos { color: var(--pos); } .neg { color: var(--neg); }

/* KPI tiles - the at-a-glance row. Every tile carries its own
   provenance line, so a number can never be read without its source. */
.tiles { display: grid; gap: 1px; margin: 12px 0 14px 0;
         background: var(--hairline); border: 1px solid var(--hairline);
         grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
.tile { background: var(--surface-2); padding: 9px 12px; }
.tile-label { font-size: 9.5px; text-transform: uppercase; letter-spacing: .13em;
              color: var(--muted); margin: 0 0 4px 0; font-weight: 700; }
.tile-value { font-size: 21px; font-weight: 600; letter-spacing: -.02em;
              margin: 0; line-height: 1.15; }
.tile-sub { font-size: 11px; color: var(--ink-2); margin: 5px 0 0 0; }
/* A pill followed by prose on one line reads as a run-on. */
.tile-sub .pill { display: flex; width: fit-content; margin-bottom: 3px; }
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

table { border-collapse: collapse; width: 100%; margin: 0; font-size: 12.5px; }
th, td { border-bottom: 1px solid var(--hairline); padding: 5px 10px;
         text-align: left; vertical-align: top; }
th { background: var(--grid-head); font-weight: 700; font-size: 9.5px;
     text-transform: uppercase; letter-spacing: .12em; color: var(--muted);
     border-bottom: 1px solid var(--accent); position: sticky; top: 0; }
tbody tr:hover { background: var(--surface-2); }
tbody tr:hover td { border-bottom-color: var(--baseline); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.scroll-x { overflow-x: auto; max-width: 100%; }
.scroll-x table { min-width: 520px; }
pre { background: #14140f; color: #e8e8e0; padding: 10px; overflow-x: auto;
      font-size: 12px; border-radius: 2px; white-space: pre-wrap;
      word-break: break-word; max-height: 380px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
details { margin: 7px 0; }
summary { cursor: pointer; font-size: 13px; color: var(--series-1); }
footer { color: var(--muted); font-size: 11.5px;
         padding: 12px 22px 26px 22px; margin-top: auto;
         border-top: 1px solid var(--hairline); }

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
         border-radius: 0; }
form.inline { display: inline-block; margin: 7px 0; }
input, select, button { font: inherit; padding: 5px 8px; border-radius: 6px;
                        border: 1px solid var(--baseline);
                        background: var(--surface); color: var(--ink); }
button { background: var(--series-1); color: #fff; border-color: transparent;
         font-weight: 600; cursor: pointer; }
.chart { display: block; margin: 10px 0; max-width: 100%; }
.chart-wrap { overflow-x: auto; }
.tag { display: inline-block; background: var(--surface-2);
       border: 1px solid var(--hairline); border-radius: 2px; padding: 1px 6px;
       font-size: 12px; margin-right: 4px; }

.gauge { margin: 4px 0 14px 0; max-width: 560px; }
.gauge-title { font-size: 12px; font-weight: 650; margin: 0 0 5px 0;
               font-variant-numeric: tabular-nums; }
.gauge-track { position: relative; height: 10px; border-radius: 5px;
               background: var(--surface-2); border: 1px solid var(--hairline); }
.gauge-fill { position: absolute; left: 0; top: 0; bottom: 0;
              border-radius: 5px 0 0 5px; background: var(--series-1); }
.gauge-mark { position: absolute; top: -4px; bottom: -4px; width: 2px;
              background: var(--critical); }

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

#: Navigation grouped by what the reader came to do, not by which
#: module produced the page. A flat row of nine links makes the reader
#: scan all nine every time; three short labelled groups make the choice
#: at most three-then-three.
NAV_GROUPS = [
    ("Monitor", [
        ("/", "Overview", "Everything at a glance"),
        ("/performance", "Performance", "Account value against the S&P"),
        ("/funnel", "Pipeline", "Raw filings through to orders"),
    ]),
    ("Investigate", [
        ("/decisions", "Decisions", "Why each trade was taken or declined"),
        ("/refusals", "Refusals", "What declined candidates went on to do"),
        ("/logs", "Logs", "Searchable event log"),
    ]),
    ("Operate", [
        ("/costs", "Cost & budget", "Spend against the cap, and the bill"),
        ("/maintenance", "Maintenance", "Is everything communicating"),
        ("/setup", "Settings", "Keys, account mode, spending limit"),
    ]),
]

#: Flat form, kept because tests and older callers index it by href.
NAV = [(href, label) for _, items in NAV_GROUPS for href, label, _ in items]


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def raw(value) -> str:
    """Escaped AND redacted — the only way stored text reaches a page."""
    return esc(redact(value))


def status_rail(items: list) -> str:
    """The always-visible state strip, in the manner of a trading
    terminal: the handful of facts you must not have to navigate for.
    `items` are (label, value_html, state) triples; state is one of
    good/warn/crit/idle and drives a marker, never colour alone."""
    cells = []
    for label, value, state in items:
        glyph = _PILL_GLYPH.get(state, _PILL_GLYPH["idle"])
        cells.append(
            f'<span class="rail-item rail-{esc(state)}">'
            f'<span class="rail-dot" aria-hidden="true">{glyph}</span>'
            f'<span class="rail-label">{esc(label)}</span>'
            f'<span class="rail-value">{value}</span></span>')
    return f'<div class="rail" role="status">{"".join(cells)}</div>'


def page(title: str, body: str, active: str, db_path: str, notes: str = "",
         rail: str = "", subtitle: str = "") -> str:
    groups = []
    for group_name, items in NAV_GROUPS:
        parts = []
        for href, label, hint in items:
            on = href == active
            current = ' aria-current="page"' if on else ""
            parts.append(
                f'<a href="{esc(href)}" class="{"active" if on else ""}"{current}>'
                f'<span class="nav-label">{esc(label)}</span>'
                f'<span class="nav-hint">{esc(hint)}</span></a>')
        links = "".join(parts)
        groups.append(
            f'<div class="nav-group"><p class="nav-group-title">'
            f"{esc(group_name)}</p>{links}</div>")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return (
        "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
        f"<title>{esc(title)} - catalyst</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<meta name='build-hash' content='{esc(BUILD_HASH)}'>"
        f"<style>{_CSS}</style></head><body>"
        '<a class="skip" href="#main">Skip to content</a>'
        '<div class="shell">'
        '<aside class="sidebar">'
        '<a class="brand" href="/"><span class="brand-mark" aria-hidden="true">'
        "&#9679;</span>catalyst</a>"
        f'<nav aria-label="Sections">{"".join(groups)}</nav>'
        f'<p class="sidebar-foot">build <code>{esc(BUILD_HASH)}</code><br>'
        f"{esc(generated)}</p>"
        "</aside>"
        '<div class="content">'
        f'<header><div class="titlebar"><h1>{esc(title)}</h1>'
        + (f'<p class="subtitle">{esc(subtitle)}</p>' if subtitle else "")
        + "</div>"
        + (rail or "")
        + "</header>"
        f'<main id="main">{body}</main>'
        f"<footer>db <code>{esc(db_path)}</code> &middot; "
        "served no-store; if <code>/health</code> reports a different build hash "
        f"you are looking at a cached page. {notes}</footer>"
        "</div></div>"
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
