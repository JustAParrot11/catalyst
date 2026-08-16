"""Labels must be readable. Measured, not eyeballed.

OWNER-REPORTED, twice, in their own words:

    "you can only read a few lines before it cuts off"
    "it still getting cut off, same with the decisions neural network,
     they dont format well its a bunch of lines with cut off text"

TWO SEPARATE DEFECTS WORE THE SAME SYMPTOM, which is why the first fix
did not settle it.

1. THE NEURAL MAP TRUNCATED. Columns carved a fixed 1180px into equal
   shares, so each label got `col_w / 2 - r - 14` pixels - measured at
   14 characters across five columns and 11 across six. "below_
   conviction_floor" and "J. Restrepo, CFO" were both cut mid-word.
   Columns are now sized to their longest label and the map is as wide
   as it needs to be, with the panel scrolling.

2. THE DECISION SPIDER OVERLAPPED. Nothing was truncated there at all -
   leaf labels wrap - but boxes were painted on top of each other, so
   text was hidden behind other text and read as cut off. Measured on
   an ordinary three-arm decision: FOUR overlapping pairs, two of them
   arm labels sitting over the candidate name, and one box outside the
   viewBox entirely (which simply does not render).

So this file measures GEOMETRY rather than asserting on markup: every
box inside the frame, no two boxes overlapping, and no label trimmed
when there was room for it. A screenshot cannot be asserted on, but the
numbers behind one can.
"""

import re

import pytest

from catalyst.dashboard.charts import decision_spider, neural_map

# A realistic decision: the labels here are the shapes the live system
# actually produces - skip reasons, entity names with roles, and the
# per-stock sizing sentences added with stock_gap.py.
GROUPS = [
    ("What the model saw", [
        ("J. Restrepo, CFO bought 12,000 shares", "$412k"),
        ("EDGAR Form 4 filed 2026-08-12", ""),
        ("alpaca_news: FDA grants priority review", ""),
    ]),
    ("What it concluded", [
        ("long, conviction 0.72", ""),
        ("priced_in: no", ""),
        ("invalidation: readout slips past Q4", ""),
    ]),
    ("What the code did", [
        ("per_stock_stop_width 8% not 50%", ""),
        ("size $200.00", ""),
        ("exit by 2026-09-02", ""),
    ]),
]

LAYERS = [
    ("feeds", [("f:edgar", "edgar_form4", 5), ("f:news", "alpaca_news", 3)]),
    ("events", [("e:1", "J. Restrepo, CFO bought", 4),
                ("e:2", "SEC 8-K strategic alternatives", 2)]),
    ("candidates", [("c:1", "NVDA insider cluster", 3),
                    ("c:2", "REGN clinical readout", 2)]),
    ("view", [("v:long", "long", 4), ("v:no", "no_trade", 6)]),
    ("decision", [("d:s", "skip: priced_in_below_raised_floor", 7),
                  ("d:t", "trade", 1)]),
]
EDGES = [("f:edgar", "e:1", 3, "t"), ("e:1", "c:1", 2, "t"),
         ("c:1", "v:long", 1, "t"), ("v:long", "d:t", 1, "t")]


def _view_box(svg):
    m = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([\d.]+) ([\d.]+)"', svg)
    return tuple(float(g) for g in m.groups())


def _boxes(svg):
    """Every rect except the first, which is the background."""
    found = re.findall(
        r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" height="([\d.]+)"',
        svg)
    return [tuple(float(v) for v in b) for b in found[1:]]


def _overlaps(boxes):
    out = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                    and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]):
                out.append((i, j))
    return out


class TestTheDecisionSpiderIsReadable:
    def test_no_two_boxes_overlap(self):
        """The actual defect. Four pairs overlapped before this."""
        boxes = _boxes(decision_spider("REGN clinical readout", "trade",
                                       GROUPS, chart_id="s"))
        bad = _overlaps(boxes)
        assert not bad, (
            f"{len(bad)} pair(s) of boxes overlap - labels are painted on "
            f"top of each other, which is what reads as cut-off text")

    def test_nothing_is_drawn_outside_the_frame(self):
        """A box past the viewBox is not clipped politely, it simply is
        not rendered."""
        svg = decision_spider("REGN clinical readout", "trade", GROUPS,
                              chart_id="s")
        x, y, w, h = _view_box(svg)
        outside = [b for b in _boxes(svg)
                   if b[0] < x or b[1] < y
                   or b[0] + b[2] > x + w or b[1] + b[3] > y + h]
        assert not outside, f"{len(outside)} box(es) fall outside the viewBox"

    @pytest.mark.parametrize("centre,verdict,groups", [
        ("F", "skip", [("Why", [("below_conviction_floor", "")])]),
        ("SOMEVERYLONGTICKERNAME", "trade",
         [(f"Group number {i}",
           [(f"a very long fact label number {j} here", "")
            for j in range(6)]) for i in range(3)]),
        ("AAPL", "trade",
         [("One", [("x", "")]), ("Two", [("y", "")]), ("Three", [("z", "")])]),
    ])
    def test_it_holds_for_other_shapes_too(self, centre, verdict, groups):
        svg = decision_spider(centre, verdict, groups, chart_id="s")
        x, y, w, h = _view_box(svg)
        boxes = _boxes(svg)
        assert not _overlaps(boxes)
        assert not [b for b in boxes
                    if b[0] < x or b[1] < y
                    or b[0] + b[2] > x + w or b[1] + b[3] > y + h]

    def test_the_arm_labels_do_not_sit_on_the_candidate(self):
        """Two of the four original overlaps were exactly this: the hub
        boxes are drawn last so they sit ON TOP of the candidate name,
        hiding the one thing the whole picture is about."""
        svg = decision_spider("REGN clinical readout", "trade", GROUPS,
                              chart_id="s")
        assert not _overlaps(_boxes(svg))

    def test_it_is_still_deterministic(self):
        """Collision resolution must not make the picture depend on
        anything but its input - two screenshots have to be comparable."""
        a = decision_spider("REGN", "trade", GROUPS, chart_id="s")
        b = decision_spider("REGN", "trade", GROUPS, chart_id="s")
        assert a == b


class TestTheNeuralMapDoesNotTruncate:
    def test_no_label_is_trimmed_when_the_map_can_widen(self):
        svg = neural_map(LAYERS, EDGES, chart_id="m")
        trimmed = re.findall(r">([^<>]*…)</text>", svg)
        assert not trimmed, (
            f"labels still cut off: {trimmed} - the column should have "
            f"widened to fit them")

    def test_the_longest_label_survives_intact(self):
        """The specific string the owner would have seen cut."""
        svg = neural_map(LAYERS, EDGES, chart_id="m")
        assert "skip: priced_in_below_raised_floor" in svg

    def test_the_map_widens_rather_than_cutting(self):
        """A narrow map with long labels must grow, not trim."""
        long_layers = [
            (f"layer{i}", [(f"n{i}", "a genuinely long node label here", 1)])
            for i in range(5)]
        svg = neural_map(long_layers, [], chart_id="m")
        width = _view_box(svg)[2]
        assert width > 1180, (
            f"map stayed at {width}px with labels that cannot fit in it")

    def test_it_does_not_widen_without_limit(self):
        """Scrolling for a minute to reach the last column is its own
        kind of unreadable."""
        from catalyst.dashboard.charts import MAX_MAP_WIDTH

        huge = [(f"l{i}", [(f"n{i}", "x" * 200, 1)]) for i in range(6)]
        assert _view_box(neural_map(huge, [], chart_id="m"))[2] <= MAX_MAP_WIDTH

    def test_a_label_with_no_room_left_is_trimmed_with_an_ellipsis(self):
        """At the width cap, trimming is the honest last resort - but it
        must be a deliberate ellipsis, never a clip at a random pixel,
        and the full text stays in the node's title."""
        huge = [(f"l{i}", [(f"n{i}", "y" * 300, 1)]) for i in range(6)]
        svg = neural_map(huge, [], chart_id="m")
        assert "…" in svg
        assert "y" * 300 in svg, "the full label is no longer recoverable"

    def test_it_is_still_deterministic(self):
        assert (neural_map(LAYERS, EDGES, chart_id="m")
                == neural_map(LAYERS, EDGES, chart_id="m"))
