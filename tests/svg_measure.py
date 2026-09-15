"""Measure SVG text that takes its size from a CSS CLASS, not an attribute.

WHY THIS EXISTS. `charts.text_boxes` requires an inline
`font-size="N"` attribute:

    r'<text\\s+x="..."\\s+y="..."\\s+font-size="(\\d+)"\\s+...'

Every chart built with CSS classes - the position chart, and the P&L
chart added 2026-09-14 - therefore measures as **zero text elements**,
and `labels_outside_viewbox` returns an empty list for a chart whose
labels are off the page entirely. That is the vacuous pass this project
has now paid for twice (WHAT-WE-TRIED sections 23 and 25): a check that
reads nothing reports the same "all clear" as a check that read
everything and found nothing wrong.

THE FIX IS NOT TO DUPLICATE THE FONT SIZE INTO THE SVG. Writing
`font-size="10"` beside `class="pos-label"` creates two numbers meaning
the same thing, which section 18 records going wrong in exactly this
file's charts (a 13px gap against a 14.3px measured box). The CSS stays
the single source of truth and this module READS it.
"""

import re
import pathlib

CSS = pathlib.Path(__file__).resolve().parent.parent / (
    "catalyst/dashboard/render.py")

#: Same per-character width and line height `charts.py` measures with,
#: imported rather than restated so the two cannot disagree.
from catalyst.dashboard.charts import CHAR_W, FONT_SIZE, LINE_H  # noqa: E402

_RULE = re.compile(r"\.([a-zA-Z][\w-]*)\s*\{([^}]*)\}", re.S)
_SIZE = re.compile(r"font-size:\s*([\d.]+)px")
_TEXT = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.S)
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def class_font_sizes(css_text: str | None = None) -> dict:
    """{css class: font-size in px} for every rule that sets one."""
    text = css_text if css_text is not None else CSS.read_text()
    out = {}
    for name, body in _RULE.findall(text):
        found = _SIZE.search(body)
        if found:
            out[name] = float(found.group(1))
    return out


def text_boxes(svg: str, sizes: dict | None = None, default: float = FONT_SIZE):
    """Boxes for EVERY `<text>`, whether it sizes itself by attribute or
    by class. Same geometry as `charts.text_boxes`.

    Raises when the SVG contains no text at all, because an empty result
    is exactly what a broken measurement looks like and this module
    exists because that was once indistinguishable from a clean one.
    """
    sizes = class_font_sizes() if sizes is None else sizes
    boxes = []
    for attrs, content in _TEXT.findall(svg):
        a = dict(_ATTR.findall(attrs))
        try:
            x, y = float(a.get("x", "nan")), float(a.get("y", "nan"))
        except ValueError:
            continue
        if x != x or y != y:
            continue
        size = float(a["font-size"]) if a.get("font-size") else default
        for cls in str(a.get("class", "")).split():
            if cls in sizes:
                size = sizes[cls]
                break
        body = re.sub(r"<[^>]+>", "", content).strip()
        w = len(body) * CHAR_W * (size / FONT_SIZE)
        anchor = a.get("text-anchor", "start")
        x0 = x - w if anchor == "end" else x - w / 2 if anchor == "middle" else x
        boxes.append((x0, y - size, x0 + w, y + size * (LINE_H - 1.0), body))
    if not boxes:
        raise AssertionError(
            "no <text> elements were measured in this SVG, so any "
            "assertion about their positions would pass vacuously")
    return boxes


def viewbox(svg: str):
    m = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', svg)
    if not m:
        raise AssertionError("this SVG has no viewBox to measure against")
    return tuple(float(g) for g in m.groups())


def outside_viewbox(svg: str) -> list:
    """Labels that fall outside the drawable area, with their boxes."""
    vx, vy, vw, vh = viewbox(svg)
    bad = []
    for x0, y0, x1, y1, body in text_boxes(svg):
        if x0 < vx or y0 < vy or x1 > vx + vw or y1 > vy + vh:
            bad.append(f"{body!r} box=({x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f})")
    return bad


def overlaps(svg: str, pad: float = 2.0) -> list:
    """Pairs of labels whose boxes collide - the "picket fence" check."""
    boxes = text_boxes(svg)
    bad = []
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if (a[0] - pad < b[2] and b[0] - pad < a[2]
                    and a[1] - pad < b[3] and b[1] - pad < a[3]):
                bad.append((a[4], b[4]))
    return bad
