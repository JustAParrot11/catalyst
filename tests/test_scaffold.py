"""Stage-1 scaffold tests: the interface contract is importable, the
schema initializes, the boundary object cannot carry a size, and the
offline guard actually guards.
"""

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
    """The forced tool schema and the dataclass must never drift apart."""
    from catalyst.research.schema import SUBMIT_RESEARCH_VIEW_TOOL, ResearchView

    schema_fields = set(SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["properties"])
    dataclass_fields = {f.name for f in dataclasses.fields(ResearchView)} - {"candidate_id"}
    assert schema_fields == dataclass_fields
    assert SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["additionalProperties"] is False
    assert set(SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["required"]) == schema_fields


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


def test_human_review_required_files_carry_the_marker():
    """Files on the risk/execution/broker ownership row (CLAUDE.md house
    rule 5: 'Changes to risk, execution or broker code need human
    review') carry a literal, greppable marker. This is what lets a
    human find every file that needs their sign-off without having to
    remember the list from memory."""
    root = Path(__file__).resolve().parents[1] / "catalyst"
    must_carry_marker = [
        "risk/sizing.py", "risk/evaluate.py", "risk/kill_switches.py",
        "risk/__init__.py",
        "execution/broker.py", "execution/orders.py", "execution/exits.py",
        "execution/reconcile.py", "execution/__init__.py",
        "research/boundary.py", "research/schema.py",
    ]
    missing = [
        rel for rel in must_carry_marker
        if "HUMAN REVIEW REQUIRED" not in (root / rel).read_text()
    ]
    assert not missing, f"ownership marker missing from: {missing}"
