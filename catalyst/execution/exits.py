"""Time-based and stop-triggered exits. HUMAN REVIEW REQUIRED.

Every position carries a hard exit date set when it is opened; if the
thesis has not played out by then, the position closes regardless
(BUILD-BRIEF.md trading behaviour requirements).
"""

from datetime import datetime


def manage_exits(portfolio, as_of: datetime) -> list:
    raise NotImplementedError("stage 5")
