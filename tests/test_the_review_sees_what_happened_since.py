"""The review was asked whether the thesis broke, and shown no evidence.

OWNER-ASKED 2026-09-14: *"what sort of extra checks will it do next, its
still not clear, will it check what the CEO does next or will it see if a
partner of them did for example"*.

**THE ANSWER WAS "NEITHER".** Measured by reading `render_prompt`: it
carried the date, the ticker, the dates held and remaining, the thesis,
the invalidation condition and the price move. **No new evidence of any
kind.** So a review was asked *"has that invalidation condition actually
occurred?"* with nothing to check it against except the price — and
RLMD's own invalidation names *"any 8-K or press release disclosing a new
Phase 3 NDV-01 delay"*, which no code looked for.

**AND THE TRIGGER ALREADY KNEW.** `news_since` was computed on every
review to decide *when* to look, and never shown *to* the review: news
woke it up and then the prompt did not mention the news. Tenth instance
of this project's recurring defect (§12, §14, §17, §22, §23, §26, §29,
§30).

**THE DATA WAS ALREADY ON DISK, and that is the part worth recording.**
The Form 4 feed sweeps the **whole daily index** — every filing, every
transaction code (`"P"` purchase, `"S"` sale, …), 562 accessions a day.
Only `form4_adapter` filters to code `P` when building clusters. So a
later SALE by the same officer the bot bought behind has been stored the
whole time and nothing read it.

That is the sharpest case here: every order this bot has ever placed came
from insiders BUYING, so an insider selling afterwards is the most direct
contradiction of the thesis that exists — and it was invisible.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.research import position_review as P
from catalyst.storage import init_db

# Measured against a clock, never anchored to a calendar date (house
# rule 6): the code under test compares against `datetime.now()`.
NOW = datetime.now(timezone.utc).replace(microsecond=0)
OPEN = NOW - timedelta(days=11)


def form4(name, role, code, way, shares="1000", price="5.00",
          day=None, ticker="RLMD"):
    """A payload shaped like the one `edgar_form4.to_payload` writes."""
    return json.dumps({
        "accession": f"acc-{name}-{code}-{way}",
        "ticker": ticker,
        "issuer_name": "Relmada Therapeutics",
        "owners": [{"name": name, "relationship": role}],
        "transactions": [{
            "table": "non_derivative", "code": code,
            "acquired_disposed": way, "shares": shares,
            "price_per_share": price,
            "value_usd": str(float(shares) * float(price)),
            "transaction_date": (day or (OPEN + timedelta(days=4)).date()
                                 .isoformat()),
        }],
    })


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "r.db"))
    # §14's lesson: go through init_db so the pragma matches production
    # and an impossible row is impossible in the fixture too.
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    yield c
    c.close()


def seed(c, rows, at=None):
    for i, (src, payload) in enumerate(rows):
        c.execute("INSERT INTO raw_events (source,source_id,fetched_at,"
                  "payload_raw) VALUES (?,?,?,?)",
                  (src, f"s{i}-{payload[:12]}",
                   (at or OPEN + timedelta(days=3)).isoformat(), payload))
    c.commit()


class TestTheCEOSellingIsTheHeadline:
    """"will it check what the CEO does next" — yes, and a disposal is
    presented apart from a purchase because it means the opposite."""

    def test_a_sale_after_entry_is_found(self, conn):
        seed(conn, [("edgar_form4", form4(
            "TRAVERSA SERGIO", "officer:Chief Executive Officer",
            "S", "D", "120000", "5.20"))])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert ev.sales, "an insider disposal since entry was not found"
        assert "TRAVERSA" in ev.sales[0]
        assert "sale" in ev.sales[0]
        assert "624,000" in ev.sales[0], ev.sales[0]

    def test_a_sale_is_not_filed_under_purchases(self, conn):
        seed(conn, [("edgar_form4", form4("X", "officer:CEO", "S", "D"))])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert ev.sales and not ev.purchases

    def test_the_prompt_puts_disposals_before_purchases(self, conn):
        """A thesis built on insiders buying is contradicted by a sale in
        a way it is not by anything else, so burying it under a list of
        option exercises would be a presentation choice with money on
        it."""
        seed(conn, [
            ("edgar_form4", form4("Seller", "officer:CEO", "S", "D")),
            ("edgar_form4", form4("Buyer", "officer:CFO", "P", "A")),
            ("edgar_form4", form4("Granted", "director", "M", "A")),
        ])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        text = "\n".join(P._evidence_lines(ev, "RLMD", True))
        assert text.index("DISPOSED") < text.index("bought more")
        assert text.index("bought more") < text.index("Other insider")

    def test_direction_comes_from_acquired_disposed_not_the_code(self, conn):
        """`code` says what KIND of transaction; `A`/`D` says which way
        the stock went, and it is the field the cluster adapter itself
        trusts. A disposal with an unusual code must still read as a
        disposal."""
        seed(conn, [("edgar_form4", form4("Gifter", "director", "G", "D"))])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert ev.sales, "a disposal with a non-S code was not counted"
        assert not ev.purchases

    def test_an_acquisition_that_is_not_a_purchase_is_kept_apart(self, conn):
        """An option exercise is compensation mechanics, not a view.
        Counting it as a purchase would overstate the signal."""
        seed(conn, [("edgar_form4", form4("Exerciser", "officer:CEO",
                                          "M", "A"))])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert ev.other_insider and not ev.purchases and not ev.sales

    def test_an_unknown_code_is_passed_through_not_guessed(self, conn):
        """House rule 7, in the direction that matters: mislabelling a
        transaction is worse than printing the letter."""
        seed(conn, [("edgar_form4", form4("Odd", "director", "Z", "A"))])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert any("transaction code Z" in x for x in ev.other_insider), (
            ev.other_insider)


class TestItOnlyLooksAtThisCompany:
    def test_another_ticker_is_not_reported(self, conn):
        seed(conn, [("edgar_form4", form4("X", "officer:CEO", "S", "D",
                                          ticker="AAPL"))])
        assert not P.evidence_since(conn, "RLMD", OPEN).anything

    def test_the_match_is_case_insensitive(self, conn):
        seed(conn, [("edgar_form4", form4("X", "officer:CEO", "S", "D",
                                          ticker="rlmd"))])
        assert P.evidence_since(conn, "rLmD", OPEN).sales

    def test_no_ticker_asks_nothing(self, conn):
        seed(conn, [("edgar_form4", form4("X", "officer:CEO", "S", "D"))])
        assert not P.evidence_since(conn, "", OPEN).anything


class TestOnlyWhatArrivedSINCE:
    def test_a_filing_from_before_entry_is_not_new(self, conn):
        """It is the evidence the thesis was WRITTEN on. Presenting it as
        new would invite the model to re-decide on what it already knew."""
        seed(conn, [("edgar_form4", form4("Old", "officer:CEO", "P", "A"))],
             at=OPEN - timedelta(days=5))
        assert not P.evidence_since(conn, "RLMD", OPEN).anything

    def test_a_filing_after_entry_is_new(self, conn):
        seed(conn, [("edgar_form4", form4("New", "officer:CEO", "P", "A"))],
             at=OPEN + timedelta(days=1))
        assert P.evidence_since(conn, "RLMD", OPEN).purchases


class TestNewsFinallyReachesTheReview:
    def test_a_headline_is_shown_not_merely_counted(self, conn):
        """`news_since` woke the review and never told it what woke it."""
        seed(conn, [("alpaca_news", json.dumps(
            {"ticker": "RLMD",
             "headline": "Relmada discloses NDV-01 manufacturing delay"}))])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert any("manufacturing delay" in h for h in ev.news), ev.news

    def test_a_filing_is_named_by_its_form_type(self, conn):
        """RLMD's own invalidation names an 8-K. It has to be nameable."""
        seed(conn, [("edgar_fts", json.dumps(
            {"ticker": "RLMD", "form_type": "8-K",
             "filed_date": "2026-09-23"}))])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert any("8-K" in f for f in ev.filings), ev.filings


class TestAbsenceIsNotEvidence:
    """The most dangerous reading here. EDGAR publishes nothing while the
    market is shut, so "no new filings" means two different things."""

    def test_a_shut_market_says_the_absence_is_structural(self):
        text = "\n".join(P._evidence_lines(P.Evidence(), "RLMD", False))
        assert "MARKET IS SHUT" in text
        assert "not evidence" in text

    def test_an_open_market_says_it_is_a_real_quiet_spell(self):
        text = "\n".join(P._evidence_lines(P.Evidence(), "RLMD", True))
        assert "quiet spell" in text
        assert "MARKET IS SHUT" not in text

    def test_UNKNOWN_IS_NOT_SHUT(self):
        """None means nobody looked. Saying the market was shut when
        nobody checked is the same class of error as §22's 1000%
        spread — a value chosen to mean "unknown" rendered as a fact."""
        text = "\n".join(P._evidence_lines(P.Evidence(), "RLMD", None))
        assert "not checked" in text
        assert "MARKET IS SHUT" not in text
        assert "quiet spell" not in text

    def test_the_three_states_all_read_differently(self):
        said = {s: "\n".join(P._evidence_lines(P.Evidence(), "RLMD", s))
                for s in (True, False, None)}
        assert len(set(said.values())) == 3, said


class TestItIsBounded:
    def test_a_flood_is_capped_and_the_remainder_counted(self, conn):
        """An unbounded list would push the thesis out of the model's
        attention and multiply the cost of a call that exists to be
        cheap. Anything dropped is COUNTED, never silently lost."""
        seed(conn, [("edgar_form4", form4(f"Seller{i}", "director", "S", "D"))
                    for i in range(P.MAX_EVIDENCE_PER_KIND + 5)])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert len(ev.sales) == P.MAX_EVIDENCE_PER_KIND
        assert ev.omitted == 5, ev.omitted
        text = "\n".join(P._evidence_lines(ev, "RLMD", True))
        assert "further item(s) exist" in text
        assert "5" in text

    def test_each_kind_has_its_own_room(self, conn):
        """A flood of news must not crowd out the one disposal, which is
        the item most likely to matter."""
        rows = [("alpaca_news", json.dumps({"ticker": "RLMD",
                                            "headline": f"noise {i}"}))
                for i in range(20)]
        rows.append(("edgar_form4", form4("TheSeller", "officer:CEO",
                                          "S", "D")))
        seed(conn, rows)
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert ev.sales, "the disposal was crowded out by news"
        assert len(ev.news) == P.MAX_EVIDENCE_PER_KIND


class TestItNeverRaisesAndNeverLeads:
    def test_a_database_with_no_raw_events_returns_nothing_found(
            self, tmp_path):
        import sqlite3

        c = sqlite3.connect(str(tmp_path / "bare.db"))
        try:
            assert not P.evidence_since(c, "RLMD", OPEN).anything
        finally:
            c.close()

    def test_unparseable_payloads_are_skipped_not_fatal(self, conn):
        seed(conn, [("edgar_form4", "{not json"),
                    ("edgar_form4", "[]"),
                    ("edgar_form4", form4("Good", "officer:CEO", "S", "D"))])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert len(ev.sales) == 1

    def test_it_does_not_tell_the_model_what_to_conclude(self, conn):
        """Presenting a disposal is a fact. Telling the model to exit on
        it would move the judgement into the renderer, which is the one
        thing this project does not do."""
        seed(conn, [("edgar_form4", form4("S", "officer:CEO", "S", "D"))])
        text = "\n".join(P._evidence_lines(
            P.evidence_since(conn, "RLMD", OPEN), "RLMD", True)).lower()
        for leading in ("you should", "consider exiting", "this invalidates",
                        "the thesis is broken", "exit_now"):
            assert leading not in text, f"the section leads the model: {leading}"
        assert "not a verdict" in text


class TestThePromptActuallyCarriesIt:
    """Section 22's corollary: assert the OUTCOME, because a call-site
    substring survives a sabotage."""

    def _prompt(self, ev, live=True):
        return P.render_prompt(
            {"ticker": "RLMD", "opened_at_date": OPEN.date().isoformat(),
             "planned_exit_date": (NOW + timedelta(days=4)).date()
             .isoformat()},
            {"thesis": "insiders bought the dip",
             "invalidation": "a close below $3.80, or an 8-K naming a delay"},
            {"entry_price": "4.49", "last_price": "5.05", "move_pct": "12.3"},
            now=NOW, evidence=ev, market_is_live=live)

    def test_a_disposal_reaches_the_rendered_prompt(self, conn):
        seed(conn, [("edgar_form4", form4(
            "TRAVERSA SERGIO", "officer:Chief Executive Officer", "S", "D",
            "120000", "5.20"))])
        text = self._prompt(P.evidence_since(conn, "RLMD", OPEN))
        assert "TRAVERSA SERGIO" in text
        assert "DISPOSED" in text

    def test_the_evidence_sits_between_the_invalidation_and_the_question(
            self, conn):
        """Read what would prove it wrong, then what happened, THEN be
        asked whether it did."""
        seed(conn, [("edgar_form4", form4("X", "officer:CEO", "S", "D"))])
        text = self._prompt(P.evidence_since(conn, "RLMD", OPEN))
        assert (text.index("WOULD INVALIDATE")
                < text.index("WHAT HAS BEEN FILED")
                < text.index("ANSWER"))

    def test_a_caller_passing_no_evidence_gets_no_empty_heading(self):
        text = self._prompt(None)
        assert "WHAT HAS BEEN FILED" not in text, (
            "a caller that passes nothing gets a heading with nothing "
            "under it, which reads as 'nothing happened'")

    def test_the_prompt_still_says_hold_cannot_extend(self, conn):
        """The property that must survive every change to this prompt."""
        seed(conn, [("edgar_form4", form4("X", "officer:CEO", "S", "D"))])
        text = self._prompt(P.evidence_since(conn, "RLMD", OPEN))
        assert "cannot extend it" in text
        assert "FIXED" in text


class TestTheCycleWiresItThrough:
    """The review is handed a `market` dict built in `cycle.py`, and my
    first version read a key that dict never carried - so the absence
    branch would have said "nobody looked" forever. Found by checking
    the caller, not by a test, and this is the test."""

    def test_the_market_dict_carries_the_market_state(self):
        from source_guard import source_matches

        hits = source_matches('"market_is_live": market_is_live(snapshot)',
                              "catalyst/orchestrator")
        assert hits, (
            "cycle.py does not put the market state into the review's "
            "market dict, so every absence reads as 'nobody looked'")

    def test_it_uses_the_SHARED_helper_rather_than_a_second_derivation(self):
        """§22 derived this once from `priced_off`. A second copy would
        drift, which is §28's lesson."""
        from catalyst.research.prompts import market_is_live

        class Snap:
            def __init__(self, p):
                self.priced_off = p

        assert market_is_live(Snap("live_nbbo")) is True
        assert market_is_live(Snap("cached_daily_close")) is False
        assert market_is_live(None) is None

    def test_gathering_the_evidence_costs_no_api_call(self):
        """Pure database. If this ever reaches a broker or the model, a
        review becomes dearer for every position on every cycle."""
        import inspect

        src = inspect.getsource(P.evidence_since)
        for forbidden in ("broker", "transport", "authorize", "record_usage",
                          "requests", "httpx"):
            assert forbidden not in src, (
                f"evidence_since reaches {forbidden!r} - it must stay a "
                "database read")


class TestTheAdversarialReadsOwnFindings:
    """Two defects the house-rule-5 read found in my own change, neither
    of which any test above would have caught."""

    def test_an_upstream_field_cannot_bloat_the_prompt(self, conn):
        """EVERY value in a Form 4 payload comes from a filer. A 10KB
        owner name would multiply the cost of every review of that
        position for as long as it is held."""
        payload = json.dumps({
            "ticker": "RLMD",
            "owners": [{"name": "A" * 10000, "relationship": "B" * 5000}],
            "transactions": [{"code": "S", "acquired_disposed": "D",
                              "shares": "1", "price_per_share": "1"}]})
        seed(conn, [("edgar_form4", payload)])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert ev.sales
        assert len(ev.sales[0]) < 300, (
            f"an upstream field reached the prompt unbounded: "
            f"{len(ev.sales[0])} chars")
        assert "A" * (P.MAX_FIELD_CHARS + 1) not in ev.sales[0]

    def test_a_truncated_scan_says_so_rather_than_reading_as_quiet(self):
        """THE DANGEROUS DIRECTION. A scan that hit its limit and found
        nothing looks identical to a company that filed nothing - and
        one of those means a disposal may be sitting one row past the
        limit."""
        text = "\n".join(P._evidence_lines(
            P.Evidence(truncated=True), "RLMD", True))
        assert "INCOMPLETE" in text
        assert "absence below as unknown" in text

    def test_the_warning_comes_BEFORE_the_findings(self):
        text = "\n".join(P._evidence_lines(
            P.Evidence(truncated=True, sales=("someone sold",)), "RLMD", True))
        assert text.index("INCOMPLETE") < text.index("DISPOSED")

    def test_an_untruncated_scan_does_not_cry_wolf(self, conn):
        """A guard that fires on every review teaches the reader to
        ignore it (§17's generic-word lesson)."""
        seed(conn, [("edgar_form4", form4("X", "officer:CEO", "S", "D"))])
        ev = P.evidence_since(conn, "RLMD", OPEN)
        assert ev.truncated is False
        assert "INCOMPLETE" not in "\n".join(
            P._evidence_lines(ev, "RLMD", True))

    def test_the_scan_drops_the_OLDEST_when_it_truncates(self):
        """Ordered `fetched_at DESC`, so a truncation loses the rows a
        thesis has most likely already been judged against - the right
        direction, and asserted because the opposite would silently hide
        today's filings."""
        from source_guard import source_matches

        hits = source_matches("ORDER BY fetched_at DESC LIMIT ?",
                              "catalyst/research")
        assert hits, "the evidence scan is no longer newest-first"
