"""Stage-1 scaffold tests: the interface contract is importable, the
schema initializes, the boundary object cannot carry a size, and the
offline guard actually guards.
"""

import ast
import dataclasses
import socket
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest


def test_all_modules_import():
    import catalyst.backtest.harness
    import catalyst.cost.governor
    import catalyst.cost.ledger
    import catalyst.cost.tracker
    import catalyst.data.normalize
    import catalyst.discovery.candidates
    import catalyst.discovery.correlation
    import catalyst.execution.broker
    import catalyst.execution.exits
    import catalyst.execution.orders
    import catalyst.execution.reconcile
    import catalyst.orchestrator.cycle
    import catalyst.research.boundary
    import catalyst.research.prompts
    import catalyst.risk.adaptive_params
    import catalyst.risk.evaluate
    import catalyst.risk.kill_switches
    import catalyst.risk.sizing  # noqa: F401


def test_schema_initializes_and_has_every_architecture_table(tmp_db):
    expected = {
        "raw_events", "raw_events_errors", "candidates",
        "research_calls", "research_call_turns", "research_views",
        "risk_decisions", "limit_applications", "refusals",
        "kill_switch_events", "adaptive_param_log",
        "orders", "stop_replacements", "stop_confirmations",
        "fills", "positions", "closed_trades",
        "cost_events", "cost_governor_events", "cost_reconciliation_events",
        "backtest_results", "backtest_sample_stats",
    }
    rows = tmp_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    actual = {r[0] for r in rows}
    missing = expected - actual
    assert not missing, f"schema.sql is missing tables from ARCHITECTURE.md section 5: {missing}"


def test_research_view_structurally_cannot_carry_a_size():
    """The model/code boundary (ARCHITECTURE.md section 4.1): the field
    set is frozen and none of it is shaped like money or quantity. If
    this test fails, someone widened the boundary object - that change
    requires human review, and this test is the tripwire."""
    from catalyst.research.schema import ResearchView

    field_names = {f.name for f in dataclasses.fields(ResearchView)}
    assert field_names == {
        "candidate_id", "direction", "conviction", "thesis",
        "invalidation", "expected_holding_days", "priced_in",
        "priced_in_reasoning",
    }
    forbidden_fragments = ("size", "qty", "quantity", "notional", "usd",
                           "dollar", "shares", "order", "stop", "price_target")
    for name in field_names:
        for frag in forbidden_fragments:
            assert frag not in name.lower(), (
                f"ResearchView.{name} looks size/order-shaped; the boundary "
                "object must not carry one (ARCHITECTURE.md section 4.1)"
            )


def test_research_view_tool_schema_matches_dataclass():
    """The forced tool schema and the dataclass must never drift apart.

    ONE deliberate exception, and the test states it rather than
    loosening: `findings` is evidence for the graph, not part of the
    view. It is offered on the tool, is never required, and must NOT
    exist on the dataclass the risk engine reads - because everything on
    that object is something sizing is allowed to see, and evidence is
    not (CLAUDE.md: the model never sizes a position).
    """
    from catalyst.research.schema import (
        _NON_VIEW_FIELDS, SUBMIT_RESEARCH_VIEW_TOOL, ResearchView,
    )

    schema = SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]
    schema_fields = set(schema["properties"])
    dataclass_fields = {f.name for f in dataclasses.fields(ResearchView)} - {"candidate_id"}
    assert schema_fields - _NON_VIEW_FIELDS == dataclass_fields
    assert _NON_VIEW_FIELDS <= schema_fields
    assert not (_NON_VIEW_FIELDS & dataclass_fields), (
        "a non-view field reached the object sizing reads")
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == dataclass_fields
    assert not (_NON_VIEW_FIELDS & set(schema["required"])), (
        "evidence must never be required - a view without it is valid")


def test_sizing_signature_cannot_receive_a_research_view():
    """The third enforcement layer (ARCHITECTURE.md section 4.1): the
    only model-derived parameter sizing accepts is a bool gate."""
    import inspect

    from catalyst.risk.sizing import size

    params = inspect.signature(size).parameters
    assert "passed_gate" in params
    assert "view" not in params
    assert "research_view" not in params
    assert "conviction" not in params


def test_hard_bounds_are_frozen():
    from catalyst.risk.hard_bounds import HARD_BOUNDS

    with pytest.raises(dataclasses.FrozenInstanceError):
        HARD_BOUNDS.max_loss_per_position_pct = Decimal("1.0")  # type: ignore[misc]


def test_adaptive_params_module_has_no_writable_path_to_hard_bounds():
    """Structural check from ARCHITECTURE.md section 6.2 layer 1: the
    adaptive-params source must never reference the HARD_BOUNDS constant
    (read-only bounds arrive as an explicit function argument instead)."""
    import inspect

    import catalyst.risk.adaptive_params as ap

    source = inspect.getsource(ap)
    assert "HARD_BOUNDS" not in source.replace("HardBounds", "")


def test_governor_base_cap_is_five_dollars():
    """BUILD-BRIEF.md: base cap $5/month, hard."""
    from catalyst.cost.governor import BASE_CAP_CENTS

    assert BASE_CAP_CENTS == Decimal("500")


def test_network_guard_blocks_sockets():
    """The offline contract is enforced, not asserted."""
    with pytest.raises(RuntimeError, match="fully offline"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("example.com", 443))


def test_credentials_stripped_from_test_environment():
    import os

    leaked = [v for v in os.environ if v.startswith(("ALPACA", "APCA", "ANTHROPIC"))]
    assert leaked == [], f"credential env vars visible inside tests: {leaked}"


def test_dataclasses_round_trip():
    """The frozen shapes construct and hash (tuples not lists)."""
    from catalyst.data import RawEvent
    from catalyst.discovery import Candidate

    ev = RawEvent(source="edgar", source_id="x1", fetched_at=datetime(2026, 1, 1),
                  payload_raw={"a": 1})
    c = Candidate(
        id="01H", ticker="TEST", catalyst_type="earnings_drift",
        catalyst_date=date(2026, 2, 1), catalyst_date_confidence="confirmed",
        source_event_ids=("x1",), discovered_at=datetime(2026, 1, 1),
        sector="tech", correlation_tags=("tech-2026w5",),
    )
    assert ev.source == "edgar"
    assert c.source_event_ids == ("x1",)


def test_raw_event_fields_match_raw_events_table_columns(tmp_db):
    """data/ and storage/ are owned by different agents (data-engineer,
    shared schema session). If one adds a field the other doesn't know
    about, RawEvent and raw_events silently drift apart."""
    from catalyst.data import RawEvent

    columns = {r[1] for r in tmp_db.execute("PRAGMA table_info(raw_events)").fetchall()}
    fields = {f.name for f in dataclasses.fields(RawEvent)}
    assert columns == fields, (
        f"RawEvent fields and raw_events columns drifted: "
        f"only in dataclass={fields - columns}, only in table={columns - fields}"
    )


def test_candidate_fields_match_candidates_table_columns(tmp_db):
    """Same drift risk as above, for discovery's Candidate <-> candidates."""
    from catalyst.discovery import Candidate

    columns = {r[1] for r in tmp_db.execute("PRAGMA table_info(candidates)").fetchall()}
    fields = {f.name for f in dataclasses.fields(Candidate)}
    assert columns == fields, (
        f"Candidate fields and candidates columns drifted: "
        f"only in dataclass={fields - columns}, only in table={columns - fields}"
    )


def test_schema_safe_to_apply_twice(tmp_path):
    """BUILD-BRIEF.md: the installer must be 'safe to run twice'. That
    rests on every CREATE TABLE using IF NOT EXISTS - if one loses it,
    re-running init against an existing database starts raising."""
    from catalyst.storage import connect

    db_file = tmp_path / "reapply.db"
    schema_sql = (
        Path(__file__).resolve().parents[1] / "catalyst" / "storage" / "schema.sql"
    ).read_text()
    conn = connect(str(db_file))
    conn.executescript(schema_sql)
    conn.commit()
    # Re-applying the same schema against the same file must not raise.
    conn.executescript(schema_sql)
    conn.commit()
    conn.close()


def _decimal_typed(field_type) -> bool:
    """True if a dataclass field's annotation is Decimal, or a union
    that includes it (e.g. `Decimal | None`)."""
    if field_type is Decimal:
        return True
    return Decimal in getattr(field_type, "__args__", ())


def test_money_shaped_dataclass_fields_are_decimal_not_float():
    """TRAPS.md: cost figures are decimal-string cents upstream, and
    float arithmetic on money is exactly the class of bug that silently
    mis-prices a bill. Every dollar/cents/price/notional field in the
    dataclasses that carry money must be typed Decimal, never float."""
    from catalyst.cost import CostEstimate, CostEvent, GovernorDecision
    from catalyst.execution import Fill
    from catalyst.risk import RiskDecision, SizingResult
    from catalyst.risk.hard_bounds import HardBounds

    money_fields = {
        SizingResult: ("notional_usd", "qty", "stop_price"),
        RiskDecision: ("notional_usd", "qty", "stop_price"),
        Fill: ("price", "qty", "broker_reported_price"),
        CostEstimate: ("estimated_cents",),
        CostEvent: ("priced_cents",),
        GovernorDecision: ("cap_cents", "period_to_date_cents", "shortfall_cents"),
        HardBounds: (
            "max_loss_per_position_pct", "max_total_exposure_pct",
            "daily_loss_kill_pct", "drawdown_kill_pct", "max_correlated_cluster_pct",
        ),
    }
    offenders = []
    for cls, names in money_fields.items():
        by_name = {f.name: f for f in dataclasses.fields(cls)}
        for name in names:
            if not _decimal_typed(by_name[name].type):
                offenders.append(f"{cls.__name__}.{name} -> {by_name[name].type!r}")
    assert not offenders, f"money-shaped fields not typed Decimal: {offenders}"


def test_cost_governor_caps_are_decimal_not_float():
    """Same trap, module-level constants rather than dataclass fields."""
    from catalyst.cost.governor import BASE_CAP_CENTS, MANUAL_SPEND_CAP_CENTS_PER_MONTH

    assert isinstance(BASE_CAP_CENTS, Decimal)
    assert isinstance(MANUAL_SPEND_CAP_CENTS_PER_MONTH, Decimal)


def test_usage_components_captures_cache_tokens_explicitly():
    """TRAPS.md: cache tokens are billed but excluded from input_tokens;
    missing cache_read_input_tokens/cache_creation_input_tokens
    understates the bill by about half. This is the field set that
    price() depends on - guard it against a well-meaning refactor that
    drops what looks like a duplicate of input_tokens."""
    from catalyst.research.schema import UsageComponents

    field_names = {f.name for f in dataclasses.fields(UsageComponents)}
    for required in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "web_search_requests", "raw",
    ):
        assert required in field_names, (
            f"UsageComponents is missing {required!r} - cost.tracker.price() "
            "cannot bill accurately without it (TRAPS.md)"
        )


def test_cost_ledger_exposes_no_annualizing_function():
    """cost/ledger.py's own docstring: 'Deliberately exposes NO function
    that multiplies a partial-month figure into an annual estimate -
    annualizing is refused, not performed' (ARCHITECTURE.md section 7.4).
    A partial month multiplied up is exactly the kind of number that
    looked fine for the first three days of a real run (TRAPS.md)."""
    import inspect

    import catalyst.cost.ledger as ledger

    names = [name for name, _ in inspect.getmembers(ledger, inspect.isfunction)]
    offenders = [n for n in names if "annual" in n.lower()]
    assert not offenders, f"cost.ledger exposes an annualizing function: {offenders}"


def test_money_critical_files_carry_the_marker():
    """Every file that sizes, stops, orders, reconciles or crosses the
    model/code boundary carries a literal, greppable marker (CLAUDE.md
    house rule 5).

    THE MARKER CHANGED NAME ON 2026-08-31, and the reason is worth
    keeping. It used to read HUMAN REVIEW REQUIRED, because changing one
    of these files meant waiting on the owner. The owner removed that
    gate - "merge all, change rules so you dont want me everytime" - so
    the marker now names the PROPERTY rather than the process: this code
    decides or moves real money.

    What it demands is unchanged in substance and is stated in house
    rule 5: risk-reviewer's read, a test that fails against the old
    behaviour, the full suite green. What is gone is the wait.

    The marker exists so that list is greppable rather than remembered -
    by a person or by the next session."""
    root = Path(__file__).resolve().parents[1] / "catalyst"
    must_carry_marker = [
        "risk/sizing.py", "risk/evaluate.py", "risk/kill_switches.py",
        "risk/__init__.py",
        "execution/broker.py", "execution/orders.py", "execution/exits.py",
        "execution/reconcile.py", "execution/__init__.py",
        "research/boundary.py", "research/schema.py",
        # Can close a position, so it belongs on the list; it carried the
        # old marker without ever being checked for it.
        "research/position_review.py",
        # Decides whether a stored view may still be acted on, which is
        # the difference between entering at the price the thesis was
        # written about and entering after the move already happened.
        "risk/stale_view.py",
    ]
    missing = [
        rel for rel in must_carry_marker
        if "MONEY-CRITICAL" not in (root / rel).read_text()
    ]
    assert not missing, f"ownership marker missing from: {missing}"


def test_the_old_review_marker_is_gone_everywhere():
    """A file still saying HUMAN REVIEW REQUIRED would send the next
    session to ask the owner for a sign-off they abolished - which is
    exactly the stale instruction that costs an evening."""
    root = Path(__file__).resolve().parents[1] / "catalyst"
    stale = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if "HUMAN REVIEW REQUIRED" in p.read_text())
    assert not stale, f"the retired marker is still in: {stale}"


def _string_literals(path: Path):
    """Every string literal in a file, with its line number."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _machine_specific_roots(checkout: Path, home: Path):
    """The paths no literal may name, derived and never enumerated.

    House rule 7. No list of /home, /Users, /root generalises: the
    offending path is whatever directory this checkout happens to live
    in, which is only knowable by asking at runtime.

    Returns (self_or_under, under_only). The CHECKOUT counts even when a
    literal IS it exactly, because that is the shape that broke the
    upgrade: cwd="/home/user/catalyst". HOME counts only for literals
    pointing INSIDE it - a bare home directory is not a path into
    anything, and on this machine home is /root, which a test may
    legitimately write down while explaining why short roots are
    handled differently.
    """
    self_or_under = [str(checkout)]
    under_only = []
    # Guarded because a home of "/" would make every absolute path in
    # the project an offender, and a checkout that IS home would be
    # covered twice by the line above.
    if len(home.parts) > 1 and checkout != home:
        under_only.append(str(home))
    return self_or_under, under_only


def _literals_naming_a_local_path(files, roots, base: Path):
    """Every literal that IS the checkout or points inside either root.

    MATCHED AT THE START, AND ONLY THERE, and that is measured rather
    than tidy. Matching a root ANYWHERE in a literal was tried first, on
    the reasoning that "cd /the/checkout && pytest" is as unportable as
    the cwd= that broke the upgrade. It flagged
    catalyst/dashboard/render.py:88 - a CSS **comment**, inside the one
    big style literal, that quotes a path it once rendered badly. Prose
    embedded in a large literal is not something an AST can separate
    from code, so the broader rule cannot be kept without forcing
    someone to mangle a comment. Section 17's generic-word trap in a new
    coat: a rule that cries wolf teaches the next reader to silence it.

    A root must be followed by a separator to count, which is also what
    stops /some/where/catalyst-backup reading as being inside
    /some/where/catalyst.
    """
    self_or_under, under_only = roots
    offenders = []
    for path in files:
        for line, value in _string_literals(path):
            inside = any(value.startswith(root + "/")
                         for root in list(self_or_under) + list(under_only))
            if not (inside or value in self_or_under):
                continue
            try:
                where = path.relative_to(base).as_posix()
            except ValueError:
                where = str(path)
            offenders.append(f"{where}:{line}: {value!r}")
    return offenders


def _shipped_python(checkout: Path) -> list[Path]:
    files = []
    for folder in ("tests", "catalyst", "scripts"):
        directory = checkout / folder
        if not directory.is_dir():
            continue
        files.extend(path for path in sorted(directory.rglob("*.py"))
                     if "__pycache__" not in path.parts)
    assert files, (
        f"found no Python under {checkout} to check, so this guard would "
        f"pass by looking at nothing")
    return files


def test_no_file_names_this_checkout_by_absolute_path():
    """THE OWNER'S UPGRADE FAILED AND ROLLED BACK ON EXACTLY THIS.

    2026-09-13: a guard test passed cwd="/home/user/catalyst" - the
    location of the sandbox it was written in - to subprocess.run. On
    the owner's machine that directory does not exist, the call raised
    FileNotFoundError, one test of 4120 failed, and upgrade.sh correctly
    refused to ship the version. The suite was green everywhere it was
    developed and red on the only machine that matters.

    A path that must be absolute is either computed from __file__ or
    handed in by a fixture. There is no third correct case.

    THIS ASSERTION IS VACUOUS TODAY - there are no offenders left - so
    the rule it rests on is exercised against synthetic input by
    TestTheLocalPathRuleReallyCatchesOne below. Without that, deleting
    the rule's body would pass.

    It is deliberately NOT enough on its own: the suite must also be run
    from a different absolute path before a version is called green,
    because a rule can only catch the literals somebody wrote down."""
    checkout = Path(__file__).resolve().parents[1]
    roots = _machine_specific_roots(checkout, Path.home().resolve())
    offenders = _literals_naming_a_local_path(
        _shipped_python(checkout), roots, checkout)
    assert not offenders, (
        "these literals name a directory that exists only on the machine "
        "they were written on, so they cannot run on the owner's:\n"
        + "\n".join(offenders)
        + "\n\nDerive the path from __file__ instead.")


class TestTheLocalPathRuleReallyCatchesOne:
    """The non-vacuity half. The guard above finds nothing because
    nothing is left to find, which is exactly the shape of assertion
    that broke the owner's upgrade in the first place."""

    @staticmethod
    def _write(tmp_path, body: str) -> Path:
        path = tmp_path / "offender.py"
        path.write_text(body)
        return path

    def test_a_literal_under_the_checkout_is_flagged(self, tmp_path):
        checkout = Path("/some/where/catalyst")
        path = self._write(
            tmp_path,
            'cwd = "/some/where/catalyst"\n')
        found = _literals_naming_a_local_path(
            [path], _machine_specific_roots(checkout, Path("/home/someone")),
            tmp_path)
        assert len(found) == 1, found
        assert "offender.py:1" in found[0]

    def test_a_literal_under_the_home_directory_is_flagged(self, tmp_path):
        path = self._write(tmp_path, 'p = "/home/someone/notes/x.db"\n')
        found = _literals_naming_a_local_path(
            [path],
            _machine_specific_roots(Path("/srv/catalyst"),
                                    Path("/home/someone")),
            tmp_path)
        assert len(found) == 1, found

    def test_a_deeper_path_under_the_checkout_is_flagged(self, tmp_path):
        path = self._write(tmp_path, 'p = "/some/where/catalyst/data/x.csv"\n')
        found = _literals_naming_a_local_path(
            [path], _machine_specific_roots(Path("/some/where/catalyst"),
                                            Path("/home/someone")),
            tmp_path)
        assert len(found) == 1, found

    def test_a_relative_or_production_path_is_not_flagged(self, tmp_path):
        path = self._write(
            tmp_path,
            'a = "catalyst/risk/sizing.py"\n'
            'b = "/var/lib/catalyst"\n'
            'c = "/v2/account"\n')
        found = _literals_naming_a_local_path(
            [path], _machine_specific_roots(Path("/some/where/catalyst"),
                                            Path("/home/someone")),
            tmp_path)
        assert found == [], found

    def test_a_sibling_directory_sharing_the_prefix_is_not_flagged(
            self, tmp_path):
        """/some/where/catalyst-backup is a different directory, and
        without the separator check it would read as being inside the
        checkout."""
        path = self._write(tmp_path, 'p = "/some/where/catalyst-backup/x"\n')
        found = _literals_naming_a_local_path(
            [path], _machine_specific_roots(Path("/some/where/catalyst"),
                                            Path("/home/someone")),
            tmp_path)
        assert found == [], found

    def test_a_home_shaped_token_in_prose_is_not_flagged(self, tmp_path):
        """MEASURED, not preferred. Matching a root ANYWHERE in a literal
        was tried first and flagged the dashboard's own CSS comment at
        render.py:88, which quotes a path it once rendered badly - prose
        inside one big style literal, which no AST can separate from
        code. Section 17's generic-word trap: a rule that cries wolf
        gets silenced by the next reader."""
        path = self._write(
            tmp_path,
            'CSS = "\\n:root { color: red; }\\n"\n'
            'NOTE = "it used to read /some/where/catalyst/dashboa, cut off"\n')
        found = _literals_naming_a_local_path(
            [path], _machine_specific_roots(Path("/some/where/catalyst"),
                                            Path("/root")),
            tmp_path)
        assert found == [], found

    def test_a_bare_home_directory_is_not_flagged(self, tmp_path):
        """A home root on its own is not a path into anything, and a
        fixture may legitimately write one down - this suite writes
        "/root" while explaining why short roots differ. The CHECKOUT
        root is the opposite case and is asserted above, because
        cwd="/the/checkout" is exactly what broke the upgrade."""
        path = self._write(tmp_path, 'h = "/root"\n')
        found = _literals_naming_a_local_path(
            [path], _machine_specific_roots(Path("/some/where/catalyst"),
                                            Path("/root")),
            tmp_path)
        assert found == [], found

    def test_a_home_of_root_only_is_never_used_as_a_prefix(self):
        """Every absolute path in the project starts with "/", so a home
        of "/" would flag all of them and the guard would be useless
        noise rather than a rule."""
        self_or_under, under_only = _machine_specific_roots(
            Path("/srv/catalyst"), Path("/"))
        assert self_or_under == ["/srv/catalyst"]
        assert under_only == []


class TestTheSourceGuardCannotPassBySearchingNothing:
    """The guards that assert "this string appears nowhere in the money
    path" are only worth anything if the search really happened. The
    version these replaced asserted `grep`'s stdout was empty, which is
    also what an unresolved path produces - so it could pass while
    reading no files at all."""

    def test_a_missing_directory_raises_instead_of_matching_nothing(self):
        from source_guard import source_matches

        with pytest.raises(AssertionError) as caught:
            source_matches("anything", "catalyst/not_a_real_package")
        assert "not a directory" in str(caught.value)

    def test_a_missing_file_raises_instead_of_matching_nothing(self):
        from source_guard import pattern_matches

        with pytest.raises(AssertionError) as caught:
            pattern_matches("x", "catalyst/risk/not_a_real_module.py")
        assert "not a file" in str(caught.value)

    def test_a_directory_holding_no_python_raises(self):
        """docs/ is real and contains no .py, so "nothing matched" there
        would be true for the wrong reason."""
        from source_guard import source_matches

        with pytest.raises(AssertionError) as caught:
            source_matches("anything", "docs")
        assert "vacuously true" in str(caught.value)

    def test_an_empty_needle_is_refused(self):
        from source_guard import source_matches

        with pytest.raises(AssertionError):
            source_matches("", "catalyst/risk")

    def test_it_finds_a_string_that_is_really_there(self):
        """The positive half. Without this, every guard in the suite
        could be satisfied by a search that reads nothing."""
        from source_guard import source_matches

        hits = source_matches("MONEY-CRITICAL", "catalyst/risk")
        assert hits, (
            "the source search found no MONEY-CRITICAL marker under "
            "catalyst/risk, which certainly has them - so the search is "
            "not reading the files and every guard built on it is empty")
        assert all(":" in hit for hit in hits)

    def test_the_repo_root_is_derived_and_correct(self):
        from source_guard import REPO_ROOT

        assert (REPO_ROOT / "catalyst" / "risk" / "sizing.py").is_file()
        assert REPO_ROOT == Path(__file__).resolve().parents[1]


def test_the_hard_bounds_still_say_a_human_decides():
    """The one exception house rule 5 keeps. Hard bounds prevent ruin;
    the system may propose a change and must never make one. If this
    ever goes quiet, the only remaining owner decision has gone quiet
    with it."""
    root = Path(__file__).resolve().parents[1] / "catalyst"
    text = (root / "risk" / "hard_bounds.py").read_text().lower()
    assert "human decides" in text, (
        "hard_bounds.py no longer says the owner decides these; that is "
        "the single gate house rule 5 did not remove")
