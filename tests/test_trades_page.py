"""The first real trade, and a page that explains it in English.

OWNER-ASKED, on the day the bot finally traded: "I want a tab to be
actually getting data about past and present trades like every thing, if
i traded i want to know why, the decisions its taking and will take, for
complete trades an entire breakdown. I also want it breaking into
english, chat responses claude gives, I want to understand in plain
text."

And, in the same message, a real defect: "the dashboard throws this error
- position 85fb5edc... is unprotected". It HAD been unprotected, for
about fifteen minutes on 2026-08-17, and the ten checks after it all
said ok. The alerts query asked for every non-ok row ever written, so a
resolved gap alarmed forever - the same class as the stale HTTP 400s and
the reconciliation prompt.

THE FIXTURE IS THE REAL TRADE, rebuilt from the diagnostic bundle:
EMBC, four insiders including the CEO and CFO buying on one day, 79.1295
shares at $5.06, stop at $4.55, hard exit 2026-08-29. Conviction 0.60 -
the first long ever to clear the floor, and the first evidence that
defining conviction as a frequency fixed a units mismatch rather than
merely reworded one.
"""

import json
import re

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from catalyst.storage import init_db

CID = "insider_cluster-EMBC-2026-08-13-e0aa1df6061c"
POS = "85fb5edc-a8f5-4bd8-a6a1-0b91c4953b4c"
ENTRY = "142c93b5-985e-412e-b029-fc2c9d30de4b"
STOP_OK = "b57faf5a-6a86-4eea-89df-db4596917085"

THESIS = ("Four insiders including the CEO ($140,937 at $4.70), CFO "
          "($93,360 at $4.67), a director ($223,848 ...) all bought on the "
          "open market on the same day (Aug 12).")
INVALIDATION = ("Close below $4.60 (below the low end of the insider "
                "purchase price cluster, $4.67-$4.99).")
PRICED_IN_WHY = ("Since the Form 4 cluster became public price has been "
                 "roughly flat; coverage is limited to aggregators.")


def _seed(tmp_path, *, closed=False, reviews=(), stops=None):
    path = str(tmp_path / "t.db")
    conn = init_db(path)
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (CID, "EMBC", "insider_cluster", "2026-08-13", "confirmed",
                  "[]", "2026-08-17T16:00:00+00:00", "health", "[]"))
    conn.execute("INSERT INTO candidate_origin VALUES (?,?,?,?)",
                 (CID, "screen", None, "2026-08-17T16:00:00+00:00"))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 (CID, "long", 0.6, THESIS, INVALIDATION, 12, 0,
                  PRICED_IN_WHY))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("d1", CID, "trade", "long", "400.00", "79.1295", "4.55",
                  "2026-08-29", "[]", "{}", "2026-08-17T16:28:54+00:00"))
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (ENTRY, CID, "e6c14963", "buy", "79.1295", "market", "day",
                  "2026-08-17T16:28:54+00:00", "filled", "{}"))
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("rej1", CID, None, "sell", "15", "stop", "day",
         "2026-08-17T16:28:56+00:00", "rejected",
         json.dumps({"code": 40310000,
                     "message": "potential wash trade detected.",
                     "reject_reason": "opposite side market/stop order exists"})))
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("stp1", CID, STOP_OK, "sell", "79.1295", "stop", "day",
                  "2026-08-17T16:43:57+00:00", "new", "{}"))
    conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?)",
                 (ENTRY, "5.06", "79.1295", "2026-08-17T16:28:59+00:00",
                  "5.06", "0.3964"))
    conn.execute("INSERT INTO entry_market_context VALUES (?,?,?,?)",
                 (ENTRY, "4.2", "5.055", "2026-08-17T16:28:59+00:00"))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 (POS, "EMBC", json.dumps([ENTRY]), STOP_OK,
                  "2026-08-17T16:27:56+00:00", "2026-08-29",
                  "closed" if closed else "open"))
    if stops is None:
        stops = [("2026-08-17T16:43:57+00:00", "[]", "unprotected")] + [
            (f"2026-08-17T1{h}:00:00+00:00", json.dumps([STOP_OK]), "ok")
            for h in range(6, 10)]
    for when, ids, status in stops:
        conn.execute("INSERT INTO stop_confirmations VALUES (?,?,?,?)",
                     (POS, when, ids, status))
    for when, action, triggered, reasoning, changed in reviews:
        conn.execute(
            "INSERT INTO position_reviews (id,position_id,ticker,action,"
            "invalidation_triggered,reasoning,what_changed_json,"
            "prompt_rendered,raw_response_json,model,cost_cents,reviewed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"rv-{when}", POS, "EMBC", action, int(triggered), reasoning,
             json.dumps(changed), "p", "{}", "m", "1", when))
    if closed:
        conn.execute("INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
                     (POS, "paper", "5.06", "5.62", "hard_exit_date", 4431,
                      12, 12, "2026-08-29T20:00:00+00:00"))
    conn.commit()
    conn.close()
    return path


def _page(path, params=None):
    db = Db(path)
    try:
        return panels.trades_panel(db, params or {}, p="tr")
    finally:
        db.close()


class TestTheStaleUnprotectedAlarm:
    """OWNER-REPORTED verbatim. The gap was real and is over."""

    def test_a_resolved_gap_no_longer_alarms(self, tmp_path):
        db = Db(_seed(tmp_path))
        try:
            a = queries.alerts(db)
        finally:
            db.close()
        alarms = [t for sev, t, _ in a.items if sev == "alarm"]
        assert not [t for t in alarms if "unprotected" in t], (
            f"a resolved gap is still alarming: {alarms}")

    def test_a_position_unprotected_RIGHT_NOW_still_alarms(self, tmp_path):
        """The direction that matters. Silencing history must not
        silence a live one."""
        path = _seed(tmp_path, stops=[
            ("2026-08-17T16:00:00+00:00", json.dumps([STOP_OK]), "ok"),
            ("2026-08-17T17:00:00+00:00", "[]", "unprotected")])
        db = Db(path)
        try:
            a = queries.alerts(db)
        finally:
            db.close()
        alarms = [t for sev, t, _ in a.items if sev == "alarm"]
        assert any("unprotected" in t for t in alarms), (
            "a position with no resting stop RIGHT NOW is not alarming")

    def test_duplicate_stops_also_still_alarm(self, tmp_path):
        path = _seed(tmp_path, stops=[
            ("2026-08-17T17:00:00+00:00", json.dumps([STOP_OK, "other"]),
             "duplicate_stops")])
        db = Db(path)
        try:
            a = queries.alerts(db)
        finally:
            db.close()
        assert any("duplicate_stops" in t for sev, t, _ in a.items)

    def test_the_gap_is_still_VISIBLE_on_the_trade(self, tmp_path):
        """Not alarming is not the same as hidden. A position that was
        briefly naked is a fact worth knowing after it is fixed."""
        html = _page(_seed(tmp_path))
        assert "There was a gap earlier" in html
        assert "unprotected" in html


class TestItTellsTheStoryInEnglish:
    def test_it_opens_with_what_actually_happened(self, tmp_path):
        html = _page(_seed(tmp_path))
        for phrase in ("79.1295 shares", "$5.06", "400.00 dollars",
                       "$4.55", "2026-08-29"):
            assert phrase in html, f"the headline omits {phrase}"
        assert "hard exit date" in html

    def test_conviction_is_translated_not_just_printed(self, tmp_path):
        """0.60 means nothing to a reader. "about 60 times in 100" is
        the definition the model was given, said back in English."""
        html = _page(_seed(tmp_path))
        assert "0.60" in html
        assert "60 times in 100" in html

    def test_the_model_is_QUOTED_not_summarised(self, tmp_path):
        """A summary of a thesis is just another opinion. The owner
        asked to read what Claude actually said."""
        html = _page(_seed(tmp_path))
        assert THESIS[:60] in html
        assert INVALIDATION[:40] in html
        assert PRICED_IN_WHY[:40] in html
        assert 'class="said"' in html, "the model's words are not set apart"

    def test_priced_in_is_explained_in_plain_words(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "whether the market had already reacted" in html

    def test_it_says_who_found_the_candidate(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "mechanical screen" in html

    def test_it_states_that_claude_did_not_choose_the_size(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "Claude never chooses the amount" in html


class TestItShowsWhatHappensNext:
    def test_an_open_position_says_what_will_happen(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "What happens next" in html
        assert "never push it out" in html

    def test_reviews_are_quoted_with_their_action_in_english(self, tmp_path):
        path = _seed(tmp_path, reviews=[
            ("2026-08-18T14:00:00+00:00", "hold", False,
             "The thesis is intact; price is holding above the insider "
             "cluster.", ["no new filings"])])
        html = _page(path)
        assert "keep holding" in html
        assert "The thesis is intact" in html
        assert "no new filings" in html

    def test_an_exit_review_reads_as_an_exit(self, tmp_path):
        path = _seed(tmp_path, reviews=[
            ("2026-08-19T14:00:00+00:00", "exit_now", True,
             "Closed below $4.60, which was the stated invalidation.", [])])
        html = _page(path)
        assert "close it now" in html
        assert "its invalidation had triggered" in html


class TestAClosedTradeGetsTheWholeBreakdown:
    def test_the_result_is_shown_with_the_expectation_beside_it(
            self, tmp_path):
        html = _page(_seed(tmp_path, closed=True))
        assert "$44.31" in html, "the realised P&L is not shown"
        assert "$5.62" in html and "hard_exit_date" in html
        assert "expected 12d" in html

    def test_a_loss_is_not_dressed_up(self, tmp_path):
        path = _seed(tmp_path, closed=True)
        import sqlite3

        conn = sqlite3.connect(path)
        conn.execute("UPDATE closed_trades SET realized_pnl_cents = -2200, "
                     "exit_price = '4.55', exit_reason = 'stop_hit'")
        conn.commit()
        conn.close()
        html = _page(path)
        assert "a loss" in html
        assert "stop_hit" in html


class TestEveryOrderIncludingTheFailures:
    def test_the_rejected_stop_is_shown_with_the_brokers_own_words(
            self, tmp_path):
        """The rejection IS the story of the fifteen-minute gap. Hiding
        it is how a gap goes unnoticed."""
        html = _page(_seed(tmp_path))
        assert "REJECTED" in html
        assert "wash trade" in html
        assert "opposite side market/stop order exists" in html

    def test_all_three_orders_appear(self, tmp_path):
        html = _page(_seed(tmp_path))
        block = re.search(r'<table id="tr-t0-orders".*?</table>', html, re.S)
        assert block, "the order table did not render"
        rows = block.group(0).count("<tr")
        assert rows >= 4, f"only {rows - 1} order(s) shown, expected 3"


class TestItSaysNothingItDoesNotKnow:
    def test_no_positions_is_explained_not_blank(self, tmp_path):
        path = str(tmp_path / "empty.db")
        init_db(path).close()
        html = _page(path)
        assert "no position has been opened yet" in html
        assert "positions" in html

    def test_a_position_with_no_research_view_says_so(self, tmp_path):
        path = _seed(tmp_path)
        import sqlite3

        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM research_views")
        conn.commit()
        conn.close()
        html = _page(path)
        assert "No research view is on record" in html

    def test_a_position_with_no_stop_check_yet_says_so(self, tmp_path):
        path = _seed(tmp_path, stops=[])
        html = _page(path)
        assert "No stop check has run" in html
