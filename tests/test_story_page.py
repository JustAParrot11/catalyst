"""Click a headline, read what the bot made of it.

OWNER-ASKED, in their own words: "I want to be able to click the news
and see what the bot thought of each and connectiosn fit ahts what he
did."

WHAT WAS THERE BEFORE. The news map drew a story column, but story nodes
were not clickable at all - only the TICKER nodes were, and they led to
a log search. "What was logged about this symbol" is a different and
much narrower question than "what did the bot think of this story", and
only the second one is worth clicking a headline for.

THE HARDEST CASE IS THE COMMON ONE. Most stories lead nowhere: the news
feed is used for corroboration and sentiment and does not on its own
manufacture a candidate. A page that renders that as an empty panel is
indistinguishable from a broken one, and telling those apart is
repeatedly the whole diagnosis (BUILD-BRIEF: "'No data' and 'the query
is broken' look identical otherwise"). So the tests below check just as
hard that a dead end SAYS it is a dead end.
"""

import json
import os
import sqlite3
import tempfile

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from catalyst.storage import init_db

STORY = {
    "ticker": "REGN",
    "headline": "FDA grants priority review",
    "publisher": "Reuters",
    "filed_date": "2026-08-15",
    "catalyst_type": "fda_decision",
    "direction_hint": 1,
}


def _db(tmp_path, *, corroborated=False, candidate=False, decision=None,
        note=None):
    path = str(tmp_path / "c.db")
    conn = init_db(path)
    conn.execute(
        "INSERT INTO raw_events VALUES ('alpaca_news','n1',"
        "'2026-08-15T12:00:00+00:00',?)", (json.dumps(STORY),))
    if corroborated:
        conn.execute(
            "INSERT INTO raw_events VALUES ('edgar_fts','e9',"
            "'2026-08-15T09:00:00+00:00',?)",
            (json.dumps({"ticker": "REGN",
                         "headline": "8-K: PDUFA date disclosed"}),))
    if candidate:
        conn.execute(
            "INSERT INTO candidates VALUES ('cand-1','REGN','fda_decision',"
            "'2026-09-01','confirmed',?,'2026-08-15T12:05:00+00:00',"
            "'health','[]')", (json.dumps(["n1"]),))
        conn.execute(
            "INSERT INTO research_views VALUES ('cand-1','long',0.72,"
            "'Priority review shortens the path.','Readout slips past Q4',"
            "14,0,'not in the price yet')")
    if decision:
        conn.execute(
            "INSERT INTO risk_decisions (id,candidate_id,action,side,"
            "notional_usd,qty,stop_price,planned_exit_date,skip_reasons,"
            "adaptive_params_snapshot,decided_at) VALUES "
            "('dec-1','cand-1',?,'long','200.00','4','46.00','2026-09-02',"
            "?,'{}','2026-08-15T12:06:00+00:00')",
            (decision, json.dumps(["below_conviction_floor"])
             if decision == "skip" else "[]"))
    if note:
        conn.execute("INSERT INTO limit_application_notes VALUES "
                     "('dec-1','per_stock_stop_width',?)", (note,))
    conn.commit()
    conn.close()
    return Db(path)


class TestAStoryThatLedNowhere:
    """The common case, and the one most likely to look like a bug."""

    def test_it_says_nothing_was_built_rather_than_showing_a_blank(
            self, tmp_path):
        db = _db(tmp_path)
        html = panels.story_panel(db, {"id": ["n1"]}, p="story")
        db.close()
        assert "Nothing was built from this story" in html
        assert "ordinary outcome" in html, (
            "a dead end is presented without saying it is normal")

    def test_it_still_shows_the_headline_and_its_provenance(self, tmp_path):
        db = _db(tmp_path)
        html = panels.story_panel(db, {"id": ["n1"]}, p="story")
        db.close()
        assert "FDA grants priority review" in html
        assert "Reuters" in html

    def test_a_lone_newsroom_is_named_as_one_observation(self, tmp_path):
        db = _db(tmp_path)
        html = panels.story_panel(db, {"id": ["n1"]}, p="story")
        db.close()
        assert "Only the news feed mentioned" in html
        assert "normal, not a fault" in html


class TestAStoryThatBecameATrade:
    def test_the_whole_path_is_readable_in_one_page(self, tmp_path):
        """BUILD-BRIEF's test: someone who was not there can read it and
        understand why the trade was made."""
        db = _db(tmp_path, corroborated=True, candidate=True,
                 decision="trade",
                 note=("REGN moves 2.6% on a bad day, so its stop sits at "
                       "8% - 3x its own noise, rather than the 50% the "
                       "fda_decision category assumes"))
        html = panels.story_panel(db, {"id": ["n1"]}, p="story")
        db.close()
        for probe in (
                "FDA grants priority review",       # what was said
                "another feed named the same company",   # corroboration
                "The model read it as long",        # what it concluded
                "0.72",
                "Priority review shortens",         # the thesis, verbatim
                "What would prove it wrong",        # the invalidation
                "The risk engine traded it",        # what the code did
                "$200.00",
                "3x its own noise",                 # WHY that size
                "/decision?candidate_id=cand-1"):   # the full record
            assert probe in html, f"missing from the narrative: {probe!r}"

    def test_it_marks_which_candidate_came_from_THIS_story(self, tmp_path):
        db = _db(tmp_path, candidate=True, decision="trade")
        html = panels.story_panel(db, {"id": ["n1"]}, p="story")
        db.close()
        assert "built from THIS story" in html

    def test_a_declined_candidate_shows_the_reason(self, tmp_path):
        db = _db(tmp_path, candidate=True, decision="skip")
        html = panels.story_panel(db, {"id": ["n1"]}, p="story")
        db.close()
        assert "The risk engine declined it" in html
        assert "below_conviction_floor" in html

    def test_the_per_stock_sizing_sentence_reaches_the_page(self, tmp_path):
        """These notes exist precisely to answer "why is this position
        that size", which a rule name and two numbers cannot."""
        db = _db(tmp_path, candidate=True, decision="trade",
                 note="REGN's worst overnight gap is 21%, better than 60%")
        html = panels.story_panel(db, {"id": ["n1"]}, p="story")
        db.close()
        assert "worst overnight gap is 21%" in html


class TestTheHeadlineIsActuallyClickable:
    def test_story_nodes_carry_a_link(self, tmp_path):
        db = _db(tmp_path)
        m = queries.news_map(db, days=30)
        db.close()
        story_links = {k: v for k, v in m.node_links.items()
                       if k.startswith("st-")}
        assert story_links, "headlines are still not clickable"
        assert all(v.startswith("/story?id=") for v in story_links.values())

    def test_tickers_keep_their_own_link(self, tmp_path):
        """The ticker's log search answers a different question and is
        still worth having."""
        db = _db(tmp_path)
        m = queries.news_map(db, days=30)
        db.close()
        assert any(k.startswith("tk-") and v.startswith("/logs?")
                   for k, v in m.node_links.items())


class TestItNeverRendersAConfusingBlank:
    def test_an_unknown_id_explains_itself(self, tmp_path):
        db = _db(tmp_path)
        html = panels.story_panel(db, {"id": ["nope"]}, p="story")
        db.close()
        assert "no stored news story has the id" in html

    def test_no_id_at_all_points_back_to_the_map(self, tmp_path):
        db = _db(tmp_path)
        html = panels.story_panel(db, {}, p="story")
        db.close()
        assert "/newsmap" in html

    @pytest.mark.parametrize("bad", ["", "'; DROP TABLE raw_events;--",
                                     "../../etc/passwd", "x" * 500])
    def test_hostile_ids_do_not_break_it(self, tmp_path, bad):
        db = _db(tmp_path)
        html = panels.story_panel(db, {"id": [bad]}, p="story")
        db.close()
        assert "<" in html          # it rendered something
        assert "Traceback" not in html

    def test_a_story_with_no_ticker_does_not_crash(self, tmp_path):
        path = str(tmp_path / "c.db")
        conn = init_db(path)
        conn.execute(
            "INSERT INTO raw_events VALUES ('alpaca_news','n2',"
            "'2026-08-15T12:00:00+00:00',?)",
            (json.dumps({"headline": "A story about nobody"}),))
        conn.commit()
        conn.close()
        db = Db(path)
        html = panels.story_panel(db, {"id": ["n2"]}, p="story")
        db.close()
        assert "A story about nobody" in html
