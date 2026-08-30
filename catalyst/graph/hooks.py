"""Integration hooks between the research pipeline and the evidence
graph. Hooks ONLY — the full wiring into research/boundary.py is the
orchestrator's stage-5 work.

Same boundary guard as store.py: no imports from catalyst.risk or
catalyst.execution, and nothing returned here is shaped like a trade
instruction — assertions in, rendered context text out.

Cost posture (BUILD-BRIEF: "build rich, run cheap"): neither hook makes
a model call. research_findings_to_graph() persists structured findings
Claude ALREADY produced during a research pass it was already paid for —
graph updates are batched into that pass, never a separate pass.
graph_context_for_candidate() is a pure database read.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from catalyst.graph.store import (
    ENTITY_KINDS,
    RELIABILITY_CLASSES,
    SOURCE_CLASSES,
    Assertion,
    assert_link,
    chain_to_catalyst,
    company_key,
    neighbors,
    render_chain_for_prompt,
    upsert_entity,
    _render_assertion,
)

# Bounds on the rendered context so a densely connected name cannot
# balloon the research prompt (every token is billed).
MAX_CHAINS_IN_PROMPT = 5
MAX_LOOSE_ASSERTIONS_IN_PROMPT = 10

_CONTEXT_HEADER = (
    "Evidence graph — what earlier passes found (informational only; the "
    "graph informs judgement, it never decides a trade). Every assertion "
    "shows [source_class, reliability, asserted date]; weigh weak links "
    "(secondary_report, model_inference) accordingly."
)


def research_findings_to_graph(
    call_log_id: str,
    findings: list[dict],
    conn: sqlite3.Connection,
) -> list[Assertion]:
    """Persist the structured findings one research pass already
    produced. Called ONCE per pass with the whole batch — no extra model
    passes, no per-finding calls.

    Each finding dict:
        {
          "subject":     {"kind": ..., "canonical_key": ..., "display_name": ...},
          "predicate":   "filed" | "mentions" | "schedules" | ...,
          "object":      {same shape as subject},   # optional
          "object_date": "YYYY-MM-DD",              # optional; set when the
                                                    # finding DATES an event
          "source_class": one of store.SOURCE_CLASSES,
          "reliability":  one of store.RELIABILITY_CLASSES,
          "source_ref":   raw_events source_id,     # optional; defaults to
                                                    # "research_call:<call_log_id>"
          "asserted_at":  ISO timestamp,            # optional; defaults to now
        }

    All-or-nothing: the batch runs in one transaction, and a malformed
    finding raises ValueError naming its index with NOTHING persisted —
    a half-written chain that looks complete is worse than a loud
    failure (house rule 3: no silent zeros).
    """
    if not call_log_id or not str(call_log_id).strip():
        raise ValueError("call_log_id must be the research_calls.id of the "
                         "pass that produced these findings")
    recorded: list[Assertion] = []
    with conn:  # one transaction: commit on success, roll back on any raise
        for i, finding in enumerate(findings):
            try:
                subject = _entity_from_spec(conn, finding["subject"])
                obj_spec = finding.get("object")
                obj = _entity_from_spec(conn, obj_spec) if obj_spec else None
                recorded.append(assert_link(
                    conn,
                    subject.id,
                    finding["predicate"],
                    object_entity_id=obj.id if obj else None,
                    object_date=finding.get("object_date"),
                    source_class=_coerce_provenance(
                        finding.get("source_class"), SOURCE_CLASSES)[0],
                    reliability=_coerce_provenance(
                        finding.get("reliability"), RELIABILITY_CLASSES)[0],
                    source_ref=(finding.get("source_ref")
                                or f"research_call:{call_log_id}"),
                    asserted_at=finding.get("asserted_at"),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"finding {i} rejected ({exc}); batch rolled back, "
                    "nothing persisted"
                ) from exc
    return recorded


#: WHERE A KIND NOBODY ANTICIPATED LANDS.
#:
#: OWNER'S BUNDLE, 2026-08-26: three research calls in a day lost every
#: finding they made, to "unknown entity kind 'ticker'", "'metric'" and
#: "'equity'". The schema's kinds are the right eight, and the model
#: reaches for a synonym or for a category that is not one - which is
#: what a model will always eventually do, whatever list it is given.
#:
#: Rejecting the batch made the strictness cost the evidence it was
#: protecting: one unfamiliar word and NOTHING from that call is kept.
#: `other` exists precisely for "this does not fit a category", so an
#: unrecognised kind belongs there - with the word the model actually
#: used preserved in the display name, so nothing is silently renamed.
#:
#: Classified by the rule, not by a synonym table (house rule 7): the
#: rule is "a kind we do not model is `other`", which is true of the
#: next unfamiliar word as well as of these three.
UNKNOWN_KIND_FALLBACK = "other"


def _coerce_kind(kind: Any) -> tuple[str, str | None]:
    """(kind to store, the original if it was not one we model)."""
    text = str(kind or "").strip().lower()
    if text in ENTITY_KINDS:
        return text, None
    return UNKNOWN_KIND_FALLBACK, (text or "(missing)")


#: THE SAME PROBLEM, ONE FIELD OVER.
#:
#: Coercing `kind` stopped the "unknown entity kind" losses, and the
#: owner's next log showed the model failing on the NEIGHBOURS instead:
#:
#:   reliability 0.9 is not one of (...)          <- a number, not a class
#:   source_class 'company_filing' is not one of (...)
#:
#: Fixing one field and leaving its siblings was the mistake; a model
#: will reach for an unlisted word in any enum it is given.
#:
#: BUT THE DIRECTION IS OPPOSITE TO `other`. These two fields say how
#: much a claim should be TRUSTED, so an unrecognised value must fall to
#: the LEAST trusted class, never a middling one. `model_inference` is
#: exactly that: "this came from the model's own reasoning". Guessing
#: 'edgar_filing' for an unreadable source_class would promote a guess
#: to a primary document, which is the one direction that could make the
#: graph lie about its own evidence.
UNKNOWN_PROVENANCE_FALLBACK = "model_inference"


def _coerce_provenance(value: Any, allowed) -> tuple[str, str | None]:
    """(value to store, the original if it was not one we model)."""
    text = str(value or "").strip().lower()
    if text in allowed:
        return text, None
    return UNKNOWN_PROVENANCE_FALLBACK, (text or "(missing)")


def _entity_from_spec(conn: sqlite3.Connection, spec: Any) -> Any:
    if not isinstance(spec, dict):
        raise ValueError(f"entity spec must be a dict, got {type(spec).__name__}")
    kind, original = _coerce_kind(spec.get("kind"))
    display = spec.get("display_name")
    if original is not None:
        # NOT SILENTLY RENAMED. The word the model chose is kept where a
        # reader will see it, so "other" never hides what it really was.
        display = f"{display} [kind: {original}]" if display else f"[kind: {original}]"
    return upsert_entity(
        conn,
        kind=kind,
        canonical_key=spec.get("canonical_key"),
        display_name=display,
        first_seen_at=spec.get("first_seen_at"),
    )


def graph_context_for_candidate(candidate, conn: sqlite3.Connection) -> str | None:
    """Rendered evidence-graph context for the research prompt builder,
    or None when the graph knows nothing about this candidate (the
    prompt builder then simply omits the section — an absent section is
    honest; an empty one looks like a broken query).

    Accepts any object with a .ticker (discovery.Candidate in the live
    pipeline) — deliberately duck-typed so graph/ imports nothing from
    the pipeline modules.
    """
    ticker = getattr(candidate, "ticker", None)
    if not ticker:
        return None
    row = conn.execute(
        "SELECT id FROM graph_entities WHERE canonical_key = ?",
        (company_key(ticker),),
    ).fetchone()
    if row is None:
        return None
    entity_id = row[0]
    chains = chain_to_catalyst(conn, entity_id)
    parts = [_CONTEXT_HEADER, ""]
    if chains:
        for chain in chains[:MAX_CHAINS_IN_PROMPT]:
            parts.append(render_chain_for_prompt(chain))
        if len(chains) > MAX_CHAINS_IN_PROMPT:
            parts.append(f"({len(chains) - MAX_CHAINS_IN_PROMPT} further "
                         "chain(s) omitted for brevity)")
    else:
        loose = neighbors(conn, entity_id)
        if not loose:
            return None  # entity row exists but carries no evidence at all
        parts.append("No chain from this name reaches a dated event yet. "
                     "Assertions on record:")
        for a in loose[:MAX_LOOSE_ASSERTIONS_IN_PROMPT]:
            parts.append(f"  - {_render_assertion(a)}")
        if len(loose) > MAX_LOOSE_ASSERTIONS_IN_PROMPT:
            parts.append(f"  ({len(loose) - MAX_LOOSE_ASSERTIONS_IN_PROMPT} "
                         "more omitted)")
    return "\n".join(parts)
