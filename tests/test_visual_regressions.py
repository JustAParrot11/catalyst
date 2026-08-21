"""Visual defects found by RENDERING the dashboard and measuring it.

OWNER-ASKED 2026-08-21: "do one final ultimate stress test, test how it
looks visually find any potential visual bugs".

Every case here was found by opening all nineteen pages in a real
browser at two widths and both colour schemes and asking the layout
engine what it had actually drawn - not by reading the code. That
distinction is the point: none of these are visible in the HTML, and
the suite was fully green while all of them were on the page.

WHAT WAS ACTUALLY WRONG:

1. The page scrolled sideways on a phone, because a <select> is sized
   by its longest option and no grid column can talk it out of that.
2. "52w low" was anchored end-at-36 while being 41px wide, so it was
   drawn at x=-5 and the browser clipped it - on every trade.
3. The floating "N% up the range" label rode the marker, so at 0% and
   100% it hung off the ends.
4. Two bar charts rendered as unlabelled rectangles. A bar chart nobody
   can attach a ticker to is decoration.
5. The cost chart's Y axis read "$1.0005 / $0.7504 / $0.5002".
6. The equity bridge laid its API-spend segment out one pixel PAST the
   bar and clipped it - the line showing real money leaving the account
   was invisible on two pages.
7. Three form fields were narrower than their own placeholders, so the
   reader got "who is making this chang".
8. "pp" survived on the status strip, which is on EVERY page, months
   after the owner asked for a percent sign.

These are pinned as UNIT tests rather than screenshots: a screenshot
test fails on a font update and teaches nothing, whereas each of these
has a rule underneath it that can be stated and checked.
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.dashboard import charts, panels, render

CSS = render.STYLE if hasattr(render, "STYLE") else ""


def _css() -> str:
    """The stylesheet as shipped, however render.py happens to hold it."""
    src = Path(render.__file__).read_text()
    blocks = re.findall(r'"""(.*?)"""', src, re.S)
    return "\n".join(b for b in blocks if "{" in b and ":" in b)


class TestNothingIsNarrowerThanItsOwnWords:

    def test_every_placeholder_has_a_field_wide_enough(self):
        """A field narrower than its placeholder shows half a sentence.
        `size` is the attribute for this, and it is checked against the
        placeholder it has to hold - measured, not eyeballed."""
        src = Path(panels.__file__).read_text()
        # The three the browser measured as too small, now each sized.
        for placeholder, need in (("who is making this change", 26),
                                  ("message, traceback or context", 32),
                                  ("moving to the $2,000 account", 30)):
            assert placeholder in src, f"{placeholder!r} no longer rendered"
            window = src[max(0, src.find(placeholder) - 400):
                         src.find(placeholder) + 200]
            m = re.search(r'size="(\d+)"', window)
            assert m, f"no size= near placeholder {placeholder!r}"
            assert int(m.group(1)) >= len(placeholder) - 2, (
                f"{placeholder!r} is {len(placeholder)} chars in a "
                f"size={m.group(1)} field")
            assert need >= len(placeholder) - 2

    def test_a_field_that_wants_to_stay_small_still_can(self):
        """The blanket min-width that would have fixed the above would
        also have stretched the log's four-character limit box. It is
        sized in the markup, so it keeps its size."""
        src = Path(panels.__file__).read_text()
        assert 'size="4"' in src
        css = _css()
        assert "min-width: 31ch" not in css
        assert "min-width: 27ch" not in css


class TestChartsStayInsideTheirOwnBox:

    @pytest.mark.parametrize("pct", [0.0, 0.4, 50.0, 99.6, 100.0])
    def test_the_range_bar_never_draws_outside_its_viewBox(self, pct):
        """Both ends, because both ends were broken: the left label was
        clipped always, and the floating label hung off at 0 and 100."""
        class Pa:
            range_position_pct = pct

        class St:
            ticker = "EMBC"
            entry_price = "5.06"

        svg = panels._range_bar(St, Pa, "p", 0)
        vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        assert vb, "the range bar lost its viewBox"
        w = int(vb.group(1))
        # Every x the chart names must sit inside the box, with room for
        # the words that hang off an anchor.
        for x in (float(v) for v in re.findall(r'[ c]x="([\d.]+)"', svg)):
            assert 0 <= x <= w, f"x={x} outside 0..{w} at pct={pct}"
        label_x = float(re.search(r'<text x="([\d.]+)" y="12"', svg).group(1))
        assert 50 <= label_x <= w - 50, (
            f"the floating label at {label_x} has no room to be centred")

    def test_the_marker_still_sits_at_the_true_position(self):
        """The label is clamped; the MARKER must not be. Clamping the
        thing that carries the meaning would be a lie for tidiness."""
        class St:
            ticker = "X"
            entry_price = None

        seen = []
        for pct in (0.0, 100.0):
            class Pa:
                range_position_pct = pct

            svg = panels._range_bar(St, Pa, "p", 0)
            seen.append(float(re.search(r'<circle cx="([\d.]+)"', svg).group(1)))
        assert seen[0] < seen[1], "the marker did not move between 0% and 100%"


class TestBarsCarryTheirNames:

    def test_a_few_bars_are_labelled(self):
        html = panels._bar_row([("EMBC", 12.0), ("NVAX", -4.0),
                                ("ARDX", 31.0)], "s")
        for ticker in ("EMBC", "NVAX", "ARDX"):
            assert f">{ticker}</text>" in html, f"{ticker} has no label"

    def test_many_bars_are_not_labelled(self):
        """Thirty dates in 300px is 10px a column. Overlapping labels
        are worse than none, so the rule is the width available."""
        many = [(f"08-{d:02d}", float(d)) for d in range(1, 31)]
        html = panels._bar_row(many, "s")
        assert "minibar-label" not in html

    def test_the_rule_is_the_step_width_not_a_list_of_charts(self):
        """House rule 7. Same count, different width, different answer -
        which is only true if the decision reads the geometry."""
        pairs = [(f"t{i}", float(i)) for i in range(8)]
        assert "minibar-label" not in panels._bar_row(pairs, "s", width=160)
        assert "minibar-label" in panels._bar_row(pairs, "s", width=900)

    def test_labels_do_not_eat_the_zero_line(self):
        """The label band is carved out of the height, so a negative bar
        still has somewhere to go."""
        html = panels._bar_row([("a", 10.0), ("b", -10.0)], "s")
        assert "minibar-pos" in html and "minibar-neg" in html
        assert "minibar-zero" in html


class TestMoneyIsWrittenTheWayMoneyIsWritten:

    def test_ordinary_amounts_get_two_places(self):
        fmt = panels._money_fmt([0.86, 0.31, 0.75])
        assert fmt(1.0) == "$1.00"
        assert fmt(0.25) == "$0.25"

    def test_sub_cent_days_keep_their_precision(self):
        """The original reason for four decimals, which still holds: a
        day can genuinely cost less than a cent, and "$0.00" on every
        bar would read as a broken feed."""
        fmt = panels._money_fmt([0.0004, 0.0009])
        assert fmt(0.0004) == "$0.0004"

    def test_bad_values_do_not_raise(self):
        fmt = panels._money_fmt([None, "", "n/a"])
        assert fmt(1.5) == "$1.50"


class TestTheEquityBridgeShowsItsSegments:

    def test_the_baseline_is_allowed_to_shrink(self):
        """It is emitted at width:100%. While every segment was
        flex:0 0 auto the baseline filled the bar and the trading and
        API segments were laid out past its right edge, then clipped -
        so the line showing real money leaving the account was drawn at
        x=639 in a 638px bar and nobody ever saw it."""
        css = _css()
        m = re.search(r"\.bridge-start\s*\{([^}]*)\}", css, re.S)
        assert m, ".bridge-start lost its rule"
        assert re.search(r"flex:\s*1\s+1", m.group(1)), (
            "the baseline cannot shrink, so the other segments are clipped")

    def test_the_other_segments_keep_their_exact_width(self):
        css = _css()
        for cls in ("bridge-pnl", "bridge-api"):
            m = re.search(rf"\.{cls}\s*\{{([^}}]*)\}}", css, re.S)
            assert m and "flex: 0 0 auto" in m.group(1), (
                f".{cls} must not be resized - its width IS the figure")


class TestNoPageScrollsSideways:

    def test_form_controls_cannot_widen_the_page(self):
        """A <select> is sized by its longest option, which is intrinsic
        content. Measured at 390px: the diagnostic bundle's menu pushed
        the document to 401px."""
        css = _css()
        m = re.search(r"input,\s*select,\s*button\s*\{([^}]*)\}", css, re.S)
        assert m, "the shared control rule is gone"
        assert "max-width: 100%" in m.group(1)

    def test_the_two_column_bundle_row_stacks_on_a_phone(self):
        css = _css()
        narrow = css[css.find("@media (max-width: 760px)"):]
        assert ".bundlerow" in narrow
        assert "grid-template-columns: 1fr" in narrow


class TestTheReferenceLabelStaysReadable:

    def test_it_gets_a_plate_so_it_does_not_read_through_the_bars(self):
        """MEASURED FROM THE RENDERED SVG, not from the source. The
        first version of this test only looked for the string "<rect"
        in the function and passed happily while a sabotage run set the
        plate to zero width and fill:none - which is exactly the defect
        it was written to catch. A test that cannot fail is not a test
        (house rule 4)."""
        svg = charts.bar_chart(
            [("08-01", 0.9, "t"), ("08-02", 0.8, "t")],
            chart_id="c", title="t",
            reference=(0.17, "cap, pro-rata per day"))
        line_y = float(re.search(
            r'<line x1="[\d.]+" y1="([\d.]+)"[^>]*stroke-dasharray', svg
        ).group(1))
        plates = [m for m in re.finditer(
            r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" '
            r'height="(\d+)" fill="([^"]+)"', svg)
            if abs(float(m.group(2)) - line_y) < 24]
        assert plates, "no plate sits behind the reference label"
        plate = plates[-1]
        assert float(plate.group(3)) > 60, (
            f"the plate is {plate.group(3)}px wide - too narrow to cover "
            '"cap, pro-rata per day"')
        assert plate.group(5) not in ("none", "transparent"), (
            f"the plate is filled {plate.group(5)!r}, so it hides nothing")
