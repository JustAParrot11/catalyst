"""Ask Claude whether an open position's thesis still holds.

HUMAN REVIEW REQUIRED - this can close a position.

The owner asked for it in these words: "a periodic checkin with current
open trades with claude to get an opinion on if it should continue to
hold or sell incase the news changes".

That is a real gap. A thesis is written once, at entry, and then the
world moves: the readout misses, the merger breaks, a competitor prints
better data, the CEO leaves. Until now nothing re-read the position
until its exit date arrived. The invalidation condition the model was
made to write at entry - "the observable fact that would prove the
thesis wrong" - was recorded and never checked against anything.

THE ONE RULE THAT MAKES THIS SAFE: A REVIEW CAN ONLY EVER SHORTEN A
HOLD, NEVER EXTEND IT.

The exit date is set once, at entry, and this module cannot move it
outward. Not by a day. If it could, the failure mode writes itself: a
position goes against you, you ask the model whether to hold, and a
model looking at a loss will find a reason - because a losing position
always has a story attached, and the story is usually true and usually
irrelevant. Each review would buy another week, and "days to weeks"
becomes "until it comes back". That is the single most common way a
disciplined strategy turns into a portfolio of hopes.

So the asymmetry is structural rather than a matter of prompting:

    exit_now  -> code closes the position early
    hold      -> nothing happens. The original exit date stands.

"Hold" is not an instruction the system acts on. It is the absence of a
reason to leave early, and it is recorded so the dashboard can show the
model was asked and what it said.

COST. One call per open position per review. At three to five positions
and two reviews a week that is roughly 40 calls a month; at the observed
~$0.03 a call that is about $1.20 against a $5 cap. Not free, so every
review goes through the cost governor exactly like research does, and a
denied review is a recorded skip rather than a silent no-op. Reviews are
also skipped entirely for a position in its first day and for one whose
exit is tomorrow anyway - neither can change what happens.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

#: What the model may answer. Deliberately three, and deliberately not a
#: number: a score invites a threshold, a threshold invites tuning, and
#: what is actually wanted here is "is there a reason to leave early".
ACTIONS = ("hold", "exit_now", "no_opinion")

#: Skip a review that cannot change anything. Both save a paid call.
MIN_AGE_DAYS_BEFORE_REVIEW = 1
MIN_DAYS_REMAINING_TO_BOTHER = 1

POSITION_REVIEW_TOOL = {
    "name": "submit_position_review",
    "description": (
        "Report whether the original thesis for this open position still "
        "holds. You are NOT being asked to size anything, to set a price, "
        "or to choose an exit date - the exit date is already fixed and "
        "cannot be extended by this review."),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string", "enum": list(ACTIONS),
                "description": (
                    "exit_now if the thesis is broken or its invalidation "
                    "condition has occurred. hold if it is intact - note "
                    "that hold changes nothing, the position closes on its "
                    "existing date either way. no_opinion if you could not "
                    "find enough to judge, which is a valid and useful "
                    "answer."),
            },
            "invalidation_triggered": {
                "type": "boolean",
                "description": (
                    "Has the specific invalidation condition written at "
                    "entry actually occurred? Answer on the facts, not on "
                    "whether the position is up or down."),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "What changed, or what did not, in one or two "
                    "sentences. Name the evidence."),
            },
            "what_changed": {
                "type": "array", "items": {"type": "string"},
                "description": (
                    "Specific new facts since entry. Empty if nothing "
                    "material has happened, which is the common case."),
            },
        },
        "required": ["action", "invalidation_triggered", "reasoning"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class PositionReview:
    position_id: str
    ticker: str
    action: str
    invalidation_triggered: bool
    reasoning: str
    what_changed: tuple = ()
    reviewed_at: datetime | None = None
    cost_cents: Decimal = Decimal("0")
    skipped_reason: str | None = None

    @property
    def wants_early_exit(self) -> bool:
        """The ONLY thing this object can cause. Note that it does not
        cause it - the caller decides, and the caller is code."""
        return self.action == "exit_now"


@dataclass
class ReviewOutcome:
    reviews: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def make_review_from_tool_input(position_id: str, ticker: str,
                                tool_input: dict) -> PositionReview:
    """Validate the model's answer, or raise.

    A malformed review is a SKIP, never a default. Defaulting a
    missing action to "hold" would mean a broken model silently keeps
    every position to its full term, and defaulting it to "exit_now"
    would mean a broken model liquidates the book. Neither is acceptable,
    so an unreadable answer must raise and be recorded as unread.
    """
    if not isinstance(tool_input, dict):
        raise ValueError(f"tool input is not an object: {type(tool_input)}")
    action = tool_input.get("action")
    if action not in ACTIONS:
        raise ValueError(f"action not one of {ACTIONS}: {action!r}")
    triggered = tool_input.get("invalidation_triggered")
    if not isinstance(triggered, bool):
        raise ValueError(
            f"invalidation_triggered must be boolean: {triggered!r}")
    reasoning = tool_input.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("reasoning is required and must be non-empty")
    changed = tool_input.get("what_changed") or []
    if not isinstance(changed, list):
        raise ValueError(f"what_changed must be a list: {changed!r}")
    return PositionReview(
        position_id=position_id, ticker=ticker, action=action,
        invalidation_triggered=triggered, reasoning=reasoning.strip(),
        what_changed=tuple(str(c) for c in changed),
        reviewed_at=datetime.now(timezone.utc),
    )


def should_review(position: dict, as_of: date) -> tuple[bool, str]:
    """(review?, why not). Saves a paid call that cannot change anything.

    A position opened today has no new information to find. One expiring
    tomorrow closes on its own before an early exit would settle. Neither
    is worth $0.03, and at a $5/month cap that arithmetic matters.
    """
    opened = position.get("opened_at_date")
    if isinstance(opened, datetime):
        opened = opened.date()
    if isinstance(opened, date):
        age = (as_of - opened).days
        if age < MIN_AGE_DAYS_BEFORE_REVIEW:
            return False, f"opened {age} day(s) ago; nothing new to find yet"
    exit_date = position.get("planned_exit_date")
    if isinstance(exit_date, datetime):
        exit_date = exit_date.date()
    if isinstance(exit_date, date):
        left = (exit_date - as_of).days
        if left <= MIN_DAYS_REMAINING_TO_BOTHER:
            return False, (
                f"closes in {left} day(s) anyway; an early exit would not "
                "settle any sooner")
    return True, ""


def apply_review(review: PositionReview, position: dict,
                 as_of: date) -> tuple[date, str]:
    """(exit_date, why) after the review. THE ASYMMETRY LIVES HERE.

    This is the function that makes the whole feature safe, so it is
    deliberately tiny and does the decision itself rather than trusting
    any caller to honour a convention:

      exit_now  -> today. The position closes on the next pass.
      anything  -> the ORIGINAL date, unchanged. There is no branch that
      else         can return a later date, and no argument that can
                   make one, so no amount of model confidence can extend
                   a hold.
    """
    original = position.get("planned_exit_date")
    if isinstance(original, datetime):
        original = original.date()
    if review.wants_early_exit:
        # min() rather than plain `as_of`: a review arriving after the
        # exit date has already passed must not push it outward.
        return min(as_of, original) if original else as_of, (
            f"exit brought forward to {as_of} by review: "
            f"{review.reasoning[:200]}")
    if review.action == "hold":
        return original, (
            "reviewed and the thesis was judged intact - the exit date is "
            "unchanged, because a review can only ever bring it forward")
    return original, (
        "reviewed with no opinion reached - the exit date is unchanged")


def render_prompt(position: dict, view: dict, market: dict) -> str:
    """What the model sees. Facts only; it is never told the P&L in a
    way that invites loss aversion, but it IS told the price move,
    because a thesis that predicted a move which did not happen is
    evidence about the thesis."""
    lines = [
        "REVIEWING AN OPEN POSITION. Your job is to say whether the "
        "original thesis still holds, and nothing else.",
        "",
        f"TICKER: {position.get('ticker', '?')}",
        f"OPENED: {position.get('opened_at_date', '?')}  "
        f"CLOSES: {position.get('planned_exit_date', '?')} (fixed)",
        "",
        "THE THESIS WRITTEN AT ENTRY",
        str(view.get("thesis") or "(none recorded)"),
        "",
        "WHAT WAS SAID WOULD INVALIDATE IT",
        str(view.get("invalidation") or "(none recorded)"),
        "",
        "PRICE SINCE ENTRY",
        f"entry {market.get('entry_price', '?')}, "
        f"now {market.get('last_price', '?')} "
        f"({market.get('move_pct', '?')}%)",
        "",
        "ANSWER",
        "1. Has that invalidation condition actually occurred? Answer on "
        "the facts, not on whether the position is up or down.",
        "2. hold, exit_now, or no_opinion.",
        "",
        "GROUND RULES",
        "- The exit date above is FIXED. You cannot extend it, and "
        "'hold' does not buy more time - it only means there is no "
        "reason to leave early. Do not argue for more time; there is no "
        "mechanism to grant it.",
        "- A position being down is not by itself a reason to exit, and "
        "being up is not by itself a reason to stay. The question is "
        "whether the REASON for the trade is still true.",
        "- no_opinion is a good answer when you cannot find out. A guess "
        "here closes a position on nothing.",
        "- Report judgements only: no sizes, no prices, no orders.",
        "- Search only if a result could change your answer. Each search "
        "costs real money against a small monthly budget.",
    ]
    return "\n".join(lines)


def record_review(conn, review: PositionReview, *, prompt: str = "",
                  raw_response=None, model: str = "") -> str:
    """Persist it. Every review is recorded even when it changed
    nothing, because "we asked and the model said hold" is exactly the
    evidence the dashboard needs to narrate a trade afterwards - and a
    review that only got recorded when it acted would make the model
    look decisive in hindsight."""
    row_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO position_reviews
           (id, position_id, ticker, action, invalidation_triggered,
            reasoning, what_changed_json, prompt_rendered,
            raw_response_json, model, cost_cents, skipped_reason,
            reviewed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (row_id, review.position_id, review.ticker, review.action,
         int(review.invalidation_triggered), review.reasoning,
         json.dumps(list(review.what_changed)), prompt,
         json.dumps(raw_response) if raw_response is not None else None,
         model, str(review.cost_cents), review.skipped_reason,
         (review.reviewed_at or datetime.now(timezone.utc)).isoformat()))
    conn.commit()
    return row_id
