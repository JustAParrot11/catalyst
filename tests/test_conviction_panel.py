"""'Why it did not trade', answered in one number.

The bot can run unattended for a week and place no orders. Every
ingredient for diagnosing that was already on the dashboard - a
conviction gauge per decision, every drop reason in the funnel - but
only one candidate at a time, and nobody averages forty gauges by eye.

A model scoring 0.58 against a 0.60 bar and one scoring 0.30 against it
produce the IDENTICAL funnel and need opposite responses. That
distinction is what these tests hold.

No calendar dates (house rule 6): the panel windows against
datetime('now'), so every fixture is relative to the real clock.
"""

from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard import panels
from catalyst.risk.adaptive_params import DEFAULT_PARAMS
from catalyst.risk.evaluate import PRICED_IN_CONVICTION_PREMIUM
from catalyst.storage import init_db

FLOOR = float(DEFAULT_PARAMS["conviction_floor"])
RAISED = FLOOR + float(PRICED_IN_CONVICTION_PREMIUM)


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "conv.db")
    init_db(p).close()
    return p


def seed(path, views, days_ago=1):
    """views: list of (conviction, priced_in, direction)"""
    conn = init_db(path)
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    for i, (conv, pin, direction) in enumerate(views):
        cid = f"c{i}"
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     (cid, "AAA", "insider_cluster", "2026-08-20", "confirmed",
                      "[]", when, "2834", "[]"))
        conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                     (cid, direction, conv, "t", "i", 12, 1 if pin else 0, "r"))
        conn.execute(
            "INSERT INTO risk_decisions (id, candidate_id, action, "
            "skip_reasons, adaptive_params_snapshot, decided_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"d{i}", cid, "skip", "[]", "{}", when))
    conn.commit()
    conn.close()


class TestItSeparatesNearMissFromNoSignal:
    """The whole reason this panel exists."""

    def test_a_near_miss_is_called_a_threshold_question(self, db_path):
        seed(db_path, [(FLOOR - 0.02, False, "long")])
        html = panels.conviction_panel(Db(db_path))
        assert "threshold question" in html
        assert "0.02" in html

    def test_a_wide_miss_is_NOT_called_a_threshold_question(self, db_path):
        """Moving the floor to catch a 0.25 candidate buys trades the
        model does not believe in. The panel must say so."""
        seed(db_path, [(FLOOR - 0.35, False, "long")])
        html = panels.conviction_panel(Db(db_path))
        assert "not a threshold problem" in html
        assert "does not believe in" in html

    def test_a_middling_miss_points_at_the_priced_in_premium(self, db_path):
        seed(db_path, [(FLOOR - 0.12, False, "long")])
        html = panels.conviction_panel(Db(db_path))
        assert "consistently below the bar" in html

    def test_all_no_trade_is_distinguished_from_scoring_low(self, db_path):
        """A model declining to score at all is a judgement about the
        candidates, not about the floor. Lowering the floor changes
        nothing, and the panel must not imply otherwise."""
        seed(db_path, [(0.0, False, "no_trade"), (0.0, False, "no_trade")])
        html = panels.conviction_panel(Db(db_path))
        assert "declining to score them at all" in html
        assert "Lowering the floor would change nothing" in html


class TestItUsesTheBarEachCandidateActuallyFaced:
    def test_a_priced_in_candidate_is_measured_against_the_RAISED_bar(self, db_path):
        """Scoring above the base floor but below floor+premium is a
        miss, and averaging against the base floor would flatter the
        model by fifteen points on most of the sample."""
        seed(db_path, [(FLOOR + 0.05, True, "long")])     # 0.65 vs a 0.75 bar
        html = panels.conviction_panel(Db(db_path))
        assert "0 cleared the bar" in html or "<b>0</b> cleared" in html

    def test_the_same_score_clears_when_not_priced_in(self, db_path):
        seed(db_path, [(FLOOR + 0.05, False, "long")])    # 0.65 vs a 0.60 bar
        html = panels.conviction_panel(Db(db_path))
        assert "It is trading" in html

    def test_both_bars_are_shown_with_their_values(self, db_path):
        seed(db_path, [(0.5, True, "long"), (0.5, False, "long")])
        html = panels.conviction_panel(Db(db_path))
        assert f"{FLOOR:.2f}" in html and f"{RAISED:.2f}" in html
        assert "priced-in premium" in html


class TestTheWindow:
    def test_it_ignores_decisions_outside_the_window(self, db_path):
        seed(db_path, [(0.9, False, "long")], days_ago=30)
        html = panels.conviction_panel(Db(db_path), days=7)
        assert "Nothing researched in the last 7 days" in html

    def test_an_empty_window_says_where_to_look_instead(self, db_path):
        """House rule 3. 'Nothing researched' and 'the query broke' must
        not look identical, and the owner needs the next step."""
        html = panels.conviction_panel(Db(db_path))
        assert "stopping before they reach the model" in html


class TestItDoesNotBreakTheFunnelPage:
    def test_an_unreadable_conviction_is_skipped_not_fatal(self, db_path):
        conn = init_db(db_path)
        when = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     ("c0", "AAA", "insider_cluster", "2026-08-20", "confirmed",
                      "[]", when, "2834", "[]"))
        conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                     ("c0", "long", 0.7, "t", "i", 12, 0, "r"))
        conn.execute(
            "INSERT INTO risk_decisions (id, candidate_id, action, "
            "skip_reasons, adaptive_params_snapshot, decided_at) "
            "VALUES (?,?,?,?,?,?)", ("d0", "c0", "skip", "[]", "{}", when))
        conn.commit(); conn.close()
        html = panels.conviction_panel(Db(db_path))
        assert "Why it did not trade" in html
