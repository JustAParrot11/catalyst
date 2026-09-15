"""Every `tool_use` this code echoes back must carry its `tool_result`.

OWNER-REPORTED 2026-09-15, from the live logs:

    fault research skipped: invalid_request_not_sent: message 3 calls
    tool_use toolu_015PG6wEFAvX7XHwsnfiWjXD and the next message carries
    no matching tool_result - the API rejects this outright

THE GUARD DID ITS JOB. `invalid_payload_reason` refused the request
before it was sent, so nothing was paid for a certain 400 - but the
research call was still skipped and the candidate went unresearched, so
the funnel lost it.

WHAT PRODUCED THAT SHAPE. The Messages API requires a `tool_result`
immediately after every client `tool_use`. Three separate sites appended
PLAIN TEXT after echoing a turn that contained one, and message 3 in the
owner's fault is the first extraction turn's echo:

    0  user       the prompt
    1  assistant  early submission          -> tool_use
    2  user       tool_result               (this one was correct)
    3  assistant  forced extraction turn    -> tool_use
    4  user       "Your submission was not accepted: ..."  <- PLAIN TEXT

The exploration echo fifty lines above the repair branch already
answered its tool calls, with a comment explaining why. The repair
branch was written without that fix, the review has the same defect in
its only forced turn, and a paused turn carrying a client tool call
could not be continued at all. One helper now; all three call it.

WHY NO TEST CAUGHT IT: `extraction_response` in `test_boundary.py` built
`tool_use` blocks with NO `id`, and an id is what marks a call as
needing an answer. Every existing test therefore exercised the branch
where there is nothing to answer. Fourth instance of a fixture that
cannot produce the owner's state agreeing with the bug (WHAT-WE-TRIED
sections 14, 23, 24, 32).
"""

import copy
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from catalyst.discovery import Candidate
from catalyst.research import boundary, prompts
from catalyst.research.boundary import (
    CostContext,
    answer_tool_calls,
    client_tool_use_ids,
    investigate,
    invalid_payload_reason,
)

_REAL_NOW = datetime.now(timezone.utc)
NOW = _REAL_NOW.replace(day=min(10, _REAL_NOW.day), hour=14, minute=0,
                        second=0, microsecond=0)

USAGE = {"input_tokens": 1000, "output_tokens": 500,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

GOOD_VIEW = {
    "direction": "long", "conviction": 0.8, "thesis": "cluster of buys",
    "invalidation": "insiders sell", "expected_holding_days": 12,
    "priced_in": False, "priced_in_reasoning": "no move since filing",
}

#: The owner's own id, so the reproduction is theirs and not a lookalike.
OWNER_ID = "toolu_015PG6wEFAvX7XHwsnfiWjXD"


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
        lambda c, **kw: f"research {c.ticker}")
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


def ctx(db):
    return CostContext(conn=db, governor_profit_share=Decimal("0.10"),
                       cycle_id="cycle-1", kind="scheduled")


def transport_script(responses):
    """Returns (transport, sent). `sent` records a DEEP COPY of each
    payload plus the guard's verdict AT SEND TIME.

    THE SHALLOW VERSION MADE THESE TESTS VACUOUS, and I wrote it before
    catching it. `investigate` appends to ONE `messages` list and hands
    the same object to every request, so a log of references shows every
    call the FINAL conversation - not what each request carried. Asserting
    "no payload was invalid" against that reads a list that no longer
    resembles anything that was sent, and would pass while an
    intermediate request was exactly the shape the owner reported.

    Verified: with references, a call made when `messages` held one
    message logged four. The copy is taken inside the transport, which
    is the only moment the real payload exists.
    """
    sent = []

    def transport(payload):
        # JUDGED WITH THE SAME FLAG PRODUCTION PASSES. `run_turn` sets
        # `continuing_pause_turn` when it deliberately sends a trailing
        # assistant message, and a trailing assistant message is the only
        # thing this code produces for that reason - so deriving it from
        # the payload matches the caller's intent.
        #
        # It does weaken the trailing-assistant rule specifically, which
        # is why `test_a_pure_server_tool_pause_STILL_CONTINUES` asserts
        # that shape directly. The tool_use rule - the one this change is
        # about - is NOT gated on the flag, so it is judged in full here.
        msgs = payload.get("messages") or []
        continuing = bool(msgs) and msgs[-1].get("role") == "assistant"
        sent.append({"payload": copy.deepcopy(payload),
                     "refused": invalid_payload_reason(
                         payload, continuing_pause_turn=continuing)})
        return responses.pop(0)

    return transport, sent


def end_turn(content=None):
    return {"content": content or [{"type": "text", "text": "looked"}],
            "stop_reason": "end_turn", "usage": dict(USAGE)}


def submitted(view, tool_id=OWNER_ID, stop="tool_use"):
    """A turn that CALLED THE TOOL, with a real id on the block."""
    return {"content": [{"type": "tool_use", "id": tool_id,
                         "name": "submit_research_view", "input": view}],
            "stop_reason": stop, "usage": dict(USAGE)}


def refused_at_send_time(sent):
    """Every request the API would have rejected, judged when it was
    made rather than after the conversation finished."""
    return [(i, r["refused"]) for i, r in enumerate(sent)
            if r["refused"] is not None]


def messages_of(sent, i):
    return sent[i]["payload"]["messages"]


class TestTheOwnersFaultIsGone:
    """Driven through the real `investigate`, so the assertion is the
    OUTCOME and not a substring in a function (sections 22, 24)."""

    def test_an_invalid_view_reaches_the_repair_turn_and_it_is_sendable(
            self, db):
        bad = dict(GOOD_VIEW)
        del bad["thesis"]                 # make_view_from_tool_input raises
        transport, log = transport_script([
            end_turn(),                   # exploration, no submission
            submitted(bad),               # forced turn: tool_use, bad view
            submitted(dict(GOOD_VIEW), tool_id="toolu_REPAIRED"),
        ])
        result = investigate(candidate(), ctx(db), transport)

        assert refused_at_send_time(log) == []
        assert result.skipped_reason is None, result.skipped_reason
        assert result.parsed_view is not None
        # THE REPAIR REQUEST IS THE ONE THAT USED TO BE REFUSED.
        assert len(log) == 3, [r["payload"].get("tool_choice")
                               for r in log]
        repair = messages_of(log, 2)
        assert repair[-1]["role"] == "user"
        answered = {b.get("tool_use_id") for b in repair[-1]["content"]
                    if isinstance(b, dict)}
        assert OWNER_ID in answered, repair[-1]["content"]

    def test_a_TRUNCATED_submission_also_reaches_a_sendable_repair(self, db):
        """The other route to the repair branch: `stop_reason` is
        max_tokens, so the tool call is partial - but the block and its
        id are real, so it still has to be answered."""
        transport, log = transport_script([
            end_turn(),
            submitted({"direction": "long"}, stop="max_tokens"),
            submitted(dict(GOOD_VIEW), tool_id="toolu_REPAIRED"),
        ])
        result = investigate(candidate(), ctx(db), transport)
        assert refused_at_send_time(log) == []
        assert result.skipped_reason is None, result.skipped_reason

    def test_the_repair_turn_still_tells_the_model_what_was_wrong(self, db):
        """Answering the call must not cost the reason: a bare
        'submit again' teaches the model nothing and costs the same."""
        bad = dict(GOOD_VIEW)
        del bad["thesis"]
        transport, log = transport_script([
            end_turn(), submitted(bad),
            submitted(dict(GOOD_VIEW), tool_id="toolu_REPAIRED")])
        investigate(candidate(), ctx(db), transport)
        said = " ".join(str(b.get("content") or "")
                        for b in messages_of(log, 2)[-1]["content"]
                        if isinstance(b, dict))
        assert "thesis" in said, said
        assert "submit_research_view" in said, said

    def test_an_early_submission_that_parses_needs_no_repair_at_all(self, db):
        """The change must not cost the short-circuit: a good early
        submission still finishes in one turn."""
        transport, log = transport_script([submitted(dict(GOOD_VIEW))])
        result = investigate(candidate(), ctx(db), transport)
        assert result.parsed_view is not None
        assert len(log) == 1
        assert refused_at_send_time(log) == []


class TestAPausedTurnThatAlsoSubmitted:
    """A `pause_turn` is continued by sending the assistant turn back
    with NO user message after it. If that same turn carries a client
    tool call the API demands a `tool_result` immediately after, and
    both cannot be satisfied - so the loop stops and the extraction path
    answers it properly."""

    def test_it_stops_the_pause_loop_instead_of_sending_a_refused_request(
            self, db):
        paused = {"content": [
            {"type": "server_tool_use", "id": "srv_1", "name": "web_search",
             "input": {"query": "q"}},
            {"type": "tool_use", "id": OWNER_ID,
             "name": "submit_research_view", "input": dict(GOOD_VIEW)}],
            "stop_reason": "pause_turn", "usage": dict(USAGE)}
        transport, log = transport_script([paused])
        result = investigate(candidate(), ctx(db), transport)

        assert refused_at_send_time(log) == []
        # The submission in the paused turn was read and accepted, so no
        # second call was needed at all.
        assert result.parsed_view is not None
        assert len(log) == 1, "the pause loop continued a refused shape"

    def test_a_pure_server_tool_pause_STILL_CONTINUES(self, db):
        """The guard must not have become a brake: a paused turn with
        only server-tool blocks is exactly what continuation is for."""
        paused = {"content": [
            {"type": "server_tool_use", "id": "srv_1", "name": "web_search",
             "input": {"query": "q"}}],
            "stop_reason": "pause_turn", "usage": dict(USAGE)}
        transport, log = transport_script([
            paused, end_turn(), submitted(dict(GOOD_VIEW))])
        result = investigate(candidate(), ctx(db), transport)
        assert len(log) >= 2, "the pause was not continued"
        assert messages_of(log, 1)[-1]["role"] == "assistant", (
            "a continuation must send the assistant turn back bare")
        assert refused_at_send_time(log) == []
        assert result.parsed_view is not None


class TestOneDefinitionOfAClientToolCall:
    """The guard and the answerer each inlined their own copy, which is
    how three call sites came to disagree with the guard at once."""

    def test_a_server_tool_use_is_not_a_client_call(self):
        assert client_tool_use_ids(
            {"content": [{"type": "server_tool_use", "id": "srv_1"}]}) == []

    def test_a_client_tool_use_without_an_id_is_not_answerable(self):
        """It cannot be named in a `tool_result`, so it is not counted -
        and that is exactly why the old suite fixture hid this bug."""
        assert client_tool_use_ids(
            {"content": [{"type": "tool_use", "name": "x"}]}) == []

    def test_the_guard_and_the_answerer_agree_by_construction(self):
        """Not 'both happen to be right today' - the same function."""
        import inspect

        src = inspect.getsource(boundary.invalid_payload_reason)
        assert "client_tool_use_ids" in src
        assert "CLIENT_TOOL_USE" not in src, (
            "the guard has its own copy of the rule again")
        assert "client_tool_use_ids" in inspect.getsource(answer_tool_calls)

    def test_every_id_gets_its_own_result(self):
        """Two calls in one turn need two results, or the API refuses
        the request for the one that was missed."""
        echo = {"content": [
            {"type": "tool_use", "id": "a", "name": "n", "input": {}},
            {"type": "tool_use", "id": "b", "name": "n", "input": {}}]}
        out = answer_tool_calls(echo, "why", ask="do it")
        assert [b["tool_use_id"] for b in out] == ["a", "b"]
        assert all(b["is_error"] for b in out)

    def test_with_nothing_to_answer_it_is_still_plain_text(self):
        """A text-only turn must not gain a `tool_result` for a call that
        was never made - the API rejects that too."""
        got = answer_tool_calls(
            {"content": [{"type": "text", "text": "no call"}]}, None,
            ask="go on")
        assert got == "go on"

    def test_the_ask_comes_from_the_CALLER(self):
        """It was hardcoded to submit_research_view plus 'required fields
        are missing', which names the wrong tool on a review and the
        wrong cause on a truncation."""
        echo = {"content": [{"type": "tool_use", "id": "a", "name": "n",
                             "input": {}}]}
        got = answer_tool_calls(echo, None,
                                ask="Submit your review now via "
                                    "submit_position_review.")
        assert "submit_position_review" in got[0]["content"]
        assert "submit_research_view" not in got[0]["content"]


class TestTheReviewPathHasItToo:
    """Found by auditing the other paid paths rather than by a second
    owner report. `position_review` reaches its forced turn precisely
    BECAUSE an early submission failed, so the echo it appends almost
    always carries a tool_use."""

    def test_the_forced_review_turn_answers_the_early_submission(self):
        review_call = {"type": "tool_use", "id": "toolu_REVIEW",
                       "name": "submit_position_review",
                       "input": {"action": "hold"}}
        messages = [
            {"role": "user", "content": "the review prompt"},
            {"role": "assistant", "content": [review_call]},
            {"role": "user", "content": answer_tool_calls(
                {"content": [review_call]}, None,
                ask="Submit your review now via submit_position_review.")},
        ]
        assert invalid_payload_reason(
            {"messages": messages, "model": "m", "max_tokens": 2048}) is None

    def test_THE_REAL_REVIEW_reaches_a_sendable_forced_turn(self, tmp_path):
        """DRIVEN, NOT GREPPED.

        My first version asserted `"answer_tool_calls" in
        inspect.getsource(review_position)` - and the import of that name
        is INSIDE the function, so the substring matched the import while
        the call site was replaced with plain text. A sabotage doing
        exactly that came back GREEN. That is section 22's corollary
        verbatim: a substring that also occurs elsewhere in the same
        function is not a call-site assertion.

        So this runs the real `review_position` with an injected
        transport whose first turn submits a review that CANNOT be built
        - which is the only way to reach the forced turn - and asserts
        the second request is one the API would accept.
        """
        import catalyst.research.position_review as mod
        from catalyst.storage import init_db

        conn = init_db(str(tmp_path / "rv.db"))
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        opened = NOW - __import__("datetime").timedelta(days=3)
        conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                     ("p1", "RLMD", "[]", None, opened.isoformat(),
                      (NOW + __import__("datetime").timedelta(days=5))
                      .date().isoformat(), "open"))
        conn.commit()

        review_call = {"type": "tool_use", "id": "toolu_REVIEW_EARLY",
                       "name": "submit_position_review",
                       # `action` is not one of ACTIONS, so
                       # make_review_from_tool_input RAISES and the
                       # forced turn is the only way on.
                       "input": {"action": "not_a_real_action"}}
        good = {"type": "tool_use", "id": "toolu_REVIEW_FORCED",
                "name": "submit_position_review",
                "input": {"action": "hold", "invalidation_triggered": False,
                          "reasoning": "the thesis is intact"}}
        responses = [
            {"content": [review_call], "stop_reason": "tool_use",
             "usage": dict(USAGE)},
            {"content": [good], "stop_reason": "tool_use",
             "usage": dict(USAGE)},
        ]
        transport, log = transport_script(responses)

        mod.review_position(
            conn,
            {"id": "p1", "ticker": "RLMD", "opened_at": opened.isoformat(),
             "opened_at_date": opened.date().isoformat(),
             "planned_exit_date": (
                 NOW + __import__("datetime").timedelta(days=5))
             .date().isoformat()},
            {"thesis": "t", "invalidation": "i"},
            {"entry_price": 4.49, "last_price": 5.05, "move_pct": "12",
             "market_is_live": True},
            transport=transport,
            cost_context=CostContext(
                conn=conn, governor_profit_share=Decimal("0.10"),
                cycle_id="cycle-1", kind="scheduled"),
            now=NOW)
        conn.close()

        assert len(log) == 2, (
            "the forced review turn was never reached, so this test "
            f"cannot see the defect: {len(log)} call(s)")
        assert refused_at_send_time(log) == [], refused_at_send_time(log)
        answered = {b.get("tool_use_id")
                    for b in messages_of(log, 1)[-1]["content"]
                    if isinstance(b, dict)}
        assert "toolu_REVIEW_EARLY" in answered, (
            messages_of(log, 1)[-1]["content"])


class TestTheGuardItselfStillRefusesWhatItMust:
    """This change makes the payloads valid; it must not have loosened
    the guard that catches the next way of getting it wrong."""

    def test_plain_text_after_a_tool_use_is_still_refused(self):
        assert invalid_payload_reason({
            "messages": [
                {"role": "user", "content": "p"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": OWNER_ID, "name": "n",
                     "input": {}}]},
                {"role": "user", "content": "submit again please"}],
            "model": "m", "max_tokens": 2048}) is not None

    def test_a_result_for_the_WRONG_id_is_still_refused(self):
        assert invalid_payload_reason({
            "messages": [
                {"role": "user", "content": "p"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_A", "name": "n",
                     "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_B",
                     "content": "x"}]}],
            "model": "m", "max_tokens": 2048}) is not None

    def test_one_answered_and_one_not_is_still_refused(self):
        assert invalid_payload_reason({
            "messages": [
                {"role": "user", "content": "p"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_A", "name": "n",
                     "input": {}},
                    {"type": "tool_use", "id": "toolu_B", "name": "n",
                     "input": {}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_A",
                     "content": "x"}]}],
            "model": "m", "max_tokens": 2048}) is not None


class TestTheExplorationEchoAnswersItsOwnCalls:
    """The gap a sabotage found in my own tests.

    Every test above starts exploration with a plain `end_turn`, so the
    exploration echo never carried a `tool_use` and cutting ITS answer
    changed nothing. The path that matters is an early submission that
    does NOT parse: the model answered during exploration, the view
    failed to build, and the echo on the way to the forced turn still
    carries that call.
    """

    def test_a_bad_early_submission_is_answered_on_the_way_to_extraction(
            self, db):
        bad = dict(GOOD_VIEW)
        del bad["invalidation"]
        transport, log = transport_script([
            submitted(bad),                       # exploration DID submit
            submitted(dict(GOOD_VIEW), tool_id="toolu_FORCED"),
        ])
        result = investigate(candidate(), ctx(db), transport)

        assert refused_at_send_time(log) == []
        assert result.skipped_reason is None, result.skipped_reason
        # The premise: the exploration turn really did call the tool, or
        # this test is the same as the ones above.
        assert client_tool_use_ids(
            {"content": [{"type": "tool_use", "id": OWNER_ID,
                          "name": "submit_research_view", "input": bad}]})
        # The forced turn's last message answers the exploration call.
        answered = {b.get("tool_use_id")
                    for b in messages_of(log, 1)[-1]["content"]
                    if isinstance(b, dict)}
        assert OWNER_ID in answered, messages_of(log, 1)[-1]["content"]
        assert "invalidation" in " ".join(
            str(b.get("content") or "")
            for b in messages_of(log, 1)[-1]["content"]
            if isinstance(b, dict))


class TestTheFixtureItselfIsProductionSHAPED:
    """The blind spot that hid this defect, asserted so it cannot come
    back. A `tool_use` with no `id` cannot be named in a `tool_result`,
    so an id-less fixture exercises only the branch where there is
    nothing to answer - and every boundary test used one."""

    def test_the_shared_extraction_fixture_carries_a_tool_use_id(self):
        from test_boundary import extraction_response

        blocks = extraction_response()["content"]
        assert client_tool_use_ids({"content": blocks}), (
            "the boundary fixture builds a tool_use with no id again, so "
            "no test in that file can reach the unanswered-call defect")

    def test_two_fixture_calls_do_not_reuse_one_id(self):
        """The API never repeats an id, and a `tool_result` has to name
        the right call - a shared id would let a wrong-id bug pass."""
        from test_boundary import extraction_response

        a = client_tool_use_ids({"content": extraction_response()["content"]})
        b = client_tool_use_ids({"content": extraction_response()["content"]})
        assert a and b and a != b, (a, b)
