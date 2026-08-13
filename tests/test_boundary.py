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
    # **kwargs rather than a fixed signature - these stubs exist to make
    # the prompt cheap and deterministic, not to pin its parameter list.
    monkeypatch.setattr(
        prompts, "render_research_prompt",
        lambda c, graph_context=None, **kw: f"research {c.ticker}"
        + (f"\n{graph_context}" if graph_context else ""))
    monkeypatch.setattr(
        prompts, "exploration_tools",
        lambda max_searches=3, **kw: [
            {"type": "web_search_20250305", "name": "web_search",
             "max_uses": max_searches}])


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
        # summation and recording are what THIS test pins; the rate table
        # itself (incl. the sonnet-5 intro window) is pinned with explicit
        # dates in test_cost_api_adapter - rates are date-effective, so a
        # hardcoded cents figure here would flip on 2026-09-01
        from catalyst.cost.tracker import make_usage_components, price
        per_turn = price(make_usage_components(dict(USAGE)), "claude-sonnet-5")
        assert per_turn > 0
        assert result.cost_cents == 2 * per_turn
        row = db.execute("SELECT cost_cents, skipped_reason FROM research_calls"
                         ).fetchone()
        assert row == (str(2 * per_turn), None)
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

    def test_a_pause_turn_WITH_content_is_actually_continued(self, db):
        """The bounded test above passes empty content, so _assistant_echo
        returns None and the loop BREAKS - it never exercises the
        continuation it is named after.

        A real pause_turn carries content. Continuing it means sending
        the assistant's partial turn back with no user message after it,
        and that is precisely the shape invalid_payload_reason rejected:
        every continuation this loop made was killed locally, after the
        paid exploration call, with `invalid_request_not_sent`. So
        MAX_EXPLORATION_TURNS=2 bought one turn, and a candidate whose
        search loop paused was charged and then abandoned.

        Continuing a pause_turn is the documented shape, not a mistake.
        """
        paused = {"content": [{"type": "text", "text": "searching"}],
                  "stop_reason": "pause_turn", "usage": dict(USAGE)}
        transport, log = transport_script([paused, extraction_response()])
        result = investigate(candidate(), ctx(db), transport)

        assert result.skipped_reason is None, result.skipped_reason
        assert result.parsed_view is not None
        assert len(log) == 2, (
            "the paused turn was never continued - the second request is "
            "missing, so the paid first call bought nothing")
        assert log[1]["messages"][-1]["role"] == "assistant", (
            "a pause_turn is continued by sending the assistant's partial "
            "turn back; anything else restarts the search")

    def test_a_stray_trailing_assistant_turn_is_STILL_refused(self, db):
        """The allowance above must be narrow. Outside a pause_turn
        continuation a trailing assistant message means an echo was
        appended without its prompt, and the model is asked nothing."""
        reason = boundary.invalid_payload_reason({
            "model": "m", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "assistant", "content": [
                             {"type": "text", "text": "partial"}]}]})
        assert reason is not None and "assistant" in reason

    def test_no_tool_call_in_extraction(self, db):
        # the live API can ignore `required` on forced tool calls, so a
        # bad extraction gets ONE bounded repair turn before skipping
        transport, _ = transport_script([end_turn(), end_turn(), end_turn()])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is None
        assert result.skipped_reason == "no_tool_call_in_extraction_turn"
        assert len(result.api_turns) == 3     # exploration + 2 attempts

    def test_invalid_view_is_skip_not_default(self, db):
        bad = dict(GOOD_VIEW)
        del bad["invalidation"]
        transport, _ = transport_script([end_turn(),
                                         extraction_response(bad),
                                         extraction_response(bad)])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is None
        assert result.skipped_reason.startswith("invalid_view")
        assert db.execute("SELECT COUNT(*) FROM research_views"
                          ).fetchone()[0] == 0

    def test_repair_turn_recovers_an_incomplete_view(self, db):
        """Observed live 2026-08-10: forced tool_choice omitted a
        required field; the single repair turn must recover it."""
        bad = dict(GOOD_VIEW)
        del bad["invalidation"]
        transport, log = transport_script([end_turn(),
                                           extraction_response(bad),
                                           extraction_response()])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is not None
        assert result.skipped_reason is None
        # the repair request told the model what was wrong
        repair_msg = log[2]["messages"][-1]["content"]
        assert "invalidation" in repair_msg
        assert len(result.api_turns) == 3
        assert db.execute("SELECT COUNT(*) FROM research_views"
                          ).fetchone()[0] == 1   # the repaired view landed

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


class TestEmptyAssistantContentIsNeverSentBack:
    """Owner's live funnel, 2026-08-10: four of five research calls died
    on `400 Bad Request` from the Messages API, with no reason - the
    transport had thrown the response body away.

    The Messages API rejects an assistant message whose content array is
    empty. Every place this boundary continues a conversation appends
    `turn.raw_response.get("content", [])` verbatim, so a turn that
    comes back with no content blocks - which pause_turn and some
    server-tool states do - poisons the NEXT request with
    `{"role": "assistant", "content": []}` and earns a 400.
    """

    def _payloads_are_valid(self, log):
        for payload in log:
            for msg in payload["messages"]:
                content = msg["content"]
                if isinstance(content, list):
                    assert content, (
                        f"empty content array sent for role {msg['role']!r} - "
                        "the Messages API 400s on this")

    def test_an_empty_exploration_turn_does_not_poison_the_next_request(
            self, db):
        empty_pause = {"content": [], "stop_reason": "pause_turn",
                       "usage": dict(USAGE)}
        transport, log = transport_script([empty_pause, extraction_response()])
        result = investigate(candidate(), ctx(db), transport)
        self._payloads_are_valid(log)
        assert result.parsed_view is not None, (
            "an empty exploration turn is recoverable, not fatal")

    def test_an_empty_extraction_turn_does_not_poison_the_repair(self, db):
        empty = {"content": [], "stop_reason": "end_turn", "usage": dict(USAGE)}
        transport, log = transport_script([end_turn(), empty,
                                           extraction_response()])
        result = investigate(candidate(), ctx(db), transport)
        self._payloads_are_valid(log)
        assert result.parsed_view is not None

    def test_a_transport_error_records_the_upstream_body_not_just_a_status(
            self, db):
        """House rule 3. A 400 whose only detail is a link to MDN cannot
        be diagnosed; Anthropic says exactly what it objected to in the
        response body, and that is what has to reach the funnel."""
        class Boom(Exception):
            pass

        def transport(payload):
            raise Boom("HTTP 400: {\"error\":{\"message\":"
                       "\"messages.1.content: at least 1 item required\"}}")

        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is None
        assert "at least 1 item required" in (result.skipped_reason or ""), (
            "the upstream explanation must survive into the skip reason")


class TestExtractionCanBeSkipped:
    """Token optimisation, measured before it was built.

    The extraction turn re-sends the ENTIRE exploration context - 24k
    tokens of web-search results, on the real 2026-08-10 call - purely
    to collect a few hundred tokens of JSON. That second full-price read
    is 5.6c of a 14.7c candidate, 38% of the bill.

    (Prompt caching was measured FIRST and rejected: the expensive
    tokens are the search results, which do not exist when turn one is
    sent, so there is nothing to write to cache at that point. Caching a
    two-turn shape saves nothing and costs a 1.25x write.)

    So the schema tool is now offered DURING exploration. If the model
    submits its view there, the extraction turn never happens. If it
    does not, the forced extraction runs exactly as before - the saving
    is opportunistic, never at the cost of getting a view.
    """

    def submitted_during_exploration(self):
        return {"content": [
            {"type": "text", "text": "searched, concluded"},
            {"type": "tool_use", "name": "submit_research_view",
             "input": dict(GOOD_VIEW)},
        ], "stop_reason": "tool_use", "usage": dict(USAGE)}

    def test_one_turn_when_the_model_submits_during_exploration(self, db):
        transport, log = transport_script([self.submitted_during_exploration()])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is not None
        assert result.parsed_view.direction == "long"
        assert result.skipped_reason is None
        assert len(result.api_turns) == 1, (
            "the whole point: no second full-context turn")
        assert db.execute("SELECT COUNT(*) FROM research_views"
                          ).fetchone()[0] == 1

    def test_the_schema_tool_is_offered_during_exploration(self, db):
        transport, log = transport_script([self.submitted_during_exploration()])
        investigate(candidate(), ctx(db), transport)
        names = [t.get("name") for t in log[0]["tools"]]
        assert "submit_research_view" in names
        assert log[0]["tool_choice"] == {"type": "auto"}, (
            "exploration must stay unforced - forcing it here would stop "
            "the model searching at all")

    def test_it_still_falls_back_to_forced_extraction(self, db):
        """No view during exploration - behave exactly as before."""
        transport, log = transport_script([end_turn(), extraction_response()])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is not None
        assert len(result.api_turns) == 2
        assert log[1]["tool_choice"] == {"type": "tool",
                                         "name": "submit_research_view"}

    def test_an_invalid_early_submission_does_not_skip_the_fallback(self, db):
        """A malformed early view must not be accepted NOR end the
        investigation - it falls through to the forced turn."""
        bad = dict(GOOD_VIEW)
        del bad["invalidation"]
        early_bad = {"content": [
            {"type": "tool_use", "name": "submit_research_view", "input": bad},
        ], "stop_reason": "tool_use", "usage": dict(USAGE)}
        transport, _ = transport_script([early_bad, extraction_response()])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is not None
        assert len(result.api_turns) == 2
