"""Candidate A — post-earnings drift, surprise sourced from XBRL.

Pre-registered design (docs/STRATEGY-PROPOSALS.md, Candidate A):
- Surprise = seasonal random walk on the reported quarterly figure
  (this quarter vs the same quarter a year ago), standardised by the
  volatility of its own past seasonal differences (a SUE with no
  analyst data).
- Trade only where the XBRL surprise and the announcement-window price
  reaction AGREE in sign; long-only account, so positive surprise +
  positive reaction only.
- Hold 10-15 trading days (12 used, fixed before grading).

Parameters fixed BEFORE grading:
- Primary tag: us-gaap/NetIncomeLoss (USD). Net income is additive
  across quarters, so fiscal Q4 = FY minus the three reported quarters
  is exact — EPS is not additive (buybacks) and is only a fallback.
- SUE_MIN = 1.0 standard deviations; >= 4 prior seasonal diffs required
  (min 2 years of history behind every surprise).
- Reaction window: last close vs close 3 sessions earlier, measured at
  signal time from the PointInTimeView.

Point-in-time discipline:
- Event date = the `filed` date of the 10-Q/10-K that first reported
  the quarter's value. The XBRL fact provably exists in a public filing
  on that date. (Most companies press-release earnings days earlier;
  using the XBRL filed date means we trade LATER than the market first
  saw the number — this biases the measured drift DOWN, not up.)
- Every input value is the FIRST-FILED value for its period
  (min(filed) per period), so restatements never leak backwards. The
  `frames` endpoint is never used (no filed date -> restatement
  contamination; DATA-SOURCES.md §2.3).
- The signal uses only facts with filed <= event date, enforced in
  build_events by construction (prior periods' first-filed dates are
  checked against the event's filed date).
"""
from __future__ import annotations

import csv
import gzip
import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from catalyst.backtest.data import PointInTimeView
from catalyst.discovery import Candidate
from catalyst.research.schema import ResearchView

SUE_MIN = 1.0
HOLD_DAYS = 12
REACTION_SESSIONS = 3
MIN_PRIOR_DIFFS = 4
MAX_PRIOR_DIFFS = 8

_QTR_DUR = (75, 105)      # days: a "quarterly" period
_ANN_DUR = (340, 390)     # days: an "annual" period


@dataclass(frozen=True)
class EarningsEvent:
    ticker: str
    filed: date            # tradable date: filing that first reported the value
    period_end: date
    value: float           # quarterly figure, as first filed
    sue: float             # standardised seasonal surprise
    form: str


def _first_filed_periods(facts: dict, tag: str, unit: str) -> dict:
    """{(start,end): (value, first_filed_date, form)} for one tag."""
    node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
    if not node:
        return {}
    out: dict = {}
    for fact in node.get("units", {}).get(unit, []):
        try:
            start = date.fromisoformat(fact["start"])
            end = date.fromisoformat(fact["end"])
            filed = date.fromisoformat(fact["filed"])
            val = float(fact["val"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (start, end)
        if key not in out or filed < out[key][1]:
            out[key] = (val, filed, fact.get("form", ""))
    return out


def _quarterly_series(periods: dict) -> list[tuple[date, date, float, date, str]]:
    """Quarterly values incl. derived fiscal Q4 = annual - 3 quarters.
    Returns [(start, end, value, filed, form)] sorted by end date."""
    quarters = {k: v for k, v in periods.items()
                if _QTR_DUR[0] <= (k[1] - k[0]).days <= _QTR_DUR[1]}
    annuals = {k: v for k, v in periods.items()
               if _ANN_DUR[0] <= (k[1] - k[0]).days <= _ANN_DUR[1]}
    rows = [(s, e, v, f, form) for (s, e), (v, f, form) in quarters.items()]
    for (a_start, a_end), (a_val, a_filed, a_form) in annuals.items():
        inside = [((s, e), (v, f, fm)) for (s, e), (v, f, fm) in quarters.items()
                  if s >= a_start and e < a_end]
        inside.sort(key=lambda kv: kv[0][1])
        if len(inside) < 3:
            continue
        last3 = inside[-3:]
        # The three quarters must tile up to the annual end (no gaps/overlap).
        q3_end = last3[-1][0][1]
        if not (60 <= (a_end - q3_end).days <= 120):
            continue
        q4 = (q3_end, a_end)
        if any(abs((q4[0] - s).days) < 45 and abs((q4[1] - e).days) < 45
               for (s, e) in quarters):
            continue  # a real Q4 quarterly fact already exists
        # Q4 value only knowable when the ANNUAL was filed; prior quarters
        # must have been filed by then or the derivation isn't point-in-time.
        if any(f > a_filed for _, (_, f, _) in [(k, v) for k, v in last3]):
            continue
        rows.append((q3_end, a_end, a_val - sum(v for _, (v, _, _) in last3),
                     a_filed, a_form))
    rows.sort(key=lambda r: r[1])
    return rows


def build_events(facts_dir: str | Path, tickers: list[str]) -> list[EarningsEvent]:
    """Derive standardized-surprise earnings events from cached companyfacts."""
    facts_dir = Path(facts_dir)
    events: list[EarningsEvent] = []
    for ticker in tickers:
        path = facts_dir / f"{ticker}.json.gz"
        if not path.exists():
            continue
        facts = json.loads(gzip.decompress(path.read_bytes()))
        periods = _first_filed_periods(facts, "NetIncomeLoss", "USD")
        if not periods:
            periods = _first_filed_periods(
                facts, "EarningsPerShareDiluted", "USD/shares")
        series = _quarterly_series(periods)
        by_end = [(e, v, f, form) for (_, e, v, f, form) in series]
        for i, (end, val, filed, form) in enumerate(by_end):
            # Seasonal pair: the quarter ending ~1 year earlier.
            prior = [x for x in by_end[:i]
                     if 350 <= (end - x[0]).days <= 380 and x[2] <= filed]
            if not prior:
                continue
            d_now = val - prior[-1][1]
            # Historical seasonal diffs, all first-filed on/before this event.
            diffs: list[float] = []
            for j in range(i):
                e_j, v_j, f_j, _ = by_end[j]
                if f_j > filed:
                    continue
                p_j = [x for x in by_end[:j]
                       if 350 <= (e_j - x[0]).days <= 380 and x[2] <= filed]
                if p_j:
                    diffs.append(v_j - p_j[-1][1])
            if len(diffs) < MIN_PRIOR_DIFFS:
                continue
            sd = statistics.stdev(diffs[-MAX_PRIOR_DIFFS:])
            if sd <= 0:
                continue
            events.append(EarningsEvent(
                ticker=ticker, filed=filed, period_end=end,
                value=val, sue=d_now / sd, form=form))
    events.sort(key=lambda ev: ev.filed)
    return events


def write_events_csv(events: list[EarningsEvent], path: str | Path) -> None:
    with Path(path).open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "filed", "period_end", "value", "sue", "form"])
        for ev in events:
            w.writerow([ev.ticker, ev.filed.isoformat(), ev.period_end.isoformat(),
                        repr(ev.value), repr(ev.sue), ev.form])


def read_events_csv(path: str | Path) -> list[EarningsEvent]:
    out = []
    with Path(path).open(newline="") as f:
        for row in csv.DictReader(f):
            out.append(EarningsEvent(
                ticker=row["ticker"], filed=date.fromisoformat(row["filed"]),
                period_end=date.fromisoformat(row["period_end"]),
                value=float(row["value"]), sue=float(row["sue"]),
                form=row["form"]))
    return out


def build_candidates(events: list[EarningsEvent], *,
                     sue_min: float = SUE_MIN) -> tuple[list[Candidate], dict]:
    """Candidates for positive-surprise events; returns (candidates,
    side-table keyed by candidate id for the signal function)."""
    cands: list[Candidate] = []
    table: dict[str, EarningsEvent] = {}
    for k, ev in enumerate(events):
        if ev.sue < sue_min:
            continue
        cid = f"A-{ev.filed.isoformat()}-{ev.ticker}-{k}"
        cands.append(Candidate(
            id=cid, ticker=ev.ticker, catalyst_type="earnings_drift",
            catalyst_date=ev.filed, catalyst_date_confidence="confirmed",
            source_event_ids=(f"xbrl:{ev.ticker}:{ev.period_end.isoformat()}",),
            discovered_at=datetime(2016, 1, 1, tzinfo=timezone.utc),
            sector="unknown", correlation_tags=("type:earnings_drift",),
        ))
        table[cid] = ev
    return cands, table


def make_signal_fn(table: dict, *, hold_days: int = HOLD_DAYS,
                   reaction_sessions: int = REACTION_SESSIONS):
    def signal_fn(candidate: Candidate, view: PointInTimeView) -> ResearchView:
        ev = table.get(candidate.id)
        no = ResearchView(
            candidate_id=candidate.id, direction="no_trade", conviction=0.0,
            thesis="", invalidation="n/a", expected_holding_days=hold_days,
            priced_in=False, priced_in_reasoning="n/a")
        if ev is None:
            return no
        bars = view.bars(candidate.ticker)
        if len(bars) < reaction_sessions + 1:
            return no
        past, last = bars[-reaction_sessions - 1].close, bars[-1].close
        if past <= 0:
            return no
        reaction = float(last / past) - 1.0
        if reaction <= 0:
            # XBRL says beat, tape disagrees -> the refusal case A tracks.
            return no
        return ResearchView(
            candidate_id=candidate.id, direction="long", conviction=1.0,
            thesis=(f"{candidate.ticker} Q ending {ev.period_end}: seasonal "
                    f"surprise {ev.sue:+.2f} sd (first-filed XBRL, "
                    f"filed {ev.filed}); {reaction_sessions}-session reaction "
                    f"{reaction:+.2%} agrees in sign"),
            invalidation="reaction reverses before the drift window completes",
            expected_holding_days=hold_days,
            priced_in=False,
            priced_in_reasoning=("drift hypothesis is that the surprise is NOT "
                                 "fully priced within the reaction window"),
        )
    return signal_fn
