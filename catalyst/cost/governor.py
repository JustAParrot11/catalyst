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
from decimal import Decimal

from catalyst.cost import CostEstimate, GovernorDecision
from catalyst.cost.ledger import (
    lifetime_cents,
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

# HARD BOUND, and a DIFFERENT one: the most a HUMAN may deliberately
# set from the dashboard. The two ceilings above and below were one
# number until the owner asked to set the budget themselves, and they
# do different jobs:
#
#   GOVERNOR_MAX_CAP_CENTS  bounds what the SYSTEM hands itself out of
#                           its own realised profit - the anti-ratchet,
#                           so a lucky month cannot walk the cap upward.
#   OWNER_MAX_CAP_CENTS     bounds what a PERSON can choose on purpose.
#
# A human deciding to spend more is a decision; a system paying itself
# more is a ratchet, and only the first is safe to allow. $25/month is
# BUILD-BRIEF's absolute ceiling (~GBP 20), at which the strategy must
# beat roughly 30%/year merely to match holding cash - which is why the
# dashboard prints that hurdle beside the field rather than burying it.
OWNER_MAX_CAP_CENTS = Decimal("2500")

MANUAL_SPEND_CAP_CENTS_PER_MONTH = Decimal("2000")  # $20/month, human-set, never adaptive
MANUAL_LIFETIME_BUDGET_CENTS = Decimal("20000")     # the $200 one-off build budget
                                                     # (BUILD-BRIEF: "not a monthly
                                                     # allowance"; audit F7)

# Starting value for the adaptive governor_profit_share parameter
# (ARCHITECTURE section 6.1). Callers must pass the live value from the
# adaptive store once stage 5 wires it; there is deliberately no default
# on authorize() so "forgot to pass it" is unrepresentable (audit F9).
DEFAULT_GOVERNOR_PROFIT_SHARE = Decimal("0.10")


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
    cap, upward or downward, and is then clamped to OWNER_MAX_CAP_CENTS
    - a person choosing to spend more is a decision, where a system
    paying itself more out of its own profit is a ratchet, so only the
    first is allowed above the base. (Stress stage-8 E1: the field was
    collected and read by nobody, which made the setup page's promise
    false. It is read here.)"""
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

    if estimate.kind == "scheduled":
        uncapped = BASE_CAP_CENTS + (
            net_realized_profit_cents_prior_month(conn, as_of) * governor_profit_share
        )
        cap = min(uncapped, GOVERNOR_MAX_CAP_CENTS)
        reason_suffix = "_hard_capped" if uncapped > GOVERNOR_MAX_CAP_CENTS else ""
        if owner_monthly_cap_cents is not None:
            # The owner's figure REPLACES the base, up or down - it is a
            # deliberate human decision, not the system rewarding itself
            # - and is bounded by the human ceiling. A mistyped 99999
            # must not become a 99999-cent budget, and a negative must
            # read as "stop", never as "no limit".
            owner = min(max(owner_monthly_cap_cents, Decimal("0")),
                        OWNER_MAX_CAP_CENTS)
            cap = owner
            reason_suffix = "_owner_capped"
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
                reason=None if not reason_suffix else "allowed_at_hard_cap",
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
