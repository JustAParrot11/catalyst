"""Cold-start pricing table, cents per million tokens.

THE RATE THAT ACTUALLY PRICES A CALL DOES NOT LIVE HERE. It lives in
`pricing_overrides`, written by `measured_rates.py` from what Anthropic
actually billed for a closed day (owner-set 2026-09-05: "stop locally
calculating the new price full stop trust the admin API"). This table is
the starting point for a model that has never been billed yet, and it is
never a forecast - nothing in it changes on a date.

Every trap in TRAPS.md's cost section is encoded here as arithmetic:
cache writes bill at 1.25x input, cache reads at 0.1x input, web search
at $10 per 1,000 queries ON TOP of tokens. Those multipliers are cold
starts too - `measured_rates.learn_factors_from_closed_day` measures
them from the bill's own itemisation. The raw usage object is always
stored verbatim (schema: cost_events.raw_usage_json), so history can be
repriced if a rate here was ever wrong.
"""

from datetime import date
from decimal import Decimal

# cents per 1M tokens: (input, output)
MODEL_RATES_CENTS_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    # Claude 4.5 / 4.6 family and 5 family, public list prices
    "claude-haiku-4-5": (Decimal("100"), Decimal("500")),
    "claude-sonnet-4-6": (Decimal("300"), Decimal("1500")),
    "claude-sonnet-5": (Decimal("300"), Decimal("1500")),
    "claude-opus-5": (Decimal("500"), Decimal("2500")),
}

#: NO PRICE CHANGE IS PREDICTED HERE ANY MORE.
#:
#: OWNER-SET 2026-09-05: "stop locally calculating the new price full
#: stop trust the admin API".
#:
#: This file used to carry a SCHEDULE: Sonnet 5's introductory rate of
#: 200/1000 through 2026-08-31, and 300/1500 from 1 September. That
#: second half was a forecast read off a docs page, and on 1 September
#: it fired - every call priced 50% higher on a date somebody typed in,
#: with nothing having been billed to justify it. If Anthropic had held
#: the introductory rate, the bot would have been throttling itself
#: against an invented price, and the one mechanism able to correct it
#: was allowed to walk the number back only 10% per three agreeing days.
#:
#: The bill is the price. `measured_rates.py` reads it from the Admin
#: API's own closed-day figures and writes a date-effective override,
#: and that override is what prices calls from then on. What is left
#: here is a COLD START - the rate used for a model that has never been
#: billed yet - and nothing else. It is never a prediction, so it never
#: changes on a date.
#:
#: The intro figures are kept as the last rate this project has
#: EVIDENCE for on Sonnet 5: the owner's console and our ledger agreed
#: to the cent at 200/1000 (45.7446c on 2026-08-15, 364.2052c on
#: 2026-08-17, zero difference both times). Starting from a measured
#: number and letting the bill move it is the whole point; starting
#: from a guessed one is what this change removes.
MODEL_RATES_CENTS_PER_MTOK["claude-sonnet-5"] = (Decimal("200"),
                                                 Decimal("1000"))


def rates_for(model: str, on_date: date) -> tuple[Decimal, Decimal]:
    """(input, output) cents/MTok for `model` when nothing has been
    billed for it yet. Raises UnknownModelError; never returns a zero.

    `on_date` is retained because callers price historical rows and the
    date-effective lookup in `overrides.py` - which is where a MEASURED
    rate lands - needs it. This table itself no longer varies by date:
    a rate that changes on a date nobody was billed on is a forecast,
    and forecasting prices is what the owner removed.
    """
    if model not in MODEL_RATES_CENTS_PER_MTOK:
        raise UnknownModelError(
            f"No pricing for model {model!r}. Add it to pricing.py - "
            "an unknown model must never price itself at zero (TRAPS.md)."
        )
    return MODEL_RATES_CENTS_PER_MTOK[model]

CACHE_WRITE_MULTIPLIER = Decimal("1.25")      # 5m TTL x input rate (TRAPS.md)
CACHE_WRITE_MULTIPLIER_1H = Decimal("2.0")    # 1h TTL bills at 2x input (audit F3)
CACHE_READ_MULTIPLIER = Decimal("0.10")    # x input rate (TRAPS.md)
WEB_SEARCH_CENTS_PER_QUERY = Decimal("1")  # $10 / 1000 queries (TRAPS.md)

# Rate provenance (audit F3): a stale table must be noisy, not silent.
# A test fails when RATES_VERIFIED_ON is older than RATES_MAX_AGE_DAYS.
RATES_SOURCE_URL = "https://docs.anthropic.com/en/docs/about-claude/pricing"
RATES_VERIFIED_ON = "2026-08-10"
RATES_MAX_AGE_DAYS = 90


class UnknownModelError(ValueError):
    """Pricing an unknown model must be a loud failure, never a silent
    zero - a renamed model quietly pricing itself at nothing is exactly
    the TRAPS.md failure class this module exists to prevent."""


def rates_stale(as_of=None):
    """Dashboard warning, deliberately NOT a test failure (audit N5):
    a stale pricing table must be loud on the dashboard without blocking
    the upgrade path."""
    from datetime import date, datetime, timezone

    as_of = as_of or datetime.now(timezone.utc).date()
    verified = date.fromisoformat(RATES_VERIFIED_ON)
    return (as_of - verified).days > RATES_MAX_AGE_DAYS
