"""Kill switches - checked once per cycle BEFORE any candidate is
evaluated. HUMAN REVIEW REQUIRED.

FAILS CLOSED: if portfolio state cannot be built from a reliable broker
read, check() returns tripped=True with
reason="portfolio_state_unreliable" - it never proceeds as "not
tripped" on a data failure (ARCHITECTURE.md section 3.2, section 9.14).
"""

from catalyst.risk import KillSwitchState
from catalyst.risk.hard_bounds import HardBounds


def check(portfolio, hard_bounds: HardBounds) -> KillSwitchState:
    raise NotImplementedError("stage 5")
