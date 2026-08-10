"""Per-model pricing table, cents per million tokens.

Every trap in TRAPS.md's cost section is encoded here as arithmetic:
cache writes bill at 1.25x input, cache reads at 0.1x input, web search
at $10 per 1,000 queries ON TOP of tokens. Rates are the documented
public prices; update this table when Anthropic's pricing page changes
and record the change in git - the raw usage object is always stored
verbatim (schema: cost_events.raw_usage_json), so history can be
repriced if a rate here was ever wrong.
"""

from decimal import Decimal

# cents per 1M tokens: (input, output)
MODEL_RATES_CENTS_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    # Claude 4.5 / 4.6 family and 5 family, public list prices
    "claude-haiku-4-5": (Decimal("100"), Decimal("500")),
    "claude-sonnet-4-6": (Decimal("300"), Decimal("1500")),
    "claude-sonnet-5": (Decimal("300"), Decimal("1500")),
    "claude-opus-5": (Decimal("500"), Decimal("2500")),
}

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
