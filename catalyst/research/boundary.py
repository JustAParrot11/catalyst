"""The model/code boundary. HUMAN REVIEW REQUIRED on any change.

investigate() is the ONLY place in the system that can spend money on a
model call, and the only code path that produces a ResearchView. It runs
zero or more exploration turns (tool_choice=auto) then exactly one
extraction turn with tool_choice forced to submit_research_view
(ARCHITECTURE.md section 4.2). Every turn is authorized by the cost
governor before it is made and priced after (section 7.3).

No Anthropic API key is present in the build environment; this module is
written against the documented API shapes and exercised offline through
a stub transport injected by tests.
"""

from catalyst.discovery import Candidate
from catalyst.research.schema import ResearchCallLog


def investigate(candidate: Candidate, cost_context) -> ResearchCallLog:
    raise NotImplementedError("stage 5: built with the winning strategy's prompts")
