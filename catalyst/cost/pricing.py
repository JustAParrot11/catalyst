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


#: HOW MUCH DEARER THAN THE DEAREST THING WE KNOW A MODEL NOBODY HAS
#: BILLED US FOR IS ASSUMED TO BE.
#:
#: OWNER-ASKED 2026-09-12: *"i want nothing manual, i want it to auto add
#: the models"*.
#:
#: Before this, a model absent from the table above could not be priced
#: at all: `rates_for` raised, `tracker.record_usage` wrote an UNPRICED
#: row, and the governor then refused to authorise ANY spend until a
#: human edited this file and redeployed. So "try the new model" meant a
#: code change, which is the manual step the owner is removing.
#:
#: The owner's own assumption about how it would work is worth correcting
#: here, because it is the obvious one: *"the pricing is ok as it calls
#: per day doesnt it so it will just mark a higher price"*. The daily
#: reconciliation corrects a rate by RATIO - billed divided by what we
#: priced locally - so it needs an existing rate to scale. With no rate
#: at all there is no local figure to divide, and the bot halts before
#: any bill can teach it anything. The mechanism cannot bootstrap from
#: nothing; it can only correct something.
#:
#: So an unknown model gets SOMETHING, and the direction of the guess is
#: the whole design. Over-pricing throttles the bot - fewer calls inside
#: the same cap, which costs opportunity and cannot overspend.
#: Under-pricing authorises calls the budget cannot afford, which is the
#: one failure the owner has named as the thing they actually care about
#: ("a hard stop to stop bot using all the budget"). So the seed sits
#: deliberately ABOVE anything this project has ever been charged: twice
#: the dearest published rate it knows.
#:
#: WHY TWO AND NOT TEN. The seed has to be close enough that the first
#: closed day's bill can replace it. `measured_rates.SANITY_MULTIPLE` is
#: 4: a measured rate more than 4x from the one in force is treated as a
#: credit or a misread bill and refused. A 2x seed leaves the true rate
#: within a factor of 2 of it in the likely cases and inside the 4x
#: window in all of them, so the correction lands on the first clean day
#: instead of being rejected as impossible. A test holds that
#: relationship, because the two constants live in different files.
COLD_START_MULTIPLE = Decimal("2")


def cold_start_rates() -> tuple[Decimal, Decimal]:
    """(input, output) cents/MTok for a model nobody has billed us for.

    Derived from the table rather than typed, so adding a dearer model
    raises the seed automatically and nobody has to remember to.
    """
    if not MODEL_RATES_CENTS_PER_MTOK:
        # RAISED AS UnknownModelError, NOT AS A BARE ValueError. `max()`
        # on an empty table raises ValueError, and UnknownModelError is
        # the only exception the recording path catches - so a bare one
        # escapes `record_usage`, abandons the cycle, and loses the
        # record of spend that had already happened. Found by emptying
        # the table in an adversarial read of this change.
        raise UnknownModelError(
            "the pricing table is empty, so there is no rate to seed a "
            "cold start from. Nothing can be priced until it has at "
            "least one entry (TRAPS.md: never price at zero).")
    dearest_in = max(r[0] for r in MODEL_RATES_CENTS_PER_MTOK.values())
    dearest_out = max(r[1] for r in MODEL_RATES_CENTS_PER_MTOK.values())
    return (dearest_in * COLD_START_MULTIPLE,
            dearest_out * COLD_START_MULTIPLE)


def has_published_rate(model: str) -> bool:
    """Whether `model` has a rate from this table rather than the seed.

    The dashboard needs the distinction: "300c/MTok, which is Anthropic's
    published price" and "1000c/MTok, which is a deliberately high guess
    because nothing has been billed yet" are different facts and must not
    render identically (BUILD-BRIEF: every number says where it came
    from).
    """
    return model in MODEL_RATES_CENTS_PER_MTOK


def rates_for(model: str, on_date: date) -> tuple[Decimal, Decimal]:
    """(input, output) cents/MTok for `model` when nothing has been
    billed for it yet. Never returns a zero.

    A model this table has never heard of is priced at
    `cold_start_rates()` - see COLD_START_MULTIPLE for why that is a
    high guess rather than a refusal. What is still REFUSED is a model
    that is not a model: a blank or non-string id would pool the spend
    of everything that failed to name itself under one key, and
    `_sole_model` would then read a two-model day as one. Classified by
    the rule, not by a list of bad values (house rule 7).

    `on_date` is retained because callers price historical rows and the
    date-effective lookup in `overrides.py` - which is where a MEASURED
    rate lands - needs it. This table itself no longer varies by date:
    a rate that changes on a date nobody was billed on is a forecast,
    and forecasting prices is what the owner removed.
    """
    if not isinstance(model, str) or not model.strip():
        raise UnknownModelError(
            f"{model!r} is not a model id, so there is nothing to price. "
            "An unnamed call must never price itself at zero (TRAPS.md)."
        )
    if model not in MODEL_RATES_CENTS_PER_MTOK:
        return cold_start_rates()
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
    """Pricing something that is not a model must be a loud failure,
    never a silent zero - spend quietly costing nothing is exactly the
    TRAPS.md failure class this module exists to prevent.

    NARROWED 2026-09-12. This used to fire for any model absent from the
    table, which meant a model Anthropic had released but nobody had
    typed in here halted the governor. That is now a cold-start seed
    (see COLD_START_MULTIPLE). What still raises is a call that does not
    name a model at all - an empty string or a non-string - because
    there is no rate for "unnamed" and pooling that spend would also
    make a two-model day read as one to `measured_rates._sole_model`.
    """


def rates_stale(as_of=None):
    """Dashboard warning, deliberately NOT a test failure (audit N5):
    a stale pricing table must be loud on the dashboard without blocking
    the upgrade path."""
    from datetime import date, datetime, timezone

    as_of = as_of or datetime.now(timezone.utc).date()
    verified = date.fromisoformat(RATES_VERIFIED_ON)
    return (as_of - verified).days > RATES_MAX_AGE_DAYS
