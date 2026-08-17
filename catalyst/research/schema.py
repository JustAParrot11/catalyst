"""The ResearchView contract - the model/code boundary object.

HUMAN REVIEW REQUIRED on any change to this file (ARCHITECTURE.md
section 8; CLAUDE.md house rule 5).

ResearchView deliberately has NO field that can hold a position size, a
share count, a dollar amount, or an order type. That absence is the
primary enforcement of "the model proposes, deterministic code disposes"
(ARCHITECTURE.md section 4.1). Do not add one.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class ResearchView:
    candidate_id: str
    direction: Literal["long", "short", "no_trade"]
    conviction: float                # 0.0-1.0, the model's stated confidence
    thesis: str                      # free text, audit trail only
    invalidation: str                # what would prove this wrong
    expected_holding_days: int
    priced_in: bool
    priced_in_reasoning: str


@dataclass(frozen=True)
class UsageComponents:
    """Mirrors the raw Anthropic usage object field-for-field.

    TRAPS.md: cache tokens are billed but excluded from input_tokens;
    missing them understates the bill by about half. cost.tracker.price()
    is the only function permitted to turn this into a dollar figure.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    web_search_requests: int
    raw: dict = field(hash=False)    # the verbatim API usage object, always


@dataclass(frozen=True)
class APITurn:
    """One API request within an investigation. An investigation may span
    several: zero or more exploration turns (tool_choice=auto, may call
    web_search) followed by exactly one forced-choice extraction turn.
    Each turn is priced independently and summed - never just the last.
    """

    turn_index: int
    raw_response: dict = field(hash=False)
    usage: UsageComponents = None    # type: ignore[assignment]
    stop_reason: str = ""


@dataclass(frozen=True)
class ResearchCallLog:
    id: str
    candidate_id: str
    model: str
    prompt_rendered: str
    tools_offered: tuple[str, ...]
    api_turns: tuple[APITurn, ...]
    parsed_view: ResearchView | None
    cost_cents: Decimal              # sum of tracker.price(t.usage) over api_turns
    latency_ms: int
    skipped_reason: str | None       # "budget_denied", "api_error", ...


# The tool schema Claude is forced to call on the extraction turn.
# Generated from ResearchView's field set - nothing else. Keeping it
# adjacent to the dataclass so a drift between the two is visible in one
# diff (they must always change together, under human review).
SUBMIT_RESEARCH_VIEW_TOOL = {
    "name": "submit_research_view",
    "description": (
        "Submit your structured research conclusion for this candidate. "
        "This is the only accepted output format."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["long", "short", "no_trade"],
                "description": (
                    "The direction you would take if a position were "
                    "opened. Use no_trade when the evidence does not "
                    "support either side - a justified no_trade is a "
                    "good answer, not a failure."
                ),
            },
            # THE FIELD THAT HAD NO DESCRIPTION AT ALL, and the defect
            # that cost the owner every trade. Measured over the first 31
            # live views: every LONG scored between 0.30 and 0.45 while
            # the floor deciding whether to trade sat at 0.60. Nothing
            # anywhere told the model what the number meant, so it
            # calibrated on instinct while the code read the same digits
            # as a probability. Two scales, never reconciled, and a
            # system arithmetically incapable of opening a position.
            #
            # It is defined as a FREQUENCY because that is the only form
            # the refusal tracker can grade: score enough 0.6 calls and
            # roughly six in ten should have worked, or the number is
            # wrong and the evidence says by how much.
            #
            # It deliberately does NOT say what the floor is. Naming the
            # bar teaches the model to clear it, which converts the one
            # measurement worth having into a formality.
            "conviction": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": (
                    "How often this call would be RIGHT across many "
                    "setups that looked like this one - a frequency, not "
                    "a feeling. 0.50 is a coin flip: the move is as "
                    "likely to go against you as with you. 0.60 means "
                    "about six such setups in ten resolve your way. 0.75 "
                    "means three in four. Above 0.85 should be rare and "
                    "needs evidence a careful reader would also find "
                    "compelling. Below 0.50 on a long or short is a "
                    "contradiction - if you would be wrong more often "
                    "than right, the direction is no_trade.\n"
                    "On no_trade this is your confidence that NOT "
                    "trading is correct, judged the same way.\n"
                    "A deterministic threshold reads this number and "
                    "decides whether the trade happens, so it is a "
                    "measurement rather than a flourish. Do not inflate "
                    "it to get a trade through and do not shade it down "
                    "to look careful: both corrupt the only feedback "
                    "loop this system has."
                ),
            },
            "thesis": {
                "type": "string",
                "description": (
                    "The MECHANISM, in two to four sentences: what "
                    "specifically would move this price from here, why "
                    "that has not already happened, and what the insiders "
                    "plausibly knew that the market does not. Name the "
                    "figures you are relying on - who bought, how much, "
                    "at what price against what recent range. A thesis "
                    "that would read the same for any insider cluster in "
                    "any company is not a thesis."
                ),
            },
            "invalidation": {
                "type": "string",
                "description": (
                    "The single observable fact that would prove this "
                    "wrong, stated so a person could check it without "
                    "asking you. A price level, a filing, a date, a "
                    "number in a report. 'The thesis does not play out' "
                    "is not checkable and is not an answer; this text is "
                    "re-read later to decide whether to close the "
                    "position early."
                ),
            },
            "expected_holding_days": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "Whole trading days you expect the thesis to need. "
                    "This strategy holds days to weeks and every position "
                    "carries a hard exit date, so answer with how long "
                    "the MECHANISM needs, not with how long you would "
                    "like to be given."
                ),
            },
            "priced_in": {
                "type": "boolean",
                "description": (
                    "True only if the market has already consumed these "
                    "filings. Judge it on what price and volume did after "
                    "each filing became public and whether the cluster "
                    "has been reported anywhere. A priced_in call you "
                    "cannot support with a figure should be false."
                ),
            },
            "priced_in_reasoning": {
                "type": "string",
                "description": (
                    "The evidence behind that boolean, with numbers: the "
                    "move since the filing date, the volume against its "
                    "normal, where it sits in its recent range, what a "
                    "search did or did not turn up. 'Probably priced in' "
                    "with no figure behind it is not an answer."
                ),
            },
            # EVIDENCE, never a sizing input. Optional by design: a view
            # without findings is perfectly valid, and the trade decision
            # never depends on this field. It rides along in a pass
            # already paid for, which is why the evidence graph costs no
            # extra model call.
            "findings": {
                "type": "array",
                "description": (
                    "Optional. Concrete links you established while "
                    "researching - who bought, which filing said so, what "
                    "date it resolves. Each item: subject {kind, "
                    "canonical_key, display_name}, predicate, optional "
                    "object of the same shape, optional object_date, "
                    "source_class, reliability. Omit rather than guess."
                ),
                "items": {"type": "object"},
            },
        },
        "required": [
            "direction", "conviction", "thesis", "invalidation",
            "expected_holding_days", "priced_in", "priced_in_reasoning",
        ],
        "additionalProperties": False,
    },
}

_VIEW_FIELDS = set(SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["required"])

#: Accepted on the tool input but deliberately NOT part of the view the
#: risk engine reads. Evidence informs judgement; it must never reach
#: the object sizing sees (CLAUDE.md: the model never sizes a position).
_NON_VIEW_FIELDS = {"findings"}


def make_view_from_tool_input(candidate_id: str, tool_input: dict) -> ResearchView:
    """Strictly validate a submit_research_view tool call and build the
    boundary object. Raises (never guesses) on anything malformed - an
    invalid view becomes a skipped candidate, not a defaulted one.

    A size-shaped field arriving here ("qty", "notional", "position",
    "shares", "dollars") is refused by the unknown-field check: the
    schema has no such field and this function accepts nothing beyond
    the schema."""
    if not isinstance(tool_input, dict):
        raise TypeError(f"tool input is {type(tool_input).__name__}, not object")
    unknown = set(tool_input) - _VIEW_FIELDS - _NON_VIEW_FIELDS
    if unknown:
        raise ValueError(f"unknown fields in research view: {sorted(unknown)}")
    missing = _VIEW_FIELDS - set(tool_input)
    if missing:
        raise KeyError(f"missing fields in research view: {sorted(missing)}")

    direction = tool_input["direction"]
    if direction not in ("long", "short", "no_trade"):
        raise ValueError(f"invalid direction: {direction!r}")
    conviction = tool_input["conviction"]
    if not isinstance(conviction, (int, float)) or isinstance(conviction, bool) \
            or not (0.0 <= conviction <= 1.0):
        raise ValueError(f"conviction out of [0,1]: {conviction!r}")
    holding = tool_input["expected_holding_days"]
    if not isinstance(holding, int) or isinstance(holding, bool) or holding < 1:
        raise ValueError(f"invalid expected_holding_days: {holding!r}")
    if not isinstance(tool_input["priced_in"], bool):
        raise ValueError(f"priced_in must be boolean: {tool_input['priced_in']!r}")
    for text_field in ("thesis", "invalidation", "priced_in_reasoning"):
        if not isinstance(tool_input[text_field], str) or not tool_input[text_field].strip():
            raise ValueError(f"{text_field} must be a non-empty string")

    return ResearchView(
        candidate_id=candidate_id,
        direction=direction,
        conviction=float(conviction),
        thesis=tool_input["thesis"],
        invalidation=tool_input["invalidation"],
        expected_holding_days=holding,
        priced_in=tool_input["priced_in"],
        priced_in_reasoning=tool_input["priced_in_reasoning"],
    )
