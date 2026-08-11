"""The news map: what was said, about whom, what the bot did.

Owner-asked: "a second neural network for new linking news feeds e.g.
CEO appointed or something like that to see links and connections,
filters so the network doesnt get hug etc."

The claim under test is that EVERY LINE IS A ROW. A picture of a machine
that draws links the machine does not have is worse than no picture,
because it would be believed.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db

NOW = datetime.now(timezone.utc)


def seed(tmp_path, stories, others=()):
    path = str(tmp_path / "nm.db")
    conn = sqlite3.connect(path)
    conn.executescript(open("catalyst/storage/schema.sql").read())
    for i, s in enumerate(stories):
        payload = {"ticker": s["ticker"], "headline": s.get("headline", "h"),
                   "catalyst_type": s.get("catalyst", "news"),
                   "direction_hint": s.get("hint", 0),
                   "publisher": "benzinga",
                   "filed_date": NOW.date().isoformat()}
        conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                     ("alpaca_news", f"n{i}", NOW.isoformat(),
                      json.dumps(payload)))
    for i, t in enumerate(others):
        conn.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                     ("edgar_fts", f"f{i}", NOW.isoformat(),
                      json.dumps({"ticker": t, "catalyst_type": "earnings"})))
    conn.commit()
    conn.close()
    return Db(path)


class TestEveryLineIsARow:
    def test_a_story_links_only_to_the_ticker_it_named(self, tmp_path):
        db = seed(tmp_path, [{"ticker": "AAA"}, {"ticker": "BBB"}])
        m = queries.news_map(db)
        pairs = {(a, b) for a, b, _w, _t in m.edges if a.startswith("st-")}
        assert ("st-n0", "tk-AAA") in pairs
        assert ("st-n0", "tk-BBB") not in pairs, "a link nobody recorded"
        db.close()

    def test_the_headline_travels_with_the_edge(self, tmp_path):
        """Hovering a line must show what it actually was."""
        db = seed(tmp_path, [{"ticker": "AAA",
                              "headline": "Acme Appoints New CEO"}])
        m = queries.news_map(db)
        titles = " ".join(t for _a, _b, _w, t in m.edges)
        assert "Acme Appoints New CEO" in titles
        db.close()

    def test_a_ticker_with_no_candidate_is_shown_as_seen_not_invented(
            self, tmp_path):
        db = seed(tmp_path, [{"ticker": "AAA"}])
        m = queries.news_map(db)
        acts = [n[1] for n in m.layers[2][1]]
        assert acts == ["seen, not researched"]
        db.close()


class TestTheFiltersTheOwnerAskedFor:
    def test_filtering_by_kind_narrows_it(self, tmp_path):
        db = seed(tmp_path, [{"ticker": "AAA", "catalyst": "dilution"},
                             {"ticker": "BBB", "catalyst": "analyst_action"}])
        assert queries.news_map(db).story_count == 2
        assert queries.news_map(db, kind="dilution").story_count == 1
        db.close()

    def test_filtering_by_ticker_narrows_it(self, tmp_path):
        db = seed(tmp_path, [{"ticker": "AAA"}, {"ticker": "BBB"}])
        assert queries.news_map(db, ticker="aaa").story_count == 1
        db.close()

    def test_only_linked_keeps_the_cross_feed_ones(self, tmp_path):
        """The whole point. A story and a filing agreeing is two
        independent observations; a story alone is one newsroom."""
        db = seed(tmp_path, [{"ticker": "AAA"}, {"ticker": "BBB"}],
                  others=["AAA"])
        m = queries.news_map(db, only_linked=True)
        assert m.cross_feed_tickers == ("AAA",)
        assert {n[0] for n in m.layers[1][1]} == {"tk-AAA"}
        db.close()

    def test_the_columns_are_capped_so_a_firehose_is_not_a_smear(
            self, tmp_path):
        """A live firehose day carries 450+ symbols. Drawing them is not
        a map."""
        db = seed(tmp_path, [{"ticker": f"T{i:03d}"} for i in range(80)])
        m = queries.news_map(db)
        assert len(m.layers[1][1]) <= queries.MAP_MAX_TICKERS
        assert len(m.layers[0][1]) <= queries.MAP_MAX_STORIES
        assert m.story_count == 80, "the CAP must not change the COUNT"
        db.close()

    def test_cross_feed_tickers_survive_the_cap(self, tmp_path):
        """The cap must keep the interesting end, not the alphabetical
        one - otherwise the one link worth reading is the one dropped."""
        stories = [{"ticker": f"T{i:03d}"} for i in range(60)]
        stories.append({"ticker": "ZZZZ"})
        db = seed(tmp_path, stories, others=["ZZZZ"])
        m = queries.news_map(db)
        assert "tk-ZZZZ" in {n[0] for n in m.layers[1][1]}
        db.close()


class TestThePanel:
    def test_an_empty_window_prints_its_query_not_a_blank_gap(self, tmp_path):
        db = seed(tmp_path, [])
        html_out = panels.news_map_panel(db, {}, p="nm")
        # House rule 3: the zero arrives with the exact query that
        # produced it, so "no news yet" and "the query is broken" are
        # two different sentences.
        assert "FROM raw_events" in html_out
        assert "LIMIT 4000" in html_out
        assert "news feed reaches discovery from build" in html_out
        db.close()

    def test_it_explains_what_the_star_means(self, tmp_path):
        db = seed(tmp_path, [{"ticker": "AAA"}], others=["AAA"])
        html_out = panels.news_map_panel(db, {}, p="nm")
        assert "cross-feed links worth looking at" in html_out
        assert "two independent observations" in html_out
        db.close()

    def test_the_filter_form_is_present(self, tmp_path):
        db = seed(tmp_path, [{"ticker": "AAA"}])
        html_out = panels.news_map_panel(db, {}, p="nm")
        for field in ('name="days"', 'name="ticker"', 'name="kind"',
                      'name="linked"'):
            assert field in html_out, field
        db.close()

    def test_a_nonsense_days_value_does_not_break_the_page(self, tmp_path):
        db = seed(tmp_path, [{"ticker": "AAA"}])
        for bad in ("abc", "-5", "9999", ""):
            html_out = panels.news_map_panel(db, {"days": [bad]}, p="nm")
            assert "News map" in html_out
        db.close()

    def test_it_says_the_cap_is_a_cap(self, tmp_path):
        """A truncated picture that does not say it is truncated reads as
        the whole truth."""
        db = seed(tmp_path, [{"ticker": f"T{i:03d}"} for i in range(60)])
        html_out = panels.news_map_panel(db, {}, p="nm")
        assert "capped at" in html_out
        assert "Narrow the window" in html_out
        db.close()
