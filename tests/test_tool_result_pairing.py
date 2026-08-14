"""Every echoed `tool_use` needs a `tool_result` in the next message.

OWNER-REPORTED, verbatim from the live funnel:

    HTTP 400 ... "messages.2: `tool_use` ids were found without
    `tool_result` blocks immediately after: toolu_017e8FMaDoJhBf7x8ULSNfdS.
    Each `tool_use` block must have a corresponding `tool_result` block
    in the next message."

Five separate research calls died this way, and the funnel could only
report the status code. This is the mechanism.

THE PATH. The schema tool is offered DURING exploration so a valid early
view skips the extraction turn. When the model submits early but the view
is MALFORMED, boundary.py deliberately falls through to the forced turn
rather than discarding the candidate - and to do that it echoes the
assistant's content back VERBATIM, which still contains the failed
`tool_use` block. The next message it appends is plain text:

    messages[0]  user       the research prompt
    messages[1]  assistant  ...including tool_use toolu_017e8F...
    messages[2]  user       "Submit your conclusion now via ..."   <- 400

The Messages API requires messages[2] to carry a `tool_result` whose
`tool_use_id` matches. It does not, so the call is rejected AFTER the
exploration turn has already been paid for, and the candidate is lost.

The fix is not to stop echoing: the echo is what lets a malformed
submission be repaired instead of thrown away. It is to answer the
tool call properly - a tool_result saying WHY the view was rejected,
which is also more useful to the model than "submit your conclusion".
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.research import boundary, prompts


@pytest.fixture(autouse=True)
def _cheap_prompts(monkeypatch):
    monkeypatch.setattr(prompts, "render_research_prompt", lambda c, **kw: "r")
    monkeypatch.setattr(prompts, "exploration_tools", lambda *a, **kw: [])


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.executescript(open("catalyst/storage/schema.sql").read())
    c.commit()
    return c


def _candidate():
    from catalyst.discovery import Candidate

    now = datetime.now(timezone.utc)
    return Candidate(
        id="cand-tr", ticker="TOOL", catalyst_type="insider_cluster",
        catalyst_date=now.date() + timedelta(days=5),
        catalyst_date_confidence="confirmed", source_event_ids=("e1",),
        discovered_at=now, sector="tech", correlation_tags=("tech",))


#: A submission the model really made shapes like: the tool was called,
#: but the view is missing required fields, so it cannot be parsed.
MALFORMED_EARLY = {
    "id": "msg_1", "model": "claude-sonnet-5", "stop_reason": "tool_use",
    "content": [
        {"type": "text", "text": "Here is my read."},
        {"type": "tool_use", "id": "toolu_017e8FMaDoJhBf7x8ULSNfdS",
         "name": "submit_research_view",
         "input": {"direction": "long", "conviction": 0.8}},
    ],
    "usage": {"input_tokens": 1000, "output_tokens": 100},
}

GOOD_VIEW = {
    "direction": "no_trade", "conviction": 0.2, "thesis": "t",
    "invalidation": "i", "expected_holding_days": 5, "priced_in": True,
    "priced_in_reasoning": "r",
}


def _unpaired(payload):
    """tool_use ids in a message with no tool_result in the NEXT one -
    the API's own rule, checked the way the API checks it."""
    messages = payload.get("messages") or []
    bad = []
    for i, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        ids = {b.get("id") for b in content if isinstance(b, dict)
               and b.get("type") == "tool_use"}
        if not ids:
            continue
        nxt = messages[i + 1].get("content") if i + 1 < len(messages) else None
        answered = {b.get("tool_use_id") for b in nxt
                    if isinstance(b, dict) and b.get("type") == "tool_result"} \
            if isinstance(nxt, list) else set()
        bad.extend(sorted(ids - answered))
    return bad


class TestTheExtractionTurnAnswersTheToolCall:
    def test_no_payload_leaves_a_tool_use_unanswered(self, conn):
        """THE REPORTED 400. Reproduced by capturing what would be sent."""
        sent = []

        def transport(payload):
            sent.append(payload)
            if len(sent) == 1:
                return MALFORMED_EARLY
            return {"id": "m", "model": "claude-sonnet-5",
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "t2",
                                 "name": "submit_research_view",
                                 "input": dict(GOOD_VIEW)}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}

        boundary.investigate(
            _candidate(),
            boundary.CostContext(conn=conn, governor_profit_share=Decimal("0"),
                                 cycle_id="c", kind="scheduled"),
            transport)

        assert len(sent) >= 2, "the malformed view should earn a forced turn"
        for i, payload in enumerate(sent):
            assert not _unpaired(payload), (
                f"request {i} leaves tool_use "
                f"{_unpaired(payload)} unanswered - this is the exact 400 "
                "the owner reported, and it costs the paid exploration turn")

    def test_the_repair_still_produces_a_view(self, conn):
        """Answering the tool call must not break the repair path: a
        malformed early submission should still end in a parsed view."""
        calls = {"n": 0}

        def transport(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return MALFORMED_EARLY
            return {"id": "m", "model": "claude-sonnet-5",
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "t2",
                                 "name": "submit_research_view",
                                 "input": dict(GOOD_VIEW)}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}

        log = boundary.investigate(
            _candidate(),
            boundary.CostContext(conn=conn, governor_profit_share=Decimal("0"),
                                 cycle_id="c", kind="scheduled"),
            transport)
        assert log.parsed_view is not None, log.skipped_reason
        assert log.parsed_view.direction == "no_trade"

    def test_the_tool_result_says_WHY_it_was_rejected(self, conn):
        """A tool_result is required either way, so it may as well carry
        the reason - that is more use to the model than 'submit your
        conclusion' and costs the same tokens."""
        sent = []

        def transport(payload):
            sent.append(payload)
            if len(sent) == 1:
                return MALFORMED_EARLY
            return {"id": "m", "model": "claude-sonnet-5",
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": "t2",
                                 "name": "submit_research_view",
                                 "input": dict(GOOD_VIEW)}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}

        boundary.investigate(
            _candidate(),
            boundary.CostContext(conn=conn, governor_profit_share=Decimal("0"),
                                 cycle_id="c", kind="scheduled"),
            transport)
        results = [b for m in sent[-1]["messages"]
                   if isinstance(m.get("content"), list)
                   for b in m["content"]
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        assert results, "no tool_result was sent at all"
        text = " ".join(str(r.get("content", "")) for r in results).lower()
        assert any(w in text for w in ("missing", "invalid", "required")), text


class TestTheGuardCatchesItBeforeItIsSent:
    def test_invalid_payload_reason_names_an_unanswered_tool_use(self):
        """It must be caught LOCALLY. This shape cost five paid calls and
        surfaced as a bare 400 - the guard exists precisely so a request
        the API will certainly reject never consumes a call."""
        reason = boundary.invalid_payload_reason({
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_abc",
                     "name": "submit_research_view", "input": {}}]},
                {"role": "user", "content": "carry on"},
            ]})
        assert reason is not None
        assert "tool_result" in reason and "toolu_abc" in reason, reason

    def test_a_properly_answered_tool_use_passes(self):
        assert boundary.invalid_payload_reason({
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_abc",
                     "name": "submit_research_view", "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_abc",
                     "content": "rejected: missing required fields"}]},
            ]}) is None

    def test_a_server_tool_use_block_is_not_our_problem(self):
        """`server_tool_use` (web search) is executed and answered by
        Anthropic inside the same turn. Demanding a tool_result for it
        would refuse a request the API accepts."""
        assert boundary.invalid_payload_reason({
            "model": "m", "max_tokens": 10,
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [
                    {"type": "server_tool_use", "id": "srvtoolu_x",
                     "name": "web_search", "input": {}},
                    {"type": "web_search_tool_result",
                     "tool_use_id": "srvtoolu_x", "content": []}]},
                {"role": "user", "content": "carry on"},
            ]}) is None
