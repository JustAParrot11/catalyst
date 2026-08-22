"""Stage-8 escalation closures: the owner's budget really governs spend
(E1) and the installed service really serves the dashboard (E2), plus
the two-way broker/local positions agreement (E3).

Sabotage log (house rule 4):
- governor owner-cap min() inverted to max(): caught by
  test_owner_budget_lowers_the_cap AND
  test_owner_budget_cannot_raise_the_cap. Restored, green.
- ghost-check removed from _broker_positions_agree: caught by
  test_local_position_with_fill_but_no_broker_holding_blocks. Restored,
  green.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst.cost import CostEstimate
from catalyst.cost.governor import BASE_CAP_CENTS, authorize
from catalyst.execution.broker import Broker

#: THE CURRENT MONTH, ALWAYS - house rule 6.
#:
#: This was `datetime(2026, 8, 10, ...)`, a hardcoded August date, while
#: the cost governor computes month-to-date against the REAL clock. In
#: August 2026 the two agreed. From 1 September 2026 they never agree
#: again: seeded spend lands outside the month being measured, so
#: month-to-date reads 0 and every budget-denial test fails - not on the
#: 1st, but on every day thereafter, permanently.
#:
#: upgrade.sh runs this suite before it will install anything, so that
#: would have blocked every upgrade from September onward. Found by
#: moving the system clock forward, which is the only thing that finds
#: it: the suite is green today and would have stayed green until the
#: month turned.
#:
#: Same day-of-month and time as before so nothing else shifts, clamped
#: so it is never in the future on the 1st through the 9th.
_REAL_NOW = datetime.now(timezone.utc)
NOW = _REAL_NOW.replace(day=min(10, _REAL_NOW.day), hour=14, minute=0,
                        second=0, microsecond=0)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.executescript(open("catalyst/storage/schema.sql").read())
    yield conn
    conn.close()


def est(cents="8"):
    return CostEstimate(estimated_cents=Decimal(cents), basis="t",
                        kind="scheduled", component="research")


class TestOwnerBudget:
    def test_owner_budget_lowers_the_cap(self, db):
        """The setup page says 'the bot will not go past it' - an owner
        who types 1 gets a $1 cap, not the $5 base (stress E1)."""
        d = authorize(est("8"), db, Decimal("0.10"),
                      owner_monthly_cap_cents=Decimal("100"))
        assert d.cap_cents == Decimal("100")
        assert d.authorized is True     # 8 cents fits in $1
        # fill the month to the owner's cap: refused, names the cap
        db.execute("INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
                   ("e1", "{}", "claude-sonnet-5", "scheduled", "research",
                    "95", NOW.isoformat(), None))
        db.commit()
        d2 = authorize(est("8"), db, Decimal("0.10"),
                       owner_monthly_cap_cents=Decimal("100"))
        assert d2.authorized is False
        assert "owner_set" in d2.reason

    def test_the_owners_figure_has_no_ceiling_of_its_own(self, db):
        """CONTRACT CHANGED TWICE, both at the owner's explicit request.
        First the field could only lower the cap; then it could raise it
        up to a fixed $25; now there is no fixed ceiling at all, because
        a hard-coded number cannot make "how much of my own money do I
        spend" into a safety question - it can only go stale as the
        account grows.

        The guard against a slipped keyboard moved to where the number
        is ENTERED (setup/first_run.py), which is the only place that
        can tell a deliberate 100 from a mistyped one."""
        d = authorize(est("8"), db, Decimal("0.10"),
                      owner_monthly_cap_cents=Decimal("10000"))
        assert d.cap_cents == Decimal("10000"), (
            "the owner's deliberate figure is the budget, whatever it is")

    def test_a_negative_figure_reads_as_stop_never_as_no_limit(self, db):
        """The one clamp that survives, and the only one that is a
        safety property: below zero must not wrap into unlimited."""
        d = authorize(est("8"), db, Decimal("0.10"),
                      owner_monthly_cap_cents=Decimal("-500"))
        assert d.cap_cents == Decimal("0")
        assert d.authorized is False

    def test_the_anti_ratchet_is_untouched_by_any_of_this(self, db):
        """What the SYSTEM may hand itself out of its own profit still
        stops dead at the hard bound. That one never moves."""
        from catalyst.cost.governor import GOVERNOR_MAX_CAP_CENTS
        db.execute("INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
                   ("p9", "live", "10.00", "9000.00", "target_reached",
                    100, 5, 3, (NOW - timedelta(days=40)).isoformat()))
        db.commit()
        d = authorize(est("8"), db, Decimal("0.10"),
                      owner_monthly_cap_cents=None)
        assert d.cap_cents <= GOVERNOR_MAX_CAP_CENTS

    def test_none_means_no_owner_constraint(self, db):
        d = authorize(est("8"), db, Decimal("0.10"),
                      owner_monthly_cap_cents=None)
        assert d.cap_cents == BASE_CAP_CENTS


class TestOwnerBudgetIsReadSafely:
    """The setup page validates the budget on the way in, but the
    credentials file outlives any one version of that page. An
    unparseable value must fall back to the base cap, not take the
    trading cycle down and not read as 'no limit'."""

    def test_a_plain_number_becomes_cents(self):
        from catalyst.orchestrator.scheduler import _owner_cap_cents
        assert _owner_cap_cents("5") == Decimal("500")
        assert _owner_cap_cents(12.5) == Decimal("1250")

    def test_absent_means_the_base_cap(self):
        from catalyst.orchestrator.scheduler import _owner_cap_cents
        assert _owner_cap_cents(None) is None

    @pytest.mark.parametrize("junk", ["", "five dollars", "nan", "inf",
                                      "-3", [], {"a": 1}])
    def test_junk_falls_back_to_the_base_cap_rather_than_raising(self, junk):
        from catalyst.orchestrator.scheduler import _owner_cap_cents
        assert _owner_cap_cents(junk) is None, (
            f"{junk!r} must not become a spending limit")


class TestDashboardServesSetup:
    def _server_bits(self, tmp_path):
        from catalyst.dashboard.server import make_server
        from catalyst.setup.first_run import SetupApp
        from catalyst.storage import init_db

        dbf = str(tmp_path / "d.db")
        init_db(dbf).close()
        app = SetupApp(path_prefix="/setup",
                       credentials_path=str(tmp_path / "creds.json"),
                       alpaca_tester=lambda k, s, **kw: (True, "ok"),
                       anthropic_tester=lambda k: (True, "ok"),
                       require_token=False)
        server = make_server("127.0.0.1", 0, dbf, setup_app=app)
        return server

    def test_one_port_serves_dashboard_and_setup(self, tmp_path):
        """Stress E2: the installed service 404'd every dashboard route.
        One handler must answer /health, /funnel AND /setup. Driven
        socket-free (conftest blocks all sockets, loopback included)."""
        import io
        from email.message import Message

        server = self._server_bits(tmp_path)
        handler_cls = server.RequestHandlerClass
        server.server_close()

        def get(path):
            h = handler_cls.__new__(handler_cls)
            h.path = path
            h.request_version = "HTTP/1.1"
            h.requestline = f"GET {path} HTTP/1.1"
            h.client_address = ("test", 0)
            h.command = "GET"
            h.headers = Message()
            h.rfile = io.BytesIO(b"")
            h.wfile = io.BytesIO()
            h.do_GET()
            raw = h.wfile.getvalue()
            head, _, body = raw.partition(b"\r\n\r\n")
            status = int(head.split(b" ", 2)[1])
            return status, body.decode("utf-8", "replace"), head.decode()

        assert get("/health")[0] == 200
        assert get("/funnel")[0] == 200
        status, body, _ = get("/setup")
        assert status == 200 and 'name="account_mode"' in body
        # unconfigured "/" redirects the owner to the form
        status, _, head = get("/?code=abc")
        assert status == 302 and "Location: /setup?code=abc" in head


class TestPositionsAgreementBothWays:
    def test_local_position_with_fill_but_no_broker_holding_blocks(self, db):
        """Stress E3: phantom local exposure (recorded entry fill, broker
        flat) must block entries and be reported."""
        from catalyst.orchestrator.cycle import (
            CycleReport, _broker_positions_agree,
        )
        from catalyst.risk import KillSwitchState

        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ord-buy", "cand-1", "b1", "buy", "2", "market", "day",
                    "2026-08-01T14:00:00+00:00", "filled", "{}"))
        db.execute("INSERT INTO fills VALUES (?,?,?,?,?,NULL)",
                   ("ord-buy", "50.00", "2", "2026-08-01T14:00:00+00:00",
                    "50.00"))
        db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                   ("pos-1", "GHOST", json.dumps(["ord-buy"]), None,
                    "2026-08-01T14:00:00+00:00", "2026-08-20", "open"))
        db.commit()

        b = Broker("k", "s", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[])), backoff_s=0)
        report = CycleReport("c", NOW, KillSwitchState(False, None))
        assert _broker_positions_agree(b, db, report) is False
        assert any("phantom exposure" in e for e in report.errors)

    def test_unfilled_entry_does_not_false_trip(self, db):
        """A just-placed, not-yet-filled entry is legitimately
        local-open/broker-flat."""
        from catalyst.orchestrator.cycle import (
            CycleReport, _broker_positions_agree,
        )
        from catalyst.risk import KillSwitchState

        db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ord-buy", "cand-1", "b1", "buy", "2", "market", "day",
                    "2026-08-01T14:00:00+00:00", "accepted", "{}"))
        db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                   ("pos-1", "FRESH", json.dumps(["ord-buy"]), None,
                    "2026-08-01T14:00:00+00:00", "2026-08-20", "open"))
        db.commit()
        b = Broker("k", "s", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[])), backoff_s=0)
        report = CycleReport("c", NOW, KillSwitchState(False, None))
        assert _broker_positions_agree(b, db, report) is True
