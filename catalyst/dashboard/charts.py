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
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fbfbfd" stroke="#d7d7e0"/>',
    ]

    out.append(
        f'<text x="{m_left:.1f}" y="16" font-size="{FONT_SIZE}" text-anchor="start" '
        f'fill="#333">{y_axis_title}</text>'
    )

    for value, label in zip(ticks, tick_labels):
        y = py(value)
        out.append(
            f'<line x1="{m_left:.1f}" y1="{y:.1f}" x2="{m_left + plot_w:.1f}" y2="{y:.1f}" '
            f'stroke="#e4e4ee"/>'
        )
        out.append(
            f'<text x="{m_left - 6:.1f}" y="{y + 4:.1f}" font-size="{FONT_SIZE}" '
            f'text-anchor="end" fill="#555">{label}</text>'
        )

    # The 100 line: "no change" is where a reader's eye goes first.
    if y_lo <= 100.0 <= y_hi:
        y100 = py(100.0)
        out.append(
            f'<line x1="{m_left:.1f}" y1="{y100:.1f}" x2="{m_left + plot_w:.1f}" '
            f'y2="{y100:.1f}" stroke="#999" stroke-dasharray="4 3"/>'
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
            f'y2="{m_top + plot_h + 4:.1f}" stroke="#999"/>'
        )
        out.append(
            f'<text x="{cx:.1f}" y="{m_top + plot_h + 17:.1f}" font-size="{FONT_SIZE}" '
            f'text-anchor="{anchor}" fill="#555">{label}</text>'
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
                f'text-anchor="start" fill="#333">{s.label}</text>'
            )
            lx += w
        ly += 16

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
