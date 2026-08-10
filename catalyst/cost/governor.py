"""Pre-call spend authorization. Expected profit never authorizes spend.

Scheduled cap: base $5/month hard (BUILD-BRIEF.md), rising only by
governor_profit_share x NET realized monthly profit (floored at zero
for the month as a whole - ARCHITECTURE section 9.13). The share itself
is an adaptive parameter under the full section-6.3 regime.

Manual cap: separate, human-set, non-adaptive. "Tracked separately"
does not mean unbounded (section 7.2) - the judgement-mode backtest
routes through this branch.

Every decision - allow or deny - is written to cost_governor_events so
a skipped cycle is a recorded, explained event, never a silent one.
"""

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

from catalyst.cost import CostEstimate, GovernorDecision
from catalyst.cost.ledger import month_to_date_cents, net_realized_profit_cents_this_month
from catalyst.cost.tracker import has_unacknowledged_discrepancy

BASE_CAP_CENTS = Decimal("500")                     # $5/month, hard (BUILD-BRIEF.md)
MANUAL_SPEND_CAP_CENTS_PER_MONTH = Decimal("2000")  # $20/month, human-set, never adaptive

# Starting value for the adaptive governor_profit_share parameter
# (ARCHITECTURE section 6.1). Moves only via adaptive_params.apply()
# under minimum-sample / bounded-step / logged-evidence rules.
DEFAULT_GOVERNOR_PROFIT_SHARE = Decimal("0.10")


def authorize(
    estimate: CostEstimate,
    conn: sqlite3.Connection,
    as_of: date | None = None,
    governor_profit_share: Decimal = DEFAULT_GOVERNOR_PROFIT_SHARE,
) -> GovernorDecision:
    as_of = as_of or datetime.now(timezone.utc).date()
    spent = month_to_date_cents(estimate.kind, conn, as_of)

    if estimate.kind == "scheduled":
        cap = BASE_CAP_CENTS + (
            net_realized_profit_cents_this_month(conn, as_of) * governor_profit_share
        )
        # An unacknowledged reconciliation discrepancy pauses ALL new
        # scheduled spend until a human looks (ARCHITECTURE section 7.1).
        if has_unacknowledged_discrepancy(conn):
            decision = GovernorDecision(
                authorized=False, kind=estimate.kind, estimate=estimate,
                cap_cents=cap, period_to_date_cents=spent,
                shortfall_cents=None,
                reason="reconciliation_discrepancy_unacknowledged",
            )
            _log(decision, conn)
            return decision
    else:
        cap = MANUAL_SPEND_CAP_CENTS_PER_MONTH

    would_be = spent + estimate.estimated_cents
    if would_be > cap:
        decision = GovernorDecision(
            authorized=False, kind=estimate.kind, estimate=estimate,
            cap_cents=cap, period_to_date_cents=spent,
            shortfall_cents=would_be - cap,
            reason="cap_exceeded",
        )
    else:
        decision = GovernorDecision(
            authorized=True, kind=estimate.kind, estimate=estimate,
            cap_cents=cap, period_to_date_cents=spent,
            shortfall_cents=None, reason=None,
        )
    _log(decision, conn)
    return decision


def _log(decision: GovernorDecision, conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO cost_governor_events "
        "(cycle_id, requested_kind, estimate_cents, cap_cents, decision, reason, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            None,
            decision.kind,
            str(decision.estimate.estimated_cents),
            str(decision.cap_cents),
            "allow" if decision.authorized else "deny",
            decision.reason,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
