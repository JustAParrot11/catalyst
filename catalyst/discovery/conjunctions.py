"""Candidates built from AGREEMENT between independent feeds.

Until now discovery had one shape: cluster insider purchases from Form 4
and call each cluster a candidate. That is a single feed asking a single
question, and it is why every candidate the bot has ever considered was
an insider cluster.

Two more feeds now exist - EDGAR full-text search and Alpaca news - and
between them they produce roughly 1,100 events over a three-week window.
Turning each into a candidate would flood research and the budget with
it: at ~$0.03 a research call that is $33 a pass.

SO THE FILTER IS THE FEATURE. A candidate is created only where TWO OR
MORE INDEPENDENT KINDS of evidence land on the same ticker in the same
window. Measured live 2026-08-11 that was 48 tickers out of 456, so the
filter does the expensive work for nothing - conjunction is free to
compute, and the model pass it earns is the only thing that costs money.

It is also the thing the owner actually asked for: "i want to make some
complex links that may not be obvious e.g. it found company x is
expected to show Q4 reports and they are looking promising". One signal
is a fact; two unrelated signals agreeing is the observation a person
scanning one feed at a time does not make.

WHAT THESE CANDIDATES ARE, HONESTLY. The feeds say a company has just
said something, and what kind of thing it is. They do NOT say the date
it resolves - that is written in the body of the filing, and extracting
it would need the document. So catalyst_date is the day the newest
signal landed and the confidence is always "estimated". These are
post-event-drift candidates, which the brief lists as a strategy in its
own right; they are not scheduled-catalyst candidates wearing a
disguise, and calling them that would put a confirmed date on a guess.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

from catalyst.discovery import Candidate
from catalyst.discovery.links import find_links

#: FORM 4 DOES PARTICIPATE, and must.
#:
#: An earlier version of this comment claimed Form 4 was excluded here,
#: and named a constant that build_conjunction_candidates never read -
#: so the documentation and the code disagreed, and the code was right.
#: Excluding it would be wrong: "insiders bought AND an earnings call is
#: scheduled" is the owner's own worked example, and it is only findable
#: because a Form 4 event can be one half of a cross-feed link.
#:
#: The real risk the old comment was reaching for is DOUBLE-COUNTING: a
#: ticker with a qualifying insider cluster AND a news story would
#: produce a Form 4 cluster candidate and a conjunction candidate, and
#: both would be researched at ~34c each. That is handled where it
#: belongs - at the merge in scheduler.build_candidates_all, by ticker -
#: not by blinding this builder to a feed it needs.
PARTICIPATING_SOURCES = ("edgar_form4", "edgar_fts", "alpaca_news")

#: How stale the newest signal may be. A conjunction whose most recent
#: half is three weeks old is not news about now.
MAX_SIGNAL_AGE_DAYS = 10

#: A hard ceiling on candidates per pass, whatever the evidence says.
#: The governor caps spend and would simply deny past the cap, but a
#: denial is a worse outcome than a bounded pass: it stops mid-list on
#: whatever happened to sort first. Ordering is by strength, so a cap
#: keeps the best.
MAX_CANDIDATES_PER_PASS = 12


#: SIC code -> a broad industry band. Deliberately coarse: the point is
#: "is this pass all one industry", not a taxonomy. Ranges are the SEC's
#: own major groups.
def sector_band(sic) -> str:
    try:
        n = int(str(sic).strip())
    except (TypeError, ValueError):
        return "unknown"
    if n < 1000:
        return "agriculture"
    if n < 1500:
        return "mining and energy"
    if n < 1800:
        return "construction"
    if 2000 <= n < 2100:
        return "food"
    if n in (2833, 2834, 2835, 2836) or n == 8731:
        return "pharma and biotech"
    if 2800 <= n < 2900:
        return "chemicals"
    if n < 4000:
        return "manufacturing"
    if n < 5000:
        return "transport and utilities"
    if n < 5200:
        return "wholesale"
    if n < 6000:
        return "retail"
    if n < 6800:
        return "financials"
    if n < 8000:
        return "services and technology"
    return "other"


#: Most candidates one industry may take in a single pass.
#:
#: MEASURED, NOT ASSUMED. On the live feeds 2026-08-11, 10 of 12
#: candidates were SIC 2834 - the owner's complaint, and a real cost:
#: research is paid for BEFORE the risk engine sees a candidate, so the
#: bot would pay to research ten correlated biotechs and then decline
#: most of them on the correlated-cluster bound. Capping here spends the
#: same money across ten industries instead of one.
#:
#: This is NOT the risk engine's correlation limit and does not replace
#: it. That bound governs money at risk and is a hard bound a human owns;
#: this one governs where research spend goes.
MAX_PER_SECTOR_PER_PASS = 3


def _hash_id(ticker: str, kinds, when: date) -> str:
    """Content hash, so the same conjunction on the same day is the same
    candidate across passes. Candidate ids are content hashes elsewhere
    in discovery for the same reason: INSERT OR IGNORE then makes a
    re-run idempotent rather than duplicating work already researched."""
    basis = f"conj|{ticker}|{'+'.join(sorted(kinds))}|{when.isoformat()}"
    return "conj-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def _primary_kind(kinds: tuple) -> str:
    """Which of several catalyst types names the candidate.

    The risk engine keys its adverse-gap and stop-width assumptions off
    catalyst_type, so this picks the one carrying the most downside, not
    the most exciting one. A company with both a pending FDA decision
    and an equity offering is priced by the FDA binary, because that is
    what can gap.
    """
    for kind in ("distress", "dilution", "fda_decision", "clinical_readout",
                 "merger", "merger_vote", "strategic_review", "restructuring",
                 "asset_deal", "financing", "contract_award", "earnings",
                 "earnings_result", "guidance", "buyback",
                 "leadership_change", "analyst_action", "insider_cluster"):
        if kind in kinds:
            return kind
    return sorted(kinds)[0]


def build_conjunction_candidates(
    raw_events: list,
    as_of: datetime,
    *,
    min_kinds: int = 2,
    max_candidates: int = MAX_CANDIDATES_PER_PASS,
) -> tuple[list, list]:
    """(candidates, dropped) from cross-feed agreement.

    `dropped` carries (ticker, reason) for everything considered and not
    kept, so the funnel can say WHY a conjunction did not become a
    candidate rather than the count simply being smaller.
    """
    cutoff = as_of.date()
    links = find_links(raw_events, min_kinds=min_kinds)
    candidates: list = []
    dropped: list = []

    for link in links:
        if not link.last_seen:
            dropped.append((link.ticker, "no dated signal on either side"))
            continue
        if link.last_seen > cutoff:
            # Point-in-time: evidence that had not landed at as_of is
            # invisible, exactly as in the Form 4 clusterer.
            dropped.append((link.ticker, "signal is dated after this pass"))
            continue
        age = (cutoff - link.last_seen).days
        if age > MAX_SIGNAL_AGE_DAYS:
            dropped.append((link.ticker,
                            f"newest signal is {age} days old, past the "
                            f"{MAX_SIGNAL_AGE_DAYS}-day window"))
            continue
        # A conjunction inside ONE feed is one company saying one thing
        # twice - a biotech 8-K routinely mentions a readout and a PDUFA
        # date in the same breath. Measured live, 30 of 30 conjunctions
        # were single-feed before news existed; that is not two
        # observations agreeing.
        if len(link.sources) < 2:
            dropped.append((link.ticker,
                            f"all evidence came from one feed "
                            f"({link.sources[0]}), so it is one filing "
                            "trail rather than independent agreement"))
            continue
        kinds = tuple(link.kinds)
        candidates.append(Candidate(
            id=_hash_id(link.ticker, kinds, link.last_seen),
            ticker=link.ticker,
            catalyst_type=_primary_kind(kinds),
            # NOT a resolution date - see the module docstring. The feeds
            # say something happened; the date it resolves is in the body
            # of the filing and nothing here has read that.
            catalyst_date=link.last_seen,
            catalyst_date_confidence="estimated",
            source_event_ids=tuple(s.source_id for s in link.signals),
            discovered_at=as_of,
            sector=link.sector or "",
            # The kinds themselves are correlation tags: four biotech
            # binaries resolving together are one bet, and the risk
            # engine's cluster bound needs to see that.
            correlation_tags=tuple(sorted(kinds)) + (
                (f"sic-{link.sector}",) if link.sector else ()),
        ))

    # Strongest first - most independent feeds, then most kinds - so any
    # cap keeps the best rather than the alphabetically luckiest.
    candidates.sort(key=lambda c: (-len(c.correlation_tags), c.ticker))

    # ROUND-ROBIN BY INDUSTRY. Taking the strongest twelve outright gave
    # ten biotechs, because biotech files more catalyst-shaped documents
    # than anyone else - that is a property of EDGAR, not a signal about
    # where the opportunities are. Each industry gets its first slot
    # before any industry gets its second, so a pass covering a chemical
    # company, a retailer and a miner beats one covering ten biotechs
    # even when the biotechs individually score higher.
    by_sector: dict = {}
    for cand in candidates:
        by_sector.setdefault(sector_band(cand.sector), []).append(cand)
    kept: list = []
    round_n = 0
    while len(kept) < max_candidates:
        took_any = False
        for band in sorted(by_sector):
            queue = by_sector[band]
            if round_n < min(len(queue), MAX_PER_SECTOR_PER_PASS):
                kept.append(queue[round_n])
                took_any = True
                if len(kept) >= max_candidates:
                    break
        if not took_any:
            break
        round_n += 1
    kept_ids = {c.id for c in kept}
    for extra in candidates:
        if extra.id in kept_ids:
            continue
        band = sector_band(extra.sector)
        taken = sum(1 for c in kept if sector_band(c.sector) == band)
        dropped.append((extra.ticker, (
            f"{band} already has {taken} candidate(s) this pass "
            f"(cap {MAX_PER_SECTOR_PER_PASS}) - research spend goes to "
            "another industry instead")
            if taken >= MAX_PER_SECTOR_PER_PASS else (
            f"past the {max_candidates}-candidate cap for one pass; "
            "weaker than those kept")))
    return kept, dropped


def default_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The window the new feeds are asked for. Wider than the news
    horizon on purpose: a filing from three weeks ago plus a news story
    from yesterday is exactly the kind of pairing worth seeing, and
    trimming the filing side would remove it."""
    end = now or datetime.now(timezone.utc)
    return end - timedelta(days=21), end


def merge_with_form4(form4_candidates: list, conjunction_candidates: list):
    """(kept, dropped) - one candidate per COMPANY per pass.

    A ticker with a qualifying insider cluster AND a news story produces
    a Form 4 cluster candidate and a conjunction candidate: different
    ids, same company. Both would be researched at ~34c each, spending
    two of the three research slots on one name.

    The Form 4 cluster wins. It is the graded strategy - line-for-line
    the backtest arm - and its events are already one half of the
    conjunction anyway, so nothing is lost by preferring it.

    A module-level function rather than a closure inside the scheduler,
    because a closure cannot be tested by running it, and "test the
    behaviour" is the only way this class of bug gets caught: the
    previous guard asserted on a CONSTANT that the code never read.
    """
    kept = list(form4_candidates)
    seen_ids = {c.id for c in kept}
    seen_tickers = {c.ticker for c in kept}
    dropped: list = []
    for cand in conjunction_candidates:
        if cand.ticker in seen_tickers:
            dropped.append((cand.ticker,
                            "already a Form 4 cluster candidate this pass; "
                            "researching both would pay twice for one "
                            "company"))
            continue
        if cand.id in seen_ids:
            continue
        seen_ids.add(cand.id)
        seen_tickers.add(cand.ticker)
        kept.append(cand)
    return kept, dropped
