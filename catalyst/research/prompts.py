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

from datetime import datetime, timezone
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


#: What `MarketSnapshot.priced_off` means about the book being open.
#: Classified by the RULE rather than by a list of provenance strings
#: (house rule 7): the one value that means a live two-sided quote is
#: `live_nbbo`, and anything else - today's or a later session's cached
#: close - means the book was shut when the number was taken.
LIVE_PROVENANCE = "live_nbbo"


def market_is_live(market) -> bool | None:
    """True, False, or None when there is no snapshot to ask.

    None is NOT "closed". A missing snapshot means nobody looked, and
    telling the model the market is shut when nobody looked is the same
    class of mistake as telling it the spread is 1000%.
    """
    if market is None:
        return None
    priced_off = str(getattr(market, "priced_off", "") or "")
    if not priced_off:
        return None
    return priced_off == LIVE_PROVENANCE


def render_as_of_section(now: datetime | None = None, market=None,
                         candidate: Candidate | None = None) -> str:
    """WHAT DAY IT IS. The prompt did not say, and it was being asked to.

    OWNER-ASKED 2026-09-13: *"is the bot 100% aware of the active current
    date and time it is making these searches?"*

    IT WAS NOT, AND THE ANSWER IT WAS BEING ASKED FOR DEPENDS ON IT.
    Measured from the owner's own bundle - the verbatim prompt for
    candidate `conj-b3caf562223b246f8844` (CHYM, 2026-09-13T00:06) - the
    rendered text carried "2026-09-08", "2026-09-10" and "Newest signal:
    2026-09-10", and **nowhere said what today was**. Nor did the hunt
    prompt, whose one hard rule is that a nominated catalyst date must be
    "today or later"; nor did the position-review prompt, which asks for
    a check-in "number of days from today".

    So the model had to infer the current date from the evidence dates
    plus its own training cut-off, and then answer question 6 -
    "has the market already consumed these filings?" - which is
    ENTIRELY a question about how much time has passed. A filing two
    days old and a filing five weeks old get opposite answers, and the
    difference was not on the page.

    It is a frequent, quiet failure rather than a dramatic one: the
    model reasons fluently about "Sept 10" without knowing whether Sept
    10 was yesterday or last month, and nothing in the reply reveals
    which it assumed.

    THE TIME IS PASSED IN, never read from the clock here, so a rendered
    prompt is reproducible and a test can pin it (house rule 6). It
    falls back to the real clock because a prompt with no date is the
    defect being fixed - a caller that forgets must not silently
    reintroduce it.
    """
    now = now or datetime.now(timezone.utc)
    lines = ["RIGHT NOW",
             f"Today is {now.date().isoformat()}, a "
             f"{now.strftime('%A')}, and the time is "
             f"{now.strftime('%H:%M')} UTC. Every date in this prompt is "
             "a real calendar date; this one is today. Judge how stale a "
             "piece of evidence is against it rather than against your "
             "own sense of when 'now' is."]
    live = market_is_live(market)
    if live is True:
        lines.append(
            "The US equity market is OPEN, and the price below is a live "
            "quote taken moments ago.")
    elif live is False:
        lines.append(
            "The US equity market is SHUT right now. The price below is "
            "the newest CACHED DAILY CLOSE, not a live quote, and no "
            "order can be placed until the next open - so your view will "
            "be sized against the price at that open, not this one. If "
            "the stock gaps past its own normal daily range before then, "
            "code discards this view and asks again at the real price. "
            "Weekend and holiday evidence is still worth judging: EDGAR "
            "does not file, so what is new is news and what you find "
            "yourself.")
    if candidate is not None and candidate.catalyst_date is not None:
        days = (now.date() - candidate.catalyst_date).days
        lines.append(
            "The newest piece of evidence on this candidate is dated "
            f"{candidate.catalyst_date.isoformat()}, which is "
            + ("today" if days == 0 else
               f"{days} day(s) ago" if days > 0 else
               f"{-days} day(s) in the FUTURE")
            + ".")
    return "\n".join(lines)


def render_market_section(market, now: datetime | None = None) -> str:
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
    live = market_is_live(market)
    lines = ["MARKET DATA, measured at decision time (not from the model)"]
    last = getattr(market, "last_close", None)
    if last is not None and live is not False:
        lines.append(f"  - last close: ${last}")
    elif last is not None:
        # THE DATE OF THE CLOSE, AND WHERE IT CAME FROM. Owner-asked
        # 2026-09-13: *"i dont want it to read a price that may not be
        # live"*. This said only "newest cached daily close", so a close
        # from before a merger announcement read exactly like one from
        # Friday afternoon - measured, ACVA at $7.22 against a real
        # ~$10.43. Code now refuses a close older than the market has
        # plausibly been shut, and the model is told the date as well, so
        # it can judge staleness itself rather than trusting a guard it
        # cannot see.
        whence = ("it is Alpaca's own newest daily close"
                  if str(getattr(market, "priced_off", "")) ==
                  "broker_daily_close" else
                  "the broker could not be reached for a fresher figure, so "
                  "it is the newest close in this bot's local cache")
        as_of = getattr(market, "close_date", None)
        dated = ""
        if as_of is not None:
            dated = f", dated {as_of}"
            if now is not None:
                try:
                    days = (now.date() - as_of).days
                    dated += (" - that is today" if days == 0 else
                              f" - {days} day(s) before today")
                except (AttributeError, TypeError, ValueError):
                    pass
        lines.append(
            f"  - last close: ${last}{dated}. THIS IS NOT A LIVE QUOTE - "
            f"the market is shut and {whence}. If your own searching turns "
            "up a materially different price for this name, trust what you "
            "find and say so: it means something happened after this close "
            "and the figure above is behind.")
    # THE SPREAD WAS A REFUSING SENTINEL, RENDERED AS A MEASUREMENT.
    #
    # `build_closed_market_snapshot` sets `half_spread_bp = 100000`
    # deliberately: a closed book has no spread, and ZERO is the one
    # value that would sail through the owner's 20bp hard bound as the
    # tightest book ever measured. That is right for the risk engine,
    # which refuses the snapshot on `priced_off` anyway.
    #
    # IT WAS ALSO GOING STRAIGHT INTO THE PROMPT. Measured from the
    # owner's 2026-09-13 bundle, every weekend research call read:
    #
    #     - half-spread now: 100000 bp. This is what it costs to get in
    #       and out; a thesis worth less than the round trip is not a
    #       trade.
    #
    # A 1000% round trip kills every thesis that exists. The four
    # closed-market calls on record all came back `no_trade`, which is
    # not proof of causation - their theses argue coincidence - but the
    # prompt was stating a falsehood about the single number most likely
    # to end the conversation, under a heading claiming it was measured.
    #
    # So an unmeasurable spread is reported as unmeasurable. Silence is
    # not an option either: the model would fill it, and the round trip
    # genuinely is a real cost on the microcaps this screen surfaces
    # (BWFG measured 99.2bp half-spread and was refused).
    spread = getattr(market, "half_spread_bp", None)
    if spread is not None and live is not False:
        lines.append(
            f"  - half-spread now: {spread} bp. This is what it costs to "
            "get in and out; a thesis worth less than the round trip is "
            "not a trade.")
    elif spread is not None:
        lines.append(
            "  - half-spread: NOT MEASURABLE while the market is shut - "
            "there is no live book to read one from, so treat the round "
            "trip as unknown rather than as cheap or expensive. Code "
            "measures it at the open and refuses the entry outright if "
            "it is too wide, so do not try to guess the number; if this "
            "name is plausibly thin, say so in the thesis.")
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
                           record: str | None = None,
                           now: datetime | None = None) -> str:
    """`record` is the bot's own recent outcomes, rendered by
    research/record.py, or None when there is nothing to say yet.

    `now` is the decision time. It is rendered into the prompt - see
    `render_as_of_section` for the measurement that made that necessary -
    and defaults to the real clock so a caller that forgets gets a
    correct date rather than none.
    """
    searches = searches_for(candidate, signals)
    sections: list[str] = []
    sections.append(
        "You are the research step of an automated trading system, judging "
        "ONE candidate. Your answer is advisory only: deterministic code "
        "decides what, if anything, happens next. Do not say how much to "
        "trade, or name order types, entries, stops or exits — none of "
        "that is yours to decide."
    )
    # SECOND, not first. The opening paragraph is the stable standing
    # instruction; the clock goes immediately after it so it is read
    # before any dated evidence, and well before question 6 asks how
    # much of the move has already happened.
    sections.append(render_as_of_section(now, market, candidate))
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
    sections.append(render_market_section(market, now))
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
        # WHAT A REAL EDGE LOOKS LIKE ON THIS SCALE, AND WHOSE JOB THE
        # THRESHOLD IS.
        #
        # OWNER'S 2026-09-11 WINDOW, and the measurement that made this
        # necessary. Across all 293 research views on record:
        #
        #   268 no_trade   conviction median 0.68, 97 of them >= 0.80,
        #                  max 0.85
        #    25 directional     every single one between 0.30 and 0.62
        #
        # The two numbers are not the same quantity. A directional
        # conviction is a frequency over market outcomes, where being
        # right 57% of the time is a career; a no_trade conviction is
        # self-certainty about an abstention, which is nearly free to
        # feel strongly about. Sharing one name and one column, the
        # scale reads as though declining were the confident answer and
        # committing the weak one - so an honest 0.56 long looks like a
        # shrug next to a 0.85 no_trade, and 91.5% of calls went the
        # comfortable way.
        #
        # THE FLOOR IS STILL NOT NAMED, and must not be: telling the
        # model the bar teaches it to clear the bar. What is said here
        # is the project's own measured evidence for what the scale
        # means in this domain, plus the true fact that the threshold
        # decision is not the model's to make. Nothing here asks for a
        # higher number - it asks for the honest one, and removes the
        # reason to retreat from it.
        "- WHAT A REAL EDGE LOOKS LIKE HERE, because the scale is easy "
        "to misread in the abstract. Measured out of sample on this "
        "system's own backtest, the better-graded arm resolved its way "
        "57% of the time and the other managed 49%. Those are the only "
        "edges this project has ever actually demonstrated. A "
        "days-to-weeks equity direction is a hard problem, and a "
        "modest-looking frequency on it is not the same thing as no "
        "opinion - read your own number against what is achievable "
        "here, not against what certainty would feel like.\n"
        "- WHETHER A NUMBER IS BIG ENOUGH IS NOT YOUR DECISION. A "
        "deterministic threshold you cannot see reads your conviction "
        "and decides whether anything happens; code also decides the "
        "size and the stop. Your job is the honest frequency and the "
        "reasoning behind it. Do not convert a real but modest edge "
        "into no_trade because the number looks unimpressive - that "
        "discards the judgement and the measurement at once. Give the "
        "direction and the number, and let the threshold do its job.\n"
        # THE ACCOUNT CANNOT SHORT, AND HALF THE DIRECTIONAL VIEWS WERE
        # SHORTS. Owner's 2026-09-11 bundle: of four directional views
        # in a week, two were shorts (CASY 0.58, COO 0.60) that the risk
        # engine discarded as `short_unavailable_cash_account`. Both
        # theses were good work on the wrong question, and each cost a
        # paid research call.
        #
        # Said here rather than in the tool schema so it reads as a fact
        # about the account, not as pressure toward `long`: a short
        # thesis is still the right ANSWER, it is just recorded as
        # no_trade. Nothing about this makes a long more attractive.
        "- THIS IS A CASH, LONG-ONLY ACCOUNT. It cannot short, at any "
        "conviction. If your honest read is that this falls, answer "
        "no_trade and put the bearish case in the thesis - that is a "
        "correct and useful answer, and the refusal tracker scores it. "
        "What is wasted is spending your searches BUILDING a short "
        "case: the question worth your budget is whether there is a "
        "long here.\n"
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
#:
#: WAS 10, AND THE ARM SPENT IT WITHOUT EVER PRODUCING A VIEW.
#:
#: The reasoning for 10 was that the question is genuinely open ("do
#: these connect?") and the answer lives in reporting the feeds do not
#: carry, so this was the one place where more searching plausibly
#: changed the answer. It was a good argument. It has now been tested,
#: and the measurement disagrees.
#:
#: OWNER'S 7-DAY WINDOW, 2026-09-11: conjunctions took 33 of 69 paid
#: research calls and $9.15 of $15.70 - $0.277 a call against $0.178
#: for insider clusters and $0.189 for drift, the gap being these
#: searches and the results arriving as input tokens. Lifetime record
#: across 89 calls at the larger allowance: ZERO directional views.
#:
#: Zero for 89 is not a small sample for this question. If the arm
#: converted at the insider arm's measured rate (23 of 189, 12.2%), the
#: chance of seeing no view at all in 89 calls is about 1 in 80,000. So
#: the extra seven searches are not the missing ingredient, and the
#: docstring under searches_for already states the rule this violates:
#: evidence buys budget, never hope. The arm keeps its slot in the
#: rotation and can earn the larger allowance back by producing a view
#: at the same price as everything else.
CONJUNCTION_SEARCHES = BASE_SEARCHES


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
