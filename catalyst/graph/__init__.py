"""Evidence graph (stage 5a): findings persisted as connected records —
entities, events, dates, and the sources asserting them — so later
research can query what earlier passes found, follow connections, and
build a chain toward a dated catalyst.

BOUNDARY GUARD — the project's one non-negotiable rule, applied here:

- Nothing in ``catalyst/graph/`` imports from ``catalyst.risk`` or
  ``catalyst.execution``.
- No function in this package returns anything shaped like a trade
  instruction. The public surface below returns entities, assertions,
  chains of assertions, and rendered text ONLY — no sizes, sides,
  prices, order types, or buy/sell verdicts.
- The graph INFORMS Claude's judgement; it never replaces it. No trade
  fires because a traversal found a path: chains are rendered into the
  research prompt, Claude weighs them and returns a ResearchView, and
  the deterministic risk engine disposes, exactly as everywhere else.

Every edge is an assertion with provenance (source_class, source_ref,
asserted_at, reliability) — there are no bare edges — and every rendered
hop shows that provenance inline so Claude sees the weak links.

tests/test_graph.py enforces both guard properties mechanically.
"""

from catalyst.graph.hooks import (
    graph_context_for_candidate,
    research_findings_to_graph,
)
from catalyst.graph.store import (
    ENTITY_KINDS,
    RELIABILITY_CLASSES,
    SCHEMA_GRAPH_PATH,
    SOURCE_CLASSES,
    Assertion,
    Entity,
    assert_link,
    chain_to_catalyst,
    company_key,
    neighbors,
    render_chain_for_prompt,
    upsert_entity,
    weakest_reliability,
)

__all__ = [
    "ENTITY_KINDS",
    "RELIABILITY_CLASSES",
    "SCHEMA_GRAPH_PATH",
    "SOURCE_CLASSES",
    "Assertion",
    "Entity",
    "assert_link",
    "chain_to_catalyst",
    "company_key",
    "graph_context_for_candidate",
    "neighbors",
    "render_chain_for_prompt",
    "research_findings_to_graph",
    "upsert_entity",
    "weakest_reliability",
]
