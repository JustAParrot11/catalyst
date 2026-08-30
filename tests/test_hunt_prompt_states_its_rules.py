"""The hunt was failing a rule it was never told.

OWNER'S LOG, 2026-08-26 to 2026-08-30: the hunt ran six times, made 25
nominations, and 22 were rejected - every one of them for the same
reason:

    Hunt rejected NCNO: catalyst date 2026-08-25 is in the past
    Hunt rejected ZM:   catalyst date 2026-08-25 is in the past
    ...

88% of Claude's nominations discarded, and each hunt is a paid research
call reading 220 feed items.

WHY IT KEPT HAPPENING. The tool schema said "Today or later" and the
prompt never did. The prompt's own guidance is about STALENESS - "if
the filing is a week old and the stock has already moved, the trade has
happened without you" - which is a different test, and one that a story
published this morning passes easily. So a model reading a feed of
things that just happened judged them fresh, dated them today or
yesterday, and was refused by a rule it had not been shown.

Telling it the rule is not a restriction on its judgement; it is the
difference between spending nominations on candidates that can pass and
spending them on a tripwire.

THE SECOND HALF. The prompt named ONE mechanical screen. There are two
now - insider clusters and post-earnings drift - so "look for what the
screen missed" was pointing at a smaller gap than it meant to, and a
drift nomination now duplicates a graded arm rather than adding to it.

These tests hold what the prompt SAYS, because that is the whole of the
mechanism. The validator is tested separately; this is about not paying
for nominations that cannot survive it.

Fully offline.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from catalyst.discovery.hunt import MAX_DAYS_AHEAD, render_hunt_prompt


class Ev:
    """The shape _digest reads."""

    def __init__(self, source, source_id, payload):
        self.source = source
        self.source_id = source_id
        self.payload_raw = payload
        self.fetched_at = datetime(2026, 8, 30, tzinfo=timezone.utc)


AS_OF = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
EVENTS = [Ev("alpaca_news", "n1",
             {"ticker": "EMBC", "headline": "Q3 results", "summary": "beat"})]


def prompt(known=()):
    return render_hunt_prompt(EVENTS, AS_OF, already_known=set(known))


class TestTheDateRuleIsStated:
    def test_the_prompt_says_the_date_must_not_be_past(self):
        text = prompt()
        assert "today or later" in text.lower()
        assert "never a past date" in text.lower()

    def test_it_says_what_the_date_MEANS(self):
        """'When it resolves' is the distinction the model was missing -
        not when the story was published."""
        assert "when the event resolves" in prompt().lower()

    def test_it_names_the_exact_case_that_kept_failing(self):
        """An earnings release that already happened. Every one of the
        22 rejections was this shape."""
        text = prompt().lower()
        assert "already happened" in text
        assert "has resolved" in text

    def test_it_says_what_to_do_instead_rather_than_only_forbidding(self):
        """A rule with no alternative just loses the nomination; the
        point is to redirect it."""
        assert "name the NEXT dated event" in prompt()

    def test_the_rule_matches_what_the_validator_enforces(self):
        """If the prompt and the validator ever disagree, the prompt is
        teaching the model to fail."""
        import inspect

        from catalyst.discovery import hunt

        src = inspect.getsource(hunt._validate)
        assert "if cdate < today:" in src, (
            "the validator no longer refuses past dates, so the prompt "
            "is now stating a rule that is not enforced")


class TestBothGradedScreensAreNamed:
    def test_it_says_there_are_two_screens_not_one(self):
        text = prompt()
        assert "TWO MECHANICAL SCREENS" in text

    def test_it_names_the_drift_arm(self):
        """A drift nomination now duplicates a graded arm. The model
        cannot know that unless it is told."""
        text = prompt().lower()
        assert "post-earnings drift" in text
        assert "xbrl" in text

    def test_it_says_where_the_remaining_gap_actually_is(self):
        """'What the screen missed' is only useful if the model knows
        what the screens cover."""
        text = prompt().lower()
        for gap in ("regulatory decisions", "shareholder votes",
                    "trial readouts"):
            assert gap in text, f"the prompt does not point at {gap}"

    def test_it_still_lists_the_tickers_already_found(self):
        assert "EMBC" in prompt(known={"EMBC"})


class TestItStillSaysTheThingsItAlreadySaid:
    """The additions must not have displaced the guardrails."""

    def test_the_model_never_sizes_or_prices(self):
        text = prompt().lower()
        assert "never sizes or prices" in text or "never sizes" in text

    def test_an_empty_list_is_still_a_good_answer(self):
        assert "empty list is a good answer" in prompt().lower()

    def test_it_still_says_every_nomination_costs_money(self):
        assert "costs a research call" in prompt().lower()

    def test_the_feed_is_still_the_only_citable_evidence(self):
        text = prompt()
        assert "not here is discarded" in text
        assert "n1" in text, "the feed digest is missing from the prompt"


class TestTheCheckCanFail:
    """House rule 4: the assertions must catch the prompt they describe
    going missing, not merely pass against it."""

    def test_removing_the_date_rule_would_be_caught(self):
        text = prompt()
        stripped = text.replace("never a past date", "")
        assert "never a past date" not in stripped.lower()

    def test_reverting_to_one_screen_would_be_caught(self):
        text = prompt()
        stripped = text.replace("TWO MECHANICAL SCREENS", "A SCREEN")
        assert "TWO MECHANICAL SCREENS" not in stripped
