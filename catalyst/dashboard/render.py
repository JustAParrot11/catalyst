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

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       margin: 0; color: #1b1b23; background: #f4f4f8; }
header { background: #1b1b23; color: #fff; padding: 10px 18px; }
header h1 { font-size: 16px; margin: 0 0 6px 0; font-weight: 600; }
nav a { color: #cfd4ff; margin-right: 14px; text-decoration: none; font-size: 13px; }
nav a.active { color: #fff; font-weight: 700; text-decoration: underline; }
main { padding: 14px 18px 40px 18px; max-width: 1180px; }
section { background: #fff; border: 1px solid #d7d7e0; border-radius: 5px;
          padding: 12px 14px; margin-bottom: 16px; }
section > h2 { font-size: 15px; margin: 0 0 8px 0; }
h3 { font-size: 13px; margin: 14px 0 6px 0; }
.prov { color: #55555f; font-size: 12px; margin: 4px 0; }
.caveat { background: #fff8e1; border-left: 4px solid #c99700; padding: 8px 10px;
          margin: 8px 0; font-size: 13px; }
.alarm { background: #fdecec; border-left: 4px solid #b3261e; padding: 8px 10px;
         margin: 8px 0; font-size: 13px; }
.ok { background: #eaf6ec; border-left: 4px solid #2e7d32; padding: 8px 10px;
      margin: 8px 0; font-size: 13px; }
.empty { background: #f0f0f5; border: 1px dashed #9a9aa8; padding: 8px 10px;
         margin: 8px 0; font-size: 12px; }
.big { font-size: 26px; font-weight: 700; }
.pos { color: #1e6b2a; } .neg { color: #b3261e; }
table { border-collapse: collapse; width: 100%; margin: 6px 0; font-size: 13px; }
th, td { border: 1px solid #dcdce4; padding: 4px 7px; text-align: left;
         vertical-align: top; }
th { background: #f0f0f6; font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
pre { background: #1b1b23; color: #e6e6f0; padding: 8px; overflow-x: auto;
      font-size: 12px; border-radius: 4px; white-space: pre-wrap;
      word-break: break-word; max-height: 380px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
details { margin: 6px 0; }
summary { cursor: pointer; font-size: 13px; color: #2b3a8f; }
footer { color: #55555f; font-size: 12px; padding: 10px 18px 24px 18px; }
.funnel-row { display: flex; align-items: center; gap: 10px; margin: 3px 0; }
.funnel-bar { background: #3a4bbf; height: 17px; border-radius: 2px; min-width: 2px; }
.funnel-label { width: 210px; font-size: 13px; }
.funnel-n { width: 74px; text-align: right; font-variant-numeric: tabular-nums; }
.funnel-drop { color: #8a2f2f; font-size: 12px; }
.blame { background: #fdecec; border-left: 4px solid #b3261e; padding: 8px 10px;
         font-size: 13px; margin: 8px 0; }
form.inline { display: inline-block; margin: 6px 0; }
input, select, button { font: inherit; padding: 3px 6px; }
.chart { display: block; margin: 6px 0; }
.tag { display: inline-block; background: #ececf4; border: 1px solid #d0d0dc;
       border-radius: 3px; padding: 0 5px; font-size: 12px; margin-right: 4px; }
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
    not look the same (ui-designer rule 2, CLAUDE.md house rule 3)."""
    parts = [f'<div class="empty" id="{esc(eid)}">']
    parts.append("<b>Empty result — here is exactly why it is empty.</b><br>")
    if meaning:
        parts.append(f"{esc(meaning)}<br>")
    parts.append(f"rows returned: <b>{result.row_count}</b><br>")
    if result.error:
        parts.append(
            f'<span class="funnel-drop">query FAILED: {esc(result.error)} '
            "— this is a broken query, not an absence of data.</span>"
        )
    else:
        parts.append("query ran without error — this is an absence of data, not a fault.")
    parts.append(f"<pre>{esc(result.sql)}\nparams: {esc(result.params)}</pre>")
    if upstream is not None:
        parts.append("raw upstream response beside the zero:")
        parts.append(pre(upstream))
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
    return (
        f'<table id="{esc(tid)}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )


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
