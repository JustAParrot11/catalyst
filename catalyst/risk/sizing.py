"""Position sizing - the ONLY function permitted to construct a
notional_usd or qty value. HUMAN REVIEW REQUIRED.

The signature is the enforcement (ARCHITECTURE.md section 4.1 third
layer): the sole trace of the model's output that can reach this
function is passed_gate, a bool. There is no parameter shaped to
receive a ResearchView, so no future edit can read conviction,
expected_holding_days or priced_in into the arithmetic.

Sizes off max(adverse_gap_assumption[catalyst_type], nominal stop
distance) - never nominal stop distance alone, because stops do not
trigger outside regular hours (TRAPS.md).
"""

from catalyst.risk import SizingResult
from catalyst.risk.hard_bounds import HardBounds


def size(
    passed_gate: bool,
    catalyst_type: str,
    portfolio,
    params,
    hard_bounds: HardBounds,
    market,
) -> SizingResult:
    raise NotImplementedError("stage 5: sizing arithmetic lands with risk-reviewer sign-off")
