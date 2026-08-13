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


#: How often ONE position may be reviewed. This is the cost bound, and
#: it is the piece whose absence kept this module unwired: the cycle runs
#: every 15 minutes in market hours, so without a cadence gate five open
#: positions would be reviewed 26 times a day each - ~130 paid calls a
#: day against a monthly cap measured in single-digit dollars.
#:
#: THE ARITHMETIC. A review is a small prompt plus at most
#: REVIEW_SEARCHES searches, so ~8c at today's rates. Five positions
#: reviewed daily over 21 trading days is ~$8.40/month; every other day
#: is ~$4.20. Daily is the default because the owner asked for this
#: precisely so a changed news picture is noticed - "incase the news
#: changes" - and a check that runs weekly cannot do that.
REVIEW_INTERVAL_HOURS = 24
#: A review asks a narrow question about a named company, so more
#: searching does not sharpen it the way it does for a conjunction.
REVIEW_SEARCHES = 2
REVIEW_MODEL = "claude-sonnet-5"


def last_reviewed_at(conn, position_id: str):
    """When this position was last reviewed, or None. Counts SKIPPED
    reviews too: a skip that did not record a time would be retried on
    the very next cycle, which is the loop this bound exists to stop."""
    row = conn.execute(
        "SELECT MAX(reviewed_at) FROM position_reviews WHERE position_id = ?",
        (position_id,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        parsed = datetime.fromisoformat(str(row[0]))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def due_for_review(conn, positions, now: datetime,
                   interval_hours: int = REVIEW_INTERVAL_HOURS):
    """(to_review, [(position, why_not)]).

    Every gate that declines a review names itself, because a position
    that is silently never reviewed looks exactly like one that is
    reviewed and always held.
    """
    to_review: list = []
    skipped: list = []
    for position in positions:
        ok, why = should_review(position, now.date())
        if not ok:
            skipped.append((position, why))
            continue
        last = last_reviewed_at(conn, position.get("id"))
        if last is not None:
            hours = (now - last).total_seconds() / 3600.0
            if hours < interval_hours:
                skipped.append((position, (
                    f"reviewed {hours:.1f}h ago; the interval is "
                    f"{interval_hours}h")))
                continue
        to_review.append(position)
    return to_review, skipped


def _review_turn_payload(prompt: str, searches: int, messages=None,
                         forced: bool = False) -> dict:
    from catalyst.research.boundary import MAX_EXPLORATION_TOKENS

    tools: list = [POSITION_REVIEW_TOOL]
    if not forced and searches > 0:
        tools = [{"type": "web_search_20250305", "name": "web_search",
                  "max_uses": int(searches)}] + tools
    payload = {
        "model": REVIEW_MODEL, "max_tokens": MAX_EXPLORATION_TOKENS,
        "messages": messages or [{"role": "user", "content": prompt}],
        "tools": tools,
    }
    payload["tool_choice"] = ({"type": "tool",
                               "name": "submit_position_review"} if forced
                              else {"type": "auto"})
    return payload


def _tool_input(response: dict):
    """The single submit_position_review input, or None.

    TWO blocks are ambiguous, not first-wins - the same rule the research
    boundary applies. A model that answered twice did not answer once,
    and picking one of two contradictory reviews could close a position
    on the answer the model discarded.
    """
    content = (response or {}).get("content")
    if not isinstance(content, list):
        return None
    found = [b.get("input") for b in content
             if isinstance(b, dict) and b.get("type") == "tool_use"
             and b.get("name") == "submit_position_review"]
    if len(found) != 1:
        return None
    return found[0]


def review_position(conn, position: dict, view: dict, market: dict,
                    transport, cost_context, *,
                    now: datetime | None = None) -> PositionReview:
    """One governed review of one open position.

    Same discipline order as the research boundary, for the same reason:
    validate the payload before spending, authorize, call, RECORD THE RAW
    USAGE VERBATIM, then price. A turn that cannot be priced still lands
    in the ledger and blocks further spend.

    Never raises. Every failure comes back as a PositionReview carrying a
    skipped_reason, because an exception here would abandon the rest of
    the book mid-sweep - and the positions not yet reviewed are the ones
    with no protection at all.
    """
    from catalyst.cost import CostEstimate
    from catalyst.cost.governor import authorize
    from catalyst.cost.pricing import UnknownModelError
    from catalyst.cost.tracker import (
        UNPARSEABLE_USAGE_KEY,
        UnrecognizedUsageFieldError,
        record_usage,
    )
    from catalyst.research.boundary import (
        exploration_turn_estimate_cents,
        extraction_turn_estimate_cents,
        invalid_payload_reason,
    )

    now = now or datetime.now(timezone.utc)
    position_id = str(position.get("id") or "")
    ticker = str(position.get("ticker") or "")
    prompt = render_prompt(position, view, market)
    call_id = str(uuid.uuid4())
    cost_cents = Decimal("0")

    def skip(reason: str) -> PositionReview:
        review = PositionReview(
            position_id=position_id, ticker=ticker, action="no_opinion",
            invalidation_triggered=False,
            reasoning=f"no review was obtained: {reason}",
            reviewed_at=now, cost_cents=cost_cents, skipped_reason=reason)
        record_review(conn, review, prompt=prompt, model=REVIEW_MODEL)
        return review

    def run(payload: dict):
        nonlocal cost_cents
        bad = invalid_payload_reason(payload)
        if bad is not None:
            return None, f"invalid_request_not_sent: {bad}"
        forced = (payload.get("tool_choice") or {}).get("type") == "tool"
        try:
            cents = (extraction_turn_estimate_cents(REVIEW_SEARCHES,
                                                    model=REVIEW_MODEL)
                     if forced else
                     exploration_turn_estimate_cents(REVIEW_SEARCHES,
                                                     model=REVIEW_MODEL))
        except UnknownModelError:
            cents = Decimal("60")
        decision = authorize(
            CostEstimate(estimated_cents=cents,
                         basis="pre-registered per-turn pessimistic "
                               "estimate (position_review.py)",
                         kind=cost_context.kind, component="position_review"),
            conn, cost_context.governor_profit_share,
            cycle_id=cost_context.cycle_id,
            owner_monthly_cap_cents=cost_context.owner_monthly_cap_cents)
        if not decision.authorized:
            return None, f"budget_denied: {decision.reason}"
        try:
            response = transport(payload)
        except Exception as exc:  # noqa: BLE001 - one position, not the book
            return None, f"transport_error: {type(exc).__name__}: {exc}"
        if not isinstance(response, dict):
            response = {"unparseable_response": repr(response)[:2000]}
        raw_usage = response["usage"] if "usage" in response else {
            UNPARSEABLE_USAGE_KEY: "response carried no usage object"}
        try:
            event = record_usage(raw_usage, REVIEW_MODEL, cost_context.kind,
                                 "position_review", conn, api_call_id=call_id)
            if event.priced_cents is not None:
                cost_cents += event.priced_cents
        except (UnknownModelError, UnrecognizedUsageFieldError) as exc:
            return None, f"usage_unpriced_governor_blocked: {exc}"
        return response, None

    response, error = run(_review_turn_payload(prompt, REVIEW_SEARCHES))
    if error is not None:
        return skip(error)

    # The review tool is offered during exploration, so an answer given
    # with the search results in hand skips the forced turn entirely -
    # the same saving the research path takes, and it is the common case.
    early = _tool_input(response)
    if early is not None:
        try:
            review = make_review_from_tool_input(position_id, ticker, early)
            review = _with_cost(review, cost_cents)
            record_review(conn, review, prompt=prompt,
                          raw_response=response, model=REVIEW_MODEL)
            return review
        except (KeyError, TypeError, ValueError):
            pass          # fall through to the forced turn

    messages = [{"role": "user", "content": prompt}]
    content = (response or {}).get("content")
    if isinstance(content, list) and content:
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": (
            "Submit your review now via submit_position_review.")})
    response, error = run(_review_turn_payload(
        prompt, REVIEW_SEARCHES, messages=messages, forced=True))
    if error is not None:
        return skip(error)
    forced_input = _tool_input(response)
    if forced_input is None:
        return skip("no_single_tool_call_in_forced_review_turn")
    try:
        review = make_review_from_tool_input(position_id, ticker, forced_input)
    except (KeyError, TypeError, ValueError) as exc:
        # A malformed review is a SKIP, never a default - defaulting to
        # hold would let a broken model keep every position to term, and
        # defaulting to exit_now would liquidate the book.
        return skip(f"invalid_review: {exc}")
    review = _with_cost(review, cost_cents)
    record_review(conn, review, prompt=prompt, raw_response=response,
                  model=REVIEW_MODEL)
    return review


def _with_cost(review: PositionReview, cents: Decimal) -> PositionReview:
    return PositionReview(
        position_id=review.position_id, ticker=review.ticker,
        action=review.action,
        invalidation_triggered=review.invalidation_triggered,
        reasoning=review.reasoning, what_changed=review.what_changed,
        reviewed_at=review.reviewed_at, cost_cents=cents,
        skipped_reason=review.skipped_reason)


def bring_exit_forward(conn, position: dict, review: PositionReview,
                       now: datetime) -> tuple[bool, str]:
    """Persist what apply_review decided. (moved?, why).

    THE ONLY WRITER of planned_exit_date after entry, and it re-checks
    the asymmetry at the point of writing rather than trusting the value
    handed to it. apply_review already guarantees a date that never moves
    outward; this refuses to write one that does anyway. Two independent
    checks on the rule that keeps "days to weeks" from becoming "until it
    comes back" is the right number for a rule with no safe failure mode.
    """
    original = position.get("planned_exit_date")
    if isinstance(original, datetime):
        original = original.date()
    new_date, why = apply_review(review, position, now.date())
    if new_date is None or original is None:
        return False, why
    if new_date >= original:
        return False, why
    conn.execute(
        "UPDATE positions SET planned_exit_date = ? WHERE id = ?",
        (new_date.isoformat(), position.get("id")))
    conn.commit()
    return True, why


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
