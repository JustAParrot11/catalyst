"""Size against THIS stock's history, not just its catalyst's category.

MEASURED 2026-08-15 over 8,432,494 overnight gaps across 4,674 tickers
with ten years of daily bars (scripts/measure_adverse_gaps.py, offline,
re-runnable). Two findings, and the second one reverses the first.

UNCONDITIONALLY, a 60% overnight gap is astronomically rare: the 99.9th
percentile of the most volatile decile is 30.2%. On that reading the
0.60 assumption looks absurdly over-conservative.

CONDITIONAL ON THE DAY THAT MATTERS, it does not. Taking each ticker's
single WORST overnight gap in ten years - which for a small-cap biotech
is overwhelmingly likely to be exactly the readout, CRL or trial halt
this parameter exists for - the volatile decile's median worst day is
35.3%, its 90th percentile 66.3%, and 16.3% of those names saw a gap at
least as bad as 60%. So 0.60 is roughly the 88th percentile of worst-day
outcomes for a volatile name: invented, but not wrong.

    all tickers, worst day each:  median 18.6%  p75 31.4%  p90 50.7%
    volatile decile, worst day:   median 35.3%  p75 52.1%  p90 66.3%

WHICH MEANS THE DEFECT IS NOT THE NUMBER, IT IS THAT IT IS ONE NUMBER.
A blanket 0.60 sizes a $40bn pharma with a PDUFA date exactly as it
sizes a $50m microcap, and those are not the same risk. The median
ticker's worst day in a decade is 18.6%; sizing it against 60% cuts the
position to under a third of what its own history justifies, and does so
silently.

So the gap becomes per-stock where that stock's history is known, and
stays the catalyst-type value where it is not.

THE BOUNDS ON IT, and why each is where it is:

  CEILING - the catalyst type's own value. Per-stock evidence may only
  ever make a position SMALLER than the category says, never larger.
  A calm history is not permission to exceed the category's judgement.

  FLOOR - MIN_EVENT_GAP, 0.20, for binary catalyst types. A stock that
  has never had a binary event has a calm history precisely because
  nothing has happened to it yet, and sizing on that is how a surprise
  CRL takes a position that was never stress-tested. 0.20 is the
  measured median worst-day gap across all 4,674 tickers (18.6%, rounded
  up), so it is a number from the data rather than a preference.

This is an ADAPTIVE input, not a hard bound. Every hard bound - max loss
per position, total exposure, position count, the kill switches - is
untouched and still applies underneath.
"""

import csv
from decimal import Decimal
from pathlib import Path

#: Never size a binary event as though it were a quiet stock, however
#: calm that stock's own history looks. The measured median worst-day
#: overnight gap across all 4,674 tickers was 18.6%; this rounds up.
MIN_EVENT_GAP = Decimal("0.20")

#: A stop has to sit outside the stock's ordinary noise or it is churn
#: rather than protection. Three times the 95th-percentile daily move
#: means an ordinary bad day does not touch it, but a move roughly three
#: times worse than that does.
STOP_NOISE_MULTIPLE = Decimal("3")

#: However quiet a stock is, a stop nearer than this is inside the
#: spread and the day's chop for anything this bot can actually trade.
MIN_STOP_WIDTH = Decimal("0.08")

#: Catalyst types whose whole nature is a single scheduled yes/no. These
#: get the floor above; everything else is free to use its own measured
#: history down to the stop width.
BINARY_CATALYST_TYPES = frozenset({"fda_decision", "clinical_readout"})

#: Below this a "gap" is sub-penny noise rather than a market move.
_MIN_PRICE = 1.0
#: Above this it is almost always an unadjusted split or corporate
#: action. Left in, these alone would pin every stock at the ceiling.
_MAX_PLAUSIBLE_GAP = 0.90
#: Under a year of history cannot describe a tail.
_MIN_SESSIONS = 250


def worst_overnight_gap(bars_dir, ticker: str) -> Decimal | None:
    """That stock's worst adverse overnight gap on record, as a
    fraction. None when there is not enough history to say.

    Deliberately the WORST rather than a percentile: the quantity of
    interest is "how badly has this name actually gapped when something
    happened to it", and one bad day is the event, not an outlier to be
    trimmed away.
    """
    path = Path(bars_dir) / f"{ticker.upper()}.csv"
    try:
        with path.open(newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error):
        return None
    if len(rows) < _MIN_SESSIONS:
        return None

    worst = Decimal("0")
    seen = 0
    prev_close = None
    for row in rows:
        try:
            open_, close = float(row["open"]), float(row["close"])
        except (KeyError, TypeError, ValueError):
            prev_close = None
            continue
        if prev_close is not None and prev_close >= _MIN_PRICE and open_ > 0:
            gap = (open_ - prev_close) / prev_close
            seen += 1
            if -_MAX_PLAUSIBLE_GAP <= gap < 0:
                worst = max(worst, Decimal(str(-gap)))
        prev_close = close if close > 0 else None

    if seen < _MIN_SESSIONS or worst == 0:
        return None
    return worst.quantize(Decimal("0.0001"))


def daily_move_percentile(bars_dir, ticker: str,
                          q: float = 0.95) -> Decimal | None:
    """How far this stock moves in an ordinary bad day, close to close.

    A stop has to sit OUTSIDE that stock's own noise or it is just a
    donation - stopped out by a Tuesday, not by the thesis breaking.
    This is the number that says where "outside the noise" is for this
    particular name, rather than for its catalyst's whole category.
    """
    path = Path(bars_dir) / f"{ticker.upper()}.csv"
    try:
        with path.open(newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error):
        return None
    if len(rows) < _MIN_SESSIONS:
        return None

    moves = []
    prev_close = None
    for row in rows:
        try:
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            prev_close = None
            continue
        if prev_close is not None and prev_close >= _MIN_PRICE and close > 0:
            move = abs(close - prev_close) / prev_close
            if move <= _MAX_PLAUSIBLE_GAP:
                moves.append(move)
        prev_close = close if close > 0 else None

    if len(moves) < _MIN_SESSIONS:
        return None
    moves.sort()
    idx = int(q * (len(moves) - 1))
    measured = Decimal(str(moves[idx])).quantize(Decimal("0.0001"))

    # A STOCK THAT NEVER MOVED IS NOT A CALM STOCK, IT IS A BAD FILE.
    # Found by stress: several kinds of unusable history parse into
    # constant prices - a column of 1s, a file whose only readable
    # column is `close`, one mangled by a BOM. Every daily move is then
    # zero, which reads as "no volatility" and puts the stop at the
    # MIN_STOP_WIDTH floor: an 8% stop on a biotech binary, where the
    # category says 50%.
    #
    # That is not dangerous - the adverse gap still dominates sizing, so
    # the position does not grow - but it is a stop that ordinary noise
    # takes out, and the cause is unreadable data rather than measured
    # risk. No real security has a 95th-percentile daily move of zero
    # over a year, so this is the file telling us it is not real.
    if measured <= 0:
        return None
    return measured


def effective_stop(catalyst_type: str, type_stop, bars_dir,
                   ticker: str) -> tuple[Decimal, str]:
    """Where the stop should sit for THIS stock, and why.

    THIS IS THE ONE THAT ACTUALLY MOVES THE POSITION SIZE. Sizing uses
    max(gap, stop), and for the binary catalyst types the shipped stop
    width is 0.50 - wider than almost any measured gap, so it, not the
    gap, is what caps the position. Lowering the gap alone changes
    almost nothing, which is only visible once both are measured.

    A 50% stop is defensible on a microcap that routinely moves 15% in a
    session. On a large-cap pharma that has never moved 11% in a decade
    it is not a stop at all - the position is a total loss long before
    it triggers. So the stop is placed at STOP_NOISE_MULTIPLE times that
    stock's 95th-percentile daily move, which is what "outside its own
    noise" means, bounded on both sides.

    Bounds, same shape as the gap: the catalyst type's value is the
    CEILING, so per-stock evidence can only ever tighten. MIN_STOP_WIDTH
    is the floor, because a stop inside the spread and the ordinary
    day's chop is churn, not protection.
    """
    type_stop = Decimal(str(type_stop))
    try:
        noise = daily_move_percentile(bars_dir, ticker)
    except Exception:  # noqa: BLE001 - sizing must never die on a file
        noise = None

    if noise is None:
        return type_stop, (
            f"no usable price history for {ticker}, so the {catalyst_type} "
            f"category stop of {type_stop:.0%} stands")

    proposed = (noise * STOP_NOISE_MULTIPLE).quantize(Decimal("0.0001"))
    stop = max(MIN_STOP_WIDTH, min(proposed, type_stop))
    if stop >= type_stop:
        return type_stop, (
            f"{ticker} moves {noise:.1%} on a bad day, so a stop outside "
            f"its noise is wider than the {catalyst_type} category stop of "
            f"{type_stop:.0%} - the category value stands")
    if stop == MIN_STOP_WIDTH and proposed < MIN_STOP_WIDTH:
        return stop, (
            f"{ticker} is quiet enough ({noise:.1%} on a bad day) that "
            f"{STOP_NOISE_MULTIPLE}x its noise is under the {MIN_STOP_WIDTH:.0%} "
            "floor - a stop tighter than that is stopped out by ordinary "
            "chop rather than by the thesis breaking")
    return stop, (
        f"{ticker} moves {noise:.1%} on a bad day, so its stop sits at "
        f"{stop:.0%} - {STOP_NOISE_MULTIPLE}x its own noise, rather than "
        f"the {type_stop:.0%} the {catalyst_type} category assumes")


def effective_gap(catalyst_type: str, type_gap, bars_dir,
                  ticker: str) -> tuple[Decimal, str]:
    """The adverse gap to size THIS candidate against, and why.

    Returns (gap, reason). The reason is a sentence, because it lands on
    the decision page beside the position size and "why is this position
    that size" is the question the whole page exists to answer.

    Falls back to the catalyst-type value on ANY doubt - no history, too
    little history, an unreadable file. The fallback is the conservative
    direction: the type value is a ceiling, so falling back can only
    ever make the position smaller.
    """
    type_gap = Decimal(str(type_gap))
    measured = None
    try:
        measured = worst_overnight_gap(bars_dir, ticker)
    except Exception:  # noqa: BLE001 - sizing must never die on a file
        measured = None

    if measured is None:
        return type_gap, (
            f"no usable price history for {ticker}, so the {catalyst_type} "
            f"category assumption of {type_gap:.0%} stands")

    floor = (MIN_EVENT_GAP if catalyst_type in BINARY_CATALYST_TYPES
             else Decimal("0"))
    gap = max(measured, floor)
    if gap >= type_gap:
        return type_gap, (
            f"{ticker} has gapped {measured:.0%} overnight in its own "
            f"history, at or beyond the {catalyst_type} assumption of "
            f"{type_gap:.0%}, so the category value stands")
    if gap == floor and measured < floor:
        return gap, (
            f"{ticker}'s own worst overnight gap is {measured:.0%}, but a "
            f"{catalyst_type} is a single yes/no event and a quiet history "
            f"only means one has not happened yet - held at the {floor:.0%} "
            "floor measured across 4,674 tickers")
    return gap, (
        f"{ticker}'s worst overnight gap in its own history is "
        f"{measured:.0%}, better than the {catalyst_type} category "
        f"assumption of {type_gap:.0%}, so this position is sized against "
        "what this stock has actually done")
