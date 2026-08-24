"""Three defects the owner's 2026-08-24 diagnostic bundle exposed.

Each one is here as a failing test first (CLAUDE.md: the bug becomes a
test before it becomes a fix), and each was invisible from the outside:
the bot looked healthy, traded, and reported nothing wrong.

1. THE HUNT HAD NEVER RUN. One ERROR in 91,330 log lines:

       File ".../orchestrator/scheduler.py", line 1014, in
       build_candidates_all
         share = Decimal(str(current_values(conn)[
       NameError: name 'Decimal' is not defined

   The module has no top-level `Decimal` import, so every hunt that came
   due raised before it started. The guard around it reported that the
   mechanically screened candidates were unaffected - true, and it never
   said that Claude's half of discovery had not happened at all. Two
   candidate sources on the dashboard, one of them dead since it shipped.

2. THE EVIDENCE GRAPH HAD NO TABLES. "no such table: graph_entities",
   every time research tried to record a finding. schema_graph.sql was
   written, and nothing ever ran it - the same shape as the logs table
   before it, where the schema, the writer and the page all existed and
   the CREATE never happened.

3. A TABLE NAME WAS REDACTED AS IF IT WERE A SECRET. `usage_by_key`
   appeared in the bundle's row counts as "[REDACTED]" because its name
   contains "key". Over-redaction is the safe direction and this is not
   a leak - but a diagnostic bundle that hides a row count makes the one
   thing it exists for harder, so the rule has to be about VALUES.

Fully offline.
"""

import inspect
import sqlite3
from pathlib import Path

import pytest

from catalyst.orchestrator import scheduler
from catalyst.storage import init_db


#: EVERY module in the package, not the one that failed. Two instances
#: of this defect were live at once - the hunt's `Decimal` and
#: `_record_origin`'s `sqlite3` - and the second was found by the check
#: written for the first. A once-a-day job inside a broad guard is
#: exactly where an unbound name survives: the exception is caught,
#: logged and shrugged off, and the pass that never ran leaves no other
#: trace. Enumerating the modules to check would repeat the mistake
#: (house rule 7).
def _all_modules():
    import pkgutil

    import catalyst

    return sorted(m.name for m in
                  pkgutil.walk_packages(catalyst.__path__, "catalyst."))


NAME_CHECKED_MODULES = _all_modules()

#: Always bound by the import machinery, in every module.
MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                  "__spec__", "__loader__", "__path__", "__builtins__"}


def _unresolvable_names(module_name):
    """Every Name this module LOADS that nothing binds where it is used.

    Classify by the rule, not by enumeration (house rule 7): this does
    not look for `Decimal`, it looks for the defect - a name read at
    runtime that no import, assignment, parameter or builtin can supply
    in the scope that reads it. `Decimal` was one instance of it.

    Deliberately permissive where Python is stricter (comprehension and
    class scopes are treated as sharing the enclosing one), so a finding
    here is always a real unbound name and never a scoping subtlety.
    """
    import ast
    import builtins
    import importlib

    mod = importlib.import_module(module_name)
    tree = ast.parse(Path(mod.__file__).read_text())
    known = set(dir(builtins)) | MODULE_DUNDERS
    bad = []
    SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

    def bindings(node):
        """Names bound DIRECTLY in this scope. A nested function's body
        is a different scope, so it is not descended into - which is the
        whole point: the shipped bug was a name imported inside one
        function and read inside another."""
        out = set()
        args = getattr(node, "args", None)
        if args is not None:
            for a in (list(args.posonlyargs) + list(args.args)
                      + list(args.kwonlyargs)):
                out.add(a.arg)
            for extra in (args.vararg, args.kwarg):
                if extra is not None:
                    out.add(extra.arg)

        def visit(n):
            for child in ast.iter_child_nodes(n):
                if isinstance(child, SCOPES):
                    out.add(getattr(child, "name", ""))
                    continue
                if isinstance(child, ast.Name) and isinstance(child.ctx,
                                                              ast.Store):
                    out.add(child.id)
                elif isinstance(child, (ast.Import, ast.ImportFrom)):
                    out.update((a.asname or a.name).split(".")[0]
                               for a in child.names)
                elif isinstance(child, ast.ExceptHandler) and child.name:
                    out.add(child.name)
                elif isinstance(child, (ast.Global, ast.Nonlocal)):
                    out.update(child.names)
                visit(child)

        visit(node)
        out.discard("")
        return out

    def check(node, enclosing):
        scope = enclosing | bindings(node)

        def visit(n):
            for child in ast.iter_child_nodes(n):
                if isinstance(child, SCOPES):
                    check(child, scope)
                    continue
                if (isinstance(child, ast.Name)
                        and isinstance(child.ctx, ast.Load)
                        and child.id not in scope
                        and child.id not in known):
                    bad.append(f"{module_name}:{child.lineno} {child.id}")
                visit(child)

        visit(node)

    check(tree, set())
    return bad


class TestTheHuntCanActuallyStart:
    """Defect 1. The hunt raised NameError before it began, so nothing
    about the hunt itself is needed to catch it - only that every name
    it reads is bound somewhere it can see."""

    @pytest.mark.parametrize("module", NAME_CHECKED_MODULES)
    def test_every_name_resolves(self, module):
        bad = _unresolvable_names(module)
        assert not bad, (
            "name(s) read at runtime that nothing binds - each is a "
            "NameError waiting for the branch that reaches it, and the "
            "owner's 2026-08-24 bundle is what one looks like after a "
            "month in production: " + "; ".join(bad))

    def test_the_check_catches_the_original_defect(self, tmp_path):
        """House rule 4. The exact shape of the bug that shipped: a name
        imported inside one function and used inside another."""
        import ast
        import sys

        mod = tmp_path / "hunt_like.py"
        mod.write_text(
            "def a():\n"
            "    from decimal import Decimal\n"
            "    return Decimal('1')\n"
            "def b():\n"
            "    return Decimal('2')\n")
        sys.path.insert(0, str(tmp_path))
        try:
            bad = _unresolvable_names("hunt_like")
        finally:
            sys.path.remove(str(tmp_path))
            sys.modules.pop("hunt_like", None)
        assert any("Decimal" in b for b in bad), (
            "the check cannot see the defect it exists for")


class TestTheEvidenceGraphHasSomewhereToWrite:
    """Defect 2."""

    def test_init_db_creates_the_graph_tables(self, tmp_path):
        conn = init_db(str(tmp_path / "fresh.db"))
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "graph_entities" in names
            assert "graph_assertions" in names
        finally:
            conn.close()

    def test_a_database_that_predates_the_graph_gains_it_on_restart(
            self, tmp_path):
        """The owner's machine has months of history and no graph tables.
        An upgrade has to add them without touching anything else."""
        path = str(tmp_path / "old.db")
        conn = init_db(path)
        conn.execute("DROP TABLE graph_assertions")
        conn.execute("DROP TABLE graph_entities")
        conn.execute(
            "INSERT INTO candidates (id, ticker, catalyst_type, "
            " catalyst_date, catalyst_date_confidence, source_event_ids, "
            " discovered_at, sector, correlation_tags) VALUES "
            " ('c1','ABC','insider_cluster','2026-08-25','confirmed','[]',"
            "  '2026-08-24T00:00:00+00:00','unknown','[]')")
        conn.commit()
        conn.close()

        conn = init_db(path)
        try:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "graph_entities" in names
            assert conn.execute(
                "SELECT COUNT(*) FROM candidates").fetchone()[0] == 1, (
                "re-running the schema must never cost existing rows")
        finally:
            conn.close()

    def test_a_research_finding_can_be_stored(self, tmp_path):
        """The end the owner actually saw fail."""
        from catalyst.graph.store import upsert_entity

        conn = init_db(str(tmp_path / "g.db"))
        try:
            ent = upsert_entity(conn, kind="company",
                                canonical_key="company:example-inc",
                                display_name="Example Inc")
            assert ent.id
            assert conn.execute(
                "SELECT COUNT(*) FROM graph_entities").fetchone()[0] == 1
        finally:
            conn.close()


class TestTheBundleRedactsValuesNotNames:
    """Defect 3. Over-redaction is the safe direction and this is not a
    leak - but a bundle that hides a row count is worse at the one job
    it has, and the rule was never meant to be about names."""

    def test_a_table_named_for_keys_still_reports_its_row_count(self):
        from catalyst.dashboard.redact import redact_obj

        got = redact_obj({"row_counts": {"usage_by_key": 12,
                                         "candidates": 301}})
        assert got["row_counts"]["usage_by_key"] == 12, (
            "a row COUNT carries no secret whatever the table is called; "
            "the owner's bundle rendered this as [REDACTED]")
        assert got["row_counts"]["candidates"] == 301

    def test_every_other_kind_of_number_survives_too(self):
        from catalyst.dashboard.redact import redact_obj

        got = redact_obj({"api_key_count": 3, "token_total": 41234,
                          "secret_ratio": 0.5, "auth_enabled": True,
                          "session_id": None})
        assert got == {"api_key_count": 3, "token_total": 41234,
                       "secret_ratio": 0.5, "auth_enabled": True,
                       "session_id": None}

    def test_a_real_credential_under_the_same_names_is_still_masked(self):
        """House rule 4 in one test: the loosening must not be a hole.

        Every one of these is a STRING under a credential-shaped name -
        which is what a credential actually looks like - and every one
        must still be gone.
        """
        from catalyst.dashboard.redact import MASK, redact_obj

        got = redact_obj({
            "anthropic_key": "sk-ant-api03-0123456789abcdef",
            "alpaca_secret": "abcdefghijklmnop",
            "dashboard_token": "7a63aa3ad448",
            "password": "hunter2",
            "credentials": {"api_key": "PKTESTKEYVALUE12345"},
            "auth": ["Bearer abcdef123456"],
        })
        for name in ("anthropic_key", "alpaca_secret", "dashboard_token",
                     "password", "credentials", "auth"):
            assert got[name] == MASK, f"{name} was not masked"

    def test_a_secret_in_free_text_is_still_caught(self):
        """The value rule only changes what happens under a
        credential-shaped NAME. Text is still scanned for the shapes of
        real credentials wherever it appears."""
        from catalyst.dashboard.redact import MASK, redact_obj

        got = redact_obj({"note": "we set it to sk-ant-api03-abcdef123456"})
        assert MASK in got["note"]
        assert "sk-ant" not in got["note"]
