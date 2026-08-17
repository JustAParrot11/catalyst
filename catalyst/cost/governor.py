"""Pre-call spend authorization. Expected profit never authorizes spend.

Scheduled cap: base $5/month hard (BUILD-BRIEF.md), rising only by
governor_profit_share x NET realized LIVE profit from the PRIOR closed
month (audit F5: paper P&L is fictional and never raises the cap;
prior-month basis so late-month profit cannot retroactively legitimize
early-month spend) - and NEVER above GOVERNOR_MAX_CAP_CENTS, a hard
bound only a human edit can change.

Manual cap: monthly ceiling AND a lifetime ceiling enforcing the $200
one-off build budget (audit F7). Both checked; the decision records
which bound.

Authorization is refused outright while the ledger has unpriced rows
(audit F2) or an unacknowledged reconciliation discrepancy (F1) - you
cannot authorize spend on top of a ledger with holes.
"""

import sqlite3
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, Decimal

from catalyst.cost import CostEstimate, GovernorDecision
from catalyst.cost.ledger import (
    lifetime_cents,
    day_to_date_cents,
    month_to_date_cents,
    net_realized_profit_cents_prior_month,
)
from catalyst.cost.tracker import has_unacknowledged_discrepancy, has_unpriced_rows

BASE_CAP_CENTS = Decimal("500")                     # $5/month, hard (BUILD-BRIEF.md)

# HARD BOUND (human review required to change): the scheduled cap can
# never exceed this regardless of realized profit. $8/month is
# BUILD-BRIEF's stated "workable" ceiling (10% annual hurdle); the same
# table calls $36/month "not viable", and without this clamp one strong
# month walks the cap toward that line (audit F5).
GOVERNOR_MAX_CAP_CENTS = Decimal("800")

#: A CEILING ON THE RATE, not a throttle. Owner-set 2026-08-14: "$5 a
#: day usage is ok ... the limit is so that it doesnt go far beyond".
#:
#: Measured against what the bot actually does, this does not bind: the
#: owner's live day cost 193.30c across 20 scheduled calls. 500c is
#: about 2.6x that, so ordinary operation never touches it and a
#: runaway stops within one day instead of one month.
#:
#: WHY IT IS NEEDED AT ALL. The monthly cap bounds the TOTAL and not
#: the RATE. MAX_RESEARCH_PER_CYCLE=3 against a 900-second cycle
#: permits 288 investigations a day; at conjunction prices that is a
#: month's budget in an afternoon followed by thirty dark days, and a
#: strategy that only trades the first days of a month cannot be
#: compared to a backtest that trades all of it.
DAILY_CAP_CENTS = Decimal("500")

#: How many days of even spending one day is allowed to take. The point
#: of a rate ceiling is that a runaway stops within a day instead of a
#: month; three days' worth does that while leaving room for the lumpy
#: reality - insider filings arrive in clusters, and a Tuesday with six
#: fresh candidates should not be rationed to a Sunday's allowance.
DAILY_BURST_DAYS = Decimal("3")

#: Days in the budget month, for turning a monthly cap into a daily one.
BUDGET_MONTH_DAYS = Decimal("30")


def daily_cap_cents(monthly_cap_cents=None) -> Decimal:
    """The rate ceiling in force, DERIVED from the monthly cap.

    THE FLAT $5 WAS A NUMBER FROM A SMALLER BUDGET, and it stayed put
    when the owner raised their cap. Owner-reported: "my new monthly
    limit is 100, ensure the bot doesnt still try stick to lower
    standards and hinder its effectiveness."

    That is the same defect the monthly cap already had and had fixed -
    the dashboard printing BASE_CAP_CENTS while the governor spent
    against something else - so it gets the same answer: one source of
    truth, derived, never a second constant to remember.

    NEVER TIGHTER THAN THE ORIGINAL. The floor is the owner's own
    2026-08-14 figure, so raising the monthly cap can only ever loosen
    this and lowering it cannot silently strangle the bot below what was
    already agreed.
    """
    if monthly_cap_cents is None:
        return DAILY_CAP_CENTS
    try:
        monthly = Decimal(str(monthly_cap_cents))
    except (ArithmeticError, TypeError, ValueError):
        return DAILY_CAP_CENTS
    if not monthly.is_finite() or monthly <= 0:
        return DAILY_CAP_CENTS
    # QUANTIZED TO WHOLE CENTS. Decimal division is exact-but-repeating -
    # 10000 / 30 * 3 lands on 999.9999999999999999999999999, not 1000 -
    # and a ceiling a hundredth of a cent under the intended figure is
    # the kind of thing that shows up months later as an off-by-one
    # refusal nobody can explain. Rounded UP, so deriving never makes
    # the ceiling tighter than the arithmetic says.
    derived = ((monthly / BUDGET_MONTH_DAYS) * DAILY_BURST_DAYS).quantize(
        Decimal("1"), rounding=ROUND_CEILING)
    return max(DAILY_CAP_CENTS, derived)

# The owner sets their own budget, and there is deliberately NO fixed
# ceiling on it. The two limits do different jobs and only one of them
# is a safety bound:
#
#   GOVERNOR_MAX_CAP_CENTS  bounds what the SYSTEM hands itself out of
#                           its own realised profit - the anti-ratchet,
#                           so a lucky month cannot walk the cap upward.
#                           This one never moves by itself, ever.
#   the owner's figure      is a person deciding how much of their own
#                           money to spend. That is not a safety
#                           question, and a hard-coded number cannot
#                           make it one - it can only make the product
#                           wrong as the account grows.
#
# What replaces the ceiling is INFORMED CONSENT, enforced where the
# number is entered rather than here: the form prints the annual hurdle
# the strategy must clear at the figure chosen, and a value far above
# the current one has to be confirmed, so a slipped keyboard cannot
# become a spending limit. See setup/first_run.py.
#
# BUILD-BRIEF's GBP 20 / ~$25 is still the figure at which the strategy
# must beat roughly 30%/year merely to match cash. It is now advice
# printed at the point of choosing rather than a wall.
OWNER_TYPO_GUARD_FACTOR = Decimal("10")     # entry-time only, see first_run
OWNER_SOFT_ADVICE_CENTS = Decimal("2500")   # where the hurdle warning sharpens

MANUAL_SPEND_CAP_CENTS_PER_MONTH = Decimal("2000")  # $20/month, human-set, never adaptive
MANUAL_LIFETIME_BUDGET_CENTS = Decimal("20000")     # the $200 one-off build budget
                                                     # (BUILD-BRIEF: "not a monthly
                                                     # allowance"; audit F7)

# Starting value for the adaptive governor_profit_share parameter
# (ARCHITECTURE section 6.1). Callers must pass the live value from the
# adaptive store once stage 5 wires it; there is deliberately no default
# on authorize() so "forgot to pass it" is unrepresentable (audit F9).
DEFAULT_GOVERNOR_PROFIT_SHARE = Decimal("0.10")


def scheduled_cap_cents(
    conn: sqlite3.Connection,
    governor_profit_share: Decimal,
    as_of: date | None = None,
    owner_monthly_cap_cents: Decimal | None = None,
) -> tuple[Decimal, str]:
    """The scheduled cap in force, and which bound set it.

    ONE SOURCE OF TRUTH, and that is the whole point of this function
    existing. The dashboard used to print BASE_CAP_CENTS while the
    governor spent against a different number, so an owner who raised
    their budget saw "$0.00 of $5.00" forever and reasonably concluded
    the setting did nothing (owner-reported 2026-08-10). Anything that
    displays the cap must call this, not the constants.

    Returns (cap, reason_suffix) where the suffix names the bound for
    the governor's audit row.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    if owner_monthly_cap_cents is not None:
        # A deliberate human decision REPLACES the base, up or down. The
        # only thing clamped here is the direction: a negative reads as
        # "stop", never as "no limit". There is no fixed upper ceiling -
        # the owner sets their own budget - but the number is sanity
        # checked where it is ENTERED, so a slipped keyboard cannot
        # become a spending limit.
        return max(owner_monthly_cap_cents, Decimal("0")), "_owner_set"
    uncapped = BASE_CAP_CENTS + (
        net_realized_profit_cents_prior_month(conn, as_of) * governor_profit_share
    )
    if uncapped > GOVERNOR_MAX_CAP_CENTS:
        return GOVERNOR_MAX_CAP_CENTS, "_hard_capped"
    return uncapped, ""


def authorize(
    estimate: CostEstimate,
    conn: sqlite3.Connection,
    governor_profit_share: Decimal,
    as_of: date | None = None,
    cycle_id: str | None = None,
    owner_monthly_cap_cents: Decimal | None = None,
) -> GovernorDecision:
    """owner_monthly_cap_cents is the number the owner typed into the
    setup page ("the bot will not go past it"). It REPLACES the base
    cap, upward or downward, with NO fixed ceiling - a person choosing
    to spend more is a decision, where a system paying itself more out
    of its own profit is a ratchet, and only the second needs a wall.
    The figure is sanity checked where it is ENTERED (first_run.py), so
    a slipped keyboard cannot arrive here as a budget.

    The cap itself is computed by scheduled_cap_cents(), which anything
    DISPLAYING the cap must also call - the dashboard printed the base
    constant while the governor spent against this number, so a raised
    budget never appeared on screen (owner-reported 2026-08-10).

    (Stress stage-8 E1: the field was collected and read by nobody,
    which made the setup page's promise false. It is read here.)"""
    as_of = as_of or datetime.now(timezone.utc).date()
    spent = month_to_date_cents(estimate.kind, conn, as_of)

    # Ledger integrity gates apply to BOTH kinds: holes or unresolved
    # discrepancies mean nothing new is authorized until a human acts.
    for check, reason in (
        (has_unpriced_rows, "unpriced_cost_rows"),
        (has_unacknowledged_discrepancy, "reconciliation_discrepancy_unacknowledged"),
    ):
        if check(conn):
            decision = GovernorDecision(
                authorized=False, kind=estimate.kind, estimate=estimate,
                cap_cents=Decimal("0"), period_to_date_cents=spent,
                shortfall_cents=None, reason=reason,
            )
            _log(decision, conn, cycle_id)
            return decision

    # THE RATE, BEFORE THE TOTAL. Checked first because it is the more
    # specific limit and the owner needs to know WHICH one stopped it:
    # "today's allowance is gone, it resumes at midnight" and "the
    # month's budget is gone, it resumes on the 1st" need completely
    # different responses, and a shared reason would give neither.
    #
    # Scheduled only. Manual spend is a human at a keyboard, already
    # bounded monthly and for the lifetime of the build, and rate-
    # limiting a person who is deliberately testing something helps
    # nobody.
    if estimate.kind == "scheduled":
        today = day_to_date_cents(estimate.kind, conn, as_of)
        rate_ceiling = daily_cap_cents(owner_monthly_cap_cents)
        if today + estimate.estimated_cents > rate_ceiling:
            decision = GovernorDecision(
                authorized=False, kind=estimate.kind, estimate=estimate,
                cap_cents=rate_ceiling, period_to_date_cents=today,
                shortfall_cents=today + estimate.estimated_cents
                - rate_ceiling,
                reason="daily_cap_exceeded",
            )
            _log(decision, conn, cycle_id)
            return decision

    if estimate.kind == "scheduled":
        cap, reason_suffix = scheduled_cap_cents(
            conn, governor_profit_share, as_of, owner_monthly_cap_cents)
        would_be = spent + estimate.estimated_cents
        if would_be > cap:
            decision = GovernorDecision(
                authorized=False, kind=estimate.kind, estimate=estimate,
                cap_cents=cap, period_to_date_cents=spent,
                shortfall_cents=would_be - cap,
                reason="cap_exceeded" + reason_suffix,
            )
        else:
            decision = GovernorDecision(
                authorized=True, kind=estimate.kind, estimate=estimate,
                cap_cents=cap, period_to_date_cents=spent,
                shortfall_cents=None,
                # NAME THE BOUND THAT APPLIED, not a fixed string. This
                # read "allowed_at_hard_cap" for EVERY authorisation
                # under any non-default cap, so an owner-set $25 budget
                # logged 20 allows as "at the hard cap" while sitting at
                # 42% of it. Found by the cost auditor; it had already
                # misled a reading of the owner's own diagnostic bundle,
                # which is precisely the cost of a wrong label on the
                # rows the money question is settled from.
                reason=None if not reason_suffix
                else "allowed" + reason_suffix,
            )
    else:
        life = lifetime_cents("manual", conn)
        monthly_room = MANUAL_SPEND_CAP_CENTS_PER_MONTH - spent
        lifetime_room = MANUAL_LIFETIME_BUDGET_CENTS - life
        would_be = estimate.estimated_cents
        if would_be > lifetime_room:
            decision = GovernorDecision(
                authorized=False, kind=estimate.kind, estimate=estimate,
                cap_cents=MANUAL_LIFETIME_BUDGET_CENTS,
                period_to_date_cents=life,
                shortfall_cents=would_be - lifetime_room,
                reason="lifetime_build_budget_exceeded",
            )
        elif would_be > monthly_room:
            decision = GovernorDecision(
                authorized=False, kind=estimate.kind, estimate=estimate,
                cap_cents=MANUAL_SPEND_CAP_CENTS_PER_MONTH,
                period_to_date_cents=spent,
                shortfall_cents=would_be - monthly_room,
                reason="cap_exceeded",
            )
        else:
            decision = GovernorDecision(
                authorized=True, kind=estimate.kind, estimate=estimate,
                cap_cents=MANUAL_SPEND_CAP_CENTS_PER_MONTH,
                period_to_date_cents=spent,
                shortfall_cents=None, reason=None,
            )
    _log(decision, conn, cycle_id)
    return decision


def _log(decision: GovernorDecision, conn: sqlite3.Connection, cycle_id: str | None) -> None:
    conn.execute(
        "INSERT INTO cost_governor_events "
        "(cycle_id, requested_kind, estimate_cents, cap_cents, decision, reason, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            cycle_id,
            decision.kind,
            str(decision.estimate.estimated_cents),
            str(decision.cap_cents),
            "allow" if decision.authorized else "deny",
            decision.reason,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
