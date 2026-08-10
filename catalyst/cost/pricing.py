"""Per-model pricing table, cents per million tokens.

Every trap in TRAPS.md's cost section is encoded here as arithmetic:
cache writes bill at 1.25x input, cache reads at 0.1x input, web search
at $10 per 1,000 queries ON TOP of tokens. Rates are the documented
public prices; update this table when Anthropic's pricing page changes
and record the change in git - the raw usage object is always stored
verbatim (schema: cost_events.raw_usage_json), so history can be
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

# Sonnet 5 launched with INTRODUCTORY pricing - $2/M in, $10/M out -
# in effect through 2026-08-31 inclusive; the standard list price above
# applies from 2026-09-01. Found empirically 2026-08-10: the first
# tracked day priced ~41% above the owner's real-time console figure at
# standard rates ($0.326 local vs ~$0.21 console), and intro rates
# reprice the same verbatim usage to $0.231. The first closed-day
# reconciliation is the hard check; a residual mismatch still pauses.
SONNET5_INTRO_ENDS = date(2026, 8, 31)          # inclusive
SONNET5_INTRO_RATES = (Decimal("200"), Decimal("1000"))


def rates_for(model: str, on_date: date) -> tuple[Decimal, Decimal]:
    """(input, output) cents/MTok in effect for `model` ON `on_date` -
    pricing is a function of when the spend happened, not of when the
    row is priced. Raises UnknownModelError; never returns a zero."""
    if model not in MODEL_RATES_CENTS_PER_MTOK:
        raise UnknownModelError(
            f"No pricing for model {model!r}. Add it to pricing.py - "
            "an unknown model must never price itself at zero (TRAPS.md)."
        )
    if model == "claude-sonnet-5" and on_date <= SONNET5_INTRO_ENDS:
        return SONNET5_INTRO_RATES
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
