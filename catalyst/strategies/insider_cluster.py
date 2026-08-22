"""Candidate C — insider-cluster open-market buying.

Pre-registered design (docs/STRATEGY-PROPOSALS.md, Candidate C):
- Signal: open-market purchases (Form 4, TRANS_CODE='P') by >= 2
  DISTINCT insiders of the same issuer within a 10-day window.
- Exclude 10b5-1 plan trades where the flag exists (AFF10B5ONE; the
  flag only exists from ~2023 — earlier rows have it blank, which the
  filter treats as "not flagged". Direction of bias: some pre-2023
  plan trades leak in, adding NOISE, not look-ahead).
- Tradable date = FILING_DATE (never TRANS_DATE; Form 4 is due 2
  business days after the trade). Entry is the next session's open
  after the cluster completes — always strictly after the filings
  were public.
- Hold 10-15 trading days (12 used, fixed before grading).

Parameters fixed BEFORE grading:
- CLUSTER_WINDOW_DAYS = 10 (calendar), MIN_INSIDERS = 2,
  MIN_TOTAL_VALUE_USD = 50_000, one event per issuer per 20 calendar
  days (overlapping clusters collapse into the first).
- Liquidity floor applied AT SIGNAL TIME from the PointInTimeView:
  last close >= $5 and median 20-session dollar volume >= $1M. The
  floor is a universe rule, identical in- and out-of-sample.

What could NOT be replayed and in which direction it biases:
- The proposal's exclusion filter (reject names with a pending
  scheduled binary) needs a historical catalyst calendar that does not
  exist point-in-time. Its absence leaves binary-event tail risk IN
  the sample — measured returns are worse/noisier than the designed
  strategy, not better.
- Ticker symbols come from the Form 4 itself; a symbol reused by a
  different company after a delisting would splice two price
  histories. Events whose symbol has no bars near the event date are
  skipped and counted.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median

from catalyst.backtest.data import PointInTimeView
from catalyst.discovery import Candidate
from catalyst.discovery.universe import is_tradeable
from catalyst.research.schema import ResearchView

CLUSTER_WINDOW_DAYS = 10
MIN_INSIDERS = 2
MIN_TOTAL_VALUE_USD = 50_000.0
DEDUPE_DAYS = 20
HOLD_DAYS = 12
MIN_PRICE = 5.0
MIN_MEDIAN_DOLLAR_VOL = 1_000_000.0
LIQ_SESSIONS = 20


@dataclass(frozen=True)
class ClusterEvent:
    symbol: str
    filing_date: date       # date the cluster completed (last filing in window)
    n_insiders: int
    total_value_usd: float


def _valid_symbol(sym: str) -> bool:
    return sym.isalpha() and sym.isascii() and 1 <= len(sym) <= 5


def build_cluster_events(purchases_csv: str | Path) -> list[ClusterEvent]:
    """Distill the purchase table into deduplicated cluster events."""
    by_issuer: dict[str, list[tuple[date, str, float, str]]] = {}
    with Path(purchases_csv).open(newline="") as f:
        for row in csv.DictReader(f):
            if row["aff10b5one"] in ("1", "true"):
                continue
            sym = row["symbol"].strip().upper()
            if not _valid_symbol(sym):
                continue
            # The SAME universe rule the live arm applies (ESCALATION-4).
            # It is here as well as there because this function is the
            # backtest arm, and an edge measured over a different set of
            # symbols than the one actually traded is not a measurement
            # of anything. On real Form 4 data it excludes nothing - no
            # fund has insiders - so the graded result is unchanged.
            if not is_tradeable(sym):
                continue
            try:
                fd = date.fromisoformat(row["filing_date"])
                val = float(row["value_usd"])
            except ValueError:
                continue
            by_issuer.setdefault(row["issuer_cik"], []).append(
                (fd, row["owner_cik"], val, sym))
    events: list[ClusterEvent] = []
    for _cik, rows in by_issuer.items():
        rows.sort(key=lambda r: r[0])
        last_event: date | None = None
        for i, (fd, _owner, _val, sym) in enumerate(rows):
            if last_event and (fd - last_event).days < DEDUPE_DAYS:
                continue
            window = [r for r in rows
                      if timedelta(0) <= fd - r[0] <= timedelta(days=CLUSTER_WINDOW_DAYS)]
            owners = {r[1] for r in window}
            total = sum(r[2] for r in window)
            if len(owners) >= MIN_INSIDERS and total >= MIN_TOTAL_VALUE_USD:
                events.append(ClusterEvent(
                    symbol=sym, filing_date=fd,
                    n_insiders=len(owners), total_value_usd=total))
                last_event = fd
    events.sort(key=lambda ev: ev.filing_date)
    return events


def write_events_csv(events: list[ClusterEvent], path: str | Path) -> None:
    with Path(path).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "filing_date", "n_insiders", "total_value_usd"])
        for ev in events:
            w.writerow([ev.symbol, ev.filing_date.isoformat(),
                        ev.n_insiders, f"{ev.total_value_usd:.2f}"])


def read_events_csv(path: str | Path) -> list[ClusterEvent]:
    out = []
    with Path(path).open(newline="") as f:
        for row in csv.DictReader(f):
            out.append(ClusterEvent(
                symbol=row["symbol"],
                filing_date=date.fromisoformat(row["filing_date"]),
                n_insiders=int(row["n_insiders"]),
                total_value_usd=float(row["total_value_usd"])))
    return out


def load_sector_map(path: str | Path) -> dict[str, str]:
    """symbol -> SIC code, from a two-column CSV.

    WHY THE BACKTEST NEEDS THIS. correlation.py keys a concentration
    cluster on sector|catalyst_type|resolution_week. Form 4 payloads
    carry no sector, so every insider candidate used to key on
    "unknown", collapsing every same-week cluster into ONE key - and
    max_correlated_cluster_pct then capped a biotech, a bank and a miner
    as a single bet.

    Measured in backtest/harness.py against SPY, out of sample: the
    cluster bound alone moved excess return from +10.4% to -20.1%, and
    did it by excluding the weeks several clusters complete at once,
    which is when the signal is strongest.

    A MISSING FILE RETURNS AN EMPTY MAP, which reproduces the old
    behaviour exactly. That is deliberate: the graded arm must be able
    to run precisely as it did before, so the two can be compared.
    """
    out: dict[str, str] = {}
    fp = Path(path)
    if not fp.exists():
        return out
    with fp.open(newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0] and row[0] != "symbol":
                out[row[0].strip().upper()] = row[1].strip()
    return out


def build_candidates(events: list[ClusterEvent],
                     sectors: dict[str, str] | None = None,
                     ) -> tuple[list[Candidate], dict]:
    cands: list[Candidate] = []
    table: dict[str, ClusterEvent] = {}
    for k, ev in enumerate(events):
        cid = f"C-{ev.filing_date.isoformat()}-{ev.symbol}-{k}"
        cands.append(Candidate(
            id=cid, ticker=ev.symbol, catalyst_type="insider_cluster",
            catalyst_date=ev.filing_date, catalyst_date_confidence="confirmed",
            source_event_ids=(f"form4_cluster:{ev.symbol}:{ev.filing_date}",),
            discovered_at=datetime(2016, 1, 1, tzinfo=timezone.utc),
            # A real SIC where we have one, "unknown" where we do not -
            # and "unknown" still clusters conservatively with the other
            # unknowns, exactly as every candidate did before.
            sector=(sectors or {}).get(ev.symbol.upper()) or "unknown",
            correlation_tags=("type:insider_cluster",),
        ))
        table[cid] = ev
    return cands, table


def make_signal_fn(table: dict, *, hold_days: int = HOLD_DAYS,
                   min_price: float = MIN_PRICE,
                   min_median_dollar_vol: float = MIN_MEDIAN_DOLLAR_VOL):
    def signal_fn(candidate: Candidate, view: PointInTimeView) -> ResearchView:
        ev = table.get(candidate.id)
        no = ResearchView(
            candidate_id=candidate.id, direction="no_trade", conviction=0.0,
            thesis="", invalidation="n/a", expected_holding_days=hold_days,
            priced_in=False, priced_in_reasoning="n/a")
        if ev is None:
            return no
        try:
            bars = view.bars(candidate.ticker)
        except KeyError:
            return no                       # no cached prices for this symbol
        if len(bars) < LIQ_SESSIONS:
            return no
        recent = bars[-LIQ_SESSIONS:]
        last_close = float(recent[-1].close)
        # Stale-price guard against symbol reuse/halts: the most recent bar
        # must be within 5 calendar days of the signal date.
        if (view.as_of - recent[-1].day).days > 5:
            return no
        if last_close < min_price:
            return no
        med_dv = median(float(b.close) * float(b.volume) for b in recent)
        if med_dv < min_median_dollar_vol:
            return no
        return ResearchView(
            candidate_id=candidate.id, direction="long", conviction=1.0,
            thesis=(f"{ev.n_insiders} distinct insiders bought "
                    f"${ev.total_value_usd:,.0f} of {candidate.ticker} on the "
                    f"open market within {CLUSTER_WINDOW_DAYS} days "
                    f"(cluster complete {ev.filing_date}, filings public)"),
            invalidation="insiders' information horizon proves longer than the hold",
            expected_holding_days=hold_days,
            priced_in=False,
            priced_in_reasoning=("thesis is that Form 4 information in "
                                 "under-covered names is consumed slowly"),
        )
    return signal_fn
