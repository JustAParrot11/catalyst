"""The decision page does not stop at the risk engine, and its
references say what they are.

OWNER-REPORTED 2026-09-13, on the CHYM decision card:

    "I can see the graph its made but stops at what the deterministic
     engine did and what happened at the broker it just seems to cut off"

    "the graph is looking good however, these references dont actually
     mean anything to me"

    "I can see it was doing active research and finding potential stocks
     on 12/09, this is good. What did it do, are any queued up its not
     clear anywhere what it is doing."

MEASURED, all three:

    1. **The spider had exactly three arms** - saw / concluded / did -
       and `decision_spider` hard-capped `groups[:3]`. The story of a
       decision does not end at the risk engine; it ends at a fill, or at
       a sentence saying why there was never going to be one. 293 of 294
       decisions on record are declines, so "nothing was sent, and here
       is what the stock did without us" IS the outcome in almost every
       case.
    2. **The leaf labels were machine references.** `_spider_groups` drew
       `source_label(source)` with `filing <source_id>, fetched <ts>` in
       the hover, and the full record's fold was headed `source event
       edgar_fts:0001193125-26-385383:credit_amendment`. The payload
       under both already held the headline or the matched phrase, and
       `queries._describe_source` has read it since §10b - for the TRADE
       CARD alone, which exists for one candidate in seven thousand. An
       unnamed graph entity fell through to `subject_entity_id`, a
       `uuid.uuid4().hex`.
    3. **Nothing said what was queued.** The funnel counts a lifetime
       population. Its largest single row in the owner's bundle is
       `deferred_max_research_per_cycle` at 6,581 - that IS the queue,
       named and counted, and never described as one.

Fully offline. Every clock is supplied (house rule 6).
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.dashboard import charts, panels, queries
from catalyst.dashboard.db import Db

NOW = datetime(2026, 9, 13, 12, 54, tzinfo=timezone.utc)
#: `graph.store.upsert_entity` writes `uuid.uuid4().hex`. The ids matter to
#: this module: the mindmap's fallback chain ends at `subject_entity_id`,
#: so a fixture using short ids cannot reproduce the 32-character box the
#: owner reported.
HEX_COMPANY = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
HEX_UNNAMED = "3f9c1ab24e7d4c8fa1b25e6d9c704f11"
HEX_FILING = "77aa11bb22cc33dd44ee55ff66007788"
HEX_EVENT = "0011223344556677889900aabbccddee"
GRAPH_SCHEMA = (Path(__file__).resolve().parent.parent / "catalyst"
                / "storage" / "schema_graph.sql")


def _visible(html: str) -> str:
    """Only what a reader sees: tag contents, not attributes. A `<title>`
    hover is deliberately NOT visible text - a reference belongs there."""
    body = re.sub(r"<title>.*?</title>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def _hovers(html: str) -> str:
    return " ".join(re.findall(r"<title>(.*?)</title>", html, flags=re.S))


@pytest.fixture
def chym(tmp_path):
    """The owner's own candidate, with the payloads its feeds really
    carry, a decline, an unscored refusal, and an UNNAMED graph entity."""
    from catalyst.storage import init_db

    path = str(tmp_path / "chym.db")
    conn = init_db(path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, (
        "the fixture must match production; §14 is what a fixture that "
        "does not costs")
    conn.executescript(GRAPH_SCHEMA.read_text())
    iso = NOW.isoformat()
    cid = "conj-b3caf562223b246f8844"
    conn.execute(
        "INSERT INTO raw_events VALUES (?,?,?,?)",
        ("edgar_fts", "0001193125-26-385383:credit_amendment", iso,
         json.dumps({"matched_phrase": '"credit agreement" "amendment"',
                     "accession": "0001193125-26-385383", "cik": "1873835",
                     "source_url": "https://www.sec.gov/Archives/edgar/"
                                   "data/1873835/x-index.htm"})))
    conn.execute(
        "INSERT INTO raw_events VALUES (?,?,?,?)",
        ("alpaca_news", "61720763:CHYM", iso,
         json.dumps({"headline": "Chime Financial Stock Pulls Back Thursday",
                     "url": "https://example.invalid/chym",
                     "source": "Benzinga"})))
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (cid, "CHYM", "financing", "2026-09-10", "estimated",
                  json.dumps(["0001193125-26-385383:credit_amendment",
                              "61720763:CHYM"]),
                  iso, "6199", json.dumps(["financing", "news"])))
    conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                 (cid, "no_trade", 0.68, "The two feeds do not connect.",
                  "A CHYM-specific 8-K dated 2026-09-08.", 10, 1,
                  "Attributed to profit-taking."))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("d-chym", cid, "skip", None, None, None, None, None,
                  json.dumps(["model_said_no_trade"]),
                  json.dumps({"conviction_floor": 0.5}), iso))
    conn.execute("INSERT INTO refusals VALUES (?,?,?,?,?,?,?)",
                 ("d-chym", cid, "32.31", iso, None, None, None))
    # ENTITY IDS ARE `uuid.uuid4().hex` IN PRODUCTION. The fixture used to
    # use "g1".."g4", which are unreadable by the rule anyway - so the
    # fallback path could never draw the 32-character box the owner
    # reported, and a sabotage of the guard came back green.
    for eid, kind, key, name in [
            (HEX_COMPANY, "company", "company:CHYM", "CHYM"),
            # THE HEX CASE. A real entity whose display_name never got
            # written, so the mindmap's `label_of` falls through to
            # `subject_entity_id` - a uuid4 hex.
            (HEX_UNNAMED, "other", "org:unnamed", ""),
            # AND THE OTHER SHAPE OF THE SAME PROBLEM, which is the one
            # the SPIDER can reach: an entity that HAS a display_name and
            # whose display_name is itself a machine reference. The
            # spider reads `subject_label` only, so it can never see a
            # uuid - found by sabotage coming back green - but it can
            # certainly draw an accession number somebody stored as a
            # name.
            (HEX_FILING, "filing", "filing:acc", "0001193125-26-385383"),
            (HEX_EVENT, "event", "event:stride",
             "Stride Bank acquisition, $590M cash")]:
        conn.execute("INSERT INTO graph_entities VALUES (?,?,?,?,?)",
                     (eid, kind, key, name, iso))
    for i, (s_, pred, o_) in enumerate(
            [(HEX_UNNAMED, "lender_to", HEX_COMPANY),
             (HEX_FILING, "filed_against", HEX_COMPANY),
             (HEX_COMPANY, "likely_beneficiary_of", HEX_EVENT)]):
        conn.execute("INSERT INTO graph_assertions VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"ga{i}", s_, pred, o_, None, "edgar_filing",
                      "0001193125-26-385383", iso, "primary_document"))
    conn.commit()
    conn.close()
    return path


CID = "conj-b3caf562223b246f8844"


class TestThePictureDoesNotStopAtTheRiskEngine:

    def test_there_is_a_fourth_arm_for_what_happened_next(self, chym):
        html = panels.trace_simple(Db(chym), CID, p="trs")
        assert "What happened next" in html, (
            "the picture still ends at the risk engine, which is the "
            "owner's 'it just seems to cut off'")

    def test_a_DECLINED_candidate_gets_that_arm_too(self, chym):
        """293 of 294 decisions are declines. If the arm only appeared on
        a trade it would be missing from essentially every card."""
        html = panels.trace_simple(Db(chym), CID, p="trs")
        assert "no order was sent" in html

    def test_it_says_the_refusal_is_not_yet_scored(self, chym):
        """An unscored refusal is evidence of nothing, and the page has to
        say which - the refusal tracker is the project's own most
        important feedback loop and has scored ~0 of 293."""
        html = panels.trace_simple(Db(chym), CID, p="trs")
        assert "outcome not scored yet" in html
        assert "32.31" in html, "the price at refusal is not shown"

    def test_a_scored_refusal_says_what_the_stock_did_without_us(self, chym):
        import sqlite3

        conn = sqlite3.connect(chym)
        conn.execute("UPDATE refusals SET scored_at = ?, outcome_price = ?, "
                     "outcome_return = ? WHERE candidate_id = ?",
                     (NOW.isoformat(), "34.90", "+8.0%", CID))
        conn.commit()
        conn.close()
        html = panels.trace_simple(Db(chym), CID, p="trs")
        assert "+8.0%" in html
        assert "without us" in html

    def test_the_arm_cap_is_derived_from_the_palette_not_typed(self):
        """The cap used to be a literal 3, and the colour index is
        `gi % len(SPIDER_SLOTS)` - so a fourth arm silently reused the
        first arm's hue and two arms became indistinguishable. Derived,
        they cannot disagree."""
        import inspect

        src = inspect.getsource(charts.decision_spider)
        assert "[:len(SPIDER_SLOTS)]" in src
        assert "[:3]" not in src

    def test_four_arms_have_four_distinct_colours(self):
        assert len(set(charts.SPIDER_SLOTS)) == len(charts.SPIDER_SLOTS) >= 4

    def test_four_loaded_arms_do_not_overlap_or_leave_the_frame(self):
        """Measured, not eyeballed: adding an arm tightens every wedge,
        and overlapping boxes are what the owner once reported as 'a
        bunch of lines with cut off text'."""
        groups = [
            ("What it saw", [('"credit agreement" "amendment"', "a"),
                             ("Chime Financial Stock Pulls Back Thursday", "b"),
                             ("Stride Bank acquisition, $590M cash", "c"),
                             ("financing", "catalyst type"),
                             ("J. Restrepo, CFO", "d"),
                             ("SEC 8-K strategic alternatives", "e")]),
            ("What it concluded", [("no_trade", "direction"),
                                   ("conviction 0.68", "0 to 1"),
                                   ("hold 10 days", "expected"),
                                   ("already priced in", "judgement")]),
            ("What the code did", [("skip", "risk engine"),
                                   ("model said no trade", "why"),
                                   ("max_loss_per_position", "bound")]),
            ("What happened next", [("buy 4.1 sent", "a"),
                                    ("filled at 182.50", "b"),
                                    ("position open", "c"),
                                    ("closed -$6.84", "d"),
                                    ("5 re-read(s)", "e"),
                                    ("went -3.5% without us", "f")]),
        ]
        svg = charts.decision_spider("CHYM", "DECLINED", groups,
                                     chart_id="s")
        rects = [tuple(float(v) for v in b) for b in re.findall(
            r'<rect x="([-\d.]+)" y="([-\d.]+)" width="([\d.]+)" '
            r'height="([\d.]+)"', svg)][1:]
        assert len(rects) >= 19, (
            f"only {len(rects)} boxes drawn; the fourth arm is missing")
        bad = [(i, j) for i in range(len(rects))
               for j in range(i + 1, len(rects))
               if (rects[i][0] < rects[j][0] + rects[j][2]
                   and rects[j][0] < rects[i][0] + rects[i][2]
                   and rects[i][1] < rects[j][1] + rects[j][3]
                   and rects[j][1] < rects[i][1] + rects[i][3])]
        assert not bad, f"{len(bad)} pair(s) of boxes overlap"
        assert charts.labels_outside_viewbox(svg) == []


class TestTheReferencesSayWhatTheyAre:

    def test_the_news_leaf_reads_as_the_headline(self, chym):
        html = panels.trace_simple(Db(chym), CID, p="trs")
        assert "Chime Financial Stock Pulls Back Thursday" in _visible(html)

    def test_the_filing_leaf_reads_as_the_matched_phrase(self, chym):
        """A word carrying quotes fails `str.isalpha()`, so the first
        version of the readability rule threw this phrase away and showed
        the feed's machine name instead. Found by rendering."""
        html = panels.trace_simple(Db(chym), CID, p="trs")
        assert "credit agreement" in _visible(html)

    def test_the_machine_reference_moves_to_the_hover(self, chym):
        """It is not deleted - it is where a reference belongs. Somebody
        checking the record needs the accession; nobody reading the
        picture does."""
        html = panels.trace_simple(Db(chym), CID, p="trs")
        assert "61720763:CHYM" not in _visible(html)
        assert "61720763:CHYM" in _hovers(html)

    def test_an_opaque_NAME_is_not_drawn_in_the_spider(self, chym):
        """The spider reads `subject_label` only, so it can never see a
        uuid - sabotage proved that by coming back green. What it CAN
        draw is an entity whose stored display_name is itself a machine
        reference, which is the same defect wearing the other hat."""
        html = panels.trace_simple(Db(chym), CID, p="trs")
        assert "0001193125-26-385383" not in _visible(html)

    def test_an_unnamed_graph_entity_is_not_drawn_as_its_uuid(self, chym):
        """THE MINDMAP is where the uuid can actually appear: its
        `label_of` falls through to `subject_entity_id`, and it renders on
        the FULL record, not the simple view. The first version of this
        test asserted against `trace_simple` and could not fail."""
        html = panels.trace_page(Db(chym), CID, p="tr")
        assert "-mindmap" in html, (
            "no mindmap was drawn, so this test guards nothing")
        # DRAWN text only. The verbatim table below the diagram carries
        # every id on purpose - that is where a reference belongs, and a
        # slice to the end of the page would find it there.
        drawn = " ".join(re.findall(r"<text[^>]*>(.*?)</text>", html,
                                   flags=re.S))
        assert HEX_UNNAMED not in drawn, (
            "a 32-character hex box is the owner's complaint exactly")
        assert HEX_UNNAMED in html, (
            "the id vanished from the record as well, which loses the "
            "audit trail rather than tidying the picture")

    def test_the_mindmap_fallback_draws_no_entity_ids(self, chym):
        """THE BRANCH WHERE A UUID CAN ACTUALLY REACH THE PAGE, entered on
        purpose.

        `_narrative_evidence` prefers `evidence_graph`, which SELECTs
        `display_name` - so on that path an entity id is not even in the
        result set. It falls back to the generic `graph_assertions` scan
        when that query returns nothing, and THAT row set carries
        `subject_entity_id`, which `label_of` reaches.

        The first version of this test grepped the source for
        `"subject_entity_id"`, which proves the fallback chain exists and
        not that anything guards it. This enters the branch: a candidate
        whose ticker matches no `company:<TICKER>` entity, which is an
        ordinary production state (a graph built under a different symbol,
        or a database with assertions and no names).
        """
        import sqlite3

        conn = sqlite3.connect(chym)
        try:
            conn.execute("UPDATE candidates SET ticker = 'ZZZZ' WHERE id = ?",
                         (CID,))
            conn.commit()
        finally:
            conn.close()
        html = panels.trace_page(Db(chym), CID, p="tr")
        # Proof the branch was entered: the generic scan is what renders
        # the verbatim table, and it carries the raw entity ids.
        assert HEX_UNNAMED in html, (
            "the fallback did not run, so this test guards nothing")
        drawn = " ".join(re.findall(r"<text[^>]*>(.*?)</text>", html,
                                   flags=re.S))
        assert HEX_UNNAMED not in drawn, (
            "a 32-character entity id is drawn as a node label, which is "
            "the owner's complaint exactly")
        assert HEX_COMPANY not in drawn

    def test_a_NAMED_graph_entity_is_still_drawn(self, chym):
        """The suppression must not take the good ones with it."""
        html = panels.trace_simple(Db(chym), CID, p="trs")
        assert "Stride Bank acquisition" in _visible(html)

    def test_the_full_record_heading_says_what_the_source_said(self, chym):
        html = panels.trace_page(Db(chym), CID, p="tr")
        headings = " ".join(re.findall(r"<summary[^>]*>(.*?)</summary>",
                                      html, flags=re.S))
        assert "Chime Financial Stock Pulls Back Thursday" in headings, (
            "the fold is still headed by an accession number")

    def test_the_full_record_links_the_source_it_actually_fetched(self, chym):
        """§10b's lesson: when the payload carries a URL that resolved,
        that is the link - deriving one is a guess with extra steps."""
        html = panels.trace_page(Db(chym), CID, p="tr")
        assert "https://www.sec.gov/Archives/edgar/data/1873835/x-index.htm" \
            in html
        assert 'rel="noopener noreferrer"' in html

    def test_the_full_record_still_shows_the_payload_verbatim(self, chym):
        """House rule 3 is unchanged: the raw response moves BELOW a
        sentence, it is never replaced by one."""
        html = panels.trace_page(Db(chym), CID, p="tr")
        assert "0001193125-26-385383" in html


class TestTheReadabilityRuleIsARuleNotAList:
    """House rule 7. A hand-written list of id formats mislabels the first
    format nobody thought of."""

    @pytest.mark.parametrize("readable", [
        "Chime Financial Stock Pulls Back Thursday",
        '"credit agreement" "amendment"',
        "Bern Richard (CEO) bought 141,000 shares at $70.96",
        "J. Restrepo, CFO",
        "Stride Bank acquisition, $590M cash",
        "Form 4 filed by A. Smith",
    ])
    def test_a_phrase_is_readable(self, readable):
        assert panels._readable_ref(readable) is True

    @pytest.mark.parametrize("opaque", [
        "3f9c1ab24e7d4c8fa1b25e6d9c704f11",
        # A HEX ID WHOSE DIGITS ARE MOSTLY a-f. Sixteen letters in
        # thirty-two characters, so the earlier "letters carry half the
        # string plus a three-letter token" rule accepted it - and the
        # mindmap drew a 32-character box. Found by this module's own
        # test, not by inspection.
        "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
        "0011223344556677889900aabbccddee",
        "77aa11bb22cc33dd44ee55ff66007788",
        "0001193125-26-385383",
        # A reference with a real word stuck to it is still a reference.
        "61720763:CHYM",
        "0001193125-26-385383:x",
        "", "   ", None, "1234567890", "a1", "e1",
    ])
    def test_a_machine_reference_is_not(self, opaque):
        assert panels._readable_ref(opaque) is False

    def test_the_edgar_full_text_feed_has_a_name_in_words(self):
        """Found by rendering the owner's own card: the feed behind the
        conjunction arm - 48% of the research budget - was the one
        machine name missing from the table, so it read 'edgar fts'."""
        assert queries.source_label("edgar_fts") != "edgar fts"
        assert "EDGAR" in queries.source_label("edgar_fts")

    def test_an_unknown_feed_still_appears_rather_than_vanishing(self):
        assert queries.source_label("some_new_feed_2027") != ""


class TestTheDescriberIsReachableFromOutsideItsModule(object):
    """§6, fifth instance: a helper nobody calls passes its own tests.
    `_describe_source` was private and reached only through
    `_candidate_sources`, which only the trade card calls."""

    def test_describe_source_takes_the_payload_as_stored(self):
        stored = json.dumps({"headline": "A real headline"})
        assert queries.describe_source(stored) == "A real headline"
        assert queries.describe_source({"headline": "A real headline"}) \
            == "A real headline"

    def test_it_survives_a_payload_that_is_not_an_object(self):
        for junk in ("not json", "[]", "null", None, 7):
            assert queries.describe_source(junk) == ""

    def test_source_link_accepts_the_same_shapes(self):
        stored = json.dumps({"url": "https://example.invalid/x"})
        assert queries.source_link("alpaca_news", stored) \
            == "https://example.invalid/x"
        assert queries.source_link("alpaca_news", "not json") == ""

    def test_the_spider_actually_calls_the_describer(self):
        """Assert the CALL SITE, not the helper."""
        import inspect

        src = inspect.getsource(panels._spider_groups)
        assert "queries.describe_source(" in src
