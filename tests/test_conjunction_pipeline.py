"""Cross-feed conjunctions, from candidate to prompt to search budget.

Three claims under test, in order of how much they cost if wrong:

  1. A conjunction only ever earns a bigger budget when the evidence is
     genuinely INDEPENDENT. Getting this wrong spends the whole monthly
     cap on one feed talking to itself.
  2. The model is TOLD about the link. A link that exists only in the
     grouping code is not reasoning, whatever the dashboard draws.
  3. Adding two feeds never weakens the graded Form 4 strategy.
"""

from datetime import datetime, timedelta, timezone

import pytest

from catalyst.data import RawEvent
from catalyst.discovery.conjunctions import (
    MAX_CANDIDATES_PER_PASS, MAX_PER_SECTOR_PER_PASS, MAX_SIGNAL_AGE_DAYS,
    build_conjunction_candidates, sector_band,
)
from catalyst.discovery.links import signals_from_events
from catalyst.research import prompts

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def ev(ticker, catalyst, source="edgar_fts", days_ago=1, sid=None,
       sic="2834", headline="", hint=0):
    when = (NOW - timedelta(days=days_ago)).date().isoformat()
    return RawEvent(
        source=source, source_id=sid or f"{source}-{ticker}-{catalyst}",
        fetched_at=NOW,
        payload_raw={"ticker": ticker, "catalyst_type": catalyst,
                     "filed_date": when, "sic": sic,
                     "headline": headline, "direction_hint": hint})


def cross(ticker="ACME", **kw):
    """Two kinds from TWO feeds - a real conjunction."""
    return [ev(ticker, "earnings", source="edgar_fts", **kw),
            ev(ticker, "insider_cluster", source="edgar_form4", **kw)]


class TestOnlyIndependentEvidenceEarnsBudget:
    """The expensive claim. At $0.01 a search, handing the larger
    allowance to single-feed pairs would spend the cap on one company
    saying one thing twice - measured live 2026-08-11, that was 30 of 30
    conjunctions before the news feed existed."""

    def test_a_cross_feed_conjunction_earns_the_larger_budget(self):
        signals = signals_from_events(cross())["ACME"]
        assert prompts.searches_for(None, signals) == prompts.CONJUNCTION_SEARCHES

    def test_a_SINGLE_feed_pair_does_NOT(self):
        """A biotech 8-K routinely mentions a readout and a PDUFA date in
        the same breath. That is one filing trail, not agreement."""
        same = [ev("BBIO", "clinical_readout"), ev("BBIO", "fda_decision")]
        signals = signals_from_events(same)["BBIO"]
        assert prompts.searches_for(None, signals) == prompts.BASE_SEARCHES

    def test_an_ordinary_candidate_is_unchanged(self):
        assert prompts.searches_for(None, None) == prompts.BASE_SEARCHES
        assert prompts.searches_for(None, []) == prompts.BASE_SEARCHES

    def test_the_budget_reaches_the_tool_block(self):
        """searches_for returning the right number is worth nothing if
        max_uses does not carry it."""
        tools = prompts.exploration_tools(prompts.CONJUNCTION_SEARCHES)
        assert tools[0]["max_uses"] == prompts.CONJUNCTION_SEARCHES
        assert prompts.exploration_tools()[0]["max_uses"] == prompts.BASE_SEARCHES

    def test_the_budget_stays_affordable(self):
        """Arithmetic, not opinion. 12 candidates a pass is the cap; at
        $0.01 a search the search spend of one full pass must stay small
        against a monthly budget measured in tens of dollars."""
        worst_case = (MAX_CANDIDATES_PER_PASS
                      * prompts.CONJUNCTION_SEARCHES * 0.01)
        assert worst_case <= 2.00, f"${worst_case:.2f} of search in ONE pass"


class TestTheModelIsToldAboutTheLink:
    """A link that exists only in the grouping code is not reasoning."""

    def _prompt(self, ticker="ACME"):
        events = cross(ticker)
        cands, _ = build_conjunction_candidates(events, NOW)
        signals = signals_from_events(events)[ticker]
        return prompts.render_research_prompt(cands[0], signals=signals)

    def test_each_feed_is_quoted_separately_with_its_date(self):
        text = self._prompt()
        assert "WHAT EACH FEED SAID, INDEPENDENTLY" in text
        assert "edgar_fts" in text and "edgar_form4" in text
        assert "not written with each other in mind" in text

    def test_it_asks_whether_they_CONNECT(self):
        text = self._prompt()
        assert "DO THESE CONNECT?" in text

    def test_it_is_told_coincidence_is_the_alternative(self):
        """Without this the model has every reason to manufacture a
        thesis: it was handed two facts and asked what they mean."""
        text = self._prompt()
        assert "coincidence" in text
        assert "no_trade on a coincidence is worth more" in text

    def test_the_crude_sentiment_tag_is_labelled_as_crude(self):
        events = cross()
        events.append(ev("ACME", "earnings_result", source="alpaca_news",
                         headline="Acme Q2 EPS Misses Estimate", hint=-1))
        cands, _ = build_conjunction_candidates(events, NOW)
        text = prompts.render_research_prompt(
            cands[0], signals=signals_from_events(events)["ACME"])
        assert "pattern-matched as BAD" in text
        assert "not a judgement" in text
        assert "Disagree with them freely" in text

    def test_it_does_NOT_claim_a_resolution_date(self):
        """The feeds say something was said; the date it resolves is in
        the body of the filing and nothing has read that. Presenting it
        as a catalyst date would put a confirmed date on a guess."""
        text = self._prompt()
        assert "not a resolution date" in text.lower()

    def test_an_ordinary_candidate_keeps_the_insider_framing(self):
        """The single-feed path must be byte-identical - it is the
        graded strategy."""
        from catalyst.discovery import Candidate

        cand = Candidate(
            id="c1", ticker="ZZZ", catalyst_type="insider_cluster",
            catalyst_date=NOW.date(), catalyst_date_confidence="confirmed",
            source_event_ids=("a",), discovered_at=NOW, sector="tech",
            correlation_tags=())
        text = prompts.render_research_prompt(cand)
        assert "distinct insiders bought" in text
        assert "DO THESE CONNECT?" not in text


class TestConjunctionCandidates:
    def test_two_feeds_agreeing_becomes_a_candidate(self):
        cands, _ = build_conjunction_candidates(cross(), NOW)
        assert len(cands) == 1
        assert cands[0].ticker == "ACME"
        assert cands[0].catalyst_date_confidence == "estimated"

    def test_one_feed_agreeing_with_itself_does_not(self):
        cands, dropped = build_conjunction_candidates(
            [ev("BBIO", "clinical_readout"), ev("BBIO", "fda_decision")], NOW)
        assert cands == []
        assert any("one filing trail" in why for _t, why in dropped)

    def test_a_stale_conjunction_is_dropped_with_its_reason(self):
        cands, dropped = build_conjunction_candidates(
            cross(days_ago=MAX_SIGNAL_AGE_DAYS + 5), NOW)
        assert cands == []
        assert any("past the" in why and "window" in why for _t, why in dropped)

    def test_evidence_dated_after_the_pass_is_invisible(self):
        """Point-in-time, exactly as the Form 4 clusterer does it."""
        cands, dropped = build_conjunction_candidates(cross(days_ago=-3), NOW)
        assert cands == []
        assert any("dated after this pass" in why for _t, why in dropped)

    def test_the_pass_is_capped_and_says_what_it_dropped(self):
        """All one sector, so the SECTOR cap bites first - which is the
        whole point. Eighteen biotechs is one bet wearing eighteen hats,
        and paying to research all of them is money spent before the
        risk engine ever gets to decline them."""
        events = []
        for i in range(MAX_CANDIDATES_PER_PASS + 6):
            events += cross(f"T{i:02d}")            # all SIC 2834
        cands, dropped = build_conjunction_candidates(events, NOW)
        assert len(cands) == MAX_PER_SECTOR_PER_PASS
        assert all("already has" in why for _t, why in dropped)


class TestNoIndustryBias:
    """Owner-asked: "I want no industry bias i want it to find any
    opportunity regardless from microsoft to a farming company to a
    chemical company anything".

    Measured live 2026-08-11: 10 of 12 candidates were SIC 2834. Two
    causes, both fixed here - a query table where 3 of 7 queries could
    only match pharma, and a selection that took the strongest twelve
    outright."""

    def _mixed(self):
        """One conjunction each in eight different industries."""
        sics = {"AGRI": "0100", "MINE": "1311", "CHEM": "2810",
                "BIO": "2834", "MFG": "3711", "RETL": "5311",
                "BANK": "6021", "TECH": "7372"}
        events = []
        for ticker, sic in sics.items():
            events += [ev(ticker, "earnings", source="edgar_fts", sic=sic),
                       ev(ticker, "insider_cluster", source="edgar_form4",
                          sic=sic)]
        return events

    def test_every_industry_gets_a_slot_before_any_gets_a_second(self):
        cands, _ = build_conjunction_candidates(self._mixed(), NOW)
        bands = {sector_band(c.sector) for c in cands}
        assert len(bands) >= 7, f"only {len(bands)} industries: {bands}"

    def test_one_industry_cannot_take_the_whole_pass(self):
        """Twenty biotechs and one chemical company: the chemical company
        must still be researched."""
        events = []
        for i in range(20):
            events += cross(f"B{i:02d}")                      # SIC 2834
        events += [ev("CHEM", "earnings", source="edgar_fts", sic="2810"),
                   ev("CHEM", "insider_cluster", source="edgar_form4",
                      sic="2810")]
        cands, _ = build_conjunction_candidates(events, NOW)
        tickers = {c.ticker for c in cands}
        assert "CHEM" in tickers, "the one non-biotech was crowded out"
        biotech = sum(1 for c in cands
                      if sector_band(c.sector) == "pharma and biotech")
        assert biotech <= MAX_PER_SECTOR_PER_PASS

    def test_the_drop_reason_names_the_industry(self):
        events = []
        for i in range(10):
            events += cross(f"B{i:02d}")
        _c, dropped = build_conjunction_candidates(events, NOW)
        assert any("pharma and biotech already has" in why
                   for _t, why in dropped)

    def test_most_queries_are_not_pharma_only(self):
        """The table itself. Three of seven queries could only ever
        match pharma; that is a bias designed in, not observed."""
        from catalyst.data.sources.edgar_fts import QUERIES

        pharma_only = {"pdufa", "topline_expected", "phase3_endpoint"}
        assert len(QUERIES) >= 14
        assert len(pharma_only) / len(QUERIES) < 0.25

    def test_every_query_carries_its_measured_volume(self):
        """A query nobody measured is a guess that costs a request every
        cycle - one candidate query returned zero hits in 21 days."""
        from catalyst.data.sources.edgar_fts import QUERIES

        for q in QUERIES:
            assert q.measured_21d > 0, f"{q.key} has no measured volume"

    def test_the_rejected_queries_keep_their_evidence(self):
        """So nobody re-adds them on intuition."""
        from catalyst.data.sources.edgar_fts import REJECTED_QUERIES

        text = " ".join(why for _q, why in REJECTED_QUERIES)
        assert "0 hits" in text and "93% pharma" in text

    def test_sector_band_covers_the_owners_examples(self):
        assert sector_band("7372") == "services and technology"   # Microsoft
        assert sector_band("0100") == "agriculture"               # farming
        assert sector_band("2810") == "chemicals"                 # chemicals
        assert sector_band("2834") == "pharma and biotech"
        assert sector_band("") == "unknown"

    def test_a_pharma_label_does_not_outrank_a_real_problem(self):
        """distress and dilution move the share count or threaten the
        company; they must name the candidate ahead of a pending
        readout."""
        events = [ev("XX", "clinical_readout", source="edgar_fts"),
                  ev("XX", "dilution", source="alpaca_news")]
        cands, _ = build_conjunction_candidates(events, NOW)
        assert cands[0].catalyst_type == "dilution"

    def test_the_id_is_stable_across_passes(self):
        """INSERT OR IGNORE makes a re-run idempotent only if the same
        conjunction keeps the same id - otherwise every pass re-researches
        what it already paid for."""
        a, _ = build_conjunction_candidates(cross(), NOW)
        b, _ = build_conjunction_candidates(cross(), NOW + timedelta(hours=6))
        assert a[0].id == b[0].id

    def test_the_riskiest_kind_names_the_candidate(self):
        """catalyst_type drives the risk engine's gap and stop
        assumptions, so a pending FDA binary must not be filed under
        'analyst_action' because that sorted first."""
        events = [ev("XYZ", "analyst_action", source="alpaca_news"),
                  ev("XYZ", "fda_decision", source="edgar_fts")]
        cands, _ = build_conjunction_candidates(events, NOW)
        assert cands[0].catalyst_type == "fda_decision"

    def test_the_kinds_become_correlation_tags(self):
        """Four biotech binaries resolving together are one bet. The risk
        engine's cluster bound can only see that if the tags carry it."""
        cands, _ = build_conjunction_candidates(cross(), NOW)
        tags = cands[0].correlation_tags
        assert "earnings" in tags and "insider_cluster" in tags
        assert "sic-2834" in tags


class TestTheGradedStrategyIsNotWeakened:
    def test_form4_events_DO_participate_in_a_conjunction(self):
        """The owner's worked example - "insiders bought AND an earnings
        call is scheduled" - is only findable because a Form 4 event can
        be one half of a cross-feed link. An earlier comment claimed
        Form 4 was excluded and named a constant the code never read;
        the code was right and the comment was wrong."""
        cands, _ = build_conjunction_candidates(cross(), NOW)
        assert [c.ticker for c in cands] == ["ACME"]

    def test_form4_alone_still_produces_no_conjunction(self):
        """It participates; it does not conjure a link with itself."""
        only_form4 = [ev("ACME", "insider_cluster", source="edgar_form4"),
                      ev("ACME", "insider_cluster", source="edgar_form4",
                         sid="second")]
        cands, _ = build_conjunction_candidates(only_form4, NOW)
        assert cands == []

    def test_one_company_is_never_researched_twice_in_a_pass(self):
        """THE ACTUAL RISK the old comment was reaching for. A ticker
        with a Form 4 cluster AND a news story yields two candidates with
        different ids - ~34c each, and two of the three research slots
        spent on one company.

        Tested by RUNNING the merge, not by reading the source: the
        guard this replaces asserted on a constant the code never read,
        which is exactly why the duplicate went unnoticed."""
        from catalyst.discovery.conjunctions import merge_with_form4

        form4, _ = build_conjunction_candidates(cross("DUP"), NOW)
        conj, _ = build_conjunction_candidates(cross("DUP"), NOW)
        # same ticker reached both builders
        assert form4 and conj and form4[0].ticker == conj[0].ticker
        kept, dropped = merge_with_form4(form4, conj)
        assert len(kept) == 1, "one company was researched twice"
        assert any("pay twice for one company" in why for _t, why in dropped)

    def test_a_different_company_is_NOT_dropped_by_the_merge(self):
        """The de-duplication must be by ticker, not a blanket refusal
        of conjunction candidates."""
        from catalyst.discovery.conjunctions import merge_with_form4

        form4, _ = build_conjunction_candidates(cross("AAA"), NOW)
        conj, _ = build_conjunction_candidates(cross("BBB"), NOW)
        kept, dropped = merge_with_form4(form4, conj)
        assert {c.ticker for c in kept} == {"AAA", "BBB"}
        assert dropped == []
