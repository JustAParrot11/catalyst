"""Turn raw events into dated, tradeable candidates.

The as_of parameter exists for backtest point-in-time discipline: a
candidate may only be built from events that were visible at as_of.
"""

from datetime import datetime

from catalyst.data import RawEvent
from catalyst.discovery import Candidate


def build_candidates(raw_events: list[RawEvent], as_of: datetime) -> list[Candidate]:
    raise NotImplementedError("stage 4+: candidate definition is the winning strategy's")
