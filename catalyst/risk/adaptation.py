"""Running the adaptation loop. Everything under it already existed.

THE DEFECT THIS FIXES IS AN ABSENCE. `propose_adjustment`, `apply` and
`maybe_auto_revert` were built, reviewed and tested - and never called
by anything in the live path. `conviction_floor_evidence` had five
tests and no production caller at all. So the refusal tracker recorded
refusals, scored them faithfully, aggregated them into evidence, and
then the evidence went nowhere. Every threshold in the system was
frozen at the estimate it shipped with, permanently, and the dashboard
reported an adaptive system because all the parts were present.

The brief is explicit that this is the important loop:

    "The refusal tracker. Record the price whenever a candidate is
    declined, then score what it went on to do. If refused candidates
    are systematically profitable, the threshold that refused them is
    too strict - and now that is a number rather than an argument. This
    is the single most important feedback loop in the system."

It could not have been more precisely un-wired: the number was computed
and discarded.

WHAT THIS MODULE DOES NOT DO. It adds no new rule, relaxes nothing, and
decides nothing itself. Minimum sample, significance floor, bounded
step, asymmetric speed, closed-scored-outcomes-only, the joint
invariant check and the hard bounds all live below and all still
apply - `apply()` re-verifies them independently of `propose_adjustment`
on purpose. This is the wire, not the policy.

HARD BOUNDS ARE UNTOUCHED, as always. Max loss per position, total
exposure, position count and the kill switches are not adaptive
parameters and cannot be reached from here. The system may only ever
propose changing those, and a human decides.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from catalyst.risk.adaptive_params import (
    apply, current_values, maybe_auto_revert, propose_adjustment,
)
from catalyst.risk.hard_bounds import HARD_BOUNDS
from catalyst.risk.refusal_tracker import conviction_floor_evidence

_log = logging.getLogger("catalyst.adaptation")


@dataclass
class AdaptationReport:
    """What the pass did, in enough detail to put on the dashboard."""

    considered: list = field(default_factory=list)   # (parameter, outcome)
    applied: list = field(default_factory=list)      # (parameter, old, new)
    refused: list = field(default_factory=list)      # (parameter, reason)
    reverted: list = field(default_factory=list)     # (parameter, reason)
    errors: list = field(default_factory=list)

    @property
    def changed_anything(self) -> bool:
        return bool(self.applied or self.reverted)


#: Where each adaptive parameter's evidence comes from. ONLY the
#: conviction floor has a real source today, and that is stated here
#: rather than hidden: the gap, stop and holding-period parameters need
#: closed trades bucketed by catalyst type, and at a few trades a month
#: there is not yet a sample that could move them honestly. Wiring an
#: evidence function that returns noise would be worse than none - it
#: would move real thresholds on nothing.
EVIDENCE_SOURCES = {
    "conviction_floor": conviction_floor_evidence,
}


def run_adaptation_pass(conn, now: datetime | None = None,
                        hard_bounds=HARD_BOUNDS) -> AdaptationReport:
    """Gather evidence, propose, apply, and auto-revert. Once a day.

    NEVER RAISES INTO THE TRADING LOOP. Adaptation is an improvement to
    future decisions, not a duty owed to the current one; a failure here
    must not stop the bot trading today. Every failure is recorded on
    the report and logged with its traceback.
    """
    now = now or datetime.now(timezone.utc)
    report = AdaptationReport()

    try:
        live = current_values(conn)
    except Exception as exc:  # noqa: BLE001 - reporting, never fatal
        _log.exception("The live parameter values could not be read; "
                       "nothing was adapted this pass.")
        report.errors.append(f"current_values: {exc}")
        return report

    for parameter, source in sorted(EVIDENCE_SOURCES.items()):
        try:
            _adapt_one(conn, parameter, source, live, hard_bounds, now,
                       report)
        except Exception as exc:  # noqa: BLE001 - one parameter, not all
            _log.exception("Adapting %s failed; every other parameter and "
                           "all trading are unaffected.", parameter)
            report.errors.append(f"{parameter}: {exc}")

    if not report.changed_anything:
        _log.info(
            "Adaptation pass: nothing moved. %d parameter(s) considered, "
            "%d refused for want of evidence. This is the expected result "
            "for most days - at a few trades a month a meaningful sample "
            "takes months, and a system that appeared to learn faster "
            "would be fitting noise.",
            len(report.considered), len(report.refused))
    return report


def _adapt_one(conn, parameter, source, live, hard_bounds, now, report):
    """One parameter: revert first, then consider a fresh adjustment."""
    evidence = source(conn, now)
    if evidence is None:
        report.considered.append((parameter, "no_evidence"))
        report.refused.append(
            (parameter, "no scored outcomes yet - nothing has been declined "
                        "and then priced, so there is no evidence either way"))
        return

    # REVERT BEFORE PROPOSING. If the last change made things worse, the
    # honest next step is to undo it, not to stack a second change on
    # top of a first one that is already failing.
    try:
        revert = maybe_auto_revert(parameter, evidence, conn)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"{parameter} revert: {exc}")
        revert = None
    if revert is not None and revert.reverted:
        report.reverted.append((parameter, revert.reason))
        _log.warning(
            "ADAPTATION REVERTED: %s was put back to %s. The outcomes "
            "recorded since the change came out the other way, so the "
            "change is undone rather than left to run. Reason: %s",
            parameter, revert.restored_value, revert.reason)
        return                       # the value just moved; re-read next pass

    old = live.get(parameter)
    if old is None:
        report.refused.append((parameter, "not a live parameter"))
        return

    proposal = propose_adjustment(parameter, old, evidence)
    report.considered.append((parameter, "proposed" if proposal.applicable
                              else "rejected"))
    if not proposal.applicable:
        report.refused.append((parameter, proposal.reason))
        _log.info("Adaptation considered %s and did not move it: %s",
                  parameter, proposal.reason)
        return

    outcome = apply(proposal, hard_bounds, live, conn)
    if outcome.applied:
        report.applied.append(
            (parameter, str(outcome.old_value), str(outcome.new_value)))
        _log.info(
            "ADAPTATION APPLIED: %s moved from %s to %s (%s). Evidence: "
            "%d scored outcome(s) between %s and %s, effect size %s, "
            "significance %s. It reverts automatically if the outcomes "
            "recorded from here come out the other way.",
            parameter, outcome.old_value, outcome.new_value,
            proposal.direction, len(set(evidence.trade_ids)),
            evidence.window_start.date(), evidence.window_end.date(),
            evidence.effect_size, evidence.significance)
    else:
        report.refused.append((parameter, outcome.refusal_reason))
        _log.info("Adaptation proposed a change to %s and it was refused: "
                  "%s", parameter, outcome.refusal_reason)
