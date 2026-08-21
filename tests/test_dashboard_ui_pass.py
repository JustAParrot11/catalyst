"""The dashboard as an instrument, not an essay.

OWNER-ASKED 2026-08-21: "go through the entire dashboard, every section,
dont change the content just the style and way data is displayed, it
feels like there are loads of why is this here or what does this do
dropdown with no data ... I want this to be proper 100% professional
trading platform. No logic or inherent changes should take place that
affect the bot, only the UI, stress test your changes."

THE EMPTY DROPDOWNS WERE REAL, and there were five on the Overview
alone. Each promised "Where these N figures came from" and opened onto
nothing.

The cause bit at two levels. section() folds a panel's provenance away;
digest() then folds a panel's explanation away for the Overview. Both
searched the whole HTML - including inside a fold the other had just
built - so the outer pass cut those lines out of the inner disclosure
and moved them into its own. The first fold kept its promise and lost
its contents.

Everything here is presentational. No test in this file asserts a
number, a threshold or a decision: those belong to the suites that
already cover them, and this change must not have moved any of them.
"""

import re

import pytest

from catalyst.dashboard import server
from catalyst.dashboard.db import Db
from catalyst.dashboard.render import (
    NAV, _CSS, caveat_fold, details, digest, section,
)
from tests.test_trades_page import _seed


def fold_bodies(html: str):
    """(summary, inner_text) for every disclosure, nesting-aware. A
    non-greedy regex closes an outer <details> on an inner </details>,
    which silently under-reports exactly the nesting this file is
    about."""
    out, i = [], 0
    while True:
        m = re.search(r"<details[^>]*>", html[i:])
        if not m:
            return out
        start = i + m.end()
        depth, j = 1, start
        while depth and j < len(html):
            nxt = re.search(r"<details[^>]*>|</details>", html[j:])
            if not nxt:
                break
            depth += 1 if nxt.group(0).startswith("<details") else -1
            j += nxt.end()
        body = html[start:j]
        summ = re.search(r"<summary[^>]*>(.*?)</summary>", body, re.S)
        inner = re.sub(r"<summary.*?</summary>", "", body, flags=re.S)
        out.append((re.sub(r"<[^>]+>", "", summ.group(1)) if summ else "",
                    re.sub(r"<[^>]+>", "", inner).strip()))
        i = start


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
    conn = Db(_seed(tmp_path, closed=True))
    yield conn
    conn.close()


def every_route(db):
    for route, fn in sorted(server.HTML_ROUTES.items()):
        yield route, fn(db, {})


class TestNoDropdownOpensOntoNothing:
    def test_not_one_empty_disclosure_on_any_route(self, db):
        """THE REPORT, as a sweep of the whole dashboard."""
        offenders = []
        for route, html in every_route(db):
            for summ, inner in fold_bodies(html):
                if len(inner) < 12:
                    offenders.append(f"{route}: {summ[:60]!r}")
        assert not offenders, (
            "these dropdowns promise something and open onto nothing:\n"
            + "\n".join(offenders))

    def test_an_empty_details_renders_as_nothing_at_all(self):
        """Fixed in the renderer, not at each call site, so every future
        caller inherits it."""
        assert details("x", "summary", "") == ""
        assert details("x", "summary", "   \n ") == ""
        assert details("x", "summary", "<p>real</p>") != ""

    def test_a_caveat_fold_with_no_caveats_renders_as_nothing(self):
        assert caveat_fold("c", "Three standing caveats", []) == ""
        assert caveat_fold("c", "s", ["", "  "]) == ""
        assert caveat_fold("c", "s", ["a real caveat"]) != ""


class TestAFoldIsNeverEmptiedByAnotherFold:
    """The actual defect: two passes harvesting the same explanation."""

    def test_section_does_not_rob_a_fold_it_already_made(self):
        inner = section("inner", "Inner",
                        '<p class="prov">one</p><p class="prov">two</p>')
        assert "one" in inner and "two" in inner
        outer = section("outer", "Outer", inner)
        assert "one" in outer and "two" in outer
        for _summ, body in fold_bodies(outer):
            assert body.strip(), "a nested section emptied its own fold"

    def test_digest_does_not_rob_the_fold_section_made(self):
        panel = section("p", "Panel",
                        '<p class="prov">alpha</p><p class="prov">beta</p>')
        out = digest(panel)
        assert "alpha" in out and "beta" in out
        for _summ, body in fold_bodies(out):
            assert body.strip(), "digest emptied section's fold"

    def test_digest_still_folds_explanation_that_is_LOOSE(self):
        """The masking must not stop it doing its job - the Overview
        exists to be a summary."""
        loose = ('<section id="s"><h2>T</h2>'
                 '<div class="note">first</div>'
                 '<div class="note">second</div></section>')
        out = digest(loose)
        assert "workings" in out
        assert "first" in out and "second" in out

    def test_a_single_explanation_is_not_worth_a_fold(self):
        """One line behind a disclosure is more clicking than reading."""
        one = '<section id="s"><h2>T</h2><div class="note">only</div></section>'
        assert "workings" not in digest(one)


class TestItReadsLikeAnInstrument:
    def test_disclosures_are_not_styled_as_links(self):
        """A dozen blue links at body size reads as a dozen primary
        actions. They are optional detail."""
        block = _CSS[_CSS.index("summary { cursor: pointer"):][:300]
        assert "var(--muted)" in block
        assert "var(--series-1)" not in block

    def test_rows_alternate_so_a_column_can_be_scanned(self):
        assert "tbody tr:nth-child(even)" in _CSS

    def test_figures_stay_monospaced_and_tabular(self):
        """Unchanged, and asserted because a restyle is exactly when it
        would get lost: proportional digits make the eye do arithmetic
        it should not have to."""
        assert "tabular-nums" in _CSS
        assert ".tile-value" in _CSS and "ui-monospace" in _CSS

    def test_every_status_colour_still_ships_with_a_glyph(self):
        """Colour never carries meaning alone - it survives greyscale,
        forced-colours and colour blindness only because of this."""
        from catalyst.dashboard.render import _PILL_GLYPH, pill

        for state in _PILL_GLYPH:
            html = pill(state, "word")
            assert _PILL_GLYPH[state] in html
            assert "word" in html


class TestTheUIPassChangedNOTHINGTHATTRADES:
    """The owner's hard condition: "No logic or inherent changes should
    take place that affect the bot, only the UI"."""

    @pytest.mark.parametrize("module", [
        "catalyst.risk.sizing", "catalyst.risk.hard_bounds",
        "catalyst.risk.kill_switches", "catalyst.execution.orders",
        "catalyst.execution.broker", "catalyst.cost.governor",
        "catalyst.cost.pricing", "catalyst.orchestrator.cycle",
        "catalyst.research.position_review",
    ])
    def test_no_trading_module_imports_the_renderer(self, module):
        """The structural version of the promise: if nothing that
        decides or places a trade can even see the dashboard's
        rendering, a change to it cannot reach them."""
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module(module))
        assert "dashboard.render" not in src
        assert "dashboard import render" not in src

    def test_the_sizing_signature_is_untouched(self):
        """The one rule that is not negotiable, re-checked after a UI
        change for the same reason a pilot re-reads the checklist."""
        import inspect

        from catalyst.risk.sizing import size

        params = set(inspect.signature(size).parameters)
        assert not (params & {"view", "conviction", "research_view",
                              "price", "model_price"})


class TestEveryRouteSurvivesHostileData:
    def test_every_route_renders_on_an_empty_database(self, tmp_path):
        """A fresh install is the state most of these pages are seen in
        first, and the one a restyle is most likely to break."""
        from catalyst.storage import init_db

        path = str(tmp_path / "empty.db")
        init_db(path).close()
        d = Db(path)
        try:
            for route, fn in sorted(server.HTML_ROUTES.items()):
                html = fn(d, {})
                assert html and "<section" in html, route
        finally:
            d.close()

    def test_every_route_renders_with_no_database_at_all(self, tmp_path):
        d = Db(str(tmp_path / "missing.db"))
        try:
            for route, fn in sorted(server.HTML_ROUTES.items()):
                assert fn(d, {}), route
        finally:
            d.close()

    def test_no_route_emits_a_duplicated_element_id(self, db):
        """A duplicate id once meant one panel silently received
        another's data and both looked blank."""
        from catalyst.dashboard.render import duplicate_ids

        for route, html in every_route(db):
            assert not duplicate_ids(html), route

    def test_no_route_prints_a_double_escaped_entity(self, db):
        """&AMP;MDASH; on the page - a real owner-reported bug, and
        restyling is when it comes back."""
        for route, html in every_route(db):
            assert "&amp;mdash;" not in html.lower(), route
            assert "&lt;b&gt;" not in html, route

    def test_no_route_leaves_a_raw_style_attribute_unclosed(self, db):
        for route, html in every_route(db):
            assert html.count("<details") == html.count("</details>"), route
            assert html.count("<section") == html.count("</section>"), route
