"""Headings must not skip levels.

Task #30 (data density and hierarchy) had one part I could settle by
measuring rather than by eye, and measuring found four real faults: the
funnel's reason and fault lists were <h4> sitting directly under the
section's <h2>, with no <h3> between them, on both `/` and `/funnel`.

Why it matters beyond tidiness: heading level IS the document outline. A
screen reader navigates by it, and a jump from h2 to h4 tells the reader
they have missed a whole section that does not exist. The dashboard's
stated job is that nobody should need to SSH in to understand the bot;
that promise is weaker for anyone reading it through assistive
technology.

This is the objective half of the task. Whether the page READS well -
spacing, rhythm, weight - is a judgement the owner has to make by
looking at it, and no assertion here should pretend otherwise.
"""

import re

import pytest

from catalyst.dashboard import panels
from catalyst.dashboard.db import Db
from tests.test_dashboard import _iso, bare, seeded  # noqa: F401 - fixtures


@pytest.fixture
def with_drops(bare):  # noqa: F811
    """A funnel that actually renders "Why they stopped here".

    The shared `seeded` fixture produces FAULT rows but no DROP rows, so
    that heading never appears in it - a sabotage reverting it to h4 went
    undetected because the heading under test was not on the page. Same
    fixture gap that bit test_funnel_layout.py.
    """
    import sqlite3
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date()
    conn = sqlite3.connect(bare)
    for i in range(3):
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"h{i}", "ZZ", "x", today.isoformat(), "estimated",
                      "[]", _iso(today), "s", "[]"))
        conn.execute(
            "INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
            (f"hrc{i}", f"h{i}", "m", "p", "[]", "0", 1,
             "budget_denied", _iso(today)))
    conn.commit()
    conn.close()
    return bare


def _headings(html_out):
    return [(int(m.group(1)), re.sub(r"<[^>]+>", "", m.group(2)).strip()[:50])
            for m in re.finditer(r"<h([1-6])[^>]*>(.*?)</h\1>", html_out, re.S)]


def _skips(html_out):
    heads = _headings(html_out)
    out = []
    for (prev, _), (lvl, txt) in zip(heads, heads[1:]):
        if lvl > prev + 1:
            out.append(f"h{prev} -> h{lvl} at {txt!r}")
    return out


PANELS = [
    ("funnel", panels.funnel_panel),
    ("costs", panels.cost_panel),
    ("performance", panels.performance_panel),
    ("refusals", panels.refusals_panel),
    ("brain", panels.brain_panel),
]


class TestNoPanelSkipsAHeadingLevel:
    @pytest.mark.parametrize("name,fn", PANELS, ids=[n for n, _ in PANELS])
    def test_panel_outline_is_continuous(self, name, fn, seeded):
        skipped = _skips(fn(Db(seeded)))
        assert not skipped, (
            f"the {name} panel's outline jumps: {skipped}. Heading level "
            "is the document outline - a jump tells a screen-reader user "
            "they have missed a section that does not exist.")

    def test_the_funnel_fault_lists_are_the_case_that_broke(self, with_drops):
        """Named explicitly because this is what the audit found: the
        'NEEDS ATTENTION' lists were h4 directly under the section h2."""
        html_out = panels.funnel_panel(Db(with_drops))
        levels = {txt: lvl for lvl, txt in _headings(html_out)}
        # EACH heading is pinned individually, not just the outline as a
        # whole. Reverting ONE of the three to h4 left the outline
        # continuous (another h3 now precedes it) and the continuity
        # check passed - so a single regression would have slipped
        # through. Sabotage found that before it mattered.
        wanted = [txt for txt in levels
                  if "NEEDS ATTENTION" in txt or "went wrong" in txt
                  or "could not be read" in txt or "stopped here" in txt]
        assert wanted, "the seed should produce at least one such heading"
        for txt in wanted:
            assert levels[txt] <= 3, (
                f"{txt!r} is an h{levels[txt]} inside a section whose own "
                "heading is an h2 - a level is missing between them")


class TestTheOutlineSurvivesAnEmptyDatabase:
    @pytest.mark.parametrize("name,fn", PANELS, ids=[n for n, _ in PANELS])
    def test_empty_panel_outline_is_continuous(self, name, fn, bare):
        """An empty panel takes different branches - placeholders,
        empty blocks - and those headings must be levelled too."""
        assert not _skips(fn(Db(bare)))
