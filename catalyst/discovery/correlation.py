"""Concentration clustering: sector + catalyst_type + resolution-week.

risk.evaluate() consumes this, not raw ticker count, to judge whether
four biotech binaries resolving the same fortnight are one bet
(ARCHITECTURE.md section 9.7). Authored by strategy-analyst, reviewed by
risk-reviewer.
"""

from catalyst.discovery import Candidate


def cluster(candidates: list[Candidate], open_positions: list) -> dict[str, str]:
    """Returns candidate_id -> cluster_key."""
    raise NotImplementedError("stage 5: clustering keyed to the winning strategy's universe")
