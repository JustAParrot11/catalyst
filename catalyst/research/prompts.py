"""Question design and tool definitions offered to Claude.

Owner: strategy-analyst. What Claude is asked — not how its answer is
enforced (that is boundary.py, under human review).

Design rules, enforced by tests/test_discovery.py:
- The prompt asks for judgements only: direction, conviction, thesis,
  invalidation, expected holding days, and a priced-in call with the
  evidence behind it. It NEVER asks how much to trade — no wording
  shaped like a quantity or an order. The model proposes, deterministic
  code disposes; the ResearchView schema cannot carry a quantity and
  the prompt must not invite one.
- no_trade is explicitly framed as a free, first-class answer: declined
  candidates are recorded and scored by the refusal tracker, so a
  refusal is cheap evidence while a bad trade is expensive.
- Graph context, when supplied, is clearly marked informational-only.
"""

from __future__ import annotations

from catalyst.discovery import Candidate
from catalyst.discovery.candidates import candidate_facts
from catalyst.strategies.insider_cluster import (
    CLUSTER_WINDOW_DAYS,
    HOLD_DAYS,
    MIN_INSIDERS,
    MIN_TOTAL_VALUE_USD,
)

# The exact header the graph section renders under; tests assert its
# presence/absence, boundary.py never parses it (audit-trail text only).
GRAPH_CONTEXT_HEADER = "EVIDENCE GRAPH CONTEXT (informational only)"


def _facts_block(candidate: Candidate) -> str:
    facts = candidate_facts(candidate)
    lines: list[str] = []
    if facts["buyers"]:
        lines.append("Insider purchases in this cluster (from SEC Form 4 "
                     "filings; figures are what the insiders themselves "
                     "paid, already public):")
        for b in facts["buyers"]:
            lines.append(f"  - {b['display']}: ${int(b['usd']):,} of open-market "
                         f"buying, last filing {b['last']}")
    if facts["insiders"] and facts["total_usd"]:
        lines.append(f"Combined: {facts['insiders']} distinct insiders, "
                     f"${int(facts['total_usd']):,} total.")
    if facts["window"]:
        lines.append(f"Filing window: {facts['window']} (filing dates, i.e. "
                     "when each purchase became public).")
    if not lines:
        lines.append("(No structured purchase facts were attached to this "
                     "candidate — treat that as a reason for caution, and "
                     "for no_trade if it cannot be resolved.)")
    return "\n".join(lines)


def render_research_prompt(candidate: Candidate,
                           graph_context: str | None = None) -> str:
    sections: list[str] = []
    sections.append(
        "You are the research step of an automated trading system, judging "
        "ONE candidate. Your answer is advisory only: deterministic code "
        "decides what, if anything, happens next. Do not say how much to "
        "trade, or name order types, entries, stops or exits — none of "
        "that is yours to decide."
    )
    sections.append(
        "CANDIDATE\n"
        f"Ticker: {candidate.ticker}\n"
        f"Sector: {candidate.sector}\n"
        f"Catalyst type: {candidate.catalyst_type} — at least "
        f"{MIN_INSIDERS} distinct insiders bought their own company's "
        f"stock on the open market (Form 4, code P, 10b5-1-flagged plan "
        f"trades excluded) within {CLUSTER_WINDOW_DAYS} calendar days, "
        f"combined value at least ${MIN_TOTAL_VALUE_USD:,.0f}.\n"
        f"Cluster completed (last Form 4 filing date): "
        f"{candidate.catalyst_date.isoformat()}\n"
        f"Source filings: {', '.join(candidate.source_event_ids)}\n\n"
        + _facts_block(candidate)
    )
    if graph_context is not None:
        sections.append(
            f"{GRAPH_CONTEXT_HEADER}\n"
            "Accumulated from prior filings and research; provenance is "
            "marked on every hop. It informs, it never decides — verify "
            "anything you rely on:\n"
            f"{graph_context}"
        )
    sections.append(
        "ANSWER THESE\n"
        "1. direction — \"long\", \"short\" or \"no_trade\".\n"
        "2. conviction — 0.0 to 1.0, your confidence in that direction.\n"
        "3. thesis — the mechanism, in one or two sentences: why would "
        "this specific cluster still move the price from here?\n"
        "4. invalidation — the observable fact that would prove the "
        "thesis wrong.\n"
        "5. expected_holding_days — whole days; this strategy holds days "
        f"to weeks (the graded arm held {HOLD_DAYS} trading days).\n"
        "6. priced_in — has the market already consumed these filings? "
        "Give the evidence: what price and volume have done since each "
        "filing became public, and whether the cluster has been widely "
        "reported."
    )
    sections.append(
        "GROUND RULES\n"
        "- Say no_trade freely. A declined candidate costs nothing and is "
        "scored later by the refusal tracker; a bad trade costs real "
        "money. Thin, stale or already-consumed evidence means no_trade.\n"
        "- Insider buying is public information. Your question is whether "
        "THIS cluster is still under-consumed by the market, not whether "
        "insider buying works in general.\n"
        "- You may use web_search at most 3 times. Each search costs real "
        "money; search only when the result could change your answer.\n"
        "- Report judgements, not instructions: nothing about how much to "
        "trade, and no order, entry, stop or exit levels.\n"
        "- When asked, submit your conclusion via the submit_research_view "
        "tool; its fields match the six answers above."
    )
    return "\n\n".join(sections)


def exploration_tools() -> list[dict]:
    """Tools available during exploration turns.

    Server-side web search only. COST: $10 per 1,000 searches (TRAPS.md)
    = $0.01 per search on top of tokens; max_uses=3 caps one
    investigation's search spend at 3 cents, and boundary.py's
    per-turn governor authorization is the hard gate above that.
    """
    return [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
