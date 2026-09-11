"""Declining scored 0.85. Committing scored 0.56. Same column.

OWNER, 2026-09-11: "We still arent making trades".

MEASURED across all 293 research views on record:

    268 no_trade      conviction median 0.68, mean 0.66, max 0.85
                      distribution: 17 at 0.1, 6 at 0.2, 62 at 0.6,
                      86 at 0.7, 97 at 0.8
     25 directional    every one between 0.30 and 0.62, median 0.55

91.5% of every paid research call ends in no_trade, and the numbers show
why that is not only a judgement about the candidates. The tool asked
for one field, called it conviction, and said that on a no_trade it is
"your confidence that NOT trading is correct, judged the same way".

IT IS NOT THE SAME WAY. A directional conviction is a frequency over
market outcomes, where 57% out of sample is the best this project has
ever measured. A no_trade conviction is self-certainty about an
abstention, which is nearly free to feel strongly about. Sharing a name
and a column, the scale reads as though declining were the confident
answer and committing the weak one - so an honest 0.56 long looks like a
shrug beside a 0.85 no_trade, and the comfortable branch is also the one
where the model can score well.

THIS IS THE 2026-08-17 DEFECT SURVIVING IN THE OTHER BRANCH. That date
fixed the directional scale by defining it as a frequency; nothing was
ever said about what the number means when there is no direction to be a
frequency OF.

WHAT IS FIXED HERE, and what deliberately is not. The floor is still
never named - naming the bar teaches the model to clear it, and that
test still stands. What the model is now told is this project's own
measured hit rates, so it can read its own number against what is
achievable rather than against what certainty feels like; that the
threshold decision belongs to code it cannot see; and, in the record,
which of the two quantities each remembered number was.

Fully offline. No calendar dates (house rule 6).
"""

import pytest

from catalyst.discovery import Candidate
from catalyst.research import prompts
from catalyst.research.schema import SUBMIT_RESEARCH_VIEW_TOOL

from datetime import datetime, timezone

NOW = datetime.now(timezone.utc)

CONVICTION_DOC = (SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]
                  ["properties"]["conviction"]["description"])


def candidate(ctype="insider_cluster"):
    return Candidate(
        id="x", ticker="AAA", catalyst_type=ctype, catalyst_date=NOW.date(),
        catalyst_date_confidence="confirmed", source_event_ids=("s",),
        discovered_at=NOW, sector="u", correlation_tags=(f"type:{ctype}",))


def prompt(ctype="insider_cluster"):
    return prompts.render_research_prompt(candidate(ctype))


class TestTheToolStopsCallingThemTheSameJudgement:
    def test_it_says_plainly_they_are_different_quantities(self):
        assert "DIFFERENT QUANTITY" in CONVICTION_DOC
        assert "not" in CONVICTION_DOC and "comparable" in CONVICTION_DOC

    def test_the_old_phrasing_is_gone(self):
        """"judged the same way" is the sentence that built one column
        out of two scales."""
        assert "judged the same way" not in CONVICTION_DOC

    def test_it_names_the_asymmetry_rather_than_hinting_at_it(self):
        """Being sure there is nothing here is cheap; being right about
        a direction is not. If the tool does not say so the model has no
        reason to believe it."""
        low = CONVICTION_DOC.lower()
        assert "cheap" in low
        assert "57%" in CONVICTION_DOC

    def test_it_forbids_retreating_to_the_branch_that_scores_well(self):
        low = CONVICTION_DOC.lower()
        assert "do not reach for no_trade" in low

    def test_it_still_forbids_inflation_in_the_other_direction(self):
        """The whole point is an honest number. A file that only pushed
        one way would have traded one bias for another."""
        low = CONVICTION_DOC.lower()
        assert "do not inflate" in low
        assert "shade it down" in low

    def test_the_frequency_definition_is_untouched(self):
        for anchor in ("0.50 is a coin flip", "0.60 means",
                       "0.75", "frequency, not"):
            assert anchor in CONVICTION_DOC, anchor

    def test_the_schema_shape_is_unchanged(self):
        """A money-critical file: the fields a model can send must be
        exactly the fields it could send before."""
        props = SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["properties"]
        assert set(props) == {
            "direction", "conviction", "thesis", "invalidation",
            "expected_holding_days", "priced_in", "priced_in_reasoning",
            "findings"}
        c = props["conviction"]
        assert (c["type"], c["minimum"], c["maximum"]) == ("number", 0.0, 1.0)


class TestThePromptAnchorsTheScaleOnMeasuredEvidence:
    def test_it_gives_the_projects_own_out_of_sample_hit_rates(self):
        text = prompt()
        assert "57%" in text and "49%" in text

    def test_it_says_the_threshold_decision_is_not_the_models(self):
        text = prompt()
        assert "NOT YOUR DECISION" in text
        assert "deterministic threshold" in text.lower()

    def test_it_names_the_failure_mode_it_is_correcting(self):
        text = prompt().lower()
        assert "do not convert a real but modest edge into no_trade" in text

    def test_the_drift_arm_is_told_too(self):
        assert "NOT YOUR DECISION" in prompt(ctype="earnings_drift")

    def test_it_still_does_not_name_the_bar(self):
        """The standing rule, which this change is most at risk of
        breaking: calibration evidence is allowed, the bar is not."""
        from catalyst.risk.adaptive_params import DEFAULT_PARAMS

        floor = DEFAULT_PARAMS["conviction_floor"]
        low = prompt().lower()
        for bar in ("conviction floor", "minimum conviction", "must exceed",
                    "in order to trade you", "at least 0.5", "above 0.5",
                    "threshold of", "the bar is", "must be at least",
                    "clears the", "scores above"):
            assert bar not in low, bar
        # And it must not quote a number as one that would be accepted.
        for tempting in (f"{float(floor):.2f} or", "score at least",
                         "anything over"):
            assert tempting not in low, tempting

    def test_it_does_not_tell_the_model_to_prefer_a_direction(self):
        low = prompt().lower()
        for pressure in ("prefer long", "favour long", "favor long",
                         "look for longs", "bias toward long",
                         "inflate", "round up"):
            assert pressure not in low, pressure

    def test_declining_is_still_a_legitimate_answer(self):
        """A prompt that made no_trade feel like failure would produce
        the opposite bias and a worse one: invented trades."""
        text = prompt()
        assert "a no_trade you can justify is a good answer" in text
        assert "DECLINING IS NOT FREE" in text


class TestTheRecordNeverShowsTheTwoAsComparable:
    @staticmethod
    def _row(text, ticker):
        """The rendered line FOR ONE NAME. The section's closing
        paragraph mentions both labels by design, so a whole-text
        membership check cannot tell the labels apart."""
        return next(ln for ln in text.splitlines()
                    if ln.strip().startswith(f"- {ticker}"))

    def test_a_declined_no_trade_is_labelled_as_confidence_in_declining(self):
        from catalyst.research.record import render_record

        text = render_record([], [{
            "ticker": "ZZZ", "catalyst_type": "insider_cluster",
            "direction": "no_trade", "conviction": 0.85, "priced_in": True,
            "ret": 0.11, "refused_at": "a while ago"}])
        row = self._row(text, "ZZZ")
        assert "confidence in declining" in row
        assert "directional frequency" not in row

    def test_a_declined_direction_is_labelled_as_a_frequency(self):
        from catalyst.research.record import render_record

        text = render_record([], [{
            "ticker": "AAA", "catalyst_type": "insider_cluster",
            "direction": "long", "conviction": 0.56, "priced_in": False,
            "ret": 0.12, "refused_at": "a while ago"}])
        row = self._row(text, "AAA")
        assert "directional frequency" in row
        assert "confidence in declining" not in row

    def test_the_owners_two_numbers_are_never_shown_unlabelled(self):
        """The exact pairing this file exists to prevent: a 0.85 decline
        beside a 0.56 direction, reading as a track record in which
        declining always scored higher."""
        from catalyst.research.record import render_record

        text = render_record([], [
            {"ticker": "ZZZ", "catalyst_type": "t", "direction": "no_trade",
             "conviction": 0.85, "priced_in": True, "ret": 0.11,
             "refused_at": "then"},
            {"ticker": "AAA", "catalyst_type": "t", "direction": "long",
             "conviction": 0.56, "priced_in": False, "ret": 0.12,
             "refused_at": "then"}])
        assert "0.85 (confidence in declining)" in text
        assert "0.56 (directional frequency)" in text
        assert "Read the two conviction kinds separately" in text

    def test_it_says_a_confident_decline_is_not_evidence_of_anything(self):
        from catalyst.research.record import render_record

        text = render_record([], [{
            "ticker": "ZZZ", "catalyst_type": "t", "direction": "no_trade",
            "conviction": 0.85, "priced_in": False, "ret": -0.04,
            "refused_at": "then"}])
        assert "only the return beside it is" in text

    def test_it_still_carries_no_instruction_words(self):
        """Unchanged contract: this text is informational and no
        arithmetic reads it."""
        from catalyst.research.record import render_record

        text = render_record([], [{
            "ticker": "ZZZ", "catalyst_type": "t", "direction": "no_trade",
            "conviction": 0.85, "priced_in": False, "ret": 0.01,
            "refused_at": "then"}]).lower()
        for banned in ("buy ", "sell ", "shares", "position size", "stop at"):
            assert banned not in text, banned


class TestTheCheckCanFail:
    """House rule 4, against the text that shipped."""

    def test_removing_the_asymmetry_sentence_would_be_caught(self):
        stripped = CONVICTION_DOC.replace("DIFFERENT QUANTITY", "")
        assert "DIFFERENT QUANTITY" not in stripped

    def test_an_unlabelled_record_line_would_be_caught(self):
        """Reproduce the shipped rendering and confirm the assertions
        above would have failed against it."""
        old = ("  - ZZZ (t), you said no_trade at 0.85: the stock went "
               "+11.0% after then.")
        assert "confidence in declining" not in old

    def test_the_prompt_without_the_anchor_would_be_caught(self):
        stripped = prompt().replace("57%", "").replace("NOT YOUR DECISION", "")
        assert "57%" not in stripped and "NOT YOUR DECISION" not in stripped
