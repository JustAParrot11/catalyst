"""Question design and tool definitions offered to Claude.

Owner: strategy-analyst. What Claude is asked — not how its answer is
enforced (that is boundary.py, money-critical).

Design rules, enforced by tests/test_discovery.py:
- The prompt asks for judgements only: direction, conviction, thesis,
  invalidation, expected holding days, and a priced-in call with the
  evidence behind it. It NEVER asks how much to trade — no wording
  shaped like a quantity or an order. The model proposes, deterministic
  code disposes; the ResearchView schema cannot carry a quantity and
  the prompt must not invite one.
- no_trade is a first-class answer that must be JUSTIFIED, not a free
  one. It used to be framed as costing nothing; measured over the graded
  window that is false - a filter refusing without skill loses to the
  index by more than not filtering at all - and the model was declining
  87% of candidates on a question it had no data to answer.
- The market snapshot is rendered INTO the prompt. The model is asked
  what price and volume have done; it is now told.
- Graph context, when supplied, is clearly marked informational-only.
"""

from __future__ import annotations

from decimal import Decimal

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
    tagged = False
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
        tagged = tagged or bool(tone)
    # Only explain the tags if any were actually emitted. The sentence
    # used to be unconditional, so a conjunction whose feeds carried no
    # direction_hint told the model to "disagree freely" with GOOD/BAD
    # tags that were not on the page - an instruction pointing at
    # nothing, paid for by the token.
    if tagged:
        lines.append(
            "The GOOD/BAD tags above are a crude keyword match done by "
            "code, not a judgement. Disagree with them freely - saying "
            "one is wrong is useful.")
    return "\n".join(lines)


def render_market_section(market) -> str:
    """The numbers the model is asked to reason about.

    IT WAS BEING ASKED TO JUDGE PRICE WITHOUT PRICE. Question 6 asks
    "what price and volume have done since each filing became public",
    and the rendered prompt carried none: ticker, SIC code and cluster
    facts, 2,184 characters, no market data of any kind. The snapshot
    already existed - cycle.py builds it immediately before the call and
    handed it only to the risk engine. On the owner's live day the model
    answered "already priced in" 26 times out of 30, which is what a
    question with no evidence attached gets answered.

    Owner, 2026-08-14: "the real value here is the bot reading the
    market and news and using the numbers and data as backing for it to
    make the ultimate call."
    """
    if market is None:
        # NEVER SILENTLY. A missing snapshot is a fact the model should
        # weigh, not a blank the model fills with an assumption.
        return ("MARKET DATA\nUnavailable for this candidate at decision "
                "time. Treat any claim about what the price has already "
                "done as unverified.")
    lines = ["MARKET DATA, measured at decision time (not from the model)"]
    last = getattr(market, "last_close", None)
    if last is not None:
        lines.append(f"  - last close: ${last}")
    spread = getattr(market, "half_spread_bp", None)
    if spread is not None:
        lines.append(
            f"  - half-spread now: {spread} bp. This is what it costs to "
            "get in and out; a thesis worth less than the round trip is "
            "not a trade.")
    # WHAT THE PRICE HAS ALREADY DONE. Question 6 below asks exactly
    # this and the block used to carry none of it, leaving a web search
    # as the only route to an answer the cached bars can state exactly.
    action = getattr(market, "price_action", None)
    if action is not None and getattr(action, "measured", False):
        if action.move_since_catalyst_pct is not None:
            lines.append(
                f"  - move since the catalyst date: "
                f"{action.move_since_catalyst_pct:+}% over "
                f"{action.sessions_since_catalyst} session(s). THIS IS THE "
                "EVIDENCE FOR WHETHER YOU ARE TOO LATE - a large move "
                "already made is what 'consumed' looks like; a flat tape "
                "after public evidence is the opposite.")
        if action.move_5d_pct is not None:
            lines.append(f"  - move over the last 5 sessions: "
                         f"{action.move_5d_pct:+}%")
        if action.move_20d_pct is not None:
            lines.append(f"  - move over the last 20 sessions: "
                         f"{action.move_20d_pct:+}%")
        if action.range_position_pct is not None:
            lines.append(
                f"  - position in its 52-week range: "
                f"{action.range_position_pct}% (0 = at the low, 100 = at "
                "the high)")
        if action.recent_volume_ratio is not None:
            lines.append(
                f"  - recent volume against its own median: "
                f"{action.recent_volume_ratio}x. Above 1 means the name is "
                "being traded more than usual, which is what the market "
                "noticing something looks like.")

    # VOLUME, OR AN HONEST SILENCE. This rendered "$0" for every
    # candidate because the field was never populated - telling the
    # model a $60bn company has no volume at all, under a heading
    # claiming it was measured, with a nudge attached saying thin names
    # are least likely to have been consumed. A wrong number pointing
    # the judgement in one direction is worse than no number.
    vol = getattr(action, "median_daily_dollar_volume", None) if action \
        else None
    if vol is None:
        vol = getattr(market, "median_daily_dollar_volume", None)
        if vol is not None and Decimal(str(vol)) <= 0:
            vol = None
    if vol is not None:
        lines.append(
            f"  - median daily dollar volume: ${int(vol):,}. Thin names "
            "move on little, and are also where a cluster is least "
            "likely to have been consumed already.")
    else:
        lines.append(
            "  - median daily dollar volume: NOT MEASURED for this "
            "candidate. Do not assume it is thin or liquid; if that "
            "matters to your thesis, find it.")
    return "\n".join(lines)


def _drift_facts(candidate: Candidate) -> dict:
    """The `fact:` tags live_drift_candidates puts on a drift candidate."""
    out: dict = {}
    for tag in candidate.correlation_tags or ():
        if isinstance(tag, str) and tag.startswith("fact:") and "=" in tag:
            key, _, value = tag[5:].partition("=")
            out[key] = value
    return out


def _drift_brief(candidate: Candidate) -> str:
    """The CANDIDATE section for the post-earnings-drift arm.

    Bake-off Candidate A: surprise is this quarter's net income against
    the same quarter a year ago, standardised by the company's own past
    seasonal differences (a SUE with no analysts), from the first-filed
    XBRL value. Graded 2016-2026 out of sample: n=84, 57.1% hit,
    +1.59% a trade, 8.8% max drawdown - the better of the two arms.
    """
    f = _drift_facts(candidate)
    sue = f.get("sue", "?")
    return (
        "CANDIDATE\n"
        f"Ticker: {candidate.ticker}\n"
        f"Sector: {candidate.sector}\n"
        "Catalyst type: earnings_drift — a POST-EARNINGS DRIFT screen. "
        f"{candidate.ticker} filed a {f.get('form', '10-Q/10-K')} on "
        f"{f.get('filed', candidate.catalyst_date.isoformat())} for the "
        f"quarter ending {f.get('period_end', '?')}, and the reported "
        f"quarterly net income was a {sue} standard-deviation surprise "
        "against its own year-ago quarter (standardised by the company's "
        "own past seasonal swings; first-filed XBRL, no analyst estimates "
        "involved). The screen passes surprises of +1.0 sd or more.\n"
        f"Source: {', '.join(candidate.source_event_ids)}\n\n"
        "WHAT THIS ARM TRADES. The graded finding is that after a large "
        "positive surprise the price keeps adjusting for roughly twelve "
        "trading days rather than all at once - the market under-reacts. "
        "The arm holds long for that window. On the 2016-2026 bake-off "
        "it was right 57% of the time out of sample with an 8.8% maximum "
        "drawdown, which makes it the better-graded of the two arms this "
        "system runs; it is not a proven edge, and your job is to say "
        "whether THIS instance is a clean example of it or a statistical "
        "artefact - a one-off gain, a restatement, an acquisition, a "
        "denominator effect, a quarter the market was already braced for.\n\n"
        "READ THE TAPE WITH THE NUMBER. The graded arm only took the "
        "trade when the price reaction since filing AGREED with the "
        "surprise. A stock that fell on a beat is the refusal case."
    )


def render_research_prompt(candidate: Candidate,
                           graph_context: str | None = None,
                           signals: list | None = None,
                           market=None,
                           record: str | None = None) -> str:
    """`record` is the bot's own recent outcomes, rendered by
    research/record.py, or None when there is nothing to say yet."""
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
    elif candidate.catalyst_type == "earnings_drift":
        # A DIFFERENT ARM, A DIFFERENT QUESTION. Without this branch a
        # drift candidate fell into the insider text below and was
        # described to the model as a cluster of insider purchases that
        # never happened - and then asked whether "these filings" were
        # priced in, which for a strategy that BUYS AFTER THE MOVE is
        # the wrong question with the wrong answer built in.
        sections.append(_drift_brief(candidate))
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
    sections.append(render_market_section(market))
    if record:
        sections.append(record)
    drift = candidate.catalyst_type == "earnings_drift"
    sections.append(
        "ANSWER THESE\n"
        "1. direction — \"long\", \"short\" or \"no_trade\".\n"
        # THE NUMBER THAT DECIDED EVERY TRADE AND WAS NEVER DEFINED.
        # "your confidence" is not a unit. Over the first 31 live views
        # every long landed between 0.30 and 0.45 against a floor of
        # 0.60 - not disagreement, just two different scales. Stated as
        # a frequency it becomes something the refusal tracker can
        # grade: score enough 0.6 calls and about six in ten should
        # have worked, or the number is wrong and by a measurable
        # amount.
        "2. conviction — 0.0 to 1.0, and read it as a FREQUENCY, not a "
        "feeling: out of many setups that looked like this one, how "
        "often would this call be right? 0.50 is a coin flip. 0.60 is "
        "six in ten. 0.75 is three in four. Above 0.85 should be rare. "
        "Below 0.50 on a direction is a contradiction — if you would be "
        "wrong more often than right, the answer is no_trade. Code "
        "reads this number and decides whether to trade, so give the "
        "honest figure: inflating it to force a trade and shading it "
        "down to look careful both break the only feedback loop this "
        "system has.\n"
        + ("3. thesis — the MECHANISM, in two to four sentences: how big "
           "the surprise was against the company's own history, how the "
           "price has reacted since the filing, and why the rest of the "
           "adjustment is still ahead rather than done. Name your "
           "figures — the reported number, the year-ago number, the move "
           "since filing. A thesis that would read the same for any beat "
           "at any company is not a thesis.\n"
           if drift else
           "3. thesis — the MECHANISM, in two to four sentences: what "
           "would move this price from here, why it has not moved already, "
           "and what these insiders plausibly knew that the market does "
           "not. Name your figures — who bought, how much, at what price "
           "against what recent range. A thesis that would read the same "
           "for any cluster in any company is not a thesis.\n")
        +
        "4. invalidation — the ONE observable fact that would prove the "
        "thesis wrong, checkable by someone who cannot ask you: a price "
        "level, a filing, a date, a number in a report. This text is "
        "re-read on every position review to decide whether to close "
        "early, so \"the thesis does not play out\" is useless there.\n"
        "5. expected_holding_days — whole days; this strategy holds days "
        f"to weeks (the graded arm held {HOLD_DAYS} trading days).\n"
        + ("6. priced_in — for THIS arm the question is narrower than it "
           "sounds. Post-earnings drift is the finding that the market "
           "under-reacts to a large surprise and keeps adjusting for "
           "weeks, so a stock that has already moved in the direction of "
           "the surprise is CONFIRMING the setup, not exhausting it. Say "
           "priced_in only if the move since filing is already larger "
           "than the surprise plausibly justifies, or the reaction went "
           "the OTHER way (the tape disagrees with the number - the "
           "graded arm refuses those). SAY WHICH FIGURES YOU USED.\n"
           if drift else
           "6. priced_in — has the market already consumed these filings? "
           "Use the MARKET DATA above and anything you find by searching: "
           "what price and volume have done since each filing became "
           "public, and whether the cluster has been widely reported. "
           "SAY WHICH EVIDENCE YOU USED. \"Probably priced in\" with no "
           "figure behind it is not an answer to this question, and a "
           "priced_in call you cannot support should be false.")
    )
    sections.append(
        "GROUND RULES\n"
        "- DECLINING IS NOT FREE, and this brief used to say it was. "
        "Measured over the graded window, a filter that refuses without "
        "skill costs more than not filtering at all: accepting every "
        "signal beat the index by 16.6 percentage points, refusing "
        "three quarters of them lost by 59.5. Refuse when the evidence "
        "says so and say why; do not refuse to be safe.\n"
        "- Thin, stale or genuinely consumed evidence still means "
        "no_trade, and a no_trade you can justify is a good answer.\n"
        "- Insider buying is public information. Your question is whether "
        "THIS cluster is still under-consumed by the market, not whether "
        "insider buying works in general.\n"
        f"- You have web_search, up to {searches} times, and searching "
        "is the job rather than an overhead: this system exists to link "
        "what is being said publicly to an opportunity in a filing. Use "
        "them where they could find or kill an opportunity. Unused "
        "searches are not a saving if the answer is a guess.\n"
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
