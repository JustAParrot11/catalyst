"""Adaptive parameters - the only writer of adaptive state.

Every rule here is from ARCHITECTURE.md section 6.3: closed scored
outcomes only, minimum sample, asymmetric speed (tighten 3x faster than
loosen), bounded step, logged with evidence, reversible. apply() checks
proposals JOINTLY against the full live snapshot of every other
parameter, never marginally, and refuses evidence windows that overlap
the window behind this parameter's previous adjustment.

Storage: adaptive_param_log IS the source of truth. The current value of
a parameter is its latest non-reverted log row, else the default below.
There is deliberately no second "current values" table that could drift
from the log - the audit trail and the live state cannot disagree
because they are the same rows.

Parameter addressing: per-catalyst-type parameters are adjusted one leaf
at a time under a dotted name ("stop_width.insider_cluster"). Scalar
parameters ("conviction_floor") take no dot. MIN_SAMPLE_SIZE / MAX_STEP
key on the base name.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Literal

from catalyst.risk.hard_bounds import HardBounds

ADAPTIVE_PARAMETERS = [
    "conviction_floor",
    "adverse_gap_assumption",   # per catalyst_type
    "stop_width",               # per catalyst_type
    "holding_period_estimate",  # per catalyst_type
    "search_budget_allocation", # per catalyst_type
    "governor_profit_share",    # cost governor's cap-growth fraction
]

_PER_CATALYST = {
    "adverse_gap_assumption", "stop_width",
    "holding_period_estimate", "search_budget_allocation",
}

# Starting values. Every number here is an ESTIMATE pending refusal-
# tracker evidence, except holding_period_estimate for the two graded
# arms - insider_cluster and earnings_drift - whose HOLD_DAYS=12 was
# fixed before grading and measured over 2016-2026. The previous
# build's lesson (BUILD-BRIEF): these being wrong is survivable; them
# being wrong SILENTLY is not - which is what the refusal tracker and
# this module exist to fix.
# ONE CATALYST TYPE WAS TRADEABLE. MEASURED on the owner's live day:
# discovery can produce 18 kinds and the risk engine had parameters for
# `insider_cluster` alone, so 23 of 36 candidates - 64% - died on
# `unknown_catalyst_type` in evaluate.py before conviction was read.
# Some had already been paid for.
#
# That is not a strategy choice, it is a table that was never filled in,
# and it is why the bot has "a broad range of investment areas" in
# discovery and one of them in practice.
#
# ALMOST EVERY NUMBER BELOW IS AN ESTIMATE. TWO rows rest on the
# bake-off: insider_cluster (Candidate C) and earnings_drift
# (Candidate A), and in both it is the HOLD that was measured -
# HOLD_DAYS=12, fixed before grading. Their gaps and stops are still
# reasoned. The rest are reasoned from the SHAPE of the event -
# how far a name can gap when the news lands - and they exist to be
# moved by the refusal tracker, not to be believed. The brief's rule
# holds: being wrong is survivable, being wrong SILENTLY is not, which
# is why each carries its reasoning and why the dashboard now shows
# which of them has evidence behind it.
#
# SEARCH SHARES SUM TO EXACTLY 1.0. They are a share of one budget
# across catalyst types, not a per-type multiplier - a joint
# invariant in _refusal_reason() enforces it, and my first attempt
# at this table totalled 9.6 and was correctly refused. The graded
# type gets the largest share; the fastest and shallowest events get
# the least.
#
# Gap assumptions are deliberately GENEROUS on the unproven types: a
# larger assumed gap means a smaller position, so an estimate that is
# too high costs opportunity while one that is too low costs money.
_GAP = "adverse_gap_assumption"
_STOP = "stop_width"
_HOLD = "holding_period_estimate"
_SEARCH = "search_budget_allocation"

#: (gap, stop, hold days, search share, why this shape)
_CATALYST_SHAPES = {
    # GRADED. The bake-off winner; hold is measured, not assumed.
    "insider_cluster":   ("0.08", "0.10", "12", "0.12",
                          "graded 2016-2026; the only measured row here"),
    # TRUE BINARIES. The brief's own evidence: the previous build
    # measured a 60% adverse gap on these, and concluded that "edge and
    # un-sizeable risk were the same property of the same trades". A 60%
    # gap sizes the position at 0.02/0.60 = 3.3% of the account, about
    # $33 - deliberately tiny rather than excluded, because a small
    # position in a real edge accumulates a sample and a zero position
    # never does.
    "fda_decision":      ("0.60", "0.50", "5", "0.09",
                          "a true binary; the previous build MEASURED "
                          "~60% adverse gaps here"),
    "clinical_readout":  ("0.60", "0.50", "5", "0.09",
                          "a true binary, and the readout date is not "
                          "the announcement date (TRAPS.md)"),
    "distress":          ("0.25", "0.25", "10", "0.06",
                          "going-concern language; fat left tail"),
    # Scheduled, binary-ish, and the whole move lands in one print.
    "earnings":          ("0.14", "0.16", "5", "0.06",
                          "a scheduled binary; the gap IS the event"),
    "earnings_result":   ("0.12", "0.14", "5", "0.02",
                          "the print has landed - drift, not the gap"),
    # THE SECOND GRADED ARM, and the row it could not trade without.
    #
    # strategies/earnings_drift.py sets catalyst_type="earnings_drift"
    # and this table had no such key, so every candidate it produced
    # would have died on `unknown_catalyst_type` in evaluate.py - AFTER
    # its research was paid for. That is the exact failure the note at
    # the top of this table records costing 64% of one live day's
    # candidates, repeated for a brand-new arm.
    #
    # Gap and stop copy `earnings_result` because the mechanism is the
    # same one: the print has landed and what is left is drift, so the
    # gap risk is the NEXT surprise rather than this one. Those two are
    # still estimates.
    #
    # The HOLD is not an estimate. HOLD_DAYS=12 was fixed before grading
    # and is what the bake-off measured (n=84 out of sample, hit 57.1%,
    # maxDD 8.8%), so it belongs beside insider_cluster's 12 as a
    # measured row rather than a reasoned one.
    #
    # Its search share comes from `earnings_result` (0.05 -> 0.02, the
    # same mechanism ungraded) and `strategic_review` (0.06 -> 0.03, an
    # estimate with the widest tails in the table). The shares still sum
    # to exactly 1.0 - a joint invariant enforces it.
    "earnings_drift":    ("0.12", "0.14", "12", "0.06",
                          "graded 2016-2026 as bake-off Candidate A; the "
                          "hold is measured, the gap and stop copy "
                          "earnings_result's same mechanism"),
    "guidance":          ("0.12", "0.14", "8", "0.06",
                          "re-rates the forward multiple, often violently"),
    # Deal situations: outcome is known-ish, so the gap is smaller.
    "merger":            ("0.05", "0.08", "20", "0.05",
                          "a fixed ratio is mostly priced; the risk is "
                          "the deal breaking"),
    "merger_vote":       ("0.06", "0.08", "10", "0.05",
                          "the vote is usually a formality - the tail is "
                          "when it is not"),
    "asset_deal":        ("0.10", "0.12", "12", "0.05",
                          "re-rates the remaining business"),
    "contract_award":    ("0.09", "0.12", "10", "0.04",
                          "size relative to revenue is what matters"),
    # Dilution is directionally known and usually gaps DOWN.
    "dilution":          ("0.12", "0.14", "8", "0.05",
                          "offerings price at a discount; direction is "
                          "known, size is not"),
    "financing":         ("0.10", "0.12", "10", "0.05",
                          "terms decide it, and terms are in the filing"),
    # Slower re-ratings: the market takes days to agree.
    "restructuring":     ("0.12", "0.14", "15", "0.05",
                          "a slow re-rating, not a single print"),
    "strategic_review":  ("0.14", "0.16", "15", "0.03",
                          "optionality on a sale; fat tails both ways"),
    "buyback":           ("0.07", "0.10", "15", "0.03",
                          "a floor under the price rather than a jump"),
    "leadership_change": ("0.09", "0.12", "12", "0.02",
                          "sentiment, and slower than it feels"),
    "analyst_action":    ("0.07", "0.10", "5", "0.02",
                          "smallest and fastest; often already in the "
                          "price by the time it is public"),
}

DEFAULT_PARAMS: dict = {
    "conviction_floor": Decimal("0.60"),
    _GAP: {k: Decimal(v[0]) for k, v in _CATALYST_SHAPES.items()},
    _STOP: {k: Decimal(v[1]) for k, v in _CATALYST_SHAPES.items()},
    _HOLD: {k: Decimal(v[2]) for k, v in _CATALYST_SHAPES.items()},
    _SEARCH: {k: Decimal(v[3]) for k, v in _CATALYST_SHAPES.items()},
    "governor_profit_share": Decimal("0.10"),
}

#: Which catalyst types rest on a backtest rather than on reasoning.
#: The dashboard reads this so an estimate is never presented as
#: evidence - the previous build's defect was not wrong numbers, it was
#: wrong numbers that looked measured.
GRADED_CATALYST_TYPES = frozenset({"insider_cluster", "earnings_drift"})


def catalyst_shape_reason(catalyst_type: str) -> str:
    """Why this type carries the gap and hold it does, in one line."""
    shape = _CATALYST_SHAPES.get(str(catalyst_type))
    return shape[4] if shape else "no parameters recorded for this type"

# Placeholder floors pending power analysis (ARCHITECTURE.md section 6.1;
# STRATEGY-PROPOSALS.md section 3.2 argues these are too SMALL, so they
# may only be revised upward without new evidence).
MIN_SAMPLE_SIZE = {
    "conviction_floor": 30,
    "adverse_gap_assumption": 20,
    "stop_width": 20,
    "holding_period_estimate": 15,
    "search_budget_allocation": 40,
    "governor_profit_share": 20,
}

# Tightening moves 3x faster than loosening for the same evidence
# strength. Hard-coded: the system cannot adjust how fast it adjusts.
TIGHTEN_LOOSEN_RATIO = Decimal("3")

MAX_STEP = {
    "conviction_floor": Decimal("0.03"),
    "adverse_gap_assumption": Decimal("0.02"),
    "stop_width": Decimal("0.02"),
    "holding_period_estimate": Decimal("2"),   # days
    "search_budget_allocation": Decimal("0.10"),
    "governor_profit_share": Decimal("0.02"),
}

# Evidence below this significance never moves anything, in either
# direction. Human-set; not itself adaptive.
SIGNIFICANCE_FLOOR = Decimal("0.90")

# Which direction of VALUE change is the conservative ("tighten") one.
# +1: raising the value is conservative (higher conviction bar, bigger
# assumed gap / stop -> smaller position). -1: lowering is conservative
# (shorter holds, less spend).
_CONSERVATIVE_VALUE_DIRECTION = {
    "conviction_floor": 1,
    "adverse_gap_assumption": 1,
    "stop_width": 1,
    "holding_period_estimate": -1,
    "search_budget_allocation": -1,
    "governor_profit_share": -1,
}

# Absolute ranges a parameter may never leave, however the evidence
# reads. holding_period_estimate's ceiling of 21 days is the brief's
# "days to about three weeks" requirement, not a tunable.
#: THE CEILING ON THE CONVICTION FLOOR, and why it is 0.75 rather than
#: the 0.95 it used to be.
#:
#: The floor does not act alone. evaluate.py adds
#: PRICED_IN_CONVICTION_PREMIUM (0.15) for a candidate the model judged
#: already priced in, so the bar that candidate actually faces is
#: floor + premium. At a floor of 0.95 that bar is 1.10 - and conviction
#: is bounded at 1.0, so EVERY priced-in candidate is refused forever, by
#: arithmetic, no matter how good it is. The system would have been
#: incapable of the trade and nothing on the page would have said so.
#:
#: OWNER-ASKED, 2026-08-14: "i dont want it to learn and make a hard
#: limit that stops all future trades. that data may of lost a trade
#: that one time but may win another trade in the future."
#:
#: 0.75 keeps a demanding bar (the model must be clearly confident) while
#: leaving the priced-in bar at 0.90 - reachable, so the parameter can
#: still be wrong without being permanently self-sealing. The invariant
#: tying this to the premium is enforced in tests/test_conviction_ceiling.py,
#: because importing evaluate here would be circular.
CONVICTION_FLOOR_CEILING = Decimal("0.75")

PARAM_RANGE = {
    "conviction_floor": (Decimal("0.30"), CONVICTION_FLOOR_CEILING),
    "adverse_gap_assumption": (Decimal("0.02"), Decimal("0.80")),
    "stop_width": (Decimal("0.02"), Decimal("0.50")),
    "holding_period_estimate": (Decimal("1"), Decimal("21")),
    "search_budget_allocation": (Decimal("0"), Decimal("1")),
    "governor_profit_share": (Decimal("0"), Decimal("0.25")),
}


@dataclass(frozen=True)
class EvidenceSample:
    parameter: str
    trade_ids: tuple[str, ...]      # closed_trades / scored refusals only
    window_start: datetime
    window_end: datetime
    effect_size: Decimal            # sign = suggested VALUE direction
    significance: Decimal
    evidence_strength: Decimal      # derived ONLY from effect_size +
                                    # significance + sample count


@dataclass(frozen=True)
class AdjustmentProposal:
    parameter: str
    direction: Literal["tighten", "loosen"]
    old_value: Decimal
    proposed_value: Decimal
    evidence: EvidenceSample | None
    applicable: bool
    reason: str | None


@dataclass(frozen=True)
class ApplyOutcome:
    applied: bool
    parameter: str
    old_value: Decimal | None
    new_value: Decimal | None
    refusal_reason: str | None      # names the bound and by how much,
                                    # verbatim for the dashboard


@dataclass(frozen=True)
class RevertOutcome:
    reverted: bool
    parameter: str
    restored_value: Decimal | None
    reason: str | None


def _base(parameter: str) -> str:
    base, _, leaf = parameter.partition(".")
    if base not in ADAPTIVE_PARAMETERS:
        raise ValueError(f"unknown adaptive parameter: {parameter!r}")
    if base in _PER_CATALYST and not leaf:
        raise ValueError(f"{base} is per-catalyst-type; address a leaf like {base}.insider_cluster")
    if base not in _PER_CATALYST and leaf:
        raise ValueError(f"{base} is scalar; no dotted leaf allowed")
    return base


def _read_snapshot_value(snapshot: dict, parameter: str) -> Decimal:
    base, _, leaf = parameter.partition(".")
    raw = snapshot[base][leaf] if leaf else snapshot[base]
    return Decimal(str(raw))


def current_values(conn: sqlite3.Connection) -> dict:
    """Defaults overlaid with the latest non-reverted log row per
    parameter. This dict is the `params` argument evaluate() receives."""
    snapshot = {
        k: (dict(v) if isinstance(v, dict) else v)
        for k, v in DEFAULT_PARAMS.items()
    }
    rows = conn.execute(
        """SELECT parameter, new_value FROM adaptive_param_log a
           WHERE reverted_at IS NULL
             AND changed_at = (SELECT MAX(changed_at) FROM adaptive_param_log b
                               WHERE b.parameter = a.parameter
                                 AND b.reverted_at IS NULL)"""
    ).fetchall()
    for parameter, new_value in rows:
        base, _, leaf = parameter.partition(".")
        if base not in ADAPTIVE_PARAMETERS:
            continue
        if leaf:
            snapshot[base][leaf] = Decimal(new_value)
        else:
            snapshot[base] = Decimal(new_value)
    return snapshot


def _rejection(parameter, old_value, evidence, reason) -> AdjustmentProposal:
    return AdjustmentProposal(
        parameter=parameter, direction="tighten", old_value=old_value,
        proposed_value=old_value, evidence=evidence,
        applicable=False, reason=reason)


def propose_adjustment(
    parameter: str, current_value: Decimal, evidence: EvidenceSample,
) -> AdjustmentProposal:
    base = _base(parameter)

    n = len(evidence.trade_ids)
    need = MIN_SAMPLE_SIZE[base]
    if n < need:
        return _rejection(parameter, current_value, evidence,
                          f"insufficient_sample: {n} of {need} required")
    if evidence.window_start >= evidence.window_end:
        return _rejection(parameter, current_value, evidence,
                          "evidence_window_invalid: start >= end")
    if evidence.significance < SIGNIFICANCE_FLOOR:
        return _rejection(
            parameter, current_value, evidence,
            f"insufficient_significance: {evidence.significance} < {SIGNIFICANCE_FLOOR}")
    if evidence.effect_size == 0:
        return _rejection(parameter, current_value, evidence, "no_effect")

    value_direction = 1 if evidence.effect_size > 0 else -1
    direction: Literal["tighten", "loosen"] = (
        "tighten" if value_direction == _CONSERVATIVE_VALUE_DIRECTION[base]
        else "loosen")

    strength = min(max(evidence.evidence_strength, Decimal("0")), Decimal("1"))
    max_step = MAX_STEP[base]
    if direction == "loosen":
        max_step = max_step / TIGHTEN_LOOSEN_RATIO
    step = max_step * strength

    if base == "holding_period_estimate":
        # whole days, rounded TOWARD no change
        step = step.quantize(Decimal("1"), rounding=ROUND_DOWN)
    else:
        step = step.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    if step == 0:
        return _rejection(parameter, current_value, evidence,
                          "step_rounds_to_zero")

    proposed = current_value + step * value_direction
    return AdjustmentProposal(
        parameter=parameter, direction=direction, old_value=current_value,
        proposed_value=proposed, evidence=evidence,
        applicable=True, reason=None)


def _joint_check(candidate_snapshot: dict, changed: str,
                 hard_bounds: HardBounds) -> str | None:
    """Validate the WHOLE proposed snapshot, not the changed leaf alone.
    Returns a refusal string naming the bound and the margin, or None."""
    base = _base(changed)
    proposed = _read_snapshot_value(candidate_snapshot, changed)

    lo, hi = PARAM_RANGE[base]
    if proposed < lo:
        return f"range_floor:{base}: proposed {proposed} is {lo - proposed} below floor {lo}"
    if proposed > hi:
        return f"range_ceiling:{base}: proposed {proposed} is {proposed - hi} above ceiling {hi}"

    # Joint: search budget must not sum above 1 across catalyst types.
    total_search = sum(
        Decimal(str(v)) for v in candidate_snapshot["search_budget_allocation"].values())
    if total_search > 1:
        return (f"search_budget_sum: allocations total {total_search}, "
                f"{total_search - 1} above 1.0")

    # Joint worst-case sizing check per catalyst type: with the full
    # candidate snapshot (proposed leaf + every OTHER parameter's live
    # value), the loss a maximum-size position can realise must respect
    # max_loss_per_position. True by construction of sizing.size() today;
    # checked anyway so a future sizing change fails closed here rather
    # than silently compounding with a loosened parameter.
    slot_frac = Decimal("1") / Decimal(hard_bounds.max_open_positions)
    for ct in candidate_snapshot["adverse_gap_assumption"]:
        gap = Decimal(str(candidate_snapshot["adverse_gap_assumption"][ct]))
        stop = Decimal(str(candidate_snapshot["stop_width"].get(ct, gap)))
        worst_case = max(gap, stop)
        if worst_case <= 0:
            return f"worst_case_nonpositive:{ct}"
        # ROUND THE POSITION DOWN, and do it here rather than trusting
        # the division to land clean. 0.02 / 0.08 is exact; 0.02 / 0.14
        # repeats, and multiplying it back overshoots the bound by 1E-29
        # - enough to refuse a proposal that is algebraically fine. That
        # is not a hypothetical: filling in the catalyst table with gaps
        # that do not divide 0.02 evenly hit it immediately.
        #
        # Rounding DOWN is the safe direction by construction: a smaller
        # position cannot lose more. The bound is unchanged and still
        # binds - this only stops a rounding tail being read as a breach.
        notional_frac = min(
            (hard_bounds.max_loss_per_position_pct / worst_case).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN),
            slot_frac)
        joint_loss = notional_frac * worst_case
        if joint_loss > hard_bounds.max_loss_per_position_pct:
            return (f"max_loss_per_position:{ct}: joint worst-case loss "
                    f"{joint_loss} exceeds bound "
                    f"{hard_bounds.max_loss_per_position_pct} by "
                    f"{joint_loss - hard_bounds.max_loss_per_position_pct}")
    return None


def apply(
    proposal: AdjustmentProposal,
    hard_bounds: HardBounds,
    current_snapshot: dict,
    conn: sqlite3.Connection,
) -> ApplyOutcome:
    def refuse(reason: str) -> ApplyOutcome:
        return ApplyOutcome(applied=False, parameter=proposal.parameter,
                            old_value=proposal.old_value, new_value=None,
                            refusal_reason=reason)

    if not proposal.applicable:
        return refuse(f"proposal_not_applicable: {proposal.reason}")
    if proposal.evidence is None:
        return refuse("no_evidence_attached")

    base = _base(proposal.parameter)

    # Re-verify sample size and significance independently of
    # propose_adjustment (risk review F1): a hand-built proposal with
    # applicable=True must not move a parameter on n=1.
    # UNIQUE ids: one scored refusal repeated 30 times is one outcome,
    # not thirty (re-review F1 residual)
    unique_ids = set(proposal.evidence.trade_ids)
    n = len(unique_ids)
    if n < MIN_SAMPLE_SIZE[base]:
        return refuse(f"insufficient_sample: {n} unique of {MIN_SAMPLE_SIZE[base]} required")
    if proposal.evidence.significance < SIGNIFICANCE_FLOOR:
        return refuse(f"insufficient_significance: "
                      f"{proposal.evidence.significance} < {SIGNIFICANCE_FLOOR}")

    # Closed, scored outcomes ONLY - enforced, not assumed (risk review
    # F2): every evidence id must be a scored refusal's candidate or a
    # closed trade's position. Unknown ids refuse the whole proposal.
    for tid in sorted(unique_ids):
        known = conn.execute(
            """SELECT 1 FROM refusals
               WHERE candidate_id = ? AND scored_at IS NOT NULL
               UNION SELECT 1 FROM closed_trades WHERE position_id = ?""",
            (tid, tid)).fetchone()
        if known is None:
            return refuse(f"evidence_not_closed_scored_outcome: {tid!r} is "
                          "neither a scored refusal nor a closed trade")

    # Staleness: the proposal must have been computed against the value
    # that is still live.
    live = _read_snapshot_value(current_snapshot, proposal.parameter)
    if live != proposal.old_value:
        return refuse(f"stale_proposal: live value {live}, proposal built on {proposal.old_value}")

    # Bounded step, re-verified independently of propose_adjustment.
    max_step = MAX_STEP[base]
    if proposal.direction == "loosen":
        max_step = max_step / TIGHTEN_LOOSEN_RATIO
    actual_step = abs(proposal.proposed_value - proposal.old_value)
    if actual_step > max_step:
        return refuse(f"step_exceeds_bound: {actual_step} > {max_step} "
                      f"({proposal.direction})")

    # Disjoint evidence windows: this evidence must start strictly after
    # the LATEST window ever used for this parameter - reverted rows
    # included (risk review F3: if a revert hid its window, the same
    # stale evidence batch could re-fire the same adjustment forever).
    prev = conn.execute(
        """SELECT MAX(evidence_window_end) FROM adaptive_param_log
           WHERE parameter = ?""",
        (proposal.parameter,)).fetchone()
    if prev is not None and prev[0] is not None:
        prev_end = datetime.fromisoformat(prev[0])
        if proposal.evidence.window_start <= prev_end:
            return refuse(
                f"evidence_window_overlaps_previous: window starts "
                f"{proposal.evidence.window_start.isoformat()}, previous "
                f"adjustment's evidence ran to {prev_end.isoformat()}")

    # Joint check against the full snapshot with the proposal substituted.
    candidate = {
        k: (dict(v) if isinstance(v, dict) else v)
        for k, v in current_snapshot.items()
    }
    b, _, leaf = proposal.parameter.partition(".")
    if leaf:
        candidate[b][leaf] = proposal.proposed_value
    else:
        candidate[b] = proposal.proposed_value
    violation = _joint_check(candidate, proposal.parameter, hard_bounds)
    if violation is not None:
        return refuse(violation)

    ev = proposal.evidence
    conn.execute(
        """INSERT INTO adaptive_param_log
           (parameter, old_value, new_value, sample_ids,
            evidence_window_start, evidence_window_end, evidence_summary,
            changed_at, reverses_to, reverted_at)
           VALUES (?,?,?,?,?,?,?,?,?,NULL)""",
        (proposal.parameter, str(proposal.old_value),
         str(proposal.proposed_value), json.dumps(list(ev.trade_ids)),
         ev.window_start.isoformat(), ev.window_end.isoformat(),
         json.dumps({
             "direction": proposal.direction,
             "effect_size": str(ev.effect_size),
             "significance": str(ev.significance),
             "evidence_strength": str(ev.evidence_strength),
             "sample_count": len(ev.trade_ids),
             "reverses_if": "opposite-signed effect at >= significance "
                            f"floor over the next sample (auto)",
         }),
         datetime.now(timezone.utc).isoformat(),
         str(proposal.old_value)))
    conn.commit()
    return ApplyOutcome(applied=True, parameter=proposal.parameter,
                        old_value=proposal.old_value,
                        new_value=proposal.proposed_value,
                        refusal_reason=None)


def maybe_auto_revert(
    parameter: str, post_evidence: EvidenceSample, conn: sqlite3.Connection,
) -> RevertOutcome:
    """Auto-revert (ARCHITECTURE 6.3 rule 6): if the sample accumulated
    AFTER an adjustment shows the opposite effect, the adjustment is
    rolled back. Asymmetric like everything else: a loosening reverts on
    a third of the minimum sample (reverting it is a tightening); a
    tightening needs the full minimum to revert."""
    base = _base(parameter)

    row = conn.execute(
        """SELECT rowid, old_value, new_value, changed_at, evidence_summary
           FROM adaptive_param_log
           WHERE parameter = ? AND reverted_at IS NULL
           ORDER BY changed_at DESC LIMIT 1""",
        (parameter,)).fetchone()
    if row is None:
        return RevertOutcome(False, parameter, None, "nothing_to_revert")
    rowid, old_value, new_value, changed_at, summary_json = row

    changed_dt = datetime.fromisoformat(changed_at)
    if post_evidence.window_start < changed_dt:
        return RevertOutcome(
            False, parameter, None,
            "evidence_predates_adjustment: only post-change outcomes count")

    applied_direction = json.loads(summary_json)["direction"]
    need = MIN_SAMPLE_SIZE[base]
    if applied_direction == "loosen":
        need = int((Decimal(need) / TIGHTEN_LOOSEN_RATIO)
                   .quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    if len(post_evidence.trade_ids) < need:
        return RevertOutcome(
            False, parameter, None,
            f"insufficient_post_sample: {len(post_evidence.trade_ids)} of {need}")
    if post_evidence.significance < SIGNIFICANCE_FLOOR:
        return RevertOutcome(False, parameter, None,
                             "insufficient_significance")

    applied_value_sign = 1 if Decimal(new_value) > Decimal(old_value) else -1
    post_sign = (1 if post_evidence.effect_size > 0
                 else -1 if post_evidence.effect_size < 0 else 0)
    if post_sign != -applied_value_sign:
        return RevertOutcome(False, parameter, None,
                             "post_evidence_does_not_oppose_adjustment")

    conn.execute(
        "UPDATE adaptive_param_log SET reverted_at = ? WHERE rowid = ?",
        (datetime.now(timezone.utc).isoformat(), rowid))
    conn.commit()
    return RevertOutcome(True, parameter, Decimal(old_value),
                         "post_sample_opposes_adjustment")
