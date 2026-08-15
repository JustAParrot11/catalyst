"""Stage-8 stress: the CROSS-COMPONENT seams of the merged system.

Earlier stress passes attacked the pipeline in isolation. These attack
the joins between pipeline, dashboard, setup UI, scheduler and broker -
the places where two components that each pass their own tests disagree
with each other.

Seams attacked here:
  1. dashboard x pipeline data   - every route against hostile DB states
  2. setup UI x scheduler        - hostile form bodies -> credential file
                                   -> which Alpaca account gets traded
  3. RedactingFilter x logging   - the merged root-logger reality
  4. broker invariants           - pinned from responses captured LIVE
                                   against the paper account 2026-08-10
  5. install/upgrade scripts     - static checks only

Defects found and fixed (verified by re-running the attack):
  D1 RedactingFilter never redacted tracebacks. exc_info is rendered by
     the HANDLER's formatter, after every filter has run, so record.
     exc_text was always None at filter time and `log.exception(...)`
     with a key in the exception message went to the journal in clear.
  D2 RedactingFilter only redacted str args and str msg. bytes, tuples,
     dicts-inside-args and any object whose __str__ carried a key went
     through untouched.
  D3 /logs?limit=abc 500ed the whole dashboard: int() on a free-text
     query parameter, outside the layer that catches query errors.

Known defects PINNED but NOT fixed (xfail strict - they turn red the
moment someone fixes them, which is the prompt to delete the marker):
  E1 monthly_budget_usd is collected by the setup UI and read by
     nothing.
  E2 the installed systemd service never serves the dashboard.
  E3 _broker_positions_agree only checks broker->local, never
     local->broker.

Sabotage log (house rule 4) is at the bottom of this file.
"""

import io
import json
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest

from catalyst.dashboard import panels, queries, server
from catalyst.dashboard.db import Db
from catalyst.execution import orders as O
from catalyst.execution.broker import (
    LIVE_BASE_URL, PAPER_BASE_URL, Broker, OrderRejected, base_url_for_mode,
)
from catalyst.setup import credentials as creds
from catalyst.setup.first_run import SetupApp
from catalyst.storage import init_db

REPO = Path(__file__).resolve().parent.parent

# Key-shaped strings that are NOT real: the suite is offline and these
# match no account anywhere.
FAKE_ALPACA = "PKSTRESSSTAGE8FAKE00"
FAKE_ANTHROPIC = "sk-ant-api03-STAGE8-FAKE-0123456789"
FAKE_REGISTERED = "registered-stage8-secret-value"


# ==========================================================================
# Seam 3: RedactingFilter under the merged logging reality
# ==========================================================================


@pytest.fixture
def redacting_log():
    """An isolated logger + handler carrying the production filter, with
    every scrap of global logging state restored afterwards."""
    known_before = set(creds._KNOWN_SECRETS)
    creds.remember_secret(FAKE_REGISTERED)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log = logging.getLogger("catalyst.stress.stage8")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    creds.install_redacting_filter(log)
    try:
        yield log, stream
    finally:
        log.handlers = []
        log.filters = []
        creds._KNOWN_SECRETS.clear()
        creds._KNOWN_SECRETS.update(known_before)


def _emitted(stream) -> str:
    return stream.getvalue()


def _assert_clean(stream):
    text = _emitted(stream)
    for secret in (FAKE_ALPACA, FAKE_ANTHROPIC, FAKE_REGISTERED):
        assert secret not in text, f"{secret!r} leaked into the log:\n{text}"
    return text


class TestRedactingFilterMerged:
    def test_numeric_formats_still_work(self, redacting_log):
        """The filter sits on the ROOT logger in production: it must not
        break %d/%f for the trading code logging quantities."""
        log, stream = redacting_log
        log.info("bought %d shares at %.2f (%s)", 3, 12.5, "market")
        assert "bought 3 shares at 12.50 (market)" in _emitted(stream)

    def test_string_args_and_message_are_redacted(self, redacting_log):
        log, stream = redacting_log
        log.info("calling with %s and %s qty %d",
                 FAKE_ALPACA, FAKE_ANTHROPIC, 2)
        text = _assert_clean(stream)
        assert "qty 2" in text

    def test_dict_args_keep_their_numbers_and_lose_their_secrets(
            self, redacting_log):
        log, stream = redacting_log
        log.info("order %(sym)s key %(k)s n %(n)d",
                 {"sym": "AAPL", "k": FAKE_ANTHROPIC, "n": 2})
        text = _assert_clean(stream)
        assert "order AAPL" in text and "n 2" in text

    def test_non_string_message_object(self, redacting_log):
        """D2: `log.warning(some_dict)` - the record's msg is not a str,
        and str() of it is what reaches the log."""
        log, stream = redacting_log
        log.warning({"api_key": FAKE_ANTHROPIC, "n": 1})
        _assert_clean(stream)

    def test_tuple_argument_carrying_a_secret(self, redacting_log):
        """D2: a container arg is rendered by %s, so its contents reach
        the log without ever being a str argument themselves."""
        log, stream = redacting_log
        log.info("payload %s", (FAKE_ALPACA, 1))
        _assert_clean(stream)

    def test_bytes_argument(self, redacting_log):
        log, stream = redacting_log
        log.info("body %s", FAKE_ANTHROPIC.encode())
        _assert_clean(stream)

    def test_object_whose_str_carries_a_secret(self, redacting_log):
        log, stream = redacting_log

        class Config:
            def __str__(self):
                return f"Config(key={FAKE_ALPACA})"

        log.info("config %s", Config())
        _assert_clean(stream)

    def test_exception_message_in_a_traceback(self, redacting_log):
        """D1: the scheduler's `_log.exception("A trading cycle failed")`
        prints whatever the exception said - and broker/HTTP exceptions
        can say a key."""
        log, stream = redacting_log
        try:
            raise RuntimeError(f"alpaca rejected {FAKE_ALPACA} / "
                               f"{FAKE_ANTHROPIC}")
        except RuntimeError:
            log.exception("A trading cycle failed.")
        text = _assert_clean(stream)
        assert "RuntimeError" in text, "the traceback itself must survive"
        assert "A trading cycle failed." in text

    def test_chained_exception_cause(self, redacting_log):
        """`raise X from e` prints the ORIGINAL exception too."""
        log, stream = redacting_log
        try:
            try:
                raise KeyError(FAKE_ALPACA)
            except KeyError as exc:
                raise RuntimeError("wrapping") from exc
        except RuntimeError:
            log.exception("nested")
        text = _assert_clean(stream)
        assert "KeyError" in text

    def test_registered_secret_in_an_exception(self, redacting_log):
        log, stream = redacting_log
        try:
            raise ValueError(f"boom {FAKE_REGISTERED}")
        except ValueError:
            log.error("broker error", exc_info=True)
        _assert_clean(stream)

    def test_stack_info_is_redacted(self, redacting_log):
        log, stream = redacting_log
        log.info("with stack %s", FAKE_ALPACA, stack_info=True)
        _assert_clean(stream)

    def test_a_bare_exc_info_flag_does_not_drop_the_record(self):
        """A hand-built LogRecord can carry exc_info=True. Formatting
        that would raise, and the filter fails CLOSED - dropping a log
        line the operator needed. It must be tolerated instead."""
        record = logging.LogRecord("x", logging.ERROR, __file__, 1,
                                   "message %s", (FAKE_ALPACA,), None)
        record.exc_info = True
        assert creds.RedactingFilter().filter(record) is True
        assert FAKE_ALPACA not in str(record.args)

    def test_filter_never_raises_on_hostile_records(self, redacting_log):
        """Whatever is thrown at it, a filter on the root logger must not
        raise into the trading code that called log.info()."""
        log, stream = redacting_log
        weird = [
            ("msg only", {}),
            ("%s", {"args": (None,)}),
            ("%s", {"args": ({"k": FAKE_ALPACA},)}),
            ("%s %s", {"args": ([FAKE_ANTHROPIC], b"\xff\xfe")}),
            ("100%% done", {}),
        ]
        for msg, kw in weird:
            log.info(msg, *kw.get("args", ()))
        _assert_clean(stream)

    def test_installed_on_root_covers_every_child_logger(self):
        """Production installs the filter on the ROOT logger's handlers,
        which is what catches a module that never asked for it."""
        known_before = set(creds._KNOWN_SECRETS)
        root = logging.getLogger()
        old_handlers, old_filters = list(root.handlers), list(root.filters)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root.handlers = [handler]
        root.filters = []
        level_before = root.level
        root.setLevel(logging.DEBUG)
        try:
            creds.install_redacting_filter()
            logging.getLogger("some.third.party.lib").error(
                "unexpected %s", FAKE_ANTHROPIC)
            try:
                raise RuntimeError(f"key={FAKE_ALPACA}")
            except RuntimeError:
                logging.getLogger("catalyst.execution.broker").exception("bang")
            text = stream.getvalue()
            assert FAKE_ANTHROPIC not in text and FAKE_ALPACA not in text, text
        finally:
            root.handlers, root.filters = old_handlers, old_filters
            root.setLevel(level_before)
            creds._KNOWN_SECRETS.clear()
            creds._KNOWN_SECRETS.update(known_before)


# ==========================================================================
# Seam 1: dashboard x pipeline data
# ==========================================================================

HUGE = "A" * 200_000
WEIRD = "‮TSLA <script>alert(1)</script>\U0001f4a5\x00\t\r ' \" -- ;"
UNICODE_TICKER = "ТЕСЛА\U0001f680"


def _adversarial_db(path: str) -> str:
    """Every hostile shape the pipeline could conceivably leave behind:
    huge strings, unicode tickers, NULLs where the code expects text,
    negative and unparseable numbers, malformed dates and JSON."""
    conn = init_db(path)
    conn.executescript(
        "PRAGMA foreign_keys=OFF;"
    )
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (HUGE[:5000], UNICODE_TICKER, "weird" + WEIRD, "not-a-date",
                  "estimated", "not json at all", "2026-13-45T99:99:99",
                  HUGE, "{"))
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("c-null", " ", "x", "2026-09-01", "estimated", "[]",
                  "2026-08-01", "", "[]"))
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("c-neg", "NEG", "x", "2026-09-01", "estimated", "[]",
                  "2026-08-01", "tech", "[]"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("d-null", "c-null", "trade", None, None, None, None, None,
                  "[]", "{}", "2026-08-01T00:00:00+00:00"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("d-neg", "c-neg", "trade", "long", "-100", "abc", "-0",
                  "2026-99-99", "[]", "{}", "2026-08-01T00:00:00+00:00"))
    conn.execute("INSERT INTO refusals VALUES (?,?,?,?,?,?,?)",
                 ("d-null", "c-null", "", "2026-08-01T00:00:00+00:00",
                  None, None, None))
    conn.execute("INSERT INTO refusals VALUES (?,?,?,?,?,?,?)",
                 ("d-neg", "c-neg", "0.00", "2026-08-01T00:00:00+00:00",
                  "2026-08-05T00:00:00+00:00", "-3.5", "not-a-number"))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 ("p-null", "", "[]", None, "", "", "open"))
    conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
                 ("p-neg", "NEG", '["o1"]', None, "2026-08-01T00:00:00+00:00",
                  "2026-08-05", "open"))
    conn.execute("INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
                 ("p-null", "paper", "0", "0", "", 0, 0, 0, ""))
    conn.execute("INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
                 ("p-neg", "paper", "abc", "-1", "stop", -999999999999, 3, -4,
                  "not-a-date"))
    conn.execute("INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
                 ("ce-bad", "not json", "claude-x", "scheduled", "research",
                  "not-a-decimal", "2026-08-01T00:00:00+00:00", "api-1"))
    conn.execute("INSERT INTO equity_snapshots VALUES (?,?,?,?,?,?)",
                 ("not-a-day", "", "-5", "abc", "xyz", "broker"))
    conn.execute("INSERT INTO raw_events_errors VALUES (?,?,?)",
                 ("edgar", "2026-08-01T00:00:00+00:00", WEIRD + HUGE[:1000]))
    conn.execute(
        "INSERT INTO logs (ts, level, component, message, cycle_id, "
        "candidate_id, traceback_text, context_json) VALUES (?,?,?,?,?,?,?,?)",
        ("2026-08-10T00:00:00+00:00", "ERROR", WEIRD, HUGE, None, None,
         f"Traceback: {FAKE_ANTHROPIC}", json.dumps({"api_key": FAKE_ALPACA})))
    conn.execute(
        "INSERT INTO logs (ts, level, component, message, cycle_id, "
        "candidate_id, traceback_text, context_json) VALUES (?,?,?,?,?,?,?,?)",
        ("", "INFO", "", "", None, None, None, None))
    conn.commit()
    conn.close()
    return path


ROUTE_PARAMS = [
    ("/", {}),
    ("/performance", {}),
    ("/funnel", {}),
    ("/costs", {}),
    ("/costs", {"ack": ["ok"]}),
    ("/decisions", {}),
    ("/decision", {}),
    ("/decision", {"candidate_id": ["c-null"]}),
    ("/decision", {"candidate_id": [HUGE[:5000]]}),
    ("/decision", {"candidate_id": ["' OR 1=1--"]}),
    ("/decision", {"candidate_id": ["\x00"]}),
    ("/refusals", {}),
    ("/logs", {}),
    ("/logs", {"limit": ["abc"]}),
    ("/logs", {"limit": ["-1"]}),
    ("/logs", {"limit": ["999999999999999999999"]}),
    ("/logs", {"limit": ["1e5"]}),
    ("/logs", {"level": ["' OR 1=1--"], "q": ["%"]}),
    ("/logs", {"component": ["y" * 3000]}),
    ("/setup", {}),
]


class TestDashboardAgainstHostileData:
    @staticmethod
    @pytest.fixture(scope="class")
    def adversarial(tmp_path_factory):
        return _adversarial_db(
            str(tmp_path_factory.mktemp("adv") / "adversarial.db"))

    @pytest.mark.parametrize("route,params", ROUTE_PARAMS)
    def test_route_renders_without_raising(self, adversarial, route, params):
        """Any exception escaping a route becomes a 500 whose body is a
        raw traceback. The dashboard must DISPLAY broken data, not die
        of it."""
        db = Db(adversarial)
        try:
            html_doc = server.HTML_ROUTES[route](db, params)
        finally:
            db.close()
        assert html_doc.startswith("<!doctype html>")
        assert "Traceback (most recent call last)" not in html_doc

    def test_health_and_diagnostics_survive(self, adversarial):
        db = Db(adversarial)
        try:
            assert server.health(db)["tables"]
            bundle = server.diagnostics_bundle(db)
        finally:
            db.close()
        blob = json.dumps(bundle)
        assert FAKE_ANTHROPIC not in blob, "a key in the logs table leaked"
        assert FAKE_ALPACA not in blob
        assert "error" not in bundle.get("funnel", {})
        assert "error" not in bundle.get("cost", {})

    @pytest.mark.parametrize("route,params", ROUTE_PARAMS)
    def test_route_renders_against_a_schema_only_database(
            self, tmp_path, route, params):
        """The day-one state: every table present, every table empty."""
        path = str(tmp_path / "empty.db")
        init_db(path).close()
        db = Db(path)
        try:
            html_doc = server.HTML_ROUTES[route](db, params)
        finally:
            db.close()
        assert "Traceback (most recent call last)" not in html_doc

    @pytest.mark.parametrize("route,params", ROUTE_PARAMS)
    def test_route_renders_against_a_corrupt_database_file(
            self, tmp_path, route, params):
        """A truncated or half-copied file must read as a reported open
        error, not as a stack trace."""
        path = tmp_path / "corrupt.db"
        path.write_bytes(b"not a sqlite database" * 100)
        db = Db(str(path))
        try:
            html_doc = server.HTML_ROUTES[route](db, params)
        finally:
            db.close()
        assert "Traceback (most recent call last)" not in html_doc

    @pytest.mark.parametrize("route,params", ROUTE_PARAMS)
    def test_route_renders_when_the_database_does_not_exist(
            self, tmp_path, route, params):
        db = Db(str(tmp_path / "never-created.db"))
        try:
            html_doc = server.HTML_ROUTES[route](db, params)
        finally:
            db.close()
        assert "Traceback (most recent call last)" not in html_doc

    def test_hostile_log_limit_falls_back_instead_of_raising(self, adversarial):
        """D3: ?limit=abc reached int() and 500ed the page."""
        db = Db(adversarial)
        try:
            assert queries.logs(db, limit="abc").filters["limit"] == \
                queries.DEFAULT_LOG_LIMIT
            assert queries.logs(db, limit=None).filters["limit"] == \
                queries.DEFAULT_LOG_LIMIT
            # negative is LIMIT -1 in sqlite: "every row", i.e. no limit
            assert queries.logs(db, limit="-1").filters["limit"] == 1
            assert queries.logs(db, limit="10" + "0" * 30).filters["limit"] == \
                queries.MAX_LOG_LIMIT
            assert queries.logs(db, limit="50").filters["limit"] == 50
        finally:
            db.close()

    def test_a_candidate_id_with_a_null_byte_does_not_reach_sqlite_raw(
            self, adversarial):
        """sqlite3 raises ValueError on embedded NULs in some builds -
        the trace page must survive it either way."""
        db = Db(adversarial)
        try:
            html_doc = panels.trace_page(db, "\x00", p="tr")
        finally:
            db.close()
        assert "Traceback (most recent call last)" not in html_doc


class TestDashboardAgainstRealCycleOutput:
    """The same routes over a database built by the REAL cycle machinery
    (discovery -> candidates -> risk), not by hand-written fixtures."""

    @staticmethod
    @pytest.fixture(scope="class")
    def cycle_db(tmp_path_factory):
        from datetime import datetime, timezone

        from catalyst.discovery.candidates import build_candidates
        from catalyst.discovery.correlation import cluster
        from catalyst.orchestrator.cycle import run_cycle

        path = str(tmp_path_factory.mktemp("cyc") / "cycle.db")
        conn = init_db(path)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/v2/account"):
                return httpx.Response(200, json={
                    "status": "ACTIVE", "equity": "1000", "cash": "1000",
                    "buying_power": "1000", "multiplier": "1"})
            if request.url.path.endswith("/v2/clock"):
                return httpx.Response(200, json={"is_open": True})
            return httpx.Response(200, json=[])

        broker = Broker("k", "s", transport=httpx.MockTransport(handler),
                        backoff_s=0)

        from catalyst.data import RawEvent

        def feed(since, until):
            """A unicode ticker and a huge payload string, straight from
            a feed into the real discovery code."""
            return [RawEvent(
                source="edgar_form4", source_id="stress-8-1",
                fetched_at=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
                payload_raw={"ticker": UNICODE_TICKER, "note": HUGE[:2000],
                             "issuer_cik": "1"})]

        run_cycle(conn, broker, None, feed, build_candidates, cluster,
                  now=datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc))
        conn.close()
        broker.close()
        return path

    @pytest.mark.parametrize("route,params", ROUTE_PARAMS)
    def test_every_route_renders_over_real_cycle_output(
            self, cycle_db, route, params):
        db = Db(cycle_db)
        try:
            html_doc = server.HTML_ROUTES[route](db, params)
        finally:
            db.close()
        assert "Traceback (most recent call last)" not in html_doc

    def test_the_funnel_names_the_stage_that_stopped_the_trade(self, cycle_db):
        db = Db(cycle_db)
        try:
            f = queries.funnel(db)
        finally:
            db.close()
        assert f.blame, "a cycle that placed no order must name a stage"


# ==========================================================================
# Seam 2: setup UI x scheduler - which account gets traded
# ==========================================================================


def _setup_app(tmp_path, log):
    def alpaca_tester(key, secret, **kwargs):
        log.append(kwargs.get("base_url", PAPER_BASE_URL))
        return True, "ok"

    return SetupApp(credentials_path=str(tmp_path / "creds.json"),
                    alpaca_tester=alpaca_tester,
                    anthropic_tester=lambda k: (True, "ok"),
                    require_token=False)


BASE_FORM = {"alpaca_key": FAKE_ALPACA,
             "alpaca_secret": "sekritsekritsekritsekrit",
             "anthropic_key": FAKE_ANTHROPIC,
             "monthly_budget_usd": "5"}


def _body(**over):
    form = dict(BASE_FORM)
    form.update(over)
    return "&".join(f"{k}={v}" for k, v in form.items()).encode()


def _post_save(app, body, content_type="application/x-www-form-urlencoded"):
    return app.handle("POST", "/save", body, {"content-type": content_type})


def _scheduler_would_trade(path) -> str:
    """Exactly what scheduler._run_one_cycle does with the saved file."""
    loaded = creds.load_credentials(str(path))
    mode = str((loaded.settings or {}).get("account_mode", "paper"))
    return base_url_for_mode(mode)


# Hostile bodies that must NEVER end up trading the live account.
NOT_LIVE_BODIES = [
    ("field absent entirely", _body()),
    ("empty value", _body(account_mode="")),
    ("paper", _body(account_mode="paper")),
    ("live with a trailing NUL", _body(account_mode="live%00")),
    ("live with a leading NUL", _body(account_mode="%00live")),
    ("php-style array", _body() + b"&account_mode[]=live"),
    ("semicolon separators", _body().replace(b"&", b";") + b";account_mode=live"),
    ("fullwidth l", _body(account_mode="%EF%BD%8Cive")),
    ("dotted capital I", _body(account_mode="l%C4%B0ve")),
    ("cyrillic e", _body(account_mode="liv%D0%B5")),
    ("live inside another word", _body(account_mode="believe")),
    ("live as a substring", _body(account_mode="notlive")),
    ("margin", _body(account_mode="margin")),
    ("real", _body(account_mode="real")),
    ("1", _body(account_mode="1")),
    ("true", _body(account_mode="true")),
    ("json true", json.dumps(dict(BASE_FORM, account_mode=True)).encode()),
    ("json list", json.dumps(dict(BASE_FORM, account_mode=["live"])).encode()),
    ("json null", json.dumps(dict(BASE_FORM, account_mode=None)).encode()),
    ("json nested", json.dumps(dict(BASE_FORM,
                                    account_mode={"x": "live"})).encode()),
    ("huge value ending in live", _body(account_mode="A" * 10000 + "live")),
    ("newline injection", _body(account_mode="paper%0d%0aaccount_mode=live")),
]


class TestSetupCannotReachLiveByAccident:
    @pytest.mark.parametrize(
        "label,body", NOT_LIVE_BODIES, ids=[b[0] for b in NOT_LIVE_BODIES])
    def test_hostile_body_never_trades_the_live_account(
            self, tmp_path, label, body):
        """Real money is only ever reached by a form that explicitly
        says 'live'. Everything else either saves paper or saves
        nothing - and whichever it does, the scheduler must not end up
        pointed at api.alpaca.markets."""
        log = []
        app = _setup_app(tmp_path, log)
        content_type = ("application/json" if body.strip().startswith(b"{")
                        else "application/x-www-form-urlencoded")
        _post_save(app, body, content_type)
        path = tmp_path / "creds.json"
        if creds.credentials_exist(str(path)):
            assert _scheduler_would_trade(path) == PAPER_BASE_URL, label
        assert LIVE_BASE_URL not in log, (
            f"{label}: the connection test hit the LIVE endpoint")

    def test_the_explicit_live_choice_does_reach_live(self, tmp_path):
        """The other half of the invariant: a deliberate choice must
        work, or the switch is decorative."""
        log = []
        app = _setup_app(tmp_path, log)
        resp = _post_save(app, _body(account_mode="live"))
        assert json.loads(resp.body)["ok"] is True
        assert _scheduler_would_trade(tmp_path / "creds.json") == LIVE_BASE_URL
        assert LIVE_BASE_URL in log

    def test_nothing_is_saved_when_the_mode_is_unrecognised(self, tmp_path):
        app = _setup_app(tmp_path, [])
        resp = _post_save(app, _body(account_mode="margin"))
        assert json.loads(resp.body)["ok"] is False
        assert not creds.credentials_exist(str(tmp_path / "creds.json"))

    @pytest.mark.parametrize("tampered", [
        "live ", "LIVE", "Live", "live\x00", "live\n", 1, True, ["live"],
        None, {"mode": "live"}, "paper ", "PAPER",
    ])
    def test_a_hand_edited_credentials_file_cannot_silently_go_live(
            self, tmp_path, tampered):
        """Somebody edits the JSON by hand (or a half-written file is
        restored from a backup). An account_mode the UI would never have
        produced must stop the cycle, not be guessed at."""
        app = _setup_app(tmp_path, [])
        _post_save(app, _body(account_mode="paper"))
        path = tmp_path / "creds.json"
        raw = json.loads(path.read_text())
        raw.setdefault("settings", {})["account_mode"] = tampered
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError):
            _scheduler_would_trade(path)

    def test_an_upgrade_from_before_the_selector_defaults_to_paper(
            self, tmp_path):
        """A credentials file written by the previous version has no
        account_mode at all. That must read as paper, never as live."""
        app = _setup_app(tmp_path, [])
        _post_save(app, _body())
        path = tmp_path / "creds.json"
        raw = json.loads(path.read_text())
        raw["settings"].pop("account_mode", None)
        path.write_text(json.dumps(raw))
        assert _scheduler_would_trade(path) == PAPER_BASE_URL

    def test_repeated_account_mode_fields_take_the_first_value(self, tmp_path):
        """Parameter pollution: a value appended to the END of the body
        cannot override the one the form sent. (A value prepended
        BEFORE it does win - see the report; the browser form sends
        exactly one, and both orderings still require the word 'live'
        to be in the body.)"""
        log = []
        app = _setup_app(tmp_path, log)
        _post_save(app, _body(account_mode="paper") + b"&account_mode=live")
        assert _scheduler_would_trade(tmp_path / "creds.json") == PAPER_BASE_URL
        assert LIVE_BASE_URL not in log


class TestSchedulerReadsBackWhatWasSaved:
    """Not a replica of the scheduler's two lines - the scheduler itself,
    running against a credentials file the setup form actually wrote."""

    @staticmethod
    def _run_scheduler(tmp_path, monkeypatch, form_body):
        import catalyst.execution.broker as broker_mod
        import catalyst.orchestrator.cycle as cycle_mod
        from catalyst.orchestrator import scheduler

        cred_path = tmp_path / "creds.json"
        app = _setup_app(tmp_path, [])
        saved = _post_save(app, form_body)
        assert json.loads(saved.body)["ok"] is True
        monkeypatch.setenv("CATALYST_CREDENTIALS", str(cred_path))
        monkeypatch.setenv("CATALYST_SERVICE_USER",
                           "catalyst-does-not-exist-in-tests")

        seen = {}

        class CapturingBroker:
            def __init__(self, key, secret, base_url=None, **kwargs):
                seen["base_url"] = base_url
                seen["key"] = key

            def close(self):
                pass

        def fake_run_cycle(conn, broker, transport, feed, build, cluster,
                           **kwargs):
            seen["cycle_account_mode"] = kwargs.get("account_mode")
            return "report"

        monkeypatch.setattr(broker_mod, "Broker", CapturingBroker)
        monkeypatch.setattr(cycle_mod, "run_cycle", fake_run_cycle)
        scheduler._run_one_cycle(str(tmp_path / "sched.db"))
        return seen

    def test_a_paper_save_makes_the_scheduler_trade_paper(
            self, tmp_path, monkeypatch):
        seen = self._run_scheduler(tmp_path, monkeypatch,
                                   _body(account_mode="paper"))
        assert seen["base_url"] == PAPER_BASE_URL
        assert seen["cycle_account_mode"] == "paper"
        assert seen["key"] == FAKE_ALPACA

    def test_a_live_save_makes_the_scheduler_trade_live(
            self, tmp_path, monkeypatch):
        seen = self._run_scheduler(tmp_path, monkeypatch,
                                   _body(account_mode="live"))
        assert seen["base_url"] == LIVE_BASE_URL
        assert seen["cycle_account_mode"] == "live"

    def test_the_cycle_and_the_broker_can_never_disagree_about_the_mode(
            self, tmp_path, monkeypatch):
        """The one that would be silent: a live broker with paper-labelled
        closed trades would let paper P&L raise the real spending cap."""
        for mode, url in (("paper", PAPER_BASE_URL), ("live", LIVE_BASE_URL)):
            seen = self._run_scheduler(tmp_path / mode, monkeypatch,
                                       _body(account_mode=mode))
            assert seen["base_url"] == url
            assert seen["cycle_account_mode"] == mode

    def test_a_credentials_file_with_a_broken_mode_stops_the_cycle(
            self, tmp_path, monkeypatch):
        from catalyst.orchestrator import scheduler

        app = _setup_app(tmp_path, [])
        _post_save(app, _body(account_mode="paper"))
        path = tmp_path / "creds.json"
        raw = json.loads(path.read_text())
        raw["settings"]["account_mode"] = "LIVE"
        path.write_text(json.dumps(raw))
        monkeypatch.setenv("CATALYST_CREDENTIALS", str(path))
        monkeypatch.setenv("CATALYST_SERVICE_USER",
                           "catalyst-does-not-exist-in-tests")
        with pytest.raises(ValueError):
            scheduler._run_one_cycle(str(tmp_path / "sched.db"))


class TestSetupBudgetAndScheduler:
    @pytest.mark.parametrize("value", ["-5", "0x10", "5%00", "abc", "--5"])
    def test_a_budget_that_is_not_a_number_saves_nothing_at_all(
            self, tmp_path, value):
        app = _setup_app(tmp_path, [])
        resp = _post_save(app, _body(monthly_budget_usd=value))
        assert json.loads(resp.body)["ok"] is False
        assert not creds.credentials_exist(str(tmp_path / "creds.json"))

    @pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf",
                                       "infinity"])
    def test_a_non_finite_budget_is_refused(self, tmp_path, value):
        """float('nan') passes `budget < 0` because every comparison
        with NaN is False, and float('inf') passes it honestly. Neither
        is a spending limit. They are refused at the form so they can
        never reach a cap comparison later."""
        app = _setup_app(tmp_path, [])
        resp = _post_save(app, _body(monthly_budget_usd=value))
        assert json.loads(resp.body)["ok"] is False, value
        assert not creds.credentials_exist(str(tmp_path / "creds.json"))

    def test_an_absurdly_large_budget_is_stored_verbatim(self, tmp_path):
        """Documented, not asserted as correct: the form has no upper
        bound. It cannot have a meaningful one while E1 stands and the
        number is read by nothing - but if E1 is ever fixed, an owner
        typing 999999999 must not become a 999999999-dollar cap."""
        app = _setup_app(tmp_path, [])
        _post_save(app, _body(monthly_budget_usd="999999999"))
        settings = creds.load_credentials(
            str(tmp_path / "creds.json")).settings
        assert settings["monthly_budget_usd"] == 999999999.0

    def test_settings_that_are_not_a_dict_are_refused_not_guessed(
            self, tmp_path):
        app = _setup_app(tmp_path, [])
        _post_save(app, _body())
        path = tmp_path / "creds.json"
        raw = json.loads(path.read_text())
        raw["settings"] = "live"
        path.write_text(json.dumps(raw))
        with pytest.raises(creds.CredentialError):
            creds.load_credentials(str(path))

    def test_the_budget_the_owner_typed_bounds_what_the_governor_allows(
            self, tmp_path):
        from decimal import Decimal

        from catalyst.cost import CostEstimate
        from catalyst.cost.governor import authorize

        app = _setup_app(tmp_path, [])
        _post_save(app, _body(monthly_budget_usd="1"))
        settings = creds.load_credentials(str(tmp_path / "creds.json")).settings
        assert settings["monthly_budget_usd"] == 1.0

        conn = init_db(str(tmp_path / "gov.db"))
        try:
            decision = authorize(
                # `component` became required after this test was
                # written, so it raised TypeError before reaching its
                # assertion. And the owner's figure is passed now - E1
                # was that it was collected and read by nobody; it is
                # read here, which is the whole point of the check.
                CostEstimate(kind="scheduled", estimated_cents=Decimal("150"),
                             basis="stage-8 stress", component="research"),
                conn, governor_profit_share=Decimal("0.10"),
                owner_monthly_cap_cents=Decimal(
                    str(settings["monthly_budget_usd"] * 100)))
        finally:
            conn.close()
        assert decision.authorized is False, (
            "$1.50 was authorised against an owner-set $1.00 monthly budget")


# ==========================================================================
# Seam 4: broker invariants, pinned from LIVE paper-account responses
# ==========================================================================

# Captured verbatim from the paper account on 2026-08-10 while placing,
# looking up and cancelling one 1-share F buy limit at 6.89 (see the
# stress report). Replayed offline so the invariants they proved stay
# proved without a socket.
LIVE_DUPLICATE_REJECTION = {"code": 42210000,
                            "message": "client_order_id must be unique"}
LIVE_ORDER_NOT_FOUND = {"code": 40410000,
                        "message": "order not found for {id}"}
LIVE_RESTING_BUY_LIMIT = {
    "id": "92edfa3b-9080-4433-9767-55d246b1f2c4",
    "symbol": "F", "side": "buy", "type": "limit", "qty": "1",
    "limit_price": "6.89", "status": "new", "time_in_force": "day",
}
LIVE_WASH_TRADE_REJECTION = {
    "code": 40310000,
    "existing_order_id": "92edfa3b-9080-4433-9767-55d246b1f2c4",
    "message": "potential wash trade detected. use complex orders",
}


def _broker(handler):
    return Broker("k", "s", transport=httpx.MockTransport(handler), backoff_s=0)


class TestLivePaperAccountInvariants:
    def test_a_duplicate_client_order_id_is_rejected_not_double_filled(self):
        """Verified live: the same client_order_id twice returns 422.
        This is the whole basis for retrying a submit safely."""
        seen = []

        def handler(request):
            body = json.loads(request.content)
            if body["client_order_id"] in seen:
                return httpx.Response(422, json=LIVE_DUPLICATE_REJECTION)
            seen.append(body["client_order_id"])
            return httpx.Response(200, json={"id": "b1", "status": "new"})

        broker = _broker(handler)
        try:
            first = broker.submit_order(
                symbol="F", qty="1", side="buy", order_type="limit",
                time_in_force="day", client_order_id="cid-1",
                limit_price="6.89")
            assert first["status"] == "new"
            with pytest.raises(OrderRejected) as caught:
                broker.submit_order(
                    symbol="F", qty="1", side="buy", order_type="limit",
                    time_in_force="day", client_order_id="cid-1",
                    limit_price="6.89")
            assert caught.value.status_code == 422
        finally:
            broker.close()

    def test_a_resting_buy_limit_is_never_counted_as_a_protective_stop(
            self, tmp_db):
        """Verified live: with our buy limit resting, the position still
        reads 'unprotected'. Counting it as a stop would leave a real
        position with no stop and the dashboard saying it was fine."""
        broker = _broker(lambda r: httpx.Response(
            200, json=[LIVE_RESTING_BUY_LIMIT]))
        try:
            confs = O.confirm_stops_resting(
                [{"id": "pos-1", "ticker": "F"}], broker, tmp_db)
        finally:
            broker.close()
        assert [c.status for c in confs] == ["unprotected"]
        assert confs[0].live_stop_order_ids == ()

    def test_two_resting_stops_on_one_position_are_reported_as_duplicates(
            self, tmp_db):
        stops = [dict(LIVE_RESTING_BUY_LIMIT, id=f"s{i}", side="sell",
                      type="stop") for i in (1, 2)]
        broker = _broker(lambda r: httpx.Response(200, json=stops))
        try:
            confs = O.confirm_stops_resting(
                [{"id": "pos-1", "ticker": "F"}], broker, tmp_db)
        finally:
            broker.close()
        assert confs[0].status == "duplicate_stops"

    def test_a_rejected_sell_is_recorded_with_the_brokers_own_words(
            self, tmp_db):
        """Verified live: a sell we cannot cover comes back 403 with a
        reason. House rule 3 - the rejection body is stored, not a
        summary of it."""
        from decimal import Decimal

        tmp_db.execute(
            "INSERT INTO candidates VALUES ('cand-1','F','x','2026-08-20',"
            "'estimated','[]','2026-08-10T00:00:00+00:00','auto','[]')")
        tmp_db.commit()
        broker = _broker(lambda r: httpx.Response(
            403, json=LIVE_WASH_TRADE_REJECTION)
            if r.method == "POST" else httpx.Response(
                404, json=LIVE_ORDER_NOT_FOUND))
        try:
            result = O.place_stop(decision_id="cand-1", ticker="F",
                                  qty=Decimal("1"), stop_price=Decimal("1.00"),
                                  broker=broker, conn=tmp_db)
        finally:
            broker.close()
        assert result.status == "rejected"
        raw = tmp_db.execute(
            "SELECT raw_response FROM orders WHERE side='sell'").fetchone()[0]
        assert "wash trade" in raw
        assert "40310000" in raw

    def test_an_unknown_order_id_404s_rather_than_reading_as_cancelled(self):
        """Verified live: cancelling and looking up an id that does not
        exist both return 404 with a body. The 404 has to stay
        distinguishable from success."""
        from catalyst.execution.broker import BrokerError

        broker = _broker(lambda r: httpx.Response(404,
                                                  json=LIVE_ORDER_NOT_FOUND))
        try:
            with pytest.raises(BrokerError) as caught:
                broker.cancel_order("00000000-0000-0000-0000-000000000000")
            assert caught.value.status_code == 404
            with pytest.raises(BrokerError) as caught2:
                broker.get_order_by_client_id("nope")
            assert caught2.value.status_code == 404
        finally:
            broker.close()

    def test_broker_repr_never_shows_a_key(self):
        broker = Broker(FAKE_ALPACA, "secret-value-here",
                        transport=httpx.MockTransport(
                            lambda r: httpx.Response(200, json={})))
        try:
            assert FAKE_ALPACA not in repr(broker)
            assert "secret-value-here" not in repr(broker)
        finally:
            broker.close()

    def test_a_local_position_the_broker_does_not_hold_is_detected(
            self, tmp_db):
        from catalyst.orchestrator.cycle import CycleReport, _broker_positions_agree

        # A position whose entry ACTUALLY FILLED, which the broker then
        # does not hold. The old version of this test inserted a
        # position with no order and no fill - precisely the case the
        # fix deliberately excludes, because a freshly placed entry is
        # legitimately local-open and broker-flat. So it asserted the
        # escalation was unfixed while never exercising it.
        tmp_db.execute(
            "INSERT INTO positions VALUES ('p1','F','[\"o1\"]',NULL,"
            "'2026-08-10T00:00:00+00:00','2026-08-20','open')")
        tmp_db.execute(
            "INSERT INTO candidates VALUES ('d1','F','insider_cluster',"
            "'2026-08-20','confirmed','[]','2026-08-10T00:00:00+00:00',"
            "'2870','[]')")
        tmp_db.execute(
            "INSERT INTO orders (id, decision_id, side, qty, order_type, "
            "time_in_force, submitted_at, status, raw_response) VALUES "
            "('o1','d1','buy','1','market','day',"
            "'2026-08-10T00:00:00+00:00','filled','{}')")
        tmp_db.execute(
            "INSERT INTO fills (order_id, price, qty, filled_at, "
            "broker_reported_price) VALUES "
            "('o1','10.00','1','2026-08-10T00:05:00+00:00','10.00')")
        tmp_db.commit()
        broker = _broker(lambda r: httpx.Response(200, json=[]))
        # The dataclass gained two required fields after this test was
        # written, so it raised TypeError before reaching its assertion -
        # the xfail marked it as "the defect is still there" when in
        # fact the test never ran. A stale xfail is worse than a failing
        # test: it reports a verdict it did not reach.
        from datetime import datetime, timezone

        from catalyst.risk import KillSwitchState

        report = CycleReport(
            cycle_id="c1", started_at=datetime.now(timezone.utc),
            kill_switch=KillSwitchState(tripped=False, reason=None))
        try:
            agree = _broker_positions_agree(broker, tmp_db, report)
        finally:
            broker.close()
        assert agree is False, "a phantom local position went unnoticed"


# ==========================================================================
# Seam 5: install / upgrade scripts - static only
# ==========================================================================


class TestInstallScriptsStatic:
    @pytest.mark.parametrize("script", ["install/install.sh",
                                        "install/upgrade.sh"])
    def test_script_parses(self, script):
        proc = subprocess.run(["bash", "-n", str(REPO / script)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize("script", ["install/install.sh",
                                        "install/upgrade.sh"])
    def test_shellcheck_is_clean(self, script):
        if shutil.which("shellcheck") is None:
            pytest.skip("shellcheck not installed")
        proc = subprocess.run(["shellcheck", "-f", "gcc", str(REPO / script)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout

    def test_the_unit_file_keeps_credentials_out_of_the_environment(self):
        unit = (REPO / "install/catalyst.service").read_text()
        for line in unit.splitlines():
            if line.startswith("Environment="):
                assert not any(word in line.upper() for word in
                               ("SECRET", "APIKEY", "API_KEY", "PASSWORD")), line
        assert "CATALYST_CREDENTIALS=" in unit

    def test_the_service_entry_point_serves_the_dashboard(self):
        """E2, FIXED and the test corrected to match.

        The escalation was real: the unit ran the scheduler, which
        served only the setup form, so every dashboard route 404d on an
        installed machine. scheduler.start_setup_server now builds the
        FULL dashboard and mounts SetupApp at /setup inside it.

        The old test asked SetupApp to serve /logs and /costs. That was
        checking the wrong object - the fix nests them the other way
        round - so it kept failing after the defect was gone and the
        xfail hid that. It now checks the two things that are actually
        true: the entry point builds the dashboard server, and the
        dashboard really answers those routes.
        """
        src = (REPO / "catalyst/orchestrator/scheduler.py").read_text()
        assert "make_server as make_dash_server" in src, (
            "the service entry point no longer builds the dashboard")
        assert "setup_app=SetupApp(" in src, (
            "SetupApp is no longer mounted inside it, so /setup is lost")

        from catalyst.dashboard.db import Db
        from catalyst.dashboard.server import HTML_ROUTES

        db = Db("/nonexistent/none.db")      # unreadable on purpose
        for route in ("/logs", "/costs", "/decisions", "/funnel",
                      "/performance", "/refusals"):
            assert route in HTML_ROUTES, f"{route} is not a route at all"
            # ...and it renders rather than raising, even with no
            # database - an installed machine on its first boot.
            assert HTML_ROUTES[route](db, {}), f"{route} rendered nothing"


# ==========================================================================
# Sabotage log (house rule 4: a test that cannot fail is not a test)
# ==========================================================================
#
# Each check below was confirmed to FAIL against a deliberately broken
# copy before being kept:
#
# 1. RedactingFilter: reverted the exc_text/exc_info block to the
#    original `if record.exc_text:` - test_exception_message_in_a_
#    traceback, test_chained_exception_cause, test_registered_secret_in_
#    an_exception and test_installed_on_root_covers_every_child_logger
#    all failed. Restored, green.
# 2. RedactingFilter: reverted _redacted_value to `redact(a) if
#    isinstance(a, str) else a` - test_non_string_message_object,
#    test_tuple_argument_carrying_a_secret, test_bytes_argument and
#    test_object_whose_str_carries_a_secret failed. Restored, green.
# 3. RedactingFilter: deleting only the numeric short-circuit from
#    _redacted_value changed NOTHING (str(3) redacts to "3", which is
#    unchanged, so the int is returned as-is) - so that sabotage was
#    rejected as not a real one. The naive version of the same fix,
#    `return redact(str(value))` for every argument, DID fail
#    test_numeric_formats_still_work, test_string_args_and_message_are_
#    redacted and test_dict_args_keep_their_numbers_and_lose_their_
#    secrets with "%d format: a real number is required, not str".
#    Restored, green.
# 4. queries._log_limit: returned int(value) unguarded -
#    test_hostile_log_limit_falls_back_instead_of_raising and the
#    /logs?limit=abc route case failed. Restored, green.
# 5. first_run._save: made an unrecognised account_mode default to
#    "live" instead of refusing - 9 of the NOT_LIVE_BODIES cases failed.
#    Restored, green.
# 6. scheduler._run_one_cycle: hard-coded base_url_for_mode("paper")
#    instead of the saved mode - test_a_live_save_makes_the_scheduler_
#    trade_live and test_the_cycle_and_the_broker_can_never_disagree_
#    about_the_mode failed. Restored, green.
# 7. confirm_stops_resting: dropped the `side == "sell"` filter - the
#    resting BUY LIMIT then counted as a stop and
#    test_a_resting_buy_limit_is_never_counted_as_a_protective_stop
#    failed. Restored, green.


# ==========================================================================
# E9: the setup form collected fields the browser never sent.
#
# anthropic_admin_key and account_mode were rendered as inputs, explained
# at length, and then left out of saveAll()'s payload. The owner pasted a
# billing key, pressed Save, was told "All saved", and the key never left
# the page. The dashboard then reported no admin key - which reads as
# "cannot connect" - and the nightly bill check silently never ran.
#
# Reported live 2026-08-10: "tell me why it doesnt see my alpaca Admin API
# key ... running this cmd proves it is valid".
# ==========================================================================


def _app(tmp_path):
    return _setup_app(tmp_path, [])


class TestEveryRenderedFieldIsActuallySent:
    def _script(self):
        from catalyst.setup.first_run import render_setup_page
        return render_setup_page("/setup")

    @pytest.mark.parametrize("field", ["alpaca_key", "alpaca_secret",
                                       "anthropic_key", "anthropic_admin_key",
                                       "account_mode", "monthly_budget_usd"])
    def test_a_field_on_the_form_is_in_the_save_payload(self, field):
        page = self._script()
        assert f'name="{field}"' in page, f"{field} is not on the form"
        body = page.split("function saveAll()")[1].split("function ")[0]
        assert field in body, (
            f"{field} is collected by the form but saveAll() does not send "
            "it, so it is silently discarded")

    def test_the_admin_key_survives_a_round_trip_through_save(self, tmp_path):
        app = _app(tmp_path)
        admin = "sk-ant-admin01-" + "z" * 40
        app.admin_tester = lambda k: (True, "ok")
        resp = _post_save(app, _body(anthropic_admin_key=admin))
        assert json.loads(resp.body)["ok"], resp.body
        loaded = creds.load_credentials(str(tmp_path / "creds.json"))
        assert loaded.anthropic_admin_key == admin, (
            "the key was accepted and then not stored")

    def test_replacing_alpaca_keys_does_not_wipe_the_billing_key(self, tmp_path):
        """A blank admin box means "keep it", not "delete it". Otherwise
        rotating an expired Alpaca key silently switches the bill check
        off, and nothing says so."""
        app = _app(tmp_path)
        admin = "sk-ant-admin01-" + "y" * 40
        app.admin_tester = lambda k: (True, "ok")
        _post_save(app, _body(anthropic_admin_key=admin))
        _post_save(app, _body())          # no admin field at all this time
        loaded = creds.load_credentials(str(tmp_path / "creds.json"))
        assert loaded.anthropic_admin_key == admin


class TestSettingsAreChangeableAfterSetup:
    """Before this, the only post-setup page was "Replace my keys", which
    refuses to save unless all three secrets are re-pasted. The monthly
    budget was therefore fixed at whatever was typed on day one, and a
    billing key could never be added afterwards at all."""

    def _configured(self, tmp_path):
        app = _app(tmp_path)
        _post_save(app, _body(monthly_budget_usd="5"))
        return app

    def test_the_configured_page_offers_a_settings_form(self, tmp_path):
        app = self._configured(tmp_path)
        page = app.handle("GET", "/", b"", {}).body.decode()
        assert 'id="settings_form"' in page
        assert 'name="monthly_budget_usd"' in page
        assert 'name="anthropic_admin_key"' in page
        # and it must NOT demand the secrets again
        assert 'name="alpaca_secret"' not in page

    def test_the_form_shows_the_budget_currently_in_force(self, tmp_path):
        app = _app(tmp_path)
        _post_save(app, _body(monthly_budget_usd="12"))
        page = app.handle("GET", "/", b"", {}).body.decode()
        assert 'value="12' in page, "the form does not show the saved budget"

    def test_changing_the_budget_needs_no_keys_and_keeps_them(self, tmp_path):
        app = self._configured(tmp_path)
        before = creds.load_credentials(str(tmp_path / "creds.json"))
        resp = app.handle("POST", "/settings",
                          json.dumps({"monthly_budget_usd": "12"}).encode(),
                          {"content-type": "application/json"})
        assert json.loads(resp.body)["ok"], resp.body
        after = creds.load_credentials(str(tmp_path / "creds.json"))
        assert after.settings["monthly_budget_usd"] == 12
        assert after.alpaca_key == before.alpaca_key
        assert after.alpaca_secret == before.alpaca_secret
        assert after.anthropic_key == before.anthropic_key

    def test_the_new_budget_is_what_the_governor_then_uses(self, tmp_path):
        """The number has to reach the money path, not just the file -
        that is the exact defect E1 recorded the first time."""
        from decimal import Decimal

        from catalyst.orchestrator.scheduler import _owner_cap_cents
        app = self._configured(tmp_path)
        app.handle("POST", "/settings",
                   json.dumps({"monthly_budget_usd": "12"}).encode(),
                   {"content-type": "application/json"})
        loaded = creds.load_credentials(str(tmp_path / "creds.json"))
        assert _owner_cap_cents(
            loaded.settings["monthly_budget_usd"]) == Decimal("1200")

    def test_a_billing_key_can_be_added_after_setup(self, tmp_path):
        app = self._configured(tmp_path)
        assert not creds.load_credentials(
            str(tmp_path / "creds.json")).anthropic_admin_key
        admin = "sk-ant-admin01-" + "x" * 40
        app.admin_tester = lambda k: (True, "ok")
        resp = app.handle("POST", "/settings", json.dumps({
            "monthly_budget_usd": "5", "anthropic_admin_key": admin,
        }).encode(), {"content-type": "application/json"})
        assert json.loads(resp.body)["ok"], resp.body
        assert creds.load_credentials(
            str(tmp_path / "creds.json")).anthropic_admin_key == admin

    def test_a_blank_billing_key_keeps_the_saved_one(self, tmp_path):
        app = self._configured(tmp_path)
        admin = "sk-ant-admin01-" + "w" * 40
        app.admin_tester = lambda k: (True, "ok")
        app.handle("POST", "/settings", json.dumps({
            "monthly_budget_usd": "5", "anthropic_admin_key": admin,
        }).encode(), {"content-type": "application/json"})
        app.handle("POST", "/settings",
                   json.dumps({"monthly_budget_usd": "7"}).encode(),
                   {"content-type": "application/json"})
        loaded = creds.load_credentials(str(tmp_path / "creds.json"))
        assert loaded.anthropic_admin_key == admin
        assert loaded.settings["monthly_budget_usd"] == 7

    def test_a_billing_key_that_does_not_work_changes_nothing(self, tmp_path):
        app = self._configured(tmp_path)
        app.admin_tester = lambda k: (False, "Anthropic refused this key")
        resp = app.handle("POST", "/settings", json.dumps({
            "monthly_budget_usd": "9", "anthropic_admin_key": "sk-ant-nope",
        }).encode(), {"content-type": "application/json"})
        assert not json.loads(resp.body)["ok"]
        loaded = creds.load_credentials(str(tmp_path / "creds.json"))
        assert loaded.settings["monthly_budget_usd"] == 5, (
            "the budget moved despite the save being refused")
        assert not loaded.anthropic_admin_key

    @pytest.mark.parametrize("bad", ["", "lots", "nan", "inf", "-1"])
    def test_a_budget_that_is_not_a_number_changes_nothing(self, tmp_path, bad):
        app = self._configured(tmp_path)
        resp = app.handle("POST", "/settings",
                          json.dumps({"monthly_budget_usd": bad}).encode(),
                          {"content-type": "application/json"})
        assert not json.loads(resp.body)["ok"]
        assert creds.load_credentials(
            str(tmp_path / "creds.json")).settings["monthly_budget_usd"] == 5

    def test_the_settings_page_needs_the_access_code_too(self, tmp_path):
        """A write endpoint must not be the one door left unlocked."""
        app = _app(tmp_path)
        app.require_token = True
        app._stored_token = lambda: "the-code"
        resp = app.handle("POST", "/settings",
                          json.dumps({"monthly_budget_usd": "25"}).encode(),
                          {"content-type": "application/json"})
        assert resp.status == 403


class TestKeysAreReplacedOneAtATime:
    """Owner request 2026-08-10: "If I want to change the alpaca key i
    dont want to have to re-enter claude keys aswell." Two secrets typed
    to change one is how a value ends up pasted into the wrong box."""

    def _configured(self, tmp_path):
        app = _app(tmp_path)
        app.admin_tester = lambda k: (True, "ok")
        _post_save(app, _body(anthropic_admin_key="sk-ant-admin01-" + "q" * 40))
        return app

    def _replace(self, app, **payload):
        return json.loads(app.handle(
            "POST", "/replace-key", json.dumps(payload).encode(),
            {"content-type": "application/json"}).body)

    def test_replacing_alpaca_leaves_every_other_credential_alone(self, tmp_path):
        app = self._configured(tmp_path)
        before = creds.load_credentials(str(tmp_path / "creds.json"))
        r = self._replace(app, which="alpaca", alpaca_key="PKNEWNEWNEWNEWNEWNEW",
                          alpaca_secret="brandnewsecretbrandnewsecret")
        assert r["ok"], r
        after = creds.load_credentials(str(tmp_path / "creds.json"))
        assert after.alpaca_key == "PKNEWNEWNEWNEWNEWNEW"
        assert after.alpaca_secret == "brandnewsecretbrandnewsecret"
        assert after.anthropic_key == before.anthropic_key
        assert after.anthropic_admin_key == before.anthropic_admin_key
        assert after.settings == before.settings
        assert after.dashboard_token == before.dashboard_token

    def test_replacing_anthropic_leaves_the_broker_alone(self, tmp_path):
        app = self._configured(tmp_path)
        before = creds.load_credentials(str(tmp_path / "creds.json"))
        r = self._replace(app, which="anthropic",
                          anthropic_key="sk-ant-brandnewbrandnewbrandnew")
        assert r["ok"], r
        after = creds.load_credentials(str(tmp_path / "creds.json"))
        assert after.anthropic_key == "sk-ant-brandnewbrandnewbrandnew"
        assert after.alpaca_key == before.alpaca_key
        assert after.alpaca_secret == before.alpaca_secret
        assert after.anthropic_admin_key == before.anthropic_admin_key

    def test_a_key_that_does_not_work_leaves_the_working_one_in_place(self, tmp_path):
        app = self._configured(tmp_path)
        before = creds.load_credentials(str(tmp_path / "creds.json"))
        app.alpaca_tester = lambda k, s, **kw: (False, "Alpaca said 403")
        r = self._replace(app, which="alpaca", alpaca_key="PKBADBADBADBADBADBAD",
                          alpaca_secret="badbadbadbadbadbadbad")
        assert not r["ok"]
        assert "still in place" in r["message"]
        after = creds.load_credentials(str(tmp_path / "creds.json"))
        assert after.alpaca_key == before.alpaca_key
        assert after.alpaca_secret == before.alpaca_secret

    def test_half_a_broker_pair_is_refused(self, tmp_path):
        app = self._configured(tmp_path)
        r = self._replace(app, which="alpaca", alpaca_key="PKONLYTHEKEYNOSECRET")
        assert not r["ok"]
        assert "pair" in r["message"]

    def test_an_unknown_key_name_changes_nothing(self, tmp_path):
        app = self._configured(tmp_path)
        assert not self._replace(app, which="dashboard_token",
                                 anthropic_key="x")["ok"]

    def test_replacing_a_key_needs_the_access_code(self, tmp_path):
        app = self._configured(tmp_path)
        app.require_token = True
        app._stored_token = lambda: "the-code"
        resp = app.handle("POST", "/replace-key",
                          json.dumps({"which": "anthropic",
                                      "anthropic_key": "sk-ant-x"}).encode(),
                          {"content-type": "application/json"})
        assert resp.status == 403

    def test_the_page_offers_each_key_its_own_button(self, tmp_path):
        app = self._configured(tmp_path)
        page = app.handle("GET", "/", b"", {}).body.decode()
        assert "replaceKey('alpaca')" in page
        assert "replaceKey('anthropic')" in page
        assert "does not mean re-typing anything else" in page


class TestTheBudgetFieldGuardsAgainstATypo:
    """The hard $25 ceiling was removed at the owner's request - how much
    of your own money to spend is not a safety question, and a hard-coded
    number can only go stale as the account grows.

    What a ceiling was really protecting against is a slipped keyboard,
    and that is a question about the KEYSTROKE, so the guard lives here
    at the point of entry where a confirmation can be asked for."""

    def _configured(self, tmp_path, budget="5"):
        app = _app(tmp_path)
        app.admin_tester = lambda k: (True, "ok")
        _post_save(app, _body(monthly_budget_usd=budget))
        return app

    def _settings(self, app, **payload):
        return json.loads(app.handle(
            "POST", "/settings", json.dumps(payload).encode(),
            {"content-type": "application/json"}).body)

    def test_a_wild_jump_is_refused_until_confirmed(self, tmp_path):
        app = self._configured(tmp_path, "5")
        r = self._settings(app, monthly_budget_usd="2000")
        assert not r["ok"]
        assert r["needs_confirmation"] is True
        assert "2400% a year" in r["message"]
        loaded = creds.load_credentials(str(tmp_path / "creds.json"))
        assert loaded.settings["monthly_budget_usd"] == 5, "nothing may change"

    def test_the_same_figure_confirmed_is_obeyed(self, tmp_path):
        """The point is a speed bump, not a wall. Confirmed, the owner's
        number is the budget however large."""
        app = self._configured(tmp_path, "5")
        r = self._settings(app, monthly_budget_usd="2000",
                           confirm_big_budget="1")
        assert r["ok"], r
        loaded = creds.load_credentials(str(tmp_path / "creds.json"))
        assert loaded.settings["monthly_budget_usd"] == 2000

    @pytest.mark.parametrize("new", ["10", "25", "50"])
    def test_an_ordinary_increase_needs_no_confirmation(self, tmp_path, new):
        """5 -> 50 is a tenfold rise and still plausible as a decision.
        The guard must not turn into the ceiling it replaced."""
        app = self._configured(tmp_path, "5")
        assert self._settings(app, monthly_budget_usd=new)["ok"]

    def test_growing_the_budget_in_steps_is_never_blocked(self, tmp_path):
        """The owner's stated reason for removing the ceiling: "so i can
        manually change it as we grow". Each step is measured against the
        CURRENT figure, so repeated raises keep working."""
        app = self._configured(tmp_path, "5")
        for target in ("40", "300", "2500"):
            assert self._settings(app, monthly_budget_usd=target)["ok"], target
        loaded = creds.load_credentials(str(tmp_path / "creds.json"))
        assert loaded.settings["monthly_budget_usd"] == 2500

    def test_lowering_is_never_guarded(self, tmp_path):
        app = self._configured(tmp_path, "100")
        assert self._settings(app, monthly_budget_usd="1")["ok"]

    def test_zero_is_never_guarded(self, tmp_path):
        """0 means stop, and stopping must never need a confirmation."""
        app = self._configured(tmp_path, "100")
        assert self._settings(app, monthly_budget_usd="0")["ok"]

    def test_the_form_carries_the_confirmation_box(self, tmp_path):
        app = self._configured(tmp_path)
        page = app.handle("GET", "/", b"", {}).body.decode()
        assert 'id="confirm_big_budget"' in page
        assert "confirm_big_budget" in page.split("function saveSettings")[1]
