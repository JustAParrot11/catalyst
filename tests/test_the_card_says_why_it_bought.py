"""The summary said the arm's name where the reason belongs.

OWNER-REPORTED 2026-09-14, on the RLMD card - the first real trade:

    "was there a fault? should it not of traded RLMD? - the info on the
    dashboard makes me quite clueless as to the reason the supposed
    agentically thinking bot traded it and what the driving factors
    were"

THERE WAS NO FAULT. Reproduced from the owner's own bundle: 15 of 15
maintenance checks ok, zero recorded errors, the funnel's `blame_stage`
empty, and every figure on the card agreeing with its rows.

WHAT WAS WRONG IS THAT THE CARD COULD NOT SAY WHY. `_trade_summary` -
the paragraph written to be "the FIRST thing on the card" - built its
reason from a TWO-ENTRY LOOKUP TABLE keyed on `catalyst_type`:

    because = {"insider_cluster": "several insiders were buying it",
               "earnings_drift":  "it beat on earnings and kept drifting"}

So the whole answer to "why did it buy this" was seven words naming the
screen, identical on every insider-cluster trade the bot will ever make,
while the model's actual reasoning - a CMO departure read as an
overreaction, the stock 12% under its 50-day MA, $1.67M from the two
most senior officers, thin volume meaning slow digestion - sat in
section 4, below the chart and two folds.

And it made the owner's OTHER conclusion look true when it is not. The
card said "insiders were buying" and nothing else, so it read as a
mechanical screen's output; in fact Claude ran three web searches of its
own on this candidate and the thesis turns on what they found. The
searches were recorded from the first day and shown only in a fold.

Seventh instance of this project's most recurring defect (sections 12,
14, 17, 22, 23, 26): THE FACT WAS ALREADY ON `TradeStory` - `thesis`,
`priced_in`, `searches` - one caller away from the sentence that needed
it.
"""

import json

import pytest

from catalyst.dashboard import panels
from catalyst.dashboard.db import Db
from catalyst.storage import init_db

CID = "insider_cluster-RLMD-2026-09-11-8b3d3f529bfb"
DID = "baaefc24-abd3-45b2-b08e-42936c1c8526"
POS = "rlmd-position-0001"
ENTRY = "rlmd-entry-0001"
STOP = "rlmd-stop-0001"

#: VERBATIM from the owner's bundle for 2026-09-14. The length matters:
#: this is why the summary carries an excerpt rather than the whole
#: thing, and the first 240 characters are the concrete claim.
THESIS = (
    "CEO Traversa ($828K) and CFO Shenouda ($843,990) both bought RLMD "
    "stock on the open market on Sept 9-10 at $4.15-$4.25, right after the "
    "stock had fallen from its 50-day MA of $5.10 and 200-day MA of $6.05 "
    "following a Chief Medical Officer (urology) departure disclosed around "
    "Sept 1-2 and lingering concerns about NDV-01 manufacturing delays "
    "flagged even by bullish analysts (Mizuho Outperform, $19 PT).")
INVALIDATION = ("RLMD closing below $3.80 would indicate the market is "
                "rejecting the buy-the-dip thesis.")
PRICED_IN_WHY = ("The stock has moved from the insiders' average purchase "
                 "price (~$4.20) to the current last close of $4.495.")
QUERIES = [
    "RLMD Relmada Therapeutics CEO CFO insider buying September 2026",
    "RLMD Relmada Therapeutics stock news September 2026",
    "Relmada Therapeutics Chief Medical Officer exit Pruthi September 2026",
]


def _raw_response(queries):
    """A turn shaped like the real one: server_tool_use per search."""
    content = []
    for q in queries:
        content.append({"type": "server_tool_use", "name": "web_search",
                        "id": f"srv-{len(content)}", "input": {"query": q}})
    content.append({"type": "tool_use", "name": "submit_research_view",
                    "id": "t1", "input": {}})
    return json.dumps({"content": content})


def _seed(tmp_path, *, thesis=THESIS, catalyst_type="insider_cluster",
          queries=QUERIES, priced_in=0, conviction=0.58, name="rlmd"):
    path = str(tmp_path / f"{name}.db")
    conn = init_db(path)
    # The fixture goes through init_db on purpose: section 14's lesson -
    # a raw sqlite3 connect leaves PRAGMA foreign_keys OFF, and a fixture
    # that can hold impossible rows agrees with a wrong query.
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (CID, "RLMD", catalyst_type, "2026-09-11", "confirmed",
                  "[]", "2026-09-11T22:00:00+00:00", "health", "[]"))
    conn.execute("INSERT INTO candidate_origin VALUES (?,?,?,?)",
                 (CID, "screen", None, "2026-09-11T22:00:00+00:00"))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 (CID, "long", conviction, thesis, INVALIDATION, 15,
                  priced_in, PRICED_IN_WHY))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (DID, CID, "trade", "long", "398.62", "88.6812", "4.05",
                  "2026-09-29", "[]", "{}", "2026-09-14T13:57:03+00:00"))
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (ENTRY, CID, "brk-1", "buy", "88.6812", "market", "day",
                  "2026-09-14T13:57:03+00:00", "filled", "{}"))
    conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (STOP, CID, "brk-2", "sell", "88.6812", "stop", "day",
                  "2026-09-14T13:57:20+00:00", "new", "{}"))
    conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?)",
                 (ENTRY, "4.4949", "88.6812", "2026-09-14T13:57:05+00:00",
                  "4.4949", "0.40"))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 (POS, "RLMD", json.dumps([ENTRY]), STOP,
                  "2026-09-14T13:57:03+00:00", "2026-09-29", "open"))
    conn.execute("INSERT INTO stop_confirmations VALUES (?,?,?,?)",
                 (POS, "2026-09-14T14:10:00+00:00", json.dumps([STOP]), "ok"))
    conn.execute(
        "INSERT INTO research_calls (id,candidate_id,model,prompt_rendered,"
        "tools_offered,cost_cents,latency_ms,skipped_reason,called_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("call-1", CID, "m", "p", json.dumps(["web_search"]), "13.657",
         58026, None, "2026-09-14T13:57:02+00:00"))
    conn.execute(
        "INSERT INTO research_call_turns (call_id,turn_index,raw_response,"
        "usage_raw,stop_reason) VALUES (?,?,?,?,?)",
        ("call-1", 0, _raw_response(queries), "{}", "tool_use"))
    conn.commit()
    conn.close()
    return path


def _summary(path):
    """Just the opening paragraph - the thing the owner reads."""
    db = Db(path)
    try:
        html = panels.trades_panel(db, {}, p="tr")
    finally:
        db.close()
    i = html.index('class="trade-sum"')
    start = html.index(">", i) + 1
    return html[start:html.index("</p>", start)]


class TestTheOwnersQuestion:
    """"the reason ... and what the driving factors were" - answered in
    the paragraph, not four sections down."""

    def test_the_summary_carries_claudes_own_reasoning(self, tmp_path):
        s = _summary(_seed(tmp_path))
        assert "in Claude's own words" in s, (
            "the summary still does not quote the model's reasoning:\n" + s)
        # The DRIVING FACTORS, not a category. These are the model's own
        # first concrete claims and none of them is derivable from the
        # catalyst type.
        for fact in ("CEO Traversa", "$843,990", "50-day MA"):
            assert fact in s, f"{fact!r} missing from the summary:\n{s}"

    def test_the_arm_name_is_no_longer_offered_as_the_reason(self, tmp_path):
        s = _summary(_seed(tmp_path))
        assert "because several insiders were buying it" not in s, (
            "the two-entry lookup table is back: the screen's name is "
            "standing in for the model's reasoning again")

    def test_it_says_claude_did_its_own_research(self, tmp_path):
        """The answer to "is this actually agentic" was in a fold."""
        s = _summary(_seed(tmp_path))
        assert "3 web searches" in s, (
            "the summary does not say the model searched for itself:\n" + s)

    def test_it_says_whether_the_move_was_priced_in(self, tmp_path):
        """276 of 302 views were declined FOR being priced in, so a
        trade is one of the few where the model said the move was still
        there. That belongs beside the conviction."""
        assert "not yet priced in" in _summary(
            _seed(tmp_path, name="open_move"))
        assert "already partly priced in" in _summary(
            _seed(tmp_path, priced_in=1, name="priced"))


class TestTheExcerptIsHonest:
    """A fragment must never be presented as the whole thought."""

    def test_a_clipped_reason_says_it_is_clipped(self, tmp_path):
        s = _summary(_seed(tmp_path))
        assert "&hellip;" in s or "..." in s, (
            "a clipped thesis is shown with no sign it was clipped:\n" + s)
        assert "What Claude concluded" in s, (
            "nothing points the reader at the full reasoning")

    def test_a_short_reason_is_quoted_WHOLE_with_no_ellipsis(self, tmp_path):
        short = "Two officers bought $1.67M inside two days."
        s = _summary(_seed(tmp_path, thesis=short))
        assert short in s, s
        assert "&hellip;" not in s, (
            "a thesis that fits was still marked as an excerpt:\n" + s)
        assert "the rest is under" not in s

    def test_the_excerpt_is_always_a_true_prefix_of_the_thesis(self):
        """Nothing is paraphrased. Whatever comes back, the model wrote
        it, in that order."""
        for thesis in (THESIS, "Short one.", "A" * 900,
                       "One. Two. " + "word " * 200):
            got, _ = panels._reason_excerpt(thesis)
            assert " ".join(thesis.split()).startswith(got), (thesis[:40], got)

    def test_nothing_exceeds_the_budget(self):
        for thesis in (THESIS, "A" * 900, "One. " + "word " * 200):
            assert len(panels._reason_excerpt(thesis)[0]) \
                <= panels.REASON_EXCERPT_CHARS

    def test_a_missing_thesis_adds_nothing_rather_than_an_empty_quote(
            self, tmp_path):
        s = _summary(_seed(tmp_path, thesis=""))
        assert "in Claude's own words" not in s, (
            "an empty quote was rendered where there is no reasoning:\n" + s)
        # and the rest of the paragraph still works
        assert "RLMD" in s and "still open" in s

    @pytest.mark.parametrize("bad", ["", None, "   "])
    def test_no_thesis_at_all(self, bad):
        assert panels._reason_excerpt(bad) == ("", False)


class TestTheCutIsByRuleNotBySlicing:
    """The first version cut on a FRACTION OF THE DISPLAY BUDGET and
    threw away a complete 68-character sentence in favour of a clause
    chopped mid-phrase 237 characters in. Found by exercising the
    branch, not by reading it."""

    def test_a_complete_short_sentence_is_preferred_to_a_clause_cut(self):
        t = ("Insiders bought heavily last week and the stock has not "
             "re-rated yet. The mechanism is a slow unwind of an "
             "overreaction the market has not registered, which "
             "historically takes several weeks rather than days, and the "
             "volume is thin enough that digestion is unlikely soon.")
        got, clipped = panels._reason_excerpt(t)
        assert clipped
        assert got.endswith("re-rated yet."), (
            "a whole sentence was available inside the budget and was not "
            f"used; got: {got!r}")

    def test_a_stub_first_sentence_is_NOT_the_excerpt(self):
        """"Yes." is a sentence and says nothing. The floor is measured
        in words because that is a fact about prose."""
        got, _ = panels._reason_excerpt(
            "Yes. " + "insiders bought the dip and it has not re-rated " * 12)
        assert got != "Yes."
        assert len(got.split()) >= panels._SENTENCE_MIN_WORDS

    def test_a_word_is_never_split(self):
        """A thesis cut mid-word reads as corrupted data."""
        t = ("CEO and CFO both bought on the open market at prices well "
             "under the fifty day moving average right after a departure "
             "that the market read as far worse than the filings actually "
             "support, which is the whole of the setup here today and the "
             "entire reason to be long it at this particular price level.")
        assert len(t) > panels.REASON_EXCERPT_CHARS, (
            "the sample no longer exceeds the budget, so this test would "
            "pass without ever reaching the cut")
        got, clipped = panels._reason_excerpt(t)
        assert clipped
        rest = " ".join(t.split())[len(got):]
        assert rest.startswith(" ") or rest.startswith("."), (
            f"the cut landed inside a word: ...{got[-25:]!r} | {rest[:15]!r}")


class TestEveryCatalystTypeReadsAsEnglish:
    """The replaced table covered 2 of 19 catalyst types. The rule has
    to cover all of them, including the article (house rule 7)."""

    @pytest.mark.parametrize("kind,expected", [
        ("insider_cluster", "an insider cluster"),
        ("earnings_drift", "an earnings drift"),
        ("asset_deal", "an asset deal"),
        ("analyst_action", "an analyst action"),
        ("merger_vote", "a merger vote"),
        ("fda_decision", "a fda decision"),
        ("clinical_readout", "a clinical readout"),
        ("buyback", "a buyback"),
    ])
    def test_the_article_matches_the_type(self, tmp_path, kind, expected):
        s = _summary(_seed(tmp_path, catalyst_type=kind))
        assert f"matched it on {expected}" in s.replace(
            "<b>", "").replace("</b>", ""), s

    def test_a_missing_catalyst_type_says_nothing_rather_than_guessing(
            self, tmp_path):
        s = _summary(_seed(tmp_path, catalyst_type=""))
        assert "matched it on" not in s
        assert "RLMD" in s


class TestTheSearchCountIsCountedNotAsserted:
    def test_one_search_is_singular(self, tmp_path):
        s = _summary(_seed(tmp_path, queries=QUERIES[:1]))
        assert "1 web search</b>" in s, s

    def test_no_searches_claims_none(self, tmp_path):
        """An arm that reached no tool must not be described as having
        searched - that is the claim the owner is trying to check."""
        s = _summary(_seed(tmp_path, queries=[]))
        assert "web search" not in s, (
            "the card claims the model searched when it did not:\n" + s)
        assert "in Claude's own words" in s, (
            "the reasoning should still be quoted with no searches")
