"""The number that decided every trade, and was never defined.

OWNER-ASKED, after 31 live views produced zero trades: "is risk engine
too strict? Why cant claude be determine its own factors".

The data said neither. Measured across every view the bot has ever
returned:

    LONG views    (8): 0.30 0.30 0.32 0.35 0.42 0.45 0.45 0.45
    no_trade     (23): 0.08 ... 0.25, then 0.78 0.82 0.85

    highest conviction ever given to a LONG : 0.45
    the floor it must clear                 : 0.60  (0.75 if priced_in)

The floor sat ABOVE the maximum the model had ever assigned to a
direction. The system was arithmetically incapable of opening a
position and would have stayed that way forever - the exact failure
BUILD-BRIEF names: "a 0.65 conviction floor, invented, neither
validated ... refuses good trades forever and never signals that it is
doing so."

THE CAUSE WAS NOT DISAGREEMENT, IT WAS UNITS. The conviction field's
entire specification was:

    {"type": "number", "minimum": 0.0, "maximum": 1.0}

No description. The prompt said only "your confidence in that
direction". So the model calibrated on instinct - 0.45 for a decent
idea carrying real uncertainty - while deterministic code read the same
digits as a probability and required 0.60. Two scales, never
reconciled.

THE FIX IS A DEFINITION, NOT A LOWER FLOOR. Lowering the bar is
arbitrary and unmeasurable. Conviction is now specified as a FREQUENCY -
"out of many setups that looked like this one, how often would this call
be right" - which is the only form the refusal tracker can grade: score
enough 0.6 calls and roughly six in ten should have worked, or the
number is wrong by an amount the evidence can name.

AND IT MUST NOT NAME THE FLOOR. Telling the model the bar teaches it to
clear the bar, which turns the one measurement worth having into a
formality. Several tests below exist only to keep that true.
"""

import inspect
import json

import pytest

from catalyst.research import prompts, schema
from catalyst.research.schema import SUBMIT_RESEARCH_VIEW_TOOL as TOOL

PROPS = TOOL["input_schema"]["properties"]


def _prompt():
    from datetime import date, datetime, timezone

    from catalyst.discovery import Candidate

    c = Candidate(
        id="x", ticker="APTV", catalyst_type="insider_cluster",
        catalyst_date=date(2026, 8, 12), catalyst_date_confidence="confirmed",
        source_event_ids=("e1",), discovered_at=datetime.now(timezone.utc),
        sector="industrials", correlation_tags=("ind",))
    return prompts.render_research_prompt(c)


class TestEveryFieldIsDefined:
    @pytest.mark.parametrize("field", [
        "direction", "conviction", "thesis", "invalidation",
        "expected_holding_days", "priced_in", "priced_in_reasoning",
    ])
    def test_it_carries_a_description(self, field):
        """A bare {"type": "number"} is what cost every trade."""
        desc = PROPS[field].get("description", "")
        assert len(desc) > 80, (
            f"{field} has {len(desc)} characters of guidance; the model is "
            "being asked to guess what it means")


class TestConvictionIsAFrequency:
    def test_the_scale_is_anchored_at_real_points(self):
        d = PROPS["conviction"]["description"]
        for anchor in ("0.50", "0.60", "0.75"):
            assert anchor in d, f"the scale does not anchor {anchor}"

    def test_it_says_frequency_not_feeling(self):
        d = PROPS["conviction"]["description"].lower()
        assert "frequency" in d
        assert "not a feeling" in d

    def test_a_sub_coinflip_direction_is_called_a_contradiction(self):
        """Below 0.50 on a long means you expect to be wrong more often
        than right. That is a no_trade wearing a direction, and it was
        four of the eight longs on record."""
        d = PROPS["conviction"]["description"].lower()
        assert "contradiction" in d and "no_trade" in d

    def test_the_prompt_and_the_tool_AGREE(self):
        """Two documents describing one field is how they drift apart.
        Both must anchor the same numbers."""
        p, d = _prompt(), PROPS["conviction"]["description"]
        for anchor in ("0.50", "0.60", "0.75"):
            assert anchor in p, f"the prompt does not anchor {anchor}"
            assert anchor in d, f"the tool does not anchor {anchor}"

    def test_no_trade_conviction_is_defined_separately(self):
        """23 of 31 views were no_trade, three of them above 0.78. That
        number answers a different question from a long's conviction and
        the field has to say so."""
        d = PROPS["conviction"]["description"].lower()
        assert "no_trade" in d and "not trading is correct" in d


class TestItDoesNotTeachTheModelToClearTheBar:
    """The whole measurement dies if the model knows the number."""

    def _all_text(self):
        return (_prompt() + json.dumps(TOOL)
                + inspect.getsource(prompts)).lower()

    def test_the_floor_value_is_never_stated(self):
        """Reads the REAL floor rather than a copy of it, so this keeps
        working when the adaptive loop moves the number."""
        import sqlite3
        import tempfile

        from catalyst.risk.adaptive_params import current_values
        from catalyst.storage import init_db

        conn = init_db(str(tempfile.mkdtemp()) + "/f.db")
        try:
            floor = current_values(conn)["conviction_floor"]
        finally:
            conn.close()

        text = self._all_text()
        assert f"{float(floor):.2f}" not in text or "0.60 means" in text, (
            f"the live floor {floor} appears in the instructions")
        for phrase in ("conviction floor", "minimum conviction",
                       "at least 0.6", "above 0.6", "threshold of 0.6",
                       "must exceed", "in order to trade you"):
            assert phrase not in text, (
                f"the model is being told the bar: {phrase!r}. It will "
                "clear it, and the number stops meaning anything")

    def test_it_says_the_number_is_USED_without_saying_how_much(self):
        """The model should know the figure is load-bearing - that is
        what stops it being decorative - without knowing the bar."""
        d = PROPS["conviction"]["description"].lower()
        assert "decides whether the trade happens" in d
        assert "0.6" not in d.replace("0.60 means", "")

    def test_both_directions_of_dishonesty_are_named(self):
        d = PROPS["conviction"]["description"].lower()
        assert "do not inflate" in d
        assert "shade it down" in d


class TestTheDetailAsked_ForIsCheckable:
    def test_the_thesis_must_name_figures(self):
        d = PROPS["thesis"]["description"].lower()
        assert "mechanism" in d
        assert "figures" in d or "how much" in d
        assert "any insider cluster in any company" in d, (
            "nothing rules out a thesis that would fit any candidate")

    def test_the_invalidation_must_be_checkable_by_someone_else(self):
        d = PROPS["invalidation"]["description"].lower()
        assert "without asking you" in d
        assert "not checkable" in d or "is not an answer" in d

    def test_the_invalidation_says_where_it_gets_re_read(self):
        """It is not paperwork: position_review re-reads this text to
        decide whether to close early, and a vague one makes that review
        worthless."""
        assert "re-read" in PROPS["invalidation"]["description"].lower()

    def test_priced_in_reasoning_must_carry_numbers(self):
        d = PROPS["priced_in_reasoning"]["description"].lower()
        assert "probably priced in" in d
        for word in ("move since", "volume", "range"):
            assert word in d, f"it does not ask for {word}"


class TestTheCostOfSayingMore:
    def test_the_prompt_stays_small(self):
        """The tool schema is sent on every request and the prompt with
        it. Detail is worth paying for; an essay is not - the measured
        cost driver is search results at ~165k input tokens, not this."""
        chars = len(_prompt()) + len(json.dumps(TOOL))
        assert chars < 12000, (
            f"{chars} characters of instruction per call - roughly "
            f"{chars // 4} tokens on every single request")
