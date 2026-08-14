"""Clicking a node must open something that explains it.

OWNER-ASKED: "or i can click in and it opens another page with the
runbook".

A map tells you a thing is connected to another thing and then leaves
you to work out what either of them means. Before this, only some nodes
linked anywhere at all, so clicking the map was a lottery: candidates
opened their decision, tickers opened the news, and every other node -
the feeds, the entities, the reasons things stopped - did nothing.

/node works for ANY id in the graph. It says what kind of node it is,
what to do when you land on one, everything recorded as connecting to
it in both directions, and where to go next.

Two rules these tests hold:

  - AN EMPTY SIDE IS EXPLAINED, not blank. Every line on the map is a
    stored row, so "nothing led here" is a fact about the data rather
    than a gap in the page.
  - AN UNKNOWN ID SAYS SO. The graph is rebuilt from rows that exist, so
    a node can vanish; the page must say that rather than render an
    empty shell that looks like a real record.
"""

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from tests.test_dashboard import bare, seeded  # noqa: F401 - shared fixtures


class TestEveryKindOfNodeHasARunbook:
    @pytest.mark.parametrize("kind", sorted(queries.NODE_RUNBOOK))
    def test_it_says_what_the_node_is_and_what_to_do(self, kind, seeded):
        d = queries.node_detail(Db(seeded), f"{kind}:whatever")
        assert d.kind == kind
        assert d.kind_label and not d.kind_label.endswith(":")
        assert len(d.runbook) > 40, f"{kind} has no usable runbook"

    def test_an_unknown_KIND_does_not_invent_advice(self, seeded):
        """A confident wrong runbook is worse than none."""
        d = queries.node_detail(Db(seeded), "wat:123")
        assert "No runbook is recorded" in d.runbook


class TestItReportsWhatTheGraphActuallyHolds:
    def test_a_real_node_is_found_and_carries_its_edges(self, seeded):
        b = queries.brain(Db(seeded))
        nid = next(nid for _, nodes in b.layers for nid, _, _ in nodes)
        d = queries.node_detail(Db(seeded), nid)
        assert d.found
        assert d.incoming or d.outgoing, (
            "a node drawn on the map with no edges either way should not "
            "have been in the picture")

    def test_the_edge_text_is_the_RECORDED_one(self, seeded):
        """Not a re-description. The map's hover text and this page must
        be the same string, or they will drift and disagree."""
        b = queries.brain(Db(seeded))
        src, dst, _w, title = b.edges[0]
        d = queries.node_detail(Db(seeded), dst)
        assert any(why == str(title) for _, why in d.incoming), (
            f"{title!r} not carried through to the node page")

    def test_an_unknown_id_is_declared_missing(self, seeded):
        d = queries.node_detail(Db(seeded), "cand:does-not-exist")
        assert not d.found

    def test_a_missing_node_renders_an_honest_page(self, seeded):
        html_out = panels.node_panel(Db(seeded), "cand:does-not-exist")
        assert "Nothing in the map has the id" in html_out
        assert "/brain" in html_out, "there must be a way back"

    def test_an_empty_side_is_EXPLAINED_not_blank(self, bare):
        """Every line on the map is a stored row, so 'nothing led here'
        is a fact about the data, not a gap in the page."""
        html_out = panels.node_panel(Db(bare), "src:edgar_form4")
        assert "Nothing recorded" in html_out or "Nothing in the map" in html_out


class TestThePageIsNavigable:
    def test_a_candidate_node_offers_its_decision(self, seeded):
        d = queries.node_detail(Db(seeded), "cand:c1")
        hrefs = " ".join(h for _, h in d.links)
        assert "/decision?candidate_id=c1" in hrefs

    def test_every_node_offers_a_way_back(self, seeded):
        for kind in queries.NODE_RUNBOOK:
            d = queries.node_detail(Db(seeded), f"{kind}:x")
            assert any(h == "/brain" for _, h in d.links), kind

    def test_the_page_needs_no_javascript(self, seeded):
        html_out = panels.node_panel(Db(seeded), "cand:c1")
        assert "<script" not in html_out.lower()
        assert "onclick" not in html_out.lower()
