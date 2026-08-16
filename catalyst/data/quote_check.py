"""A second opinion on the live quote, from bars already on disk.

OWNER-ASKED: "I want to ensure all data is correct and validated so we
arent trading under false pretenses."

THE HONEST GAP THIS CLOSES. Every number that touches money descends
from one live Alpaca quote. If that quote is wrong - the wrong symbol,
a decimal in the wrong place, a stale book that still carries a
plausible timestamp - nothing anywhere would notice, because there is
only one source and it is believed. That is a single point of truth
holding up the entire position size.

There is a free second opinion already on disk: `bar_history` caches
three years of daily bars for every candidate immediately before the
risk engine runs. Yesterday's close cannot confirm today's price, but it
can absolutely refuse to believe a hundredfold one.

IT FLAGS LOUDLY AND REFUSES ALMOST NEVER, and that asymmetry is the
whole design. A stock CAN gap 40% on a readout - refusing that would
throw away exactly the trades this bot exists to take, and would do it
silently, which is the failure mode the owner has objected to more than
any other. So an ordinary large move is recorded as an observation and
passed through with its number attached. Only a deviation no market
produces - the shape a decimal error or a wrong symbol makes - stops the
trade.

    within  ±35%   normal; nothing said
    beyond  ±35%   FLAGGED: passed through, recorded, shown on the page
    beyond  5x or 1/5   REFUSED: not a price move, a broken number

The 5x bound is deliberately far outside anything a single session
produces. A stock that genuinely quintuples overnight is a corporate
action - a reverse split - and sizing off the un-adjusted side of one is
exactly the false pretence worth refusing.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

#: Beyond this, say so on the page. Not a refusal - a 40% gap on a
#: clinical readout is a real thing that really happens.
FLAG_DEVIATION = Decimal("0.35")

#: Beyond this it is not a price move at all. A fivefold overnight
#: change is a corporate action or a broken number, and sizing off
#: either is trading under a false pretence.
REFUSE_RATIO = Decimal("5")


@dataclass(frozen=True)
class QuoteCheck:
    """What the cached history says about the live quote."""

    checked: bool = False
    reference_close: Decimal | None = None
    reference_day: object = None
    deviation: Decimal | None = None          # signed fraction
    flagged: bool = False
    refused: bool = False
    note: str = ""

    @property
    def sentence(self) -> str:
        """For the decision page and the log. Says what was compared,
        not merely whether it passed."""
        if not self.checked:
            return ("No cached history for this ticker, so the live quote "
                    "could not be cross-checked. It is the only source for "
                    "this candidate's price.")
        pct = f"{self.deviation * 100:+.1f}%" if self.deviation is not None \
            else "?"
        base = (f"Live quote cross-checked against the last cached close "
                f"({self.reference_close} on {self.reference_day}): {pct}.")
        if self.refused:
            return base + (" REFUSED - no single session moves a price that "
                           "far. This is the shape of a decimal error, a "
                           "wrong symbol or an unadjusted corporate action, "
                           "and sizing off it would be trading on a false "
                           "number.")
        if self.flagged:
            return base + (" Larger than an ordinary day, so it is recorded "
                           "here - but large moves are exactly what this bot "
                           "trades, so it is not refused.")
        return base + " Consistent with the cached history."


def cross_check(live_price, bars_dir, ticker: str) -> QuoteCheck:
    """Compare a live quote against the newest cached daily close.

    Never raises. No history, or an unreadable one, means NOT CHECKED -
    stated as such rather than quietly reported as passing, because
    "nothing objected" and "nothing looked" are different facts.
    """
    try:
        price = Decimal(str(live_price))
        if not price.is_finite() or price <= 0:
            return QuoteCheck(note="the live quote itself is not a price")
    except (InvalidOperation, TypeError, ValueError):
        return QuoteCheck(note="the live quote itself is not a number")

    try:
        from catalyst.data.price_action import _rows

        rows = _rows(bars_dir, ticker)
    except Exception:  # noqa: BLE001 - a check must never break a cycle
        rows = []
    if not rows:
        return QuoteCheck(note="no cached history to compare against")

    day, close, _volume = rows[-1]
    try:
        reference = Decimal(str(close))
        if reference <= 0:
            return QuoteCheck(note="the cached close is not a price")
        deviation = ((price - reference) / reference).quantize(
            Decimal("0.0001"))
    except (InvalidOperation, ArithmeticError):
        return QuoteCheck(note="the comparison could not be computed")

    ratio = price / reference
    refused = ratio >= REFUSE_RATIO or ratio <= (Decimal("1") / REFUSE_RATIO)
    flagged = abs(deviation) > FLAG_DEVIATION

    return QuoteCheck(
        checked=True, reference_close=reference, reference_day=day,
        deviation=deviation, flagged=bool(flagged), refused=bool(refused))
