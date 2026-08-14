"""The brain map must be navigable: readable labels, zoom, expand.

OWNER-REPORTED: "hard to navigate all these neural states, maybe it
needs a dynamic zoom or ways to expand but you can only read a few lines
before it cuts off".

Three separate defects behind one complaint:

  1. Labels were trimmed at a FIXED 20 characters regardless of how much
     room the column actually had, and the trimmed text carried no title,
     so the full name was unreachable - the node circle had a tooltip,
     the words did not.
  2. MAX_NODES_PER_LAYER = 14 with no way to raise it. The map said
     "14 shown, 9 more" and offered no route to the nine.
  3. No zoom at any size.

ZOOM IS SERVER-SIDE ON PURPOSE. neural_map is documented as "no physics,
no animation, no JavaScript" and that is what makes it reproducible; a JS
pan/zoom draws a different picture per browser and per session. Each
control is a URL instead, so a useful view can be bookmarked or pasted
into a bug report.
"""

import re

import pytest

from catalyst.dashboard import charts


def _map(zoom=1.0, max_per_layer=14, label="a-fairly-long-node-label-here",
         n_nodes=3):
    layers = [
        ("Sources", [(f"s{i}", label, 3) for i in range(n_nodes)]),
        ("Candidates", [(f"c{i}", label, 2) for i in range(n_nodes)]),
    ]
    edges = [(f"s{i}", f"c{i}", 1, "t") for i in range(n_nodes)]
    return charts.neural_map(layers, edges, chart_id="m", zoom=zoom,
                             max_per_layer=max_per_layer)


class TestZoomActuallyWidensTheDrawing:
    def test_zoomed_carries_an_explicit_pixel_width(self):
        """With width="100%" the browser scales the bigger viewBox
        straight back down to the panel: the drawing got taller and no
        wider, so the zoom did nothing horizontally. Measured in
        Chromium before the fix - 771px at both 1x and 2x."""
        assert 'width="100%"' in _map(zoom=1.0)
        svg = _map(zoom=2.0)
        assert 'width="100%"' not in svg
        width = int(re.search(r'viewBox="0 0 (\d+)', svg).group(1))
        assert width > 1180, width
        assert f'width="{width}"' in svg

    def test_zoom_is_bounded_at_both_ends(self):
        """A pasted URL must not ask for a 100,000px canvas, and must
        not shrink the map below its designed size either."""
        small = int(re.search(r'viewBox="0 0 (\d+)', _map(zoom=0.01)).group(1))
        huge = int(re.search(r'viewBox="0 0 (\d+)', _map(zoom=99999)).group(1))
        assert small == 1180, small
        assert huge <= 1180 * 3, huge


class TestLabelsFitTheirColumn:
    def test_a_trimmed_label_carries_its_FULL_text(self):
        """The node circle had a tooltip; the words did not. So the one
        thing a reader would hover - the label they cannot finish
        reading - said nothing."""
        svg = _map(label="an-extremely-long-node-label-that-cannot-possibly-fit")
        assert "…" in svg, "this label should be trimmed at all"
        titles = re.findall(r"<text[^>]*>[^<]*…<title>([^<]+)</title>", svg)
        assert titles, "a trimmed label has no title - its full text is lost"
        assert all(t == "an-extremely-long-node-label-that-cannot-possibly-fit"
                   for t in titles), titles

    def test_an_untrimmed_label_is_not_given_a_pointless_title(self):
        svg = _map(label="SHORT")
        assert "…" not in svg
        assert "<title>SHORT</title>" not in svg.split("</text>")[0]

    def test_more_room_means_less_trimming(self):
        """The fixed 20-character cut threw away readable text in a wide
        column and still overflowed a narrow one. Trimming must follow
        the space the label actually has."""
        # Long enough that 1x MUST trim it and 3x need not. A 41-char
        # label already fitted at 1x (room is ~44 chars in a two-column
        # map), so the first version of this test compared two identical
        # full renders and read that as a regression.
        label = ("a-node-label-of-quite-considerable-length-indeed-"
                 "long-enough-that-a-narrow-column-cannot-show-it-all")
        narrow = _map(label=label, zoom=1.0)
        wide = _map(label=label, zoom=3.0)

        def shown(svg):
            """Characters of the label actually painted. Counting only
            trimmed labels was wrong: at 3x the label fits ENTIRELY, so
            there is no ellipsis to find and the count came back 0 -
            the test read a total success as a regression."""
            texts = [t for t in re.findall(r">([^<>]*)</text>", svg)
                     if t.rstrip("…") and label.startswith(t.rstrip("…"))]
            return max((len(t) for t in texts), default=0)

        assert shown(wide) > shown(narrow), (
            f"widening the columns did not let more of the label show "
            f"({shown(narrow)} -> {shown(wide)} chars)")
        assert shown(wide) == len(label), (
            "at 3x this label should fit completely")


class TestExpandingThePerLayerCap:
    def test_the_cap_is_honoured(self):
        svg = _map(max_per_layer=2, n_nodes=6)
        assert "2 shown, 4 more" in svg

    def test_raising_it_shows_more(self):
        svg = _map(max_per_layer=6, n_nodes=6)
        assert "more" not in svg.split("Edges")[0] or "6 shown" not in svg
        assert svg.count("<title>") >= 6

    def test_the_default_is_unchanged(self):
        """Raising the cap is opt-in: the default view must not suddenly
        render every node and become the wall it already was."""
        import inspect

        sig = inspect.signature(charts.neural_map)
        assert sig.parameters["max_per_layer"].default == \
            charts.MAX_NODES_PER_LAYER


class TestTheControlsAreOnThePage:
    """brain_view_controls is a pure function of (zoom, nodes), so it is
    tested directly. Driving it through a seeded database tested the
    database's contents as much as the controls, and the shared fixture
    has no graph edges at all - the panel short-circuits to "nothing is
    wired up yet" and renders no controls to check."""

    def test_every_control_is_a_plain_link(self):
        from catalyst.dashboard import panels

        html_out = panels.brain_view_controls("brain", 1.0, 14)
        assert "viewopt" in html_out
        assert "zoom=2" in html_out and "nodes=999" in html_out
        assert "<script" not in html_out.lower()
        assert "onclick" not in html_out.lower()

    def test_the_current_view_is_marked_and_not_a_link(self):
        from catalyst.dashboard import panels

        html_out = panels.brain_view_controls("brain", 2.0, 30)
        assert 'class="viewopt on">2x' in html_out
        assert 'class="viewopt on">30' in html_out
        # the selected one must NOT also be a link to itself
        assert 'href="/brain?zoom=2&amp;nodes=30"' not in html_out

    def test_changing_one_control_KEEPS_the_other(self):
        """A zoom link that reset the node count would make the two
        controls fight each other."""
        from catalyst.dashboard import panels

        html_out = panels.brain_view_controls("brain", 1.0, 60)
        assert "nodes=60" in html_out, (
            "the zoom links dropped the chosen node count")
