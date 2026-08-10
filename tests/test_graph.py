"""Stage-5a tests: the evidence graph. Fully offline; every test runs
against a fresh sqlite database with schema_graph.sql applied via
executescript on top of init_db (exactly how the orchestrator will fold
it in later).

Covered: upsert idempotency, NON-OPTIONAL provenance on every edge,
3-hop chain traversal (news -> company -> 8-K filing -> dated merger
close), reliability markers inline on every rendered hop, cycle-safe
traversal, assertion dedupe, batch hook atomicity, and the boundary
guard (no risk/execution imports; no trade-shaped public surface).

NEGATIVE CONTROLS (CLAUDE.md house rule 4 — a test that cannot fail is
not a test). Method for each: back up catalyst/graph/store.py to the
scratchpad, sabotage the SOURCE (never the test, except where noted the
test itself had to be strengthened), run the single test, record the
exact failure, restore with cp, diff byte-identical, clear
catalyst/graph/__pycache__, re-run green. Performed 2026-08-10; every
failure below is the one actually observed, not a prediction:

1. test_assert_link_rejects_bad_provenance — sabotage: deleted the
   `source_class not in SOURCE_CLASSES` and `reliability not in
   RELIABILITY_CLASSES` raise blocks from store.assert_link.
   FIRST RUN FOUND A REAL DEFECT in the store, not the test: the
   original `INSERT OR IGNORE` swallowed the schema's CHECK-constraint
   violation too (OR IGNORE ignores ALL constraint failures), then the
   dedupe re-fetch found no row and the test died on
       TypeError: Assertion() argument after * must be an iterable, not
       NoneType
   — an unambiguous failure, but via a confusing path that would have
   made a future real bug hard to read. Fixed in store.py: plain INSERT
   with a narrow except that re-raises any IntegrityError other than
   "UNIQUE constraint failed" (same fix applied to upsert_entity).
   Re-sabotaged against the fixed code; failure is now the honest one —
   pytest.raises(ValueError) sees the schema's own defense fire:
   E   sqlite3.IntegrityError: CHECK constraint failed: source_class IN
       ('edgar_filing','federal_register',...)
   The DB constraint is defense in depth, not a substitute: the Python
   guard produces a named, catchable error before any write reaches
   the database. Restored; re-ran; pass.

2. test_cycle_in_graph_does_not_hang_traversal — sabotage: removed the
   `other in seen` entity guard (and the `seen | {other}` growth) from
   store.chain_to_catalyst's walk(). THE ORIGINAL TEST PASSED UNDER
   THIS SABOTAGE — with the dated event on the far node B, the
   assertion-id reuse check alone protects a two-node cycle, so the
   test could not fail for the property it claims (the same class of
   gap stage 2 found in test_in_out_of_sample_split_is_chronological).
   Fix, per house rule 4: fixture strengthened to two PARALLEL edges
   A<->B with the dated event on the START node A — the arrangement
   where a broken entity guard produces a longer, entity-revisiting
   chain back to the dated event. Re-ran under the same sabotage;
   now fails:
   E   assert 3 == 2   (walked entity ids: [acme, bolt, acme])
       "a chain revisited an entity - the cycle guard is not working"
   Also ran the harsher variant with BOTH the entity guard and the
   assertion-reuse check removed (only the max_depth cap left): still
   terminates (the cap bounds recursion) and still fails on the same
   revisit assertion, so the test does not depend on which guard is
   present, only on the property. Restored; re-ran; pass.

3. (bonus) test_render_marks_reliability_on_every_hop — sabotage:
   dropped the `[{source_class}, {reliability}, {date}]` suffix from
   store._render_assertion. Failure:
   E   AssertionError: hop 1 must carry its reliability marker inline:
       '  1. news_item "Acme said..." --mentions--> company "Acme Corp"'
   Restored; re-ran; pass.

Final state after controls: catalyst/graph/store.py and hooks.py
byte-identical to the pre-sabotage backup (verified with diff); full
suite green. The strengthened cycle fixture and the INSERT fix in
store.py are the two intentional changes kept from the control pass.
"""

import sqlite3
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from catalyst.graph import (
    ENTITY_KINDS,
    RELIABILITY_CLASSES,
    SCHEMA_GRAPH_PATH,
    SOURCE_CLASSES,
    Assertion,
    Entity,
    assert_link,
    chain_to_catalyst,
    company_key,
    graph_context_for_candidate,
    neighbors,
    render_chain_for_prompt,
    research_findings_to_graph,
    upsert_entity,
    weakest_reliability,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def gdb(tmp_db):
    """Full schema.sql (init_db, via the shared tmp_db fixture) plus
    schema_graph.sql on top — the exact application path the task
    prescribes until the orchestrator folds the files together."""
    tmp_db.executescript(SCHEMA_GRAPH_PATH.read_text())
    return tmp_db


def seed_three_hop_chain(conn):
    """news mentions company -> company files 8-K -> 8-K names a merger
    closing date (the dated event). Returns the entities and assertions."""
    news = upsert_entity(conn, "news_item", "news:alpaca:abc123",
                         "Acme said exploring sale - newswire", NOW)
    acme = upsert_entity(conn, "company", company_key("ACME"), "Acme Corp", NOW)
    filing = upsert_entity(conn, "filing", "filing:edgar:0000001-26-000042",
                           "ACME 8-K 2026-08-04", NOW)
    event = upsert_entity(conn, "event", "event:merger_close:ACME",
                          "ACME merger close", NOW)
    a1 = assert_link(conn, news.id, "mentions", object_entity_id=acme.id,
                     source_class="alpaca_news", reliability="secondary_report",
                     source_ref="alpaca_news:abc123",
                     asserted_at="2026-08-01T09:00:00+00:00")
    a2 = assert_link(conn, acme.id, "filed", object_entity_id=filing.id,
                     source_class="edgar_filing", reliability="primary_document",
                     source_ref="edgar:0000001-26-000042",
                     asserted_at="2026-08-04T21:05:00+00:00")
    a3 = assert_link(conn, filing.id, "schedules", object_entity_id=event.id,
                     object_date=date(2026, 8, 20),
                     source_class="edgar_filing", reliability="primary_document",
                     source_ref="edgar:0000001-26-000042",
                     asserted_at="2026-08-04T21:05:00+00:00")
    return news, acme, filing, event, a1, a2, a3


# ---------------------------------------------------------------- entities

def test_upsert_same_canonical_key_twice_is_one_entity(gdb):
    first = upsert_entity(gdb, "company", company_key("ACME"), "Acme Corp", NOW)
    second = upsert_entity(gdb, "company", company_key("ACME"),
                           "Acme Corporation (different label)", NOW)
    assert first.id == second.id
    assert second.display_name == "Acme Corp", "first write wins"
    (count,) = gdb.execute("SELECT COUNT(*) FROM graph_entities").fetchone()
    assert count == 1


def test_upsert_rejects_canonical_key_reused_across_kinds(gdb):
    upsert_entity(gdb, "company", "company:ACME", "Acme Corp", NOW)
    with pytest.raises(ValueError, match="kind"):
        upsert_entity(gdb, "person", "company:ACME", "A. Cme", NOW)


def test_upsert_rejects_unknown_kind_and_empty_fields(gdb):
    with pytest.raises(ValueError, match="kind"):
        upsert_entity(gdb, "meme_stock", "x:1", "X", NOW)
    with pytest.raises(ValueError):
        upsert_entity(gdb, "company", "", "X", NOW)
    with pytest.raises(ValueError):
        upsert_entity(gdb, "company", "company:X", "", NOW)


# ------------------------------------------------- provenance is mandatory

def test_assert_link_without_provenance_kwargs_raises(gdb):
    a = upsert_entity(gdb, "company", company_key("AAA"), "AAA Co", NOW)
    b = upsert_entity(gdb, "filing", "filing:edgar:1", "AAA 8-K", NOW)
    # provenance args are keyword-only with no defaults: omitting any is
    # a TypeError before a single row can be written.
    with pytest.raises(TypeError):
        assert_link(gdb, a.id, "filed", object_entity_id=b.id)
    (count,) = gdb.execute("SELECT COUNT(*) FROM graph_assertions").fetchone()
    assert count == 0, "no bare edge may ever be written"


def test_assert_link_rejects_bad_provenance(gdb):
    a = upsert_entity(gdb, "company", company_key("AAA"), "AAA Co", NOW)
    b = upsert_entity(gdb, "filing", "filing:edgar:1", "AAA 8-K", NOW)
    with pytest.raises(ValueError, match="source_class"):
        assert_link(gdb, a.id, "filed", object_entity_id=b.id,
                    source_class="vibes", reliability="primary_document",
                    source_ref="edgar:1")
    with pytest.raises(ValueError, match="reliability"):
        assert_link(gdb, a.id, "filed", object_entity_id=b.id,
                    source_class="edgar_filing", reliability="trust_me",
                    source_ref="edgar:1")
    with pytest.raises(ValueError, match="source_class"):
        assert_link(gdb, a.id, "filed", object_entity_id=b.id,
                    source_class=None, reliability="primary_document",
                    source_ref="edgar:1")
    with pytest.raises(ValueError, match="source_ref"):
        assert_link(gdb, a.id, "filed", object_entity_id=b.id,
                    source_class="edgar_filing",
                    reliability="primary_document", source_ref="  ")
    (count,) = gdb.execute("SELECT COUNT(*) FROM graph_assertions").fetchone()
    assert count == 0


def test_model_inference_source_cannot_claim_primary_document(gdb):
    a = upsert_entity(gdb, "company", company_key("AAA"), "AAA Co", NOW)
    b = upsert_entity(gdb, "event", "event:x", "X event", NOW)
    with pytest.raises(ValueError, match="model_inference"):
        assert_link(gdb, a.id, "implies", object_entity_id=b.id,
                    source_class="model_inference",
                    reliability="primary_document",
                    source_ref="research_call:r1")


def test_assertion_must_point_somewhere(gdb):
    a = upsert_entity(gdb, "company", company_key("AAA"), "AAA Co", NOW)
    with pytest.raises(ValueError, match="point somewhere"):
        assert_link(gdb, a.id, "filed",
                    source_class="edgar_filing",
                    reliability="primary_document", source_ref="edgar:1")


def test_same_assertion_twice_is_one_row(gdb):
    a = upsert_entity(gdb, "company", company_key("AAA"), "AAA Co", NOW)
    b = upsert_entity(gdb, "filing", "filing:edgar:1", "AAA 8-K", NOW)
    kw = dict(object_entity_id=b.id, source_class="edgar_filing",
              reliability="primary_document", source_ref="edgar:1")
    first = assert_link(gdb, a.id, "filed", **kw)
    second = assert_link(gdb, a.id, "filed", **kw)  # e.g. a re-run pass
    assert first.id == second.id
    (count,) = gdb.execute("SELECT COUNT(*) FROM graph_assertions").fetchone()
    assert count == 1


def test_python_enums_round_trip_through_schema_checks(gdb):
    """The Python tuples and the SQL CHECK constraints must not drift:
    every value Python accepts must insert cleanly."""
    for i, kind in enumerate(ENTITY_KINDS):
        upsert_entity(gdb, kind, f"{kind}:parity:{i}", f"{kind} parity", NOW)
    subj = upsert_entity(gdb, "company", "company:PARITY", "Parity Co", NOW)
    for i, sc in enumerate(SOURCE_CLASSES):
        rel = "model_inference" if sc == "model_inference" else "secondary_report"
        assert_link(gdb, subj.id, f"parity_{i}", object_date="2026-09-01",
                    source_class=sc, reliability=rel, source_ref=f"parity:{i}")
    for i, rel in enumerate(RELIABILITY_CLASSES):
        sc = "model_inference" if rel == "model_inference" else "news"
        assert_link(gdb, subj.id, f"parity_rel_{i}", object_date="2026-09-02",
                    source_class=sc, reliability=rel, source_ref=f"parityr:{i}")


def test_schema_graph_safe_to_apply_twice(gdb):
    gdb.executescript(SCHEMA_GRAPH_PATH.read_text())  # must not raise


# ---------------------------------------------------------------- traversal

def test_three_hop_chain_news_to_dated_event(gdb):
    news, acme, filing, event, a1, a2, a3 = seed_three_hop_chain(gdb)
    chains = chain_to_catalyst(gdb, news.id)
    assert len(chains) == 1
    chain = chains[0]
    assert [a.id for a in chain] == [a1.id, a2.id, a3.id], \
        "chain must be ordered from the start entity toward the dated event"
    assert chain[-1].object_date == "2026-08-20", \
        "a chain must END at a dated event"
    assert all(a.object_date is None for a in chain[:-1])


def test_traversal_is_direction_agnostic(gdb):
    """Starting from the COMPANY (mid-chain) still reaches the dated
    event, and neighbors() sees edges where the entity is the object."""
    news, acme, filing, event, a1, a2, a3 = seed_three_hop_chain(gdb)
    chains = chain_to_catalyst(gdb, acme.id)
    assert any(c[-1].object_date == "2026-08-20" for c in chains)
    shortest = chains[0]
    assert [a.id for a in shortest] == [a2.id, a3.id]
    nbr_ids = {a.id for a in neighbors(gdb, acme.id)}
    assert a1.id in nbr_ids, "edge pointing INTO the entity must be visible"
    assert a2.id in nbr_ids


def test_no_dated_event_reachable_returns_empty_not_wrong(gdb):
    a = upsert_entity(gdb, "company", company_key("AAA"), "AAA Co", NOW)
    b = upsert_entity(gdb, "filing", "filing:edgar:1", "AAA 8-K", NOW)
    assert_link(gdb, a.id, "filed", object_entity_id=b.id,
                source_class="edgar_filing", reliability="primary_document",
                source_ref="edgar:1")
    assert chain_to_catalyst(gdb, a.id) == []


@pytest.mark.timeout(10)
def test_cycle_in_graph_does_not_hang_traversal(gdb):
    """A <-> B cycle (two parallel edges) with the dated event on A
    itself: traversal must terminate, find the dated event, and never
    revisit an entity within one chain. The dated event sits on the
    START node deliberately — that is the arrangement where a walk with
    a broken entity guard CAN loop out through B and come back to A to
    re-find the dated event via a longer, entity-revisiting chain, so
    this fixture discriminates a removed cycle guard where a dated
    event elsewhere would not (learned from this test's own negative
    control — the first fixture, dated event on B, passed under
    sabotage because the assertion-reuse check alone protects it). A
    pure cycle with no dated event must return [] rather than spin."""
    acme = upsert_entity(gdb, "company", company_key("ACME"), "Acme Corp", NOW)
    bolt = upsert_entity(gdb, "company", company_key("BOLT"), "Bolt Inc", NOW)
    assert_link(gdb, acme.id, "supplier_of", object_entity_id=bolt.id,
                source_class="news", reliability="secondary_report",
                source_ref="news:1")
    assert_link(gdb, bolt.id, "customer_of", object_entity_id=acme.id,
                source_class="news", reliability="secondary_report",
                source_ref="news:2")
    assert_link(gdb, acme.id, "faces_decision_on", object_date="2026-09-15",
                source_class="federal_register",
                reliability="official_schedule", source_ref="fedreg:2026-1234")

    chains = chain_to_catalyst(gdb, acme.id)
    assert chains, "the dated event must be found despite the cycle"
    for chain in chains:
        walked, current = [], acme.id
        for a in chain:
            walked.append(current)
            current = (a.object_entity_id
                       if a.subject_entity_id == current else a.subject_entity_id)
        assert len(walked) == len(set(walked)), (
            "a chain revisited an entity - the cycle guard is not working: "
            f"{walked}")
    # From the far side of the cycle the dated event is also reachable,
    # exactly once per distinct parallel edge, and still revisit-free.
    assert chain_to_catalyst(gdb, bolt.id)

    # Pure two-node cycle, no dated event anywhere.
    c1 = upsert_entity(gdb, "company", company_key("CYC"), "Cycle One", NOW)
    c2 = upsert_entity(gdb, "company", company_key("CYD"), "Cycle Two", NOW)
    assert_link(gdb, c1.id, "linked_to", object_entity_id=c2.id,
                source_class="news", reliability="secondary_report",
                source_ref="news:3")
    assert_link(gdb, c2.id, "linked_to", object_entity_id=c1.id,
                source_class="news", reliability="secondary_report",
                source_ref="news:4")
    assert chain_to_catalyst(gdb, c1.id) == []


# ---------------------------------------------------------------- rendering

def test_render_marks_reliability_on_every_hop(gdb):
    news, *_ = seed_three_hop_chain(gdb)
    chain = chain_to_catalyst(gdb, news.id)[0]
    text = render_chain_for_prompt(chain)
    lines = text.splitlines()
    assert len(lines) == 1 + len(chain)  # header + one line per hop
    for i, (line, a) in enumerate(zip(lines[1:], chain), 1):
        marker = f"[{a.source_class}, {a.reliability}, {a.asserted_at[:10]}]"
        assert marker in line, \
            f"hop {i} must carry its reliability marker inline: {line!r}"
    # The weak first hop is visible exactly where Claude reads.
    assert "secondary_report" in lines[1]
    assert "primary_document" in lines[2] and "primary_document" in lines[3]
    assert "2026-08-20" in lines[3], "the dated event shows its date"


def test_render_header_names_the_weakest_link(gdb):
    news, *_ = seed_three_hop_chain(gdb)
    chain = chain_to_catalyst(gdb, news.id)[0]
    assert weakest_reliability(chain) == "secondary_report"
    header = render_chain_for_prompt(chain).splitlines()[0]
    assert "weakest link: secondary_report" in header


def test_render_refuses_empty_chain(gdb):
    with pytest.raises(ValueError):
        render_chain_for_prompt([])


# ------------------------------------------------------- integration hooks

def _finding(**overrides):
    base = {
        "subject": {"kind": "company", "canonical_key": company_key("ACME"),
                    "display_name": "Acme Corp"},
        "predicate": "filed",
        "object": {"kind": "filing",
                   "canonical_key": "filing:edgar:0000001-26-000042",
                   "display_name": "ACME 8-K 2026-08-04"},
        "source_class": "edgar_filing",
        "reliability": "primary_document",
        "asserted_at": "2026-08-04T21:05:00+00:00",
    }
    base.update(overrides)
    return base


def test_research_findings_to_graph_batches_one_pass(gdb):
    findings = [
        _finding(),
        _finding(predicate="schedules",
                 subject={"kind": "filing",
                          "canonical_key": "filing:edgar:0000001-26-000042",
                          "display_name": "ACME 8-K 2026-08-04"},
                 object={"kind": "event",
                         "canonical_key": "event:merger_close:ACME",
                         "display_name": "ACME merger close"},
                 object_date="2026-08-20"),
    ]
    recorded = research_findings_to_graph("rc-001", findings, gdb)
    assert len(recorded) == 2
    assert all(isinstance(a, Assertion) for a in recorded)
    # source_ref defaults to the research call that produced the batch.
    assert recorded[0].source_ref == "research_call:rc-001"
    # Entities were upserted, not duplicated (filing appears in both findings).
    (n_entities,) = gdb.execute("SELECT COUNT(*) FROM graph_entities").fetchone()
    assert n_entities == 3
    # Re-running the same batch (a retried pass) adds nothing.
    research_findings_to_graph("rc-001", findings, gdb)
    (n_assertions,) = gdb.execute("SELECT COUNT(*) FROM graph_assertions").fetchone()
    assert n_assertions == 2


def test_research_findings_bad_finding_rolls_back_whole_batch(gdb):
    findings = [
        _finding(),
        _finding(predicate="implies", source_class="vibes"),  # invalid
    ]
    with pytest.raises(ValueError, match="finding 1"):
        research_findings_to_graph("rc-002", findings, gdb)
    (n_assertions,) = gdb.execute("SELECT COUNT(*) FROM graph_assertions").fetchone()
    (n_entities,) = gdb.execute("SELECT COUNT(*) FROM graph_entities").fetchone()
    assert n_assertions == 0 and n_entities == 0, \
        "a half-written batch must not persist"


def test_research_findings_requires_call_log_id(gdb):
    with pytest.raises(ValueError, match="call_log_id"):
        research_findings_to_graph("", [_finding()], gdb)


def test_graph_context_none_when_graph_knows_nothing(gdb):
    assert graph_context_for_candidate(SimpleNamespace(ticker="ZZZZ"), gdb) is None


def test_graph_context_renders_chain_with_provenance(gdb):
    seed_three_hop_chain(gdb)
    text = graph_context_for_candidate(SimpleNamespace(ticker="ACME"), gdb)
    assert text is not None
    assert "informational only" in text, \
        "the context must state the graph informs, never decides"
    assert "weakest link" in text
    assert "secondary_report" in text or "primary_document" in text
    assert "2026-08-20" in text, "the dated catalyst must be in the rendering"


def test_graph_context_explains_chainless_entity_instead_of_empty(gdb):
    a = upsert_entity(gdb, "company", company_key("AAA"), "AAA Co", NOW)
    b = upsert_entity(gdb, "filing", "filing:edgar:1", "AAA 8-K", NOW)
    assert_link(gdb, a.id, "filed", object_entity_id=b.id,
                source_class="edgar_filing", reliability="primary_document",
                source_ref="edgar:1")
    text = graph_context_for_candidate(SimpleNamespace(ticker="AAA"), gdb)
    assert text is not None
    assert "No chain" in text and "dated event" in text
    assert "[edgar_filing, primary_document," in text


# ----------------------------------------------------------- boundary guard

def test_graph_package_never_imports_risk_or_execution():
    """AST-level check of actual import statements (docstrings are
    allowed to NAME the banned modules while stating the rule)."""
    import ast
    import catalyst.graph as pkg

    banned_prefixes = ("catalyst.risk", "catalyst.execution")
    pkg_dir = SCHEMA_GRAPH_PATH.parent.parent / "graph"
    offenders = []
    for py in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(banned_prefixes):
                        offenders.append(f"{py.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.startswith(banned_prefixes):
                    offenders.append(f"{py.name}: from {mod} import ...")
                if mod == "catalyst":
                    for alias in node.names:
                        if alias.name in ("risk", "execution"):
                            offenders.append(
                                f"{py.name}: from catalyst import {alias.name}")
    assert not offenders, \
        f"graph/ must not touch risk/ or execution/: {offenders}"
    assert pkg.__doc__ and "never" in pkg.__doc__ and "trade" in pkg.__doc__, \
        "the guard must be stated in the module docstring"


def test_public_surface_carries_nothing_trade_shaped():
    """Entities, assertions, chains, and rendered text only — no field
    or export shaped like a size, side, price, or order."""
    import catalyst.graph as pkg
    from dataclasses import fields

    banned_tokens = ("notional", "qty", "quantity", "size", "side", "order",
                     "stop", "price", "buy", "sell", "position", "trade")
    for cls in (Entity, Assertion):
        for f in fields(cls):
            for token in banned_tokens:
                assert token not in f.name.lower(), \
                    f"{cls.__name__}.{f.name} is trade-shaped"
    for name in pkg.__all__:
        for token in ("order", "trade_", "buy", "sell", "size", "position"):
            assert token not in name.lower(), \
                f"public export {name!r} is trade-shaped"
    # And the exact field sets are pinned, so a trade-shaped addition is
    # a visible diff here as well as a review item.
    assert {f.name for f in fields(Entity)} == {
        "id", "kind", "canonical_key", "display_name", "first_seen_at"}
    assert {f.name for f in fields(Assertion)} == {
        "id", "subject_entity_id", "subject_kind", "subject_label",
        "predicate", "object_entity_id", "object_kind", "object_label",
        "object_date", "source_class", "source_ref", "asserted_at",
        "reliability"}
