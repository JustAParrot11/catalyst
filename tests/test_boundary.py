"""Model/code boundary tests - the one code path that can spend money.

Offline: the transport is a stub callable; prompts are monkeypatched so
these tests pin boundary MECHANICS (authorization order, forced
tool_choice, record-first pricing, strict view parsing) independent of
prompt wording.
"""

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from catalyst.discovery import Candidate
from catalyst.research import boundary, prompts
from catalyst.research.boundary import CostContext, investigate
from catalyst.research.schema import make_view_from_tool_input

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.executescript(open("catalyst/storage/schema.sql").read())
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def stub_prompts(monkeypatch):
    monkeypatch.setattr(
        prompts, "render_research_prompt",
        lambda c, graph_context=None: f"research {c.ticker}"
        + (f"\n{graph_context}" if graph_context else ""))
    monkeypatch.setattr(
        prompts, "exploration_tools",
        lambda: [{"type": "web_search_20250305", "name": "web_search",
                  "max_uses": 3}])


def candidate():
    return Candidate(
        id="cand-1", ticker="TEST", catalyst_type="insider_cluster",
        catalyst_date=date(2026, 8, 20), catalyst_date_confidence="estimated",
        source_event_ids=("e1",), discovered_at=NOW, sector="tech",
        correlation_tags=("tech",))


def ctx(db, kind="scheduled"):
    return CostContext(conn=db, governor_profit_share=Decimal("0.10"),
                       cycle_id="cycle-1", kind=kind)


USAGE = {"input_tokens": 1000, "output_tokens": 500,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

GOOD_VIEW = {
    "direction": "long", "conviction": 0.8, "thesis": "cluster of buys",
    "invalidation": "insiders sell", "expected_holding_days": 12,
    "priced_in": False, "priced_in_reasoning": "no move since filing",
}


def transport_script(responses):
    """Returns (transport, payload_log). Pops responses in order."""
    log = []

    def transport(payload):
        log.append(payload)
        return responses.pop(0)

    return transport, log


def end_turn(content=None):
    return {"content": content or [{"type": "text", "text": "looked at it"}],
            "stop_reason": "end_turn", "usage": dict(USAGE)}


def extraction_response(view=None):
    return {"content": [{"type": "tool_use", "name": "submit_research_view",
                         "input": view or dict(GOOD_VIEW)}],
            "stop_reason": "tool_use", "usage": dict(USAGE)}


class TestInvestigate:
    def test_happy_path_two_turns(self, db):
        transport, log = transport_script([end_turn(), extraction_response()])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is not None
        assert result.parsed_view.direction == "long"
        assert result.skipped_reason is None
        assert len(result.api_turns) == 2

        # extraction turn was FORCED to the schema tool
        assert log[1]["tool_choice"] == {"type": "tool",
                                         "name": "submit_research_view"}
        assert log[1]["tools"][0]["name"] == "submit_research_view"
        # exploration turn was not
        assert log[0]["tool_choice"] == {"type": "auto"}

        # both turns recorded verbatim and priced into the ledger
        assert db.execute(
            "SELECT COUNT(*) FROM research_call_turns").fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM cost_events WHERE component='research'"
        ).fetchone()[0] == 2
        # 1000 in + 500 out on sonnet = 0.3c + 0.75c = 1.05c per turn
        assert result.cost_cents == Decimal("2.10")
        row = db.execute("SELECT cost_cents, skipped_reason FROM research_calls"
                         ).fetchone()
        assert row == ("2.10", None)
        # view persisted
        assert db.execute("SELECT direction FROM research_views"
                          ).fetchone()[0] == "long"

    def test_budget_denied_before_any_call(self, db):
        # fill the scheduled month to the cap: authorize must refuse
        # BEFORE the transport is ever invoked
        db.execute(
            "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
            ("e1", json.dumps(USAGE), "claude-sonnet-5", "scheduled",
             "research", "500", NOW.isoformat(), None))
        db.commit()
        calls = {"n": 0}

        def transport(payload):
            calls["n"] += 1
            return end_turn()

        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is None
        assert result.skipped_reason == "budget_denied"
        assert calls["n"] == 0
        assert result.api_turns == ()
        # the skip is still a recorded research_calls row (audit trail)
        assert db.execute("SELECT skipped_reason FROM research_calls"
                          ).fetchone()[0] == "budget_denied"

    def test_pause_turn_continuation_is_bounded(self, db):
        responses = [
            {"content": [], "stop_reason": "pause_turn", "usage": dict(USAGE)},
            {"content": [], "stop_reason": "pause_turn", "usage": dict(USAGE)},
            extraction_response(),
        ]
        transport, log = transport_script(responses)
        result = investigate(candidate(), ctx(db), transport)
        # MAX_EXPLORATION_TURNS=2 exploration calls, then extraction -
        # the second pause_turn is NOT continued again
        assert len(result.api_turns) == 3
        assert result.parsed_view is not None

    def test_no_tool_call_in_extraction(self, db):
        transport, _ = transport_script([end_turn(), end_turn()])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is None
        assert result.skipped_reason == "no_tool_call_in_extraction_turn"

    def test_invalid_view_is_skip_not_default(self, db):
        bad = dict(GOOD_VIEW)
        del bad["invalidation"]
        transport, _ = transport_script([end_turn(),
                                         extraction_response(bad)])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is None
        assert result.skipped_reason.startswith("invalid_view")
        assert db.execute("SELECT COUNT(*) FROM research_views"
                          ).fetchone()[0] == 0

    def test_usage_recorded_even_when_view_invalid(self, db):
        # spend happened; the ledger must say so regardless of outcome
        transport, _ = transport_script([end_turn(),
                                         extraction_response({"direction": "long"})])
        investigate(candidate(), ctx(db), transport)
        assert db.execute(
            "SELECT COUNT(*) FROM cost_events").fetchone()[0] == 2

    def test_graph_context_reaches_prompt(self, db):
        transport, log = transport_script([end_turn(), extraction_response()])
        investigate(candidate(), ctx(db), transport,
                    graph_context="GRAPH: acme -> pdufa 2026-09-01")
        assert "GRAPH: acme" in log[0]["messages"][0]["content"]


class TestMakeView:
    def test_size_shaped_fields_refused(self):
        for extra in ("qty", "notional_usd", "position_size", "shares"):
            with pytest.raises(ValueError, match="unknown fields"):
                make_view_from_tool_input(
                    "c1", {**GOOD_VIEW, extra: 100})

    def test_missing_field_raises(self):
        bad = dict(GOOD_VIEW)
        del bad["thesis"]
        with pytest.raises(KeyError):
            make_view_from_tool_input("c1", bad)

    def test_conviction_bounds(self):
        with pytest.raises(ValueError):
            make_view_from_tool_input("c1", {**GOOD_VIEW, "conviction": 1.2})
        with pytest.raises(ValueError):
            make_view_from_tool_input("c1", {**GOOD_VIEW, "conviction": True})

    def test_direction_enum(self):
        with pytest.raises(ValueError):
            make_view_from_tool_input("c1", {**GOOD_VIEW,
                                             "direction": "leveraged_long"})

    def test_empty_thesis_refused(self):
        with pytest.raises(ValueError):
            make_view_from_tool_input("c1", {**GOOD_VIEW, "thesis": "  "})

    def test_holding_days_positive_int(self):
        with pytest.raises(ValueError):
            make_view_from_tool_input(
                "c1", {**GOOD_VIEW, "expected_holding_days": 0})
        with pytest.raises(ValueError):
            make_view_from_tool_input(
                "c1", {**GOOD_VIEW, "expected_holding_days": 2.5})

    def test_good_view_parses(self):
        v = make_view_from_tool_input("c1", dict(GOOD_VIEW))
        assert v.candidate_id == "c1" and v.conviction == 0.8
