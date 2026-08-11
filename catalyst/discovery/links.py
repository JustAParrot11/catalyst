"""Conjunctions: the same ticker showing up in two unrelated places.

THE IDEA. One signal is a fact. Two independent signals landing on the
same ticker inside the same few weeks is a different object entirely,
and it is the thing a person scanning one feed at a time will not see.
The owner asked for exactly this: "i want to make some complex links
that may not be obvious e.g. it found company x is expected to show Q4
reports and they are looking promising".

    insiders bought          (Form 4 cluster)
  + earnings call scheduled  (8-K, "conference call to discuss")
  = officers put their own money in weeks before the print

Neither half is news. Together they are a dated, directional, testable
claim - and both halves came from free, keyless, structured feeds, so
the whole thing costs nothing per item. That matters more than it
sounds: the build brief prices breadth from web search at $0.01 a query
forever, against a $5/month cap.

WHAT THIS MODULE IS NOT. It does not decide anything. It groups
evidence, says in English what the combination is, and hands it on.
Whether a link is worth researching is discovery's call; whether it is
worth trading is the risk engine's, and the model never sizes anything
(CLAUDE.md, the one rule that is not negotiable).

WHY CONJUNCTION IS NOT AUTOMATICALLY EDGE. Two things happening at once
is also what coincidence looks like, and with thousands of tickers some
will pair up by chance every week. So:

  * a link carries the count of DISTINCT sources, not a score the model
    invented;
  * the plain-English sentence names the evidence rather than asserting
    a conclusion, because "insiders bought before earnings" is an
    observation and "this will go up" is not something evidence alone
    can say;
  * and the refusal tracker scores what declined links went on to do, so
    "is a conjunction worth anything?" becomes a number rather than an
    argument. Until that sample exists this module is a hypothesis with
    plumbing, and the dashboard should say so.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

#: What a catalyst type MEANS, as a clause that can be joined into a
#: sentence. Keyed by the catalyst_type each source stamps on its
#: events. An unknown type falls back to its own name so a new source
#: appears in the English rather than silently reading as nothing.
CLAUSES = {
    "insider_cluster": "several insiders bought on the open market",
    "earnings": "an earnings call has been scheduled",
    "fda_decision": "an FDA decision date has been disclosed",
    "clinical_readout": "a trial readout is expected",
    "merger_vote": "a shareholder vote on a merger is scheduled",
    "merger": "a merger agreement has been signed",
    "guidance": "guidance was raised",
}

#: Pairs worth naming explicitly, because the combination says something
#: neither half does. Everything else still forms a link; it just gets
#: the generic sentence. Keys are sorted tuples so lookup is orderless.
NOTABLE = {
    ("earnings", "insider_cluster"):
        "officers bought their own stock with an earnings date already "
        "on the calendar - they were buying into a print they could see "
        "coming",
    ("fda_decision", "insider_cluster"):
        "insiders bought ahead of a disclosed FDA decision date, which "
        "is the highest-conviction and highest-risk pairing here: the "
        "outcome is binary and the gap risk is real",
    ("clinical_readout", "insider_cluster"):
        "insiders bought while a trial readout was pending",
    ("guidance", "insider_cluster"):
        "the company raised guidance and insiders bought as well - the "
        "statement and the behaviour agree",
    ("earnings", "guidance"):
        "guidance moved and an earnings call is scheduled, so the "
        "revision gets tested on a known date",
    ("merger", "merger_vote"):
        "a merger was agreed and its shareholder vote is scheduled - "
        "the outcome is close to known, so the move is usually already "
        "in the price",
}


@dataclass
class Signal:
    """One piece of evidence about one ticker."""

    source: str
    catalyst_type: str
    when: date | None
    source_id: str
    detail: dict = field(default_factory=dict)


@dataclass
class Link:
    ticker: str
    signals: list
    #: Distinct catalyst types, sorted. This is the honest measure of
    #: "how many unrelated things point here" - ten Form 4 rows from one
    #: cluster are ONE kind of evidence, not ten.
    kinds: tuple
    #: Distinct upstream feeds, sorted. Two kinds from ONE feed is a
    #: weaker claim than two kinds from two feeds.
    sources: tuple
    why: str
    first_seen: date | None
    last_seen: date | None
    sector: str = ""

    @property
    def n_kinds(self) -> int:
        return len(self.kinds)

    @property
    def is_conjunction(self) -> bool:
        return len(self.kinds) > 1


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def signals_from_events(events) -> dict:
    """RawEvents -> {ticker: [Signal, ...]}.

    Sources disagree about where the ticker lives, so this reads the
    documented key per source rather than guessing across the payload.
    An event with no ticker is skipped: it cannot be linked to anything,
    and carrying it would inflate every count downstream.
    """
    by_ticker: dict = defaultdict(list)
    for ev in events or []:
        payload = getattr(ev, "payload_raw", None) or {}
        if not isinstance(payload, dict):
            continue
        ticker = str(payload.get("ticker") or payload.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        catalyst = str(payload.get("catalyst_type") or "").strip()
        if not catalyst:
            # The Form 4 feed predates the catalyst_type stamp and its
            # rows are, by construction, insider purchases.
            catalyst = ("insider_cluster"
                        if str(getattr(ev, "source", "")).startswith("edgar_form4")
                        else "unknown")
        when = (_as_date(payload.get("filed_date"))
                or _as_date(payload.get("filing_date"))
                or _as_date(payload.get("trans_date"))
                or _as_date(getattr(ev, "fetched_at", None)))
        by_ticker[ticker].append(Signal(
            source=str(getattr(ev, "source", "") or "unknown"),
            catalyst_type=catalyst,
            when=when,
            source_id=str(getattr(ev, "source_id", "") or ""),
            detail=payload,
        ))
    return dict(by_ticker)


def _clause(catalyst_type: str) -> str:
    return CLAUSES.get(catalyst_type, catalyst_type.replace("_", " "))


def explain(ticker: str, kinds: tuple, sources: tuple,
            first: date | None, last: date | None) -> str:
    """The link, as a sentence a non-developer can read.

    It names the evidence and stops. "Insiders bought before earnings"
    is an observation; "this will go up" is a conclusion, and evidence
    alone cannot support it. The model gets to form a view later, and
    deterministic code decides after that.
    """
    window = ""
    if first and last:
        span = (last - first).days
        window = (f" Both landed on {first}." if span == 0
                  else f" They landed {span} day(s) apart, {first} to {last}.")
    if len(kinds) == 1:
        return (f"{ticker}: {_clause(kinds[0])}. One kind of evidence only, "
                f"from {len(sources)} feed(s).{window}")
    # SAME FEED IS A WEAKER CLAIM, and saying so is the difference
    # between a link and a coincidence dressed up. Measured live
    # 2026-08-11: 20 of 30 conjunctions were clinical_readout +
    # fda_decision, both from EDGAR full-text search, because a biotech
    # 8-K routinely mentions a readout and a PDUFA date in the same
    # breath. That is one company saying one thing, not two independent
    # observations agreeing.
    corroboration = (
        f" Both came from the same feed ({sources[0]}), so this is one "
        "filing trail rather than two independent observations."
        if len(sources) == 1 else
        f" These came from {len(sources)} independent feeds, which is the "
        "stronger case.")
    notable = NOTABLE.get(tuple(sorted(kinds[:2])))
    joined = ", and ".join(_clause(k) for k in kinds)
    head = f"{ticker}: {joined}."
    if notable and len(kinds) == 2:
        return f"{head} In plain terms, {notable}.{window}{corroboration}"
    return (f"{head} That is {len(kinds)} unrelated kinds of evidence "
            f"pointing at the same company.{window}{corroboration}")


def find_links(events, *, min_kinds: int = 2) -> list:
    """Tickers carrying `min_kinds` or more DISTINCT kinds of evidence.

    min_kinds=1 returns everything, which is useful for the dashboard's
    "what did we see at all" view. The default of 2 is the point of the
    module: a conjunction.

    Ordering is deterministic - most kinds first, then most signals,
    then ticker - so the same input always produces the same page. A
    list that reshuffles between refreshes is one nobody trusts.
    """
    links: list = []
    for ticker, signals in signals_from_events(events).items():
        kinds = tuple(sorted({s.catalyst_type for s in signals}))
        if len(kinds) < min_kinds:
            continue
        sources = tuple(sorted({s.source for s in signals}))
        dates = sorted(s.when for s in signals if s.when)
        first = dates[0] if dates else None
        last = dates[-1] if dates else None
        sector = ""
        for s in signals:
            sector = str(s.detail.get("sic") or s.detail.get("sector") or "")
            if sector:
                break
        links.append(Link(
            ticker=ticker,
            signals=sorted(signals, key=lambda s: (s.when or date.min, s.source_id)),
            kinds=kinds, sources=sources,
            why=explain(ticker, kinds, sources, first, last),
            first_seen=first, last_seen=last, sector=sector,
        ))
    links.sort(key=lambda l: (-l.n_kinds, -len(l.signals), l.ticker))
    return links


def link_summary(links) -> dict:
    """Counts for the dashboard, including the ones that are zero.

    A funnel that shows only what survived cannot say whether the step
    is working, so the shape of what was rejected is reported too.
    """
    by_pair: dict = defaultdict(int)
    for link in links:
        for i, a in enumerate(link.kinds):
            for b in link.kinds[i + 1:]:
                by_pair[tuple(sorted((a, b)))] += 1
    # SECTOR CONCENTRATION IS THE FAILURE MODE THIS WHOLE IDEA INVITES.
    # The brief is blunt about it: "Four small-cap biotech binaries all
    # resolving the same fortnight is a single wager on biotech
    # sentiment, not four independent trades." A link finder run over
    # filings finds biotech, because biotech files about catalysts more
    # than anyone else - measured live, 20 of 30 conjunctions were one
    # pairing in SIC 2834. The risk engine enforces the limit; this
    # reports the shape so nobody has to discover it from a rejection.
    by_sector: dict = defaultdict(int)
    for link in links:
        if link.is_conjunction:
            by_sector[link.sector or "(no sector recorded)"] += 1
    n_conj = sum(1 for l in links if l.is_conjunction)
    top_sector, top_n = ("", 0)
    if by_sector:
        top_sector, top_n = max(by_sector.items(), key=lambda kv: kv[1])
    concentration = (top_n / n_conj) if n_conj else 0.0
    single_feed = sum(1 for l in links
                      if l.is_conjunction and len(l.sources) == 1)
    return {
        "tickers_with_a_conjunction": n_conj,
        "tickers_seen": len(links),
        "sector_concentration": round(concentration, 3),
        "largest_sector": top_sector,
        "largest_sector_n": top_n,
        "single_feed_conjunctions": single_feed,
        "warning": (
            f"{top_n} of {n_conj} conjunctions are the same sector "
            f"({top_sector}). These are ONE bet, not {top_n} - the risk "
            "engine's correlation limit will bind before most of them "
            "are ever sized."
            if n_conj and concentration >= 0.5 else ""),
        "pairs": sorted(
            ({"kinds": list(k), "n": v,
              "meaning": NOTABLE.get(k, "no established meaning for this "
                                        "pairing - treat it as coincidence "
                                        "until the refusal tracker says "
                                        "otherwise")}
             for k, v in by_pair.items()),
            key=lambda d: (-d["n"], d["kinds"])),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
