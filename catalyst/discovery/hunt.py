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
    skipped_reason: str | None = None


def hunts_per_day(owner_monthly_cap_cents=None) -> int:
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
    need = HUNT_ESTIMATE_CENTS * 3
    return min(4, int(per_day // need))


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


def render_hunt_prompt(events: list, as_of: datetime,
                       already_known: set | None = None) -> str:
    """Ask for a short list, from evidence that exists."""
    digest, _ = _digest(events, as_of)
    known = ", ".join(sorted(already_known or set())) or "none"
    return "\n\n".join([
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

        "A MECHANICAL SCREEN ALREADY BUILT CANDIDATES FROM INSIDER "
        "CLUSTERS AND CROSS-FEED AGREEMENT. You are looking for what it "
        "MISSED - the filings and headlines it has no rule for. "
        f"Tickers it already found this pass, do not repeat them: {known}",

        "NOMINATE NOTHING RATHER THAN SOMETHING WEAK. Every nomination "
        "costs a research call out of a fixed monthly budget, and a "
        "candidate that was never worth researching spends money that a "
        "real one needed. An empty list is a good answer on a quiet day.",

        "THE FEED (source, id, payload). Cite ids from this list "
        "exactly; anything you cite that is not here is discarded:\n"
        + digest,

        "Submit with the nominate_candidates tool. Do not wait to be "
        "asked.",
    ])


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


def hunt(events: list, as_of: datetime, transport, cost_context,
         already_known: set | None = None, model: str | None = None
         ) -> HuntResult:
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
    result.prompt = render_hunt_prompt(events, as_of, already_known)

    conn = cost_context.conn
    call_id = str(uuid.uuid4())
    estimate = CostEstimate(
        estimated_cents=HUNT_ESTIMATE_CENTS,
        basis="one bounded digest pass (discovery/hunt.py)",
        kind=cost_context.kind, component="hunt")
    decision = authorize(estimate, conn, cost_context.governor_profit_share,
                         cycle_id=cost_context.cycle_id,
                         owner_monthly_cap_cents=(
                             cost_context.owner_monthly_cap_cents))
    if not decision.authorized:
        result.skipped_reason = ("budget_denied: " + decision.reason
                                 if decision.reason else "budget_denied")
        return result

    payload = {
        "model": model,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": result.prompt}],
        "tools": [NOMINATE_TOOL],
        "tool_choice": {"type": "tool", "name": "nominate_candidates"},
    }
    try:
        response = transport(payload)
    except Exception as exc:  # noqa: BLE001 - discovery must not die here
        result.skipped_reason = f"transport_error: {type(exc).__name__}: {exc}"
        return result
    if not isinstance(response, dict):
        response = {"unparseable_response": repr(response)[:2000]}
    result.raw_response = response

    # Price it BEFORE reading the answer. A call that produced nothing
    # usable still cost money, and a row priced later is a row that can
    # be missed entirely if the parse below raises.
    raw_usage = response.get("usage") or {
        "unparseable_usage": "response carried no usage object"}
    try:
        event = record_usage(raw_usage, model, cost_context.kind, "hunt",
                             conn, api_call_id=call_id)
        if event.priced_cents is not None:
            result.cost_cents = event.priced_cents
    except Exception:  # noqa: BLE001 - record_usage writes the row first
        _log.debug("hunt usage could not be priced", exc_info=True)

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
