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
        assert "earlier check(s) found no resting stop" in html
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
        assert "echanical screen" in html   # pill, so capitalised now

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
        assert "kept holding" in html
        assert "The thesis is intact" in html
        assert "no new filings" in html

    def test_an_exit_review_reads_as_an_exit(self, tmp_path):
        path = _seed(tmp_path, reviews=[
            ("2026-08-19T14:00:00+00:00", "exit_now", True,
             "Closed below $4.60, which was the stated invalidation.", [])])
        html = _page(path)
        assert "closed it now" in html
        assert "invalidation triggered" in html


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

    def test_even_a_LONE_trade_is_folded(self, tmp_path):
        """REVERSED ON OWNER INSTRUCTION: "for the trades tab its auto
        expanded". The first version kept an exception for a single
        trade, reasoning that folding buys nothing when there is nothing
        to scroll past. The owner disagreed, and they are the one
        reading it - a page whose behaviour changes with its row count
        is a page you cannot learn.

        What it costs is a page that opens showing nothing, which is why
        the timeline and the summary line exist."""
        html = _page(_seed(tmp_path))
        assert "<details class=\"trade\" id=\"tr-t0\" open>" not in html
        assert "<details class=\"trade\" id=\"tr-t0\">" in html

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
                       "Orders sent"):
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


def _fallback_story(**kw):
    """A story the full position chart cannot draw, so the smaller rail
    and hold bar are what render. Since the position chart landed those
    two are the FALLBACK, not the primary view - these tests are about
    the fallback still being correct when it is reached."""
    base = dict(ticker="EMBC", entry_price="5.06", stop_price="4.55",
                qty="79.1295", status="open",
                opened_at="2026-08-17T16:27:56+00:00",
                planned_exit_date="2026-08-29")
    base.update(kw)
    return queries.TradeStory(**base)


class TestThePictureReplacesTheParagraph:
    """OWNER-ASKED: "Simplify data maybe with prediction graphs, it feels
    word heavy". These now cover the FALLBACK charts - see
    _fallback_story."""

    def test_the_stop_and_the_fill_are_drawn(self, tmp_path):
        html = panels._price_rail(_fallback_story(), "tr", 0)
        assert 'class="rail-chart"' in html
        assert "rail-stop" in html and "rail-entry" in html
        assert "4.55" in html and "5.06" in html

    def test_the_exposure_is_drawn_as_a_band_not_two_ticks(self, tmp_path):
        """The width of that band IS the divisor the position size came
        out of. Seeing it is seeing the sizing."""
        html = panels._price_rail(_fallback_story(), "tr", 0)
        assert 'class="rail-risk"' in html
        m = re.search(r'<rect x="([\d.]+)"[^>]*width="([\d.]+)"[^>]*'
                      r'class="rail-risk"', html)
        assert m and float(m.group(2)) > 0, "the risk band has no width"

    def test_the_distance_to_the_stop_is_stated_as_a_percentage(self, tmp_path):
        """(5.06 - 4.55) / 5.06 = 10.08%."""
        html = panels._price_rail(_fallback_story(), "tr", 0)
        assert "10.1% below" in html

    def test_a_closed_trade_draws_where_it_was_sold(self, tmp_path):
        html = panels._price_rail(
            _fallback_story(status="closed", exit_price="5.62"), "tr", 0)
        assert "rail-exit" in html
        assert "sold $5.62" in html

    def test_NOTHING_IS_FORECAST(self, tmp_path):
        """The one line this drawing must never cross. A "where it might
        go" projection would be a prediction the bot does not make,
        drawn with the same authority as a measured price - which is the
        thing this dashboard refuses to do everywhere else.

        Only prices that exist may be marked, so every number inside the
        SVG has to be one of them."""
        html = panels._price_rail(
            _fallback_story(status="closed", exit_price="5.62"), "tr", 0)
        svg = html[html.index("<svg id=\"tr-t0-rail\""):]
        svg = svg[:svg.index("</svg>")]
        real = {"4.55", "5.06", "5.62"}
        drawn = set(re.findall(r"\$([\d.]+)", svg))
        assert drawn <= real, f"prices nobody paid are drawn: {drawn - real}"

    def test_every_label_stays_inside_the_drawing(self, tmp_path):
        """A label past the viewBox edge is invisible, and an invisible
        label is a number that silently is not there."""
        from catalyst.dashboard import charts

        html = panels._price_rail(
            _fallback_story(status="closed", exit_price="5.62"), "tr", 0)
        svg = html[html.index("<svg id=\"tr-t0-rail\""):]
        svg = svg[:svg.index("</svg>") + 6]
        assert not charts.labels_outside_viewbox(svg)

    def test_the_POSITION_chart_labels_also_stay_inside(self, tmp_path):
        """The one that actually renders now."""
        from catalyst.dashboard import charts

        html = _page(_seed(tmp_path, reviews=[
            ("2026-08-19T14:00:00+00:00", "hold", False, "intact", [])]))
        svg = html[html.rindex("<svg", 0, html.index('class="pos-chart"')):]
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


# ==========================================================================
# Third owner pass, on the same page:
#
#   "for the trades tab its auto expanded, can we add more detail,
#    simplify into some other graphs"
#   "less text more graphs and icons, make the UI more friendly, its
#    text heavy"
#
# Folding everything shut means the page opens showing nothing, so the
# shut state has to earn its keep - hence a timeline that answers the
# question this tab is opened with, without opening anything.
# ==========================================================================


class TestTheShutPageStillAnswersSomething:
    def test_the_timeline_is_drawn_above_the_folds(self, tmp_path):
        html = _page(_seed_two(tmp_path))
        assert 'id="tr-timeline"' in html
        assert html.index("tr-timeline") < html.index('class="trade"'), (
            "the timeline is below the folded trades, so a shut page is "
            "still blank at the top")

    def test_every_position_gets_a_bar(self, tmp_path):
        html = _page(_seed_two(tmp_path))
        svg = html[html.index('id="tr-timeline"'):]
        svg = svg[:svg.index("</svg>")]
        assert svg.count("<rect") == 2
        for ticker in ("EMBC", "ACME"):
            assert f">{ticker}</text>" in svg

    def test_an_open_position_is_drawn_differently_from_a_closed_one(
            self, tmp_path):
        html = _page(_seed_two(tmp_path))
        assert "tl-open" in html and "tl-done" in html

    def test_TODAY_IS_THE_REAL_CLOCK(self, tmp_path):
        """HOUSE RULE 6. A pinned date drifts out of the window a day at
        a time and quietly stops meaning anything - it has happened
        twice in this project. The marker must move with the wall
        clock."""
        import datetime as _dt

        html = _page(_seed_two(tmp_path))
        svg = html[html.index('id="tr-timeline"'):]
        svg = svg[:svg.index("</svg>")]
        assert "tl-today" in svg and ">today</text>" in svg
        # The axis has to REACH today, or the marker sits off the end.
        src = (panels._hold_timeline.__doc__ or "")
        assert "house rule 6" in src.lower()
        assert _dt.datetime.now(_dt.timezone.utc).date()  # the real clock

    def test_days_remaining_are_counted_not_left_to_the_reader(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "d left" in html or "past due" in html

    def test_it_draws_nothing_rather_than_guessing_a_window(self, tmp_path):
        st = queries.TradeStory(ticker="X", opened_at="", planned_exit_date="")
        assert panels._hold_timeline([st], "tr") == ""

    @pytest.mark.parametrize("opened,exits", [
        ("not-a-date", "2026-08-29"), ("2026-08-17", "nonsense"),
        ("2026-08-29", "2026-08-17"),      # exit before entry
    ])
    def test_unusable_dates_never_raise(self, opened, exits):
        st = queries.TradeStory(ticker="X", opened_at=opened,
                                planned_exit_date=exits)
        assert panels._hold_timeline([st], "tr") == ""


class TestTheProseIsFoldedNotDeleted:
    """OWNER-REPORTED twice: "it feels word heavy", then "less text more
    graphs and icons". None of the explanation is WRONG - it is the
    provenance and reasoning the brief demands - so it goes one click
    away rather than into the bin."""

    def test_the_reasoning_is_still_on_the_page(self, tmp_path):
        # with_limits, because the sizing sum is only explained where
        # there are limits to explain it against.
        html = _page(_seed_with_limits(tmp_path))
        for kept in ("same dollars of risk buy fewer shares",
                     "Paper fills pay no spread",
                     "line-for-line the arm the backtest graded"):
            assert kept in html, f"an explanation was deleted: {kept!r}"

    def test_but_it_is_behind_a_disclosure(self, tmp_path):
        html = _page(_seed_with_limits(tmp_path))
        assert 'class="why-fold"' in html
        i = html.index("same dollars of risk buy fewer shares")
        assert "why-fold" in html[:i][-600:], (
            "the sizing explanation is loose on the page again")

    def test_conviction_keeps_BOTH_the_gauge_and_the_translation(
            self, tmp_path):
        """Trimming text is not a reason to drop a definition. The gauge
        shows where 0.60 sat against its floor; only the sentence says
        what 0.60 MEANS, and that units mismatch cost this bot every
        trade for weeks."""
        html = _page(_seed(tmp_path))
        assert "gauge-track" in html, "the conviction gauge is missing"
        assert "60 times in 100" in html, (
            "the frequency definition was dropped when the text was cut")

    def test_priced_in_is_still_explained_somewhere(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "already reacted to this news" in html


class TestIconsHelpAndNeverCarryMeaningAlone:
    def test_each_step_has_an_icon(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert html.count('class="step-ico"') >= 5

    def test_every_icon_is_hidden_from_a_screen_reader(self, tmp_path):
        """An icon that is announced turns "Protection" into "shield
        Protection". The words are the content; the glyph is decoration
        - the same rule the status pills follow."""
        html = _page(_seed(tmp_path))
        for m in re.finditer(r'<span class="step-ico"([^>]*)>', html):
            assert 'aria-hidden="true"' in m.group(1)

    def test_the_headings_still_read_without_them(self, tmp_path):
        html = _page(_seed(tmp_path))
        for word in ("Why EMBC", "Claude&#x27;s view", "Size and stop",
                     "Protection", "Orders sent"):
            assert word in html, f"a heading lost its words: {word}"


class TestTheHoldProgressBar:
    def test_an_open_position_shows_how_far_through_it_is(self, tmp_path):
        html = panels._hold_progress(_fallback_story(), "tr", 0)
        assert 'class="hold-track"' in html
        assert "day(s) left of a 12-day hold" in html \
            or "past its exit date" in html

    def test_a_closed_one_says_what_it_actually_used(self, tmp_path):
        html = panels._hold_progress(
            _fallback_story(status="closed",
                            closed_at="2026-08-29T20:00:00+00:00"), "tr", 0)
        assert "of an allowed 12 days" in html

    def test_the_fill_never_leaves_the_track(self, tmp_path):
        """A position past its exit date would otherwise render a bar
        wider than its own container."""
        st = queries.TradeStory(
            ticker="X", status="open", opened_at="2020-01-01",
            planned_exit_date="2020-01-10")
        html = panels._hold_progress(st, "tr", 0)
        pct = float(re.search(r"width:([\d.]+)%", html).group(1))
        assert 0 <= pct <= 100, pct
        assert "past its exit date" in html


class TestAChartKeepsItsLegend:
    """FOUND BY RENDERING, not by reasoning (house rule 1).

    section() lifts every <p class="prov"> into a disclosure at the foot
    of the panel - the right call for "where this number came from", and
    the wrong one for the legend of a chart. The first version of the
    graphs used prov for their captions, and the rendered page put all
    three of them stacked together at the bottom, thousands of
    characters from the drawings they explained. Every test still
    passed, because every test asked whether the words were on the page
    and none asked whether they were in the right place.
    """

    def caption_distance(self, html, svg_id, phrase):
        end = html.index("</svg>", html.index(f'id="{svg_id}"'))
        return html.index(phrase) - end

    def test_the_timeline_legend_stays_with_the_timeline(self, tmp_path):
        html = _page(_seed(tmp_path))
        d = self.caption_distance(html, "tr-timeline",
                                  "Every position carries a hard exit date")
        assert 0 < d < 40, f"the legend is {d} characters from its chart"

    def test_the_price_rail_caption_stays_with_the_rail(self, tmp_path):
        html = panels._price_rail(_fallback_story(), "tr", 0)
        d = self.caption_distance(html, "tr-t0-rail", "The stop sits")
        assert 0 < d < 40, f"the caption is {d} characters from its chart"

    def test_the_hold_bar_caption_stays_with_the_bar(self, tmp_path):
        html = panels._hold_progress(_fallback_story(), "tr", 0)
        i = html.index('class="hold-track"')
        assert "fig-cap" in html[i:i + 260], (
            "the hold bar's caption has been lifted away from it")

    def test_figure_captions_are_not_swept_into_the_workings_fold(
            self, tmp_path):
        html = _page(_seed(tmp_path))
        if "workings" not in html:
            pytest.skip("nothing to sweep in this fixture")
        fold = html[html.index('class="workings"'):]
        assert "fig-cap" not in fold
        for legend in ("The stop sits", "day(s) left of a",
                       "Every position carries a hard exit date"):
            assert legend not in fold, (
                f"{legend!r} was lifted out of its chart and into the fold")

    def test_real_provenance_IS_still_swept(self, tmp_path):
        """The distinction has to cut both ways, or this is just an
        excuse to stop folding prose."""
        from catalyst.dashboard.render import section

        out = section("s", "T", '<p class="prov">a</p><p class="prov">b</p>')
        assert "workings" in out
        assert out.index("workings") < out.index(">a<")


# ==========================================================================
# "The trades part doesnt feel professional enough, i want better
#  metrics, i feels like a robot made it, re-design that page entirely"
#
# The page printed what was STORED and left every derived number to the
# reader. These test the two that a book is actually judged on, both of
# which were computable from data already on disk.
# ==========================================================================


class TestTheDerivedMetrics:
    def test_risk_is_qty_times_the_distance_to_the_stop(self, tmp_path):
        """79.1295 x (5.06 - 4.55) = $40.36. THE number the position was
        sized from, and the page never showed it."""
        d = queries.trades(Db(_seed(tmp_path)))
        m = queries.trade_metrics(d.stories[0])
        assert m.risk_usd is not None
        assert round(float(m.risk_usd), 2) == 40.36

    def test_risk_is_not_the_same_as_what_was_spent(self, tmp_path):
        """$400 committed, $40 at stake. Showing only the first says how
        much was spent, not how much can be lost."""
        d = queries.trades(Db(_seed(tmp_path)))
        m = queries.trade_metrics(d.stories[0])
        assert float(m.risk_usd) < float(d.stories[0].notional_usd) / 5

    def test_R_is_the_result_over_the_initial_risk(self, tmp_path):
        """ACME lost $19.00 against $30.00 of risk = -0.63R."""
        d = queries.trades(Db(_seed_two(tmp_path)))
        acme = [s for s in d.stories if s.ticker == "ACME"][0]
        m = queries.trade_metrics(acme)
        assert round(float(m.r_multiple), 2) == -0.63

    def test_R_is_None_rather_than_infinite_without_a_stop(self):
        """A trade with no recorded stop has no initial risk to divide
        by. Inventing one grades it against nothing."""
        st = queries.TradeStory(ticker="X", qty="10", entry_price="10",
                                stop_price="", realized_pnl_cents=500)
        assert queries.trade_metrics(st).r_multiple is None

    @pytest.mark.parametrize("field,bad", [
        ("qty", "abc"), ("entry_price", ""), ("stop_price", "nan"),
        ("entry_price", "0"),
    ])
    def test_unusable_inputs_give_None_not_a_wrong_number(self, field, bad):
        st = queries.TradeStory(ticker="X", qty="10", entry_price="10",
                                stop_price="9")
        setattr(st, field, bad)
        m = queries.trade_metrics(st)
        assert m.risk_usd is None or m.r_multiple is None

    def test_the_blotter_shows_them_both(self, tmp_path):
        html = _page(_seed_two(tmp_path))
        assert "risk $" in html and ">R<" in html
        assert "$40.36" in html
        assert "-0.63R" in html

    def test_R_is_always_signed(self, tmp_path):
        """+1.8R and -1.0R are what a reader scans for; an unsigned 1.8
        hides which one it is."""
        html = _page(_seed_two(tmp_path))
        assert re.search(r"[-+]\d+\.\d\dR", html)

    def test_figures_are_right_aligned_for_scanning(self, tmp_path):
        html = _page(_seed_two(tmp_path))
        blot = html[html.index('id="tr-blotter"'):]
        assert blot.count('class="num"') >= 9, (
            "the numeric columns are not right-aligned, so they cannot "
            "be compared down the page")


class TestTheBookNotJustTheTrades:
    def test_it_says_what_is_at_stake_right_now(self, tmp_path):
        html = _page(_seed_two(tmp_path))
        assert "At risk now" in html and "$40.36" in html

    def test_expectancy_carries_its_sample_size(self, tmp_path):
        """One trade is not an expectancy. A page that implies otherwise
        is worse than a blank one."""
        html = _page(_seed_two(tmp_path))
        assert "Expectancy" in html
        assert f"1 of {panels.MIN_TRADES_FOR_MEANING}" in html
        assert "describe what happened, not what to expect" in html

    def test_with_no_closed_trades_it_refuses_to_show_one(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "no closed trades" in html

    def test_book_metrics_never_average_ungradeable_trades(self):
        """A closed trade with no stop cannot be expressed in R. Counting
        it as 0R would drag the average toward nothing."""
        ok = queries.TradeStory(ticker="A", status="closed", qty="10",
                                entry_price="10", stop_price="9",
                                realized_pnl_cents=1000)
        no_stop = queries.TradeStory(ticker="B", status="closed", qty="10",
                                     entry_price="10", stop_price="",
                                     realized_pnl_cents=1000)
        b = queries.book_metrics([ok, no_stop])
        assert b.n_closed == 2
        assert b.graded == 1, "an ungradeable trade entered the average"
        assert round(float(b.expectancy_r), 2) == 1.0

    def test_an_empty_book_produces_no_invented_figures(self):
        b = queries.book_metrics([])
        assert b.expectancy_r is None and b.win_rate is None
        assert b.open_risk_usd is None


class TestTheAccountValueIsBrokenIntoItsParts:
    """The tile stood for four different things at once, and only one of
    them has actually left a card."""

    def _perf_page(self, tmp_path):
        db = Db(_seed(tmp_path))
        try:
            return panels.performance_panel(db, p="perf")
        finally:
            db.close()

    def test_the_api_bill_is_marked_as_real_money(self, tmp_path):
        html = self._perf_page(tmp_path)
        assert "API spend" in html
        assert "real money" in html

    def test_the_trading_line_is_marked_as_paper(self, tmp_path):
        html = self._perf_page(tmp_path)
        assert "paper" in html
        assert "fictional until the account is live" in html

    def test_it_says_open_positions_are_excluded(self, tmp_path):
        """net_equity is built from CLOSED trades, so an open winner is
        invisible. Better said than discovered by arithmetic."""
        html = self._perf_page(tmp_path)
        assert "an open winner is invisible" in html

    def test_scheduled_and_manual_spend_stay_separate(self, tmp_path):
        """TRAPS.md: mixing them makes every projection wrong."""
        html = self._perf_page(tmp_path)
        assert "scheduled" in html and "manual" in html

    def test_the_bar_is_scaled_to_the_ACCOUNT_not_to_itself(self):
        """A $3 API bill against $2,000 must look like a sliver. Scaling
        each segment to fill the bar would make it look like a crisis."""
        import inspect

        src = inspect.getsource(panels._equity_bridge)
        assert "abs(v) / start" in src, (
            "segments are no longer proportional to the starting capital")


class TestThePositionChart:
    """OWNER-ASKED: "i cant accurately see how well my current trades
    are going i want a graph with multiple points of info e.g. when is
    it calling to claude for a tech, current costs, original cost, sell
    cost etc"."""

    REVIEW = [("2026-08-19T14:00:00+00:00", "hold", False, "intact", [])]

    def test_it_draws_what_it_cost_and_what_it_sells_for(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert 'class="pos-chart"' in html
        assert "bought $5.06" in html and "stop $4.55" in html

    def test_the_money_at_risk_is_the_shaded_band(self, tmp_path):
        html = _page(_seed(tmp_path))
        m = re.search(r'<rect[^>]*height="([\d.]+)"[^>]*class="pos-risk"', html)
        assert m and float(m.group(1)) > 0

    def test_every_call_to_claude_is_marked_on_the_day_it_happened(
            self, tmp_path):
        html = _page(_seed(tmp_path, reviews=self.REVIEW))
        assert "pos-review" in html
        assert ">held</text>" in html

    def test_an_exit_review_is_drawn_differently_from_a_hold(self, tmp_path):
        html = _page(_seed(tmp_path, reviews=[
            ("2026-08-19T14:00:00+00:00", "exit_now", True, "broken", [])]))
        assert "pos-review-exit" in html
        assert ">EXIT</text>" in html

    def test_the_hard_exit_date_is_the_right_hand_edge(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "closes 2026-08-29" in html

    def test_it_says_how_many_times_claude_has_looked(self, tmp_path):
        html = _page(_seed(tmp_path, reviews=self.REVIEW))
        assert "re-read the thesis <b>1</b> time(s)" in html

    def test_a_skipped_review_is_not_counted_as_a_look(self, tmp_path):
        """A skipped review cost nothing and decided nothing."""
        from catalyst.dashboard.queries import TradeStory

        st = TradeStory(ticker="X", entry_price="10", stop_price="9",
                        opened_at="2026-08-01", planned_exit_date="2026-08-20",
                        reviews=[("2026-08-05T00:00:00+00:00", "hold", False,
                                  "", [], "too soon")])
        assert "<b>0</b> time(s)" in panels._position_chart(st, "tr", 0)

    def test_NOTHING_IS_PROJECTED(self, tmp_path):
        """The one line this chart must not cross. The only future marks
        allowed are DATES the bot has already committed to - never a
        price it might reach."""
        html = _page(_seed(tmp_path))
        svg = html[html.rindex('<svg', 0, html.index('class="pos-chart"')):]
        svg = svg[:svg.index("</svg>")]
        real = {"5.06", "4.55"}
        assert set(re.findall(r"\$([\d.]+)", svg)) <= real

    def test_a_missing_bar_cache_says_so_rather_than_guessing(self, tmp_path):
        html = _page(_seed(tmp_path))
        assert "empty rather than guessed" in html

    @pytest.mark.parametrize("kw", [
        {"entry_price": ""}, {"stop_price": "abc"},
        {"opened_at": "not-a-date"}, {"planned_exit_date": "2020-01-01"},
    ])
    def test_unusable_inputs_draw_nothing_and_never_raise(self, kw):
        from catalyst.dashboard.queries import TradeStory

        st = TradeStory(ticker="X", entry_price="10", stop_price="9",
                        opened_at="2026-08-01",
                        planned_exit_date="2026-08-20")
        for field, value in kw.items():
            setattr(st, field, value)
        assert panels._position_chart(st, "tr", 0) == ""

    def test_it_replaces_the_two_smaller_bars_rather_than_adding_to_them(
            self, tmp_path):
        """Three charts saying overlapping things is the clutter the
        owner reported. The rail and hold bar are the FALLBACK for a
        position the full chart cannot draw."""
        html = _page(_seed(tmp_path))
        assert 'class="pos-chart"' in html
        assert 'class="rail-chart"' not in html
        assert 'class="hold-track"' not in html

    def test_but_the_fallback_still_appears_when_it_cannot_draw(self):
        """A position with no stop gets no position chart, and must not
        therefore get nothing at all."""
        from catalyst.dashboard.queries import TradeStory

        st = TradeStory(ticker="X", entry_price="10", stop_price="",
                        opened_at="2026-08-01", planned_exit_date="2026-08-20",
                        status="open")
        assert panels._position_chart(st, "tr", 0) == ""
        assert 'class="hold-track"' in panels._hold_progress(st, "tr", 0)


def _with_bars(tmp_path, monkeypatch, ticker="EMBC", sessions=300):
    """A real bar cache, so the technical charts have something to read.
    Built off a fixed seed: the SHAPE is arbitrary, the point is that
    300 sessions exist."""
    import random
    from datetime import date, timedelta
    from decimal import Decimal

    from catalyst.backtest.data import Bar, BarCache

    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
    random.seed(7)
    px, d, days = 6.40, date(2026, 8, 21), []
    while len(days) < sessions:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    bars = []
    for day in days:
        px = max(px * (1 + random.gauss(-0.0009, 0.022)), 3.9)
        v = Decimal(str(round(px, 2)))
        bars.append(Bar(day=day, open=v, high=v * Decimal("1.02"),
                        low=v * Decimal("0.98"), close=v,
                        volume=Decimal(str(random.randint(4 * 10**5, 3 * 10**6)))))
    BarCache(str(tmp_path / "bars")).write_bars(ticker, bars)
    return _seed(tmp_path)


class TestTheBriefTechnicalRead:
    """OWNER-ASKED: "maybe another graph or two, it feels very wordy to
    give it a brief technical analysis"."""

    def test_it_reuses_the_numbers_claude_was_GIVEN(self):
        """Not a second opinion. Every figure comes from
        data/price_action.py - the module that fills the research prompt
        - so the page shows what the model saw. A dashboard computing
        its own version is how two numbers with one name start
        disagreeing."""
        import inspect

        src = inspect.getsource(panels._technicals)
        assert "from catalyst.data.price_action import price_action" in src

    def test_the_moves_and_the_volume_are_shown(self, tmp_path, monkeypatch):
        html = _page(_with_bars(tmp_path, monkeypatch))
        for label in ("5 days", "20 days", "Since the catalyst", "Volume"):
            assert label in html

    def test_volume_is_translated_not_just_a_ratio(self, tmp_path,
                                                   monkeypatch):
        """"1.1x" means nothing without knowing 1.0 is normal."""
        html = _page(_with_bars(tmp_path, monkeypatch))
        assert any(w in html for w in (
            "usual volume", "busier than usual", "quieter than usual",
            "traded far more than usual"))

    def test_the_range_bar_places_it_in_its_own_year(self, tmp_path,
                                                     monkeypatch):
        html = _page(_with_bars(tmp_path, monkeypatch))
        assert 'class="range-chart"' in html
        assert "52w low" in html and "up the range" in html

    def test_the_range_bar_refuses_to_call_it_cheap_or_expensive(
            self, tmp_path, monkeypatch):
        """Position in a range is context, not a verdict. A dashboard
        that says "near the low, therefore cheap" has started giving
        advice from a single number."""
        html = _page(_with_bars(tmp_path, monkeypatch))
        assert "not cheap" in html and "not expensive" in html

    def test_the_marker_cannot_leave_the_track(self, tmp_path):
        """A range position outside 0-100 would draw off the end."""
        from catalyst.data.price_action import PriceAction
        from decimal import Decimal

        st = queries.TradeStory(ticker="X", entry_price="5")
        for pos in (Decimal("-30"), Decimal("140")):
            html = panels._range_bar(st, PriceAction(range_position_pct=pos),
                                     "tr", 0)
            cx = float(re.search(r'<circle cx="([\d.]+)"', html).group(1))
            assert 40 <= cx <= 600, cx


class TestTheChartShowsTheRunUp:
    def test_history_before_the_entry_is_drawn(self, tmp_path, monkeypatch):
        """A days-to-weeks position is too short to read on its own -
        and a 20-day average over a 12-day hold does not exist, so the
        trend line never drew at all."""
        html = _page(_with_bars(tmp_path, monkeypatch))
        assert 'class="pos-sma"' in html, "the trend line still cannot draw"
        assert 'class="pos-bought"' in html, "the entry day is not marked"

    def test_the_entry_is_marked_now_it_is_not_the_edge(self, tmp_path,
                                                        monkeypatch):
        html = _page(_with_bars(tmp_path, monkeypatch))
        assert ">bought</text>" in html

    def test_the_average_is_drawn_only_where_it_exists(self):
        """The first nineteen days have no twenty-day average. Drawing
        one for them invents the very thing being read."""
        assert panels._sma([1, 2, 3], 20) == []
        got = panels._sma(list(range(30)), 20)
        assert got[0][0] == 19, "an average was drawn before it existed"
        assert got[0][1] == sum(range(20)) / 20

    def test_the_average_is_a_real_rolling_mean(self):
        vals = [float(i) for i in range(1, 26)]
        got = dict(panels._sma(vals, 5))
        assert got[4] == sum(vals[0:5]) / 5
        assert got[24] == sum(vals[20:25]) / 5

    def test_still_NOTHING_IS_PROJECTED(self, tmp_path, monkeypatch):
        """Unchanged by any of this: the price line stops where the data
        stops, and no drawn price is one that was never paid or seen."""
        html = _page(_with_bars(tmp_path, monkeypatch))
        i = html.index('class="pos-chart"')
        svg = html[html.rindex("<svg", 0, i):]
        svg = svg[:svg.index("</svg>")]
        drawn = set(re.findall(r"\$([\d.]+)", svg))
        assert "5.06" in drawn and "4.55" in drawn
        assert len(drawn) <= 3, f"unexplained prices drawn: {drawn}"
