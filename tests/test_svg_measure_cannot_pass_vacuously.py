"""The chart measurement must prove it read something.

WHY THIS FILE EXISTS. `charts.text_boxes` matches only text that carries
an INLINE `font-size="N"` attribute. The position chart and the P&L chart
both style their labels with CSS classes, so for those charts it returns
an EMPTY LIST - and `labels_outside_viewbox` therefore returns "nothing
wrong" for a chart whose labels are off the page entirely.

That is the vacuous pass section 25 records costing an upgrade: an
assertion whose only failure mode is a crash. `tests/svg_measure.py`
exists to close it, and this file is what stops the closer from having
the same hole - without these tests, every geometry assertion in
`test_the_pnl_graph_is_live_and_marked.py` could be satisfied by a
measurement that read no files and saw no text.
"""

import pytest

import svg_measure as M


CSS = """
.pos-label { fill: var(--muted); font-size: 10px; }
.pnl-val { fill: var(--ink); font-size: 10.5px; font-weight: 600; }
.pnl-event-label { fill: var(--muted); font-size: 9.5px; }
.no-size { fill: red; }
"""

SVG = ('<svg viewBox="0 0 200 100">'
       '<text x="10" y="20" class="pnl-val">hello</text>'
       '<text x="10" y="40" class="pnl-event-label">tiny</text>'
       '</svg>')


class TestItProvesItReadSomething:
    def test_an_svg_with_no_text_RAISES_rather_than_reporting_clean(self):
        """The whole point. An empty result must be impossible to
        confuse with a clean one."""
        with pytest.raises(AssertionError, match="no <text> elements"):
            M.text_boxes('<svg viewBox="0 0 10 10"><line x1="0"/></svg>')

    def test_outside_viewbox_inherits_that_refusal(self):
        with pytest.raises(AssertionError):
            M.outside_viewbox('<svg viewBox="0 0 10 10"></svg>')

    def test_overlaps_inherits_that_refusal(self):
        with pytest.raises(AssertionError):
            M.overlaps('<svg viewBox="0 0 10 10"></svg>')

    def test_a_missing_viewbox_raises_rather_than_defaulting(self):
        with pytest.raises(AssertionError, match="viewBox"):
            M.viewbox("<svg></svg>")


class TestItResolvesTheSizeFromTheCSS:
    def test_it_finds_the_sizes_the_charts_actually_use(self):
        sizes = M.class_font_sizes(CSS)
        assert sizes["pos-label"] == 10.0
        assert sizes["pnl-val"] == 10.5
        assert sizes["pnl-event-label"] == 9.5
        assert "no-size" not in sizes, "a rule with no font-size was invented"

    def test_the_REAL_css_carries_the_pnl_classes(self):
        """Reads the shipped stylesheet, so a renamed class fails here
        rather than silently measuring everything at the default size."""
        sizes = M.class_font_sizes()
        for cls in ("pnl-val", "pnl-event-label", "pos-label"):
            assert cls in sizes, f".{cls} has no font-size in render.py"

    def test_a_bigger_class_measures_a_wider_box(self):
        """The property that matters: the size actually changes the
        geometry. If the class were ignored, both boxes would be equal
        and every overlap check would be measuring the wrong thing."""
        wide = ('<svg viewBox="0 0 400 100">'
                '<text x="10" y="20" class="pnl-val">abcdefgh</text></svg>')
        narrow = ('<svg viewBox="0 0 400 100">'
                  '<text x="10" y="20" class="pnl-event-label">abcdefgh'
                  '</text></svg>')
        w = M.text_boxes(wide, sizes=M.class_font_sizes(CSS))[0]
        n = M.text_boxes(narrow, sizes=M.class_font_sizes(CSS))[0]
        assert (w[2] - w[0]) > (n[2] - n[0]), (w, n)

    def test_an_inline_font_size_still_wins_where_one_is_given(self):
        svg = ('<svg viewBox="0 0 400 100">'
               '<text x="10" y="20" font-size="30" text-anchor="start">'
               'abc</text></svg>')
        box = M.text_boxes(svg)[0]
        assert (box[2] - box[0]) > 3 * M.CHAR_W, box


class TestItMeasuresWhatTheStockToolCannot:
    def test_charts_text_boxes_sees_nothing_here_and_this_module_does(self):
        """The exact gap, asserted so nobody closes it by deleting this
        module and going back to the other one."""
        from catalyst.dashboard import charts

        assert charts.text_boxes(SVG) == [], (
            "charts.text_boxes now reads class-styled text - if that is "
            "deliberate, this module can go")
        assert len(M.text_boxes(SVG, sizes=M.class_font_sizes(CSS))) == 2


class TestTheGeometryChecksActuallyBite:
    def test_it_catches_a_label_off_the_right_edge(self):
        svg = ('<svg viewBox="0 0 40 100">'
               '<text x="38" y="20" class="pnl-val">a long label</text>'
               '</svg>')
        assert M.outside_viewbox(svg), "an overflowing label was not caught"

    def test_it_catches_a_label_off_the_top(self):
        """Section 18's own defect: a stack slid up through the top."""
        svg = ('<svg viewBox="0 0 400 100">'
               '<text x="50" y="2" class="pnl-val">up and out</text></svg>')
        assert M.outside_viewbox(svg)

    def test_it_catches_two_labels_printed_over_each_other(self):
        svg = ('<svg viewBox="0 0 400 100">'
               '<text x="50" y="20" class="pnl-val">held</text>'
               '<text x="52" y="21" class="pnl-val">held</text></svg>')
        assert M.overlaps(svg), "overprinted labels were not caught"

    def test_it_passes_two_labels_that_are_genuinely_apart(self):
        svg = ('<svg viewBox="0 0 400 100">'
               '<text x="20" y="20" class="pnl-val">one</text>'
               '<text x="300" y="80" class="pnl-val">two</text></svg>')
        assert M.overlaps(svg) == []
        assert M.outside_viewbox(svg) == []
