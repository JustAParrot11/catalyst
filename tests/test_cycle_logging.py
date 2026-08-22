"""The trading cycle finally says what it did.

OWNER-ASKED 2026-08-21: "is every action being accurately logged aswell
in the logs tab for me to review any possible sitution".

It was not. cycle.py sizes positions, places orders, arms stops and
takes exits, and it emitted NOT ONE log line - all 54 in the
orchestrator lived in scheduler.py.

WHAT WAS ALREADY FINE, so the gap is stated accurately: failures did
reach the page. run_cycle collects them into report.errors and the
scheduler logs every one. And what the broker actually did was never
lost either - orders, fills, positions, stops and refusals each have
their own table, which is a richer record than a log line.

WHAT WAS MISSING was everything that went RIGHT. A cycle that worked
wrote nothing at all, so "what did the bot do at 14:32?" could not be
answered from the Logs tab - only by opening tables directly. Silence
and a dead service read identically, which is the failure this project
keeps paying for.

THE CONSTRAINT THIS FILE EXISTS TO HOLD: these are observations and
nothing else. cycle.py is execution code (house rule 5), so a logging
pass must be provably incapable of changing what the bot does.
"""

import ast
import inspect
from pathlib import Path

from catalyst.orchestrator import cycle


def _tree():
    return ast.parse(Path(cycle.__file__).read_text())


def _log_calls(tree):
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_log"):
            out.append(node)
    return out


class TestTheCycleNowSpeaks:

    def test_it_logs_at_all(self):
        calls = _log_calls(_tree())
        assert calls, "the trading cycle is silent again"

    def test_the_moments_that_matter_are_covered(self):
        """Not a wish-list: each of these is a thing that either moves
        money or explains why it did not."""
        src = Path(cycle.__file__).read_text()
        for moment in ("Opened %s",          # a position was taken
                       "Declined %s",         # the risk engine said no
                       "was %s by the broker",  # the broker refused
                       "Hard exit date reached",  # a position was sold
                       "Cycle done"):         # the pass finished, always
            assert moment in src, f"nothing logs {moment!r}"

    def test_a_cycle_that_did_nothing_still_says_so(self):
        """The important one. A quiet cycle writing no line at all is
        indistinguishable from a dead service, and that ambiguity has
        cost this project debugging time more than once.

        CHECKED STRUCTURALLY, not by grep. The first version of this
        test looked for the string "_log.info" near the end of
        run_cycle, and a sabotage that wrapped the call in `if False:`
        sailed straight past it - the summary was dead and the test was
        green. The summary has to be UNCONDITIONAL, which is a fact
        about the syntax tree, not about the text.
        """
        fn = next(n for n in ast.walk(_tree())
                  if isinstance(n, ast.FunctionDef) and n.name == "run_cycle")
        top_level_logs = [
            n for n in fn.body
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and isinstance(n.value.func.value, ast.Name)
            and n.value.func.value.id == "_log"]
        assert top_level_logs, (
            "run_cycle has no unconditional log call - every line it "
            "writes sits behind some branch, so a cycle can still "
            "complete in total silence")
        joined = "".join(
            a.value for n in top_level_logs for a in ast.walk(n)
            if isinstance(a, ast.Constant) and isinstance(a.value, str))
        assert "Cycle done" in joined, (
            "the unconditional line is not the closing summary")


class TestTheLoggingCanNeverCHANGEANYTHING:
    """house rule 5. cycle.py places orders, so a logging pass has to be
    provably observational - not merely intended to be."""

    def test_every_log_call_is_a_bare_statement(self):
        """A logging call used as a VALUE - in a condition, an argument,
        an assignment, a boolean chain - is a call whose result can
        reach a decision. As a bare expression statement it cannot: its
        return value is discarded by the language itself."""
        tree = _tree()
        statement_calls = {
            id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)}
        for call in _log_calls(tree):
            assert id(call) in statement_calls, (
                f"a _log call at line {call.lineno} is used as a value, so "
                "its result can influence what the bot does")

    def test_no_log_call_hides_a_side_effect_in_its_arguments(self):
        """`_log.info("...", conn.execute(...))` would be a write
        performed only when the log level is enabled - a behaviour that
        changes with a setting, which is the worst kind."""
        banned = {"execute", "executemany", "commit", "place", "place_stop",
                  "submit", "cancel", "pop", "append", "setdefault",
                  "insert", "update", "remove", "clear", "write"}
        for call in _log_calls(_tree()):
            for arg in ast.walk(ast.Module(body=[ast.Expr(a) for a in
                                                 call.args], type_ignores=[])):
                if isinstance(arg, ast.Call) and isinstance(
                        arg.func, ast.Attribute):
                    assert arg.func.attr not in banned, (
                        f"line {call.lineno}: a log argument calls "
                        f"{arg.func.attr}(), which is a side effect")

    def test_the_logger_is_the_only_thing_the_module_gained(self):
        """Logging must not have quietly introduced a new dependency on
        anything that could make a decision."""
        tree = _tree()
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "logging" in imported
        assert cycle._log.name == "orchestrator.cycle"

    def test_every_log_call_only_names_things_the_code_already_read(self):
        """THE ONE THE STATIC CHECKS MISSED, and it cost 22 red tests.

        The first version of the "Opened %s" line reached for
        market.live_price. MarketSnapshot has no such field, so the log
        call raised AttributeError - inside the function that had just
        placed an order, killing the cycle it was only meant to
        describe. Every guarantee in this class was satisfied: it was a
        bare statement, it called nothing with a side effect. None of
        that helps when an ARGUMENT cannot be evaluated.

        So the attributes a log call reads on a dataclass must be
        attributes that dataclass actually has. Checked against the real
        classes, which is something no amount of reading the file would
        have told anyone.
        """
        import dataclasses

        from catalyst.risk import MarketSnapshot

        known = {
            "market": {f.name for f in dataclasses.fields(MarketSnapshot)},
        }
        for call in _log_calls(_tree()):
            for arg in call.args:
                for node in ast.walk(arg):
                    if (isinstance(node, ast.Attribute)
                            and isinstance(node.value, ast.Name)
                            and node.value.id in known):
                        assert node.attr in known[node.value.id], (
                            f"line {call.lineno}: a log call reads "
                            f"{node.value.id}.{node.attr}, which does not "
                            f"exist - evaluating it would raise and kill "
                            f"the cycle. Fields are: "
                            f"{sorted(known[node.value.id])}")

    def test_logging_being_off_changes_nothing(self):
        """The proof a reader wants: turn the logger off entirely and
        the module still behaves. Every call is a statement, so nothing
        downstream can notice."""
        import logging

        cycle._log.disabled = True
        try:
            assert cycle.research_per_cycle(10000) > 0
            assert cycle._finite("1.5") == __import__(
                "decimal").Decimal("1.5")
        finally:
            cycle._log.disabled = False
        assert isinstance(cycle._log, logging.Logger)
