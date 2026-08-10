"""The single gate every candidate passes through. HUMAN REVIEW REQUIRED.

Reads exactly one field of the ResearchView for anything that reaches
sizing: conviction, compared against the adaptive conviction floor to
produce passed_gate (a bool). Everything else in the view is audit
trail (ARCHITECTURE.md section 4.3).
"""

from catalyst.discovery import Candidate
from catalyst.research.schema import ResearchView
from catalyst.risk import RiskDecision


def evaluate(candidate: Candidate, view: ResearchView, portfolio, params) -> RiskDecision:
    raise NotImplementedError("stage 5")
