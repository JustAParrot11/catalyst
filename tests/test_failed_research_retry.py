"""A paid research call that FAILED must not be re-bought every cycle.

cost-auditor, 2026-08-13. The screen at cycle.py is:

    SELECT 1 FROM research_views WHERE candidate_id=?   -> already_researched

but `research_views` is only written when a view PARSES
(boundary.py:459). A paid-but-failed investigation - invalid view,
truncated extraction, transport error, ambiguous tool call - leaves no
row the screen can see, so the candidate is fully fresh again fifteen
minutes later. Forever.

boundary.py's own comment records that four of five live research calls
died on 2026-08-10, so this is not a hypothetical failure rate.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst.orchestrator.cycle import run_cycle
from catalyst.research import prompts

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def _cheap_prompts(monkeypatch):
    monkeypatch.setattr(prompts, "render_research_prompt", lambda c, **kw: "r")
    monkeypatch.setattr(prompts, "exploration_tools", lambda *a, **kw: [])


def _broker(handler):
    from catalyst.execution.broker import Broker

    return Broker("k", "s", transport=httpx.MockTransport(handler),
                  backoff_s=0)


def _account(request):
    path = request.url.path
    if path.endswith("/account"):
        return httpx.Response(200, json={
            "id": "a", "equity": "1000", "cash": "1000",
            "last_equity": "1000", "buying_power": "1000",
            "non_marginable_buying_power": "1000", "status": "ACTIVE"})
    if path.endswith("/clock"):
        return httpx.Response(200, json={"is_open": True})
    if "/positions" in path or "/orders" in path:
        return httpx.Response(200, json=[])
    if "/quotes/latest" in path or "/bars" in path:
        # The quote MUST carry a fresh timestamp: build_market_snapshot
        # refuses an undatable or stale quote, and without one research
        # never runs at all - which made the first version of this test
        # pass vacuously with zero research calls.
        return httpx.Response(200, json={
            "quote": {"ap": 10.05, "bp": 9.95, "as": 100, "bs": 100,
                      "t": datetime.now(timezone.utc).isoformat()},
            "bars": {}})
    return httpx.Response(200, json={})


def _candidate(as_of):
    from catalyst.discovery import Candidate

    return Candidate(
        id="cand-stuck", ticker="STUCK", catalyst_type="insider_cluster",
        catalyst_date=as_of.date() + timedelta(days=5),
        catalyst_date_confidence="confirmed", source_event_ids=("e1",),
        discovered_at=as_of, sector="tech", correlation_tags=("tech",))


def _always_invalid(payload):
    """A view missing a required field - the exact failure boundary.py
    documents as happening on real runs ("real runs omitted a different
    required field each time")."""
    return {
        "id": "msg_1", "stop_reason": "tool_use", "model": "claude-sonnet-5",
        "content": [{"type": "tool_use", "id": "t1",
                     "name": "submit_research_view",
                     "input": {"direction": "long", "conviction": 0.8}}],
        "usage": {"input_tokens": 1000, "output_tokens": 100},
    }


def _run(db_path, cycles):
    conn = sqlite3.connect(db_path)
    calls = []

    def transport(payload):
        calls.append(payload)
        return _always_invalid(payload)

    for _ in range(cycles):
        run_cycle(conn, _broker(_account), transport,
                  lambda since, until: [],
                  lambda events, as_of: [_candidate(as_of)],
                  lambda fresh, open_pos: {c.id: "tech" for c in fresh})
    n_calls = conn.execute("SELECT COUNT(*) FROM research_calls").fetchone()[0]
    n_views = conn.execute("SELECT COUNT(*) FROM research_views").fetchone()[0]
    conn.close()
    return len(calls), n_calls, n_views


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "r.db")
    conn = sqlite3.connect(path)
    conn.executescript(open("catalyst/storage/schema.sql").read())
    conn.commit()
    conn.close()
    return path


class TestAFailedCallIsNotReBoughtForever:
    def test_a_stuck_candidate_stops_being_researched(self, db):
        """THE MONEY. Without a bound this is ~51c per cycle on ONE
        candidate; at 26 market-hours cycles a day that is $13/day, and
        the whole $5 monthly cap is gone in under an hour - spent
        entirely on a candidate that never produces anything."""
        _api, n_calls, n_views = _run(db, cycles=6)
        assert n_views == 0, "the fixture must never produce a valid view"
        assert n_calls <= 2, (
            f"{n_calls} paid research calls for one stuck candidate over 6 "
            "cycles - the failed call is being re-bought every cycle")

    def test_the_funnel_says_WHY_it_stopped(self, db):
        """A candidate that silently stops being researched is worse than
        one that is researched forever: the funnel would show it
        vanishing with no reason."""
        conn = sqlite3.connect(db)

        def transport(payload):
            return _always_invalid(payload)

        report = None
        for _ in range(4):
            report = run_cycle(conn, _broker(_account), transport,
                               lambda since, until: [],
                               lambda events, as_of: [_candidate(as_of)],
                               lambda fresh, open_pos: {c.id: "tech" for c in fresh})
        conn.close()
        reasons = " ".join(report.drop_reasons.get("screened", [])
                           + report.drop_reasons.get("researched", []))
        assert "research" in reasons.lower()
        assert any(word in reasons.lower()
                   for word in ("failed", "attempt", "twice")), reasons

    def test_ONE_failure_does_not_burn_the_candidate(self, db):
        """A single 529 or a transient transport error must not
        permanently discard a good candidate - the bound is on repeated
        failure, not on any failure at all."""
        conn = sqlite3.connect(db)
        seen = []

        def flaky(payload):
            seen.append(1)
            if len(seen) == 1:
                raise RuntimeError("transient overload")
            return {
                "id": "m", "stop_reason": "tool_use", "model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "id": "t", "name":
                             "submit_research_view",
                             "input": {"direction": "no_trade",
                                       "conviction": 0.2, "thesis": "t",
                                       "invalidation": "i",
                                       "expected_holding_days": 5,
                                       "priced_in": False,
                                       "priced_in_reasoning": "r"}}],
                "usage": {"input_tokens": 900, "output_tokens": 80},
            }

        for _ in range(3):
            run_cycle(conn, _broker(_account), flaky,
                      lambda since, until: [],
                      lambda events, as_of: [_candidate(as_of)],
                      lambda fresh, open_pos: {c.id: "tech" for c in fresh})
        n_views = conn.execute(
            "SELECT COUNT(*) FROM research_views").fetchone()[0]
        conn.close()
        assert n_views == 1, (
            "a candidate that failed once then succeeded must still produce "
            "a view - the bound is on repeated failure, not any failure")
