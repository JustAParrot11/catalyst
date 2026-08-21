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

from catalyst.dashboard.charts import decision_spider, mindmap, neural_map

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
        """A map whose labels cannot fit must grow, not trim.

        THE LABELS HERE GOT LONGER when the label moved from beside its
        node to above it. Beside it, a label had half a column and the
        column had to be twice its width; above it, the label has the
        whole column and needs only its own width plus padding - so the
        old fixture (32 characters, five columns) now genuinely FITS in
        1180px and widening it would be padding for its own sake.

        The invariant is unchanged and is asserted on both counts: when
        the labels really are too long the map widens, AND nothing is
        cut either way.
        """
        long_layers = [
            (f"layer{i}", [(f"n{i}", "a genuinely long node label here", 1)])
            for i in range(5)]
        svg = neural_map(long_layers, [], chart_id="m")
        assert not re.findall(r">([^<>]*…)</text>", svg), (
            "labels cut off in a map that had room for them")

        # Now past what 1180px can hold, whatever the geometry.
        longer = [
            (f"layer{i}",
             [(f"n{i}", "a considerably longer node label than that one", 1)])
            for i in range(6)]
        svg = neural_map(longer, [], chart_id="m")
        width = _view_box(svg)[2]
        assert width > 1180, (
            f"map stayed at {width}px with labels that cannot fit in it")
        assert not re.findall(r">([^<>]*…)</text>", svg), (
            "the map widened and still cut its labels")

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


class TestTheMapsCanActuallyBeUNDERSTOOD:
    """OWNER-ASKED 2026-08-21: "if you visually look at the nerual
    networks were generating, are they really easy to understand? or can
    you review how they are presented".

    Rendered and looked at, the honest answer was no, for four reasons
    that had nothing to do with the data being wrong.
    """

    def test_a_label_never_lies_along_its_own_connector(self):
        """THE ONE THAT MADE THEM UNREADABLE. Labels sat beside their
        node on a side chosen by which half of the map the column was
        in - which put every label on top of a connector: a node in the
        left half was labelled to its RIGHT, exactly where its edges
        leave. Measured: EMBC at x=295 with its label running right from
        309, straight along its own outgoing curve. It read as struck
        through.

        Above the node there is no horizontal connector, in any column.
        """
        svg = neural_map(LAYERS, EDGES, chart_id="m")
        centres = {m.group(1): (float(m.group(2)), float(m.group(3)))
                   for m in re.finditer(
                       r'data-node="([^"]+)"[^>]*>\s*<circle cx="([\d.]+)" '
                       r'cy="([\d.]+)"', svg)}
        assert centres, "no nodes drawn"
        labels = [(float(m.group(1)), float(m.group(2)), m.group(3))
                  for m in re.finditer(
                      r'<text x="([\d.]+)" y="([\d.]+)" font-size="\d+" '
                      r'text-anchor="(\w+)"', svg)]
        found = 0
        for nid, (nx, ny) in centres.items():
            # A node's own label: the one sitting in its column, within
            # a row of it. Matched by position rather than by reading
            # the text, so the assertion is about geometry.
            near = [(x, y, a) for x, y, a in labels
                    if abs(x - nx) < 2 and 0 < ny - y < 30]
            assert near, f"{nid} has no label above it"
            for _x, y, anchor in near:
                assert y < ny, (
                    f"{nid}'s label is at the node's own height, where "
                    "every connector runs")
                assert anchor == "middle", (
                    f"{nid}'s label is anchored to one side, which is how "
                    "it ends up lying along the edges leaving that side")
                found += 1
        assert found, "no node labels drawn"

    def test_an_empty_stage_is_a_strip_not_a_third_of_the_canvas(self):
        """Three of the brain's six stages are routinely empty. At the
        same width as a full one they took 38% of the drawing to say
        nothing, and the content that existed was squeezed into what
        was left - which reads as a chart that failed to load."""
        layers = [("Sources", []),
                  ("Candidates", [("c", "EMBC", 2)]),
                  ("What it linked", []),
                  ("Model view", [("v", "long", 2)])]
        svg = neural_map(layers, [("c", "v", 1, "t")], chart_id="m")
        centres = [float(m.group(1)) for m in re.finditer(
            r'<text x="([\d.]+)" y="30"', svg)]
        assert len(centres) == 4
        # Recover each column's width from the centres: the first is
        # twice its own centre, and every later one follows from the
        # gap. Comparing neighbouring GAPS cannot work - each gap is the
        # mean of two adjacent widths, so an alternating empty/full
        # layout gives identical gaps throughout.
        widths = [2 * centres[0]]
        for i in range(1, len(centres)):
            widths.append(2 * (centres[i] - centres[i - 1]) - widths[-1])
        empty = [widths[0], widths[2]]
        full = [widths[1], widths[3]]
        assert max(empty) < min(full), (
            f"an empty stage ({empty}) is as wide as a full one ({full})")

    def test_an_empty_stage_still_says_so_in_words(self):
        """It must not vanish either - a stage that recorded nothing is
        a fact. And a bare "0" is the unexplained zero this project
        keeps banning."""
        svg = neural_map([("Sources", []), ("Candidates", [("c", "X", 1)])],
                         [], chart_id="m")
        assert "SOURCES" in svg
        assert "nothing yet" in svg

    def test_every_edge_shows_which_way_it_runs(self):
        """Left to right was stated in the prose above the chart and
        nowhere in the chart. A node-link diagram with undirected lines
        reads as "these are related", not "this became that" - which is
        the entire content of this drawing."""
        svg = neural_map(LAYERS, EDGES, chart_id="m")
        assert "<marker" in svg, "no arrowhead is defined"
        assert svg.count("marker-end=") >= 1

    def test_the_arrowhead_is_not_hidden_under_the_node(self):
        """Nodes are painted last, on purpose. An edge that ends at the
        target's centre has its arrowhead covered by the target - which
        is how the first attempt shipped with markers declared,
        attached, and invisible in every rendering."""
        layers = [("A", [("a", "a", 1)]), ("B", [("b", "b", 1)])]
        svg = neural_map(layers, [("a", "b", 1, "t")], chart_id="m")
        bx = float(re.search(
            r'data-node="b"[^>]*>\s*<circle cx="([\d.]+)"', svg).group(1))
        end_x = float(re.search(
            r'<path class="edge" d="M[\d.]+,[\d.]+ C[^"]* ([\d.]+),[\d.]+"',
            svg).group(1))
        assert end_x < bx - 4, (
            f"the edge ends at {end_x} against a node centred at {bx}, so "
            "the arrowhead is under the node")

    def test_what_a_dot_and_a_line_MEAN_is_written_down(self):
        """Node colour carries its column and node size carries its link
        count, and neither was stated anywhere - so a bigger circle
        looked like emphasis somebody chose rather than a fact."""
        svg = neural_map(LAYERS, EDGES, chart_id="m")
        assert "each dot is one thing the bot recorded" in svg
        assert "bigger dot has more links" in svg


class TestEvidenceIsNotDressedUpAsSomethingItIsNot:

    def test_a_filed_document_is_never_drawn_as_a_model_guess(self):
        """THE DISHONEST ONE. The line-style table keyed on "primary",
        "secondary" and "inferred", but schema_graph.sql stores
        `primary_document`, `official_schedule`, `secondary_report` and
        `model_inference` - so NOTHING matched, every edge fell to the
        default, and the default was the dotted style meaning "Claude
        inferred this". An SEC filing was drawn as speculation.

        House rule 7: classify by the rule, not by a list.
        """
        from catalyst.dashboard.charts import reliability_dash

        assert reliability_dash("primary_document") == ""
        assert reliability_dash("official_schedule") == ""
        assert reliability_dash("secondary_report") == "5 3"
        assert reliability_dash("model_inference") == "2 4"

    def test_an_unknown_reliability_under_claims_rather_than_over_claims(self):
        """The default has to point at the weakest style. Drawing an
        unrecognised value as a filed document would dress a guess up as
        evidence, which is worse than the bug this replaces."""
        from catalyst.dashboard.charts import reliability_dash

        for unknown in ("", None, "something_nobody_added_yet", "vibes"):
            assert reliability_dash(unknown) == "2 4", unknown

    def test_the_mindmap_says_what_its_line_styles_mean(self):
        """It was explained in the paragraph under the chart, which is
        not where anyone looks while reading the chart - and the
        distinction is the most important one on it."""
        svg = mindmap("EMBC", [("mentions", "A person", "person",
                                "primary_document", "edgar")],
                      chart_id="m")
        assert "filed with a regulator" in svg
        assert "Claude inferred it" in svg
