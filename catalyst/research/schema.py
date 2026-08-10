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
            "direction": {"type": "string", "enum": ["long", "short", "no_trade"]},
            "conviction": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "thesis": {"type": "string"},
            "invalidation": {"type": "string"},
            "expected_holding_days": {"type": "integer", "minimum": 1},
            "priced_in": {"type": "boolean"},
            "priced_in_reasoning": {"type": "string"},
        },
        "required": [
            "direction", "conviction", "thesis", "invalidation",
            "expected_holding_days", "priced_in", "priced_in_reasoning",
        ],
        "additionalProperties": False,
    },
}
