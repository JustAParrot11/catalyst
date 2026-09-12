"""Has the stock moved past the view that was formed on it? MONEY-CRITICAL.

OWNER-ASKED 2026-09-12: *"is there any harm in doing a deep dive into the
news to find potential for monday. e.g. it finds a good connection and
market opportunity, it says if price is less than this on monday buy, if
not resume as normal?"*

THIS IS THAT CONDITION, WITH ONE CHANGE THE OWNER SHOULD KNOW ABOUT.

The owner's phrasing puts the threshold in the model's hands - *"it says
if price is less than this"*. That is the one rule in this project that
does not move: **the model decides what and whether; code decides how
much and at what price.** A price that gates an order is a number that
touches money, and a thesis that also sets its own entry price converts
persuasiveness directly into position. This bot's own record has a
candidate scoring 0.82 conviction on a compelling argument whose
conclusion was *do not trade*.

So the behaviour the owner asked for is built, and the number is measured
rather than argued: **the threshold is that stock's own 95th-percentile
daily move**, read from three years of its cached bars by
`risk.stock_gap.daily_move_percentile` - the same measurement that
already decides where its stop sits. If the stock has moved further than
an ordinary bad day since the view was formed, the price the model
reasoned about no longer exists, and the view is not evidence about the
price on offer now.

WHY BOTH DIRECTIONS, AND NAMED SEPARATELY. They are different mistakes
and the refusal tracker has to be able to tell them apart:

  - UP, on a long: the move has already happened. You are paying for it.
    This is the case the owner described.
  - DOWN, on a long: something occurred over the weekend that the view
    never saw. A thesis written on Friday at $50 is not a thesis about
    $42, and "it got cheaper" is indistinguishable from "it broke" until
    somebody looks.

Both are recorded with the price, so the record can eventually say which
half was right. If the down-gap refusals turn out to have been leaving
money on the table, that is evidence to loosen - which is how every
adaptive number in this system is supposed to move, rather than by
argument.

A REFUSAL HERE IS NOT A DISCARD. The candidate keeps its place; what it
loses is the right to trade on a price that has gone. The owner's own
words for the other branch were *"if not, resume as normal"* - so the
stale view is superseded and the candidate goes back through the normal
flow, which researches it at the live price if it is still in window.

NO HISTORY MEANS REFUSE, NOT WAVE THROUGH. If the stock's own noise
cannot be measured there is no way to tell an ordinary move from a
violent one, and the only safe reading of an unmeasurable move is that
we do not know. That is the tight direction. It should also be rare:
`ensure_history` caches a candidate's bars during the same pass that
researches it, so by the time a view exists the bars exist too.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: Which point of the stock's own daily-move distribution counts as "an
#: ordinary bad day". The same 0.95 `stock_gap` uses to place a stop
#: outside a name's noise - one idea, one number, so the two cannot
#: drift apart and mean different things about the same stock.
ORDINARY_MOVE_PERCENTILE = 0.95

#: Why a candidate's stored view may not be acted on. Named, because
#: "refused" with no reason is what the refusal tracker cannot score.
MOVED_UP = "moved_up_past_view_price"
MOVED_DOWN = "moved_down_past_view_price"
UNMEASURABLE = "view_price_move_unmeasurable"


def _finite(value) -> Decimal:
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError(f"{value!r} is not finite")
    return d


def move_against_view(price_at_view, live_price,
                      ordinary_daily_move) -> tuple[str | None, Decimal]:
    """(reason the stored view may not be acted on, the move as a
    fraction). `reason` is None when the view still stands.

    Pure arithmetic on three numbers - no database, no clock, no broker -
    so it can be reasoned about and tested exhaustively. The caller reads
    the prices and supplies the measured move.

    NEVER RAISES. An input that cannot be read is refused as
    UNMEASURABLE, not allowed through: the failure mode this exists to
    prevent is trading on a price that has gone, and a number nobody can
    parse is not evidence that the price is still there.
    """
    try:
        was = _finite(price_at_view)
        now = _finite(live_price)
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return UNMEASURABLE, Decimal("0")
    if was <= 0 or now <= 0:
        return UNMEASURABLE, Decimal("0")

    move = (now - was) / was
    if ordinary_daily_move is None:
        return UNMEASURABLE, move
    try:
        bound = _finite(ordinary_daily_move)
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return UNMEASURABLE, move
    if bound <= 0:
        # A stock that never moves is a bad history file, not a calm
        # stock - `daily_move_percentile` says so in its own comment and
        # returns None for it. A zero arriving here anyway would refuse
        # every candidate on any move at all, so it is treated as the
        # measurement failing rather than as a threshold of nothing.
        return UNMEASURABLE, move

    if move > bound:
        return MOVED_UP, move
    if move < -bound:
        return MOVED_DOWN, move
    return None, move


def sentence(reason: str | None, move: Decimal, bound) -> str:
    """One line a person can read, for the dashboard and the log.

    Every refusal in this system has to be explainable after the fact,
    and "moved_up_past_view_price" is a key, not an explanation.
    """
    pct = f"{move * 100:+.1f}%"
    if reason is None:
        return (f"moved {pct} since the view was formed, inside this "
                f"stock's own ordinary daily move - the view still "
                f"describes the price on offer.")
    if reason == UNMEASURABLE:
        return (f"moved {pct} since the view was formed, but this stock's "
                "own daily move could not be measured from its cached "
                "history - so there is no way to tell an ordinary move "
                "from a violent one, and the view is not acted on.")
    try:
        bound_pct = f"{_finite(bound) * 100:.1f}%"
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        bound_pct = "its ordinary daily move"
    if reason == MOVED_UP:
        return (f"moved {pct} since the view was formed, past this stock's "
                f"own ordinary daily move of {bound_pct} - the move has "
                "already happened, so the price the thesis was written "
                "about is not the price on offer.")
    return (f"moved {pct} since the view was formed, past this stock's own "
            f"ordinary daily move of {bound_pct} - something happened that "
            "the thesis never saw, and cheaper is not the same as better.")
