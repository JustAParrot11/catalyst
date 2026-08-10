-- Evidence graph (stage 5a) — findings persisted as connected records:
-- entities, dated events, and the sources asserting the links between
-- them, so later research can query what earlier passes found and build
-- a chain toward a dated catalyst.
--
-- SEPARATE FILE ON PURPOSE: catalyst/storage/schema.sql is routed
-- through a single coordinating session (CLAUDE.md "Avoiding
-- collisions"); the stage-5 orchestrator folds this file into it during
-- integration. Until then callers apply it explicitly on top of
-- init_db() (see catalyst/graph/store.py SCHEMA_GRAPH_PATH; tests do
-- conn.executescript(SCHEMA_GRAPH_PATH.read_text())). Safe to apply
-- twice, like everything else in storage/.
--
-- Design rule: there are NO bare edges. Every edge is an assertion
-- carrying provenance — which source class asserted it, a reference to
-- the raw record or research call that did, when, and how reliable that
-- source class is. Claude must be able to see the weak links, so the
-- render layer prints provenance inline on every hop.

CREATE TABLE IF NOT EXISTS graph_entities (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN
                      ('company','person','filing','agency','event',
                       'news_item','trial','other')),
    canonical_key TEXT NOT NULL UNIQUE,  -- "kind:natural-id", e.g. "company:ACME",
                                         -- "person:cik:0001234567",
                                         -- "filing:edgar:0001234567-26-000123"
    display_name  TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_graph_entities_kind
    ON graph_entities (kind);

CREATE TABLE IF NOT EXISTS graph_assertions (
    id                TEXT PRIMARY KEY,
    subject_entity_id TEXT NOT NULL REFERENCES graph_entities(id),
    predicate         TEXT NOT NULL,     -- "mentions", "filed", "schedules", ...
    object_entity_id  TEXT REFERENCES graph_entities(id),
    object_date       TEXT,              -- ISO date; set when this assertion
                                         -- DATES an event — the chain terminal
                                         -- chain_to_catalyst() traverses toward.
                                         -- Incidental dates (a filing's own
                                         -- date) belong in asserted_at, not here.
    source_class      TEXT NOT NULL CHECK (source_class IN
                          ('edgar_filing','federal_register','clinicaltrials',
                           'openfda','alpaca_news','news','model_inference')),
    source_ref        TEXT NOT NULL,     -- the raw_events source_id or
                                         -- "research_call:<research_calls.id>"
                                         -- that asserted this. Never blank.
    asserted_at       TEXT NOT NULL,
    reliability       TEXT NOT NULL CHECK (reliability IN
                          ('primary_document','official_schedule',
                           'secondary_report','model_inference')),
    -- An assertion must point somewhere: another entity, a date, or both.
    CHECK (object_entity_id IS NOT NULL OR object_date IS NOT NULL),
    -- A model inference can never masquerade as a primary document.
    CHECK (source_class != 'model_inference' OR reliability = 'model_inference')
);

-- Traversal patterns actually implemented (catalyst/graph/store.py):
-- neighbors() reads assertions by either endpoint; chain_to_catalyst()
-- walks neighbors() and terminates on dated assertions.
CREATE INDEX IF NOT EXISTS idx_graph_assertions_subject
    ON graph_assertions (subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_graph_assertions_object
    ON graph_assertions (object_entity_id) WHERE object_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_assertions_dated
    ON graph_assertions (object_date) WHERE object_date IS NOT NULL;

-- Re-running a research pass must not duplicate the graph: the same
-- (edge, source) pair is one row however many times it is re-asserted.
-- asserted_at is deliberately excluded so a batched re-run is idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_assertions_dedupe
    ON graph_assertions (subject_entity_id, predicate,
                         COALESCE(object_entity_id, ''),
                         COALESCE(object_date, ''),
                         source_class, source_ref);
