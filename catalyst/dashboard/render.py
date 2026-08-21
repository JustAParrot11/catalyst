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
import re
from datetime import datetime, timezone
from decimal import Decimal
from html.parser import HTMLParser

import catalyst
from catalyst.dashboard.build import BUILD_HASH, build_manifest

#: Where the RUNNING dashboard was loaded from. Printed in the sidebar
#: beside the build hash, because a repo on disk and the copy the
#: service actually imports can be different things - and when they are,
#: the page is the only place that can say which one you are reading.
_SOURCE_DIR = build_manifest()["directory"]
#: major.minor.patch, the number a person reads. The patch is counted
#: from the repository, so it moves without anyone remembering to.
_VERSION = catalyst.__version__
#: The commit, for when two machines disagree about what they run.
_BUILD = catalyst.__build__
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
  /* Slot 3. The spider diagram is an all-pairs form (any node may
     sit beside any other), which caps the categorical set at three:
     validated all-pairs in BOTH modes, worst CVD dE 9.2 light /
     9.4 dark, worst normal-vision dE 24.0 / 20.9. Aqua sits at
     2.74:1 on the light surface, so every node carries a visible
     text label - identity is never colour alone. */
  --series-3:    #1baf7a;
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
    --series-3:  #199e70;
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
/* --- type scale -----------------------------------------------------
   One ratio, six steps, so nothing on the page is sized by eye. A
   passive trader reads this in glances: the hierarchy has to do the
   work of telling a headline figure from its supporting detail before
   a single word is read. Prose sits at --t-base and never competes
   with a number. */
:root {
  --t-micro: 9.5px;   /* uppercase micro-labels */
  --t-fine:  11px;    /* provenance, captions */
  --t-base:  13px;    /* body, table cells */
  --t-lead:  15px;    /* the one-sentence read at the top of a section */
  --t-fig:   22px;    /* tile figures */
  --t-hero:  34px;    /* one per section, no more */
  --gap:     4px;
}
* { box-sizing: border-box; }
body { font: var(--t-base)/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
       margin: 0; color: var(--ink); background: var(--page);
       -webkit-font-smoothing: antialiased;
       text-rendering: optimizeLegibility; }
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
h3 { font-size: 11px; margin: 14px 0 5px 0; text-transform: uppercase;
     letter-spacing: .11em; color: var(--ink-2); font-weight: 700; }
.prov { color: var(--muted); font-size: var(--t-fine); margin: 4px 0;
        line-height: 1.45; }
/* Explanatory text inside a table cell. Not .prov for the same reason
   as .fig-cap: a reason lifted out of the row it explains is a reason
   attached to nothing. */
/* THE ACCOUNT-VALUE BRIDGE. Segment widths are proportional to the
   STARTING capital, never rescaled to fill the bar - a $3 API bill
   against $2,000 should look like a sliver, because it is one. */
/* AN ACTION THAT DESTROYS SOMETHING. Styled to look deliberate rather
   than convenient: it discards real history, so it should never be the
   easiest thing on the page to click. */
/* ONE POSITION, EVERYTHING THAT HAPPENED TO IT. Owner-asked: "i cant
   accurately see how well my current trades are going". Time across,
   price up, the risk band shaded, and a rule for every call to Claude. */
.pos-chart { width: 100%; max-width: 660px; height: auto; margin: 10px 0 2px; }
.pos-risk { fill: var(--critical); opacity: .08; }
.pos-entry { stroke: var(--ink-2); stroke-width: 1.5; }
.pos-stop { stroke: var(--critical); stroke-width: 1.5; stroke-dasharray: 4 3; }
.pos-price { fill: none; stroke: var(--series-1); stroke-width: 2;
  stroke-linejoin: round; }
.pos-now { fill: var(--series-1); }
.pos-review { stroke: var(--accent); stroke-width: 1; stroke-dasharray: 2 3; }
.pos-review-exit { stroke: var(--series-2); stroke-width: 1.5; }
.pos-today { stroke: var(--series-2); stroke-width: 1.5; stroke-dasharray: 3 2; }
.pos-label { fill: var(--muted); font-size: 10px; }
.danger-form { border: 1px solid var(--critical); border-radius: 6px;
  padding: 12px 16px; margin: 12px 0; background: var(--crit-wash);
  max-width: 640px; }
.danger-form p { margin: 0 0 8px; font-size: var(--t-base); }
.danger-form label { font-size: var(--t-fine); color: var(--ink-2); }
.danger-form input { font-family: ui-monospace, SFMono-Regular, Menlo,
  monospace; padding: 4px 8px; margin: 0 8px; border-radius: 4px;
  border: 1px solid var(--hairline); }
.danger-form button { padding: 5px 12px; border-radius: 4px;
  border: 1px solid var(--critical); background: transparent;
  color: var(--critical); cursor: pointer; font-weight: 600; }
.danger-form button:hover { background: var(--critical); color: #fff; }
.bridge { border: 1px solid var(--hairline); border-radius: 6px;
  padding: 10px 16px 14px; margin: 14px 0; background: var(--surface); }
.bridge h3 { margin: 0 0 8px; font-size: 0.95em; color: var(--ink-2); }
.bridge-bar { display: flex; height: 10px; border-radius: 5px;
  overflow: hidden; background: var(--surface-2);
  border: 1px solid var(--hairline); max-width: 640px; margin-bottom: 10px; }
.bridge-seg { display: block; height: 100%; }
.bridge-start { background: var(--baseline); opacity: .55; flex: 0 0 auto; }
.bridge-pnl { background: var(--pos); opacity: .7; flex: 0 0 auto; }
.bridge-api { background: var(--critical); opacity: .7; flex: 0 0 auto; }
.prov-inline { color: var(--muted); font-size: var(--t-fine);
        line-height: 1.45; }
/* A chart's legend, which must stay WITH the chart. Looks like .prov
   and is deliberately not .prov, because section() lifts every .prov
   into a fold at the foot of the panel. */
.fig-cap { color: var(--muted); font-size: var(--t-fine); margin: 2px 0 8px;
        line-height: 1.45; max-width: 640px; }
/* The one-sentence read. Sits above the detail on every page that has
   a simple view, and is the only prose allowed to outweigh a figure. */
.lede-line { font-size: var(--t-lead); }
/* The glance. One line, above every panel, answering "is anything
   wrong, is anything open, did anything happen" before the reader has
   to navigate for it. */
.state-line { font-size: var(--t-lead); line-height: 1.6; margin: 0 0 12px 0;
              padding: 10px 14px; background: var(--surface);
              border: 1px solid var(--hairline);
              border-left: 3px solid var(--accent); }
.state-line b { font-variant-numeric: tabular-nums;
                font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.caveat { background: var(--warn-wash); border-left: 3px solid var(--warning);
          padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0; }
.alarm { background: var(--crit-wash); border-left: 3px solid var(--critical);
         padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0; }
.ok { background: var(--good-wash); border-left: 3px solid var(--good);
      padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0; }
/* Neutral. For "not ready yet, and that is normal" - which is neither a
   warning nor a success, and wearing either colour teaches the owner to
   misread the page. */
/* Simple / Full record. A visible pair, because a hidden preference is
   a setting nobody finds; both options are always on screen. */
.switch { display: flex; gap: 2px; margin: 0 0 14px 0; }
.switch-opt { display: block; padding: 7px 13px; text-decoration: none;
              background: var(--surface-2); border: 1px solid var(--hairline);
              color: var(--ink-2); }
.switch-opt b { display: block; font-size: 12px; letter-spacing: .04em;
                text-transform: uppercase; }
.switch-opt span { display: block; font-size: 11px; color: var(--muted); }
.switch-opt.active { background: var(--accent); border-color: var(--accent); }
.switch-opt.active b, .switch-opt.active span { color: var(--header); }
.lede-line { font-size: 15px; line-height: 1.55; color: var(--ink);
             max-width: 74ch; margin: 4px 0 14px 0; }
/* Legend keys. The spider's three arms are labelled on the diagram too -
   these repeat the identity in text so it is never colour alone. */
/* Diagram interaction. SVG <title> is the tooltip, but it only fires
   on a real hit - and a 1.1px line is close to unhittable with a mouse
   and impossible with a finger, which is why the owner reported that
   nothing happened at all. Wide transparent hit paths sit under each
   stroke; these rules make the response visible as well as functional,
   so it is obvious the diagram IS interactive. */
.chart .edge { transition: opacity .12s ease, stroke-width .12s ease; }
.chart .edge-hit { cursor: help; }
.chart .edge-wrap:hover .edge { opacity: 1 !important; stroke-width: 2.6; }
.chart .node { cursor: help; }
.chart .node:hover rect { filter: brightness(1.35); }
.chart .node:hover circle { filter: brightness(1.35); }
.chart a:hover text { text-decoration: underline; }
.chart a { cursor: pointer; }
@media (prefers-reduced-motion: reduce) {
  .chart .edge { transition: none; }
}
.key { display: inline-block; width: 9px; height: 9px; border-radius: 2px;
       margin: 0 5px 0 12px; vertical-align: baseline; }
.key-1 { background: var(--series-1); }
.key-2 { background: var(--series-2); }
.key-3 { background: var(--series-3); }
/* Provenance, folded. The rule is unchanged - every figure says where
   it came from - but it says so on request rather than in the middle of
   the page. Closed it costs one line; open it is the same text as
   before. */
.workings { margin: 10px 14px 4px 14px; border-top: 1px solid var(--hairline);
            padding-top: 6px; }
.workings > summary { font-size: var(--t-fine); color: var(--muted);
                      letter-spacing: .04em; }
.workings[open] > summary { color: var(--ink-2); margin-bottom: 4px; }
.workings .prov { margin: 6px 0; }
.note { background: var(--surface-2); border-left: 3px solid var(--accent);
        padding: 9px 11px; margin: 9px 0; font-size: 13px; border-radius: 0; }
.empty { background: var(--surface-2); border: 1px dashed var(--baseline);
         padding: 10px 12px; margin: 9px 0; font-size: 12px; border-radius: 2px; }
.big { font-size: var(--t-hero); font-weight: 650; letter-spacing: -.025em;
       line-height: 1.1; font-feature-settings: "tnum" 1, "zero" 1; }
.pos { color: var(--pos); } .neg { color: var(--neg); }
/* A figure that is not a verdict: absent because it is early,
   not absent because something broke. */
.muted-fig { color: var(--muted); }

/* KPI tiles - the at-a-glance row. Every tile carries its own
   provenance line, so a number can never be read without its source. */
/* Density. Tiles were 190px wide with 9px of padding, so four figures
   filled a screen and the eye travelled a long way between them. A
   trading desk packs more into the same glance: narrower minimum,
   tighter padding, so a KPI row reads as one instrument rather than as
   four cards. */
.tiles { display: grid; gap: 1px; margin: 8px 0 10px 0;
         background: var(--hairline); border: 1px solid var(--hairline);
         grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); }
.tile { background: var(--surface-2); padding: 7px 10px; }
.tile-label { font-size: var(--t-micro); text-transform: uppercase; letter-spacing: .13em;
              color: var(--muted); margin: 0 0 4px 0; font-weight: 700; }
.tile-value { font-size: var(--t-fig); font-weight: 600; letter-spacing: -.02em;
              margin: 0; line-height: 1.15; }
.tile-sub { font-size: var(--t-fine); color: var(--ink-2); margin: 5px 0 0 0; }
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
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums;
                 font-feature-settings: "tnum" 1, "zero" 1; }
/* Scanning a column is the core motion on this page, so the row under
   the pointer is marked and the header stays put while it scrolls. */
tbody tr:hover { background: var(--surface-2); }
thead th { position: sticky; top: 0; background: var(--grid-head);
           z-index: 1; }
.scroll-x { max-height: 70vh; }
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

.funnel-drop { color: var(--serious); font-size: 12px; }
/* A reason that has not recurred is history, and must not keep wearing
   the colour that means "something is wrong right now". */
.funnel-drop .drop-live { color: var(--serious); }
.funnel-drop .drop-stale { color: var(--muted); }
.funnel-drop .drop-stale b { color: var(--ink-2); }
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
.chart.map-fit { height: auto; display: block; }
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

/* Funnel, rebuilt 2026-08-11. One numbered step per block: a full-width
   track whose bar length IS the surviving count against the widest step,
   the arithmetic spelled out underneath ("N arrived -> M continued"), and
   the reasons indented under the step they belong to.

   THE COLOUR RULE. Candidates stopping is normal - it is most of what
   this page shows - so "why they stopped" is neutral text. Only genuine
   faults get the warning colour, and they carry a chip saying so. The
   previous version painted normal attrition, governor denials and
   "order status: filled" in the same orange, which is why the owner
   read the whole panel as errors. */
.funnel-step { padding: 16px 0 14px 0; border-top: 1px solid var(--hairline); }
.funnel-step:first-of-type { border-top: none; padding-top: 4px; }
.funnel-head { display: grid; grid-template-columns: 26px 1fr auto;
               align-items: baseline; gap: 10px; }
.funnel-num { width: 22px; height: 22px; border-radius: 50%;
              background: var(--surface-2); border: 1px solid var(--hairline);
              color: var(--ink-2); font-size: 11px; font-weight: 700;
              display: inline-flex; align-items: center;
              justify-content: center; align-self: center; }
.funnel-label { font-size: 14px; font-weight: 650; letter-spacing: .01em; }
.funnel-n { text-align: right; font-variant-numeric: tabular-nums;
            font-weight: 700; font-size: 22px; line-height: 1; }
.funnel-track { height: 8px; border-radius: 4px; background: var(--surface-2);
                border: 1px solid var(--hairline); margin: 8px 0 6px 36px;
                overflow: hidden; }
.funnel-bar { display: block; height: 100%; background: var(--series-1); }
.funnel-flow { display: block; margin-left: 36px; font-size: 12px;
               color: var(--ink-2); font-variant-numeric: tabular-nums; }
.funnel-flow.lost b { color: var(--ink); }
.funnel-plain { margin: 6px 0 0 36px; font-size: 12.5px; color: var(--ink-2);
                max-width: 78ch; }
.funnel-why, .funnel-fault { margin: 10px 0 0 36px; max-width: 78ch;
                             border-left: 2px solid var(--hairline);
                             padding: 2px 0 2px 12px; }
.funnel-fault { border-left-color: var(--serious); }
.funnel-why h3, .funnel-fault h3 { margin: 0 0 4px 0; font-size: 11px;
                                   letter-spacing: .07em; text-transform: uppercase;
                                   color: var(--muted); font-weight: 700; }
.funnel-why ul, .funnel-fault ul { margin: 0; padding: 0; list-style: none; }
/* Grid, not an inline number: a wrapped reason used to run back under
   its own count and the machine code trailing it landed in the margin. */
.funnel-why li, .funnel-fault li { display: grid;
                                   grid-template-columns: 2.4em 1fr;
                                   gap: 8px; font-size: 12.5px;
                                   padding: 3px 0; color: var(--ink-2); }
.funnel-fault li { color: var(--serious); }
/* A reason not seen for days is history and must stop wearing the colour
   that means "wrong right now" (owner-reported: a wall of 400s read as a
   live fault days after the bug behind them was fixed). */
/* CLAUDE'S OWN WORDS, set apart from the page's narration. The owner
   asked to read the model's reasoning directly rather than a summary of
   it, so it has to be visibly a QUOTE - a summary of a thesis is just
   another opinion. */
.said { margin: 10px 0; padding: 10px 14px; border-left: 3px solid
  var(--accent); background: var(--surface-2); border-radius: 0 4px 4px 0;
  color: var(--ink); font-size: 0.97em; line-height: 1.55; }
.said b { color: var(--ink-2); font-weight: 600; }
.trade { border: 1px solid var(--hairline); border-radius: 6px;
  padding: 4px 16px 16px; margin: 18px 0; background: var(--surface); }
.trade h4 { margin: 18px 0 6px; font-size: 0.95em; color: var(--ink-2);
  letter-spacing: .01em; }
/* FOLDED BY DEFAULT. Owner-reported: "its already uncollapsed which
   will get messy as there are many open and closed trades". Each story
   runs several screens, so the summary line has to carry enough to
   decide whether to open it - it is styled as a row of facts, not as a
   link. */
.trade > summary { cursor: pointer; padding: 12px 0; font-size: 1.02em;
  color: var(--ink); list-style-position: outside; }
.trade > summary:hover { color: var(--accent); }
.trade[open] > summary { border-bottom: 1px solid var(--hairline);
  margin-bottom: 4px; }
/* THE RISK BAND, DRAWN. Owner-asked: "Simplify data maybe with
   prediction graphs, it feels word heavy". The shaded span between the
   stop and the fill IS the exposure - and it is the divisor the
   position size came out of, so seeing its width is seeing the sizing.
   Nothing here is forecast: only prices that actually exist are drawn. */
.rail-chart { width: 100%; max-width: 640px; height: auto; margin: 10px 0 2px; }
.rail-axis { stroke: var(--hairline); stroke-width: 1; }
.rail-risk { fill: var(--accent); opacity: .16; }
.rail-stop { stroke: var(--critical); stroke-width: 2; }
.rail-entry { stroke: var(--ink-2); stroke-width: 2; }
.rail-exit { stroke: var(--accent); stroke-width: 2; stroke-dasharray: 3 2; }
.rail-label { fill: var(--muted); font-size: 11px; }
/* HOW LONG EACH POSITION HAS LEFT. Owner-asked: "less text more graphs
   and icons, make the UI more friendly, its text heavy". "Opened the
   17th, closes the 29th" is two dates the reader has to subtract; a bar
   against today is the subtraction already done. */
.tl-wrap { border: 1px solid var(--hairline); border-radius: 6px;
  padding: 10px 16px 14px; margin: 14px 0; background: var(--surface); }
.tl-wrap h3 { margin: 0 0 6px; font-size: 0.95em; color: var(--ink-2); }
.tl-chart { width: 100%; max-width: 640px; height: auto; }
.tl-name { fill: var(--ink-2); font-size: 11px; font-weight: 600; }
.tl-note { fill: var(--muted); font-size: 10px; }
.tl-open { fill: var(--accent); opacity: .55; }
.tl-done { fill: var(--muted); opacity: .35; }
.tl-today { stroke: var(--series-2); stroke-width: 1.5; stroke-dasharray: 3 2; }
.hold { margin: 8px 0 2px; max-width: 640px; }
.hold-track { height: 8px; border-radius: 4px; background: var(--surface-2);
  border: 1px solid var(--hairline); overflow: hidden; }
.hold-fill { display: block; height: 100%; background: var(--accent);
  opacity: .55; }
/* ICONS BESIDE HEADINGS, never instead of them. Every step keeps its
   words and the glyph is aria-hidden, so nothing is carried by the
   picture alone - the same rule the status pills follow. */
.step-ico { margin-right: 7px; font-size: 1.05em; }
/* THE PROSE, FOLDED. Reported "text heavy" twice. None of it is wrong -
   it is the provenance and reasoning the brief demands - so it is put
   one click away rather than deleted. */
.why-fold { margin: 6px 0 10px; }
.why-fold summary { font-size: var(--t-fine); color: var(--muted);
  cursor: pointer; }
.why-fold[open] summary { margin-bottom: 4px; }
.why-fold p { font-size: var(--t-fine); color: var(--ink-2);
  margin: 4px 0; max-width: 62ch; }
.funnel-why li.drop-live { color: var(--ink-2); }
.funnel-why li.drop-stale, .funnel-why li.drop-stale .funnel-why-n {
  color: var(--muted); }
/* THREE KINDS, THREE WEIGHTS. Routine attrition and a bound doing its
   job are recessive; only a genuine fault carries colour. Owner-
   reported: an HTTP 400 traceback sitting in the same style as "the
   market was closed" made a working bot look broken. The tag is text,
   never colour alone, so it survives greyscale and colour blindness. */
.funnel-why li.drop-routine, .funnel-why li.drop-routine .funnel-why-n,
.funnel-why li.drop-limit, .funnel-why li.drop-limit .funnel-why-n {
  color: var(--muted); }
.drop-tag { display: inline-block; font-size: 0.78em; font-weight: 700;
  letter-spacing: .04em; text-transform: uppercase; padding: 1px 6px;
  margin-right: 6px; border-radius: 3px; background: var(--surface-2);
  color: var(--ink-2); border: 1px solid var(--hairline);
  vertical-align: 1px; }
.drop-tag-fault { background: var(--crit-wash); color: var(--critical);
  border-color: var(--critical); }
.funnel-why-n { font-weight: 700; font-variant-numeric: tabular-nums;
                color: var(--ink); text-align: right; }
/* The reason and its provenance, as ONE grid child. min-width: 0 is not
   decoration: a grid item defaults to min-width:auto and refuses to
   wrap narrower than its longest unbroken word, so a long machine code
   would widen the column instead of wrapping inside it. */
.funnel-why-text { min-width: 0; overflow-wrap: anywhere; }
/* The machine code beside the sentence: present for grep and for a
   developer, never competing with the English. */
.funnel-why .prov, .funnel-fault .prov { display: block; margin: 1px 0 0 0;
                                         font-size: 11px; }
/* A raw upstream body can be a 4KB HTML page. Kept verbatim (house rule
   3) but folded, and forced to wrap and scroll rather than stretching
   the panel to the width of the longest unbroken line. */
.raw-fold { margin: 4px 0 0 0; }
/* A zoomed map is WIDER than the panel on purpose: it scrolls sideways
   rather than being squashed back to fit, which would undo the zoom. */
.chart-scroll { overflow-x: auto; }
.bundlerow { display: grid; grid-template-columns: 13em 1fr; gap: 12px;
             align-items: baseline; margin: 6px 0; }
.bundlebtn { display: inline-block; text-align: center; padding: 5px 10px;
             border: 1px solid var(--accent); border-radius: 3px;
             text-decoration: none; color: var(--accent); font-weight: 700;
             font-size: 12.5px; }
.bundlebtn:hover { background: var(--accent); color: #fff; }
.bundlebtn.master { background: var(--accent); color: #fff; }
/* min-width:0 so the description wraps inside its column rather than
   widening it - the same grid trap the funnel hit. */
.bundlewhy { min-width: 0; overflow-wrap: anywhere; color: var(--muted);
             font-size: 12.5px; }
/* The chain: one row per step, expandable in place. Reading the story
   top to bottom must not mean losing your place, so a step opens where
   it sits rather than navigating away. */
.chain { border: 1px solid var(--hairline); border-radius: 4px;
         margin: 14px 0; overflow: hidden; }
.chain-head { margin: 0; padding: 8px 12px; font-size: 14px;
              background: var(--surface-2);
              border-bottom: 1px solid var(--hairline); }
.chain-head .prov { font-weight: 400; margin-left: 8px; }
.chain-step { border-top: 1px solid var(--hairline); }
.chain-step:first-of-type { border-top: none; }
.chain-step > summary { display: grid; grid-template-columns: 2em 6.5em 1fr;
                        gap: 10px; align-items: baseline; cursor: pointer;
                        padding: 8px 12px; list-style: none; }
.chain-step > summary::-webkit-details-marker { display: none; }
.chain-step > summary:hover { background: var(--surface-2); }
.chain-step[open] > summary { background: var(--surface-2); }
.chain-n { font-variant-numeric: tabular-nums; font-weight: 700;
           color: var(--muted); text-align: right; }
.chain-stage { font-size: 11px; letter-spacing: .06em; font-weight: 700;
               text-transform: uppercase; color: var(--accent); }
/* min-width:0 so a long line wraps INSIDE its column instead of
   widening it - the same grid trap the funnel hit. */
.chain-text { min-width: 0; overflow-wrap: anywhere; }
.chain-why { display: block; color: var(--muted); font-size: 12px;
             margin-top: 2px; }
.chain-step.stopped .chain-stage { color: var(--serious); }
.chain-body { padding: 4px 12px 12px 12px; }
.chain-fact { display: grid; grid-template-columns: 12em 1fr; gap: 10px;
              padding: 3px 0; font-size: 12.5px; }
.chain-k { color: var(--muted); }
.chain-v { min-width: 0; overflow-wrap: anywhere; }
.chain-link { display: inline-block; margin-top: 6px; font-size: 12px; }
.chart-scroll svg { max-width: none; }
.viewbar { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px;
           margin: 8px 0 4px 0; }
.viewbar-label { font-size: 11px; letter-spacing: .07em;
                 text-transform: uppercase; color: var(--muted);
                 font-weight: 700; margin-left: 10px; }
.viewbar-label:first-of-type { margin-left: 0; }
.viewopt { font-size: 12px; padding: 2px 8px; border-radius: 3px;
           border: 1px solid var(--hairline); text-decoration: none;
           color: var(--ink-2); }
a.viewopt:hover { border-color: var(--accent); color: var(--ink); }
.viewopt.on { background: var(--accent); color: #fff;
              border-color: var(--accent); font-weight: 700; }
.raw-fold summary { font-size: 11px; color: var(--muted); }
.raw-fold pre { max-height: 16em; overflow: auto; white-space: pre-wrap;
                overflow-wrap: anywhere; font-size: 10.5px;
                background: var(--surface-2); border: 1px solid var(--hairline);
                border-radius: 3px; padding: 6px 8px; margin: 4px 0 0 0; }
.funnel-fault .funnel-why-n { color: var(--serious); }
.fault-chip { display: inline-block; background: var(--serious); color: #fff;
              border-radius: 2px; padding: 0 5px; font-size: 10px;
              letter-spacing: .06em; font-weight: 700; margin-right: 6px;
              vertical-align: 1px; }
/* --- the map as a map ---------------------------------------------
   Drag to move, scroll to zoom, click to follow a thread. The tool
   strip is hidden until the script adds .map-live, so with scripting
   off the page never advertises a drag that does nothing. */
.maptools { display: none; align-items: center; flex-wrap: wrap; gap: 12px;
            margin: 0 0 8px; font-size: 12px; }
.maptools.on { display: flex; }
/* The magnification links are the no-JS zoom. Where the mouse works
   they are a worse version of something already in your hand. */
.map-tools-live .viewbar-camera { display: none; }
.maphint { color: var(--muted); }
.mapfind input { font: inherit; padding: 3px 7px; width: 12em;
                 border: 1px solid var(--line); border-radius: 3px;
                 background: var(--panel); color: inherit; }
.mapzoom { color: var(--muted); font-variant-numeric: tabular-nums; }
.chart-wrap { position: relative; }
.map-live svg.chart { cursor: grab; touch-action: none; }
.map-grabbing svg.chart { cursor: grabbing; }
.map-live svg.chart:focus { outline: 2px solid var(--accent); outline-offset: 2px; }
.map-live .node { cursor: pointer; }
/* Picking a node dims what it does not touch. Dimming, never hiding:
   a map that removes what you did not click is a different picture,
   not the same one with your answer marked. */
.map-picked .node { opacity: .25; }
.map-picked .node.near { opacity: .9; }
.map-picked .node.on { opacity: 1; }
.map-picked .node.on circle { stroke: var(--accent); stroke-width: 3; }
.map-picked .edge-wrap { opacity: .12; }
.map-picked .edge-wrap.on { opacity: 1; }
.map-picked .edge-wrap.on .edge { stroke-width: 2; }
.map-finding .node { opacity: .3; }
.map-finding .node.found { opacity: 1; }
.map-finding .node.found circle { stroke: var(--accent); stroke-width: 3; }
.mapcard { display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px;
           margin: 0 0 8px; padding: 7px 11px; font-size: 12px;
           border: 1px solid var(--accent); border-radius: 4px;
           background: var(--surface-2); }
.mapcard[hidden] { display: none; }
.mapcard .cardsub { color: var(--muted); font-size: 11px; }
/* Entry points into the map. A graph with no obvious place to click
   leaves the reader scanning a texture; these are the busiest nodes,
   named, each opening its own neighbourhood. */
.waysin { margin: 10px 0 14px; }
.waysin h3 { margin: 0 0 6px; }
.waysin p { margin: 0 0 4px; display: flex; flex-wrap: wrap; gap: 6px; }
.waychip { display: inline-flex; align-items: baseline; gap: 6px;
           padding: 4px 9px; border: 1px solid var(--line);
           border-radius: 12px; font-size: 12px; text-decoration: none;
           background: var(--panel); }
.waychip:hover { border-color: var(--accent); color: var(--accent); }
.waychip-n { color: var(--muted); font-size: 11px;
             font-variant-numeric: tabular-nums; }
.crumb { margin: 0 0 6px; font-size: 12px; color: var(--muted); }
/* The way OUT of a fault, beside the description of it. A block that
   says what is wrong and not where to fix it sends the reader hunting -
   the reconciliation block told the owner to open the wrong page. */
.fault-fix { display: inline-block; margin-top: 4px; padding: 3px 8px;
             border: 1px solid var(--serious); border-radius: 2px;
             font-size: 12px; font-weight: 600; text-decoration: none;
             white-space: nowrap; }
.fault-fix:hover { background: var(--serious); color: #fff; }
.quiet { color: var(--muted); font-size: 12px; margin: 0 0 6px 36px;
         max-width: 70ch; }
.quiet summary { color: var(--muted); font-size: 12px; }
.quiet code { font-size: 11.5px; }
@media (max-width: 760px) {
  .funnel-track, .funnel-flow, .funnel-plain, .funnel-why,
  .funnel-fault, .quiet { margin-left: 0; }
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
        ("/trades", "Trades", "Every position, and why it was taken"),
        ("/next", "What happens next",
         "When Claude next re-reads each thesis, and what closes when"),
        ("/funnel", "Pipeline", "Raw filings through to orders"),
    ]),
    ("Investigate", [
        ("/chain", "Every decision", "Found \u2192 linked \u2192 judged \u2192 sized \u2192 traded"),
        ("/brain", "The brain", "Everything it has linked, as one map"),
        ("/newsmap", "News map",
         "What was said, about whom \u2014 click a headline"),
        ("/decisions", "Decisions", "Why each trade was taken or declined"),
        ("/refusals", "Learning",
         "What declined candidates did, and what moved because of it"),
        ("/integrity", "Data integrity",
         "Fill against intended, and where every price came from"),
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
        # THE VERSION FIRST, the fingerprints under it. Owner-reported:
        # "the version numbering is crazy complicated". A twelve-
        # character hash is the right thing to quote when two machines
        # disagree and the wrong thing to lead with - version 0.3.14 is
        # what a person reads to know what they are looking at.
        f'<p class="sidebar-foot">v<code>{esc(_VERSION)}</code><br>'
        f'<span title="the exact commit this code was built from">'
        f'code <code>{esc(_BUILD)}</code></span><br>'
        f'build <code>{esc(BUILD_HASH)}</code><br>'
        # THE DIRECTORY, not just the hash. Owner-reported 2026-08-11: the
        # repo on disk was byte-for-byte current while the service ran an
        # older copy from somewhere else, and the page could not say so.
        # "The repo says X" is a different claim from "the running code
        # is X", and only the second one is what you are looking at.
        f'<code title="the directory this page was loaded from">'
        f'{esc(_SOURCE_DIR)}</code><br>'
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


#: Matches a provenance paragraph, including its id when it carries one.
_PROV_RE = re.compile(r'<p class="prov"[^>]*>.*?</p>', re.S)
#: Explanation that a SUMMARY page folds away. Every one of these is a
#: flat element by construction (see note/caveat/prov below), so a
#: non-greedy match cannot swallow a sibling.
_EXPLAIN_RE = re.compile(
    r'<p class="prov"[^>]*>.*?</p>'
    r'|<div class="note"[^>]*>.*?</div>'
    r'|<div class="caveat"[^>]*>.*?</div>'
    r'|<p class="funnel-plain"[^>]*>.*?</p>', re.S)


def digest(html: str) -> str:
    """Fold a section's EXPLANATION away, keeping its figures.

    The overview used to render each panel in full - every provenance
    line, every standing caveat, every plain-English gloss - which came
    to 76 words of prose for each figure on the page. A trading desk
    runs nearer ten. The words are not wrong and they are not deleted:
    they move into one disclosure per section, and the dedicated page
    for that panel still shows everything inline.

    Alarms, warnings and empty-result blocks are NEVER folded. Those are
    not explanation, they are the page telling you something is wrong,
    and a summary that hides them is worse than no summary.
    """
    parts = _EXPLAIN_RE.findall(html)
    if len(parts) < 2:
        return html
    kept = _EXPLAIN_RE.sub("", html)
    fold = ('<details class="workings"><summary>'
            f"Why these {len(parts)} figures read as they do, and where they "
            "came from</summary>" + "".join(parts) + "</details>")
    # Land the disclosure INSIDE the section, before its closing tag, so
    # it belongs to the panel it explains rather than drifting to the
    # foot of the page.
    idx = kept.rfind("</section>")
    return kept[:idx] + fold + kept[idx:] if idx != -1 else kept + fold


def section(sid: str, title: str, body: str) -> str:
    """A panel, with its provenance FOLDED rather than inline.

    Every number on this dashboard has to say where it came from, and it
    still does - but the owner measured the cost of printing all of it
    at once: 94 words of prose per figure on the Overview, 291 on the
    Cost page. A trading desk runs nearer ten. The page read as an essay
    with numbers in it rather than an instrument.

    So the rule is unchanged and the default view is not: every
    provenance line in this section is collected into one disclosure at
    the foot of it, one click from the figure it explains. Warnings,
    alarms and empty-result blocks stay inline - those are not
    provenance, they are the page telling you something is wrong.

    Done here, in the one place every panel already passes through, so
    no panel has to remember to do it.
    """
    provs = _PROV_RE.findall(body)
    if len(provs) > 1:
        body = _PROV_RE.sub("", body)
        body += (f'<details class="workings" id="{esc(sid)}-workings">'
                 f"<summary>Where these {len(provs)} figures came from"
                 "</summary>" + "".join(provs) + "</details>")
    return f'<section id="{esc(sid)}"><h2>{esc(title)}</h2>{body}</section>'


def prov(text: str) -> str:
    """Provenance line. Every number on this dashboard gets one."""
    return f'<p class="prov">{esc(text)}</p>'


def figcap(html_text: str) -> str:
    """A caption that belongs to the figure directly above it.

    NOT provenance, and the distinction is not pedantry: section() lifts
    every provenance line into a disclosure at the foot of the panel,
    which is right for "where this number came from" and wrong for the
    legend of a chart. Caught by rendering the trades page and finding
    three graphs with their captions stacked together at the bottom,
    hundreds of elements away from the drawings they explained. A chart
    whose legend is somewhere else is not a chart.

    So this looks like a provenance line and stays put.
    """
    return f'<p class="fig-cap">{html_text}</p>'


def caveat(text: str) -> str:
    return f'<div class="caveat">{esc(text)}</div>'


def note(text_html: str) -> str:
    """Neutral, informational. Not a warning and not a success.

    "The comparison is not ready yet" is neither, and dressing it in
    amber or red teaches the owner that a healthy page looks broken.
    """
    return f'<div class="note">{text_html}</div>'


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


def zero_block(zid: str, result: QueryResult, meaning: str = "") -> str:
    """A COUNT that came back ZERO - which is not the same thing as a
    query returning no rows, and must not be described as one.

    Owner report 2026-08-10: the funnel said "Empty result - here is
    exactly why it is empty ... rows returned: 1", which is a flat
    contradiction. Both halves were true and neither was useful: the
    stage counted zero, and the COUNT query that established this
    returned its single row exactly as it should.
    """
    parts = [f'<div class="empty" id="{esc(zid)}">']
    parts.append("<b>This stage counted zero.</b> ")
    if meaning:
        parts.append(f"{esc(meaning)} ")
    if result.error:
        parts.append(
            f'<span class="funnel-drop">The count FAILED: {esc(result.error)} '
            "&mdash; that is a broken query, not an absence of data.</span>")
    else:
        parts.append("The counting query ran normally and returned 0.")
    parts.append(
        f'<details id="{esc(zid)}-detail"{" open" if result.error else ""}>'
        "<summary>the query that counted it</summary>"
        f"<pre>{esc(result.sql)}\nparams: {esc(result.params)}</pre></details>")
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
