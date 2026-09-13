"""The research prompt is marked cacheable, and nothing else is.

OWNER-REPORTED 2026-09-13: Anthropic's own usage tip said the prompt
cache hit rate was low and offered a 57% saving. **Measured:
`cache_control` appeared NOWHERE in the request path**, so the rate was
zero by construction rather than by the nature of the bot.

WHAT THESE TESTS PROTECT, and every one of them is a decision that was
measured rather than preferred:

    1. **THE PROMPT IS ASKED TO BE CACHED.** One breakpoint, on the
       prompt, present on every turn of a call. Turn 1 writes it at
       1.25x; turns 2-4 read it at 0.10x. Break-even is two requests and
       the normal call is three or four.
    2. **THE MODEL SEES EXACTLY WHAT IT SAW BEFORE.** The prompt text is
       unchanged - only the container shape moved from a bare string to a
       one-block list, because `cache_control` attaches to a block. If
       this ever alters the prompt, it changes the judgement the whole
       project exists to measure.
    3. **EXACTLY ONE BREAKPOINT.** Four is the API limit and a call runs
       up to four turns; one marker moved nowhere cannot drift into a
       fifth. It also keeps us off the documented way to waste money -
       marking a tail that nothing reads back.
    4. **THE 5-MINUTE TTL, NOT 1-HOUR.** 1-hour doubles the write to 2x
       and only pays across cycles; the cycle is 15 minutes, so each
       cycle's first call would write at 2x for reads that may never
       come.
    5. **THE PAYLOAD IS STILL VALID.** `invalid_payload_reason` is the
       guard that has caught five live research calls dying on a
       malformed request. A content list must pass it.
    6. **THE COST LEDGER STILL PRICES IT.** Cache tokens are billed and
       are not in `input_tokens` (TRAPS.md). They were already captured;
       this asserts the chain end to end, because a saving the ledger
       cannot see would look like a saving whether or not it happened.

WHAT IS DELIBERATELY NOT DONE, and must not be added without evidence:
caching ACROSS calls. Two candidates' prompts share 300 characters, ~75
tokens, 5.3% - the ticker is the third line - and the cacheable minimum
is 1024 tokens on Sonnet 5. Making it work means reordering the prompt so
the standing brief comes first and the candidate last, which changes what
the model reads first. A cost saving is not worth making the live record
incomparable with its own history.

Fully offline.
"""

import inspect
import sqlite3

import pytest

from catalyst.research import boundary
from tests.test_boundary import (  # noqa: F401 - fixtures used by name
    candidate, ctx, db, end_turn, extraction_response, stub_prompts,
    transport_script,
)


class TestTheBreakpointExists:

    def test_the_prompt_carries_a_cache_control_marker(self):
        m = boundary.cacheable_prompt_message("the prompt")
        blocks = m["content"]
        assert isinstance(blocks, list), (
            "cache_control attaches to a content block, so a bare string "
            "can never carry one")
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_it_is_the_DEFAULT_five_minute_ttl(self):
        """1-hour doubles the write to 2x and only pays across cycles; the
        cycle is 15 minutes, so each cycle's first call would write at 2x
        for reads that may never come."""
        block = boundary.cacheable_prompt_message("p")["content"][0]
        assert "ttl" not in block["cache_control"], (
            "an explicit ttl here is the 1-hour TTL at double the write "
            "price, which needs a measured reason")

    def test_exactly_one_breakpoint(self):
        """Four is the API limit and a call runs up to four turns."""
        blocks = boundary.cacheable_prompt_message("p")["content"]
        assert sum(1 for b in blocks if "cache_control" in b) == 1

    def test_investigate_actually_uses_it(self):
        """Section 6 of the memory doc, four times over: a helper nobody
        calls passes its own tests. Assert the CALL SITE."""
        src = inspect.getsource(boundary.investigate)
        assert "cacheable_prompt_message(prompt)" in src, (
            "the prompt is built without the marker, so nothing is cached")
        assert '{"role": "user", "content": prompt}' not in src, (
            "the old unmarked message is still being built")


class TestTheModelSeesTheSamePrompt:
    """The one property that outranks the saving."""

    def test_the_text_is_byte_identical(self):
        prompt = "GRAPH: acme\nCANDIDATE\nTicker: AAPL\n...long brief..."
        m = boundary.cacheable_prompt_message(prompt)
        assert m["content"][0]["text"] == prompt

    def test_the_role_is_still_user(self):
        assert boundary.cacheable_prompt_message("p")["role"] == "user"

    def test_nothing_is_added_to_the_text(self):
        """No marker, no preamble, no separator - a cache hint must not
        become something the model reads."""
        m = boundary.cacheable_prompt_message("exactly this")
        assert [b["type"] for b in m["content"]] == ["text"]
        assert m["content"][0]["text"] == "exactly this"

    def test_an_empty_prompt_is_still_refused_downstream(self):
        """The payload guard must keep catching an empty prompt - wrapping
        it in a block must not smuggle one past."""
        m = boundary.cacheable_prompt_message("")
        why = boundary.invalid_payload_reason(
            {"model": "claude-sonnet-5", "max_tokens": 10, "messages": [m]})
        assert why, "an empty prompt now passes the guard"


class TestThePayloadIsStillValid:
    """`invalid_payload_reason` has caught five live calls dying on a
    malformed request."""

    def test_a_marked_message_passes_the_guard(self):
        m = boundary.cacheable_prompt_message("a real prompt")
        assert boundary.invalid_payload_reason({
            "model": "claude-sonnet-5", "max_tokens": 100,
            "messages": [m]}) is None

    def test_every_turn_of_a_real_call_sends_a_valid_payload(self, db):
        """The guard runs on each turn, so the marker has to survive the
        assistant echoes and the forced extraction turn too."""
        transport, log = transport_script([end_turn(), extraction_response()])
        boundary.investigate(candidate(), ctx(db), transport)
        assert log, "no request was sent"
        for i, payload in enumerate(log):
            why = boundary.invalid_payload_reason(payload)
            assert why is None, f"turn {i} payload invalid: {why}"

    def test_the_marker_is_on_every_turn_not_just_the_first(self, db):
        """The prompt is the SAME bytes on every turn - that is the whole
        mechanism. A marker present only on turn 1 would write an entry
        and then never offer a read point."""
        transport, log = transport_script([end_turn(), extraction_response()])
        boundary.investigate(candidate(), ctx(db), transport)
        assert len(log) >= 2, "expected an exploration and an extraction turn"
        for i, payload in enumerate(log):
            first = payload["messages"][0]["content"]
            assert isinstance(first, list), f"turn {i} lost the block list"
            assert first[0].get("cache_control") == {"type": "ephemeral"}, (
                f"turn {i} sent the prompt unmarked, so it cannot be read "
                "from cache")

    def test_the_prompt_bytes_are_identical_across_turns(self, db):
        """A cache is a byte match. If anything in the prompt varied
        between turns - a timestamp, a counter - every turn would miss and
        this change would be a pure surcharge."""
        transport, log = transport_script([end_turn(), extraction_response()])
        boundary.investigate(candidate(), ctx(db), transport)
        texts = {p["messages"][0]["content"][0]["text"] for p in log}
        assert len(texts) == 1, (
            "the prompt differs between turns, so the cache can never hit - "
            "something volatile is being rendered into it")


class TestTheLedgerStillSeesTheCost:
    """Cache tokens are billed and are NOT in input_tokens (TRAPS.md).
    A saving the ledger cannot see looks the same whether or not it
    happened."""

    def test_cache_fields_are_recorded_from_a_real_call(self, db):
        transport, _log = transport_script([end_turn(), extraction_response()])
        log = boundary.investigate(candidate(), ctx(db), transport)
        usages = [t.usage for t in log.api_turns if t.usage]
        assert usages, "no usage was recorded"
        for u in usages:
            assert hasattr(u, "cache_read_input_tokens")
            assert hasattr(u, "cache_creation_input_tokens")

    def test_a_cache_read_is_priced_at_a_tenth(self):
        """The multiplier the saving depends on, asserted rather than
        assumed - and read from the module the reconciliation corrects."""
        from catalyst.cost import pricing

        assert pricing.CACHE_READ_MULTIPLIER < pricing.CACHE_WRITE_MULTIPLIER
        from decimal import Decimal

        assert pricing.CACHE_READ_MULTIPLIER == Decimal("0.10")
        assert pricing.CACHE_WRITE_MULTIPLIER == Decimal("1.25")
        # Two reads must beat two uncached sends, or the change is a loss.
        write_then_read = (pricing.CACHE_WRITE_MULTIPLIER
                           + pricing.CACHE_READ_MULTIPLIER)
        assert write_then_read < 2, (
            f"caching does not pay by the second request: {write_then_read}")


class TestCachingAcrossCallsIsNotAttempted:
    """Recorded as a decision, because the obvious "improvement" here is
    to reorder the prompt - and that would change the judgement."""

    def test_two_candidates_share_far_too_little_to_cache(self):
        """The measurement that settles it: 1024 tokens is the minimum on
        Sonnet 5, and two prompts share ~75."""
        from datetime import date, datetime, timezone

        from catalyst.discovery.candidates import Candidate
        from catalyst.research import prompts

        def cand(cid, ticker):
            return Candidate(
                id=cid, ticker=ticker, catalyst_type="insider_cluster",
                catalyst_date=date(2026, 9, 15),
                catalyst_date_confidence="confirmed", source_event_ids=("e1",),
                discovered_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
                sector="Technology", correlation_tags=("tech",))

        a = prompts.render_research_prompt(cand("c1", "AAPL"))
        b = prompts.render_research_prompt(cand("c2", "NVDA"))
        shared = 0
        for x, y in zip(a, b):
            if x != y:
                break
            shared += 1
        # Four characters per token is generous; even so this is nowhere
        # near a cacheable prefix.
        assert shared // 4 < 512, (
            f"the shared prefix is now {shared} chars (~{shared//4} tokens). "
            "If the prompt has been reordered so the standing brief comes "
            "first, cross-call caching became possible - but that also "
            "changed what the model reads first, which changes the "
            "judgement this project measures. Decide that deliberately, "
            "not as a side effect.")

    def test_the_reason_is_written_down_where_the_next_session_will_look(self):
        doc = inspect.getdoc(boundary.cacheable_prompt_message) or ""
        assert "ACROSS CALLS IT CANNOT WORK" in doc
        assert "REORDERING" in doc, (
            "the next session must be told why the obvious fix is refused")
