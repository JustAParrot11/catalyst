"""Ask Claude whether an open position's thesis still holds.

MONEY-CRITICAL - this can close a position.

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
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
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
            # WHEN TO LOOK AGAIN - the model's call, bounded by code.
            #
            # OWNER-ASKED 2026-09-11: "i want claude if it does trade to
            # suggest when is best to check back in e.g. 3 days it
            # checks in makes whatever decision but if it holds then it
            # sets another date to check back in".
            #
            # This is NOT an exit decision and cannot become one. The
            # exit date is fixed at entry and this field cannot move it;
            # every bound - the stop resting at the broker, the hard
            # exit date, the kill switches - is unchanged by whatever
            # number arrives here. What it changes is when the next PAID
            # REVIEW happens, so the money goes where something is
            # actually due rather than on a flat clock.
            "next_check_in_days": {
                "type": "integer", "minimum": 1, "maximum": 30,
                "description": (
                    "When should this thesis be re-read? Answer with the "
                    "number of days from today. Judge it on WHEN THE NEXT "
                    "THING HAPPENS: a readout or a filing due on Tuesday "
                    "means 1 or 2, a slow re-rating with nothing scheduled "
                    "means 5 or 7. Every review costs money out of a fixed "
                    "monthly budget, so asking to be woken daily when "
                    "nothing is due spends the budget that would find the "
                    "next trade - and asking for a fortnight when a "
                    "catalyst lands on Thursday misses it. Code clamps "
                    "this to the position's exit date and to a ceiling, "
                    "and news about this company brings the review forward "
                    "whatever you answer."),
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
    #: Days the model asked to wait before the next review, or None if
    #: it did not say. Advisory: `next_check_at` applies the bounds.
    next_check_in_days: int | None = None

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
    # OPTIONAL, AND A BAD VALUE IS DROPPED RATHER THAN RAISING. The
    # action, the invalidation and the reasoning are the answer; this is
    # a scheduling preference. A review that is otherwise sound must not
    # be discarded - which would leave the position unread - because the
    # model returned a float or a negative number here. Dropped means
    # "it did not say", which falls back to the standing clock.
    asked = tool_input.get("next_check_in_days")
    if isinstance(asked, bool) or not isinstance(asked, int) or asked < 1:
        asked = None
    return PositionReview(
        position_id=position_id, ticker=ticker, action=action,
        invalidation_triggered=triggered, reasoning=reasoning.strip(),
        what_changed=tuple(str(c) for c in changed),
        reviewed_at=datetime.now(timezone.utc),
        next_check_in_days=asked,
    )


#: However long the model asks for, a held position is re-read at least
#: this often. A missed review cannot cost money beyond the stop - the
#: stop rests at the broker and the hard exit date stands, and a review
#: can only ever bring an exit FORWARD - so the cost of waiting is a
#: forgone early exit, not a larger loss. Seven days bounds that without
#: paying for six answers of "nothing has changed".
MAX_CHECK_IN_DAYS = 7


def next_check_at(review, position: dict, now: datetime):
    """(the datetime of the next review, what clamped it) or (None, "").

    THE MODEL PROPOSES, CODE DISPOSES, applied to a date. The number
    that arrives is a request; this decides what is honoured:

      - nothing asked            -> None, and the standing clock applies
      - past the hard exit date  -> clamped to the exit date, because a
                                    review after the position closes is
                                    a paid call about nothing
      - beyond MAX_CHECK_IN_DAYS -> clamped to the ceiling
      - an exit_now review       -> None; the position is leaving, and
                                    scheduling its next read would be
                                    an answer to a question nobody asked

    Both the request and the honoured date are recorded by the caller,
    so "it asked for 30 and got 7" is readable afterwards.
    """
    asked = getattr(review, "next_check_in_days", None)
    if not asked or getattr(review, "wants_early_exit", False):
        return None, ""
    days = int(asked)
    clamped = ""
    if days > MAX_CHECK_IN_DAYS:
        days, clamped = MAX_CHECK_IN_DAYS, (
            f"asked for {asked}d, capped at {MAX_CHECK_IN_DAYS}d")
    when = now + timedelta(days=days)
    exit_date = position.get("planned_exit_date")
    if exit_date:
        try:
            if isinstance(exit_date, str):
                exit_date = date.fromisoformat(exit_date[:10])
            deadline = datetime.combine(
                exit_date, time(0, 0), tzinfo=timezone.utc)
            if when > deadline:
                return deadline, (
                    f"asked for {asked}d, clamped to the exit date "
                    f"{exit_date}")
        except (TypeError, ValueError):
            pass
    return when, clamped


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


def _days_between(a, b) -> int | None:
    """(b - a) in whole days, or None if either is not a readable date.

    Both come from the database as text, so this parses rather than
    trusts - an unreadable date must produce no sentence at all, never a
    confident wrong number of days.
    """
    try:
        start = date.fromisoformat(str(a)[:10])
        end = date.fromisoformat(str(b)[:10])
    except (TypeError, ValueError):
        return None
    return (end - start).days


def _evidence_lines(ev, ticker: str, market_is_live) -> list:
    """The evidence section of the review prompt, or a stated absence.

    ABSENCE IS NOT EVIDENCE, and this is where that matters most. EDGAR
    publishes nothing at a weekend or a holiday, so "no new filings"
    means two completely different things depending on whether the
    market has been open - and a review told "nothing has been filed"
    on a Sunday would read it as the company being quiet. The market
    state is passed in rather than computed from `weekday()` (house
    rule 7: a holiday is the case nobody thinks of).

    `market_is_live` is THREE-VALUED on purpose: True, False, or None
    for "nobody looked". None must not read as closed, the same
    asymmetry section 22 needed.
    """
    out = ["", "WHAT HAS BEEN FILED OR REPORTED SINCE THIS WAS OPENED"]
    if ev.truncated:
        # FIRST, because it changes how everything below reads - and it
        # matters MOST when nothing was found, since that is the reading
        # that would be wrong.
        out.append("INCOMPLETE: this check reached its row limit, so what "
                   "follows is the most recent part of the record and not "
                   "all of it. Treat an absence below as unknown.")
    if not ev.anything:
        if market_is_live is False:
            out.append(
                f"Nothing new naming {ticker} is on file. THE MARKET IS "
                "SHUT, and EDGAR publishes no filings while it is - so "
                "this is an absence of OPPORTUNITY to file, not evidence "
                "that nothing is happening.")
        elif market_is_live is None:
            out.append(
                f"Nothing new naming {ticker} is on file, and whether the "
                "market has been open was not checked - so this absence "
                "carries no information either way.")
        else:
            out.append(
                f"Nothing new naming {ticker} has been filed or reported "
                "since. The feeds have been reading normally, so this is "
                "a real quiet spell rather than a gap in the data.")
        return out
    # SALES FIRST. On a thesis built from insiders buying, an insider
    # selling is the most direct contradiction available, and burying it
    # under a list of option exercises would be a presentation choice
    # with money attached.
    if ev.sales:
        out.append("Insiders DISPOSED of stock:")
        out += [f"  - {line}" for line in ev.sales]
    if ev.purchases:
        out.append("Insiders bought more on the open market:")
        out += [f"  - {line}" for line in ev.purchases]
    if ev.other_insider:
        out.append("Other insider transactions (option exercises, grants "
                   "and similar are compensation mechanics more often than "
                   "a view, so weigh them accordingly):")
        out += [f"  - {line}" for line in ev.other_insider]
    if ev.filings:
        out.append("Filings naming the company:")
        out += [f"  - {line}" for line in ev.filings]
    if ev.news:
        out.append("Headlines naming the company:")
        out += [f"  - {line}" for line in ev.news]
    if ev.omitted:
        out.append(f"({ev.omitted} further item(s) exist and are not listed "
                   "here - there is more than this section can carry.)")
    out.append("This is what the feeds hold; it is not a verdict, and "
               "none of it is checked against the invalidation condition "
               "for you.")
    return out


def render_prompt(position: dict, view: dict, market: dict,
                  now: datetime | None = None, evidence=None,
                  market_is_live=None) -> str:
    """What the model sees. Facts only; it is never told the P&L in a
    way that invites loss aversion, but it IS told the price move,
    because a thesis that predicted a move which did not happen is
    evidence about the thesis.

    `now` is rendered into the prompt. IT WAS NOT, AND THIS PROMPT ASKS
    TWO QUESTIONS THAT NEED IT: it prints a fixed exit date without
    saying how far away it is, and it asks for `next_check_in_days` as a
    "number of days from today" while never naming today. A model that
    does not know the date cannot tell a position with six days left
    from one with one day left, and those want different answers.
    Owner-asked 2026-09-13. Defaults to the real clock because no date
    is the defect.
    """
    now = now or datetime.now(timezone.utc)
    opened = position.get('opened_at_date', '?')
    closes = position.get('planned_exit_date', '?')
    held = _days_between(opened, now.date().isoformat())
    left = _days_between(now.date().isoformat(), closes)
    lines = [
        "REVIEWING AN OPEN POSITION. Your job is to say whether the "
        "original thesis still holds, and nothing else.",
        "",
        f"RIGHT NOW: today is {now.date().isoformat()}, a "
        f"{now.strftime('%A')}, {now.strftime('%H:%M')} UTC.",
        f"TICKER: {position.get('ticker', '?')}",
        f"OPENED: {opened}  CLOSES: {closes} (fixed)",
        (("Held " + (f"{held} day(s)" if held is not None else "an "
                     "unrecorded number of days"))
         + ("; " + (f"{left} day(s) remain before that exit date."
                    if left is not None and left > 0 else
                    "the exit date is TODAY." if left == 0 else
                    f"the exit date passed {-left} day(s) ago."
                    if left is not None else
                    "the days remaining could not be computed."))),
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
    ]
    # THE EVIDENCE, BETWEEN THE INVALIDATION AND THE QUESTION. It is
    # placed here deliberately: the model has just read what would prove
    # the thesis wrong, and reads what has since happened before being
    # asked whether it did. A caller that passes nothing gets no section
    # at all rather than an empty heading.
    if evidence is not None:
        lines += _evidence_lines(evidence, str(position.get("ticker", "?")),
                                 market_is_live)
    lines += [
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

#: However much news breaks, a position is not re-read more often than
#: this. Without it a company in the headlines all day is reviewed every
#: cycle - 96 times - which is how a rule meant to be responsive becomes
#: the largest line on the bill.
MIN_REVIEW_GAP_HOURS = 4
#: A review asks a narrow question about a named company, so more
#: searching does not sharpen it the way it does for a conjunction.
REVIEW_SEARCHES = 2

#: THE FALLBACK, NOT THE CHOICE. The model actually used is the one the
#: owner selected for research, passed in per call; this is only what
#: answers when nothing was passed.
#:
#: WHY IT FOLLOWS THE RESEARCH SELECTION (2026-09-12). This was a
#: standalone constant, and that quietly broke the thing the owner asked
#: for - *"i want nothing manual"*. Switching research to another model
#: while reviews stayed here would bill TWO models on the same day, and
#: `measured_rates._sole_model` refuses to learn a rate from a
#: two-model day because the ratio would be a blend. So the new model's
#: cold-start estimate - deliberately set high - would never be
#: corrected by the bill, and the bot would throttle itself against a
#: guess indefinitely. One selection keeps the whole cost chain closed.
#:
#: The reviews are also the same kind of judgement as the research: if a
#: model is trusted to decide whether to open a position it should be
#: the one deciding whether to keep it.
DEFAULT_REVIEW_MODEL = "claude-sonnet-5"
REVIEW_MODEL = DEFAULT_REVIEW_MODEL   # historical name, same value


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


def news_since(conn, ticker: str, since: datetime) -> tuple[int, str]:
    """Stored news naming this company since `since`, and the newest
    headline. Pure database - no broker call and no model call, so
    asking is free.

    THE POINT OF ASKING. A 24-hour clock spends the same money whether
    the world moved or not: five quiet positions cost exactly as much
    to re-read as five where the thesis just broke. News is the cheapest
    available signal that something changed, and it is already being
    stored for discovery.
    """
    if not ticker:
        return 0, ""
    try:
        rows = conn.execute(
            "SELECT payload_raw FROM raw_events "
            "WHERE source = 'alpaca_news' AND fetched_at > ? "
            "ORDER BY fetched_at DESC LIMIT 200",
            (since.isoformat(),)).fetchall()
    except sqlite3.Error:
        return 0, ""
    hits, newest = 0, ""
    want = str(ticker).strip().upper()
    for (payload,) in rows:
        try:
            parsed = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, dict):
            continue
        if str(parsed.get("ticker") or "").strip().upper() != want:
            continue
        hits += 1
        if not newest:
            newest = str(parsed.get("headline") or "")[:120]
    return hits, newest


#: How many items of each kind reach the prompt.
#:
#: A BOUND, not a preference. The review prompt is ~600 tokens; a busy
#: name can file a dozen Form 4s in a fortnight, and an unbounded list
#: would push the thesis out of the model's attention while multiplying
#: the cost of a call that exists to be cheap. Six per kind keeps the
#: new section comparable in size to the thesis it is being checked
#: against, and anything dropped is COUNTED rather than silently lost.
MAX_EVIDENCE_PER_KIND = 6

#: Longest any single upstream field may be in a rendered line.
#: Every value in a Form 4 payload comes from a filer, so a name, a role
#: or a security title is data this project does not control.
MAX_FIELD_CHARS = 80

#: How many `raw_events` rows one evidence check reads.
#:
#: THE FEED SWEEPS ~562 FORM 4s A DAY, so a position held three weeks
#: sits behind more rows than it is worth scanning on every review. The
#: order is `fetched_at DESC`, so a truncation drops the OLDEST - the
#: right direction, because the newest filings are the ones a thesis has
#: not already been judged against. And when it truncates it SAYS SO:
#: an incomplete answer that reads as a complete one is exactly how a
#: review would conclude "nothing has happened" while a disposal sat one
#: row past the limit.
EVIDENCE_SCAN_ROWS = 20000

#: Transaction codes worth naming in words. Everything else is passed
#: through AS THE CODE rather than guessed at - house rule 7 in the
#: direction that matters here, because mislabelling a transaction is
#: worse than printing a letter the model can ask about. `P` and `S` are
#: the two that bear on an insider-cluster thesis: one is more of the
#: same, the other contradicts it.
TRANSACTION_WORDS = {"P": "open-market purchase", "S": "sale"}


@dataclass(frozen=True)
class Evidence:
    """What has arrived about this company since a moment.

    OWNER-ASKED 2026-09-14: *"what sort of extra checks will it do next,
    its still not clear, will it check what the CEO does next or will it
    see if a partner of them did for example"*.

    THE ANSWER WAS "NEITHER", AND THAT WAS A REAL GAP. `render_prompt`
    carried the thesis, the invalidation condition, the price move and
    the dates - and no new evidence of any kind. So a review was asked
    "has the invalidation occurred?" with nothing to check it against
    except the price.

    Worse, `news_since` was already being computed to decide WHEN to
    review, and never shown TO the review: the trigger knew and the
    prompt did not. Tenth instance of this project's recurring defect.

    PURE DATABASE. No broker call, no model call, no filing fetch -
    every row here is already stored by feeds that run anyway, so
    asking costs nothing and cannot fail a cycle.
    """

    #: Insider transactions that DISPOSED of stock. First, and separate,
    #: because a thesis built on insiders buying is contradicted by an
    #: insider selling in a way it is not by anything else here.
    sales: tuple = ()
    #: Insider transactions that acquired stock.
    purchases: tuple = ()
    #: Other insider transactions - option exercises, grants, gifts.
    #: Kept apart because they are mostly compensation mechanics rather
    #: than a view, and lumping them in with a purchase would overstate
    #: the signal.
    other_insider: tuple = ()
    filings: tuple = ()
    news: tuple = ()
    #: How many rows were found beyond what is shown, per kind.
    omitted: int = 0
    #: True when the scan hit its row limit, so this answer may be
    #: INCOMPLETE. House rule 3 in the direction that matters: a review
    #: must never read a truncated scan as "nothing happened".
    truncated: bool = False

    @property
    def anything(self) -> bool:
        return bool(self.sales or self.purchases or self.other_insider
                    or self.filings or self.news)


def _describe_transaction(owner_name: str, role: str, tx: dict) -> str:
    """One insider transaction, in a sentence a person can read."""
    code = str(tx.get("code") or "").strip().upper()
    words = TRANSACTION_WORDS.get(code) or f"transaction code {code or '?'}"
    shares = str(tx.get("shares") or "").strip()
    value = str(tx.get("value_usd") or "").strip()
    price = str(tx.get("price_per_share") or "").strip()
    when = str(tx.get("transaction_date") or "").strip()
    # BOUNDED, because every field here is UPSTREAM DATA. A filing with
    # a 10KB owner name would otherwise multiply the cost of every
    # review of that position for as long as it is held. Found by the
    # adversarial read, not by a test.
    bits = [(owner_name or "an insider")[:MAX_FIELD_CHARS]]
    if role:
        bits.append(f"({role[:MAX_FIELD_CHARS]})")
    bits.append(words)
    if shares:
        bits.append(f"of {shares} shares")
    if price:
        bits.append(f"at ${price}")
    try:
        if value:
            bits.append(f"= ${float(value):,.0f}")
    except (TypeError, ValueError):
        pass
    if when:
        bits.append(f"on {when}")
    return " ".join(bits)


def evidence_since(conn, ticker: str, since) -> Evidence:
    """Filings, insider transactions and news naming this company since
    `since`. Never raises: a database missing a table returns nothing
    found, because a review that cannot read the feed must still run.

    WHY A SALE IS SEPARATED FROM A PURCHASE. Every order this bot has
    ever placed came from insiders BUYING. The Form 4 feed sweeps the
    whole daily index - every filing, every transaction code - so a
    later sale by the same officer is already on disk; only the cluster
    adapter filters to code `P`. Presenting the two in one list would
    bury the single most direct contradiction of the thesis in with its
    confirmation.

    NOTHING HERE TELLS THE MODEL WHAT TO CONCLUDE. It states what was
    filed. The prompt already asks whether the invalidation condition
    has occurred, and that question is the model's to answer.
    """
    want = str(ticker or "").strip().upper()
    if not want:
        return Evidence()
    cutoff = since.isoformat() if hasattr(since, "isoformat") else str(since)
    sales: list = []
    purchases: list = []
    other: list = []
    filings: list = []
    news: list = []
    omitted = 0
    try:
        rows = conn.execute(
            "SELECT source, payload_raw FROM raw_events "
            "WHERE fetched_at > ? AND source IN "
            "('edgar_form4','alpaca_news','edgar_fts','edgar_xbrl') "
            "ORDER BY fetched_at DESC LIMIT ?",
            (cutoff, EVIDENCE_SCAN_ROWS)).fetchall()
    except sqlite3.Error:
        return Evidence()

    def _room(bucket: list) -> bool:
        nonlocal omitted
        if len(bucket) < MAX_EVIDENCE_PER_KIND:
            return True
        omitted += 1
        return False

    for row in rows:
        source = str(row[0] or "")
        try:
            payload = json.loads(row[1]) if row[1] else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("ticker") or "").strip().upper() != want:
            continue
        if source == "edgar_form4":
            owners = payload.get("owners") or []
            first = owners[0] if isinstance(owners, list) and owners else {}
            name = str((first or {}).get("name") or "").strip()
            role = str((first or {}).get("relationship")
                       or (first or {}).get("officer_title") or "").strip()
            for tx in (payload.get("transactions") or []):
                if not isinstance(tx, dict):
                    continue
                # DIRECTION FROM `acquired_disposed`, not from the code.
                # The code says what KIND of transaction it was; A/D says
                # which way the stock went, and it is the field the
                # cluster adapter itself trusts.
                way = str(tx.get("acquired_disposed") or "").strip().upper()
                line = _describe_transaction(name, role, tx)
                code = str(tx.get("code") or "").strip().upper()
                if way == "D":
                    if _room(sales):
                        sales.append(line)
                elif way == "A" and code == "P":
                    if _room(purchases):
                        purchases.append(line)
                elif _room(other):
                    other.append(line)
        elif source == "alpaca_news":
            head = str(payload.get("headline") or "").strip()[:140]
            if head and _room(news):
                news.append(head)
        elif source.startswith("edgar_"):
            what = (str(payload.get("form_type") or "").strip()
                    or str(payload.get("match") or "").strip()
                    or "a filing")
            when = str(payload.get("filed_date")
                       or payload.get("filed") or "").strip()
            line = f"{what}{(' filed ' + when) if when else ''}"
            if _room(filings):
                filings.append(line)
    return Evidence(sales=tuple(sales), purchases=tuple(purchases),
                    other_insider=tuple(other), filings=tuple(filings),
                    news=tuple(news), omitted=omitted,
                    truncated=len(rows) >= EVIDENCE_SCAN_ROWS)


def requested_check_at(conn, position_id: str):
    """The datetime Claude asked to be woken for this position, or None.

    The NEWEST request wins: each review supersedes the last, so a
    position told "look in five days" and then, after news brought a
    review forward, "look tomorrow", is looked at tomorrow.

    Never raises - a database that predates the table simply has no
    request, which falls back to the standing clock.
    """
    try:
        row = conn.execute(
            "SELECT next_check_at FROM position_review_checkins "
            "WHERE position_id = ? ORDER BY recorded_at DESC LIMIT 1",
            (str(position_id),)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        when = datetime.fromisoformat(str(row[0]))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def due_for_review(conn, positions, now: datetime,
                   interval_hours: int = REVIEW_INTERVAL_HOURS):
    """(to_review, [(position, why_not)]).

    Every gate that declines a review names itself, because a position
    that is silently never reviewed looks exactly like one that is
    reviewed and always held.

    THE INTERVAL IS A FLOOR ON ATTENTION, NOT A CEILING. A fixed 24-hour
    clock means a thesis can break at ten in the morning and go unread
    until the next day - the owner's point, and a fair one. Raising the
    rate for everything is the expensive answer: five positions reviewed
    every six hours is $18/month against a $25 cap, and it competes
    directly with discovery, which is what finds the next trade.

    So news brings a review FORWARD. If a feed has published something
    naming this company since it was last read, it is read now rather
    than on the clock. That spends the money where something has
    actually changed instead of paying repeatedly to be told nothing
    has, and asking costs nothing - the news is already stored, and the
    check is one indexed query.

    MIN_REVIEW_GAP_HOURS is the guard underneath. A company in the news
    all day would otherwise be re-read every cycle, which is how a
    responsive rule becomes a runaway bill; a busy name is read at most
    that often however much is written about it.
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
            if hours < MIN_REVIEW_GAP_HOURS:
                skipped.append((position, (
                    f"reviewed {hours:.1f}h ago; nothing is re-read inside "
                    f"{MIN_REVIEW_GAP_HOURS}h however much news there is")))
                continue
            # CLAUDE'S OWN DATE, WHERE IT SET ONE, in place of the
            # flat clock. Owner-asked 2026-09-11: "if it holds then it
            # sets another date to check back in".
            #
            # It can push the next read LATER than the standing
            # interval, which is the point - paying six times to be
            # told nothing has changed is the waste this removes - and
            # it is bounded before it ever reaches here: clamped to the
            # exit date and to MAX_CHECK_IN_DAYS when it was recorded.
            # News still overrides it below, and MIN_REVIEW_GAP_HOURS
            # above, so this can only ever move a QUIET position's
            # review later.
            asked_at = requested_check_at(conn, position.get("id"))
            due_at = asked_at if asked_at is not None else (
                last + timedelta(hours=interval_hours))
            if now < due_at:
                hits, headline = news_since(conn, position.get("ticker"), last)
                if not hits:
                    when = ("Claude asked to be woken "
                            f"{due_at.date()}" if asked_at is not None
                            else f"the interval is {interval_hours}h")
                    skipped.append((position, (
                        f"reviewed {hours:.1f}h ago, {when}, and no news "
                        f"has named {position.get('ticker')} since")))
                    continue
                position["review_trigger"] = (
                    f"{hits} news item(s) named {position.get('ticker')} "
                    f"since it was last read {hours:.1f}h ago"
                    + (f': "{headline}"' if headline else ""))
        to_review.append(position)
    return to_review, skipped


def _review_turn_payload(prompt: str, searches: int, messages=None,
                         forced: bool = False,
                         model: str | None = None) -> dict:
    from catalyst.research.boundary import MAX_EXPLORATION_TOKENS

    tools: list = [POSITION_REVIEW_TOOL]
    if not forced and searches > 0:
        tools = [{"type": "web_search_20250305", "name": "web_search",
                  "max_uses": int(searches)}] + tools
    payload = {
        "model": model or DEFAULT_REVIEW_MODEL,
        "max_tokens": MAX_EXPLORATION_TOKENS,
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
                    now: datetime | None = None,
                    model: str | None = None) -> PositionReview:
    """One governed review of one open position.

    `model` is the one the owner selected for research (see
    DEFAULT_REVIEW_MODEL for why they are the same choice). It is used
    for the payload, for the pre-call estimate and for the recorded
    cost row, so all three agree - a call billed as one model and
    estimated as another is how a ledger stops reconciling.

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
    model = model or DEFAULT_REVIEW_MODEL
    position_id = str(position.get("id") or "")
    ticker = str(position.get("ticker") or "")
    # GATHER THE EVIDENCE BEFORE SPENDING. Pure database (no broker
    # call, no fetch), so it cannot fail the call and costs nothing -
    # and it happens here rather than in `render_prompt` so the renderer
    # stays pure and a test can pin it (house rule 6).
    #
    # `opened_at` is the right cutoff, not the last review: a review
    # that saw a filing last week and held is a review that already
    # weighed it, but the model has no memory across calls, so dropping
    # it would hide a fact from the only reader who needs it.
    evidence = evidence_since(conn, ticker,
                              position.get("opened_at")
                              or position.get("opened_at_date") or "")
    prompt = render_prompt(position, view, market, now=now,
                           evidence=evidence,
                           market_is_live=market.get("market_is_live"))
    call_id = str(uuid.uuid4())
    cost_cents = Decimal("0")

    def skip(reason: str) -> PositionReview:
        review = PositionReview(
            position_id=position_id, ticker=ticker, action="no_opinion",
            invalidation_triggered=False,
            reasoning=f"no review was obtained: {reason}",
            reviewed_at=now, cost_cents=cost_cents, skipped_reason=reason)
        record_review(conn, review, prompt=prompt, model=model)
        return review

    def run(payload: dict):
        nonlocal cost_cents
        bad = invalid_payload_reason(payload)
        if bad is not None:
            return None, f"invalid_request_not_sent: {bad}"
        forced = (payload.get("tool_choice") or {}).get("type") == "tool"
        try:
            cents = (extraction_turn_estimate_cents(REVIEW_SEARCHES,
                                                    model=model)
                     if forced else
                     exploration_turn_estimate_cents(REVIEW_SEARCHES,
                                                     model=model))
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
            event = record_usage(raw_usage, model, cost_context.kind,
                                 "position_review", conn, api_call_id=call_id)
            if event.priced_cents is not None:
                cost_cents += event.priced_cents
        except (UnknownModelError, UnrecognizedUsageFieldError) as exc:
            return None, f"usage_unpriced_governor_blocked: {exc}"
        return response, None

    response, error = run(_review_turn_payload(prompt, REVIEW_SEARCHES,
                                               model=model))
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
                          raw_response=response, model=model,
                          position=position)
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
        prompt, REVIEW_SEARCHES, messages=messages, forced=True,
        model=model))
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
                  model=model, position=position)
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
                  raw_response=None, model: str = "",
                  position: dict | None = None) -> str:
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
    # WHEN THE MODEL WANTS TO LOOK AGAIN, and what was honoured.
    #
    # Recorded in a side table (CLAUDE.md) and never raises: a review
    # that cannot store its scheduling preference must still be a
    # recorded review, because the alternative is a paid call whose
    # answer is lost. Both the request and the clamped date go in, so
    # "asked for 30 and got 7" is readable afterwards rather than
    # inferred.
    if review.next_check_in_days and position is not None:
        try:
            when, clamped = next_check_at(
                review, position, review.reviewed_at
                or datetime.now(timezone.utc))
            if when is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO position_review_checkins "
                    "(review_id, position_id, requested_days, "
                    " next_check_at, clamped_by, recorded_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (row_id, review.position_id,
                     int(review.next_check_in_days), when.isoformat(),
                     clamped or None,
                     datetime.now(timezone.utc).isoformat()))
        except Exception:            # noqa: BLE001 - scheduling is not the answer
            pass
    conn.commit()
    return row_id
