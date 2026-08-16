"""What the price has already done — the evidence for "are we too late".

TWO DEFECTS IN ONE PLACE, both live on every research call.

FIRST, THE MODEL WAS ASKED ABOUT HISTORY AND GIVEN ONLY THE PRESENT.
Question 6 of the research brief asks "has the market already consumed
these filings? ... what price and volume have done since each filing
became public". The MARKET DATA block it is given carries the last
close, the current spread, and nothing else. There is no move, no
trend, no range - the one question that decides whether an opportunity
is still open was asked with the evidence withheld, leaving a web
search as the only way to answer it. A search rarely returns "up 12%
since 5 August".

SECOND, AND WORSE, IT WAS BEING TOLD SOMETHING FALSE. `median_daily_
dollar_volume` is populated as Decimal("0") - the snapshot's own
comment says "not consumed by any current sizing rule" - and the prompt
renders that as:

    "median daily dollar volume: $0. Thin names move on little, and are
     also where a cluster is least likely to have been consumed
     already."

under a heading that reads "measured at decision time (not from the
model)". So a $60bn company arrives described as having no volume at
all, with a nudge attached saying that means the signal is probably
still fresh. That is not a missing number, it is a wrong one wearing a
measurement's clothes, and it points the judgement in a specific
direction on every single call.

BOTH ARE NOW FREE TO FIX. `data/bar_history.ensure_history` caches three
years of daily bars for every candidate immediately before the risk
engine runs, so the answer is already on disk. No API call, no search,
no model tokens spent guessing at what a file could state exactly.

WHAT THIS DELIBERATELY DOES NOT DO. It draws no conclusion. It reports
the move, the trend, the range position and the real volume, and leaves
"is that already priced in" to the model - which is the judgement step -
and leaves what to do about it to deterministic code. Handing over a
number is not the same as handing over an opinion.
"""

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median

#: Sub-dollar rows are noise rather than prices, same threshold the gap
#: measurement uses.
_MIN_PRICE = 1.0
#: A year of sessions, for the range position.
_RANGE_SESSIONS = 252


@dataclass(frozen=True)
class PriceAction:
    """What the tape has already done. Every field may be None, and None
    means NOT MEASURED - never zero, because zero is a claim."""

    move_since_catalyst_pct: Decimal | None = None
    sessions_since_catalyst: int | None = None
    move_5d_pct: Decimal | None = None
    move_20d_pct: Decimal | None = None
    #: 0 = at the 52-week low, 100 = at the high.
    range_position_pct: Decimal | None = None
    median_daily_dollar_volume: Decimal | None = None
    #: Recent volume against that median. Above 1 means the name is
    #: being traded more than usual, which is what "the market noticed"
    #: looks like.
    recent_volume_ratio: Decimal | None = None

    @property
    def measured(self) -> bool:
        return self.move_since_catalyst_pct is not None


def _rows(bars_dir, ticker: str) -> list[tuple]:
    """(day, close, dollar_volume), oldest first. Unreadable rows are
    dropped rather than guessed at."""
    path = Path(bars_dir) / f"{str(ticker).upper()}.csv"
    out: list[tuple] = []
    try:
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                try:
                    day = date.fromisoformat(str(row["date"])[:10])
                    close = float(row["close"])
                    volume = float(row.get("volume") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                if close < _MIN_PRICE or close != close:
                    continue
                out.append((day, close, close * volume))
    except (OSError, csv.Error):
        return []
    out.sort(key=lambda r: r[0])
    return out


def _pct(now: float, then: float) -> Decimal | None:
    if not then:
        return None
    try:
        return (Decimal(str((now - then) / then * 100))
                .quantize(Decimal("0.1")))
    except (InvalidOperation, ArithmeticError):
        return None


def price_action(bars_dir, ticker: str,
                 since: date | None = None) -> PriceAction:
    """What this stock has done, from the cached bars. Never raises.

    `since` is the catalyst date - the day the evidence became public.
    The move from there to now IS the answer to "are we too late", and
    it is a fact rather than an opinion.
    """
    try:
        rows = _rows(bars_dir, ticker)
    except Exception:  # noqa: BLE001 - research must never die on a file
        return PriceAction()
    if len(rows) < 2:
        return PriceAction()

    last_day, last_close, _ = rows[-1]
    closes = [r[1] for r in rows]
    volumes = [r[2] for r in rows if r[2] > 0]

    since_pct = sessions = None
    if since is not None:
        at_or_after = [r for r in rows if r[0] >= since]
        if at_or_after and at_or_after[0][0] != last_day:
            since_pct = _pct(last_close, at_or_after[0][1])
            sessions = len(at_or_after) - 1

    window = closes[-_RANGE_SESSIONS:]
    lo, hi = min(window), max(window)
    range_pos = None
    if hi > lo:
        try:
            range_pos = (Decimal(str((last_close - lo) / (hi - lo) * 100))
                         .quantize(Decimal("0.1")))
        except (InvalidOperation, ArithmeticError):
            range_pos = None

    med_vol = ratio = None
    if len(volumes) >= 20:
        med = median(volumes[-_RANGE_SESSIONS:])
        if med > 0:
            med_vol = Decimal(str(int(med)))
            recent = median(volumes[-5:])
            ratio = Decimal(str(recent / med)).quantize(Decimal("0.01"))

    return PriceAction(
        move_since_catalyst_pct=since_pct,
        sessions_since_catalyst=sessions,
        move_5d_pct=_pct(last_close, closes[-6]) if len(closes) > 5 else None,
        move_20d_pct=_pct(last_close, closes[-21]) if len(closes) > 20 else None,
        range_position_pct=range_pos,
        median_daily_dollar_volume=med_vol,
        recent_volume_ratio=ratio,
    )
