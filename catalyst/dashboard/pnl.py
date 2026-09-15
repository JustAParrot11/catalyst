"""Profit and loss over time for one position, and what happened when.

OWNER-ASKED 2026-09-14: *"i want to be able to view some live or semi
live profit/loss graph info when viewing each individual trade, almost
live wall street like for current trades, and a profit loss graph with
lines detailing events so we can see maybe when something happened"*.

WHY THIS IS P&L AND NOT PRICE. The card already draws price. "How am I
doing" is a question about money, and the answer is one multiplication
the page already has every term for - so nothing here is modelled,
estimated or projected:

    pnl(t) = (price(t) - fill) x qty

The stop becomes a HORIZONTAL FLOOR at a known number of dollars,
`(stop - fill) x qty`, which is the single most useful line on the
chart: it is the most this position can lose while the stop does its
job, and on RLMD it is -$39.45 against a $40.00 bound.

WHAT IS NOT CLAIMED, AND THE PAGE SAYS SO. This is UNREALISED. Nothing
here is banked until the position closes, and paper fills pay no spread
(TRAPS.md), so the round trip is a real cost this line does not carry.

NOTHING IN THIS MODULE CAN SIZE, SPEND OR TRADE. It reads a fill, a
quantity and a series of prices and returns points to draw. A test
greps `risk/`, `execution/` and `cost/` for its symbols and requires
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

#: Widest the plot can be, in pixels, matching the existing position
#: chart's own geometry (W 660, L 88, R 58 -> 514 drawable).
#:
#: THE POINT BUDGET IS DERIVED FROM IT, not typed: more points than
#: pixels cannot be drawn distinctly, so fetching them buys bandwidth
#: and nothing a reader can see. That makes the bar resolution a
#: consequence of the chart's width rather than a number somebody chose.
PLOT_PX = 514

#: Alpaca's intraday timeframes, coarsest last. The resolution is picked
#: as the FINEST one whose point count still fits the plot, so a
#: position held an hour gets minutes and one held three weeks gets
#: hours, with no table of hold-length bands to get wrong.
TIMEFRAMES: tuple[tuple[str, int], ...] = (
    ("1Min", 1), ("5Min", 5), ("15Min", 15), ("30Min", 30),
    ("1Hour", 60), ("1Day", 390),
)

#: Minutes in a US regular session (09:30-16:00 ET). Used only to turn a
#: hold length into an expected bar count, never to decide whether the
#: market is open - that comes from the broker clock (house rule 7).
SESSION_MINUTES = 390


def timeframe_for(held: timedelta, plot_px: int = PLOT_PX) -> str:
    """The finest Alpaca timeframe whose bars still fit the plot.

    A position open for 40 minutes wants `1Min`; one open three weeks
    wants `1Hour`. Derived, so there is no band table to maintain and
    no hold length that falls between two rows.
    """
    days = max(held.total_seconds() / 86400.0, 0.0)
    # Sessions, not calendar days: a weekend adds no bars.
    minutes = max(days * SESSION_MINUTES * (5.0 / 7.0), 1.0)
    for name, per_bar in TIMEFRAMES:
        if minutes / per_bar <= plot_px:
            return name
    return TIMEFRAMES[-1][0]


def window_for(opened: datetime, now: datetime) -> tuple[str, str]:
    """The fetch window: from the day the position opened to now.

    A day EITHER SIDE, because a bar's timestamp is the start of its
    interval in UTC and a session straddles the date line in some
    timezones - asking for exactly the open date has dropped the first
    bar of a position opened late in the session.
    """
    start = (opened - timedelta(days=1)).date().isoformat()
    end = (now + timedelta(days=1)).date().isoformat()
    return start, end


@dataclass(frozen=True)
class Point:
    """One moment, and the unrealised P&L at it."""

    at: datetime
    price: float
    pnl: float
    #: True only for the point taken from the live quote rather than a
    #: closed bar. The chart labels it and dates it; a reader must be
    #: able to tell a settled bar from a quote read seconds ago.
    live: bool = False
    #: True only for the SALE of a closed position. `live` and `final`
    #: are different claims about the last point and the caption has to
    #: tell them apart: one is a quote that will move again, the other is
    #: money already banked. A point is never both.
    final: bool = False


@dataclass(frozen=True)
class Mark:
    """Something that happened, at a time, with a sentence."""

    at: datetime
    kind: str
    label: str
    detail: str = ""


@dataclass
class Series:
    """Everything the chart needs, and nothing it has to compute."""

    points: list = field(default_factory=list)
    marks: list = field(default_factory=list)
    fill: float = 0.0
    qty: float = 0.0
    #: P&L if the stop fills - the floor, in dollars. None when no stop
    #: is on record, which must draw no floor rather than draw zero.
    stop_pnl: float | None = None
    stop_price: float | None = None
    #: Why there is no line, in words, when there is no line.
    empty_reason: str = ""
    #: The timeframe actually requested, so the caption can say it.
    timeframe: str = ""
    #: WHICH SOURCE DREW THE LINE - "broker_intraday" or
    #: "cached_daily_close". Named rather than implied, because section
    #: 24 already paid for the lesson that "Alpaca said" and "a file on
    #: disk said" are different claims and only one of them was true.
    source: str = ""
    #: Why there is no live point, when there is none. A closed position
    #: has no live price BY DESIGN; an open one that cannot be quoted is
    #: a fault, and the caption must be able to tell them apart.
    quote_error: str = ""
    #: True when the final point is the BROKER'S OWN realised figure
    #: rather than this module's multiplication. Named because the two
    #: differ by fees and slippage, and a caption that says "banked" must
    #: only say it about the first.
    realised_is_broker: bool = False

    @property
    def last(self):
        return self.points[-1] if self.points else None

    @property
    def segments(self) -> list:
        """The line broken wherever the market was shut.

        MEASURED 2026-09-15, on a three-day hold at 30-minute bars:
        **67% of the chart's width was a straight line drawn across
        hours when nothing traded.** Alpaca returns session bars only, so
        an overnight gap is 17.5 hours of horizontal distance with no
        prices in it - and a single polyline renders that as a long clean
        diagonal between two jagged runs. That is what the owner was
        reading as price movement, and it was the loudest feature of the
        picture.

        THE BREAK RULE IS ONE SESSION, and `SESSION_MINUTES` is the
        number already in this module rather than a new one to drift
        (sections 18 and 30 both cost a second constant meaning the same
        thing). A gap longer than the market can be open in one day
        cannot be anything but a closure, so the caption's claim - the
        line breaks where the market was shut - is exactly the rule. A
        shorter hole inside a session stays connected, because at that
        scale interpolating is not a lie worth a broken line.

        A DAILY SERIES IS NEVER BROKEN: one point per trading day is what
        a daily series IS, so a weekend is not a gap in it.
        """
        if len(self.points) < 2:
            return [list(self.points)]
        if str(self.timeframe or "") == "1Day":
            return [list(self.points)]
        limit = timedelta(minutes=SESSION_MINUTES)
        runs, run = [], [self.points[0]]
        for prev, pt in zip(self.points, self.points[1:]):
            if pt.at - prev.at > limit:
                runs.append(run)
                run = []
            run.append(pt)
        runs.append(run)
        return runs

    @property
    def gaps(self) -> list:
        """(from, to) for every closure the line breaks across.

        Breaking the line stops the chart claiming movement that never
        happened, and on a three-day hold that leaves two thirds of the
        width blank. **Blank is its own confusion** - it reads as missing
        data or a broken chart rather than as a closed market - so the
        spans are shaded, and this is the same segmentation rule read a
        second way rather than a second rule that could disagree with it.
        """
        runs = [r for r in self.segments if r]
        return [(a[-1].at, b[0].at) for a, b in zip(runs, runs[1:])]

    @property
    def lo(self) -> float:
        """Lowest P&L the chart must show - the floor included, because a
        chart that crops the stop hides the worst case."""
        vals = [p.pnl for p in self.points] + [0.0]
        if self.stop_pnl is not None:
            vals.append(self.stop_pnl)
        return min(vals)

    @property
    def hi(self) -> float:
        vals = [p.pnl for p in self.points] + [0.0]
        if self.stop_pnl is not None:
            vals.append(self.stop_pnl)
        return max(vals)


def _as_dt(value) -> datetime | None:
    """A timestamp from a bar, a row or a column, or None.

    Alpaca stamps bars `2026-09-14T13:35:00Z`; the database writes
    isoformat with an offset; some rows carry a bare date. All three
    reach this, and an unreadable one is DROPPED rather than guessed -
    a mark at the wrong moment is worse than a mark that is missing,
    because the reader cannot tell it is wrong.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _as_float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def build(*, fill, qty, stop=None, bars=(), live_price=None, live_at=None,
          opened=None, marks=(), timeframe="", side="long",
          exit_price=None, exit_at=None, realised_pnl=None) -> Series:
    """The P&L series for one position.

    `bars` are Alpaca bar dicts (`t` and `c`) or anything with `.day`
    and `.close`, so a daily cache and an intraday fetch both work and
    the caller does not have to normalise first.

    A SHORT IS NOT SUPPORTED AND IS REFUSED RATHER THAN INVERTED. The
    account is long-only (WHAT-WE-TRIED section 3 row 11), so a short
    here would be a sign flip nothing has ever exercised - and a P&L
    chart with the sign backwards is worse than no chart at all.
    """
    out = Series(timeframe=str(timeframe or ""))
    f, q = _as_float(fill), _as_float(qty)
    if f is None or q is None or f <= 0 or q <= 0:
        out.empty_reason = (
            "no fill price or quantity is on record for this position, so "
            "profit and loss cannot be computed from it")
        return out
    if str(side or "long").lower() != "long":
        out.empty_reason = (
            f"this position is recorded as {side!r}, and the account is "
            "long-only - refusing to draw a P&L line whose sign has never "
            "been exercised")
        return out
    out.fill, out.qty = f, q

    s = _as_float(stop)
    if s is not None and s > 0:
        out.stop_price, out.stop_pnl = s, (s - f) * q

    open_at = _as_dt(opened)
    for bar in bars or ():
        at = _as_dt(bar.get("t") if isinstance(bar, dict)
                    else getattr(bar, "day", None))
        price = _as_float(bar.get("c") if isinstance(bar, dict)
                          else getattr(bar, "close", None))
        if at is None or price is None or price <= 0:
            continue
        # NEVER BEFORE THE FILL. A bar from the morning of the entry day
        # would draw P&L on a position that did not exist, and on an
        # intraday chart that is most of the first day.
        if open_at is not None and at < open_at:
            continue
        out.points.append(Point(at=at, price=price, pnl=(price - f) * q))
    out.points.sort(key=lambda p: p.at)

    # THE PURCHASE IS A POINT, AND ITS VALUE IS ZERO BY DEFINITION.
    # `(fill - fill) x qty` is nothing modelled or assumed - at the
    # instant it was bought the position had made and lost nothing. Three
    # things follow, and the third is a defect fixed by construction:
    #   - the line visibly DEPARTS from break-even instead of starting
    #     somewhere unexplained partway up;
    #   - the first bar of the entry day no longer decides where the
    #     chart begins;
    #   - the "Bought" mark can no longer be dropped for sitting before
    #     the first bar - the same silent omission the exit had at the
    #     other end, which is recorded below.
    if open_at is not None and out.points and out.points[0].at > open_at:
        out.points.insert(0, Point(at=open_at, price=f, pnl=0.0))

    lp, la = _as_float(live_price), _as_dt(live_at)
    if lp is not None and lp > 0 and la is not None:
        # THE LIVE POINT REPLACES A BAR AT THE SAME MINUTE rather than
        # sitting beside it, so the line cannot double back on itself.
        out.points = [p for p in out.points if p.at < la]
        out.points.append(Point(at=la, price=lp, pnl=(lp - f) * q, live=True))

    # THE SALE IS A POINT ON THE LINE, NOT A TICK BESIDE IT.
    #
    # MEASURED 2026-09-15 on the owner's own closed card: the line ended
    # at the last BAR and the dot was labelled with that bar's
    # mark-to-market - `+$20.10 unrealised` against `+$11.95` actually
    # banked, **$8.15 wrong**, on a trade that had settled. `exit_price`
    # and `realized_pnl_cents` were both on `TradeStory` and neither
    # reached this module. Eleventh instance of this project's most
    # recurring defect, and the first to put a WRONG MONEY FIGURE on a
    # page rather than merely an unhelpful one.
    #
    # It also fixes a second defect by construction. The mark window
    # below is `first <= at <= last`, so an exit that settled after the
    # final bar - the ordinary case, since a bar is stamped at the START
    # of its interval - was SILENTLY DROPPED. Section 10b calls the exit
    # the single most important mark on a closed trade, and the chart
    # could omit it without saying so. Making the sale the last point
    # moves `last`, so the mark can no longer fall outside the window.
    xp, xa = _as_float(exit_price), _as_dt(exit_at)
    if xp is not None and xp > 0 and xa is not None:
        out.points = [p for p in out.points if p.at < xa]
        # ONE NUMBER DRIVES BOTH THE DOT'S HEIGHT AND ITS LABEL. The
        # broker's realised figure and this module's multiplication
        # differ by fees and slippage, so picking one for the position
        # and the other for the caption would put a dot at one value
        # labelled another - the two-numbers-one-meaning trap sections 18
        # and 30 have already paid for twice. The broker's figure wins
        # when it exists, because it is what the account actually holds.
        booked = _as_float(realised_pnl)
        if booked is not None:
            out.realised_is_broker = True
        out.points.append(Point(at=xa, price=xp, final=True,
                                pnl=booked if booked is not None
                                else (xp - f) * q))

    if not out.points:
        out.empty_reason = (
            "no prices are on record for this position yet, so there is "
            "nothing to plot - the figures below are the ones that exist")

    first = out.points[0].at if out.points else None
    last = out.points[-1].at if out.points else None
    for mark in marks or ():
        at = _as_dt(mark.get("at") if isinstance(mark, dict) else mark[0])
        if at is None:
            continue
        # A mark outside the plotted window would be drawn at an edge and
        # read as having happened there. Dropped, and the caller's own
        # tables still list it.
        if first is not None and not (first <= at <= last):
            continue
        if isinstance(mark, dict):
            out.marks.append(Mark(at=at, kind=str(mark.get("kind") or ""),
                                  label=str(mark.get("label") or ""),
                                  detail=str(mark.get("detail") or "")))
        else:
            out.marks.append(Mark(at=at, kind=str(mark[1]),
                                  label=str(mark[2]),
                                  detail=str(mark[3]) if len(mark) > 3 else ""))
    out.marks.sort(key=lambda m: m.at)
    return out
