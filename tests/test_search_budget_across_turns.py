"""`max_uses` is per REQUEST, not per investigation.

prompts.exploration_tools() says "max_uses IS the search budget for one
investigation". That is true only while an investigation is one request.
It is not: boundary.py continues a `pause_turn` by re-sending the same
tools list, and a fresh request carries a fresh `max_uses`. So a
conjunction candidate authorised for CONJUNCTION_SEARCHES can spend
twice that across MAX_EXPLORATION_TURNS turns.

Web search is $10 per 1,000 queries (TRAPS.md) on top of tokens, and it
is the one line of the bill the model controls directly. A doubled
budget is not a rounding error against a $5-30 monthly cap: at 30
conjunction candidates a month it is $3.00 of search where the
arithmetic in prompts.py budgeted $2.10 total.

The budget must be what the candidate EARNED, counted across the whole
investigation.
"""

import sqlite3
from decimal import Decimal

import pytest

from catalyst.research import boundary, prompts


def _tools_of(payload):
    return {t.get("name", t.get("type")): t for t in payload.get("tools", [])}


def _search_budget(payload):
    """max_uses on the web_search tool, or None if it was not offered."""
    tool = _tools_of(payload).get("web_search")
    return None if tool is None else tool.get("max_uses")


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "b.db"))
    c.executescript(open("catalyst/storage/schema.sql").read())
    c.commit()
    return c


def _cost_context(conn):
    return boundary.CostContext(
        conn=conn, governor_profit_share=Decimal("0"),
        cycle_id="cyc-1", kind="scheduled")


def _candidate():
    from catalyst.discovery import Candidate
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return Candidate(
        id="cand-search", ticker="SRCH", catalyst_type="insider_cluster",
        catalyst_date=now.date() + timedelta(days=5),
        catalyst_date_confidence="confirmed", source_event_ids=("e1",),
        discovered_at=now, sector="tech", correlation_tags=("tech",))


def _two_source_signals():
    """Two distinct sources => searches_for() grants the conjunction
    allowance. This is the expensive path, so it is the one to bound.
    The REAL Signal type, so a field renamed upstream breaks this test
    rather than letting it pass against a stand-in."""
    from datetime import date

    from catalyst.discovery.links import Signal

    return [
        Signal(source="edgar_form4", catalyst_type="insider_cluster",
               when=date.today(), source_id="s1"),
        Signal(source="alpaca_news", catalyst_type="news",
               when=date.today(), source_id="s2"),
    ]


def _pause_then_pause(searches_per_turn):
    """A transport that burns `searches_per_turn` searches and asks to
    continue - the exact shape that re-sends the tools list."""
    payloads = []

    def transport(payload):
        payloads.append(payload)
        forced = (payload.get("tool_choice") or {}).get("type") == "tool"
        if forced:
            return {
                "id": "m", "model": "claude-sonnet-5",
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": "t",
                             "name": "submit_research_view",
                             "input": {"direction": "no_trade",
                                       "conviction": 0.1, "thesis": "t",
                                       "invalidation": "i",
                                       "expected_holding_days": 5,
                                       "priced_in": True,
                                       "priced_in_reasoning": "r"}}],
                "usage": {"input_tokens": 100, "output_tokens": 10},
            }
        return {
            "id": "m", "model": "claude-sonnet-5",
            "stop_reason": "pause_turn",
            "content": [{"type": "text", "text": "still looking"}],
            "usage": {"input_tokens": 1000, "output_tokens": 100,
                      "server_tool_use": {
                          "web_search_requests": searches_per_turn}},
        }

    return transport, payloads


class TestTheSearchBudgetIsPerInvestigation:
    def test_a_continuation_does_not_refill_the_search_budget(self, conn):
        """THE MONEY. Turn one spends the whole allowance; turn two must
        not be handed a fresh one."""
        budget = prompts.CONJUNCTION_SEARCHES
        transport, payloads = _pause_then_pause(searches_per_turn=budget)
        signals = _two_source_signals()

        boundary.investigate(_candidate(), _cost_context(conn), transport,
                             signals=signals)

        exploration = [p for p in payloads
                       if (p.get("tool_choice") or {}).get("type") != "tool"]
        assert len(exploration) >= 2, (
            "the transport paused, so there must be a continuation turn "
            "to inspect - otherwise this test proves nothing")
        granted = [_search_budget(p) for p in exploration]
        assert granted[0] == budget, granted
        total = sum(g for g in granted if g)
        assert total <= budget, (
            f"search budgets granted across the investigation: {granted} "
            f"= {total} searches, but this candidate earned {budget}. "
            "max_uses is per REQUEST; the continuation refilled it.")

    def test_a_spent_budget_removes_the_tool_rather_than_offering_zero(
            self, conn):
        """max_uses=0 is not a documented way to say 'no searches left'.
        Offering the tool with a zero budget invites a rejected request,
        which costs a paid call and tells the owner nothing."""
        budget = prompts.CONJUNCTION_SEARCHES
        transport, payloads = _pause_then_pause(searches_per_turn=budget)
        signals = _two_source_signals()

        boundary.investigate(_candidate(), _cost_context(conn), transport,
                             signals=signals)

        exploration = [p for p in payloads
                       if (p.get("tool_choice") or {}).get("type") != "tool"]
        assert _search_budget(exploration[1]) is None, (
            "with the budget spent, web_search should not be offered at "
            f"all; got max_uses={_search_budget(exploration[1])}")

    def test_the_submit_tool_survives_a_spent_search_budget(self, conn):
        """Removing web_search must not remove the schema tool - that
        would delete the short-circuit that avoids the full-price
        extraction re-read."""
        transport, payloads = _pause_then_pause(
            searches_per_turn=prompts.CONJUNCTION_SEARCHES)
        signals = _two_source_signals()

        boundary.investigate(_candidate(), _cost_context(conn), transport,
                             signals=signals)

        exploration = [p for p in payloads
                       if (p.get("tool_choice") or {}).get("type") != "tool"]
        assert "submit_research_view" in _tools_of(exploration[1])

    def test_a_PARTLY_spent_budget_grants_only_the_remainder(self, conn):
        """Not a blunt all-or-nothing: a turn that used 2 of 10 must
        leave 8 available, not 0 and not 10."""
        budget = prompts.CONJUNCTION_SEARCHES
        transport, payloads = _pause_then_pause(searches_per_turn=2)
        signals = _two_source_signals()

        boundary.investigate(_candidate(), _cost_context(conn), transport,
                             signals=signals)

        exploration = [p for p in payloads
                       if (p.get("tool_choice") or {}).get("type") != "tool"]
        assert _search_budget(exploration[1]) == budget - 2, (
            f"used 2 of {budget}; the continuation should offer "
            f"{budget - 2}, got {_search_budget(exploration[1])}")
