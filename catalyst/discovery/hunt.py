"""Let Claude nominate candidates, and validate every one it names.

OWNER-ASKED: "surely to make this properly agentic we want claude go out
and finds its own trades... deterministic isnt an agentically trading
bit surely?"

They were right, and the objection I had been making was to the wrong
thing. Two rules had been treated as one:

  WHO SIZES AND PLACES     must stay deterministic. That is the
                           ruin-prevention line and it does not move.
  WHO CHOOSES WHAT TO LOOK AT   the brief says nothing about this. There
                           is no safety reason it cannot be the model.

So this module gives the second one to Claude, and applies the SAME
discipline to it that the first one already had: the model proposes, and
deterministic code disposes.

WHAT THE MECHANICAL SCREEN WAS THROWING AWAY. The feeds already collect
far more than `build_candidates` turns into candidates - measured on the
owner's own day, 70 EDGAR full-text hits and 48 news items arrived and
almost none became a candidate, because the screen only builds Form 4
clusters and cross-feed conjunctions. The evidence was fetched, stored,
paid for and then ignored. A hunt costs one model call to read what is
already on disk.

NOMINATION IS NOT CREATION, and this is the part that keeps it honest.
Claude cannot invent a ticker, a date or an event. It may only point at
raw_events that already exist, by their real source_id, and every
nomination is checked against them:

  - the source ids must resolve to rows actually in the feed
  - the ticker must appear in those rows' own payloads
  - the catalyst type must be one the risk engine already prices
  - the date must be near-term and not in the past
  - the ticker must pass the same tradeability screen as everything else

A nomination failing any of those is dropped with a reason, and the
reason is counted on the funnel. So the worst a confidently wrong model
can do is waste its own nomination - it cannot conjure a company.

FROM THERE IT IS THE ORDINARY PATH. A hunted candidate is researched,
priced, quote-cross-checked, sized and stopped by exactly the same code
as a screened one. Nothing about the risk engine knows or cares where a
candidate came from.

COST. One call per hunt, bounded input, and the number of hunts a day
derives from the owner's monthly cap the same way every other throttle
now does. Measured at the owner's own $0.192/call, a daily hunt plus the
candidates it produces is about $41/month at a $100 cap.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from catalyst.discovery import Candidate

_log = logging.getLogger("catalyst.hunt")

#: The catalyst types the risk engine already carries a gap and stop
#: for. Read from the shapes table itself rather than copied, so a type
#: added there is nominable here without anyone remembering this file.
#: A nomination outside the set has no sizing basis and is refused
#: rather than silently given a default.
from catalyst.risk.adaptive_params import _CATALYST_SHAPES  # noqa: E402

CATALYST_TYPES = frozenset(_CATALYST_SHAPES)

#: How far ahead a nominated catalyst may sit. Beyond this the position
#: would breach the hold bound before the event arrives.
MAX_DAYS_AHEAD = 45

#: Raw events offered to one hunt. Enough to see a day's flow, bounded
#: so the input cost cannot run away with the feed volume.
MAX_EVENTS_IN_DIGEST = 220

#: Characters of each event's payload put in front of the model. Long
#: enough to carry a headline and the matched phrase, short enough that
#: 220 of them stay affordable.
DIGEST_CHARS = 320

#: Nominations accepted from one hunt, before validation. The point is a
#: short list of the best, not a re-run of the screen.
MAX_NOMINATIONS = 8

#: A hunt is one model call. This is what it may cost before the
#: governor is asked - deliberately generous against the measured
#: $0.192 research call, because the digest is a larger input.
HUNT_ESTIMATE_CENTS = Decimal("60")


NOMINATE_TOOL = {
    "name": "nominate_candidates",
    "description": (
        "Nominate the tradeable events you found in the feed. Only "
        "events that are ALREADY in the list you were shown - you are "
        "selecting and interpreting, never inventing. Nominate nothing "
        "if nothing in the list is worth a position; an empty list is a "
        "valid and often correct answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "nominations": {
                "type": "array",
                "maxItems": MAX_NOMINATIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {
                            "type": "string",
                            "description": (
                                "The US-listed symbol, exactly as it "
                                "appears in the event you are citing."
                            ),
                        },
                        "catalyst_type": {
                            "type": "string",
                            "enum": sorted(CATALYST_TYPES),
                            "description": (
                                "Which of these the event is. This "
                                "decides how the risk engine sizes the "
                                "position, so choose the one that "
                                "matches the MECHANISM, not the one "
                                "that sounds closest."
                            ),
                        },
                        "catalyst_date": {
                            "type": "string",
                            "description": (
                                "ISO date the event resolves or is "
                                "expected to. Today or later, within "
                                f"{MAX_DAYS_AHEAD} days. If the source "
                                "gives no date, estimate it and say so "
                                "in date_confidence."
                            ),
                        },
                        "date_confidence": {
                            "type": "string",
                            "enum": ["confirmed", "estimated"],
                            "description": (
                                "confirmed only when the source states "
                                "the date. A guess called confirmed is "
                                "worse than an honest estimate."
                            ),
                        },
                        "source_event_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "The source_id values, copied exactly, "
                                "of the events you are relying on. These "
                                "are checked against the feed: a "
                                "nomination citing an id that does not "
                                "exist is discarded, so cite only what "
                                "you were actually shown."
                            ),
                        },
                        "why": {
                            "type": "string",
                            "description": (
                                "Two sentences: what the event is, and "
                                "why it could move this price. This is "
                                "not the trade thesis - a full research "
                                "pass happens afterwards - it is why "
                                "this is worth paying to research."
                            ),
                        },
                    },
                    "required": ["ticker", "catalyst_type", "catalyst_date",
                                 "date_confidence", "source_event_ids",
                                 "why"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["nominations"],
        "additionalProperties": False,
    },
}


@dataclass
class HuntResult:
    """What one hunt produced, and what was thrown away."""

    candidates: list = field(default_factory=list)
    #: {candidate_id: the model's two-sentence reason}. Audit trail
    #: only - no arithmetic reads it, and it is NOT the trade thesis,
    #: which a full research pass writes afterwards.
    rationales: dict = field(default_factory=dict)
    nominations: int = 0
    rejected: list = field(default_factory=list)   # [(ticker, reason)]
    prompt: str = ""
    raw_response: object = None
    cost_cents: Decimal = Decimal("0")
    #: What the hunt DID before nominating: [(tool, inputs, is_error)].
    #: Audit trail for "how did it find this" (BUILD-BRIEF: every trade
    #: reconstructable), and the count the dashboard reads.
    tool_calls: list = field(default_factory=list)
    found: int = 0          # events the tools added to the citable set
    turns: int = 0
    skipped_reason: str | None = None


def hunts_per_day(owner_monthly_cap_cents=None, conn=None) -> int:
    """How many hunts a day the budget supports.

    Derived, like every other throttle, so raising the cap raises what
    the bot does without anyone remembering this constant exists. One a
    day at the owner's $100; none at all on a budget too small to afford
    both hunting and judging, because a nomination nobody can afford to
    research is worse than no nomination.
    """
    from catalyst.cost.governor import BUDGET_MONTH_DAYS

    if owner_monthly_cap_cents is None:
        return 0
    try:
        monthly = Decimal(str(owner_monthly_cap_cents))
        if not monthly.is_finite() or monthly <= 0:
            return 0
        per_day = monthly / BUDGET_MONTH_DAYS
    except (ArithmeticError, TypeError, ValueError):
        return 0
    # A hunt plus the candidates it produces. Below that, spend the
    # budget judging what the mechanical screen already found.
    #
    # 2x, not 3x, since 2026-09-05. The owner's 7-day bundle showed the
    # bot spending $1.33 and $6.69 on its two live days against a $10
    # ceiling, and researching everything the screen produced with
    # slots to spare - so the constraint was candidate SUPPLY, not the
    # budget to judge them. A hunt is the one source that can widen
    # supply, and the budget cap, not this divisor, is what stops it
    # (owner-set: "i dont really need any hard limit except a hard stop
    # to stop bot using all the budget").
    #
    # TRIED AND REJECTED 2026-09-11: halving this to 1x, to take the
    # hunt to four a day.
    #
    # OWNER-ASKED: "i feel we're heavily looking at insider trades not
    # just claude spotting potential... can we get it to do even more
    # agentic research to find very lucrative trades". The feeling is
    # correct and the record is blunt: ALL 23 directional views this
    # system has ever produced came from insider clusters, and the hunt
    # has had TWO paid research calls in its life.
    #
    # SO I HALVED THE DIVISOR, AND A TEST CAUGHT IT. This 2x is not a
    # rate dial - it is the RESERVE FOR RESEARCHING WHAT A HUNT FINDS,
    # which is what the line above it means by "a hunt plus the
    # candidates it produces". At 1x, a $20/month cap returns one hunt a
    # day costing 60c of a 67c daily allowance, leaving nothing to judge
    # the nominations with - the exact failure the docstring warns
    # about. Halving it did not buy more discovery, it bought
    # nominations nobody could afford to research.
    #
    # SO THE HUNT RATE IS NOT THE LEVER, and the honest answer to the
    # owner's question is arithmetic rather than a constant: at $100 the
    # budget supports two hunts a day AND researching what they find;
    # more agentic discovery costs more money, and the cap is the dial.
    # What DID change on the same day is where research slots go - the
    # conjunction arm was taking 48% of paid calls at the highest price
    # each for zero directional views, and the rotation plus its probe
    # share moved that to the arms that convert, the hunt among them.
    #
    # AND THE HUNT IS ALREADY SELF-LIMITING either way: the same rule
    # that demoted conjunctions applies to every arm, so no directional
    # view in 40+ paid calls drops the hunt to a probe share too,
    # recomputed from the record every cycle.
    # MEASURED COST PER HUNT WHERE THERE IS ONE, for the same reason
    # research_per_cycle does it: HUNT_ESTIMATE_CENTS is 60c and the
    # measured cost was 11.6c over 18 calls. Owner-asked 2026-09-11:
    # "we dont want to be changing estimates manually."
    #
    # NOTE THE DIRECTION THIS MOVES. A measured cost BELOW the seed
    # raises the hunt rate, and that is the loosening case - so it is
    # bounded twice: by min(4, ...) here, and by the governor's real
    # daily cap on actual spend. A measured cost ABOVE the seed lowers
    # the rate, which needs no guard at all.
    per_hunt = HUNT_ESTIMATE_CENTS
    if conn is not None:
        from catalyst.cost.observed import observed_call_cents

        measured, _n = observed_call_cents(conn, "hunt", HUNT_ESTIMATE_CENTS)
        if measured > 0:
            per_hunt = measured
    need = per_hunt * 2
    affordable = int(per_day // need)

    # THE HARD-CODED CEILING IS GONE, AND WHAT REPLACED IT IS THE RECORD.
    #
    # OWNER-ASKED 2026-09-12: "well surely the weekend search will be more
    # purely agentic as it isnt influenced by SEC of insider trade info",
    # and separately: "if we have 10 trades in 2 weeks at least 5 are
    # fully claude research from news and trade deals etc".
    #
    # WHAT WAS ACTUALLY LIMITING IT, measured: at the owner's $100 cap
    # this function returned min(4, 333c // 23.2c) = min(4, 14). The
    # BUDGET afforded fourteen and a typed 4 was the limiter - the exact
    # kind of number the 2026-09-11 cost audit was supposed to have
    # removed, and the reason CLAUDE.md's throttle table still said "2 at
    # $100" long after the measured 11.6c had moved it to 4.
    #
    # SO WHY NOT SIMPLY UNCAP IT? Because 14 hunts a day is 162c of a
    # 333c daily allowance - 49% of the budget on an arm with ZERO
    # directional views in two lifetime calls. That is precisely the
    # conjunction mistake (48% of paid calls, 0 views in 89) which this
    # project spent three weeks measuring and then fixed.
    #
    # THE ANSWER IS THE RULE THAT ALREADY EXISTS, applied one stage
    # earlier. `cycle.demoted_arms` drops an arm to a probe share once
    # its own record says it has never produced a directional view on a
    # sample big enough to mean it (ARM_PROBE_MIN_CALLS = 40). That was
    # bounding RESEARCH slots only, so a non-converting hunt kept
    # nominating at full rate while its nominations were rationed - the
    # spend continued and the bound did not reach it.
    #
    # Now it does. So the hunt gets a real run at proving itself - 40
    # paid calls at the measured 11.6c is under $5 for the whole
    # experiment - and if it does not convert, the same measured rule
    # that killed the conjunction allowance throttles it without anybody
    # choosing a number. Evidence buys budget; hope does not.
    # AND A CEILING THAT IS DERIVED, NOT TYPED. Removing the old `4`
    # outright removed the bound with it, and three tests caught that -
    # correctly: a very cheap measured hunt returned 166 a day, and an
    # absurd cap returned 277,777. "Bounded by the record" is not enough
    # on its own, because the demotion only bites an arm that converts
    # NOTHING; one early directional view would restore a full allowance
    # and leave it unbounded.
    #
    # The structural limit is the CADENCE: a hunt runs at most once per
    # cycle, so the day cannot hold more hunts than it holds cycles.
    # That is a real ceiling rather than a guess at diminishing returns,
    # and it moves on its own if the cycle interval ever changes. At the
    # owner's $100 cap the budget binds long before it (14 against 96),
    # so this only ever catches the pathological case the tests describe.
    ceiling = _hunts_the_cadence_allows()
    affordable = min(affordable, ceiling)

    if conn is not None and _hunt_is_a_proven_non_converter(conn):
        # The same 1-in-4 the research rotation uses, and never zero:
        # an arm on a probe share must keep generating the evidence that
        # would restore it.
        from catalyst.orchestrator.cycle import ARM_PROBE_EVERY

        return max(1, affordable // int(ARM_PROBE_EVERY))
    return affordable


def _hunts_the_cadence_allows() -> int:
    """How many hunts a day the cycle interval physically permits.

    A hunt is asked once per cycle at most, so this is the number of
    cycles in a day. Read from the scheduler's own interval - including
    the environment override the owner's VPS could be running - so the
    ceiling cannot disagree with the loop that enforces it.

    Never raises and never returns less than one: a misread interval must
    not silently stop the bot hunting.
    """
    import os

    from catalyst.orchestrator.scheduler import DEFAULT_CYCLE_SECONDS

    try:
        seconds = int(os.environ.get("CATALYST_CYCLE_SECONDS",
                                     DEFAULT_CYCLE_SECONDS))
    except (TypeError, ValueError):
        seconds = int(DEFAULT_CYCLE_SECONDS)
    if seconds <= 0:
        seconds = int(DEFAULT_CYCLE_SECONDS)
    return max(1, 86400 // seconds)


def _hunt_is_a_proven_non_converter(conn) -> bool:
    """Does the hunt's OWN RECORD say it has never produced a directional
    view, on a sample large enough to mean it?

    Reads `cycle.arm_conversion`/`demoted_arms` rather than reimplementing
    the test, so discovery and research cannot end up demoting on
    different rules. Recomputed every cycle: the hunt returns to a full
    allowance on the cycle after it finally produces a view, because this
    is a reading of the record and never a stored flag.

    Never raises. A database that cannot answer demotes nothing, which is
    the direction that keeps the arm alive rather than the one that
    silently starves it.
    """
    try:
        from catalyst.orchestrator.cycle import arm_conversion, demoted_arms

        return "hunt" in demoted_arms(arm_conversion(conn))
    except Exception:  # noqa: BLE001
        return False


def feed_changed_since_last_hunt(conn, *, now=None) -> tuple[bool, str]:
    """(is there anything new to read, why). NEVER RAISES.

    THE REASON A CEILING EXISTED AT ALL, stated as a rule instead of a
    number. This function's older sibling comment said it plainly: "the
    feed does not change materially between 15-minute cycles, so hunting
    every cycle would pay to re-read the same digest ~26 times a day for
    the same nominations." That is a real constraint and it is about the
    INPUT, not about money - so bounding it with a spend-shaped constant
    was answering the wrong question.

    The rule: a hunt is worth paying for when the feed has gained at
    least one event since the last hunt was billed. One new event is a
    genuinely different digest; zero new events is the identical one.

    A day with no hunt on record yet is worth paying for - that is the
    first hunt of the day, not a repeat. And a weekend is where this
    earns its keep in the other direction: EDGAR is shut, so if nothing
    new arrived, nothing is paid for.
    """
    from datetime import datetime, timezone

    try:
        row = conn.execute(
            "SELECT MAX(priced_at) FROM cost_events "
            "WHERE component = 'hunt' AND priced_at IS NOT NULL").fetchone()
    except Exception:  # noqa: BLE001 - an unanswerable ledger is not a veto
        return True, "no hunt history could be read, so this is treated as "\
                     "the first"
    last = (row or [None])[0]
    if not last:
        return True, "no hunt has ever been billed, so there is nothing to "\
                     "have already read"
    try:
        fresh = conn.execute(
            "SELECT COUNT(*) FROM raw_events WHERE fetched_at > ?",
            (str(last),)).fetchone()[0]
    except Exception:  # noqa: BLE001
        return True, "the feed could not be counted, so this is not treated "\
                     "as a repeat"
    if int(fresh or 0) > 0:
        return True, f"{int(fresh)} event(s) arrived since the last hunt at "\
                     f"{str(last)[:16]}"
    return False, (f"no event has arrived since the last hunt at "
                   f"{str(last)[:16]}, so this would pay to re-read the "
                   "identical digest")


def _digest(events: list, as_of: datetime) -> tuple[str, dict]:
    """The feed, compactly, with the real source ids alongside."""
    by_id: dict = {}
    lines: list[str] = []
    recent = sorted(
        events, key=lambda e: str(getattr(e, "fetched_at", "")), reverse=True
    )[:MAX_EVENTS_IN_DIGEST]
    for e in recent:
        sid = str(getattr(e, "source_id", "") or "")
        if not sid:
            continue
        by_id[sid] = e
        payload = getattr(e, "payload_raw", None)
        try:
            text = json.dumps(payload, sort_keys=True) if not isinstance(
                payload, str) else payload
        except (TypeError, ValueError):
            text = str(payload)
        lines.append(f"[{getattr(e, 'source', '?')}] {sid}\n  "
                     + text[:DIGEST_CHARS].replace("\n", " "))
    return "\n".join(lines), by_id


def _tools_section(searchers: dict | None) -> str:
    """What the model may do before it nominates, when it has hands."""
    from catalyst.discovery.hunt_tools import MAX_TOOL_CALLS, tool_schemas

    offered = [t["name"] for t in tool_schemas(searchers)]
    if not offered:
        return ""
    from catalyst.discovery.hunt_tools import HUNT_SEARCHES

    try:
        from catalyst.data.sources.edgar_fts import QUERIES

        fixed = "; ".join(q.phrase for q in QUERIES)
    except Exception:  # noqa: BLE001 - the list is guidance, not a gate
        fixed = "(the fixed query table could not be read)"
    return (
        "YOU HAVE HANDS - USE THEM BEFORE YOU NOMINATE. The feed below is "
        "a skim: 320 characters per item, and the mechanical feed only "
        "ever searches these fixed phrases: " + fixed + ". Everything "
        "that table does not say is yours to look for.\n"
        + ("- search_filings: full-text search of the last 21 days of SEC "
           "filings for a phrase YOU choose. Think about what a tradeable "
           "dated event looks like in a filing and search for THAT - a "
           "special meeting date, a hearing, a decision deadline, a "
           "contract award with a start date, a going-private vote, an "
           "activist demand with a deadline.\n"
           if "search_filings" in offered else "")
        + ("- read_filing: open any item in the feed, or anything you "
           "found, and read the body. The DATE is in the body, not the "
           "snippet. Read before you nominate on a date.\n"
           if "read_filing" in offered else "")
        + ("- search_news: what is being written about a name you are "
           "considering, to check whether an event is already widely "
           "reported.\n"
           if "search_news" in offered else "")
        + f"- web_search, up to {HUNT_SEARCHES} times: the world outside "
        "the filings. Who supplies whom, what a route closing does to a "
        "freight rate, which company a shortage reaches. This is where a "
        "chain starts, and it is the one tool that can tell you something "
        "no screen could have known.\n"
        + f"Up to {MAX_TOOL_CALLS} client tool calls this hunt. Anything a tool "
        "returns is citable by its source_id exactly as if it had been in "
        "the feed. Then submit with nominate_candidates - and an empty "
        "list after a real search is still a good answer."
    )


def closed_market_brief() -> str:
    """The extra brief for a hunt run while the exchange is shut.

    OWNER-ASKED 2026-09-12: *"well surely the weekend search will be more
    purely agentic as it isnt influenced by SEC of insider trade info"* -
    and that is exactly right, structurally. EDGAR does not file at
    weekends or on holidays, so no new Form 4 and no new filing arrives.
    The two mechanical screens have nothing new to match, which means
    whatever a closed-market hunt produces is Claude's own reasoning
    rather than a pattern found in a fresh filing.

    ONE CORRECTION TO THE PREMISE, and it is in the model's favour: the
    hunt still READS the week's stored filings. It is not blind to
    insider data on a Saturday, it just gets no NEW filings - which suits
    a second-order chain better, because the week's filings become
    context to reason outward FROM rather than a pattern to match.

    What this brief does NOT do is hand out more searches. Conjunctions
    were given ten instead of three on the argument that "the answer
    lives in reporting the feeds do not carry", and after 89 paid calls
    at the larger allowance produced zero directional views the
    allowance was cut back. Evidence buys budget; hope does not. So the
    weekend gets a different JOB, at the same price.
    """
    return (
        "THE EXCHANGE IS SHUT RIGHT NOW, AND THAT CHANGES YOUR JOB.\n"
        "No new SEC filing has arrived and none will until it reopens, so "
        "the two mechanical screens have nothing fresh to match and are "
        "not competing with you. Everything you nominate now is your own "
        "reasoning rather than a pattern in a new filing - which is the "
        "half of this system nothing else can do.\n"
        "- The filings below are the WEEK'S, and they are context to "
        "reason outward FROM, not a list to pick from. Ask what a filing "
        "implies about somebody else: a supplier, a customer, a "
        "competitor, a company on the other side of the same shortage.\n"
        "- Start from a cause in the world, not from a ticker. This is "
        "when web_search earns its place: a route closing, a strike, a "
        "tariff, a shortage, a ruling. Name the company and the mechanism "
        "in one line, THEN find the dated event, because a consequence is "
        "not a catalyst.\n"
        "- The date rule is unchanged and it is the one that kills most "
        "nominations: the event must resolve today or later. When the "
        "market reopens is not itself an event - 'the price will react on "
        "Monday' is not a catalyst, it is a market opening.\n"
        "- Nothing is bought while the exchange is shut. What you "
        "nominate is researched now and can only be acted on at the next "
        "open, and code checks first that the price has not already moved "
        "past the setup. So an idea that only works if you get filled in "
        "the next ten minutes is not worth nominating.")


def render_hunt_prompt(events: list, as_of: datetime,
                       already_known: set | None = None,
                       searchers: dict | None = None,
                       market_open: bool | None = None) -> str:
    """Ask for a short list, from evidence that exists.

    `market_open=False` adds `closed_market_brief()`. None means unknown,
    which keeps the ordinary brief - the conservative direction, since
    that is what every hunt got before this existed.
    """
    digest, _ = _digest(events, as_of)
    known = ", ".join(sorted(already_known or set())) or "none"
    tools_text = _tools_section(searchers)
    return "\n\n".join([part for part in [
        closed_market_brief() if market_open is False else "",
        # WHAT DAY IT IS, and this prompt needed it more than any other.
        #
        # `as_of` has been a parameter of this function since it was
        # written, and went only to `_digest` and `_validate`. So the
        # code refused any nomination whose catalyst date was before
        # `as_of.date()` while the prompt never said what that date was
        # - the model was asked to obey a rule about "today" without
        # being told when today is.
        #
        # That is the 88%-rejection defect one level deeper. §3 row 12's
        # fix STATED THE RULE ("the event must resolve today or later"),
        # which helped; it did not supply the date the rule is measured
        # against. Both halves are needed and now both are here.
        "RIGHT NOW\n"
        f"Today is {as_of.date().isoformat()}, a "
        f"{as_of.strftime('%A')}, and the time is "
        f"{as_of.strftime('%H:%M')} UTC. Read every date below against "
        "that, and pick your catalyst dates against it: the hard rule "
        "further down is measured from this exact date by code that "
        "refuses anything earlier.",
        "You are the discovery step of an automated trading system. You "
        "are reading a day of raw regulatory filings and market news, "
        "and choosing which of them are worth paying to research "
        "properly. Deterministic code decides everything after that: "
        "whether to trade, how large, and where the stop sits. Nominate "
        "judgements, never sizes or prices.",

        "WHAT MAKES A NOMINATION WORTH ITS COST\n"
        "- A DATED, resolvable event. Something happens on or before a "
        "day you can name, and the price should react when it does.\n"
        "- A mechanism you can state. 'This company is interesting' is "
        "not one; 'the FDA advisory committee meets on the 3rd and the "
        "stock is a single-product company' is.\n"
        "- Not already consumed. If the filing is a week old and the "
        "stock has already moved, the trade has happened without you.\n"
        "- Liquid enough to matter. A micro-cap nobody can exit is not "
        "an opportunity.",

        # THE RULE THAT WAS COSTING 88% OF EVERY HUNT.
        #
        # OWNER'S LOG, 2026-08-26..30: 25 nominations, 22 rejected, all
        # for the same reason - "catalyst date is in the past". The
        # schema field said "Today or later" and the prompt never did,
        # while the prompt's own guidance ("not already consumed") is
        # about STALENESS, which is a different test. A model reading a
        # feed of things that happened this morning reasonably judges
        # them fresh, dates them today-or-yesterday, and is refused.
        #
        # Saying the rule plainly, with the reason, lets the model spend
        # its nominations on what can pass instead of learning a
        # tripwire by hitting it.
        "THE ONE HARD RULE ON DATES. The catalyst date is when the event "
        "RESOLVES, and it must be today or later - never a past date. An "
        "earnings release that already happened this morning is not a "
        "catalyst dated today; it is an event that has resolved, and the "
        "code will refuse it. If a story's only event is already over, "
        "the nomination is wasted - either name the NEXT dated event for "
        "that company, or leave it out.",

        "TWO MECHANICAL SCREENS ALREADY BUILT CANDIDATES: insider "
        "clusters with cross-feed agreement, and post-earnings drift "
        "from XBRL surprise. BOTH ARE GRADED, and between them they "
        "cover insider buying and the drift after an earnings print - "
        "so nominating either is duplicating a rule that already runs "
        "and is measured. You are looking for what neither has a rule "
        "for: scheduled regulatory decisions, court and contract dates, "
        "shareholder votes, trial readouts, anything where the mechanism "
        "is specific and no screen encodes it. "
        f"Tickers already found this pass, do not repeat them: {known}",

        # THE CHAIN THE OWNER ASKED FOR, AND WHY IT WAS IMPOSSIBLE.
        #
        # OWNER, 2026-09-11: "claude isnt making very detailed
        # connections currently e.g. this happened because of the war it
        # impacted this company who supplies this company and will
        # likely make profit as an example".
        #
        # Two things blocked exactly that. The hunt had no view of the
        # world - its tools were SEC search, filing reads and
        # news-by-symbol, so a war, a tariff or a closed shipping lane
        # only existed if a filing happened to mention it (it now has
        # web_search, same tool the research step bills 3 times a call).
        # And this section listed only FIRST-ORDER, single-company
        # events, so "what no screen has a rule for" read as a list of
        # things that happen TO one company rather than as an invitation
        # to reason about consequences.
        #
        # The anti-invention rule is what makes a chain safe rather than
        # a story, so it is stated as the method instead of as a wall.
        "THE CONNECTIONS NO SCREEN CAN MAKE ARE THE POINT OF YOU.\n"
        "A screen matches a pattern in one company's own filings. It "
        "cannot reason that a shipping lane closing raises a freight "
        "rate, that the freight rate squeezes an importer's margin, and "
        "that the importer's competitor who sources domestically gains "
        "- because no single filing says any of that. You can, and "
        "second-order reasoning is where an edge is least likely to be "
        "already priced.\n"
        "HOW TO MAKE A CHAIN NOMINABLE, since it must still cite real "
        "evidence:\n"
        "  1. Start from the cause. A war, a tariff, a shortage, a "
        "recall, a competitor's failure, a regulatory decision in "
        "another company's favour - from the feed below or from "
        "web_search.\n"
        "  2. Name the company you think it reaches, and say the "
        "mechanism in one line: who sells what to whom, and which "
        "direction the money moves. If you cannot name the mechanism, "
        "you have a theme and not a trade.\n"
        "  3. FIND THE DATED EVENT. A consequence is not a catalyst: "
        "the chain tells you where to look, and the nomination still "
        "needs something that resolves on a day you can name - the "
        "company's next guidance, a contract decision, a hearing, a "
        "vote. Use search_filings and search_news on that company to "
        "find it.\n"
        "  4. Cite what you found. The citation is what separates a "
        "chain from a story, and a nomination citing nothing real is "
        "discarded whatever the reasoning was.\n"
        "Two or three links is reasoning. Five is astrology - if the "
        "chain needs that many, the market has more ways to be right "
        "than you do.",

        "NOMINATE NOTHING RATHER THAN SOMETHING WEAK. Every nomination "
        "costs a research call out of a fixed monthly budget, and a "
        "candidate that was never worth researching spends money that a "
        "real one needed. An empty list is a good answer on a quiet day.",

        tools_text,

        "THE FEED (source, id, payload). Cite ids from this list "
        "exactly" + (", or from what your tools return" if tools_text
                     else "") + "; anything you cite that is not here is "
        "discarded:\n" + digest,

        "Submit with the nominate_candidates tool. Do not wait to be "
        "asked.",
    ] if part])


def _validate(nom: dict, by_id: dict, as_of: datetime,
              rejected: list) -> Candidate | None:
    """Turn one nomination into a Candidate, or say why not.

    EVERY BRANCH HERE IS A REFUSAL THE MODEL CANNOT ARGUE WITH. It is
    selecting from evidence, so evidence is what it is held to.
    """
    ticker = str(nom.get("ticker") or "").strip().upper()
    if not ticker.isalpha() or not 1 <= len(ticker) <= 5:
        rejected.append((ticker or "?", "not a plausible US symbol"))
        return None

    ctype = str(nom.get("catalyst_type") or "")
    if ctype not in CATALYST_TYPES:
        rejected.append((ticker, f"catalyst type {ctype!r} has no sizing basis"))
        return None

    # THE ANTI-INVENTION CHECK. Ids must resolve to rows really in the
    # feed, and the ticker must appear in those rows' own payloads - so
    # a real event cannot be re-labelled onto a different company.
    ids = [str(s) for s in (nom.get("source_event_ids") or [])]
    found = [i for i in ids if i in by_id]
    if not found:
        rejected.append((ticker, "cited no source event that exists in the feed"))
        return None
    corroborated = False
    for sid in found:
        payload = getattr(by_id[sid], "payload_raw", None)
        try:
            text = json.dumps(payload) if not isinstance(payload, str) \
                else payload
        except (TypeError, ValueError):
            text = str(payload)
        if ticker in text.upper():
            corroborated = True
            break
    if not corroborated:
        rejected.append(
            (ticker, "the cited events do not mention this ticker"))
        return None

    try:
        cdate = date.fromisoformat(str(nom.get("catalyst_date"))[:10])
    except (TypeError, ValueError):
        rejected.append((ticker, "catalyst date is not a date"))
        return None
    today = as_of.date()
    if cdate < today:
        rejected.append((ticker, f"catalyst date {cdate} is in the past"))
        return None
    if cdate > today + timedelta(days=MAX_DAYS_AHEAD):
        rejected.append(
            (ticker, f"catalyst date {cdate} is beyond {MAX_DAYS_AHEAD} days"))
        return None

    from catalyst.discovery.universe import excluded_reason

    excluded = excluded_reason(ticker)
    if excluded:
        rejected.append((ticker, excluded))
        return None

    confidence = str(nom.get("date_confidence") or "estimated")
    if confidence not in ("confirmed", "estimated"):
        confidence = "estimated"

    return Candidate(
        id=f"hunt-{ticker}-{cdate.isoformat()}-{uuid.uuid5(uuid.NAMESPACE_URL, ticker + ctype + cdate.isoformat()).hex[:12]}",
        ticker=ticker,
        catalyst_type=ctype,
        catalyst_date=cdate,
        catalyst_date_confidence=confidence,
        source_event_ids=tuple(found),
        discovered_at=as_of,
        # Sector is unknown from a headline. "unknown" is what the
        # cluster key already expects for an unclassified name, and it
        # is honest - guessing one would corrupt the correlation bound
        # that stops four bets on the same thing looking like four bets.
        sector="unknown",
        correlation_tags=(ctype,),
    )


#: Per additional tool turn, re-authorised by the governor before it is
#: sent: the digest plus everything found so far is re-sent each turn.
#: The first turn is covered by HUNT_ESTIMATE_CENTS.
HUNT_TURN_ESTIMATE_CENTS = Decimal("20")


def _turn_estimate(conn) -> Decimal:
    """A continuation turn, estimated from what whole hunts have cost.

    There is no separate ledger row for a single turn - a hunt's cost is
    recorded per CALL, which is why this constant was never measured and
    stayed at 20c indefinitely. What can be measured is the whole hunt,
    and a continuation turn re-reads the transcript so far, so it is
    bounded above by the hunt total. Using that total is deliberately
    pessimistic: the governor compares an estimate against actual spend
    rather than reserving it, so over-estimating costs only at the cap
    boundary while under-estimating costs the cap.
    """
    from catalyst.cost.observed import observed_call_cents

    measured, n = observed_call_cents(conn, "hunt", HUNT_TURN_ESTIMATE_CENTS)
    return max(measured, HUNT_TURN_ESTIMATE_CENTS) if n else \
        HUNT_TURN_ESTIMATE_CENTS
#: Turns in one hunt, tool turns plus the final nomination.
MAX_HUNT_TURNS = 10


def hunt(events: list, as_of: datetime, transport, cost_context,
         already_known: set | None = None, model: str | None = None,
         searchers: dict | None = None,
         market_open: bool | None = None) -> HuntResult:
    """One hunt: read the feed, nominate, validate, return candidates.

    NEVER RAISES. Discovery is upstream of everything, so a hunt that
    fails - no budget, a transport error, a malformed reply - must leave
    the mechanical screen's candidates untouched and the cycle running.
    Every failure path returns a HuntResult carrying its reason.

    Spend goes through the SAME governor as research, tagged component
    "hunt", so it is bounded by the same monthly cap, the same derived
    daily ceiling, and stops on the same unacknowledged discrepancy.
    """
    from catalyst.cost.governor import CostEstimate, authorize
    from catalyst.cost.tracker import UnknownModelError, record_usage
    from catalyst.research.boundary import RESEARCH_MODEL

    result = HuntResult()
    if transport is None:
        result.skipped_reason = "no_model_transport_configured"
        return result
    if not events:
        result.skipped_reason = "no_raw_events_to_read"
        return result

    model = model or RESEARCH_MODEL
    digest, by_id = _digest(events, as_of)
    if not by_id:
        result.skipped_reason = "no_raw_events_carried_a_source_id"
        return result
    result.prompt = render_hunt_prompt(events, as_of, already_known,
                                       searchers, market_open=market_open)

    conn = cost_context.conn
    call_id = str(uuid.uuid4())
    # WHAT A HUNT HAS ACTUALLY COST, not what someone typed after
    # reading one bundle. Owner-asked 2026-09-11: "we dont want to be
    # changing estimates manually." The seed stays for the cold start,
    # and the basis says which of the two this figure is so the
    # authorisation record can be read later.
    from catalyst.cost.observed import observed_call_cents

    per_hunt, n_seen = observed_call_cents(conn, "hunt", HUNT_ESTIMATE_CENTS)
    estimate = CostEstimate(
        estimated_cents=per_hunt,
        basis=("one bounded digest pass (discovery/hunt.py), "
               + (f"p75 of {n_seen} measured hunt(s)" if n_seen
                  else "seed estimate, nothing measured yet")),
        kind=cost_context.kind, component="hunt")
    decision = authorize(estimate, conn, cost_context.governor_profit_share,
                         cycle_id=cost_context.cycle_id,
                         owner_monthly_cap_cents=(
                             cost_context.owner_monthly_cap_cents))
    if not decision.authorized:
        result.skipped_reason = ("budget_denied: " + decision.reason
                                 if decision.reason else "budget_denied")
        return result

    from catalyst.discovery.hunt_tools import (
        HUNT_SEARCHES, MAX_TOOL_CALLS, run_tool, searches_billed,
        tool_schemas, web_search_tools,
    )

    searchers = dict(searchers or {})
    # CLIENT tools are the ones this process executes and must answer
    # with a tool_result. web_search is SERVER-side: Anthropic runs it
    # inside the turn and answers it there, so it is offered in the
    # payload and never appears in `uses` below.
    client_tools = tool_schemas(searchers)
    client_names = {t["name"] for t in client_tools}
    usages: list = []
    offered = client_tools + web_search_tools()
    forced = {"type": "tool", "name": "nominate_candidates"}
    messages = [{"role": "user", "content": result.prompt}]
    payload = {
        "model": model,
        "max_tokens": 4000,
        "messages": messages,
        "tools": [NOMINATE_TOOL] + offered,
        # With hands, the model chooses when it has searched enough. Without
        # them there is nothing to wait for, so the nomination is forced on
        # the first turn exactly as before.
        "tool_choice": {"type": "auto"} if offered else forced,
    }

    def _priced(response):
        # Price it BEFORE reading the answer. A call that produced nothing
        # usable still cost money, and a row priced later is a row that can
        # be missed entirely if the parse below raises.
        raw_usage = response.get("usage") or {
            "unparseable_usage": "response carried no usage object"}
        usages.append(raw_usage)
        try:
            event = record_usage(raw_usage, model, cost_context.kind, "hunt",
                                 conn, api_call_id=call_id)
            if event.priced_cents is not None:
                result.cost_cents += event.priced_cents
        except Exception:  # noqa: BLE001 - record_usage writes the row first
            _log.debug("hunt usage could not be priced", exc_info=True)

    def _has_nomination(response):
        return any(isinstance(b, dict) and b.get("name") == "nominate_candidates"
                   for b in (response.get("content") or []))

    def _live_tools():
        """The tools for the NEXT request, with the search allowance
        carried over. max_uses is per request, so re-sending the list
        verbatim would refill it every turn."""
        return ([NOMINATE_TOOL] + client_tools
                + web_search_tools(HUNT_SEARCHES - searches_billed(usages)))

    response: dict = {}
    # With no tools at all the first turn IS the forced nomination, so
    # there is nothing to ask a second time - one call, exactly as the
    # hunt was before it had any.
    forced_already = not offered
    for _turn in range(MAX_HUNT_TURNS):
        try:
            response = transport(payload)
        except Exception as exc:  # noqa: BLE001 - discovery must not die here
            result.skipped_reason = f"transport_error: {type(exc).__name__}: {exc}"
            return result
        if not isinstance(response, dict):
            response = {"unparseable_response": repr(response)[:2000]}
        result.raw_response = response
        result.turns += 1
        _priced(response)
        if _has_nomination(response):
            break

        content = response.get("content") or []
        uses = [b for b in content if isinstance(b, dict)
                and b.get("type") == "tool_use" and b.get("id")
                and b.get("name") in client_names]
        # THE MODEL SEES ITS OWN LAST TURN. The API refuses an assistant
        # message with no content, so a turn that carried none is not
        # echoed (research/boundary.py learned this on four dead calls).
        echo = ({"role": "assistant", "content": [b for b in content
                                                  if isinstance(b, dict)]}
                if any(isinstance(b, dict) for b in content) else None)

        # A SERVER-SIDE SEARCH STILL IN FLIGHT. `pause_turn` means
        # Anthropic ran a web_search inside the turn and stopped to let
        # us continue; the answer is already in the content it handed
        # back. Echoing it and asking again is the whole protocol
        # (research/boundary.py does the same) - forcing a nomination
        # here instead would cut the search off mid-thought, which is
        # the one thing that would make the tool useless.
        if (response.get("stop_reason") == "pause_turn" and not uses
                and echo is not None):
            messages.append(echo)
            payload["tools"] = _live_tools()
            continue

        # MORE TOOL WORK? Only while there is budget for it: the whole
        # transcript is re-sent every turn, and the governor is asked
        # before each one so a hunt cannot outspend its day.
        room = (uses and len(result.tool_calls) + len(uses) <= MAX_TOOL_CALLS
                and not forced_already)
        if room:
            decision = authorize(
                CostEstimate(estimated_cents=_turn_estimate(conn),
                             basis="one hunt tool turn (discovery/hunt.py)",
                             kind=cost_context.kind, component="hunt"),
                conn, cost_context.governor_profit_share,
                cycle_id=cost_context.cycle_id,
                owner_monthly_cap_cents=cost_context.owner_monthly_cap_cents)
            room = decision.authorized
        answers = []
        for use in uses:
            if room:
                text, is_error = run_tool(str(use.get("name")),
                                          use.get("input") or {}, searchers,
                                          by_id, conn, as_of)
                result.tool_calls.append((use.get("name"), use.get("input"),
                                          is_error))
            else:
                text, is_error = ("No more tool calls are available in this "
                                  "hunt. Nominate from what you have, or "
                                  "nothing."), True
            answers.append({"type": "tool_result", "tool_use_id": use["id"],
                            "is_error": is_error, "content": text})
        if echo is not None:
            messages.append(echo)
        if room and answers:
            messages.append({"role": "user", "content": answers})
            payload["tools"] = _live_tools()
            continue
        # NO NOMINATION AND NOTHING LEFT TO DO: ask for it once, forced.
        if forced_already:
            break
        forced_already = True
        messages.append({"role": "user", "content": (answers or []) + [
            {"type": "text", "text": "Submit your nominations now via "
                                     "nominate_candidates - an empty list "
                                     "is a valid answer."}]}
                        if answers else
                        {"role": "user", "content": "Submit your nominations "
                         "now via nominate_candidates - an empty list is a "
                         "valid answer."})
        payload["tool_choice"] = forced
    result.found = max(0, len(by_id) - len(_digest(events, as_of)[1]))

    noms = None
    for block in response.get("content") or []:
        if isinstance(block, dict) and block.get("name") == "nominate_candidates":
            noms = (block.get("input") or {}).get("nominations")
            break
    if noms is None:
        result.skipped_reason = "the model returned no nominate_candidates call"
        return result
    if not isinstance(noms, list):
        result.skipped_reason = "nominations was not a list"
        return result

    result.nominations = len(noms)
    seen: set = set()
    for nom in noms[:MAX_NOMINATIONS]:
        if not isinstance(nom, dict):
            result.rejected.append(("?", "nomination was not an object"))
            continue
        cand = _validate(nom, by_id, as_of, result.rejected)
        if cand is None:
            continue
        if cand.ticker in seen:
            result.rejected.append((cand.ticker, "nominated twice in one hunt"))
            continue
        seen.add(cand.ticker)
        result.candidates.append(cand)
        result.rationales[cand.id] = str(nom.get("why") or "")[:2000]

    _log.info(
        "Hunt read %d feed item(s) and nominated %d; %d became candidates, "
        "%d were rejected against the evidence.",
        len(by_id), result.nominations, len(result.candidates),
        len(result.rejected))
    return result
