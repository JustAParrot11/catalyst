"""The model/code boundary. HUMAN REVIEW REQUIRED on any change.

investigate() is the ONLY place in the system that can spend money on a
model call, and the only code path that produces a ResearchView. It runs
zero or more exploration turns (tool_choice=auto) then exactly one
extraction turn with tool_choice forced to submit_research_view
(ARCHITECTURE.md section 4.2). Every turn is authorized by the cost
governor before it is made and priced after (section 7.3).

No Anthropic API key is present in the build environment; this module is
written against the documented API shapes and exercised offline through
a stub transport injected by tests. The live transport is only
constructed when an api key exists in the runtime credential store.

Discipline order per turn (audit F2/N1): authorize -> call -> RECORD the
raw usage verbatim -> price. A turn whose usage cannot be priced still
lands in the ledger, and the governor blocks further spend until it is
repriced - never the reverse.
"""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Literal

from catalyst.cost import CostEstimate
from catalyst.cost.governor import authorize
from catalyst.cost.pricing import (
    WEB_SEARCH_CENTS_PER_QUERY,
    UnknownModelError,
    rates_for,
)
from catalyst.cost.tracker import (
    UNPARSEABLE_USAGE_KEY,
    UnrecognizedUsageFieldError,
    record_usage,
)
from catalyst.discovery import Candidate
from catalyst.research import prompts
from catalyst.research.schema import (
    SUBMIT_RESEARCH_VIEW_TOOL,
    APITurn,
    ResearchCallLog,
    ResearchView,
    UsageComponents,
    make_view_from_tool_input,
)

RESEARCH_MODEL = "claude-sonnet-5"      # judgement calls; cheap enough to
                                        # keep the $5/month cap honest
MAX_EXPLORATION_TURNS = 2               # pause_turn continuations included
#: Output cap on the EXPLORATION turn - the one that thinks and searches.
#: MEASURED, from the owner's live bundle for 2026-08-14. Of 65
#: exploration turns, 38 stopped on `max_tokens` and 26 on `tool_use`:
#:
#:     turn 0 (exploration):  max_tokens 38   tool_use 26   end_turn 1
#:     turn 1 (extraction):   tool_use 18
#:
#: 58% of the bot's thinking turns were being cut off mid-sentence and
#: then forced, on the very next turn, to submit a conclusion. That is
#: not a cost control, it is a quality ceiling, and it was invisible
#: because a truncated turn still produces a view.
#:
#: max_tokens is a CEILING, NOT A PURCHASE: output is billed on what the
#: model actually emits. Raising it costs nothing on the 26 turns that
#: already finished, and buys room on the 38 that did not.
#:
#: Owner's steer, 2026-08-14: "i dont want to be narrowing the bots
#: scope" and "we dont want to reduce quality".
MAX_EXPLORATION_TOKENS = 8192

#: The FORCED turn emits one JSON tool call and nothing else, and this
#: number is already tuned by a live failure: a 512 ceiling truncated a
#: real forced tool call mid-JSON, the parser refused the partial view,
#: and the paid call was wasted. It stays where it was - the extraction
#: turn was never the one being starved (18 of 18 stopped on tool_use,
#: none on max_tokens).
MAX_EXTRACTION_TOKENS = 2048
_MTOK = Decimal(1_000_000)

# Pre-call estimates, deliberately pessimistic (the governor compares
# against these BEFORE the call; an optimistic estimate is a hole in the
# cap). Basis: MEASURED, not assumed (cost-audit F4 - the original 8c/5c
# "pessimistic" figures were 58% under reality). The first live research
# call (2026-08-10, fixture in tests/test_cost_api_adapter.py) cost
# 12.63c for exploration: 24k tokens in (web search results are large),
# 2.3k out, 2 searches at 1c each. 15c covers that with a third search;
# extraction measured ~1.3c, 8c leaves headroom for a repair turn.
#
# THE FLAT CONSTANT WAS THE HOLE. 15c was calibrated when BASE_SEARCHES
# = 3 was the only search budget there was. CONJUNCTION_SEARCHES = 10
# broke it, and priced against the live rate table the gap is not small:
#
#     searches  input     today (intro)   after 2026-08-31
#      3        24k        9.85c   ok      13.27c   ok
#     10        24k       16.85c   OVER    20.27c   OVER
#     10        60k       24.05c   OVER    31.07c   OVER
#     10       120k       36.05c   OVER    49.07c   OVER
#
# Two things move underneath the estimate and BOTH are known before the
# call: how many searches this candidate earned, and what the model
# costs on the day. Sonnet 5's introductory rate ends 2026-08-31, which
# lifts every figure above by ~50% on a date already in the calendar -
# a flat constant would silently become optimistic overnight.
#
# Pessimism here is cheap. The governor compares the estimate against
# ACTUAL month-to-date spend rather than reserving it, so an overestimate
# costs only at the cap boundary; an underestimate costs the cap itself.
EXPLORATION_TURN_ESTIMATE_CENTS = Decimal("15")   # base path, no searches
EXTRACTION_TURN_ESTIMATE_CENTS = Decimal("8")

#: SEED value for input tokens a single web search adds. Measured from
#: the first live research call (2026-08-10): 24k input tokens carrying
#: 2 searches against a ~2k prompt, so ~11k per search, rounded up.
#:
#: A sample of ONE, which is why input_tokens_per_search() below replaces
#: it with the real distribution as soon as there is one. This constant
#: is the floor and the starting point, never the last word.
INPUT_TOKENS_PER_SEARCH = 12_000
#: The prompt itself, before any search results come back.
PROMPT_TOKENS_ESTIMATE = 2_000
#: Turns that actually searched, before the measured figure is used at
#: all. BUILD-BRIEF: "Adapting on four trades is fitting noise, and noise
#: is what you are trying to remove."
MIN_CALIBRATION_SAMPLE = 8
#: Which point of the observed distribution to estimate from. The mean is
#: dragged down by cheap turns and the estimate exists to cover the
#: expensive ones, so this sits high without chasing the single worst.
CALIBRATION_PERCENTILE = 0.75


def observed_tokens_per_search(conn) -> tuple[int | None, int]:
    """(tokens per search at CALIBRATION_PERCENTILE, sample size).

    Read from the raw usage object stored verbatim on every turn
    (TRAPS.md) rather than from named columns, so a renamed or nested
    field shows up as a missing sample instead of silently pricing
    itself at zero.

    Turns that billed no searches are excluded: they say nothing about
    what a search costs, and dividing by zero of them says nothing at
    all.
    """
    if conn is None:
        return None, 0
    try:
        rows = conn.execute(
            "SELECT raw_usage_json FROM cost_events "
            "WHERE component IN ('research', 'position_review')").fetchall()
    except sqlite3.Error:
        return None, 0
    per_search: list[float] = []
    for (blob,) in rows:
        try:
            usage = json.loads(blob)
            searches = int((usage.get("server_tool_use") or {}).get(
                "web_search_requests", 0) or 0)
            if searches < 1:
                continue
            search_tokens = int(usage.get("input_tokens", 0) or 0) \
                - PROMPT_TOKENS_ESTIMATE
            if search_tokens <= 0:
                continue
            per_search.append(search_tokens / searches)
        except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
            continue      # evidence lost, never a raised exception
    if len(per_search) < MIN_CALIBRATION_SAMPLE:
        return None, len(per_search)
    per_search.sort()
    idx = min(len(per_search) - 1,
              int(len(per_search) * CALIBRATION_PERCENTILE))
    return int(per_search[idx]), len(per_search)


def input_tokens_per_search(conn=None) -> int:
    """The seed, RAISED by measured evidence and never lowered.

    THE ASYMMETRY IS THE WHOLE DESIGN. Raising is always safe: the
    governor compares the estimate against ACTUAL month-to-date spend
    rather than reserving it, so pessimism costs only at the cap
    boundary. Lowering re-opens the hole a flat constant already put in
    that cap once - and a quiet fortnight of cheap turns is all the
    "evidence" an auto-lowering estimate would need.

    So a human lowers the seed, on the observed figure, which the
    dashboard shows beside it. That is BUILD-BRIEF's "tighten quickly on
    evidence of harm; loosen slowly on evidence of over-caution", taken
    to its limit for a parameter guarding a spend cap.
    """
    observed, _sample = observed_tokens_per_search(conn)
    if observed is None:
        return INPUT_TOKENS_PER_SEARCH
    return max(INPUT_TOKENS_PER_SEARCH, observed)


def exploration_turn_estimate_cents(searches: int,
                                    on_date: date | None = None,
                                    model: str = RESEARCH_MODEL,
                                    conn=None) -> Decimal:
    """Pessimistic cost of one exploration turn at this search budget.

    Priced through rates_for(), the same table that prices the actual
    bill, so the estimate cannot drift away from what is charged when a
    rate changes. Search itself is exact: 1c a query (TRAPS.md), and the
    query count is the budget this candidate earned.
    """
    on_date = on_date or datetime.now(timezone.utc).date()
    in_rate, out_rate = rates_for(model, on_date)
    input_tokens = _exploration_input_tokens(searches, conn)
    return (Decimal(input_tokens) * in_rate / _MTOK
            + Decimal(MAX_EXPLORATION_TOKENS) * out_rate / _MTOK
            + Decimal(searches) * WEB_SEARCH_CENTS_PER_QUERY)


def _exploration_input_tokens(searches: int, conn=None) -> int:
    return (PROMPT_TOKENS_ESTIMATE
            + searches * input_tokens_per_search(conn))


#: Output tokens the forced turn emits: one small JSON tool call. It is
#: capped at MAX_EXTRACTION_TOKENS, but the schema is a handful of
#: fields and the measured call emitted far less.
EXTRACTION_OUTPUT_TOKENS_ESTIMATE = 512


def extraction_turn_estimate_cents(searches: int,
                                   on_date: date | None = None,
                                   model: str = RESEARCH_MODEL,
                                   conn=None) -> Decimal:
    """Pessimistic cost of the forced extraction turn.

    THE SAME HOLE AS EXPLORATION'S, one line down. This turn RE-SENDS THE
    ENTIRE exploration context - avoiding that re-read is the whole
    reason the schema tool is offered during exploration - so its cost
    scales with the search budget exactly as exploration's does. A flat
    8c, measured once at ~1.3c on a two-search call, was over on BOTH
    paths and 4.7x over on a conjunction:

        searches   re-read   today (intro)   after 2026-08-31
         3          40k       8.52c           12.78c
        10         124k      25.32c           37.98c

    No search charge: the forced turn offers only the schema tool, so it
    cannot search, and estimating for a mechanism that does not exist is
    pessimism without a reason.
    """
    on_date = on_date or datetime.now(timezone.utc).date()
    in_rate, out_rate = rates_for(model, on_date)
    # prompt + every search result + the assistant turn being echoed back
    input_tokens = (_exploration_input_tokens(searches, conn)
                    + MAX_EXPLORATION_TOKENS)
    return (Decimal(input_tokens) * in_rate / _MTOK
            + Decimal(EXTRACTION_OUTPUT_TOKENS_ESTIMATE) * out_rate / _MTOK)


@dataclass(frozen=True)
class CostContext:
    """Everything investigate() needs to spend money accountably."""

    conn: object                        # sqlite3.Connection
    governor_profit_share: Decimal      # live adaptive value, always passed
    cycle_id: str
    kind: Literal["scheduled", "manual"]
    owner_monthly_cap_cents: Decimal | None = None  # setup-page budget;
                                        # only ever LOWERS the cap (E1)


# A transport is a callable taking the full Messages API payload dict
# and returning the parsed response dict. Tests inject stubs; the live
# one wraps httpx against https://api.anthropic.com/v1/messages.
Transport = Callable[[dict], dict]


#: Content blocks the API pairs itself. `server_tool_use` (web search)
#: is executed by Anthropic inside the same turn and answered there, so
#: demanding a tool_result for one would refuse a request the API
#: accepts.
CLIENT_TOOL_USE = "tool_use"


def _answer_tool_calls(echo: dict | None, why: str | None) -> list | str:
    """The user turn that follows an echoed assistant message.

    Plain text when there is nothing to answer; otherwise one
    `tool_result` per `tool_use` in the echo, because the Messages API
    requires the match and rejects the whole request without it.
    """
    ask = "Submit your conclusion now via submit_research_view."
    blocks = (echo or {}).get("content")
    ids = [b.get("id") for b in blocks
           if isinstance(b, dict) and b.get("type") == CLIENT_TOOL_USE
           and b.get("id")] if isinstance(blocks, list) else []
    if not ids:
        return ask
    detail = (f"That submission was rejected: {why}. " if why else
              "That submission could not be read. ")
    return [{"type": "tool_result", "tool_use_id": tid, "is_error": True,
             "content": detail + "Required fields are missing or invalid. "
                        + ask}
            for tid in ids]


def _assistant_echo(turn) -> dict | None:
    """The assistant message to send back, or None if there is nothing
    valid to send.

    The Messages API REJECTS an assistant message whose content array is
    empty (HTTP 400). Every continuation here echoes the previous turn's
    content verbatim, so a turn that returns no content blocks - which
    pause_turn and some server-tool states do - poisoned the next
    request and killed the whole investigation. Four of the owner's five
    live research calls died this way on 2026-08-10.
    """
    content = (turn.raw_response or {}).get("content") or []
    if not isinstance(content, list) or not content:
        return None
    return {"role": "assistant", "content": content}


def searches_used(turns) -> int:
    """Web searches billed across this investigation so far.

    Read from the usage object each turn already records, so it counts
    what was CHARGED rather than what was intended. A turn whose usage
    could not be parsed contributes 0 - the governor's per-turn spend
    check is the hard gate above this, and guessing high here would
    silently deny a candidate searches it paid for.
    """
    total = 0
    for turn in turns:
        usage = getattr(turn, "usage", None)
        total += int(getattr(usage, "web_search_requests", 0) or 0)
    return total


def _tools_with_remaining_searches(budget: int, turns, tools: list) -> list:
    """`tools` for a continuation, with the search budget carried over.

    `max_uses` is per REQUEST. Re-sending the same tools list on a
    pause_turn continuation hands the model a FRESH allowance, so a
    candidate granted CONJUNCTION_SEARCHES could spend that many again
    on every continued turn. Web search is $10 per 1,000 queries
    (TRAPS.md) on top of tokens and is the one line of the bill the
    model moves directly, so the budget has to mean the investigation,
    not the request.

    With the allowance spent, web_search is dropped rather than offered
    at `max_uses: 0` - a zero budget is not a documented way to say "no
    searches left", and a rejected request costs a paid call. Every
    other tool survives, including the schema tool whose early
    submission is what avoids the full-price extraction re-read.
    """
    remaining = budget - searches_used(turns)
    kept = [t for t in tools if t.get("name") != "web_search"]
    if remaining <= 0:
        return kept
    return [dict(t, max_uses=remaining) if t.get("name") == "web_search"
            else t for t in tools]


#: Message shapes the Messages API rejects with a 400. Checked LOCALLY,
#: before the request, so the funnel names the actual defect instead of
#: showing a status code the owner has to guess at.
#:
#: The empty-content case (fff78e1, 2026-08-10) killed four of five live
#: research calls. Fixing that one instance is not the same as fixing
#: the class: any future shape that violates the contract would again
#: surface as a bare "400 Bad Request", cost a paid call, and take a
#: whole investigation with it. The owner reported the 400s as "quite
#: prevalent" a day later, which is exactly what an opaque error looks
#: like whether or not it is still happening.
def invalid_payload_reason(payload: dict,
                           *, continuing_pause_turn: bool = False) -> str | None:
    """Why the Messages API would reject this, or None if it looks sound.

    Deliberately conservative: it rejects only shapes that are certainly
    invalid. A false positive here silently skips a candidate that would
    have worked, which is worse than the 400 it is trying to prevent.

    `continuing_pause_turn` is that false positive, found by running.
    Continuing a paused server-tool loop means sending the assistant's
    partial turn back with NO user message after it - the exact shape
    the trailing-assistant rule below refuses. So every continuation the
    exploration loop made was killed locally, after the paid call, and
    MAX_EXPLORATION_TURNS=2 bought exactly one turn. The caller says
    when a trailing assistant message is intended; it is never assumed.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages is empty - the API requires at least one message"
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            return f"message {i} is not an object: {type(message).__name__}"
        role = message.get("role")
        if role not in ("user", "assistant"):
            return f"message {i} has role {role!r}, not 'user' or 'assistant'"
        content = message.get("content")
        if isinstance(content, str):
            if not content.strip():
                return f"message {i} ({role}) has empty text content"
            continue
        if not isinstance(content, list):
            return (f"message {i} ({role}) content is "
                    f"{type(content).__name__}, not a list or a string")
        if not content:
            # THE ONE THAT ACTUALLY HAPPENED.
            return (f"message {i} ({role}) has an EMPTY content array - the "
                    "Messages API rejects this, and a pause_turn or a "
                    "server-tool turn can return no content blocks")
        for j, block in enumerate(content):
            if not isinstance(block, dict):
                return (f"message {i} content block {j} is not an object: "
                        f"{type(block).__name__}")
            if not block.get("type"):
                return f"message {i} content block {j} has no 'type'"
    # EVERY `tool_use` MUST BE ANSWERED IN THE NEXT MESSAGE. The API
    # rejects the whole request otherwise - "tool_use ids were found
    # without tool_result blocks immediately after" - and it does so
    # after the previous turn has already been paid for. Five of the
    # owner's live research calls died on exactly this.
    #
    # `server_tool_use` (web search) is deliberately NOT checked: it is
    # executed and answered by Anthropic inside the same turn, so
    # demanding a tool_result for one would refuse a request the API
    # accepts.
    for i, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        pending = [b.get("id") for b in content if isinstance(b, dict)
                   and b.get("type") == CLIENT_TOOL_USE and b.get("id")]
        if not pending:
            continue
        following = (messages[i + 1].get("content")
                     if i + 1 < len(messages) else None)
        answered = {b.get("tool_use_id") for b in following
                    if isinstance(b, dict) and b.get("type") == "tool_result"} \
            if isinstance(following, list) else set()
        missing = [t for t in pending if t not in answered]
        if missing:
            return (f"message {i} calls tool_use {missing[0]} and the next "
                    "message carries no matching tool_result - the API "
                    "rejects this outright")

    if messages[-1].get("role") == "assistant" and not continuing_pause_turn:
        # A trailing assistant message asks the API to CONTINUE it. That
        # is exactly right for a pause_turn and wrong everywhere else,
        # where it means an echo was appended without the follow-up user
        # turn and the model is prompted with nothing to answer.
        return ("the last message is from the assistant with no user turn "
                "after it - a continuation was appended without its prompt")
    if not payload.get("model"):
        return "no model in the payload"
    if not isinstance(payload.get("max_tokens"), int) or payload["max_tokens"] < 1:
        return f"max_tokens is {payload.get('max_tokens')!r}, not a positive int"
    return None



def _record_findings(call_id: str, tool_input: dict, conn) -> None:
    """Persist the evidence links the pass already produced.

    Context, never a gate: the trade decision has been made and paid for
    by the time this runs, so a malformed finding must cost the graph a
    row, never cost the candidate its view. The hook is all-or-nothing
    per batch by design, and that transaction is its own business.
    """
    findings = tool_input.get("findings") if isinstance(tool_input, dict) else None
    if not isinstance(findings, list) or not findings:
        return
    try:
        from catalyst.graph.hooks import research_findings_to_graph
        research_findings_to_graph(call_id, findings, conn)
    except Exception:  # noqa: BLE001 - evidence is never worth a skip
        import logging
        logging.getLogger("catalyst.research").warning(
            "Research findings could not be stored in the evidence graph; "
            "the view itself is unaffected.", exc_info=True)


def investigate(
    candidate: Candidate,
    cost_context: CostContext,
    transport: Transport,
    model: str = RESEARCH_MODEL,
    graph_context: str | None = None,
    signals: list | None = None,
) -> ResearchCallLog:
    """`signals` is what each independent feed said about this ticker.

    Passing them turns a link the grouping code computed into something
    the model can actually weigh: it changes the question from "is this
    insider cluster priced in" to "do these unrelated things connect",
    and it earns a larger search budget because that question is open.
    None means an ordinary single-feed candidate, unchanged.
    """
    call_id = str(uuid.uuid4())
    conn = cost_context.conn
    started = time.monotonic()
    prompt = prompts.render_research_prompt(
        candidate, graph_context=graph_context, signals=signals)
    # The schema tool is offered DURING exploration as well as in the
    # forced turn. If the model submits its view while it still has the
    # search results in hand, the extraction turn - which re-sends the
    # ENTIRE context, 24k tokens on the measured live call, to collect a
    # few hundred tokens of JSON - never happens. That second full-price
    # read is 38% of a candidate's cost. The saving is opportunistic:
    # tool_choice stays `auto` here, so the model is never pushed into
    # concluding before it has searched, and the forced turn still runs
    # whenever no valid view arrives early.
    # The search budget follows the EVIDENCE. A conjunction - two or
    # more independent feeds agreeing on this ticker - earns a larger
    # allowance because its question ("do these connect?") is genuinely
    # open and the answer lives in reporting the feeds do not carry.
    # An ordinary candidate keeps the base allowance.
    search_budget = prompts.searches_for(candidate, signals)
    tools = list(prompts.exploration_tools(
        search_budget)) + [SUBMIT_RESEARCH_VIEW_TOOL]
    tools_offered = tuple(t.get("name", t.get("type", "?")) for t in tools)

    turns: list[APITurn] = []
    last_view_error: str | None = None
    # WHICH gate refused, not merely that one did. authorize() separates
    # cap_exceeded from reconciliation_discrepancy_unacknowledged from
    # unpriced_cost_rows, and the three have completely different fixes -
    # raise the budget, click acknowledge, fix a pricing bug. Collapsing
    # them to a bare "budget_denied" left a funnel of 125 refusals that
    # could not tell the owner which problem they had (owner-reported).
    denied_reason: str | None = None
    cost_cents = Decimal("0")
    unpriced: list[str] = []
    transport_errors: list[str] = []
    messages: list[dict] = [{"role": "user", "content": prompt}]

    def finish(view: ResearchView | None, skipped: str | None) -> ResearchCallLog:
        log = ResearchCallLog(
            id=call_id, candidate_id=candidate.id, model=model,
            prompt_rendered=prompt, tools_offered=tools_offered,
            api_turns=tuple(turns), parsed_view=view,
            cost_cents=cost_cents,
            latency_ms=int((time.monotonic() - started) * 1000),
            skipped_reason=skipped)
        _persist(log, conn)
        return log

    def run_turn(payload: dict,
                 *, continuing_pause_turn: bool = False) -> APITurn | None:
        """validate -> authorize -> call -> record -> price. None = stop."""
        nonlocal cost_cents, denied_reason
        # BEFORE SPENDING ANYTHING. A payload the API will certainly
        # reject must not consume a paid call and must not come back as
        # a status code the owner has to guess at - it names itself, in
        # the funnel, in English.
        bad = invalid_payload_reason(
            payload, continuing_pause_turn=continuing_pause_turn)
        if bad is not None:
            transport_errors.append(f"invalid_request_not_sent: {bad}")
            return None
        forced = (payload.get("tool_choice") or {}).get("type") == "tool"
        # The exploration estimate is derived from THIS candidate's
        # search budget at TODAY's rate, not a flat constant: a
        # conjunction buys ten searches where the base path buys three,
        # and Sonnet 5's introductory pricing ends 2026-08-31.
        try:
            cents = (extraction_turn_estimate_cents(
                         search_budget, model=model, conn=conn) if forced
                     else exploration_turn_estimate_cents(
                         search_budget, model=model, conn=conn))
        except UnknownModelError:
            # No rate for this model means record_usage cannot price the
            # call either, and has_unpriced_rows will block the next
            # authorization. Estimate high so this turn is the one that
            # stops, rather than sliding under the cap.
            cents = (EXTRACTION_TURN_ESTIMATE_CENTS if forced
                     else EXPLORATION_TURN_ESTIMATE_CENTS) * 4
        estimate = CostEstimate(
            estimated_cents=cents,
            basis="pre-registered per-turn pessimistic estimate (boundary.py)",
            kind=cost_context.kind, component="research")
        decision = authorize(estimate, conn,
                             cost_context.governor_profit_share,
                             cycle_id=cost_context.cycle_id,
                             owner_monthly_cap_cents=(
                                 cost_context.owner_monthly_cap_cents))
        if not decision.authorized:
            denied_reason = ("budget_denied: " + decision.reason
                             if decision.reason else "budget_denied")
            return None
        try:
            response = transport(payload)
        except Exception as exc:
            # The live transport raises on a network error or an API 5xx.
            # That used to escape investigate() and kill the whole cycle
            # mid-loop, leaving no research_calls row for a call that may
            # have been billed (stress-tester defect 23).
            transport_errors.append(
                f"transport_error: {type(exc).__name__}: {exc}")
            return None
        if not isinstance(response, dict):
            response = {"unparseable_response": repr(response)[:2000]}
        # An ABSENT usage object is not a free call. TRAPS.md's
        # renamed-field trap is exactly this: the unknown-field guard
        # inspects the usage object's contents, so it cannot see the
        # object itself going missing, and the turn priced at $0.00
        # (stress-tester defect 24).
        raw_usage = response["usage"] if "usage" in response else {
            UNPARSEABLE_USAGE_KEY: "response carried no usage object"}
        try:
            event = record_usage(raw_usage, model,
                                 cost_context.kind, "research", conn,
                                 api_call_id=call_id)
            usage = event.usage if isinstance(event.usage, UsageComponents) else None
            if event.priced_cents is not None:
                cost_cents += event.priced_cents
        except (UnknownModelError, UnrecognizedUsageFieldError) as exc:
            # record_usage has ALREADY written the row with priced_cents
            # NULL (record-first, TRAPS.md), and has_unpriced_rows now
            # blocks every further authorization. The exception used to
            # escape investigate() and kill the whole cycle mid-loop -
            # after an earlier candidate may already have been traded -
            # and the research_calls row for this paid call was never
            # written (stress-tester defect 13).
            unpriced.append(f"{type(exc).__name__}: {exc}")
            usage = None
        turn = APITurn(
            turn_index=len(turns), raw_response=response,
            usage=usage,
            stop_reason=response.get("stop_reason") or "")
        turns.append(turn)
        return turn

    # ---- exploration: tool_choice auto, server-side web search loops
    # inside one request; pause_turn continues it.
    turn = run_turn({
        "model": model, "max_tokens": MAX_EXPLORATION_TOKENS,
        "messages": messages, "tools": tools,
        "tool_choice": {"type": "auto"},
    })
    if turn is None:
        return finish(None, transport_errors[0] if transport_errors
                      else (denied_reason or "budget_denied"))
    if unpriced:
        return finish(None, f"usage_unpriced_governor_blocked: {unpriced[0]}")

    exploration_rounds = 1
    while (turn.stop_reason == "pause_turn"
           and exploration_rounds < MAX_EXPLORATION_TURNS):
        echo = _assistant_echo(turn)
        if echo is None:
            break            # nothing to continue from; go to extraction
        messages.append(echo)
        turn = run_turn({
            "model": model, "max_tokens": MAX_EXPLORATION_TOKENS,
            "messages": messages,
            "tools": _tools_with_remaining_searches(
                search_budget, turns, tools),
            "tool_choice": {"type": "auto"},
        }, continuing_pause_turn=True)
        if turn is None:
            return finish(None, transport_errors[0] if transport_errors
                          else (denied_reason or "budget_denied"))
        if unpriced:
            return finish(
                None, f"usage_unpriced_governor_blocked: {unpriced[0]}")
        exploration_rounds += 1

    # Did the model already answer? Only a FULLY VALID view short-circuits
    # - a malformed early submission falls through to the forced turn
    # rather than becoming a skipped candidate.
    try:
        early = _extract_tool_input(turn.raw_response)
    except AmbiguousExtraction:
        early = None
    if early is not None:
        try:
            # finish() persists the view; there is no separate writer.
            view = make_view_from_tool_input(candidate.id, early)
            _record_findings(call_id, early, conn)
            return finish(view, None)
        except (KeyError, TypeError, ValueError) as exc:
            # fall through to the forced extraction turn, carrying the
            # reason so the tool_result can name it
            last_view_error = f"{type(exc).__name__}: {exc}"

    echo = _assistant_echo(turn)
    if echo is not None:
        messages.append(echo)
    # ANSWER THE TOOL CALL. The echo above is verbatim, so when the model
    # submitted early and the view failed to parse it still carries that
    # `tool_use` block - and the Messages API requires the very next
    # message to carry a matching `tool_result`. Sending plain text
    # instead is a 400 ("tool_use ids were found without tool_result
    # blocks immediately after"), AFTER the exploration turn has been
    # paid for. Five of the owner's live research calls died this way.
    #
    # The result also carries WHY the view was rejected, which is more
    # use to the model than "submit your conclusion" and costs the same.
    messages.append({"role": "user",
                     "content": _answer_tool_calls(echo, last_view_error)})

    # ---- extraction: tool_choice FORCED. The model cannot answer in
    # prose; the only way out is the schema. Two live-API facts learned
    # 2026-08-10 shape this block:
    # - max_tokens 2048: a 512 ceiling truncated a real forced tool call
    #   mid-JSON (stop_reason max_tokens) - the parser refused the
    #   partial view, but the paid call was wasted;
    # - forced tool_choice does NOT enforce the schema's `required` list:
    #   real runs omitted a different required field each time. One
    #   bounded REPAIR turn names the problem and re-forces the tool;
    #   still-invalid output is then a named skip, never a default.
    last_error: str | None = None
    for attempt in ("first", "repair"):
        turn = run_turn({
            "model": model, "max_tokens": MAX_EXTRACTION_TOKENS,
            "messages": messages,
            "tools": [SUBMIT_RESEARCH_VIEW_TOOL],
            "tool_choice": {"type": "tool", "name": "submit_research_view"},
        })
        if turn is None:
            return finish(None, transport_errors[0] if transport_errors
                          else (denied_reason or "budget_denied"))
        if unpriced:
            return finish(None,
                          f"usage_unpriced_governor_blocked: {unpriced[0]}")

        if turn.stop_reason == "max_tokens":
            last_error = "extraction_truncated_max_tokens"
        else:
            try:
                tool_input = _extract_tool_input(turn.raw_response)
            except AmbiguousExtraction as exc:
                return finish(
                    None, f"multiple_tool_calls_in_extraction_turn: {exc}")
            if tool_input is None:
                last_error = "no_tool_call_in_extraction_turn"
            else:
                try:
                    view = make_view_from_tool_input(candidate.id, tool_input)
                    _record_findings(call_id, tool_input, conn)
                    return finish(view, None)
                except (KeyError, TypeError, ValueError) as exc:
                    last_error = f"invalid_view: {type(exc).__name__}: {exc}"

        if attempt == "first":
            echo = _assistant_echo(turn)
            if echo is not None:
                messages.append(echo)
            messages.append({"role": "user", "content": (
                "Your submission was not accepted: "
                f"{last_error}. Submit again via submit_research_view "
                "with EVERY required field present and complete.")})
    return finish(None, last_error or "extraction_failed")


class AmbiguousExtraction(ValueError):
    """More than one submit_research_view block in one response."""


def _extract_tool_input(response: dict) -> dict | None:
    """The forced extraction turn's single tool call, or None.

    Defensive about shape: a content field that is not a list of block
    objects used to raise AttributeError from inside investigate(),
    after the call was billed and before the research_calls row was
    written - a paid call that vanished from the audit trail
    (stress-tester defect 11).

    Two submit_research_view blocks are AMBIGUOUS, not first-wins: a
    model that retracts its view in the second block would otherwise be
    traded on the first (defect 12)."""
    content = response.get("content")
    if not isinstance(content, list):
        return None
    found = [block.get("input") for block in content
             if isinstance(block, dict)
             and block.get("type") == "tool_use"
             and block.get("name") == "submit_research_view"]
    if len(found) > 1:
        raise AmbiguousExtraction(
            f"{len(found)} submit_research_view blocks in one response")
    return found[0] if found else None


def _persist(log: ResearchCallLog, conn) -> None:
    conn.execute(
        """INSERT INTO research_calls
           (id, candidate_id, model, prompt_rendered, tools_offered,
            cost_cents, latency_ms, skipped_reason, called_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (log.id, log.candidate_id, log.model, log.prompt_rendered,
         json.dumps(list(log.tools_offered)), str(log.cost_cents),
         log.latency_ms, log.skipped_reason,
         datetime.now(timezone.utc).isoformat()))
    for t in log.api_turns:
        conn.execute(
            """INSERT INTO research_call_turns
               (call_id, turn_index, raw_response, usage_raw, stop_reason)
               VALUES (?,?,?,?,?)""",
            (log.id, t.turn_index, json.dumps(t.raw_response),
             json.dumps(t.usage.raw if t.usage else None), t.stop_reason))
    if log.parsed_view is not None:
        v = log.parsed_view
        conn.execute(
            """INSERT OR REPLACE INTO research_views
               (candidate_id, direction, conviction, thesis, invalidation,
                expected_holding_days, priced_in, priced_in_reasoning)
               VALUES (?,?,?,?,?,?,?,?)""",
            (v.candidate_id, v.direction, v.conviction, v.thesis,
             v.invalidation, v.expected_holding_days, int(v.priced_in),
             v.priced_in_reasoning))
    conn.commit()
