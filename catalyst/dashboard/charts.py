"""Server-rendered SVG charts, sized so labels cannot escape the viewBox.

Two hard-won lessons are encoded here rather than trusted to review:

1. "A chart drew its labels outside its own viewBox and the code looked
   perfect." So the left margin is COMPUTED from the widest y-axis label
   actually produced, not guessed at, and text_boxes() below exists so a
   test can measure the rendered output instead of reading the code.

2. "A chart reading 100 on a $1,000 account looks like a bug." So every
   y tick carries three readings: the index value, the percentage move,
   and the dollar value on the fixed $1,000 account.
"""

import re
from dataclasses import dataclass

FONT_SIZE = 11
#: Conservative advance width for the monospace stack below, at
#: FONT_SIZE. Used both to size margins and to measure in tests; being
#: an over-estimate is the safe direction for both.
CHAR_W = 6.9


@dataclass(frozen=True)
class Series:
    label: str
    points: list  # [(x_float, y_index_float)]
    color: str
    dash: str = ""


def _fmt_tick(index_value: float, start_capital_dollars: float) -> str:
    pct = index_value - 100.0
    dollars = start_capital_dollars * index_value / 100.0
    return f"{index_value:6.1f} | {pct:+5.1f}% | ${dollars:,.0f}"


def _nice_bounds(values: list[float]) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        lo, hi = lo - 1.0, hi + 1.0
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


def index_chart(
    series: list[Series],
    *,
    chart_id: str,
    x_labels: list,           # [(x_float, "2026-08-01"), ...]
    width: int = 820,
    height: int = 320,
    start_capital_dollars: float = 1000.0,
    y_axis_title: str = "Index (start = 100)  |  % move  |  $ on a $1,000 account",
) -> str:
    """Return an SVG string. Raises on empty input — an empty chart is the
    caller's job to explain with its raw query, not this function's job
    to fake."""
    usable = [s for s in series if s.points]
    if not usable:
        raise ValueError("index_chart called with no points; render the empty-state block instead")

    ys = [p[1] for s in usable for p in s.points]
    xs = [p[0] for s in usable for p in s.points]
    y_lo, y_hi = _nice_bounds(ys)
    x_lo, x_hi = min(xs), max(xs)
    if x_hi - x_lo < 1e-9:
        x_hi = x_lo + 1.0

    ticks = [y_lo + (y_hi - y_lo) * i / 4 for i in range(5)]
    tick_labels = [_fmt_tick(t, start_capital_dollars) for t in ticks]

    # Margin computed from the widest label actually rendered (lesson 1).
    m_left = max(len(t) for t in tick_labels) * CHAR_W + 12
    m_right = 12
    m_top = 34
    m_bottom = 46

    # Lay the legend out BEFORE fixing the height, wrapping onto extra
    # rows if the labels are long. Same lesson as the left margin: the
    # box grows to fit the text, the text never spills out of the box.
    entries = [(s, 24 + len(s.label) * CHAR_W + 22) for s in usable]
    available = width - m_left - 8
    legend_rows: list[list] = []
    current, current_w = [], 0.0
    for s, w in entries:
        if current and current_w + w > available:
            legend_rows.append(current)
            current, current_w = [], 0.0
        current.append((s, w))
        current_w += w
    if current:
        legend_rows.append(current)
    extra = 16 * (len(legend_rows) - 1)
    m_bottom += extra
    height += extra

    plot_w = width - m_left - m_right
    plot_h = height - m_top - m_bottom

    def px(x: float) -> float:
        return m_left + (x - x_lo) / (x_hi - x_lo) * plot_w

    def py(y: float) -> float:
        return m_top + (1 - (y - y_lo) / (y_hi - y_lo)) * plot_h

    out = [
        f'<svg id="{chart_id}" class="chart" viewBox="0 0 {width} {height}" '
        f'width="100%" height="{height}" role="img" '
        f'aria-label="{y_axis_title}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="var(--surface)" stroke="var(--hairline)"/>',
    ]

    out.append(
        f'<text x="{m_left:.1f}" y="16" font-size="{FONT_SIZE}" text-anchor="start" '
        f'fill="var(--ink-2)">{y_axis_title}</text>'
    )

    for value, label in zip(ticks, tick_labels):
        y = py(value)
        out.append(
            f'<line x1="{m_left:.1f}" y1="{y:.1f}" x2="{m_left + plot_w:.1f}" y2="{y:.1f}" '
            f'stroke="var(--hairline)"/>'
        )
        out.append(
            f'<text x="{m_left - 6:.1f}" y="{y + 4:.1f}" font-size="{FONT_SIZE}" '
            f'text-anchor="end" fill="var(--muted)">{label}</text>'
        )

    # The 100 line: "no change" is where a reader's eye goes first.
    if y_lo <= 100.0 <= y_hi:
        y100 = py(100.0)
        out.append(
            f'<line x1="{m_left:.1f}" y1="{y100:.1f}" x2="{m_left + plot_w:.1f}" '
            f'y2="{y100:.1f}" stroke="var(--baseline)" stroke-dasharray="4 3"/>'
        )

    for x, label in x_labels:
        cx = px(x)
        anchor = "middle"
        half = len(label) * CHAR_W / 2
        if cx - half < 2:
            anchor, cx = "start", max(cx, 2)
        elif cx + half > width - 2:
            anchor, cx = "end", min(cx, width - 2)
        out.append(
            f'<line x1="{px(x):.1f}" y1="{m_top + plot_h:.1f}" x2="{px(x):.1f}" '
            f'y2="{m_top + plot_h + 4:.1f}" stroke="var(--baseline)"/>'
        )
        out.append(
            f'<text x="{cx:.1f}" y="{m_top + plot_h + 17:.1f}" font-size="{FONT_SIZE}" '
            f'text-anchor="{anchor}" fill="var(--muted)">{label}</text>'
        )

    for s in usable:
        pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in s.points)
        dash = f' stroke-dasharray="{s.dash}"' if s.dash else ""
        out.append(
            f'<polyline fill="none" stroke="{s.color}" stroke-width="2" '
            f'points="{pts}"{dash}/>'
        )

    # Legend, below the plot, on as many rows as it needs.
    ly = m_top + plot_h + 34
    for row in legend_rows:
        lx = m_left + 6
        for s, w in row:
            out.append(
                f'<line x1="{lx:.1f}" y1="{ly - 4:.1f}" x2="{lx + 18:.1f}" '
                f'y2="{ly - 4:.1f}" stroke="{s.color}" stroke-width="3"/>'
            )
            out.append(
                f'<text x="{lx + 24:.1f}" y="{ly:.1f}" font-size="{FONT_SIZE}" '
                f'text-anchor="start" fill="var(--ink-2)">{s.label}</text>'
            )
            lx += w
        ly += 16

    out.append("</svg>")
    return "\n".join(out)


def placeholder(
    *,
    chart_id: str,
    title: str,
    explanation: str,
    width: int = 820,
    height: int = 190,
) -> str:
    """An empty chart drawn as an EMPTY CHART, not as nothing.

    A blank space where a graph belongs reads as a broken page. This
    draws the frame, the baseline and a plain-English line saying what
    will appear here and what has to happen first - so "no data yet"
    and "the query is broken" stay visually distinct, which is the same
    reason empty_block() prints its raw query beside every zero.
    """
    m_left, m_bottom, m_top = 46, 26, 30
    plot_w = width - m_left - 12
    plot_h = height - m_top - m_bottom
    out = [
        f'<svg id="{chart_id}" class="chart" viewBox="0 0 {width} {height}" '
        f'width="100%" height="{height}" role="img" '
        f'aria-label="{title}: {explanation}" '
        f'xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="var(--surface)" stroke="var(--hairline)"/>',
        f'<text x="{m_left:.1f}" y="16" font-size="{FONT_SIZE}" '
        f'text-anchor="start" fill="var(--ink-2)">{title}</text>',
    ]
    for i in range(4):
        y = m_top + plot_h * i / 3
        out.append(
            f'<line x1="{m_left}" y1="{y:.1f}" x2="{m_left + plot_w}" '
            f'y2="{y:.1f}" stroke="var(--hairline)" stroke-dasharray="3 4"/>')
    out.append(
        f'<line x1="{m_left}" y1="{m_top + plot_h:.1f}" x2="{m_left + plot_w}" '
        f'y2="{m_top + plot_h:.1f}" stroke="var(--baseline)"/>')
    out.append(
        f'<text x="{m_left + plot_w / 2:.1f}" y="{m_top + plot_h / 2:.1f}" '
        f'font-size="{FONT_SIZE}" text-anchor="middle" '
        f'fill="var(--muted)">{explanation}</text>')
    out.append("</svg>")
    return "\n".join(out)


def bar_chart(
    bars: list,                 # [(label, value_float, tooltip_str)]
    *,
    chart_id: str,
    title: str,
    value_fmt=lambda v: f"{v:,.2f}",
    reference: tuple | None = None,   # (value, label) e.g. the daily cap
    width: int = 820,
    height: int = 240,
    color: str = "var(--series-1)",
) -> str:
    """Vertical bars for a single measure over time (daily spend).

    One series, so no legend box - the title names it (a legend for one
    series is noise). Bars carry a <title> child, which is the whole
    hover layer: a real tooltip with no JavaScript and nothing fetched
    from outside, which this page could not load anyway.
    """
    if not bars:
        raise ValueError("bar_chart called with no bars; use placeholder()")
    values = [b[1] for b in bars]
    top = max(values + ([reference[0]] if reference else []) + [0.0])
    if top <= 0:
        top = 1.0
    top *= 1.15

    tick_labels = [value_fmt(top * i / 4) for i in range(5)]
    m_left = max(len(t) for t in tick_labels) * CHAR_W + 12
    m_right, m_top, m_bottom = 14, 30, 40
    plot_w = width - m_left - m_right
    plot_h = height - m_top - m_bottom

    def py(v: float) -> float:
        return m_top + (1 - v / top) * plot_h

    out = [
        f'<svg id="{chart_id}" class="chart" viewBox="0 0 {width} {height}" '
        f'width="100%" height="{height}" role="img" aria-label="{title}" '
        f'xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="var(--surface)" stroke="var(--hairline)"/>',
        f'<text x="{m_left:.1f}" y="16" font-size="{FONT_SIZE}" '
        f'text-anchor="start" fill="var(--ink-2)">{title}</text>',
    ]
    for i, label in enumerate(tick_labels):
        y = py(top * i / 4)
        out.append(
            f'<line x1="{m_left:.1f}" y1="{y:.1f}" x2="{m_left + plot_w:.1f}" '
            f'y2="{y:.1f}" stroke="var(--hairline)"/>')
        out.append(
            f'<text x="{m_left - 6:.1f}" y="{y + 4:.1f}" font-size="{FONT_SIZE}" '
            f'text-anchor="end" fill="var(--muted)">{label}</text>')

    # 2px surface gap between neighbouring bars, per the mark spec.
    slot = plot_w / len(bars)
    bar_w = max(2.0, min(46.0, slot - 2))
    for i, (label, value, tip) in enumerate(bars):
        cx = m_left + slot * (i + 0.5)
        x = cx - bar_w / 2
        y = py(max(value, 0.0))
        h = max(1.0, m_top + plot_h - y)
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
            f'rx="3" fill="{color}"><title>{tip}</title></rect>')
        # Label every bar only while they are few enough to stay legible;
        # past that, first and last only - never a number on every mark.
        if len(bars) <= 12 or i in (0, len(bars) - 1):
            out.append(
                f'<text x="{cx:.1f}" y="{m_top + plot_h + 15:.1f}" '
                f'font-size="{FONT_SIZE}" text-anchor="middle" '
                f'fill="var(--muted)">{label}</text>')

    if reference is not None:
        rv, rlabel = reference
        ry = py(rv)
        out.append(
            f'<line x1="{m_left:.1f}" y1="{ry:.1f}" x2="{m_left + plot_w:.1f}" '
            f'y2="{ry:.1f}" stroke="var(--critical)" stroke-width="2" '
            f'stroke-dasharray="6 4"/>')
        # A PLATE UNDER THE LABEL. It sits inside the plot, so on a full
        # chart it lands on top of the bars and the two colours fight -
        # rendered and looked at: "cap, pro-rata per day" was reading
        # through the last three bars. The plate is the page ground, so
        # the words sit on the same background as the axis labels.
        lw = len(rlabel) * CHAR_W + 8
        out.append(
            f'<rect x="{m_left + plot_w - lw:.1f}" y="{ry - 16:.1f}" '
            f'width="{lw:.1f}" height="14" fill="var(--page)" '
            f'opacity="0.85" rx="2"/>')
        out.append(
            f'<text x="{m_left + plot_w - 4:.1f}" y="{ry - 5:.1f}" '
            f'font-size="{FONT_SIZE}" text-anchor="end" '
            f'fill="var(--critical)">{rlabel}</text>')
    out.append("</svg>")
    return "\n".join(out)


_TEXT_RE = re.compile(
    r'<text\s+x="([-\d.]+)"\s+y="([-\d.]+)"\s+font-size="(\d+)"\s+'
    r'text-anchor="(\w+)"[^>]*>(.*?)</text>',
    re.S,
)
_VIEWBOX_RE = re.compile(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"')


def viewbox(svg: str) -> tuple[float, float, float, float]:
    m = _VIEWBOX_RE.search(svg)
    if not m:
        raise ValueError("svg has no viewBox")
    return tuple(float(g) for g in m.groups())  # type: ignore[return-value]


def text_boxes(svg: str) -> list[tuple[float, float, float, float, str]]:
    """Approximate bounding boxes of every <text> in the SVG, so a test
    can MEASURE that labels landed inside the viewBox rather than read
    the code and believe it."""
    boxes = []
    for x, y, size, anchor, content in _TEXT_RE.findall(svg):
        x, y, size = float(x), float(y), float(size)
        w = len(content) * CHAR_W * (size / FONT_SIZE)
        if anchor == "end":
            x0 = x - w
        elif anchor == "middle":
            x0 = x - w / 2
        else:
            x0 = x
        boxes.append((x0, y - size, x0 + w, y + size * 0.3, content))
    return boxes


def labels_outside_viewbox(svg: str) -> list[str]:
    vx, vy, vw, vh = viewbox(svg)
    bad = []
    for x0, y0, x1, y1, content in text_boxes(svg):
        if x0 < vx or y0 < vy or x1 > vx + vw or y1 > vy + vh:
            bad.append(f"{content!r} box=({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f})")
    return bad


# --------------------------------------------------------------------------
# Evidence mindmap
# --------------------------------------------------------------------------

#: Reliability drives the EDGE, because it is a property of the link
#: (how the claim was sourced), not of the thing at either end.
#: How firm a link is, drawn as a line style.
#:
#: THIS WAS A LOOKUP TABLE AND IT WAS WRONG ABOUT EVERY REAL VALUE.
#: It keyed on "primary", "secondary", "inferred" - but schema_graph.sql
#: stores `primary_document`, `official_schedule`, `secondary_report`
#: and `model_inference`, none of which matched. Every edge fell through
#: to the default, and the default was the DOTTED style that means "the
#: model guessed this". So a fact taken from an SEC filing was drawn
#: identically to Claude's speculation - and drawn AS speculation.
#:
#: House rule 7, exactly: classify by the rule, not by enumeration. A
#: hand-written list mislabels the first case nobody thought of, and
#: here it mislabelled all four of the ones that actually exist.
def reliability_dash(reliability) -> str:
    """"" solid for a document, "5 3" for a report, "2 4" for a guess."""
    text = str(reliability or "").lower()
    if "model" in text or "infer" in text:
        return "2 4"
    if "secondary" in text or "report" in text or "news" in text:
        return "5 3"
    # SOLID IS EARNED, never assumed. Only a value that says it came
    # from a document or an official schedule gets the strongest line;
    # anything unrecognised - including blank - falls to the weakest.
    #
    # This is the direction the default has to point. The version this
    # replaces had it the other way round and drew SEC filings as model
    # guesses; getting it wrong in the other direction would be worse,
    # because it would dress a guess up as a document.
    if any(w in text for w in ("primary", "document", "official",
                               "schedule", "filed", "exchange")):
        return ""
    return "2 4"


#: Kept as a name for anything still importing it, derived from the rule
#: above so the two can never disagree.
_RELIABILITY_DASH = {k: reliability_dash(k) for k in (
    "primary_document", "official_schedule", "secondary_report",
    "model_inference", "filed", "exchange", "primary", "reported",
    "secondary", "inferred", "model")}


def _wrap(text: str, width: int) -> list:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines[:3] or [""]


def mindmap(
    centre_label: str,
    branches: list,              # [(predicate, node_label, kind, reliability, source)]
    *,
    chart_id: str,
    width: int = 900,
    max_branches: int = 12,
) -> str:
    """A radial node-link view of the evidence behind one candidate.

    Deterministic layout by design: nodes are placed on a circle in the
    order given, so the same evidence always draws the same picture and
    two screenshots of one candidate can be compared. No physics, no
    animation, no JavaScript - this page cannot load any.

    Long labels are WRAPPED rather than clipped, and the node box is
    sized from the wrapped text, so a label can never overflow its own
    box (the same lesson index_chart records about axis labels).
    """
    shown = branches[:max_branches]
    if not shown:
        raise ValueError("mindmap needs at least one branch; use placeholder()")

    import math

    n = len(shown)
    rows = max(_wrap(b[1], 22) for b in shown)
    node_h = 20 + 13 * max(len(_wrap(b[1], 22)) for b in shown)
    radius_x = width / 2 - 105
    radius_y = max(150.0, 34.0 * n / 2)
    # +22 for the legend strip along the bottom, so it never lands on
    # the lowest node.
    height = int(2 * radius_y + node_h + 90) + 22
    cx, cy = width / 2, height / 2

    out = [
        f'<svg id="{chart_id}" class="chart" viewBox="0 0 {width} {height}" '
        f'width="100%" height="{height}" role="img" '
        f'aria-label="Evidence linked to {centre_label}: '
        f'{n} connected facts" xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="var(--surface)" stroke="var(--hairline)"/>',
    ]

    # Edges first, so nodes paint over them.
    positions = []
    for i, (predicate, label, kind, reliability, source) in enumerate(shown):
        angle = -math.pi / 2 + (2 * math.pi * i / n)
        nx = cx + radius_x * math.cos(angle)
        ny = cy + radius_y * math.sin(angle)
        positions.append((nx, ny))
        dash = reliability_dash(reliability)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{nx:.1f}" y2="{ny:.1f}" '
            f'stroke="var(--baseline)" stroke-width="1.5"{dash_attr}>'
            f"<title>{predicate} ({reliability or 'unrecorded'} - "
            f"{source or 'no source recorded'})</title></line>")
        # Predicate label at the midpoint of the edge.
        mx, my = (cx + nx) / 2, (cy + ny) / 2
        out.append(
            f'<text x="{mx:.1f}" y="{my - 3:.1f}" font-size="{FONT_SIZE - 1}" '
            f'text-anchor="middle" fill="var(--muted)">{predicate}</text>')

    # Branch nodes.
    for (nx, ny), (predicate, label, kind, reliability, source) in zip(
            positions, shown):
        lines = _wrap(label, 22)
        box_w = max(len(ln) for ln in lines) * CHAR_W + 22
        box_h = 16 + 13 * len(lines)
        x, y = nx - box_w / 2, ny - box_h / 2
        out.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{box_w:.1f}" '
            f'height="{box_h:.1f}" rx="6" fill="var(--surface-2)" '
            f'stroke="var(--hairline)"><title>{kind or "entity"}: {label}'
            f"</title></rect>")
        for j, ln in enumerate(lines):
            out.append(
                f'<text x="{nx:.1f}" y="{y + 15 + 13 * j:.1f}" '
                f'font-size="{FONT_SIZE}" text-anchor="middle" '
                f'fill="var(--ink-2)">{ln}</text>')

    # The candidate, last and largest.
    clines = _wrap(centre_label, 20)
    cw = max(len(ln) for ln in clines) * CHAR_W + 30
    ch = 20 + 14 * len(clines)
    out.append(
        f'<rect x="{cx - cw / 2:.1f}" y="{cy - ch / 2:.1f}" width="{cw:.1f}" '
        f'height="{ch:.1f}" rx="8" fill="var(--step-3)" '
        f'stroke="var(--step-5)" stroke-width="2"/>')
    for j, ln in enumerate(clines):
        out.append(
            f'<text x="{cx:.1f}" y="{cy - ch / 2 + 18 + 14 * j:.1f}" '
            f'font-size="{FONT_SIZE + 1}" text-anchor="middle" '
            f'fill="#ffffff">{ln}</text>')

    # WHAT THE LINE STYLES MEAN, ON THE PICTURE. It was written in the
    # paragraph below the chart, which is not where anyone looks while
    # reading the chart - and the distinction it carries is the most
    # important one here: whether a link came from a filed document or
    # from the model guessing.
    ly = height - 12
    lx = 12.0
    for dash, words in (("", "filed with a regulator"),
                        ("5 3", "reported"),
                        ("2 4", "Claude inferred it")):
        da = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(
            f'<line x1="{lx:.1f}" y1="{ly - 4:.1f}" x2="{lx + 22:.1f}" '
            f'y2="{ly - 4:.1f}" stroke="var(--baseline)" '
            f'stroke-width="1.5"{da}/>')
        out.append(
            f'<text x="{lx + 28:.1f}" y="{ly:.1f}" '
            f'font-size="{FONT_SIZE - 1}" text-anchor="start" '
            f'fill="var(--muted)">{words}</text>')
        lx += 34 + _text_width_px(words) + 18

    out.append("</svg>")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Decision spider: the whole decision as one picture
# --------------------------------------------------------------------------

#: Three groups, three categorical slots. A spider is an ALL-PAIRS form -
#: any node can end up beside any other - and the reference palette caps
#: all-pairs categorical sets at three slots. Validated in both modes:
#: worst CVD dE 9.2 light / 9.4 dark, worst normal-vision dE 24.0 / 20.9.
#: Aqua is under 3:1 on the light surface, so the relief rule applies and
#: every node carries a visible text label - identity is never colour alone.
SPIDER_SLOTS = ("var(--series-1)", "var(--series-2)", "var(--series-3)")


def _leaf_box(leaf_label: str) -> tuple[float, float]:
    """The box a leaf label will occupy. One definition, used both to
    place the leaf and to draw it - two functions disagreeing about a
    box size is precisely how things end up overlapping."""
    lines = _wrap(leaf_label, 20)
    return (max(len(ln) for ln in lines) * CHAR_W + 20,
            15.0 + 13 * len(lines))


def _boxes_overlap(a, b, pad: float = 6.0) -> bool:
    """Axis-aligned overlap with a little breathing room, so boxes are
    separated rather than merely not-quite-touching."""
    return (a[0] - pad < b[0] + b[2] and b[0] - pad < a[0] + a[2]
            and a[1] - pad < b[1] + b[3] and b[1] - pad < a[1] + a[3])


def decision_spider(
    centre_label: str,
    verdict: str,
    groups: list,          # [(group_label, [(leaf_label, detail), ...]), ...]
    *,
    chart_id: str,
    width: int = 940,
) -> str:
    """One decision as a spider: candidate, its branches, their facts.

    The flat mindmap answers "what evidence exists". This answers the
    bigger question - what did the bot SEE, CONCLUDE and DO - by giving
    each of those its own arm rather than mixing them on one ring, so
    the shape of the decision is readable before any of the words are.

    Deterministic: arms are placed by their position in `groups` and
    leaves by their order within an arm, so the same decision always
    draws the same picture and two screenshots can be compared. No
    physics, no animation, no JavaScript - this page cannot load any.
    """
    import math

    groups = [(label, leaves) for label, leaves in groups if leaves][:3]
    if not groups:
        raise ValueError("decision_spider needs at least one populated group")

    n_groups = len(groups)
    max_leaves = max(len(leaves) for _, leaves in groups)
    height = int(max(430, 250 + 74 * max_leaves))
    cx, cy = width / 2, height / 2
    hub_r = min(width, height) * 0.21
    leaf_r = min(width / 2 - 118, height / 2 - 52)
    #: Adjacent leaves in one wedge are wide boxes at the same radius, so
    #: they collide the moment an arm carries more than three (caught by
    #: rendering it: "J. Restrepo, CFO" sat on top of two neighbours).
    #: Alternating the radius separates them without moving any label off
    #: its own spoke, and stays deterministic.
    STAGGER = 26.0

    out = [
        f'<svg id="{chart_id}" class="chart" viewBox="0 0 {width} {height}" '
        f'width="100%" height="{height}" role="img" xmlns="http://www.w3.org/2000/svg" '
        f'aria-label="Decision for {centre_label}: {verdict}. '
        + "; ".join(f"{label} has {len(leaves)} point(s)"
                    for label, leaves in groups) + '">',
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="var(--surface)" stroke="var(--hairline)"/>',
    ]

    # Every box placed so far, across ALL arms. Collision is checked
    # globally rather than per arm, because adjacent arms are exactly
    # where two wedges meet and where leaves collided in practice.
    #
    # THE CENTRE AND THE HUBS GO IN FIRST. They are drawn last, so they
    # sit on top - which is exactly why a leaf underneath one is
    # invisible rather than merely untidy. Registering them before any
    # leaf is placed is what stops a fact being painted over by the very
    # label that is supposed to introduce it.
    clines = _wrap(centre_label, 14)
    cw = max(max(len(ln) for ln in clines), len(verdict)) * CHAR_W + 34
    ch = 22 + 15 * len(clines) + 14
    placed: list = [(cx - cw / 2, cy - ch / 2, cw, ch)]

    # HOW FAR OUT THE HUBS SIT IS DERIVED, not a fixed fraction of the
    # canvas. `min(width, height) * 0.21` ignores how big the centre
    # label actually is, so a two-line candidate name pushed the centre
    # box out under its own arm labels - measured, two hub boxes
    # overlapping the centre box on an ordinary decision.
    hub_boxes = []
    for glabel, _leaves in groups:
        hub_lines = _wrap(glabel, 16)
        hub_boxes.append((max(len(ln) for ln in hub_lines) * CHAR_W + 22,
                          16 + 13 * len(hub_lines), hub_lines))
    _max_hw = max(b[0] for b in hub_boxes)
    _max_hh = max(b[1] for b in hub_boxes)
    hub_r = max(hub_r,
                cw / 2 + _max_hw / 2 + 16,
                ch / 2 + _max_hh / 2 + 16)
    leaf_r = max(leaf_r, hub_r + _max_hh)

    hubs = []
    for gi, (glabel, _leaves) in enumerate(groups):
        arm_angle = -math.pi / 2 + (2 * math.pi * gi / n_groups)
        hx = cx + hub_r * math.cos(arm_angle)
        hy = cy + hub_r * math.sin(arm_angle)
        hw, hh, hub_lines = hub_boxes[gi]
        hubs.append((hx, hy, hw, hh, hub_lines))
        placed.append((hx - hw / 2, hy - hh / 2, hw, hh))

    # Arms are spread over the full circle, each arm's leaves fanned
    # inside its own wedge so two groups never interleave.
    for gi, (glabel, leaves) in enumerate(groups):
        colour = SPIDER_SLOTS[gi % len(SPIDER_SLOTS)]
        arm_angle = -math.pi / 2 + (2 * math.pi * gi / n_groups)
        hx = cx + hub_r * math.cos(arm_angle)
        hy = cy + hub_r * math.sin(arm_angle)

        wedge = (2 * math.pi / n_groups) * 0.78
        k = len(leaves)

        spots: dict = {}

        def place(li: int):
            """Where leaf `li` sits, with its box guaranteed not to
            overlap any box already placed.

            THE OLD VERSION COLLIDED, and that is what the owner was
            looking at: "they dont format well its a bunch of lines with
            cut off text". Measured on a realistic decision - three arms
            of three facts - FOUR pairs of leaf boxes overlapped, so
            labels were painted over each other and read as clipped.

            The old fix was a fixed 26px stagger on alternate leaves,
            which is a guess at how wide a box is. This measures the box
            instead and walks the leaf outwards along its own spoke
            until it is clear. Still deterministic: same groups in, same
            picture out, because the search order is fixed and the step
            is fixed.
            """
            # MEMOISED. place() is called once for the connector and
            # again for the box; without this the second call would see
            # the first call's own box already registered and shove the
            # leaf outwards, so the line and the box it points at would
            # be drawn in two different places.
            if li in spots:
                return spots[li]
            offset = 0.0 if k == 1 else (li / (k - 1) - 0.5) * wedge
            a = arm_angle + offset
            bw, bh = _leaf_box(leaves[li][0])
            r = leaf_r - (STAGGER if (k > 3 and li % 2) else 0.0)
            for _ in range(40):          # bounded: never loop forever
                x = cx + r * math.cos(a)
                y = cy + r * math.sin(a)
                box = (x - bw / 2, y - bh / 2, bw, bh)
                if not any(_boxes_overlap(box, other) for other in placed):
                    placed.append(box)
                    spots[li] = (x, y)
                    return x, y
                r += 18.0
            placed.append((cx + r * math.cos(a) - bw / 2,
                           cy + r * math.sin(a) - bh / 2, bw, bh))
            spots[li] = (cx + r * math.cos(a), cy + r * math.sin(a))
            return spots[li]

        for li, (leaf_label, detail) in enumerate(leaves):
            lx, ly = place(li)
            tip = (f"{glabel}: {leaf_label}"
                   + (f" - {detail}" if detail else ""))
            out.append(
                f'<g class="edge-wrap">'
                f'<line class="edge" x1="{hx:.1f}" y1="{hy:.1f}" '
                f'x2="{lx:.1f}" y2="{ly:.1f}" '
                f'stroke="{colour}" stroke-width="1.4" opacity="0.75" '
                f'pointer-events="none"/>'
                f'<line class="edge-hit" x1="{hx:.1f}" y1="{hy:.1f}" '
                f'x2="{lx:.1f}" y2="{ly:.1f}" stroke="transparent" '
                f'stroke-width="16" pointer-events="stroke">'
                f"<title>{tip}</title></line></g>")

        # Leaf boxes, painted after every line in the arm so they sit on top.
        for li, (leaf_label, detail) in enumerate(leaves):
            lx, ly = place(li)
            lines = _wrap(leaf_label, 20)
            box_w = max(len(ln) for ln in lines) * CHAR_W + 20
            box_h = 15 + 13 * len(lines)
            bx, by = lx - box_w / 2, ly - box_h / 2
            # A 2px surface ring keeps overlapping marks separable.
            tip = (f"{glabel}: {leaf_label}"
                   + (f" - {detail}" if detail else ""))
            out.append(
                f'<g class="node"><title>{tip}</title>'
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{box_w:.1f}" '
                f'height="{box_h:.1f}" rx="5" fill="var(--surface-2)" '
                f'stroke="{colour}" stroke-width="1.4" '
                f'paint-order="stroke" style="stroke-linejoin:round"/>'
                + "".join(
                    f'<text x="{lx:.1f}" y="{by + 14 + 13 * j:.1f}" '
                    f'font-size="{FONT_SIZE}" text-anchor="middle" '
                    f'fill="var(--ink-2)">{ln}</text>'
                    for j, ln in enumerate(lines))
                + "</g>")

        # The hub, and its spoke to the centre. Drawn after the leaves so
        # the arm's label is never buried under a fact.
        out.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{hx:.1f}" y2="{hy:.1f}" '
            f'stroke="{colour}" stroke-width="2.5"/>')
        hx, hy, hw, hh, hub_lines = hubs[gi]
        out.append(
            f'<rect x="{hx - hw / 2:.1f}" y="{hy - hh / 2:.1f}" width="{hw:.1f}" '
            f'height="{hh:.1f}" rx="5" fill="{colour}" stroke="var(--surface)" '
            f'stroke-width="2"/>')
        for j, ln in enumerate(hub_lines):
            out.append(
                f'<text x="{hx:.1f}" y="{hy - hh / 2 + 15 + 13 * j:.1f}" '
                f'font-size="{FONT_SIZE}" font-weight="700" text-anchor="middle" '
                f'fill="#ffffff">{ln}</text>')

    # The candidate last and largest: it is what everything else is about.
    out.append(
        f'<rect x="{cx - cw / 2:.1f}" y="{cy - ch / 2:.1f}" width="{cw:.1f}" '
        f'height="{ch:.1f}" rx="9" fill="var(--surface-2)" '
        f'stroke="var(--ink-2)" stroke-width="2"/>')
    for j, ln in enumerate(clines):
        out.append(
            f'<text x="{cx:.1f}" y="{cy - ch / 2 + 20 + 15 * j:.1f}" '
            f'font-size="{FONT_SIZE + 3}" font-weight="700" text-anchor="middle" '
            f'fill="var(--ink)">{ln}</text>')
    out.append(
        f'<text x="{cx:.1f}" y="{cy - ch / 2 + 20 + 15 * len(clines) + 12:.1f}" '
        f'font-size="{FONT_SIZE}" text-anchor="middle" '
        f'fill="var(--muted)">{verdict}</text>')

    out.append("</svg>")
    svg = "\n".join(out)

    # THE FRAME FITS THE DRAWING, rather than the drawing being trusted
    # to stay inside a frame chosen before it was laid out. Leaves are
    # pushed outwards to resolve collisions, so how far out the furthest
    # one ends up is not known until every one is placed - and a box
    # past the edge of the viewBox is simply not rendered, which is the
    # other half of "cut off text".
    #
    # Recomputed from the boxes actually placed, then applied to the
    # viewBox and the background rect together so they cannot disagree.
    pad = 14.0
    min_x = min(b[0] for b in placed) - pad
    min_y = min(b[1] for b in placed) - pad
    max_x = max(b[0] + b[2] for b in placed) + pad
    max_y = max(b[1] + b[3] for b in placed) + pad
    vb_x, vb_y = min(0.0, min_x), min(0.0, min_y)
    vb_w, vb_h = max(float(width), max_x) - vb_x, max(float(height), max_y) - vb_y
    svg = svg.replace(
        f'viewBox="0 0 {width} {height}"',
        f'viewBox="{vb_x:.0f} {vb_y:.0f} {vb_w:.0f} {vb_h:.0f}"', 1)
    svg = svg.replace(
        f'<rect x="0" y="0" width="{width}" height="{height}" '
        f'fill="var(--surface)" stroke="var(--hairline)"/>',
        f'<rect x="{vb_x:.0f}" y="{vb_y:.0f}" width="{vb_w:.0f}" '
        f'height="{vb_h:.0f}" fill="var(--surface)" '
        f'stroke="var(--hairline)"/>', 1)
    return svg.replace(f'height="{height}"',
                       f'height="{vb_h:.0f}"', 1)


# --------------------------------------------------------------------------
# Neural map: the whole system as layers of connected nodes
# --------------------------------------------------------------------------

#: Layer depth is ORDINAL - sources come before candidates come before
#: decisions - so the ramp is sequential single-hue, not categorical.
#: Validated for ordinal use in both modes: monotonic, adjacent OKLab
#: dL 0.093 light / 0.095 dark against a 0.06 floor.
NEURAL_STEPS = ("var(--step-1)", "var(--step-2)", "var(--step-3)",
                "var(--step-4)", "var(--step-5)")

MAX_NODES_PER_LAYER = 14

#: Columns are sized to their longest label (see neural_map). These bound
#: that: never so narrow that the nodes crowd, never so wide that the
#: owner is scrolling for a minute to reach the last column.
MIN_COL_WIDTH = 150.0
#: What a stage that recorded nothing is worth on the canvas. Enough to
#: be seen and read as a stage, not enough to push the drawing aside.
EMPTY_COL_WIDTH = 96.0
MAX_MAP_WIDTH = 2600.0
#: Node radius plus breathing room between glyph and circle.
NODE_LABEL_PAD = 26.0


def _text_width_px(text: str, font_size: float = FONT_SIZE - 1) -> float:
    """Roughly how wide `text` renders, in pixels.

    0.56em per character is the measured average for the dashboard's
    stack at these sizes. It is an approximation on purpose - the server
    cannot measure a font it is not rendering - but it is the same
    approximation the trimming already used, so widths and trim points
    agree with each other instead of disagreeing by a few characters.
    """
    return len(str(text)) * font_size * 0.56

#: Sweeps of the barycentre pass. Two forward and two back settles a
#: graph this size; more buys nothing and costs determinism nothing.
UNTANGLE_SWEEPS = 2


def _untangle(kept: list, edges: list) -> list:
    """Order each layer so its strands cross as little as possible.

    Nodes were drawn in whatever order the query returned them, so a
    node's neighbours could be at the far end of the next column and the
    map read as a tangle rather than as a flow (owner-reported
    2026-08-11: "the neural map still isnt easy to navigate and
    understand it feels jumbled").

    This is the barycentre heuristic: put each node at the average
    position of the nodes it connects to, sweep forward and back a few
    times, and crossings fall out. It is deterministic - same graph,
    same picture - because ties break on the node's existing index, and
    it never adds, removes or relabels anything. Only the vertical order
    changes.
    """
    adjacency: dict = {}
    for src, dst, _, _ in edges:
        adjacency.setdefault(src, []).append(dst)
        adjacency.setdefault(dst, []).append(src)

    def normalised() -> dict:
        """Every node's vertical position as 0..1, so layers of
        different heights can be compared without one dominating."""
        out = {}
        for _, nodes, _ in kept:
            last = max(len(nodes) - 1, 1)
            for i, (nid, _, _) in enumerate(nodes):
                out[nid] = i / last
        return out

    for _ in range(UNTANGLE_SWEEPS):
        for order in (range(len(kept)), range(len(kept) - 1, -1, -1)):
            for ci in order:
                # RECOMPUTED PER LAYER. Reading one snapshot for the
                # whole sweep let two layers reorder against each other's
                # OLD positions, so a straight reversal reversed both and
                # left every crossing exactly where it was.
                pos = normalised()
                def key(item, pos=pos):
                    nid, _, _ = item
                    # BOTH sides. Sorting against one neighbouring layer
                    # leaves the other free to tangle, which is why the
                    # first version of this only removed 18% of the
                    # crossings.
                    seen = [pos[n] for n in adjacency.get(nid, []) if n in pos]
                    return (sum(seen) / len(seen)) if seen else 2.0
                indexed = list(enumerate(kept[ci][1]))
                indexed.sort(key=lambda pair: (key(pair[1]), pair[0]))
                kept[ci][1] = [n for _, n in indexed]
    return [(label, nodes, extra) for label, nodes, extra in kept]


def neural_map(
    layers: list,       # [(layer_label, [(node_id, node_label, weight)])]
    edges: list,        # [(src_id, dst_id, weight, title)]
    *,
    chart_id: str,
    width: int = 1180,
    row_gap: int = 34,
    links: dict | None = None,   # node_id -> href, for clickable nodes
    max_per_layer: int = MAX_NODES_PER_LAYER,
    zoom: float = 1.0,
    column_notes: dict | None = None,   # column label -> what a node IS
) -> str:
    """The system's live wiring, drawn as connected layers.

    EVERY EDGE IS A RECORDED RELATIONSHIP. Nothing is added to make the
    picture denser - a connector nobody can trace back to a row is a
    decoration that looks like evidence, which is the one thing this
    dashboard must never draw.

    Deterministic: columns follow `layers`, rows follow each layer's own
    order, so the same database always draws the same brain. No physics,
    no animation, no JavaScript.
    """
    import math

    kept = []
    for label, nodes in layers:
        shown = list(nodes)[:max_per_layer]
        kept.append([label, shown, max(0, len(nodes) - len(shown))])
    if not any(nodes for _, nodes, _ in kept):
        raise ValueError("neural_map needs at least one node; use placeholder()")
    kept = _untangle(kept, edges)

    # ZOOM IS SERVER-SIDE, deliberately. This chart is documented as
    # "no physics, no animation, no JavaScript" and that is what makes it
    # reproducible; a JS pan/zoom would draw a different picture per
    # browser. Scaling the geometry and letting the container scroll
    # gives the owner room without giving up determinism.
    # ZOOM WITH THE NETWORK. The owner asked for "maybe zoom is as the
    # network gets bigger": a map that is comfortable at 20 nodes is a
    # wall at 120, and asking someone to keep re-picking a zoom as the
    # graph grows is asking them to do the layout's job. The floor rises
    # with the tallest column, so the drawing stays legible on its own
    # and an explicit choice still wins above it.
    _tallest = max((len(nodes) for _, nodes, _ in kept), default=1)
    auto = 1.0 if _tallest <= 16 else min(3.0, 1.0 + (_tallest - 16) / 24.0)
    zoom = max(1.0, min(3.0, max(float(zoom or 1.0), auto)))
    width = int(width * zoom)
    row_gap = int(row_gap * zoom)
    n_cols = len(kept)

    # COLUMNS ARE SIZED TO THEIR CONTENT, not carved out of a fixed
    # width. Dividing 1180px equally gave every label
    # `col_w / 2 - r - 14` pixels to live in - about 14 characters at
    # five columns and 11 at six - so "below_conviction_floor" and
    # "J. Restrepo, CFO" were both cut mid-word. That is the owner's
    # report, twice: "you can only read a few lines before it cuts off"
    # and "its a bunch of lines with cut off text".
    #
    # Widening the whole map instead is the honest fix: the drawing is
    # as wide as its longest name needs, and .chart-scroll scrolls it.
    # A label that has room does not need trimming at all, so the
    # trimming below becomes a genuine last resort rather than the
    # normal case.
    label_px = [
        max((_text_width_px(nl) for _, nl, _ in nodes), default=0.0)
        for _, nodes, _ in kept]
    # LABELS SIT ABOVE THEIR NODE, so a column needs its longest label
    # plus a little padding - not twice that.
    #
    # They used to sit beside it, on the side chosen by which half of
    # the map the column was in, and that rule put every label directly
    # on top of a connector: a node in the left half was labelled to its
    # RIGHT, which is exactly where its edges leave, and one in the
    # right half was labelled to its LEFT, which is where its edges
    # arrive. Measured, not guessed - EMBC sat at x=295 with its label
    # running right from 309 straight along its own outgoing curve.
    # It read as "EMBC" with a dash through it.
    #
    # Above the node there is never a horizontal connector, whichever
    # half the column is in, and the label gets the WHOLE column to live
    # in rather than half of it - which retires most of the trimming the
    # owner reported twice as "cut off text".
    #
    # AN EMPTY COLUMN COLLAPSES. Three of the six stages here are
    # routinely empty, and at a 150px floor each they took 38% of the
    # canvas to say nothing, leaving the real content crammed into the
    # middle and reading as a chart that failed to load. It still
    # appears - a stage that recorded nothing is a fact worth seeing -
    # but as a narrow strip.
    col_needs = [(EMPTY_COL_WIDTH if not nodes
                  else max(MIN_COL_WIDTH, px + NODE_LABEL_PAD))
                 for px, (_, nodes, _) in zip(label_px, kept)]
    # Never narrower than it was, never wider than can be scrolled
    # comfortably.
    width = int(max(width, min(sum(col_needs), MAX_MAP_WIDTH)))
    # SPARE WIDTH GOES TO THE COLUMNS THAT HAVE SOMETHING IN THEM.
    # Scaling every column by the same factor to fill the canvas undid
    # the collapse above: an empty stage was still handed its share of
    # the surplus, so the three empty ones came back to within a third
    # of the full ones and the drawing was still mostly blank. An empty
    # column keeps its strip; the rest share what is left.
    filled = [i for i, (_, nodes, _) in enumerate(kept) if nodes]
    spare = width - sum(col_needs)
    col_widths = list(col_needs)
    if spare > 0 and filled:
        for i in filled:
            col_widths[i] += spare * (col_needs[i] / sum(
                col_needs[j] for j in filled))
    elif sum(col_needs):
        _scale = width / sum(col_needs)
        col_widths = [w * _scale for w in col_needs]
    col_centres, _acc = [], 0.0
    for w in col_widths:
        col_centres.append(_acc + w / 2)
        _acc += w

    tallest = max((len(nodes) for _, nodes, _ in kept), default=1)
    # THE BOX FITS THE DRAWING. A 340px floor put a four-node focused map
    # in a mostly-empty frame, which reads as a chart that failed to load
    # rather than as a small answer. The floor is now only what the
    # header and caption need.
    # ROOM FOR THE COLUMN NOTES. Without them a reader has to infer
    # what a node in each column IS, and the inference goes wrong in the
    # one place it matters: three tickers all run into a single node
    # labelled "long", which reads as three candidates being MERGED when
    # it means all three were judged long. Saying "the direction Claude
    # returned - candidates sharing one are drawn into it" is the
    # difference between a picture that misleads and one that informs.
    column_notes = column_notes or {}
    note_lines = {}
    for _lbl, _nodes, _x in kept:
        if _nodes and column_notes.get(_lbl):
            note_lines[_lbl] = _wrap(column_notes[_lbl], 26)
    note_rows = max((len(v) for v in note_lines.values()), default=0)
    note_h = 12 * note_rows
    height = max(180, 96 + note_h + row_gap * tallest)
    top = 74 + note_h

    # Position every node first: edges need both endpoints.
    pos, radius = {}, {}
    max_w = max((w for _, nodes, _ in kept for _, _, w in nodes), default=1) or 1
    for ci, (label, nodes, _) in enumerate(kept):
        cx = col_centres[ci]
        for ri, (nid, _, weight) in enumerate(nodes):
            pos[nid] = (cx, top + row_gap * ri + row_gap / 2)
            radius[nid] = 4.0 + 4.0 * math.sqrt(max(weight, 0) / max_w)

    # AT ZOOM 1 the map is responsive (width 100%, shrinks to the panel).
    # ZOOMED, it must carry an explicit PIXEL width: with width="100%" the
    # browser scales the bigger viewBox straight back down to the panel,
    # so the drawing got taller and no wider and the zoom did nothing
    # horizontally. An explicit width overflows the panel, which is what
    # .chart-scroll is there to scroll.
    # AND ITS HEIGHT MUST FOLLOW. At width 100% the drawing scales down
    # to the panel but a fixed pixel height does not, so the browser
    # centres a 252px picture in a 368px box and leaves a dead band
    # above and below it. Measured, not guessed: svgH 368, content 252.
    # .map-fit hands the height back to the aspect ratio.
    svg_width = "100%" if zoom <= 1.0 else str(width)
    fit = " map-fit" if zoom <= 1.0 else ""
    out = [
        f'<svg id="{chart_id}" class="chart{fit}" viewBox="0 0 {width} {height}" '
        f'width="{svg_width}" height="{height}" role="img" xmlns="http://www.w3.org/2000/svg" '
        'aria-label="The bot\'s wiring: '
        + "; ".join(f"{label} ({len(nodes)})" for label, nodes, _ in kept)
        + '. Every line is one recorded link.">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="var(--page)" '
        f'stroke="var(--hairline)"/>',
        # EVERYTHING DRAWN GOES IN ONE GROUP. An interaction layer can
        # then pan and zoom by transforming this node alone, which is
        # the whole of what it is permitted to do: move the camera,
        # never decide what is in shot. The SVG itself stays free of
        # script, so the drawing remains a server-rendered artifact that
        # reproduces exactly with JavaScript turned off.
        # AN ARROWHEAD, so the picture says which way it runs. The
        # direction was only ever stated in prose above the chart, and a
        # node-link diagram whose lines have no direction is read as
        # "these are related", not as "this became that" - which is the
        # whole content of this drawing.
        f'<defs><marker id="{chart_id}-arrow" viewBox="0 0 8 8" refX="7" '
        f'refY="4" markerWidth="5" markerHeight="5" orient="auto-start-reverse">'
        f'<path d="M0,1 L7,4 L0,7 z" fill="var(--accent)"/></marker></defs>',
        f'<g class="camera" id="{chart_id}-camera">',
    ]

    # Column headings, then the edges beneath every node.
    for ci, (label, nodes, extra) in enumerate(kept):
        cx = col_centres[ci]
        out.append(
            f'<text x="{cx:.1f}" y="30" font-size="{FONT_SIZE}" font-weight="700" '
            f'text-anchor="middle" fill="var(--ink-2)" '
            f'letter-spacing="0.08em">{label.upper()}</text>')
        out.append(
            f'<text x="{cx:.1f}" y="46" font-size="{FONT_SIZE - 1}" '
            f'text-anchor="middle" fill="var(--muted)">'
            + (f"{len(nodes)} shown, {extra} more" if extra
               # A BARE "0" IS THE ZERO THIS PROJECT KEEPS BANNING. It
               # says nothing about whether the stage is broken or
               # simply had nothing to record, and it is the reading a
               # person lands on first.
               else "nothing yet" if not nodes else f"{len(nodes)}")
            + "</text>")
        for li, line in enumerate(note_lines.get(label, [])):
            out.append(
                f'<text x="{cx:.1f}" y="{60 + 12 * li}" '
                f'font-size="{FONT_SIZE - 2}" text-anchor="middle" '
                f'fill="var(--muted)" font-style="italic">{line}</text>')
        if not nodes:
            # A dotted spine down the empty column, so it reads as a
            # stage that is present and empty rather than as a gap where
            # the drawing failed.
            out.append(
                f'<line x1="{cx:.1f}" y1="64" x2="{cx:.1f}" '
                f'y2="{height - 26}" stroke="var(--hairline)" '
                f'stroke-dasharray="2 5"/>')

    # Edges: cubic beziers with horizontal control points. Opacity
    # carries strength, so a heavily-used link reads as a brighter one.
    max_e = max((w for _, _, w, _ in edges), default=1) or 1
    drawn = 0
    records = 0
    for src, dst, weight, title in edges:
        if src not in pos or dst not in pos:
            continue        # an endpoint past the per-layer cap
        x1, y1 = pos[src]
        x2, y2 = pos[dst]
        # STOP SHORT OF THE TARGET, so the arrowhead lands OUTSIDE the
        # circle. Nodes are painted last, on purpose, so an edge ending
        # at the node centre has its arrowhead covered by the node -
        # which is how the first version of this shipped with markers
        # declared, attached, and invisible in every rendering.
        _gap = radius[dst] + 6.0
        if x2 >= x1:
            x2 = max(x1, x2 - _gap)
        else:
            x2 = min(x1, x2 + _gap)
        cxa, cxb = x1 + (x2 - x1) * 0.45, x2 - (x2 - x1) * 0.45
        opacity = 0.22 + 0.55 * (weight / max_e)
        # TWO paths per edge. A 1.1px line is close to unhittable with a
        # mouse and impossible with a finger, so the owner reported that
        # nothing happened on hover at all. The first path is a fat,
        # invisible hit area; the second is the line you see. Both carry
        # the <title>, so the tooltip fires anywhere near the strand.
        d = (f'M{x1:.1f},{y1:.1f} C{cxa:.1f},{y1:.1f} {cxb:.1f},{y2:.1f} '
             f'{x2:.1f},{y2:.1f}')
        # ONE GROUP per edge, and the fat transparent hit path LAST.
        # Two things had to be true before hovering did anything, and
        # both were found in a browser rather than in the markup:
        #   1. pointer-events="stroke" - the default is visiblePainted,
        #      under which a fully transparent stroke is not painted and
        #      so cannot be hit at all.
        #   2. the hit path on top and the highlight driven by GROUP
        #      hover - with the visible line painted last it was the
        #      element under the pointer, so a sibling selector on the
        #      hit path never matched.
        out.append(
            f'<g class="edge-wrap" data-src="{src}" data-dst="{dst}">'
            f'<path class="edge" d="{d}" fill="none" stroke="var(--accent)" '
            f'stroke-width="1.1" opacity="{opacity:.2f}" pointer-events="none" '
            f'marker-end="url(#{chart_id}-arrow)"/>'
            f'<path class="edge-hit" d="{d}" fill="none" stroke="transparent" '
            f'stroke-width="14" pointer-events="stroke">'
            f"<title>{title}</title></path></g>")
        drawn += 1
        records += max(1, int(weight or 1))

    # Nodes last, so no connector crosses a label.
    links = links or {}
    for ci, (label, nodes, _) in enumerate(kept):
        colour = NEURAL_STEPS[min(ci, len(NEURAL_STEPS) - 1)]
        for nid, node_label, weight in nodes:
            x, y = pos[nid]
            r = radius[nid]
            out.append(
                f'<g class="node" data-node="{nid}" data-label="{node_label}" '
                f'data-links="{weight}" data-layer="{label}" '
                f'tabindex="0" role="button">'
                f'<circle cx="{x:.1f}" cy="{y:.1f}" '
                f'r="{max(r + 9, 13):.1f}" fill="transparent" '
                f'pointer-events="all">'
                f"<title>{node_label} - {weight} link(s)</title></circle>"
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{colour}" '
                f'stroke="var(--page)" stroke-width="2" '
                f'pointer-events="none"/></g>')
            # ABOVE THE NODE, CENTRED. See the column-width note above:
            # beside it, the label always lay along the node's own
            # connectors. Above it there is nothing to collide with, the
            # rule is the same in every column, and the label gets the
            # whole column instead of half.
            tx, ty = x, y - r - 6
            anchor = "middle"
            room_px = max(40.0, col_widths[ci] - 10)
            fits = max(8, int(room_px / (FONT_SIZE * 0.56)))
            trimmed = len(node_label) > fits
            text = node_label if not trimmed else node_label[:fits - 1] + "…"
            # A HALO, not a shadow. With this many connectors a bare
            # label is crossed by three of them and reads as struck
            # through - painting the page colour as a stroke UNDER the
            # glyphs cuts a clean gap in every line behind it.
            label_svg = (
                f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="{FONT_SIZE - 1}" '
                f'text-anchor="{anchor}" fill="var(--ink-2)" '
                f'stroke="var(--page)" stroke-width="3.5" paint-order="stroke" '
                f'style="stroke-linejoin:round">{text}'
                # A trimmed label must still be READABLE somewhere. The
                # node circle already has a title; the text did not, so
                # hovering the words themselves said nothing.
                + (f"<title>{node_label}</title>" if trimmed else "")
                + "</text>")
            href = links.get(nid)
            if href:
                # A node that maps to a page is a LINK, so clicking it
                # goes there. Hovering tells you what it is; clicking
                # takes you to the record behind it.
                out.append(f'<a href="{href}">{label_svg}'
                           f"<title>Open {node_label}</title></a>")
            else:
                out.append(label_svg)

    out.append("</g>")
    out.append(
        f'<text x="{width - 10}" y="{height - 10}" font-size="{FONT_SIZE - 1}" '
        # SAY BOTH NUMBERS WHEN THEY DIFFER. Duplicate edges are drawn
        # once and carry their count as weight, so "9 links drawn" beside
        # a headline of "20 links" reads as an error in one of them.
        f'text-anchor="end" fill="var(--muted)">'
        # PLAIN ENGLISH. "4 line(s) drawn, carrying 6 recorded link(s)"
        # is accurate and tells a reader nothing: it never says that a
        # repeated link is drawn once and thickened, which is the only
        # reason the two numbers differ.
        + (f"{drawn} line(s) — repeats are drawn once, so these carry "
           f"{records} recorded links between them"
           if records != drawn else f"{drawn} recorded link(s)")
        + "</text>")
    # WHAT A DOT AND A LINE ACTUALLY MEAN. Node colour carries its
    # column and node size carries how many links it has, and neither
    # was written down anywhere - so a bigger circle looked like
    # emphasis somebody had chosen rather than a fact being reported.
    out.append(
        f'<text x="10" y="{height - 10}" font-size="{FONT_SIZE - 1}" '
        f'text-anchor="start" fill="var(--muted)">'
        "each dot is one thing the bot recorded · a bigger dot has more "
        "links · each arrow is one recorded link, pointing the way the "
        "work flowed</text>")
    out.append("</svg>")
    return "\n".join(out)
