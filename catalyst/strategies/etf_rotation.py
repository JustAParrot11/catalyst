"""Candidate E — control arm: cross-sectional relative strength on liquid ETFs.

Pre-registered design (docs/STRATEGY-PROPOSALS.md, Candidate E):
- Universe: liquid sector + asset-class ETFs, price data only.
- Rank by trailing relative strength; hold the top N on a fixed
  rebalance schedule. No other data source, by design — this is the
  null-hypothesis arm the data-linked candidates must beat.

Parameters fixed BEFORE grading (recorded here so tuning is visible):
- LOOKBACK_SESSIONS = 60   (~12 weeks trailing total return)
- TOP_N = 4                (proposal §E.4: "4 positions")
- Rebalance every 5 sessions (proposal: "weekly"), hold 4 trading days
  so the exit open, T+1 settlement, and the next entry open line up on
  a cash account.
- Absolute filter: only long an ETF whose own lookback return is > 0.

A tuned variant (rebalance every 10 sessions, hold 9) exists as
`make_signal_fn(hold_days=9)` + `build_candidates(step=10)`; per the
bake-off rules it may only be selected on IN-sample evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from catalyst.backtest.data import BarCache, PointInTimeView
from catalyst.discovery import Candidate
from catalyst.research.schema import ResearchView

ETF_UNIVERSE = [
    # SPDR sectors
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    # broad equity styles / regions
    "QQQ", "IWM", "DIA", "EFA", "EEM",
    # rates / credit
    "TLT", "IEF", "LQD", "HYG",
    # commodities / real assets
    "GLD", "SLV", "VNQ",
]

LOOKBACK_SESSIONS = 60
TOP_N = 4
DEFAULT_STEP = 5          # rebalance every N sessions
DEFAULT_HOLD = 4          # trading days; exit open aligns with next entry post-T+1


def build_candidates(cache: BarCache, *, step: int = DEFAULT_STEP,
                     etfs: list[str] | None = None,
                     benchmark: str = "SPY") -> list[Candidate]:
    """One candidate per ETF per rebalance session (every `step` sessions
    of the benchmark calendar). The signal function does the ranking."""
    etfs = etfs or ETF_UNIVERSE
    calendar = [b.day for b in cache.load_bars(benchmark)]
    out: list[Candidate] = []
    # Start late enough that the lookback window can exist at all.
    for idx in range(LOOKBACK_SESSIONS + 1, len(calendar), step):
        day = calendar[idx]
        for etf in etfs:
            out.append(Candidate(
                id=f"E-{day.isoformat()}-{etf}",
                ticker=etf,
                catalyst_type="cross_sectional_momentum",
                catalyst_date=day,
                catalyst_date_confidence="confirmed",
                source_event_ids=("etf_rotation_schedule",),
                discovered_at=datetime(2016, 1, 1, tzinfo=timezone.utc),
                sector="etf",
                correlation_tags=("beta:market",),
            ))
    return out


def make_signal_fn(*, hold_days: int = DEFAULT_HOLD, top_n: int = TOP_N,
                   lookback: int = LOOKBACK_SESSIONS,
                   etfs: list[str] | None = None):
    etfs = etfs or ETF_UNIVERSE
    _rank_memo: dict = {}

    def _ranks(view: PointInTimeView) -> dict[str, float]:
        """Momentum rank across the whole ETF list as of view.as_of.
        Memoised per as_of (the ranking is identical for the 23
        same-day candidates)."""
        key = view.as_of
        if key in _rank_memo:
            return _rank_memo[key]
        mom: dict[str, float] = {}
        for etf in etfs:
            bars = view.bars(etf)
            if len(bars) < lookback + 1:
                continue            # ETF not alive long enough (e.g. XLC pre-2018)
            past, last = bars[-lookback - 1].close, bars[-1].close
            if past > 0:
                mom[etf] = float(last / past) - 1.0
        _rank_memo[key] = mom
        return mom

    def signal_fn(candidate: Candidate, view: PointInTimeView) -> ResearchView:
        mom = _ranks(view)
        my = mom.get(candidate.ticker)
        no = ResearchView(
            candidate_id=candidate.id, direction="no_trade", conviction=0.0,
            thesis="not in momentum top set at this rebalance",
            invalidation="n/a", expected_holding_days=hold_days,
            priced_in=False, priced_in_reasoning="n/a")
        if my is None or my <= 0.0:
            return no
        top = sorted(mom.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        if candidate.ticker not in {t for t, _ in top}:
            return no
        return ResearchView(
            candidate_id=candidate.id, direction="long",
            conviction=1.0,
            thesis=(f"{candidate.ticker} ranks in top {top_n} of {len(mom)} ETFs "
                    f"by {lookback}-session return ({my:+.2%}), which is > 0"),
            invalidation="drops out of the top set at a later rebalance",
            expected_holding_days=hold_days,
            priced_in=False,
            priced_in_reasoning="cross-sectional rank, not an event to price in",
        )
    return signal_fn
