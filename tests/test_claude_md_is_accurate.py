"""CLAUDE.md must describe the bot that exists.

OWNER-ASKED: "is my original md file now obsolete? Can you re-write it
to actually be accurate about what the bot does so you arent referencing
old incorrect parameters".

It was. It said the capital was $1,000 when the account is $2,000, that
the budget ceiling was £20 when the owner had set $100, and that "the
strategy is not decided" long after one had been graded and shipped. A
spec that has drifted is worse than none: every session that reads it
starts from figures that were true once.

WHY THIS IS A TEST AND NOT A REVIEW HABIT. The file's whole purpose is
to be the first thing read, and prose has no compiler. The numbers in it
that are ALSO in the code are pinned here, so the two cannot drift apart
silently again - the same reasoning that put the daily ceiling and the
research belt behind derivations rather than constants.

Only the CHECKABLE claims are tested. Judgement, history and reasoning
are not, and should not be.
"""

import re
from decimal import Decimal
from pathlib import Path

import pytest

DOC = (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text()


class TestTheThrottleTableMatchesTheCode:
    """The table exists so nobody has to read governor.py to know what
    raising the cap does. That is only useful while it is true."""

    @pytest.mark.parametrize("monthly,daily,per_cycle,hunts", [
        (2000, "5.00", 3, 0),
        (10000, "10.00", 6, 2),      # 1 -> 2 on 2026-09-05 (supply, not budget, was binding)
        (30000, "30.00", 12, 4),
    ])
    def test_each_row_is_what_the_code_returns(
            self, monthly, daily, per_cycle, hunts):
        from catalyst.cost.governor import daily_cap_cents
        from catalyst.discovery.hunt import hunts_per_day
        from catalyst.orchestrator.cycle import research_per_cycle

        assert daily_cap_cents(monthly) == Decimal(daily) * 100
        assert research_per_cycle(monthly) == per_cycle
        assert hunts_per_day(monthly) == hunts

    def test_the_row_is_actually_printed_in_the_file(self):
        """A correct table nobody wrote down helps no one."""
        for cell in ("$100", "$10.00", "| 6 |", "| 2 |"):
            assert cell in DOC, f"the throttle table is missing {cell!r}"


class TestTheStatedFiguresAreTheRealOnes:
    def test_the_quote_cross_check_bounds(self):
        from catalyst.data.quote_check import FLAG_DEVIATION, REFUSE_RATIO

        assert f"{FLAG_DEVIATION * 100:.0f}%" in DOC
        assert f"{int(REFUSE_RATIO)}x" in DOC

    def test_the_years_of_history(self):
        """Prose legitimately spells small numbers, so accept either
        form. Forcing a digit to satisfy a test is the tail wagging the
        dog - the point is that the figure is RIGHT, not how it reads."""
        from catalyst.data.bar_history import HISTORY_YEARS

        words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
        spelled = words.get(HISTORY_YEARS, str(HISTORY_YEARS))
        assert (f"{HISTORY_YEARS} years" in DOC
                or f"{spelled} years" in DOC), (
            f"the cache holds {HISTORY_YEARS} years and the doc says "
            "something else")

    def test_the_submission_tool_field_count(self):
        from catalyst.research.schema import SUBMIT_RESEARCH_VIEW_TOOL as TOOL

        n = len(TOOL["input_schema"]["properties"])
        assert f"{'eight' if n == 8 else n} fields" in DOC, (
            f"the tool has {n} fields; the doc says otherwise")

    def test_the_conviction_anchors(self):
        from catalyst.research.schema import SUBMIT_RESEARCH_VIEW_TOOL as TOOL

        d = TOOL["input_schema"]["properties"]["conviction"]["description"]
        for anchor in ("0.50", "0.60", "0.75"):
            assert anchor in DOC and anchor in d

    def test_the_budget_hurdle_arithmetic(self):
        """$100/month against $2,000 is 60% a year. If that sum is wrong
        in the doc, every judgement about whether the bot is working
        starts from the wrong place."""
        capital, monthly = Decimal("2000"), Decimal("100")
        hurdle = monthly * 12 / capital * 100
        assert f"{hurdle:.0f}%" in DOC
        assert "$2,000" in DOC and "$100/month" in DOC


class TestTheNonNegotiableRuleIsStillStated:
    def test_the_doc_says_the_model_never_sizes(self):
        assert "never sizes a position or places an order" in DOC

    def test_and_the_code_still_makes_that_true(self):
        """The claim and the barrier, checked together. A doc asserting
        a safety property the code has stopped enforcing is worse than a
        doc that never mentioned it."""
        import inspect

        from catalyst.risk.sizing import size

        params = set(inspect.signature(size).parameters)
        assert not (params & {"view", "conviction", "research_view",
                              "price", "model_price"}), (
            "sizing can now reach the model's own numbers, and CLAUDE.md "
            "still promises it cannot")


class TestItIsHonestAboutWhatIsUnproven:
    """The section most likely to be quietly dropped as it becomes
    inconvenient, and the one that stops a bot looking finished while
    doing nothing."""

    def test_it_admits_the_bot_has_not_traded(self):
        assert "never traded" in DOC.lower()

    def test_it_admits_the_backtest_never_included_the_model(self):
        assert "judgement.py" in DOC
        src = (Path(__file__).resolve().parent.parent
               / "catalyst" / "backtest" / "judgement.py").read_text()
        stub = len([ln for ln in src.splitlines() if ln.strip()]) <= 5
        assert stub, (
            "judgement.py is no longer a stub, so CLAUDE.md's claim that "
            "the model has never been backtested may have stopped being "
            "true - check it")

    def test_it_no_longer_claims_the_model_learns_nothing(self):
        """This test used to hold the OPPOSITE: that the doc admitted
        no past outcome reached the prompt, and that prompts.py read no
        outcome. On 2026-09-05 research/record.py started rendering the
        closed trades and scored refusals into the prompt, so the doc
        has to say so - and say what is still unmeasured."""
        from catalyst.research import boundary, record

        assert "learns nothing between calls" not in DOC.lower()
        assert "sees its own record" in DOC.lower()
        assert "outcome_return" in inspect_source(record)
        assert "recent_record(conn)" in inspect_source(boundary), (
            "the doc says outcomes reach the prompt; nothing renders them")
        assert "unmeasured" in DOC.lower()


def inspect_source(module):
    import inspect

    return inspect.getsource(module)


class TestTheStaleFiguresAreGone:
    @pytest.mark.parametrize("stale,why", [
        ("$1,000, fixed", "the account is $2,000"),
        ("£20/month absolute ceiling", "the owner set $100"),
        ("The strategy is not decided", "one was graded and shipped"),
    ])
    def test_a_figure_that_is_no_longer_true_is_not_stated(self, stale, why):
        assert stale not in DOC, f"{stale!r} is still in CLAUDE.md, but {why}"

    def test_the_brief_is_marked_as_historical_not_current(self):
        """docs/BUILD-BRIEF.md is deliberately unchanged - it is the
        original goal and worth keeping as written. The doc has to say
        which one wins when they disagree, or a future session follows
        the wrong numbers."""
        assert "this file is what the code" in DOC
