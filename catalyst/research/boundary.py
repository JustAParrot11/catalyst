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
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Literal

from catalyst.cost import CostEstimate
from catalyst.cost.governor import authorize
from catalyst.cost.pricing import UnknownModelError
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

# Pre-call estimates, deliberately pessimistic (the governor compares
# against these BEFORE the call; an optimistic estimate is a hole in the
# cap). Basis: MEASURED, not assumed (cost-audit F4 - the original 8c/5c
# "pessimistic" figures were 58% under reality). The first live research
# call (2026-08-10, fixture in tests/test_cost_api_adapter.py) cost
# 12.63c for exploration: 24k tokens in (web search results are large),
# 2.3k out, 2 searches at 1c each. 15c covers that with a third search;
# extraction measured ~1.3c, 8c leaves headroom for a repair turn.
EXPLORATION_TURN_ESTIMATE_CENTS = Decimal("15")
EXTRACTION_TURN_ESTIMATE_CENTS = Decimal("8")


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
def invalid_payload_reason(payload: dict) -> str | None:
    """Why the Messages API would reject this, or None if it looks sound.

    Deliberately conservative: it rejects only shapes that are certainly
    invalid. A false positive here silently skips a candidate that would
    have worked, which is worse than the 400 it is trying to prevent.
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
    if messages[-1].get("role") == "assistant":
        # A trailing assistant message asks the API to CONTINUE it, which
        # is valid but never what this loop intends - it means an echo
        # was appended without the follow-up user turn, and the model
        # would be prompted with nothing to answer.
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
    tools = list(prompts.exploration_tools(
        prompts.searches_for(candidate, signals))) + [SUBMIT_RESEARCH_VIEW_TOOL]
    tools_offered = tuple(t.get("name", t.get("type", "?")) for t in tools)

    turns: list[APITurn] = []
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

    def run_turn(payload: dict) -> APITurn | None:
        """validate -> authorize -> call -> record -> price. None = stop."""
        nonlocal cost_cents
        # BEFORE SPENDING ANYTHING. A payload the API will certainly
        # reject must not consume a paid call and must not come back as
        # a status code the owner has to guess at - it names itself, in
        # the funnel, in English.
        bad = invalid_payload_reason(payload)
        if bad is not None:
            transport_errors.append(f"invalid_request_not_sent: {bad}")
            return None
        forced = (payload.get("tool_choice") or {}).get("type") == "tool"
        estimate = CostEstimate(
            estimated_cents=(EXTRACTION_TURN_ESTIMATE_CENTS if forced
                             else EXPLORATION_TURN_ESTIMATE_CENTS),
            basis="pre-registered per-turn pessimistic estimate (boundary.py)",
            kind=cost_context.kind, component="research")
        decision = authorize(estimate, conn,
                             cost_context.governor_profit_share,
                             cycle_id=cost_context.cycle_id,
                             owner_monthly_cap_cents=(
                                 cost_context.owner_monthly_cap_cents))
        if not decision.authorized:
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
        "model": model, "max_tokens": 2048,
        "messages": messages, "tools": tools,
        "tool_choice": {"type": "auto"},
    })
    if turn is None:
        return finish(None, transport_errors[0] if transport_errors
                      else "budget_denied")
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
            "model": model, "max_tokens": 2048,
            "messages": messages, "tools": tools,
            "tool_choice": {"type": "auto"},
        })
        if turn is None:
            return finish(None, transport_errors[0] if transport_errors
                          else "budget_denied")
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
        except (KeyError, TypeError, ValueError):
            pass          # fall through to the forced extraction turn

    echo = _assistant_echo(turn)
    if echo is not None:
        messages.append(echo)
    messages.append({"role": "user", "content": (
        "Submit your conclusion now via submit_research_view.")})

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
            "model": model, "max_tokens": 2048,
            "messages": messages,
            "tools": [SUBMIT_RESEARCH_VIEW_TOOL],
            "tool_choice": {"type": "tool", "name": "submit_research_view"},
        })
        if turn is None:
            return finish(None, transport_errors[0] if transport_errors
                          else "budget_denied")
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
