"""Evidence-graph store: entities, provenance-carrying assertions,
chain traversal toward dated events, and prompt rendering.

THE GRAPH INFORMS; IT NEVER DECIDES. Boundary guard, stated per the
project's one non-negotiable rule ("the model proposes, deterministic
code disposes"):

- Nothing in ``catalyst/graph/`` may import from ``catalyst.risk`` or
  ``catalyst.execution``, directly or indirectly.
- No function in this package returns anything shaped like a trade
  instruction. The public surface returns entities, assertions, chains
  of assertions, and rendered text — nothing with a size, side, price,
  order type, or ticker-to-buy. A traversal finding a path to a dated
  catalyst is CONTEXT handed to Claude's research prompt; Claude reads
  the chain, weighs it, returns a ResearchView; the risk engine
  disposes. No trade fires because a path exists.

Both properties are enforced by tests/test_graph.py, not just asserted
here.

Provenance is non-optional: every edge is an assertion recording which
source class asserted it (``source_class``), the exact record that did
(``source_ref`` — a raw_events source_id or "research_call:<id>"), when
(``asserted_at``), and how reliable that source class is
(``reliability``). ``assert_link`` raises on a missing or unrecognized
value; there is no code path that writes a bare edge. The render layer
prints provenance inline on every hop so weak links (secondary_report,
model_inference) are visible to Claude in the prompt text itself.

Schema lives in catalyst/storage/schema_graph.sql (SCHEMA_GRAPH_PATH) —
a separate file because schema.sql is single-session-routed; the
stage-5 orchestrator folds it in.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

SCHEMA_GRAPH_PATH = Path(__file__).parent.parent / "storage" / "schema_graph.sql"

# Keep these tuples in lockstep with the CHECK constraints in
# schema_graph.sql — tests/test_graph.py round-trips every value through
# the database to catch drift.
ENTITY_KINDS = (
    "company", "person", "filing", "agency", "event",
    "news_item", "trial", "other",
)
SOURCE_CLASSES = (
    "edgar_filing", "federal_register", "clinicaltrials",
    "openfda", "alpaca_news", "news", "model_inference",
)
RELIABILITY_CLASSES = (
    "primary_document", "official_schedule",
    "secondary_report", "model_inference",
)
# Rank for "weakest link" reporting: higher = weaker.
_RELIABILITY_RANK = {r: i for i, r in enumerate(RELIABILITY_CLASSES)}

# Traversal bounds — the graph is context, not a search engine; keep the
# prompt payload small and the walk cheap.
DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_CHAINS = 25


@dataclass(frozen=True)
class Entity:
    id: str
    kind: str
    canonical_key: str
    display_name: str
    first_seen_at: str            # ISO timestamp, TEXT convention as elsewhere


@dataclass(frozen=True)
class Assertion:
    """One provenance-carrying edge, with endpoint labels denormalized in
    so rendering needs no database connection."""

    id: str
    subject_entity_id: str
    subject_kind: str
    subject_label: str
    predicate: str
    object_entity_id: str | None
    object_kind: str | None
    object_label: str | None
    object_date: str | None       # ISO date; set = this assertion dates an event
    source_class: str
    source_ref: str
    asserted_at: str              # ISO timestamp
    reliability: str


def company_key(ticker: str) -> str:
    """The canonical_key convention for company entities, shared by the
    writers (research_findings_to_graph) and the reader
    (graph_context_for_candidate) so they always meet on the same row."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise ValueError("company_key needs a non-empty ticker")
    return f"company:{ticker}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_entity(
    conn: sqlite3.Connection,
    kind: str,
    canonical_key: str,
    display_name: str,
    first_seen_at: str | datetime | None = None,
) -> Entity:
    """Insert-or-fetch by canonical_key. Idempotent: the same
    canonical_key twice is one entity (first write wins on
    display_name/first_seen_at). A canonical_key reused with a different
    kind is a caller bug and raises rather than silently merging two
    different things into one node."""
    if kind not in ENTITY_KINDS:
        raise ValueError(f"unknown entity kind {kind!r}; expected one of {ENTITY_KINDS}")
    if not canonical_key or not str(canonical_key).strip():
        raise ValueError("canonical_key must be non-empty")
    if not display_name or not str(display_name).strip():
        raise ValueError("display_name must be non-empty")
    if isinstance(first_seen_at, datetime):
        first_seen_at = first_seen_at.isoformat()
    try:
        conn.execute(
            "INSERT INTO graph_entities"
            " (id, kind, canonical_key, display_name, first_seen_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, kind, canonical_key, display_name,
             first_seen_at or _now_iso()),
        )
    except sqlite3.IntegrityError as exc:
        # Benign only when it is the canonical_key uniqueness (the
        # idempotent re-upsert path); any other constraint failure is a
        # real defect and stays loud (see the matching note in
        # assert_link).
        if "UNIQUE constraint failed" not in str(exc):
            raise
    row = conn.execute(
        "SELECT id, kind, canonical_key, display_name, first_seen_at"
        " FROM graph_entities WHERE canonical_key = ?",
        (canonical_key,),
    ).fetchone()
    entity = Entity(*row)
    if entity.kind != kind:
        raise ValueError(
            f"canonical_key {canonical_key!r} already exists with kind "
            f"{entity.kind!r}, refusing to reuse it as {kind!r}"
        )
    return entity


def assert_link(
    conn: sqlite3.Connection,
    subject_entity_id: str,
    predicate: str,
    *,
    object_entity_id: str | None = None,
    object_date: str | date | None = None,
    source_class: str,
    reliability: str,
    source_ref: str,
    asserted_at: str | datetime | None = None,
) -> Assertion:
    """Record one edge WITH its provenance. There are no bare edges:
    source_class, reliability and source_ref are keyword-only and
    validated — a missing one raises TypeError (Python) and an
    invalid/empty one raises ValueError, before anything is written.

    Idempotent per (edge, source): re-asserting the identical link from
    the identical source is one row (see idx_graph_assertions_dedupe).
    """
    if not subject_entity_id:
        raise ValueError("subject_entity_id must be non-empty")
    if not predicate or not str(predicate).strip():
        raise ValueError("predicate must be non-empty")
    if source_class not in SOURCE_CLASSES:
        raise ValueError(
            f"provenance is non-optional: source_class {source_class!r} "
            f"is not one of {SOURCE_CLASSES}"
        )
    if reliability not in RELIABILITY_CLASSES:
        raise ValueError(
            f"provenance is non-optional: reliability {reliability!r} "
            f"is not one of {RELIABILITY_CLASSES}"
        )
    if source_class == "model_inference" and reliability != "model_inference":
        raise ValueError(
            "a model_inference source cannot claim reliability "
            f"{reliability!r} — model inferences are model_inference"
        )
    if not source_ref or not str(source_ref).strip():
        raise ValueError(
            "provenance is non-optional: source_ref must name the raw_events "
            "source_id or research_call id that asserted this link"
        )
    if object_entity_id is None and object_date is None:
        raise ValueError(
            "an assertion must point somewhere: object_entity_id, "
            "object_date, or both"
        )
    if object_date is not None:
        if isinstance(object_date, date):
            object_date = object_date.isoformat()
        else:
            object_date = date.fromisoformat(str(object_date)).isoformat()
    if isinstance(asserted_at, datetime):
        asserted_at = asserted_at.isoformat()
    asserted_at = asserted_at or _now_iso()

    assertion_id = uuid.uuid4().hex
    try:
        conn.execute(
            "INSERT INTO graph_assertions"
            " (id, subject_entity_id, predicate, object_entity_id, object_date,"
            "  source_class, source_ref, asserted_at, reliability)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (assertion_id, subject_entity_id, predicate, object_entity_id,
             object_date, source_class, source_ref, asserted_at, reliability),
        )
    except sqlite3.IntegrityError as exc:
        # Only the dedupe uniqueness is an expected, benign collision
        # (same edge re-asserted by the same source). Anything else —
        # CHECK, NOT NULL, foreign key — is a real defect and must stay
        # loud; a plain INSERT OR IGNORE would swallow those too, which
        # is exactly how a bare edge could sneak past the DB's own
        # defense-in-depth. (Found by this test file's negative control.)
        if "UNIQUE constraint failed" not in str(exc):
            raise
        # Duplicate of an existing (edge, source) — return the existing row.
        row = conn.execute(
            _ASSERTION_SELECT
            + " WHERE a.subject_entity_id = ? AND a.predicate = ?"
            "   AND COALESCE(a.object_entity_id, '') = COALESCE(?, '')"
            "   AND COALESCE(a.object_date, '') = COALESCE(?, '')"
            "   AND a.source_class = ? AND a.source_ref = ?",
            (subject_entity_id, predicate, object_entity_id, object_date,
             source_class, source_ref),
        ).fetchone()
        return Assertion(*row)
    row = conn.execute(
        _ASSERTION_SELECT + " WHERE a.id = ?", (assertion_id,)
    ).fetchone()
    return Assertion(*row)


_ASSERTION_SELECT = """
SELECT a.id, a.subject_entity_id, s.kind, s.display_name,
       a.predicate, a.object_entity_id, o.kind, o.display_name,
       a.object_date, a.source_class, a.source_ref, a.asserted_at,
       a.reliability
FROM graph_assertions a
JOIN graph_entities s ON s.id = a.subject_entity_id
LEFT JOIN graph_entities o ON o.id = a.object_entity_id
"""


def neighbors(conn: sqlite3.Connection, entity_id: str) -> list[Assertion]:
    """Every assertion touching entity_id, as subject or object, in
    deterministic order (asserted_at, then id)."""
    rows = conn.execute(
        _ASSERTION_SELECT
        + " WHERE a.subject_entity_id = ? OR a.object_entity_id = ?"
        " ORDER BY a.asserted_at, a.id",
        (entity_id, entity_id),
    ).fetchall()
    return [Assertion(*r) for r in rows]


def chain_to_catalyst(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_chains: int = DEFAULT_MAX_CHAINS,
) -> list[tuple[Assertion, ...]]:
    """Ordered chains of assertions from entity_id to a dated event.

    Depth-first walk over neighbors() in both edge directions (an
    assertion keeps its stored subject->object direction in the result;
    the walk itself is direction-agnostic, because evidence connects
    whichever way the source phrased it). An assertion with object_date
    set is a chain terminal — the dated event the chain was built
    toward. Cycle-safe by construction: the walk never revisits an
    entity already on the current path and never reuses an assertion,
    and max_depth bounds the worst case, so a cyclic graph terminates.

    Returns up to max_chains chains, shortest first (ties: earliest
    catalyst date first). An empty list means no dated event is
    reachable — the caller says so rather than showing nothing.
    """
    chains: list[tuple[Assertion, ...]] = []

    def walk(current: str, path: list[Assertion], seen: frozenset[str]) -> None:
        if len(chains) >= max_chains or len(path) >= max_depth:
            return
        used = {a.id for a in path}
        for a in neighbors(conn, current):
            if a.id in used:
                continue
            if len(chains) >= max_chains:
                return
            if a.object_date is not None:
                chains.append(tuple(path) + (a,))
                continue
            other = (a.object_entity_id if a.subject_entity_id == current
                     else a.subject_entity_id)
            if other is None or other in seen:
                continue
            walk(other, path + [a], seen | {other})

    walk(entity_id, [], frozenset({entity_id}))
    chains.sort(key=lambda c: (len(c), c[-1].object_date or "", c[-1].id))
    return chains


def _render_assertion(a: Assertion) -> str:
    """One hop, provenance inline — every assertion shows its source
    class, reliability, and date in the text Claude reads, e.g.
    'filing "8-K 2026-08-04" --schedules--> event "merger close" on
    2026-08-20  [edgar_filing, primary_document, 2026-08-04]'."""
    if a.object_entity_id is not None:
        obj = f'{a.object_kind} "{a.object_label}"'
        if a.object_date is not None:
            obj += f" on {a.object_date}"
    else:
        obj = f"date {a.object_date}"
    return (
        f'{a.subject_kind} "{a.subject_label}" --{a.predicate}--> {obj}'
        f"  [{a.source_class}, {a.reliability}, {a.asserted_at[:10]}]"
    )


def weakest_reliability(chain: tuple[Assertion, ...] | list[Assertion]) -> str:
    """A chain is only as good as its weakest assertion; name it."""
    if not chain:
        raise ValueError("empty chain has no reliability")
    return max(chain, key=lambda a: _RELIABILITY_RANK[a.reliability]).reliability


def render_chain_for_prompt(chain: tuple[Assertion, ...] | list[Assertion]) -> str:
    """Compact, human/model-readable text for one chain. Every hop
    carries its [source_class, reliability, asserted-date] marker
    inline, and the header names the chain's weakest link, so a chain
    resting on secondary_report or model_inference cannot read as
    stronger than it is."""
    if not chain:
        raise ValueError("cannot render an empty chain")
    lines = [
        f"Chain ({len(chain)} hop{'s' if len(chain) != 1 else ''}, "
        f"weakest link: {weakest_reliability(chain)}):"
    ]
    for i, a in enumerate(chain, 1):
        lines.append(f"  {i}. {_render_assertion(a)}")
    return "\n".join(lines)
