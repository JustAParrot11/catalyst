"""The map must be readable, and have a way in.

OWNER-REPORTED: "redisgn the way the nerual network looks, it feels too
clunky its got too much data all at once and isnt easy to navigate."

Both halves of that are measurable, and both are fixed by drawing LESS
of the same truth rather than by drawing it differently.

TOO MUCH AT ONCE. Measured in Chromium at the owner's 1049px viewport,
on a 40-candidate graph:

    old default (14/layer)   46 dots, 122 lines, page 1287px
    new default (8/layer)    34 dots,  30 lines, page 1083px
    focused on one node    4-20 dots, 2-18 lines, page ~980px

Two changes did that. The per-layer default dropped to eight, and
duplicate edges collapsed: twenty decisions citing the same reason were
twenty identical lines stacked on each other, which the reader sees as
one line and the browser draws forty times.

HARD TO NAVIGATE. A graph where every node looks like every other node
has no entry point. The busiest nodes are now named as chips, and any
node opens its own neighbourhood.

WHAT MUST NOT CHANGE: a focused map is a SUBSET of the same edges. A
line on it is the same recorded row it was on the whole map. If focusing
could invent, merge or reroute a link, the picture would be a drawing
rather than evidence - and this dashboard's whole claim is that it never
draws a connector nobody can trace to a row.
"""

import pathlib
import sqlite3
from datetime import datetime, timezone

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from catalyst.dashboard.server import DEFAULT_BRAIN_NODES, HTML_ROUTES

SCHEMA_FILES = ("catalyst/storage/schema.sql",
                "catalyst/storage/schema_graph.sql",
                "catalyst/dashboard/schema_logs.sql")


@pytest.fixture
def wired(tmp_path):
    """Forty candidates across five feeds, half of them researched and
    declined for the same reason - which is what produces the stacked
    duplicate edges."""
    p = str(tmp_path / "brain.db")
    conn = sqlite3.connect(p)
    root = pathlib.Path(__file__).resolve().parents[1]
    for f in SCHEMA_FILES:
        conn.executescript((root / f).read_text())
    now = datetime.now(timezone.utc).isoformat()
    srcs = ["edgar_form4", "federal_register", "clinicaltrials",
            "alpaca_news", "openfda"]
    for i in range(40):
        conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                     (srcs[i % 5], f"e{i}", now, "{}"))
        conn.execute(
            "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
            (f"c{i}", f"TK{i:02d}", ["financing", "earnings", "guidance"][i % 3],
             "2026-08-25", "confirmed", f'["e{i}"]', now, "2870", "[]"))
        if i % 2 == 0:
            conn.execute(
                "INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                (f"c{i}", "long" if i % 4 else "no_trade", 0.6 + i / 200,
                 "t", "inv", 10, 0, "x"))
            conn.execute(
                "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"rd{i}", f"c{i}", "skip", None, None, None, None, None,
                 '["conviction_below_floor"]', "{}", now))
    conn.commit()
    conn.close()
    return p


class TestFocusingDrawsLESS:
    def test_a_focused_map_is_smaller_than_the_whole_one(self, wired):
        whole = queries.brain(Db(wired))
        nid = queries.busiest_nodes(whole, 1)[0][0]
        focused = queries.brain_focus(whole, nid)
        assert focused.edge_count < whole.edge_count
        assert focused.node_count < whole.node_count

    def test_one_hop_is_the_default_because_two_gives_back_the_wall(self, wired):
        """Measured, not assumed: this graph is hub-and-spoke, so two
        hops from a busy node reaches most of it."""
        assert queries.FOCUS_HOPS == 1
        whole = queries.brain(Db(wired))
        nid = queries.busiest_nodes(whole, 1)[0][0]
        one = queries.brain_focus(whole, nid, hops=1)
        two = queries.brain_focus(whole, nid, hops=2)
        assert one.node_count < two.node_count

    def test_the_default_view_is_smaller_than_it_was(self, wired):
        assert DEFAULT_BRAIN_NODES < 14, (
            "the default that the owner called too much was 14 per layer")

    def test_the_whole_map_is_still_one_click_away(self, wired):
        """Smaller by default must not mean unavailable."""
        html_out = panels.brain_panel(Db(wired), nodes=999)
        assert 'class="chart-wrap' in html_out


class TestAFocusedMapIsTheSAMETRUTH:
    """The property that makes it evidence rather than a drawing."""

    def test_every_focused_edge_exists_on_the_whole_map(self, wired):
        whole = queries.brain(Db(wired))
        for nid, _lbl, _l, _w in queries.busiest_nodes(whole, 5):
            focused = queries.brain_focus(whole, nid)
            for edge in focused.edges:
                assert edge in whole.edges, (
                    f"focusing on {nid} produced an edge the whole map "
                    f"does not have: {edge}")

    def test_every_focused_edge_touches_the_neighbourhood(self, wired):
        whole = queries.brain(Db(wired))
        nid = queries.busiest_nodes(whole, 1)[0][0]
        focused = queries.brain_focus(whole, nid, hops=1)
        for src, dst, _w, _t in focused.edges:
            assert nid in (src, dst), (
                "a one-hop focus drew an edge touching neither the node "
                "nor anything it connects to")

    def test_the_counts_describe_the_PICTURE_not_the_database(self, wired):
        """Reporting the whole graph's totals beside a fragment of it
        would misdescribe what is on screen."""
        whole = queries.brain(Db(wired))
        nid = queries.busiest_nodes(whole, 1)[0][0]
        focused = queries.brain_focus(whole, nid)
        assert focused.edge_count == len(focused.edges)
        assert focused.node_count == sum(len(n) for _, n in focused.layers)

    def test_an_unknown_focus_says_so_rather_than_drawing_nothing(self, wired):
        html_out = panels.brain_panel(Db(wired), focus="cand:does-not-exist")
        assert "Nothing is recorded as connecting to" in html_out
        assert 'href="/brain"' in html_out, "there must be a way back"


class TestDuplicateEdgesCollapse:
    def test_twenty_identical_links_are_drawn_once(self, wired):
        """Twenty decisions citing one reason are twenty rows, and were
        twenty lines stacked on each other: forty SVG elements the reader
        sees as one."""
        whole = queries.brain(Db(wired))
        collapsed = queries.collapse_edges(whole.edges)
        assert len(collapsed) < len(whole.edges)
        pairs = [(s, d) for s, d, _w, _t in collapsed]
        assert len(pairs) == len(set(pairs)), "still drawing duplicates"

    def test_the_count_is_kept_not_lost(self, wired):
        """A heavy relationship must not become indistinguishable from a
        single one."""
        whole = queries.brain(Db(wired))
        collapsed = queries.collapse_edges(whole.edges)
        assert sum(w for _s, _d, w, _t in collapsed) == sum(
            (w or 1) for _s, _d, w, _t in whole.edges)
        heavy = [t for _s, _d, _w, t in collapsed if "recorded links" in t]
        assert heavy, "no collapsed edge says how many rows are behind it"

    def test_the_headline_still_reports_every_recorded_link(self, wired):
        """Collapsing is a drawing decision. The database still holds
        what it holds, and the page must still say so."""
        whole = queries.brain(Db(wired))
        html_out = panels.brain_panel(Db(wired))
        assert f">{whole.edge_count}<" in html_out


class TestThereIsAWayIn:
    def test_the_page_names_the_busiest_nodes_to_start_from(self, wired):
        html_out = panels.brain_panel(Db(wired))
        assert 'class="waysin"' in html_out
        assert html_out.count('class="waychip"') >= 5

    def test_each_one_opens_that_nodes_own_neighbourhood(self, wired):
        html_out = panels.brain_panel(Db(wired))
        assert "/brain?focus=" in html_out

    def test_they_are_ordered_busiest_first(self, wired):
        whole = queries.brain(Db(wired))
        weights = [w for _n, _l, _lyr, w in queries.busiest_nodes(whole, 8)]
        assert weights == sorted(weights, reverse=True)

    def test_a_node_page_offers_to_draw_its_corner_of_the_map(self, wired):
        """The navigation loop: map -> runbook -> that node's corner ->
        the next runbook. Without the middle step the only way back into
        the picture is the whole picture."""
        whole = queries.brain(Db(wired))
        nid = queries.busiest_nodes(whole, 1)[0][0]
        d = queries.node_detail(Db(wired), nid)
        assert any(h == f"/brain?focus={nid}" for _t, h in d.links)

    def test_the_focused_page_offers_a_way_back_and_a_runbook(self, wired):
        whole = queries.brain(Db(wired))
        nid = queries.busiest_nodes(whole, 1)[0][0]
        html_out = panels.brain_panel(Db(wired), focus=nid)
        assert 'href="/brain"' in html_out
        assert f'href="/node?id={nid}"' in html_out

    def test_the_zoom_controls_keep_the_focus(self, wired):
        """Changing the zoom must not silently throw you back to the
        whole map."""
        whole = queries.brain(Db(wired))
        nid = queries.busiest_nodes(whole, 1)[0][0]
        html_out = panels.brain_panel(Db(wired), focus=nid)
        assert f"focus={nid}" in html_out.split('class="viewbar"')[1][:2000]


class TestItStillNeedsNoJavaScript:
    """The map is documented as deterministic; a JS pan/zoom draws a
    different picture per browser and cannot be pasted into a bug
    report."""

    def test_the_focused_page_has_no_script(self, wired):
        whole = queries.brain(Db(wired))
        nid = queries.busiest_nodes(whole, 1)[0][0]
        html_out = panels.brain_panel(Db(wired), focus=nid)
        assert "<script" not in html_out.lower()
        assert "onclick" not in html_out.lower()


class TestAPastedURLCannotBreakThePage:
    @pytest.mark.parametrize("focus", [
        "", "wat", "cand:'; DROP TABLE candidates--", "x" * 500,
        "src:<script>alert(1)</script>"])
    def test_it_renders_rather_than_raising(self, focus, wired):
        html_out = HTML_ROUTES["/brain"](Db(wired), {"focus": [focus]})
        assert html_out
        assert "<script>alert" not in html_out, "unescaped focus reached the page"
