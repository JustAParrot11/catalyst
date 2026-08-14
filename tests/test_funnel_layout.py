"""The funnel's drop reasons must not be squeezed into the count column.

OWNER-REPORTED, with a screenshot: the provenance text under each drop
reason rendered one word per line down a column about thirty pixels
wide - "last / seen / yesterday / (2026- / 08- / 13," - while the rest
of the page had space to spare.

THE CAUSE IS A CSS GRID COUNTING ITS CHILDREN, and it is worth naming
because the markup looks completely innocent:

    .funnel-why li { display: grid; grid-template-columns: 2.4em 1fr; }

    <li><span class="funnel-why-n">125</span> research skipped: budget
        <span class="prov">last seen yesterday...</span></li>

That <li> has THREE grid items, not two - CSS wraps the bare reason text
in an anonymous grid item. So the count takes column 1, the reason takes
column 2, and `.prov` wraps onto a second row where it lands back in
column 1: the 2.4em one.

The fix is structural rather than another CSS rule: the reason and its
provenance go inside ONE element, so the grid gets exactly the two
children it was written for. A `grid-column: 2` override would have
fixed this instance while leaving the next added element to fall into
the same 2.4em trap.

So this test asserts the STRUCTURE, not the stylesheet: every row of
every reason list has exactly two element children and no loose text.
"""

import pytest
from html.parser import HTMLParser

from catalyst.dashboard import panels
from catalyst.dashboard.db import Db
from tests.test_dashboard import _iso, bare, seeded  # noqa: F401 - fixtures


class _Rows(HTMLParser):
    """Collects the children of each <li> inside a reason list."""

    def __init__(self):
        super().__init__()
        self.depth_in_list = 0
        self.li_depth = None
        self.current = None
        self.rows = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "")
        self._stack.append(tag)
        if tag == "div" and ("funnel-why" in classes or "funnel-fault" in classes):
            self.depth_in_list += 1
        if tag == "li" and self.depth_in_list:
            self.li_depth = len(self._stack)
            self.current = {"children": 0, "text": ""}
        elif self.current is not None and len(self._stack) == self.li_depth + 1:
            self.current["children"] += 1

    def handle_endtag(self, tag):
        if tag == "li" and self.current is not None:
            self.rows.append(self.current)
            self.current = None
            self.li_depth = None
        if tag == "div" and self.depth_in_list:
            self.depth_in_list -= 1
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data):
        # Text directly inside the <li> - the anonymous grid item.
        if self.current is not None and len(self._stack) == self.li_depth:
            self.current["text"] += data


def _rows_of(db_path):
    parser = _Rows()
    parser.feed(panels.funnel_panel(Db(db_path)))
    return parser.rows


@pytest.fixture
def with_drops(bare):  # noqa: F811
    """A funnel carrying real DROP reasons, which is what the owner
    screenshotted ("125 research skipped: budget_denied").

    The shared `seeded` fixture produces only FAULT rows, so a version of
    this file that used it alone passed while the drop list still had the
    bug - the sabotage proved it. Both list shapes are exercised now.
    """
    import sqlite3
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date()
    conn = sqlite3.connect(bare)
    for i in range(3):
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"d{i}", "ZZ", "x", today.isoformat(), "estimated",
                      "[]", _iso(today), "s", "[]"))
        conn.execute(
            "INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
            (f"rc{i}", f"d{i}", "m", "p", "[]", "0", 1,
             "budget_denied", _iso(today)))
    conn.commit()
    conn.close()
    return bare


class TestEveryReasonRowIsTwoGridItems:
    @pytest.mark.parametrize("fixture", ["seeded", "with_drops"])
    def test_no_row_has_loose_text_beside_its_count(self, fixture, request):
        seeded = request.getfixturevalue(fixture)
        """The anonymous grid item is the whole bug. A bare text node
        inside the <li> becomes a third grid child and pushes everything
        after it into the 2.4em count column."""
        rows = _rows_of(seeded)
        assert rows, "the seed produced no drop-reason rows to check"
        loose = [r for r in rows if r["text"].strip()]
        assert not loose, (
            f"{len(loose)} reason row(s) carry bare text inside the <li>: "
            f"{loose[0]['text'].strip()[:80]!r}. CSS wraps that in an "
            "anonymous grid item, so anything after it wraps into the "
            "2.4em count column and renders one word per line.")

    @pytest.mark.parametrize("fixture", ["seeded", "with_drops"])
    def test_every_row_has_exactly_two_element_children(self, fixture, request):
        seeded = request.getfixturevalue(fixture)
        rows = _rows_of(seeded)
        wrong = [r for r in rows if r["children"] != 2]
        assert not wrong, (
            f"{len(wrong)} row(s) have {wrong[0]['children']} element "
            "children; the grid is declared with two columns "
            "(2.4em 1fr), so a third child lands back in the narrow one")


class TestTheTextStillReachesTheReader:
    def test_the_reason_and_its_provenance_are_both_present(self, seeded):
        """Restructuring must not drop content - the machine-readable
        code beside the English is what makes a drop reason greppable."""
        html_out = panels.funnel_panel(Db(seeded))
        assert "funnel-why-n" in html_out
        assert "funnel-why-text" in html_out, (
            "the reason and its provenance should share one wrapper so "
            "the grid sees exactly two children")

    def test_the_wrapper_can_actually_shrink(self):
        """A grid item defaults to min-width:auto and refuses to wrap
        below its longest word, which would push the column wide instead
        of narrow. The wrapper needs min-width:0 to wrap at all."""
        from catalyst.dashboard import render

        css = render._CSS
        block = css[css.index(".funnel-why-text"):]
        block = block[:block.index("}")]
        assert "min-width" in block and "0" in block, block
