"""The owner's emergency stop. MONEY-CRITICAL.

OWNER-ASKED 2026-09-15, after a day that spent $10.02 against a $10.00
daily ceiling: *"Add an emergency pause button that suspends everything
and just lets current trades that are active just sit until they hit the
hard exit data incase we suddenly run out of money. The bot just
suspends claude API activity and wont trade except sell what it
currently has at the date."*

THE TWO HALVES ARE EQUALLY LOAD-BEARING and they pull in opposite
directions, so both are tested through the real code path rather than by
reading it:

  1. NOTHING IS SPENT AND NOTHING IS BOUGHT. `governor.authorize`
     refuses every kind, and the cycle blocks entries.
  2. EVERY EXIT STILL WORKS. A position still sells on its hard exit
     date and its stop is still re-placed. The existing
     `TestKillTripProtection` in test_cycle.py proves the same property
     for a kill switch; this is the identical shape for a switch the
     owner throws by hand, and it is the half that would be dangerous to
     get wrong - a pause that stopped the exits would turn "hold days to
     weeks" into an open-ended hold and leave positions unprotected
     overnight, while reading to the owner as caution.

AND AN UNREADABLE SWITCH IS NOT AN ENGAGED ONE, but it can never
RELEASE one either - the asymmetry sections 16, 22, 27 and 31 each needed
and which was got wrong by guessing every time.
"""

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst import emergency_stop
from catalyst.cost import CostEstimate
from catalyst.cost.governor import authorize
from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from catalyst.dashboard.server import set_emergency_stop
from catalyst.execution.broker import Broker
from catalyst.orchestrator.cycle import run_cycle
from catalyst.research import prompts
from catalyst.storage import init_db

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)

ACCOUNT = {"equity": "1000", "cash": "1000", "last_equity": "1000",
           "buying_power": "1000", "id": "acct-1", "account_number": "A1"}


@pytest.fixture
def conn(tmp_path):
    """Through `init_db`, so foreign keys are ON as they are in
    production (WHAT-WE-TRIED section 14)."""
    c = init_db(str(tmp_path / "t.db"))
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    yield c
    c.close()


@pytest.fixture(autouse=True)
def forget_the_remembered_state():
    """The fallback memory is keyed by database path and lives for the
    process. Cleared between tests so one test's engaged stop cannot
    decide another's fallback."""
    emergency_stop._LAST_KNOWN.clear()
    yield
    emergency_stop._LAST_KNOWN.clear()


@pytest.fixture(autouse=True)
def frozen_kill_switch_clock(monkeypatch):
    import catalyst.risk.kill_switches as kill_switches

    class _FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(kill_switches, "datetime", _FrozenClock)


@pytest.fixture(autouse=True)
def stub_prompts(monkeypatch):
    monkeypatch.setattr(prompts, "render_research_prompt",
                        lambda c, **kw: "research")
    monkeypatch.setattr(prompts, "exploration_tools", lambda *a, **kw: [])


def _estimate(kind="scheduled", component="research"):
    return CostEstimate(estimated_cents=Decimal("20"), basis="test",
                        kind=kind, component=component)


def _ask(conn, kind="scheduled"):
    return authorize(_estimate(kind), conn, Decimal("0.10"),
                     owner_monthly_cap_cents=Decimal("10000"))


def _seed_open_position(db, *, due=False, stop_id="brok-stop", qty="2"):
    """Lifted from test_cycle.py's own fixture, so the two tests of this
    property are the same shape."""
    db.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
               ("cand-old", "OLDPOS", "insider_cluster", "2026-08-01",
                "confirmed", "[]", "insider_cluster", "{}",
                "2026-08-01T12:00:00+00:00"))
    db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
               ("ord-buy", "cand-old", "b1", "buy", qty, "market", "day",
                "2026-08-01T14:00:00+00:00", "filled", "{}"))
    db.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
               ("ord-buy", "50.00", qty, "2026-08-01T14:00:00+00:00",
                "50.00"))
    db.execute(
        "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("dec-old", "cand-old", "trade", "long", "100", qty, "45.00",
         "2026-08-20", "[]", "{}", "2026-08-01T13:00:00+00:00"))
    db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
               ("pos-old", "OLDPOS", json.dumps(["ord-buy"]), stop_id,
                "2026-08-01T14:00:00+00:00",
                "2026-08-09" if due else "2026-08-20", "open"))
    db.commit()


class TestNothingIsSpent:
    def test_scheduled_spend_is_refused(self, conn):
        emergency_stop.engage(conn)
        got = _ask(conn)
        assert not got.authorized
        assert got.reason == emergency_stop.STOP_REASON

    def test_MANUAL_spend_is_refused_too(self, conn):
        """"Suspends claude API activity" has no carve-out for a human at
        a keyboard, and the switch exists for the case where there is no
        money left - which does not care who is spending it."""
        emergency_stop.engage(conn)
        got = _ask(conn, "manual")
        assert not got.authorized
        assert got.reason == emergency_stop.STOP_REASON

    def test_it_refuses_AHEAD_of_the_integrity_gate(self, conn):
        """Both refuse, so the reason is the only observable difference -
        and the owner has to be told which thing stopped their bot. A
        stop they threw themselves is not an unpriced row to go and
        investigate."""
        conn.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at) VALUES (?,?,?,?,?,?,?)",
            ("ce-1", "{}", "m", "scheduled", "research", None,
             NOW.isoformat()))
        conn.commit()
        assert _ask(conn).reason == "unpriced_cost_rows"
        emergency_stop.engage(conn)
        assert _ask(conn).reason == emergency_stop.STOP_REASON

    def test_releasing_lets_spending_resume(self, conn):
        emergency_stop.engage(conn)
        assert not _ask(conn).authorized
        emergency_stop.release(conn)
        assert _ask(conn).authorized

    def test_every_refusal_is_in_the_governor_record(self, conn):
        emergency_stop.engage(conn)
        _ask(conn)
        rows = conn.execute(
            "SELECT decision, reason FROM cost_governor_events").fetchall()
        assert ("deny", emergency_stop.STOP_REASON) in [tuple(r) for r in rows]


class TestEveryExitStillWorks:
    """THE HALF THAT WOULD BE DANGEROUS TO GET WRONG, driven through the
    real `run_cycle` - not by reading which line sets `block_entries`."""

    def _cycle(self, conn, *, due):
        _seed_open_position(conn, due=due, stop_id=None)
        emergency_stop.engage(conn)
        state = {"posts": []}

        def handler(request):
            url = str(request.url)
            if "/v2/account" in url:
                return httpx.Response(200, json=ACCOUNT)
            if request.method == "POST" and url.endswith("/v2/orders"):
                state["posts"].append(json.loads(request.content))
                return httpx.Response(200, json={"id": "x",
                                                 "status": "accepted"})
            if "by_client_order_id" in url:
                return httpx.Response(200, json={
                    "id": "b1", "status": "filled", "filled_qty": "2",
                    "filled_avg_price": "50.00",
                    "filled_at": "2026-08-01T14:00:00Z"})
            if "/v2/clock" in url:
                return httpx.Response(200, json={"is_open": True})
            if "/v2/positions" in url:
                return httpx.Response(200, json=[])
            if "/v2/orders" in url:
                return httpx.Response(200, json=[])
            return httpx.Response(404, json={})

        broker = Broker("k", "s", transport=httpx.MockTransport(handler),
                        backoff_s=0)

        def transport(_payload):
            raise AssertionError(
                "a paid model call was made while suspended")

        report = run_cycle(conn, broker, transport, lambda s, u: [],
                           lambda e, a: [], lambda c, o: {}, now=NOW)
        return report, state["posts"]

    def test_a_DUE_position_still_sells_at_market(self, conn):
        _report, posts = self._cycle(conn, due=True)
        sells = [p for p in posts if p.get("side") == "sell"]
        assert len(sells) == 1, posts
        assert sells[0]["type"] == "market"

    def test_an_undue_position_gets_its_stop_re_placed(self, conn):
        """Fractional stops are DAY-only and expire nightly (TRAPS.md).
        A pause that stopped re-placing them would leave an open
        position's downside unbounded overnight - INCREASING risk while
        reading as caution."""
        _report, posts = self._cycle(conn, due=False)
        stops = [p for p in posts if p.get("type") in ("stop", "stop_limit")]
        assert stops, posts
        assert all(p.get("side") == "sell" for p in stops)

    def test_no_model_call_is_made(self, conn):
        """The transport raises if reached, so reaching the end of a
        cycle at all is the assertion."""
        report, _posts = self._cycle(conn, due=False)
        assert report is not None


class TestNoNewPositionIsOpened:
    def test_the_cycle_blocks_entries_and_names_the_switch(self, conn):
        from catalyst.discovery import Candidate
        from datetime import date as _date

        emergency_stop.engage(conn)
        cand = Candidate(
            id="cand-new", ticker="NEWCO", catalyst_type="insider_cluster",
            catalyst_date=_date(2026, 8, 20),
            catalyst_date_confidence="confirmed", source_event_ids=("e1",),
            discovered_at=NOW, sector="tech", correlation_tags=("tech",))

        def handler(request):
            url = str(request.url)
            if "/v2/account" in url:
                return httpx.Response(200, json=ACCOUNT)
            if "/v2/clock" in url:
                return httpx.Response(200, json={"is_open": True})
            if request.method == "POST":
                raise AssertionError("an order was placed while suspended")
            return httpx.Response(200, json=[])

        broker = Broker("k", "s", transport=httpx.MockTransport(handler),
                        backoff_s=0)

        def transport(_payload):
            raise AssertionError("a paid model call was made while suspended")

        run_cycle(conn, broker, transport, lambda s, u: [],
                  lambda e, a: [cand], lambda c, o: {}, now=NOW)
        rows = conn.execute(
            "SELECT skipped_reason FROM research_calls "
            "WHERE candidate_id = ?", ("cand-new",)).fetchall()
        assert rows, "the candidate left no record of why it stopped"
        assert any(emergency_stop.STOP_REASON in str(r[0]) for r in rows), \
            [r[0] for r in rows]

    def test_the_funnel_calls_it_a_limit_and_not_a_fault(self):
        """A bot obeying the owner is working. Tagging it FAULT in red is
        the "routine attrition reading as damage" failure CLAUDE.md says
        has already cost real debugging time twice - and it would fire
        on the `researched` stage, where an unknown reason defaults to
        FAULT."""
        assert queries.skip_kind(
            f"research skipped: {emergency_stop.STOP_REASON}",
            "researched") == "LIMIT"


class TestTheSwitchItself:
    def test_no_rows_means_running(self, conn):
        assert emergency_stop.current(conn).engaged is False

    def test_the_newest_row_wins(self, conn):
        emergency_stop.engage(conn)
        emergency_stop.release(conn)
        emergency_stop.engage(conn)
        assert emergency_stop.is_engaged(conn) is True

    def test_nothing_is_ever_deleted(self, conn):
        emergency_stop.engage(conn, reason="first")
        emergency_stop.release(conn, reason="second")
        past = emergency_stop.history(conn)
        assert [h["reason"] for h in past] == ["second", "first"]
        assert conn.execute(
            "SELECT COUNT(*) FROM emergency_stop_events").fetchone()[0] == 2

    def test_an_unreadable_switch_is_not_an_engaged_one(self, conn):
        """Inventing a pause from a database hiccup would suspend the bot
        with nobody having asked - section 16's failure, a transient
        answer becoming a verdict. The budget caps do not depend on this
        table, so failing open here removes no money guard."""
        conn.execute("DROP TABLE emergency_stop_events")
        conn.commit()
        state = emergency_stop.current(conn)
        assert state.engaged is False
        assert state.read_failed, "the raw reason must be carried"
        assert state.from_memory is True

    def test_a_FAILED_READ_CANNOT_RELEASE_AN_ENGAGED_STOP(self, conn):
        """The other half, and the one with money on it. A process that
        has seen the stop engaged keeps it engaged; only a successful
        read of a release lifts it."""
        emergency_stop.engage(conn)
        assert emergency_stop.is_engaged(conn) is True
        conn.execute("DROP TABLE emergency_stop_events")
        conn.commit()
        state = emergency_stop.current(conn)
        assert state.engaged is True, "a failed read released the stop"
        assert state.read_failed and state.from_memory
        # AND THE GATE STILL REFUSES, which is what actually matters.
        assert _ask(conn).reason == emergency_stop.STOP_REASON

    def test_an_unrecognised_state_word_is_not_engaged(self, conn):
        """The CHECK constraint makes this unreachable through the
        database. If somebody relaxes it, an unreadable word must not be
        able to suspend the bot by accident."""
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO emergency_stop_events VALUES (?,?,?,?,?)",
                ("x", "halted", "", "", NOW.isoformat()))
        conn.rollback()


class TestTheDashboardButton:
    def test_engaging_is_one_click(self, tmp_path, conn):
        """A switch for "we are running out of money" that needs a typed
        confirmation is a switch that is not there when it is wanted -
        and engaging spends nothing and buys nothing."""
        okay, message = set_emergency_stop(conn_path(conn), "engage", "")
        assert okay, message
        assert emergency_stop.is_engaged(conn) is True

    def test_releasing_needs_the_word(self, conn):
        emergency_stop.engage(conn)
        for confirm in ("", "yes", "resume please", "RESUMEX"):
            okay, message = set_emergency_stop(
                conn_path(conn), "release", confirm)
            assert not okay, confirm
            assert "RESUME" in message
            assert emergency_stop.is_engaged(conn) is True
        okay, _ = set_emergency_stop(conn_path(conn), "release", "RESUME")
        assert okay
        assert emergency_stop.is_engaged(conn) is False

    def test_the_dashboards_own_connection_cannot_write_the_switch(
            self, conn):
        """`Db` opens the database READ-ONLY, so no rendering path can
        ever flip this switch - only the POST handler's own writable
        connection can. Found by running it: the first version of this
        check wrote through `Db.conn` and got "attempt to write a
        readonly database", which is the property rather than a
        problem."""
        db = Db(conn_path(conn))
        try:
            with pytest.raises(sqlite3.OperationalError):
                emergency_stop.engage(db.conn)
        finally:
            db.close()

    def test_the_panel_renders_in_BOTH_states(self, conn):
        db = Db(conn_path(conn))
        try:
            running = panels.emergency_stop_panel(db, p="estop")
            # THE PROPERTY, NOT THE PHRASE. The first version of this
            # asserted the sentence "running normally", which then had
            # to be trimmed to nine words to stay inside the Overview's
            # word budget - so a test of mine broke on a change that
            # altered no behaviour, for the third time in three days
            # (WHAT-WE-TRIED sections 26, 27, 35). What must hold is
            # that the switch is offered and the page does not claim to
            # be suspended.
            assert "SUSPEND EVERYTHING NOW" in running
            assert "SUSPENDED" not in running
            assert "Type RESUME" not in running
            emergency_stop.engage(conn)
            stopped = panels.emergency_stop_panel(db, p="estop")
            assert "THE BOT IS SUSPENDED" in stopped
            assert "SUSPEND EVERYTHING NOW" not in stopped
            assert "Type RESUME" in stopped
        finally:
            db.close()

    def test_the_engaged_panel_says_what_STILL_happens(self, conn):
        """"Everything is suspended" would be false and frightening in
        the wrong direction."""
        emergency_stop.engage(conn)
        db = Db(conn_path(conn))
        try:
            html = panels.emergency_stop_panel(db, p="estop")
            assert "still resting at the broker" in html
            assert "hard exit date arrives" in html
        finally:
            db.close()

    def test_a_suspended_bot_cannot_read_as_a_quiet_one(self, conn):
        emergency_stop.engage(conn)
        db = Db(conn_path(conn))
        try:
            assert "SUSPENDED by you" in panels.state_line(db, p="s")
        finally:
            db.close()

    def test_the_panel_says_so_when_it_cannot_read_the_switch(self, conn):
        emergency_stop.engage(conn)
        db = Db(conn_path(conn))
        try:
            panels.emergency_stop_panel(db, p="estop")   # prime the memory
            conn.execute("DROP TABLE emergency_stop_events")
            conn.commit()
            html = panels.emergency_stop_panel(db, p="estop")
            assert "could not be read" in html
            assert "SUSPENDED" in html
            assert "no such table" in html, "house rule 3: the raw response"
        finally:
            db.close()


def conn_path(conn) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if str(row[1]) == "main":
            return str(row[2])
    raise AssertionError("no main database")


class TestNothingHereCanSizeOrOrder:
    def test_the_switch_reaches_no_sizing_code(self):
        """It can only ever REFUSE. A switch that could reach sizing
        would be a model-supplied number by another route."""
        from tests.source_guard import source_matches

        for name in ("emergency_stop", "STOP_REASON"):
            hits = source_matches(name, "catalyst/risk")
            assert not hits, hits

    def test_risk_sizing_is_untouched(self):
        from tests.source_guard import source_matches

        assert source_matches("MONEY-CRITICAL", "catalyst/risk"), \
            "the haystack must be real, or this proves nothing"
