"""A conjunction's identity must be the QUESTION, not the newest headline.

cost-auditor, 2026-08-13. Conjunction ids were hashed from
(ticker, kinds, last_seen). `last_seen` is the date of the newest signal,
so one extra headline about the same company - same two feeds, same two
kinds, same conjunction - minted a brand new candidate id.

Two screens are keyed on candidate_id and both miss when it churns:

  - already_researched, so the conjunction is re-bought at full price.
    A conjunction is the expensive path: ten searches, ~36c today and
    ~50c once Sonnet 5's intro pricing ends.
  - the MAX_RESEARCH_ATTEMPTS bound, so a candidate that fails research
    repeatedly escapes the bound as soon as a fresh headline lands.

News feeds produce headlines continuously, so this bites hardest on
exactly the actively-covered names the bot most wants to look at.

The rule this file pins: MORE OF THE SAME KIND of evidence is the same
question and must not be paid for twice. A NEW KIND of evidence is a
different question and is allowed to be researched again.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from catalyst.discovery.conjunctions import build_conjunction_candidates

AS_OF = datetime.now(timezone.utc)


def _day(n):
    return (AS_OF.date() - timedelta(days=n)).isoformat()


@dataclass
class Ev:
    source: str
    payload_raw: dict
    id: str = "e"
    fetched_at: object = None


def _form4(day, ident):
    return Ev("edgar_form4", {"ticker": "ACME",
                              "catalyst_type": "insider_cluster",
                              "filed_date": _day(day)}, id=ident)


def _news(day, ident):
    return Ev("alpaca_news", {"ticker": "ACME", "catalyst_type": "news",
                              "filed_date": _day(day)}, id=ident)


def _ids(events):
    cands, _ = build_conjunction_candidates(events, AS_OF)
    return [c.id for c in cands]


class TestTheSameQuestionKeepsTheSameId:
    def test_an_extra_headline_does_not_mint_a_new_candidate(self):
        """THE MONEY. Same ticker, same two feeds, same two kinds - one
        more news item. That is not a new question."""
        before = [_form4(3, "f1"), _news(3, "n1")]
        after = before + [_news(2, "n2")]
        assert _ids(before), "the fixture must produce a conjunction at all"
        assert _ids(before) == _ids(after), (
            "one extra headline changed the candidate id, so every screen "
            "keyed on it misses and the conjunction is researched again")

    def test_the_id_survives_the_newest_signal_getting_newer(self):
        """The churn is driven by last_seen specifically: hold the
        evidence fixed and only move its date."""
        old = [_form4(5, "f1"), _news(5, "n1")]
        new = [_form4(5, "f1"), _news(1, "n1")]
        assert _ids(old) == _ids(new)

    def test_a_NEW_KIND_of_evidence_is_a_new_question(self):
        """The bound must not be so blunt that genuinely different
        evidence is ignored. A third, unrelated kind changes what is
        being asked and is allowed to be researched."""
        two_kinds = [_form4(3, "f1"), _news(3, "n1")]
        three_kinds = two_kinds + [
            Ev("federal_register", {"ticker": "ACME",
                                    "catalyst_type": "regulatory_meeting",
                                    "filed_date": _day(2)}, id="r1")]
        assert _ids(two_kinds) != _ids(three_kinds), (
            "a new KIND of evidence is a different question and should "
            "not be screened out as already researched")

    def test_different_tickers_still_differ(self):
        acme = _ids([_form4(3, "f1"), _news(3, "n1")])
        other = _ids([
            Ev("edgar_form4", {"ticker": "ZZZZ",
                               "catalyst_type": "insider_cluster",
                               "filed_date": _day(3)}, id="f2"),
            Ev("alpaca_news", {"ticker": "ZZZZ", "catalyst_type": "news",
                               "filed_date": _day(3)}, id="n2")])
        assert acme and other and acme != other


class TestTheDateItselfIsStillCarried:
    def test_catalyst_date_still_tracks_the_newest_signal(self):
        """Only the IDENTITY stops moving. The candidate must still
        report when its newest evidence landed - the age window and the
        funnel both read that date."""
        cands, _ = build_conjunction_candidates(
            [_form4(5, "f1"), _news(2, "n1")], AS_OF)
        assert cands
        assert cands[0].catalyst_date == AS_OF.date() - timedelta(days=2)

    def test_every_source_event_is_still_carried(self):
        """A stable id must not mean dropping the new evidence - the
        decision trail has to show everything the conjunction rested on."""
        cands, _ = build_conjunction_candidates(
            [_form4(3, "f1"), _news(3, "n1"), _news(2, "n2")], AS_OF)
        assert cands
        assert len(cands[0].source_event_ids) == 3, cands[0].source_event_ids
