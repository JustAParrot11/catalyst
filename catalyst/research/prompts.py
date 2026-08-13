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


def _signals_block(signals: list) -> str:
    """What each feed independently said about this ticker.

    THE POINT OF SHOWING THIS. Until now the model saw one candidate and
    one kind of evidence, and any agreement between feeds existed only
    in the grouping code that assembled the candidate - never in the
    model's reasoning. It could not weigh a link it was never told
    about. This block is what lets it say "the analyst raised a target
    two days after the offering was filed, which is odd", which is the
    kind of connection the owner asked for.

    Dates are given because the ORDER matters and the model cannot see
    it otherwise: a downgrade before a readout means something different
    from a downgrade after one.
    """
    lines = ["WHAT EACH FEED SAID, INDEPENDENTLY",
             "These arrived from separate sources. They were not written "
             "with each other in mind, and nothing has judged them yet:"]
    for sig in signals:
        detail = sig.detail or {}
        when = sig.when.isoformat() if sig.when else "undated"
        what = (detail.get("headline")
                or detail.get("matched_phrase")
                or detail.get("catalyst_type") or "")
        hint = detail.get("direction_hint")
        tone = ("" if not hint else
                "  [pattern-matched as GOOD for the equity]" if hint > 0 else
                "  [pattern-matched as BAD for the equity]")
        lines.append(f"  - {when}  ({sig.source}) {str(what)[:180]}{tone}")
    lines.append(
        "The GOOD/BAD tags above are a crude keyword match done by code, "
        "not a judgement. Disagree with them freely - saying one is wrong "
        "is useful.")
    return "\n".join(lines)


def render_research_prompt(candidate: Candidate,
                           graph_context: str | None = None,
                           signals: list | None = None) -> str:
    searches = searches_for(candidate, signals)
    sections: list[str] = []
    sections.append(
        "You are the research step of an automated trading system, judging "
        "ONE candidate. Your answer is advisory only: deterministic code "
        "decides what, if anything, happens next. Do not say how much to "
        "trade, or name order types, entries, stops or exits — none of "
        "that is yours to decide."
    )
    if signals:
        # A CONJUNCTION IS A DIFFERENT QUESTION, so it gets a different
        # brief. The insider-cluster framing below asks "is this cluster
        # already priced in"; that is the wrong question for a ticker
        # surfaced because two unrelated feeds agreed.
        kinds = sorted({s.catalyst_type for s in signals})
        feeds = sorted({s.source for s in signals})
        sections.append(
            "WHY THIS ONE\n"
            f"{candidate.ticker} was surfaced because {len(kinds)} "
            f"unrelated kinds of evidence, from {len(feeds)} independent "
            "feeds, landed on it in the same window. Nothing has judged "
            "whether that means anything - that is what you are for.\n\n"
            "Your question is: DO THESE CONNECT? Say plainly if they do "
            "not. Two things happening at once is also what coincidence "
            "looks like, and with thousands of tickers some pair up by "
            "chance every week. A confident no_trade on a coincidence is "
            "worth more than a thesis stretched to fit."
        )
        sections.append(_signals_block(signals))
        sections.append(
            "CANDIDATE\n"
            f"Ticker: {candidate.ticker}\n"
            f"Sector (SIC): {candidate.sector}\n"
            f"Grouped as: {candidate.catalyst_type}\n"
            f"Newest signal: {candidate.catalyst_date.isoformat()}\n"
            "NOTE: that is when the newest piece of evidence LANDED, not "
            "a resolution date. Nothing here has read the body of the "
            "filings, so if the timing matters to your thesis, check it."
        )
    else:
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
        f"- You may use web_search at most {searches} times. Each search "
        "costs real money; search only when the result could change your "
        "answer.\n"
        "- Report judgements, not instructions: nothing about how much to "
        "trade, and no order, entry, stop or exit levels.\n"
        "- Submit your conclusion via the submit_research_view tool once "
        "you have searched as much as you need to; its fields match the "
        "six answers above. Do not wait to be asked."
    )
    return "\n\n".join(sections)


#: Searches for an ordinary candidate. One feed said one thing; the
#: question is narrow and more searching does not sharpen it.
BASE_SEARCHES = 3
#: Searches for a CONJUNCTION - two or more independent feeds agreeing.
#: The question is genuinely open ("do these connect?") and the answer
#: lives in reporting the feeds do not carry, so this is the one place
#: where more searching plausibly changes the answer.
#:
#: THE ARITHMETIC, because the budget is small and near-fixed. Measured
#: live 2026-08-11: 48 cross-feed conjunctions in three weeks, capped at
#: 12 candidates a pass. At the extra 7 searches below that is $0.07 a
#: candidate, so even 30 conjunctions a month is ~$2.10 of search on top
#: of tokens. The free structured feeds do the filtering; the paid model
#: pass only fires where two unrelated sources already agree.
CONJUNCTION_SEARCHES = 10


def searches_for(candidate=None, signals=None) -> int:
    """How many searches this candidate has EARNED.

    Evidence buys budget, never hope. A candidate is given the larger
    allowance only when independent feeds already agree about it - which
    is measured, free, and computed before any model call.
    """
    if signals and len({getattr(s, "source", "") for s in signals}) > 1:
        return CONJUNCTION_SEARCHES
    return BASE_SEARCHES


def exploration_tools(max_searches: int = BASE_SEARCHES) -> list[dict]:
    """Tools available during exploration turns.

    Server-side web search only. COST: $10 per 1,000 searches (TRAPS.md)
    = $0.01 per search on top of tokens.

    `max_uses` is per REQUEST, not per investigation - this docstring
    claimed otherwise and the claim was wrong. A pause_turn continuation
    is a new request, so re-sending this list verbatim refills the
    allowance. boundary._tools_with_remaining_searches() subtracts what
    has already been billed, which is what makes the budget mean one
    investigation. The per-turn governor authorization is the hard gate
    above both.
    """
    return [{"type": "web_search_20250305", "name": "web_search",
             "max_uses": int(max_searches)}]
