"""A scoped bundle must contain the evidence for its question.

OWNER-ASKED, after being told to send logs: "claude has spent money but
i dont know what its spent it on was effective and correct, do the new
logs show decisions also and chat logs also?"

Verified by running, before this was written. `scope=logic` - the button
labelled "Decisions & logic", offered for "what the model concluded and
what the risk engine did" - contained NONE of it:

    --- scope=logic
       no   the prompt the model saw
       no   raw model response (the chat log)
       no   what the model concluded
       no   which limit bound, and by how much

Two separate faults produced that, and both are pinned here.

1. SCOPED BUNDLES CARRIED COUNTS, NOT ROWS. "research_views: 1" answers
   no question anyone would send a bundle to ask. Only `everything`
   carried data, so the five scoped buttons were decoration.

2. FIVE OF THE NAMED TABLES DID NOT EXIST. `token_prices`,
   `adaptive_params`, `edgar_filings_seen`, `graph_nodes`, `graph_edges`
   - all plausible, none real. The filter simply never matched them, so
   the rate table, the adaptive log and the WHOLE evidence graph were
   silently absent from the bundles that advertised them. A typo cost
   the entire contents of a scope and nothing anywhere said so.

The defence against a recurrence is not care. It is that the names are
checked against the shipped schema by a test, and that a name the
database does not have is DECLARED in the bundle rather than dropped.
"""

import json
import pathlib
import sqlite3

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard.server import DIAGNOSTIC_SCOPES, diagnostics_bundle

SCHEMA_FILES = ("catalyst/storage/schema.sql",
                "catalyst/storage/schema_graph.sql",
                "catalyst/dashboard/schema_logs.sql")


def _schema_tables() -> set:
    """Read the SHIPPED schema, not a fixture. A fixture that happens to
    omit a table would let a wrong name through."""
    conn = sqlite3.connect(":memory:")
    root = pathlib.Path(__file__).resolve().parents[1]
    for f in SCHEMA_FILES:
        conn.executescript((root / f).read_text())
    return {r[0] for r in
            conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture
def loaded(tmp_path):
    """A database with one of everything the logic scope claims to carry,
    each marked with a sentinel so its presence is unambiguous."""
    p = str(tmp_path / "loaded.db")
    conn = sqlite3.connect(p)
    root = pathlib.Path(__file__).resolve().parents[1]
    for f in SCHEMA_FILES:
        conn.executescript((root / f).read_text())
    conn.execute(
        "INSERT INTO candidates VALUES ('c1','ABCD','insider_cluster',"
        "'2026-08-20','confirmed','[\"e1\"]','2026-08-14T00:00:00+00:00',"
        "'tech','[\"tech\"]')")
    conn.execute(
        "INSERT INTO research_calls VALUES ('rc1','c1','claude-sonnet-5',"
        "'PROMPT-SENTINEL','[]','12.5',900,NULL,'2026-08-14T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO research_call_turns VALUES ('rc1',0,"
        "'{\"content\":[{\"text\":\"REPLY-SENTINEL\"}]}',"
        "'{\"cache_read_input_tokens\":4}','tool_use')")
    conn.execute(
        "INSERT INTO research_views VALUES ('c1','long',0.72,'THESIS-SENTINEL',"
        "'inv',10,0,'not priced')")
    conn.execute(
        "INSERT INTO risk_decisions VALUES ('rd1','c1','skip',NULL,NULL,NULL,"
        "NULL,NULL,'[\"RISK-SENTINEL\"]','{}','2026-08-14T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO limit_applications VALUES ('rd1','LIMIT-SENTINEL','80',"
        "'120','hard',1)")
    conn.execute(
        "INSERT INTO adaptive_param_log VALUES ('conviction_floor','0.65',"
        "'0.60','[\"t1\"]','2026-07-01','2026-08-01','ADAPTIVE-SENTINEL',"
        "'2026-08-14T00:00:00+00:00','0.65',NULL)")
    conn.execute(
        "INSERT INTO graph_entities VALUES ('g1','company','company:ABCD',"
        "'GRAPH-SENTINEL','2026-08-14T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO pricing_overrides VALUES ('po1','claude-sonnet-5',"
        "'2026-09-01','300','1500','owner','2026-08-14T00:00:00+00:00',"
        "'RATE-SENTINEL')")
    conn.commit()
    conn.close()
    return p


def _blob(path, scope):
    return json.dumps(diagnostics_bundle(Db(path), scope=scope), default=str)


class TestEveryNamedTableIsReal:
    """FAULT 2. A name that does not exist costs a scope its contents."""

    @pytest.mark.parametrize("scope", sorted(DIAGNOSTIC_SCOPES))
    def test_the_scope_names_only_tables_the_schema_defines(self, scope):
        wanted = DIAGNOSTIC_SCOPES[scope]["tables"]
        if wanted is None:
            return
        missing = sorted(set(wanted) - _schema_tables())
        assert not missing, (
            f"scope {scope!r} names {missing}, which the shipped schema "
            "does not define. The filter will never match them, so that "
            "part of the bundle is silently empty.")

    def test_a_table_the_database_lacks_is_DECLARED_not_dropped(self, loaded):
        """Belt and braces for the case a test cannot catch: an older
        database, mid-migration, that genuinely lacks a table."""
        conn = sqlite3.connect(loaded)
        conn.execute("DROP TABLE research_call_turns")
        conn.commit()
        conn.close()
        b = diagnostics_bundle(Db(loaded), scope="logic")
        assert "research_call_turns" in b.get("scope_tables_absent", {}), (
            "a named table this database does not have vanished without "
            "comment - the reader cannot tell missing from empty")


class TestTheLogicBundleCarriesTheConversation:
    """FAULT 1, and the owner's actual question."""

    def test_it_contains_the_prompt_the_model_saw(self, loaded):
        assert "PROMPT-SENTINEL" in _blob(loaded, "logic")

    def test_it_contains_the_models_reply_VERBATIM(self, loaded):
        """The chat log. Without it you have what the bot decided but not
        what it was told - the half that separates a wrong thesis from an
        unlucky one."""
        assert "REPLY-SENTINEL" in _blob(loaded, "logic")

    def test_it_contains_the_verbatim_usage_object(self, loaded):
        """TRAPS.md: cache tokens are billed and are not in input_tokens.
        Reading named fields is how a rename prices itself at zero."""
        assert "cache_read_input_tokens" in _blob(loaded, "logic")

    def test_it_contains_what_the_model_concluded(self, loaded):
        assert "THESIS-SENTINEL" in _blob(loaded, "logic")

    def test_it_contains_what_the_risk_engine_did_and_which_limit_bound(
            self, loaded):
        blob = _blob(loaded, "logic")
        assert "RISK-SENTINEL" in blob
        assert "LIMIT-SENTINEL" in blob, (
            "which rule bound, and by how much, is the risk engine's own "
            "explanation of the size it chose")

    def test_it_contains_adaptive_changes_and_their_evidence(self, loaded):
        assert "ADAPTIVE-SENTINEL" in _blob(loaded, "logic")


class TestEachScopeCarriesItsOwnEvidenceAndNotTheRest:
    """The point of narrowing. If every scope carried everything there
    would be one button, and if none carried anything there would be no
    reason to press one."""

    def test_pricing_carries_the_rate_table(self, loaded):
        assert "RATE-SENTINEL" in _blob(loaded, "pricing")

    def test_data_carries_the_evidence_graph(self, loaded):
        assert "GRAPH-SENTINEL" in _blob(loaded, "data")

    def test_pricing_does_not_drag_in_the_research_conversation(self, loaded):
        assert "REPLY-SENTINEL" not in _blob(loaded, "pricing")

    def test_data_does_not_drag_in_the_rate_table(self, loaded):
        assert "RATE-SENTINEL" not in _blob(loaded, "data")

    def test_everything_still_carries_all_of_it(self, loaded):
        blob = _blob(loaded, "everything")
        for s in ("PROMPT-SENTINEL", "REPLY-SENTINEL", "THESIS-SENTINEL",
                  "RISK-SENTINEL", "LIMIT-SENTINEL", "ADAPTIVE-SENTINEL",
                  "GRAPH-SENTINEL", "RATE-SENTINEL"):
            assert s in blob, f"{s} missing from the full dump"

    def test_the_overview_is_still_counts_only(self, loaded):
        """It is the one that is deliberately a summary, and it must stay
        distinguishable from the others."""
        b = diagnostics_bundle(Db(loaded), scope="all")
        assert "rows" not in b
        assert b["row_counts"]


class TestTheButtonsAreWhereSomeoneWouldLook:
    """OWNER-REPORTED: "I dont see different download log buttons, are
    you sure theyre in main." They were - on /maintenance only. The owner
    went to Logs, which had a single unlabelled link."""

    def test_the_logs_page_offers_every_scope(self, loaded):
        from catalyst.dashboard import panels

        html_out = panels.logs_panel(Db(loaded), {})
        for key in DIAGNOSTIC_SCOPES:
            assert f"scope={key}" in html_out, (
                f"the Logs page does not offer {key}")

    def test_the_logs_page_offers_them_even_with_no_logs_table(self, tmp_path):
        """A missing logs table is precisely when the evidence needs
        sending on."""
        from catalyst.dashboard import panels

        p = str(tmp_path / "nologs.db")
        sqlite3.connect(p).close()
        html_out = panels.logs_panel(Db(p), {})
        assert "scope=everything" in html_out

    def test_maintenance_still_offers_them_too(self, loaded):
        from catalyst.dashboard import maintenance, panels

        html_out = panels.maintenance_panel(
            maintenance.build_report(Db(loaded), None, run_active=False))
        for key in DIAGNOSTIC_SCOPES:
            assert f"scope={key}" in html_out

    def test_they_download_rather_than_opening_in_a_tab(self, loaded):
        """OWNER-REPORTED: "The download button just opens the text in a
        new tab and doesnt start a download". The bare link on the Logs
        page had no download attribute at all."""
        from catalyst.dashboard import panels

        html_out = panels.logs_panel(Db(loaded), {})
        assert 'download="catalyst-everything.json"' in html_out
        assert 'href="/diagnostics.json"' not in html_out, (
            "the old scopeless link is still there and still opens in a tab")
