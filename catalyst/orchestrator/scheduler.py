"""systemd-invoked entry point.

Started by the unit installed at `install/catalyst.service`, as the
service user, with CATALYST_DB and CATALYST_CREDENTIALS in its
environment. Thin by design (ARCHITECTURE.md section 8: "Thin wiring
only") - it does three things and no domain logic:

1. Makes sure the database exists (safe on every start; the schema is
   CREATE TABLE IF NOT EXISTS throughout).
2. Serves the setup page on port 8000 so the owner can enter their
   credentials in a browser and never touch a config file. At stage 6
   the dashboard takes this port over and mounts `SetupApp` itself.
3. Runs a trading cycle on a schedule, once credentials exist.

It must not exit on a bad cycle: systemd's Restart=on-failure covers a
crash, but an unattended bot that stops trying because one cycle raised
is a silent outage. Every loop failure is logged with its traceback and
the loop continues.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

DEFAULT_DB = "/var/lib/catalyst/catalyst.db"
DEFAULT_CYCLE_SECONDS = 900       # 15 minutes
WAITING_LOG_SECONDS = 300         # how often to repeat "still waiting for setup"

_log = logging.getLogger("catalyst.scheduler")
_stop = threading.Event()


def configure_logging(level: str | None = None) -> None:
    """Log to stdout for the journal, with the redacting filter installed
    on the root logger so no credential can reach the log from anywhere
    in the system."""
    from catalyst.setup.credentials import install_redacting_filter

    logging.basicConfig(
        level=getattr(logging, (level or os.environ.get("CATALYST_LOG_LEVEL", "INFO")).upper(),
                      logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    install_redacting_filter()
    _install_db_log_handler()


class _DbLogHandler(logging.Handler):
    """Every log line into the database as well as the journal.

    The brief asks for logs searchable from the browser so nobody has to
    SSH in to troubleshoot. The table and the page both existed; nothing
    ever wrote a row, so the Logs page was permanently blank and the
    promise was false (owner-reported 2026-08-11).

    Opens its own connection per emit. That is not the fast choice, but
    a logging handler that holds a connection across threads is how a
    logger starts corrupting the database it is reporting on, and this
    runs a few times a minute.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            import sqlite3
            from datetime import datetime, timezone

            from catalyst.dashboard.redact import redact

            tb = ""
            if record.exc_info:
                tb = redact(logging.Formatter().formatException(record.exc_info))
            conn = sqlite3.connect(db_path(), timeout=2.0)
            try:
                conn.execute(
                    "INSERT INTO logs (ts, level, component, message, "
                    "cycle_id, candidate_id, traceback_text, context_json) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(),
                     record.levelname, record.name,
                     redact(record.getMessage()),
                     getattr(record, "cycle_id", None),
                     getattr(record, "candidate_id", None),
                     tb or None, None))
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            # A logger that raises takes down whatever it was reporting
            # on. Never. The journal still has the line either way.
            pass


def _install_db_log_handler() -> None:
    root = logging.getLogger()
    if any(isinstance(h, _DbLogHandler) for h in root.handlers):
        return
    handler = _DbLogHandler()
    handler.setLevel(logging.INFO)
    root.addHandler(handler)


def db_path() -> str:
    return os.environ.get("CATALYST_DB", DEFAULT_DB)


def start_setup_server() -> threading.Thread | None:
    """Serve the setup/first-run page in the background.

    Returns None (having logged why) rather than raising if the port is
    taken: a bot that refuses to trade because its web page could not
    bind is the wrong trade-off.
    """
    from catalyst.setup.first_run import DEFAULT_BIND, DEFAULT_PORT, SetupApp

    host = os.environ.get("CATALYST_BIND", DEFAULT_BIND)
    port = int(os.environ.get("CATALYST_PORT", DEFAULT_PORT))
    try:
        # The full dashboard IS the service's web face (BUILD-BRIEF calls
        # it not optional; stress stage-8 E2 found only the setup form was
        # served). SetupApp mounts at /setup; an unconfigured system's
        # "/" redirects there so install.sh's printed link still lands on
        # the form.
        from catalyst.dashboard.server import make_server as make_dash_server
        server = make_dash_server(host, port, db_path(),
                                  setup_app=SetupApp(path_prefix="/setup"))
    except OSError as exc:
        # Actionable, because the owner is not a developer (owner report
        # 2026-08-10: the old message named no command and no culprit).
        # The dangerous case - another Catalyst - is caught earlier by
        # the instance lock in main(), so by here it is another program.
        _log.error(
            "Could not open the setup page on %s:%s (%s).\n"
            "  What this means: the bot is fine and keeps trading, but the web "
            "page will not answer until port %s is free.\n"
            "  To find what is using it, run:  sudo ss -ltnp | grep :%s\n"
            "  Then either stop that program, or give Catalyst a different port "
            "with:  sudo systemctl edit catalyst   and add\n"
            "      [Service]\n"
            "      Environment=CATALYST_PORT=8001\n"
            "  followed by:  sudo systemctl restart catalyst",
            host, port, exc, port, port,
        )
        return None

    thread = threading.Thread(target=server.serve_forever, name="setup-http", daemon=True)
    thread.start()
    _log.info("Setup page listening on %s:%s", host, port)
    return thread


def _handle_signal(signum, _frame) -> None:
    _log.info("Received signal %s - shutting down cleanly.", signum)
    _stop.set()


def _anthropic_transport(api_key: str):
    """The live Messages API transport for research.investigate(). Built
    only here, only from the credential store - the boundary itself never
    sees a key. Documented shapes; exercised offline via injected stubs
    (no Anthropic key exists in the build environment)."""
    import httpx

    class AnthropicHTTPError(RuntimeError):
        """Carries the API's own explanation, not just a status code."""

    def transport(payload: dict) -> dict:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload, timeout=120.0)
        if resp.status_code >= 400:
            # raise_for_status() THREW THE BODY AWAY, so the owner's
            # funnel showed "400 Bad Request" and a link to MDN - which
            # says nothing about what this request got wrong. Anthropic
            # states the exact objection in the response body; that is
            # the only part worth reading (house rule 3).
            detail = (resp.text or "")[:800]
            raise AnthropicHTTPError(
                f"HTTP {resp.status_code} from the Messages API: {detail}")
        return resp.json()

    return transport


# How far back the nightly bill check will backfill days missed while
# the service was down (cost-audit F3: a three-day outage must not
# permanently lose three days from the drift window).
RECONCILE_BACKFILL_DAYS = 7


def _maybe_reconcile_yesterday(db_file: str) -> None:
    """Nightly bill check: compare the local ledger against Anthropic's
    Cost API for recent CLOSED days, when the owner supplied an admin
    key. Backfills the oldest unreconciled day in the last
    RECONCILE_BACKFILL_DAYS, one day per cycle. Read-only against the
    API; a discrepancy pauses scheduled spend until a human acknowledges
    it (cost.tracker.reconcile_day). A FAILED check is itself recorded
    as a check_failed row with the raw error beside it (cost-audit F2:
    a dark instrument must not look like a healthy one), and is retried
    on later cycles because check_failed rows do not mark a day done.
    Never fatal to trading."""
    import json
    import sqlite3
    import traceback
    import uuid
    from datetime import datetime, timedelta, timezone
    from functools import partial

    from catalyst.setup.credentials import load_credentials

    try:
        creds = load_credentials()
    except Exception:  # noqa: BLE001
        return
    if not creds.anthropic_admin_key:
        return
    today = datetime.now(timezone.utc).date()
    conn = sqlite3.connect(db_file)
    try:
        from catalyst.cost.cost_api import fetch_cost_api_day
        from catalyst.cost.tracker import reconcile_day

        # NEVER RECONCILE A DAY BEFORE THE BOT'S FIRST SPEND. The Cost
        # API reports the whole ORGANISATION's bill; the local ledger
        # holds only what this bot spent. On any day the bot did not run,
        # the honest comparison is $0.00 against whatever else the key
        # was used for - which is not a discrepancy, it is two different
        # questions. The owner was shown three of those and asked to
        # acknowledge them without understanding what they meant
        # (2026-08-11).
        first_row = conn.execute(
            "SELECT MIN(priced_at) FROM cost_events").fetchone()
        first_day = None
        if first_row and first_row[0]:
            try:
                first_day = datetime.fromisoformat(str(first_row[0])).date()
            except ValueError:
                first_day = None

        # oldest first, so the drift window accumulates in date order
        for days_back in range(RECONCILE_BACKFILL_DAYS, 0, -1):
            day = today - timedelta(days=days_back)
            if first_day is None or day < first_day:
                continue
            done = conn.execute(
                "SELECT 1 FROM cost_reconciliation_events "
                "WHERE target_date = ? AND action_taken != 'check_failed'",
                (day.isoformat(),)).fetchone()
            if done:
                continue
            try:
                result = reconcile_day(
                    day, conn,
                    partial(fetch_cost_api_day,
                            admin_key=creds.anthropic_admin_key))
            except Exception as exc:  # noqa: BLE001 - never kill trading
                _log.exception(
                    "The nightly bill check for %s failed; recorded as a "
                    "check_failed row, will retry next cycle. Trading is "
                    "unaffected.", day)
                detail = {"check_failed": traceback.format_exc()[-2000:]}
                raw_body = getattr(exc, "body", None)
                if raw_body:   # house rule 3: raw upstream beside the failure
                    detail["raw_upstream_body"] = str(raw_body)[:2000]
                already = conn.execute(
                    "SELECT 1 FROM cost_reconciliation_events "
                    "WHERE target_date = ? AND action_taken = 'check_failed'",
                    (day.isoformat(),)).fetchone()
                if not already:
                    conn.execute(
                        "INSERT INTO cost_reconciliation_events "
                        "(id, target_date, kind, component, "
                        " local_total_cents, cost_api_total_cents, "
                        " discrepancy_cents, threshold_cents, "
                        " api_raw_response, api_record_count, action_taken, "
                        " acknowledged_by, acknowledged_at, reconciled_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), day.isoformat(), "all", "{}",
                         "0", "0", "0", "0",
                         json.dumps(detail),
                         0, "check_failed", None, None,
                         datetime.now(timezone.utc).isoformat()))
                    conn.commit()
                return   # a failing key fails for every day; stop here
            if result.action_taken != "none":
                _log.warning(
                    "Nightly bill check for %s: ledger %sc vs Anthropic "
                    "%sc - action %s. Scheduled spending is paused until "
                    "the discrepancy is acknowledged on the cost page.",
                    day, result.local_total_cents,
                    result.cost_api_total_cents, result.action_taken)
            else:
                _log.info("Nightly bill check for %s: ledger matches "
                          "Anthropic's records (%sc).", day,
                          result.cost_api_total_cents)
            return   # one day per cycle; the next cycle takes the next
    finally:
        conn.close()


def _maybe_refresh_benchmark(state: dict) -> None:
    """Keep the SPY comparison series current, once a day.

    The dashboard's headline is performance against the S&P net of
    costs; `data/` is gitignored, so a fresh install arrives with no
    benchmark at all, and nothing else in the running bot ever writes
    the cache. Failures are logged and never reach the trading loop -
    a stale benchmark is a reporting problem, not a trading one."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date()
    if state.get("benchmark_day") == today:
        return
    state["benchmark_day"] = today
    try:
        from catalyst.data import benchmark
        from catalyst.dashboard.db import bars_path
        from catalyst.setup.credentials import load_credentials

        creds = load_credentials()
        result = benchmark.refresh_benchmark(
            bars_path(), creds.alpaca_key, creds.alpaca_secret)
        if result.skipped_reason in (None, "already_current"):
            _log.info("Benchmark series: %s bar(s) added, %s.",
                      result.written, result.skipped_reason or "up to date")
        else:
            _log.warning(
                "Benchmark series not updated (%s). The performance page "
                "compares against SPY, so that comparison will stay stale "
                "until this succeeds. Raw upstream: %s",
                result.skipped_reason, (result.raw_response or "")[:500])
    except Exception:  # noqa: BLE001 - reporting must never stop trading
        _log.exception("The benchmark refresh failed; trading is unaffected.")


def _owner_cap_cents(budget_usd):
    """The owner's monthly budget, in cents, or None for the base cap.

    The setup page validates this on the way in, but the credentials
    file outlives any one version of that page, and a value it cannot
    parse must not take the trading cycle down with it. Falling back to
    None means the governor uses its $5 base - the safe direction, and
    it is logged loudly because a budget silently ignored is how the
    setup page's promise became false once already.
    """
    from decimal import Decimal as _D
    if budget_usd is None:
        return None
    try:
        cents = (_D(str(budget_usd)) * 100).quantize(_D("1"))
    except Exception:  # noqa: BLE001 - any unparseable value, same answer
        _log.error(
            "The saved monthly research budget %r is not a number the "
            "governor can use. Falling back to the $5 base cap. Re-enter "
            "it on the setup page to raise it.", budget_usd)
        return None
    if not cents.is_finite() or cents < 0:
        _log.error(
            "The saved monthly research budget %r is not a usable amount "
            "(%s cents). Falling back to the $5 base cap.", budget_usd, cents)
        return None
    return cents


def _run_one_cycle(db_file: str):
    """Wire the live dependencies and run exactly one cycle. Thin by
    design: every piece here is constructed, none is decided."""
    import sqlite3

    from catalyst.data.form4_adapter import flatten_form4_events
    from catalyst.data.sources.edgar_form4 import fetch_events
    from catalyst.discovery.candidates import build_candidates
    from catalyst.discovery.correlation import cluster
    from catalyst.execution.broker import Broker
    from catalyst.orchestrator.cycle import run_cycle
    from catalyst.setup.credentials import load_credentials

    from catalyst.execution.broker import base_url_for_mode

    creds = load_credentials()
    account_mode = str((creds.settings or {}).get("account_mode", "paper"))
    broker = Broker(creds.alpaca_key, creds.alpaca_secret,
                    base_url=base_url_for_mode(account_mode))
    transport = (_anthropic_transport(creds.anthropic_key)
                 if creds.anthropic_key else None)

    def _filing_cache(conn_path):
        """(already_have, on_fetched) backed by edgar_filings.

        This pair is what stops the feed re-downloading its whole window
        every cycle. Measured before it existed: 2,815 requests a pass,
        9.4 minutes of continuous sec.gov traffic inside a 15-minute
        cycle, and a rate-limit block on 2026-08-11.
        """
        import sqlite3 as _sq

        def already_have(accession):
            try:
                c = _sq.connect(conn_path, timeout=5.0)
                try:
                    row = c.execute(
                        "SELECT parsed_json FROM edgar_filings WHERE "
                        "accession = ?", (accession,)).fetchone()
                finally:
                    c.close()
                return json.loads(row[0]) if row else None
            except Exception:  # noqa: BLE001 - a cache miss is never fatal
                return None

        def on_fetched(accession, parsed):
            try:
                c = _sq.connect(conn_path, timeout=5.0)
                try:
                    c.execute(
                        "INSERT OR REPLACE INTO edgar_filings "
                        "(accession, parsed_json, fetched_at) VALUES (?,?,?)",
                        (accession, json.dumps(parsed, default=str),
                         datetime.now(timezone.utc).isoformat()))
                    c.commit()
                finally:
                    c.close()
            except Exception:  # noqa: BLE001 - storing is best effort
                pass

        return already_have, on_fetched

    def feed(since, until):
        """All three feeds. Form 4 is the only one that may fail the pass.

        THE TWO NEW FEEDS ARE ADDITIVE AND NEVER FATAL. Form 4 is the
        graded strategy - the one the backtest measured over 2016-2026 -
        so a Form 4 outage is a real discovery failure and propagates,
        exactly as it always has. EDGAR full-text search and the news
        firehose ENRICH: they create candidates only where they agree
        with something else, so losing one of them costs breadth, not
        correctness. Letting either take the cycle down would trade a
        working strategy for a new one's outage.
        """
        from catalyst.data.sources.edgar_form4 import (
            RateLimitBlocked, fetch_form4,
        )

        have, store = _filing_cache(db_file)
        try:
            got = fetch_form4(since, until, already_have=have,
                              on_fetched=store)
            _log.info(
                "Form 4: %d request(s), %d filing(s) replayed from local "
                "storage. Cache hits cost nothing and are what keeps this "
                "inside sec.gov's fair-use limits.",
                got.requests_made, got.from_cache)
            events = list(flatten_form4_events(got.events))
        except RateLimitBlocked as exc:
            # NOT a feed error to log and continue past. sec.gov blocked
            # this IP and every further request extends the timeout, so
            # the whole pass stops touching SEC endpoints - full-text
            # search below shares the same budget and is skipped too.
            _log.error("sec.gov rate-limited this IP; skipping every SEC "
                       "feed this pass. %s", exc)
            return []

        try:
            from catalyst.data.sources import edgar_fts

            fts = edgar_fts.fetch_events(since, until)
        except RateLimitBlocked as exc:
            _log.error("sec.gov rate-limited this IP during full-text "
                       "search; no further SEC request this pass. %s", exc)
            fts = None
        except Exception:  # noqa: BLE001 - breadth is not correctness
            _log.exception(
                "EDGAR full-text search failed; the pass continues on the "
                "other feeds. Cross-feed conjunctions will be thinner.")
            fts = None
        if fts is not None:
            events.extend(fts.events)
            for err in fts.errors:
                _log.warning("Full-text search query %r failed: %s",
                             err.get("query"), str(err.get("error"))[:300])

        try:
            from catalyst.data.sources import alpaca_news

            news_start, news_end = alpaca_news.default_window(days=3)
            news = alpaca_news.fetch_events(
                news_start, news_end,
                alpaca_key=creds.alpaca_key,
                alpaca_secret=creds.alpaca_secret)
            events.extend(news.events)
            if news.error:
                _log.warning("News feed: %s", news.error[:300])
        except Exception:  # noqa: BLE001
            _log.exception(
                "The news feed failed; the pass continues on the other "
                "feeds. Sentiment and news links will be missing.")
        return events

    def build_candidates_all(raw_events, as_of):
        """Form 4 clusters, PLUS candidates from cross-feed agreement.

        The two builders are kept apart deliberately. build_candidates is
        line-for-line the backtest arm and must stay that way for its
        measured edge to mean anything; conjunctions are a different
        claim on different evidence. A conjunction never re-derives a
        Form 4 cluster, so one piece of evidence cannot produce two rows
        on the funnel.
        """
        from catalyst.discovery.conjunctions import build_conjunction_candidates

        out = list(build_candidates(raw_events, as_of))
        seen = {c.id for c in out}
        try:
            extra, dropped = build_conjunction_candidates(raw_events, as_of)
            for cand in extra:
                if cand.id not in seen:
                    seen.add(cand.id)
                    out.append(cand)
            _log.info("Conjunctions: %d candidate(s) from cross-feed "
                      "agreement, %d considered and dropped.",
                      len(extra), len(dropped))
            for ticker, why in dropped[:10]:
                _log.debug("Conjunction dropped %s: %s", ticker, why)
        except Exception:  # noqa: BLE001 - never lose the graded strategy
            _log.exception(
                "Conjunction discovery failed; the Form 4 candidates from "
                "this pass are unaffected.")
        return out

    conn = sqlite3.connect(db_file)
    try:
        owner_cap = _owner_cap_cents((creds.settings or {}).get("monthly_budget_usd"))
        return run_cycle(conn, broker, transport, feed,
                         build_candidates_all, cluster,
                         account_mode=account_mode,
                         owner_monthly_cap_cents=owner_cap)
    finally:
        conn.close()
        broker.close()


LOCK_RETRY_SECONDS = 30


def _acquire_lock_or_stand_by(once: bool = False) -> bool:
    """Take the single-instance lock, WAITING for it if another copy
    holds it. True when this copy may trade.

    Risk review finding 1: exiting 0 here was the dangerous option.
    The unit file treats a clean exit as a deliberate stop, so a
    systemd start that lost the race to a manual copy would stay down -
    and when that manual copy later died, nothing would be running,
    nothing would restart it, and systemctl would report success. An
    account with open positions and no scheduler has no re-placed DAY
    stops, no hard-exit checks and no kill switches. Standing by costs
    a log line and self-heals the moment the holder exits.
    """
    from catalyst.orchestrator import instance_lock

    # NOTE: the returned handle is intentionally not stored here - the
    # lock lives as long as instance_lock._held keeps the fd open. Do
    # not "tidy" that global away: closing the fd frees the lock while
    # this process keeps trading (risk review finding 7).
    if instance_lock.acquire() is not None:
        return True

    port = os.environ.get("CATALYST_PORT", "8000")
    if once:
        # A one-off manual/cron run. Nothing is supervising it, so
        # there is no stay-down hazard - just decline.
        _log.error(
            "Catalyst is already running on this machine (process %s), so this "
            "one-off run is stopping. Two copies would place every order twice. "
            "The running copy is unaffected; its page is on port %s.",
            instance_lock.holder_pid(), port)
        return False

    _log.error(
        "Another Catalyst is already running on this machine (process %s). This "
        "copy will NOT trade while that one holds the lock - two copies would "
        "place every order twice and take double the intended position. It will "
        "stay up and take over automatically if that copy stops, rechecking "
        "every %ss. The running copy's page is on port %s.",
        instance_lock.holder_pid(), LOCK_RETRY_SECONDS, port)
    while not _stop.is_set():
        _stop.wait(LOCK_RETRY_SECONDS)
        if _stop.is_set():
            break
        if instance_lock.acquire() is not None:
            _log.warning(
                "The other Catalyst has stopped; this copy has taken over and "
                "is now trading.")
            return True
    _log.info("Asked to shut down while waiting for the other copy.")
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    configure_logging()

    from catalyst.setup.credentials import credentials_exist
    from catalyst.storage import init_db

    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    init_db(path).close()
    _log.info("Database ready at %s", path)

    if "--selftest" in args:
        # Used by install.sh to prove the installed package can actually
        # start before the service is enabled.
        configured = credentials_exist()
        _log.info("Self-test passed. Credentials %s.",
                  "are set" if configured else "not entered yet")
        return 0

    # Registered BEFORE the lock wait below, so a stand-by copy still
    # answers systemctl stop instead of having to be killed.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle_signal)

    once = "--once" in args

    # ONE Catalyst per machine, taken BEFORE anything can trade (owner
    # report 2026-08-10). Two schedulers on one Alpaca account duplicate
    # every order and double the intended exposure; the only symptom was
    # a line about the web page.
    if not _acquire_lock_or_stand_by(once=once):
        return 0

    start_setup_server()

    cycle_seconds = int(os.environ.get("CATALYST_CYCLE_SECONDS", DEFAULT_CYCLE_SECONDS))
    _daily_state: dict = {}   # once-a-day markers for the loop
    last_waiting_log = 0.0
    announced_ready = False

    while not _stop.is_set():
        if not credentials_exist():
            now = time.monotonic()
            if now - last_waiting_log > WAITING_LOG_SECONDS or last_waiting_log == 0.0:
                _log.info(
                    "Waiting for setup: open the Catalyst page in a browser and enter "
                    "the Alpaca and Anthropic details. No trading happens until then."
                )
                last_waiting_log = now
            if once:
                return 0
            _stop.wait(5)
            continue

        if not announced_ready:
            _log.info("Setup is complete. Trading cycles are running.")
            announced_ready = True

        try:
            _maybe_reconcile_yesterday(path)
            _maybe_refresh_benchmark(_daily_state)
            report = _run_one_cycle(path)
            if report.kill_switch.tripped:
                _log.warning("Kill switch tripped: %s. New entries are "
                             "blocked; protective duties still ran.",
                             report.kill_switch.reason)
            for err in report.errors:
                _log.error("cycle: %s", err)
        except Exception:  # noqa: BLE001 - an unattended service keeps trying
            _log.exception("A trading cycle failed. The next one will still run.")

        if once:
            return 0
        _stop.wait(cycle_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
