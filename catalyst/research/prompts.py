"""Question design and tool definitions offered to Claude.

Owner: strategy-analyst. What Claude is asked - not how its answer is
enforced (that is boundary.py, under human review).
"""

from catalyst.discovery import Candidate


def render_research_prompt(candidate: Candidate) -> str:
    raise NotImplementedError("stage 5: prompt design follows the winning strategy")


def exploration_tools() -> list[dict]:
    """Tools available during exploration turns (web search etc.)."""
    raise NotImplementedError("stage 5")
