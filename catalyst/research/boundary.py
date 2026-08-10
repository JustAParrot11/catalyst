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
# cap). Basis: sonnet pricing at $3/M in, $15/M out; a research prompt
# runs ~3k tokens in / ~1.5k out => ~3.2c; up to 3 web searches at $10
# per 1,000 adds 3c; extraction is prompt + forced tool json, no search.
EXPLORATION_TURN_ESTIMATE_CENTS = Decimal("8")
EXTRACTION_TURN_ESTIMATE_CENTS = Decimal("5")


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


def investigate(
    candidate: Candidate,
    cost_context: CostContext,
    transport: Transport,
    model: str = RESEARCH_MODEL,
    graph_context: str | None = None,
) -> ResearchCallLog:
    call_id = str(uuid.uuid4())
    conn = cost_context.conn
    started = time.monotonic()
    prompt = prompts.render_research_prompt(candidate, graph_context=graph_context)
    tools = prompts.exploration_tools()
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
        """authorize -> call -> record -> price. None = budget denied."""
        nonlocal cost_cents
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
        messages.append({"role": "assistant",
                         "content": turn.raw_response.get("content", [])})
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

    messages.append({"role": "assistant",
                     "content": turn.raw_response.get("content", [])})
    messages.append({"role": "user", "content": (
        "Submit your conclusion now via submit_research_view.")})

    # ---- extraction: exactly one turn, tool_choice FORCED. The model
    # cannot answer in prose; the only way out is the schema.
    turn = run_turn({
        "model": model, "max_tokens": 1024,
        "messages": messages,
        "tools": [SUBMIT_RESEARCH_VIEW_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_research_view"},
    })
    if turn is None:
        return finish(None, transport_errors[0] if transport_errors
                      else "budget_denied")
    if unpriced:
        return finish(None, f"usage_unpriced_governor_blocked: {unpriced[0]}")

    try:
        tool_input = _extract_tool_input(turn.raw_response)
    except AmbiguousExtraction as exc:
        return finish(None, f"multiple_tool_calls_in_extraction_turn: {exc}")
    if tool_input is None:
        return finish(None, "no_tool_call_in_extraction_turn")
    try:
        view = make_view_from_tool_input(candidate.id, tool_input)
    except (KeyError, TypeError, ValueError) as exc:
        return finish(None, f"invalid_view: {type(exc).__name__}: {exc}")
    return finish(view, None)


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
