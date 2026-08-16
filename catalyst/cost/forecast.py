"""When will the budget run out, and what happens when it does.

THE FAILURE THIS EXISTS TO PREVENT is the quietest one in the system:
the bot spends its monthly cap early, the governor correctly refuses
every further call, and the bot stops researching anything for the rest
of the month. Nothing is broken. Nothing errors. It simply stops, and
the first anyone knows is an empty funnel days later.

The arithmetic is not marginal. Measured on the owner's own live day -
193.30c of scheduled spend across 20 calls:

    base default $5/month   exhausted after   2.6 days
    $25/month                                12.9 days
    $50/month                                25.9 days

So on the SHIPPED DEFAULT the bot researches for two and a half days a
month and sits idle for the other twenty-seven. That is not a bug in the
governor - the cap is doing exactly what it was told - it is a number
nobody was told about.

The dashboard already had a pace marker, which answers "am I ahead of
pace" for someone looking at it. This answers the question that actually
matters to someone who is NOT looking: on this burn rate, what date does
the bot stop, and is that before the month ends. It is logged as well as
drawn, because an unattended bot's owner reads the journal after the
fact, not the page during.

Deliberately a projection, and labelled as one everywhere it appears.
The brief is explicit that expected profit may never authorise spend and
that projections are not evidence - this forecasts nothing about
returns and authorises nothing. It is arithmetic on money already spent.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Forecast:
    """What the current burn rate implies for the rest of the month."""

    spent_cents: Decimal
    cap_cents: Decimal
    days_elapsed: int
    days_in_month: int
    #: None when there is not yet a rate to project from.
    daily_rate_cents: Decimal | None = None
    #: The day the cap is expected to run out, or None if it is not
    #: expected to run out this month.
    exhausted_on: date | None = None
    already_exhausted: bool = False

    @property
    def days_left_in_month(self) -> int:
        return max(0, self.days_in_month - self.days_elapsed)

    @property
    def will_stop_early(self) -> bool:
        """True when the bot is expected to go quiet before month end."""
        return self.already_exhausted or self.exhausted_on is not None

    def sentence(self) -> str:
        """Plain English, for the log and the page alike. The owner is
        not a developer; "projected_exhaustion=2026-08-19" is an event
        code, not a warning."""
        cap = f"${self.cap_cents / 100:,.2f}"
        spent = f"${self.spent_cents / 100:,.2f}"
        if self.already_exhausted:
            return (
                f"THE MONTH'S BUDGET IS GONE. {spent} of the {cap} cap has "
                f"been spent with {self.days_left_in_month} day(s) of the "
                "month left, so no further research will run until the 1st. "
                "The bot is not broken and nothing needs fixing - it is "
                "doing what the cap tells it. Raise the monthly budget on "
                "the Settings page if you want it to keep going.")
        if self.daily_rate_cents is None:
            return (f"{spent} spent of the {cap} monthly cap. Too early in "
                    "the month to project a burn rate.")
        rate = f"${self.daily_rate_cents / 100:,.2f}"
        if self.exhausted_on is None:
            return (
                f"{spent} of the {cap} monthly cap spent over "
                f"{self.days_elapsed} day(s), about {rate} a day. At that "
                "rate the budget lasts the whole month.")
        early = self.days_in_month - self.exhausted_on.day
        return (
            f"AT THIS RATE THE BOT STOPS RESEARCHING ON {self.exhausted_on}, "
            f"about {early} day(s) before the month ends. "
            f"{spent} of the {cap} cap is gone after "
            f"{self.days_elapsed} day(s), about {rate} a day. Nothing is "
            "broken - the cap is being enforced - but the bot will sit idle "
            "until the 1st unless the monthly budget is raised on the "
            "Settings page.")


def _days_in_month(day: date) -> int:
    nxt = date(day.year + (day.month == 12), (day.month % 12) + 1, 1)
    return (nxt - date(day.year, day.month, 1)).days


def forecast(spent_cents, cap_cents, as_of: date) -> Forecast:
    """Project the month's spend from what has already been spent.

    A straight-line projection off month-to-date spend, which is the
    only honest one available: the bot's cost per day is driven by how
    many candidates appear, and nothing here can know that in advance.
    Straight-line is stated as the method rather than dressed up.
    """
    try:
        spent = Decimal(str(spent_cents))
        cap = Decimal(str(cap_cents))
        # NaN and Infinity BUILD from a string quite happily and then
        # raise on the first COMPARISON - the same trap cycle._finite
        # exists for, and it surfaces here inside the trading loop.
        if not (spent.is_finite() and cap.is_finite()):
            raise InvalidOperation("non-finite")
    except (InvalidOperation, TypeError, ValueError):
        spent, cap = Decimal("0"), Decimal("0")

    days_in_month = _days_in_month(as_of)
    elapsed = as_of.day

    if cap <= 0:
        return Forecast(spent, cap, elapsed, days_in_month)
    if spent >= cap:
        return Forecast(spent, cap, elapsed, days_in_month,
                        already_exhausted=True)
    if elapsed < 1 or spent <= 0:
        return Forecast(spent, cap, elapsed, days_in_month)

    rate = spent / Decimal(elapsed)
    if rate <= 0:
        return Forecast(spent, cap, elapsed, days_in_month)

    days_of_headroom = (cap - spent) / rate
    # ROUNDED DOWN, then one day added: the day it runs out is the day
    # partway through which the cap is reached, not the last day it
    # survived. Rounding the optimistic way would report a stop date
    # after the bot had already gone quiet.
    stop_day = elapsed + int(days_of_headroom) + 1
    if stop_day > days_in_month:
        return Forecast(spent, cap, elapsed, days_in_month,
                        daily_rate_cents=rate)
    return Forecast(spent, cap, elapsed, days_in_month,
                    daily_rate_cents=rate,
                    exhausted_on=date(as_of.year, as_of.month, stop_day))
