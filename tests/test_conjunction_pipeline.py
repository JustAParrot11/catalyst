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
    MAX_CANDIDATES_PER_PASS, MAX_SIGNAL_AGE_DAYS, build_conjunction_candidates,
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
        events = []
        for i in range(MAX_CANDIDATES_PER_PASS + 6):
            events += cross(f"T{i:02d}")
        cands, dropped = build_conjunction_candidates(events, NOW)
        assert len(cands) == MAX_CANDIDATES_PER_PASS
        assert sum(1 for _t, why in dropped if "cap" in why) == 6

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
    def test_a_conjunction_never_re_derives_a_form4_cluster(self):
        """One piece of evidence must not produce two funnel rows. Form 4
        has its own clusterer - line-for-line the backtest arm - and this
        builder consumes its events only as the OTHER half of a link."""
        import inspect

        from catalyst.discovery import conjunctions

        assert "edgar_form4" not in conjunctions.SOURCES

    def test_form4_only_events_produce_no_conjunction_candidates(self):
        only_form4 = [ev("ACME", "insider_cluster", source="edgar_form4"),
                      ev("ACME", "insider_cluster", source="edgar_form4",
                         sid="second")]
        cands, _ = build_conjunction_candidates(only_form4, NOW)
        assert cands == []
