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
        """The FACTS, not the sentence they were once written in. These
        four numbers moved from a paragraph into tiles when the owner
        reported the page "feels word heavy" - asserting the prose would
        have made a layout change look like a data loss."""
        html = _page(_seed(tmp_path))
        for phrase in ("79.1295", "$5.06", "400.00", "$4.55", "2026-08-29"):
            assert phrase in html, f"the summary omits {phrase}"
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
        assert "<b>Next.</b>" in html
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


# ------------------------------------------------ why THAT amount
#
# OWNER-ASKED: "will the dashboard explain why it decided to for example
# spend 15% of account value instead of 30% etc".
#
# It has always stored the answer - limit_applications records every rule
# with what was wanted, what was allowed and whether it bound, and
# limit_application_notes carries the sentence behind a per-stock bound.
# None of it was shown anywhere a person would look.
#
# THE SIZE IS ONE SHORT SUM and it is completely explainable:
#
#     notional = (equity x most it may lose on one position)
#                / how far this stock could fall before the stop
#
# On the owner's real trade: $2,000 x 2% = $40 of risk, divided by a 10%
# stop, is $400 - 20% of the account. Widen the stop and the number
# falls. That is the whole answer, and the page now says it.
#
# THIS PATH ALSO CRASHED THE WHOLE PAGE. `for ... note in st.limits`
# shadowed the module-level `note()` renderer, raising UnboundLocalError
# on the FIRST line of the story - and every test above passed anyway,
# because none of their fixtures had limit rows. Hence these.

LIMITS = [("per_stock_adverse_gap", "adaptive", "0.08", "0.08", 1),
          ("per_stock_stop_width", "adaptive", "0.10", "0.10", 1),
          ("max_loss_per_position", "hard", "0.10", "0.02", 0),
          ("max_hold_days", "hard", "12", "31", 0)]
GAP_NOTE = ("EMBC has gapped 45% overnight in its own history, at or "
            "beyond the insider_cluster assumption of 8%, so the category "
            "value stands")


def _seed_with_limits(tmp_path, equity="2000.00", limits=LIMITS):
    import sqlite3

    path = _seed(tmp_path)
    conn = sqlite3.connect(path)
    for rule, bt, req, bound, binds in limits:
        conn.execute("INSERT INTO limit_applications VALUES (?,?,?,?,?,?)",
                     ("d1", rule, bound, req, bt, binds))
    conn.execute("INSERT INTO limit_application_notes VALUES (?,?,?)",
                 ("d1", "per_stock_adverse_gap", GAP_NOTE))
    if equity:
        conn.execute("INSERT INTO equity_snapshots VALUES (?,?,?,?,?,?)",
                     ("2026-08-17", "2026-08-17T16:00:00+00:00", equity,
                      equity, "0", "broker_read"))
    conn.commit()
    conn.close()
    return path


class TestItExplainsTheSize:
    def test_the_page_still_renders_when_limits_exist(self, tmp_path):
        """The regression. A loop variable shadowed the note() renderer
        and took the entire page down, and no fixture above had limits."""
        html = _page(_seed_with_limits(tmp_path))
        assert "EMBC" in html and len(html) > 2000

    def test_it_states_the_share_of_the_account(self, tmp_path):
        """$400 of a $2,000 account is 20%. That is the number the owner
        asked about, so it is the number printed."""
        html = _page(_seed_with_limits(tmp_path))
        assert "20% of the" in html
        assert "$2,000.00 account" in html

    def test_it_gives_the_rule_in_words_not_just_a_formula(self, tmp_path):
        html = _page(_seed_with_limits(tmp_path))
        assert "most it may lose on a single position" in html
        assert "before the stop rescues it" in html
        assert "SMALLER position" in html, (
            "nothing explains why a wider stop means less money, which is "
            "the counter-intuitive half")

    def test_the_limit_that_decided_it_is_marked(self, tmp_path):
        html = _page(_seed_with_limits(tmp_path))
        assert "THIS ONE DECIDED IT" in html
        assert "did not bind" in html, (
            "limits that were checked and did not bind are hidden, so the "
            "reader cannot see what else was considered")

    def test_rule_names_are_translated(self, tmp_path):
        html = _page(_seed_with_limits(tmp_path))
        assert "how far this stock has gapped overnight before" in html
        assert "longest it may hold anything" in html
        assert "per_stock_adverse_gap" not in html, (
            "the raw machine name is shown instead of English")

    def test_the_per_stock_reasoning_is_shown(self, tmp_path):
        """"EMBC has gapped 45% overnight in its own history" is the
        difference between a number and an explanation."""
        html = _page(_seed_with_limits(tmp_path))
        assert "gapped 45% overnight in its own history" in html

    def test_no_equity_snapshot_means_no_invented_percentage(self, tmp_path):
        """A share of the account cannot be computed without the account.
        Better silent than wrong."""
        html = _page(_seed_with_limits(tmp_path, equity=""))
        assert "% of the" not in html
        assert "most it may lose on a single position" in html

    def test_nothing_binding_is_said_plainly(self, tmp_path):
        html = _page(_seed_with_limits(
            tmp_path, limits=[("max_hold_days", "hard", "12", "31", 0)]))
        assert "Nothing bound" in html


# ==========================================================================
# Second owner pass, once there was a trade to look at:
#
#   "Where it shows logic for each trade, its already uncollapses which
#    will get messy as there are many open and closed trades. Simplify
#    data maybe with prediction graphs, it feels word heavy and also the
#    data at the bottom appears to just be raw json not easily
#    understandable"
#
# Three separate complaints, three classes below.
# ==========================================================================


def _seed_two(tmp_path):
    """The real trade, plus a second one, so folding has something to
    fold. One position is a page nobody scrolls; the complaint is about
    what happens at a few trades a month."""
    path = _seed(tmp_path)
    conn = init_db(path)
    cid2 = "insider_cluster-ACME-2026-08-10-deadbeef"
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (cid2, "ACME", "insider_cluster", "2026-08-10", "confirmed",
                  "[]", "2026-08-10T16:00:00+00:00", "tech", "[]"))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 (cid2, "long", 0.71, "A different thesis entirely.",
                  "A different invalidation.", 9, 0, "why"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("d2", cid2, "trade", "long", "250.00", "10", "22.00",
                  "2026-08-24", "[]", "{}", "2026-08-10T16:00:00+00:00"))
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("e2", cid2, "b2", "buy", "10", "market", "day",
                  "2026-08-10T16:00:00+00:00", "filled", "{}"))
    conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?)",
                 ("e2", "25.00", "10", "2026-08-10T16:00:05+00:00",
                  "25.00", "0.5"))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 ("pos-2", "ACME", json.dumps(["e2"]), "s2",
                  "2026-08-10T16:00:00+00:00", "2026-08-24", "closed"))
    conn.execute("INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
                 ("pos-2", "paper", "25.00", "23.10", "hard_exit_date",
                  -1900, 9, 9, "2026-08-24T20:00:00+00:00"))
    conn.commit()
    conn.close()
    return path


class TestEachTradeFoldsShut:
    """OWNER-REPORTED: "its already uncollapsed which will get messy as
    there are many open and closed trades"."""

    def test_two_trades_are_both_folded(self, tmp_path):
        html = _page(_seed_two(tmp_path))
        assert html.count("<details class=\"trade\"") == 2
        assert "<details class=\"trade\" id=\"tr-t0\" open>" not in html, (
            "a trade is still open by default, so at a few trades a month "
            "the page becomes unnavigable - the reported complaint")

    def test_the_summary_alone_says_whether_to_open_it(self, tmp_path):
        """A fold is only useful if the closed state carries enough to
        decide. Ticker, state, size, date, conviction - the five things
        that answer "is this the one I am looking for"."""
        html = _page(_seed_two(tmp_path))
        head = html[html.index("<summary"):html.index("</summary>")]
        for fact in ("EMBC", "open", "$400.00", "2026-08-17", "0.60"):
            assert fact in head, f"the folded summary omits {fact}"

    def test_a_closed_trade_shows_its_result_while_folded(self, tmp_path):
        """The single most useful thing about a finished trade, and the
        reason to open it or not."""
        html = _page(_seed_two(tmp_path))
        summaries = re.findall(r"<summary.*?</summary>", html, re.S)
        acme = [s for s in summaries if "ACME" in s]
        assert acme and "lost" in acme[0] and "19.00" in acme[0], acme

    def test_a_lone_trade_is_NOT_folded(self, tmp_path):
        """Folding buys nothing when there is nothing to scroll past,
        and a page that opens showing nothing at all reads as broken."""
        html = _page(_seed(tmp_path))
        assert "<details class=\"trade\" id=\"tr-t0\" open>" in html

    def test_a_trade_asked_for_by_id_opens(self, tmp_path):
        html = _page(_seed_two(tmp_path), {"id": POS})
        assert " open>" in html, (
            "following a link to one specific trade lands on a shut box")

    def test_the_body_is_still_all_there(self, tmp_path):
        """Folded is not dropped. Everything the previous version said
        must survive inside the disclosure."""
        html = _page(_seed_two(tmp_path))
        for phrase in (THESIS[:50], INVALIDATION[:30],
                       "Claude never chooses the amount",
                       "Every order sent to the broker"):
            assert phrase in html


class TestTheRawJsonIsTranslated:
    """OWNER-REPORTED: "the data at the bottom appears to just be raw
    json not easily understandable". It was a broker response object,
    truncated mid-object at 220 characters, in a table cell."""

    def test_the_wash_trade_rejection_is_in_english(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "possible wash trade" in html
        assert "while a buy for the same stock is still working" in html

    def test_the_brokers_own_message_is_still_quoted(self, tmp_path):
        """The translation adds to the broker's words, never replaces
        them - the message is the authoritative part."""
        html = _page(_seed(tmp_path))
        assert "potential wash trade detected." in html

    def test_the_exact_response_is_kept_but_folded(self, tmp_path):
        """House rule 3: the raw response goes BESIDE the answer, not
        instead of it. It just stops being the largest thing on screen."""
        html = _page(_seed(tmp_path))
        assert "the broker&#x27;s exact response" in html \
            or "the broker's exact response" in html
        assert "reject_reason" in html, "the exact response was thrown away"
        assert 'class="raw-fold"' in html

    def test_it_is_no_longer_truncated_mid_object(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "opposite side market/stop order exists" in html, (
            "the response is still being cut off part-way through")

    def test_an_unknown_code_falls_back_to_the_brokers_words(self):
        """HOUSE RULE 7 - classify by the rule, not by enumeration. The
        code table is a convenience; a code nobody listed must still
        produce English, because the broker names its own reason."""
        said, exact = panels._broker_said(json.dumps(
            {"code": 99999999, "message": "account is restricted"}))
        assert "account is restricted" in said
        assert exact

    def test_a_nested_body_is_unwrapped(self):
        """Rejections arrive wrapped: {"submit_error":..,"body":{..}}."""
        said, _ = panels._broker_said(json.dumps(
            {"submit_error": "400", "status_code": 400,
             "body": json.dumps({"code": 40310000, "message": "no"})}))
        assert "wash trade" in said

    @pytest.mark.parametrize("bad", ["", "{oops", "null", "[]", "42", None])
    def test_unparseable_input_never_raises(self, bad):
        """This runs on stored text from a broker. It may be anything."""
        said, exact = panels._broker_said(bad)
        assert isinstance(said, str) and isinstance(exact, str)

    def test_an_empty_object_offers_no_fold(self):
        """"{}" is not a response worth a disclosure widget."""
        assert panels._broker_said("{}") == ("", "")

    def test_a_fill_is_said_as_a_fill(self):
        said, _ = panels._broker_said(json.dumps(
            {"status": "filled", "filled_qty": "79.1295",
             "filled_avg_price": "5.0600"}))
        assert "79.1295" in said and "5.06" in said


class TestThePictureReplacesTheParagraph:
    """OWNER-ASKED: "Simplify data maybe with prediction graphs, it feels
    word heavy"."""

    def test_the_stop_and_the_fill_are_drawn(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert 'class="rail-chart"' in html
        assert "rail-stop" in html and "rail-entry" in html
        assert "4.55" in html and "5.06" in html

    def test_the_exposure_is_drawn_as_a_band_not_two_ticks(self, tmp_path):
        """The width of that band IS the divisor the position size came
        out of. Seeing it is seeing the sizing."""
        html = _page(_seed(tmp_path))
        assert 'class="rail-risk"' in html
        m = re.search(r'<rect x="([\d.]+)"[^>]*width="([\d.]+)"[^>]*'
                      r'class="rail-risk"', html)
        assert m and float(m.group(2)) > 0, "the risk band has no width"

    def test_the_distance_to_the_stop_is_stated_as_a_percentage(self, tmp_path):
        """(5.06 - 4.55) / 5.06 = 10.08%."""
        html = _page(_seed(tmp_path))
        assert "10.1% below" in html

    def test_a_closed_trade_draws_where_it_was_sold(self, tmp_path):
        html = _page(_seed(tmp_path, closed=True))
        assert "rail-exit" in html
        assert "sold $5.62" in html

    def test_NOTHING_IS_FORECAST(self, tmp_path):
        """The one line this drawing must never cross. A "where it might
        go" projection would be a prediction the bot does not make,
        drawn with the same authority as a measured price - which is the
        thing this dashboard refuses to do everywhere else.

        Only prices that exist may be marked, so every number inside the
        SVG has to be one of them."""
        html = _page(_seed(tmp_path, closed=True))
        svg = html[html.index("<svg id=\"tr-t0-rail\""):]
        svg = svg[:svg.index("</svg>")]
        real = {"4.55", "5.06", "5.62"}
        drawn = set(re.findall(r"\$([\d.]+)", svg))
        assert drawn <= real, f"prices nobody paid are drawn: {drawn - real}"

    def test_every_label_stays_inside_the_drawing(self, tmp_path):
        """A label past the viewBox edge is invisible, and an invisible
        label is a number that silently is not there."""
        from catalyst.dashboard import charts

        html = _page(_seed(tmp_path, closed=True))
        svg = html[html.index("<svg id=\"tr-t0-rail\""):]
        svg = svg[:svg.index("</svg>") + 6]
        assert not charts.labels_outside_viewbox(svg)

    def test_a_missing_stop_draws_nothing_rather_than_guessing(self, tmp_path):
        """No stop price means no risk band. An empty chart area is
        better than one drawn from a value that was never recorded."""
        st = queries.TradeStory(ticker="X", entry_price="10", stop_price="")
        assert panels._price_rail(st, "tr", 0) == ""

    @pytest.mark.parametrize("entry,stop", [
        ("0", "1"), ("10", "0"), ("", "1"), ("abc", "1"), ("10", "nan"),
    ])
    def test_unusable_prices_never_raise(self, entry, stop):
        st = queries.TradeStory(ticker="X", entry_price=entry, stop_price=stop)
        assert panels._price_rail(st, "tr", 0) == ""
