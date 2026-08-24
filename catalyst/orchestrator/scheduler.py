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

#: Set to cut the sleep between cycles short. The signal handler sets it
#: (so a stop is not waited out) and so does the setup page the moment
#: credentials are saved or replaced - a swapped Alpaca key should be
#: noticed in seconds, not on whatever quarter-hour boundary comes next.
_wake = threading.Event()


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


def _credentials_changed(which: str = "all") -> None:
    """The setup page just saved or replaced credentials.

    Runs on the web server's thread, so it does the smallest possible
    thing: it says so in the log and cuts the sleep short. The next
    cycle - a second or two away rather than up to fifteen minutes -
    reloads the file, reads the account those keys actually point at,
    and strikes the benchmark baseline if it is a different account
    (`_sync_benchmark_baseline`). Keeping the broker read on the trading
    loop means there is exactly ONE place that decides the baseline, and
    a browser request can never hang on Alpaca being slow.

    No credential value is passed in, and none is read here.
    """
    _log.info(
        "New details saved from the setup page (%s). Checking straight away "
        "which broker account they belong to, rather than waiting for the "
        "next scheduled pass.", which)
    _wake.set()


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
        # OWNER-ASKED: "when I change the Alpaca keys i want it to
        # register there is a new account". The cycle below reloads the
        # credentials file every pass, so a swap is picked up anyway -
        # but up to fifteen minutes later, during which the page still
        # shows the old account's comparison and the owner reasonably
        # concludes nothing happened. Waking the loop turns that into
        # seconds. It deliberately does NO work on the web thread: no
        # broker call, no database write, nothing that can make pressing
        # Save hang on an unreachable Alpaca.
        # The full dashboard IS the service's web face (BUILD-BRIEF calls
        # it not optional; stress stage-8 E2 found only the setup form was
        # served). SetupApp mounts at /setup; an unconfigured system's
        # "/" redirects there so install.sh's printed link still lands on
        # the form.
        from catalyst.dashboard.server import make_server as make_dash_server
        server = make_dash_server(
            host, port, db_path(),
            setup_app=SetupApp(path_prefix="/setup",
                               on_credentials_changed=_credentials_changed))
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
    _wake.set()      # do not sit out the rest of the sleep first


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
                # CORRECT THE LEDGER BEFORE COMPARING IT. Owner-reported
                # 2026-08-20: "it doesnt accurately reflect my costings,
                # i need it updating historically so it looks correct".
                #
                # A day the bot spent on but failed to record - 2026-08-15
                # billed 45.7446c against an empty ledger - is a hole in
                # every historical figure AND a pause the next morning,
                # from one cause. Rebuilding the day from Anthropic's own
                # token counts fixes both, and it is only legitimate
                # because price() reproduces their charges exactly:
                # verified to the cent on five separate days.
                #
                # Failure here never blocks the comparison; an
                # uncorrected day simply reconciles as it always did.
                try:
                    from catalyst.cost.backfill import backfill_day, fetch_usage_day
                    fixed = backfill_day(
                        conn, day,
                        fetch=partial(fetch_usage_day,
                                      admin_key=creds.anthropic_admin_key))
                    if fixed.applied:
                        _log.warning(
                            "Ledger corrected for %s: %s", day, fixed.reason)
                except Exception:  # noqa: BLE001 - reporting, never trading
                    _log.exception(
                        "Could not rebuild the ledger for %s from the usage "
                        "report; reconciling the day as recorded.", day)

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


def record_benchmark_refresh(conn, result, *, now=None) -> bool:
    """Keep what the SPY refresh actually did, where the chart can read it.

    OWNER-REPORTED 2026-08-24: "its stopped tracking SPY", against a
    performance chart whose SPY line ended before the bot's own.

    A weekend and a dead feed make exactly the same shape on that chart.
    The refresh has always known which it was - `routine` is decided
    beside the reasons themselves - and had nowhere to say it except the
    log, which the dashboard cannot reach. Now it says it in the
    database, with the raw upstream body beside a failure (house rule 3).

    Never raises: a database that cannot take the note must not cost the
    refresh itself, which is the part that keeps the comparison alive.
    """
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    now = now or _dt.now(_tz.utc)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO benchmark_refreshes "
            "(checked_at, outcome, routine, bars_written, last_bar_day, "
            " feed, raw_response) VALUES (?,?,?,?,?,?,?)",
            (now.isoformat(),
             result.skipped_reason or "updated",
             1 if result.routine else 0,
             int(result.written or 0),
             result.last_day.isoformat() if result.last_day else None,
             result.feed or None,
             (result.raw_response or None)))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001 - the note is not worth the refresh
        return False


def _maybe_refresh_benchmark(state: dict | None, *, force: bool = False,
                             db_file: str | None = None, conn=None) -> None:
    """Keep the SPY comparison series current, once a day.

    The dashboard's headline is performance against the S&P net of
    costs; `data/` is gitignored, so a fresh install arrives with no
    benchmark at all, and nothing else in the running bot ever writes
    the cache. Failures are logged and never reach the trading loop -
    a stale benchmark is a reporting problem, not a trading one.

    `force=True` ignores the once-a-day guard. It is used when the
    benchmark BASELINE changes - a new broker account restarts the
    comparison from today, and indexing a brand-new window against bars
    that stopped four days ago is how a restarted comparison reads as
    "no data". Note what force does NOT do: it never clears the cache.
    The bars are raw SPY closes on a fixed basis (feed + adjustment,
    pinned in cache metadata), so they are true regardless of which
    account is connected; only the day the comparison is indexed FROM
    moves. Wiping and re-fetching a decade of history to change an
    index base would be work for nothing, and would throw away the one
    thing an unentitled account cannot get back.
    """
    from datetime import datetime, timezone

    state = {} if state is None else state
    today = datetime.now(timezone.utc).date()
    if state.get("benchmark_day") == today and not force:
        return
    # NOT MARKED DONE YET. Owner-reported 2026-08-21: "SPY line has
    # failed to populate for 48 hours", while every other daily job kept
    # working.
    #
    # The marker used to be set HERE, before the attempt - so one failed
    # refresh burned the whole day's only try. A transient timeout or
    # rate limit meant the series silently fell a day behind, and two of
    # them meant two days, which is exactly the gap on the chart. The
    # comparison decays and nothing ever says so, because from the
    # bot's point of view the job "ran".
    #
    # Marked on SUCCESS instead: a failure is retried on the next cycle,
    # fifteen minutes later, rather than tomorrow.
    try:
        from catalyst.data import benchmark
        from catalyst.dashboard.db import bars_path
        from catalyst.setup.credentials import load_credentials

        creds = load_credentials()
        result = benchmark.refresh_benchmark(
            bars_path(), creds.alpaca_key, creds.alpaca_secret)
        if conn is not None:
            record_benchmark_refresh(conn, result)
        elif db_file:
            import sqlite3 as _sq

            own = _sq.connect(db_file)
            try:
                record_benchmark_refresh(own, result)
            finally:
                own.close()
        if result.skipped_reason in (None, "already_current"):
            state["benchmark_day"] = today          # only now is it done
            _log.info("Benchmark series: %s bar(s) added, %s.",
                      result.written, result.skipped_reason or "up to date")
        elif result.routine:
            # NOT A WARNING, AND THE DAY IS DONE. A weekend or a market
            # holiday has no bar to fetch, and re-asking every fifteen
            # minutes produced a warning every fifteen minutes for the
            # whole weekend - routine attrition reading as damage,
            # exactly the failure CLAUDE.md names. The next weekday
            # moves the window and the guard lets it through again.
            state["benchmark_day"] = today
            _log.info(
                "Benchmark series: nothing to fetch (%s) - the window held "
                "no trading day, so there is no bar to be missing.",
                result.skipped_reason)
        else:
            _log.warning(
                "Benchmark series not updated (%s). The performance page "
                "compares against SPY, so that comparison will stay stale "
                "until this succeeds. Raw upstream: %s",
                result.skipped_reason, (result.raw_response or "")[:500])
            _maybe_rebuild_refused_feed(
                conn, db_file, result, creds, state, today)
    except Exception:  # noqa: BLE001 - reporting must never stop trading
        _log.exception("The benchmark refresh failed; trading is unaffected.")


#: A pinned feed refused on every attempt across this many distinct days,
#: with no success in between, is not an outage - it is an entitlement
#: the credentials no longer have, and every future refresh will ask the
#: same refused feed forever. Two days rather than one so a single bad
#: afternoon cannot discard a series.
FEED_REFUSED_DAYS_BEFORE_REBUILD = 2

#: The markers refresh_benchmark uses for "the feed itself said no",
#: as opposed to a flaky upstream. Kept in one place, matched rather
#: than enumerated, so a new reason of the same kind is still caught.
_REFUSAL_MARKERS = ("feed_no_longer_available", "feeds_refused_http")


def _maybe_rebuild_refused_feed(conn, db_file, result, creds, state, today):
    """Move the SPY series onto a feed these credentials can read.

    OWNER-ASKED 2026-08-24: "can we sort it so its got it historical and
    ready for the future."

    refresh_benchmark pins the feed on purpose - a series half
    consolidated tape and half one exchange's prints makes every
    comparison against it quietly wrong. But the pin has a trap the
    owner has now hit: a cache built on `sip` keeps asking for `sip`,
    and a key without that subscription is refused every time, forever.
    Waiting cannot fix it, so a page that says "wait" is wrong.

    THE PIN IS ABOUT MIXING, AND A REBUILD DOES NOT MIX. It discards the
    series and refetches the whole thing on one basis. So the only thing
    the manual gate was really protecting was the decision to throw away
    history - and against a benchmark that can never update again, that
    trade is worth making by itself.

    EVIDENCE, NOT A GUESS. It fires only when the refusals span
    FEED_REFUSED_DAYS_BEFORE_REBUILD distinct days with no successful
    refresh in between, and at most once a day, so a bad afternoon and a
    rebuild that also fails both cost one attempt rather than a loop.
    """
    reason = str(result.skipped_reason or "")
    if not any(m in reason for m in _REFUSAL_MARKERS):
        return
    if state.get("benchmark_rebuild_day") == today:
        return          # one attempt a day, whatever happens

    own = None
    try:
        if conn is None:
            if not db_file:
                return
            import sqlite3 as _sq

            own = conn = _sq.connect(db_file)
        rows = conn.execute(
            "SELECT outcome, routine FROM benchmark_refreshes "
            "WHERE date(checked_at) >= date(?, '-14 day') "
            "ORDER BY checked_at DESC LIMIT 400", (today.isoformat(),)
        ).fetchall()
        days = conn.execute(
            "SELECT COUNT(DISTINCT date(checked_at)) FROM benchmark_refreshes "
            "WHERE date(checked_at) >= date(?, '-14 day') AND routine = 0",
            (today.isoformat(),)).fetchone()[0] or 0
    except Exception:  # noqa: BLE001 - no history means no evidence
        return
    finally:
        if own is not None:
            own.close()

    # A SUCCESS ANYWHERE IN THE WINDOW means the feed still works and
    # this was an outage, not an entitlement that has gone.
    if any(str(o or "") == "updated" for o, _r in rows):
        return
    if days < FEED_REFUSED_DAYS_BEFORE_REBUILD:
        return

    state["benchmark_rebuild_day"] = today
    _log.warning(
        "The SPY series has been refused by its own feed on %d separate "
        "days (%s) with no successful refresh in between, so it can never "
        "update again as it stands. Rebuilding it on a feed these "
        "credentials can actually read. The stored series is discarded "
        "rather than spliced - a benchmark on two bases would make every "
        "comparison against it quietly wrong.", days, reason)
    try:
        from catalyst.data import benchmark
        from catalyst.dashboard.db import bars_path

        rebuilt = benchmark.rebuild_benchmark(
            bars_path(), creds.alpaca_key, creds.alpaca_secret)
    except Exception:  # noqa: BLE001 - reporting must never stop trading
        _log.exception("The SPY rebuild failed; the old series is unchanged.")
        return
    if conn is not None or db_file:
        own2 = None
        try:
            target = conn
            if target is None:
                import sqlite3 as _sq

                own2 = target = _sq.connect(db_file)
            record_benchmark_refresh(target, rebuilt)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if own2 is not None:
                own2.close()
    if rebuilt.skipped_reason in (None, "already_current"):
        state["benchmark_day"] = today
        _log.warning(
            "SPY series rebuilt on the %s feed: %d bar(s), through %s. The "
            "comparison is live again, and the page names the feed it is "
            "now on - IEX is one exchange's prints rather than the "
            "consolidated tape.",
            rebuilt.feed, rebuilt.written, rebuilt.last_day)
    else:
        _log.error(
            "The SPY rebuild did not recover the series either (%s). Raw "
            "upstream: %s", rebuilt.skipped_reason,
            (rebuilt.raw_response or "")[:500])


def _maybe_prune_logs(db_file: str, daily_state: dict) -> None:
    """Delete log lines past the retention window, once a day.

    Once a day rather than every cycle: it is a whole-table scan, and
    running it 96 times a day to delete the same nothing is wasted I/O
    on a small VPS.
    """
    import sqlite3 as _sq

    from catalyst.orchestrator.retention import LOG_RETENTION_DAYS, prune_logs

    today = datetime.now(timezone.utc).date().isoformat()
    if daily_state.get("logs_pruned_on") == today:
        return
    conn = _sq.connect(db_file)
    try:
        gone = prune_logs(conn)
    finally:
        conn.close()
    daily_state["logs_pruned_on"] = today
    # SAY IT EVEN WHEN IT IS ZERO. "pruned 0" on a young install and
    # "pruned 0" because the delete is broken look identical otherwise,
    # and this is the one job whose failure shows up months later as a
    # full disk.
    _log.info("Log retention: deleted %d line(s) older than %d days "
              "(tracebacks are kept regardless of age).",
              gone, LOG_RETENTION_DAYS)


def _maybe_forecast_budget(db_file: str, state: dict | None) -> None:
    """Say, once a day, when the month's budget is expected to run out.

    THE QUIETEST FAILURE IN THE SYSTEM. The bot spends its cap early,
    the governor correctly refuses every further call, and it researches
    nothing for the rest of the month. Nothing errors; the funnel just
    empties. On the shipped $5/month default and the owner's own
    measured rate of $1.93/day that happens on day THREE.

    The dashboard had a pace marker, which serves someone looking at it.
    This is for the owner who is not looking: it goes in the journal and
    the searchable log, where an unattended bot's owner actually reads.
    """
    from datetime import datetime as _dt

    state = {} if state is None else state
    today = _dt.now(timezone.utc).date()
    if state.get("forecast_day") == today:
        return
    state["forecast_day"] = today

    import sqlite3

    conn = sqlite3.connect(db_file)
    try:
        from decimal import Decimal

        from catalyst.cost.forecast import forecast
        from catalyst.cost.governor import scheduled_cap_cents
        from catalyst.cost.ledger import month_to_date_cents
        from catalyst.risk.adaptive_params import current_values
        from catalyst.setup.credentials import load_credentials

        try:
            settings = (load_credentials().settings or {})
        except Exception:  # noqa: BLE001 - unconfigured is not an error
            settings = {}
        owner_cap = _owner_cap_cents(settings.get("monthly_budget_usd"))
        share = current_values(conn).get("governor_profit_share",
                                         Decimal("0.10"))
        # The SAME function the governor enforces with, so the forecast
        # cannot quietly project against a different cap than the one
        # that will actually stop the bot.
        cap, _bound = scheduled_cap_cents(conn, share, today,
                                          owner_monthly_cap_cents=owner_cap)
        spent = month_to_date_cents("scheduled", conn, today)
        f = forecast(spent, cap, today)
        if f.will_stop_early:
            _log.warning("%s", f.sentence())
        else:
            _log.info("%s", f.sentence())
    except Exception:  # noqa: BLE001 - a forecast must never stop trading
        _log.exception("The budget forecast could not be computed. Trading "
                       "and the spending cap itself are unaffected.")
    finally:
        conn.close()


def _maybe_adapt(db_file: str, state: dict | None) -> None:
    """Run the adaptation loop once a day.

    IT HAD NEVER RUN. propose_adjustment, apply, maybe_auto_revert and
    conviction_floor_evidence were all built and tested, and nothing in
    the live path called any of them - so the refusal tracker scored
    refusals into evidence that was then discarded, and every threshold
    stayed frozen at the estimate it shipped with. The brief calls this
    "the single most important feedback loop in the system"; it was the
    one loop that was not connected.

    Once a day, not once a cycle: the inputs only change when a refusal
    is scored or a trade closes, so ninety-six passes a day would be
    ninety-five reads of the same numbers. It is also pure database
    work - no broker, no model, no cost.
    """
    from datetime import datetime as _dt

    state = {} if state is None else state
    today = _dt.now(timezone.utc).date()
    if state.get("adaptation_day") == today:
        return
    state["adaptation_day"] = today

    import sqlite3

    conn = sqlite3.connect(db_file)
    try:
        from catalyst.risk.adaptation import run_adaptation_pass

        report = run_adaptation_pass(conn, _dt.now(timezone.utc))
        for parameter, old, new in report.applied:
            _log.info("Parameter %s is now %s (was %s).", parameter, new, old)
        for err in report.errors:
            _log.warning("Adaptation problem: %s", err)
    except Exception:  # noqa: BLE001 - learning must never stop trading
        _log.exception(
            "The adaptation pass failed. Trading is unaffected and every "
            "parameter keeps the value it already had.")
    finally:
        conn.close()


def _dollars(cents) -> str:
    """Cents (a Decimal, or its string form) as money the owner reads."""
    from decimal import Decimal as _D
    try:
        return f"${_D(str(cents)) / 100:,.2f}"
    except Exception:  # noqa: BLE001 - a figure we cannot format is still a fact
        return f"{cents} cents"


def _announce_new_baseline(before, after) -> None:
    """Say, in plain English, that the comparison has just restarted.

    The owner is not a developer. "baseline source=account_changed" is
    an event code; what they need to read is what happened, what it
    means for the numbers on the page, and what was NOT lost. The
    benchmark row already carries a written reason - it is quoted here
    rather than paraphrased, so the log line and the page agree.

    The only identifier in any of this is the account FINGERPRINT: a
    truncated one-way hash of the broker's account id. No key, no
    secret, and nothing that can be turned back into either.
    """
    old_fp = before.account_fingerprint or "(none recorded yet)"
    if after.source == "first_run":
        _log.info(
            "THE SPY COMPARISON IS NOW SET AGAINST YOUR REAL ACCOUNT.\n"
            "  What happened: the bot read your broker account for the first "
            "time, and used what that account is actually worth instead of "
            "the placeholder figure it shows before it has ever connected.\n"
            "  What it means: from %s, \"how would the same money have done "
            "in the S&P 500 instead\" means buying %s of SPY on that day. "
            "Every performance figure on the page is measured against "
            "that.\n"
            "  Account fingerprint: %s. That is a short one-way hash of your "
            "account number - never a key or a password, and safe to quote "
            "if you ask anyone for help.\n"
            "  Recorded reason: %s",
            after.start_date, _dollars(after.capital_cents),
            after.account_fingerprint, after.reason)
        return
    _log.info(
        "YOUR BROKER ACCOUNT HAS CHANGED, SO THE S&P COMPARISON HAS STARTED "
        "AGAIN FROM TODAY.\n"
        "  What happened: the Alpaca details now in use belong to a "
        "different account from the one the comparison was set up for.\n"
        "  Account fingerprint: %s -> %s. Those are short one-way hashes of "
        "the account numbers - never keys or passwords, and safe to quote if "
        "you ask anyone for help.\n"
        "  What it means: from %s the bot is measured against buying %s of "
        "SPY, which is what the new account is actually worth. Measuring a "
        "new account's profit against the old account's starting money "
        "would be arithmetic on two different things, so the comparison "
        "starts again rather than carrying on.\n"
        "  What is NOT lost: the old baseline stays in the history with the "
        "reason it was replaced, and no trade, cost or log record is "
        "touched. The SPY price history is kept as it is - it is the same "
        "SPY either way; only the day the comparison counts from has "
        "moved.\n"
        "  Recorded reason: %s",
        old_fp, after.account_fingerprint, after.start_date,
        _dollars(after.capital_cents), after.reason)


def _sync_benchmark_baseline(conn, broker, daily_state: dict | None = None,
                             *, today=None) -> bool:
    """Notice a swapped broker account, and restart the SPY tracker.

    OWNER-ASKED: "when I change the Alpaca keys i want it to register
    there is a new account and restart the SPY tracker."

    Called once per cycle from `_run_one_cycle`, off its own confirmed
    `get_account()` read. `benchmark.sync_with_account` is idempotent
    and does nothing at all unless the account is genuinely different,
    so calling it every pass costs one Alpaca request (free, and one
    request against a 200/minute ceiling) and one indexed SELECT.

    WHY THE READ IS HERE AND NOT IN THE CYCLE. `cycle.build_portfolio_
    state` already has a confirmed account read, and reusing it would
    save the request - but that is risk code under human review, and a
    reporting feature is not a reason to touch it. The scheduler owns
    the broker object it built, so it can ask for itself.

    Never raises. A baseline is reporting: a broker that will not answer
    means the baseline stays exactly as it was, which is the honest
    outcome anyway - striking a new one from a read that failed would
    invent a comparison out of nothing.
    """
    from catalyst import benchmark

    try:
        account = broker.get_account()
    except Exception:  # noqa: BLE001 - any broker failure, same answer
        _log.debug(
            "The broker account could not be read this cycle, so the "
            "benchmark baseline was left exactly as it was.", exc_info=True)
        return False

    try:
        before = benchmark.current(conn)
        after, changed = benchmark.sync_with_account(conn, account, today)
    except Exception:  # noqa: BLE001 - reporting must never stop trading
        _log.exception(
            "The benchmark baseline could not be checked against the broker "
            "account. Trading is unaffected and the baseline is unchanged.")
        return False

    if not changed:
        return False

    _announce_new_baseline(before, after)
    # Bring the SPY bars up to date NOW rather than tomorrow: the new
    # baseline indexes from today, and a cache that last updated four
    # days ago has nothing in that window to index against.
    _maybe_refresh_benchmark(daily_state, force=True, conn=conn)
    return True


def _selected_research_model(creds) -> str:
    """The model the owner picked, or the built-in default.

    Never raises and never returns something unpriceable: a model the
    cost table cannot price would record an unpriced row and block ALL
    spend on the next authorize().
    """
    try:
        from catalyst.setup.models import selected_model

        return selected_model(getattr(creds, "settings", None))
    except Exception:  # noqa: BLE001
        from catalyst.research.boundary import DEFAULT_RESEARCH_MODEL

        return DEFAULT_RESEARCH_MODEL


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


#: A hunt is a DAILY act, not a per-cycle one. The feed does not change
#: materially between 15-minute cycles, so hunting every cycle would pay
#: to re-read the same digest ~26 times a day for the same nominations.
#: The marker lives in the loop's once-a-day state dict, beside the
#: benchmark refresh which works the same way.
def _hunt_due(daily_state: dict | None, owner_cap, as_of) -> bool:
    """Has today's hunt allowance been used?

    Returns False when the budget affords none - a nomination nobody can
    afford to research is worse than no nomination, so a small budget
    spends everything judging what the screen already found.
    """
    from catalyst.discovery.hunt import hunts_per_day

    allowed = hunts_per_day(owner_cap)
    if allowed <= 0:
        return False
    if daily_state is None:
        return True
    day = as_of.date().isoformat()
    if daily_state.get("hunt_day") != day:
        daily_state["hunt_day"] = day
        daily_state["hunt_count"] = 0
    if daily_state.get("hunt_count", 0) >= allowed:
        return False
    daily_state["hunt_count"] = daily_state.get("hunt_count", 0) + 1
    return True


def _record_origin(conn, candidates, origin: str, rationales, as_of) -> None:
    """Stamp where each candidate came from.

    Written BEFORE research, so a candidate that never gets researched
    still carries its provenance - otherwise the only hunted candidates
    on record would be the ones that got far enough to be interesting,
    which is the shape of every survivorship bias.

    Never raises: provenance is observability, and losing it must not
    cost a trade.
    """
    if not candidates:
        return
    # SECOND INSTANCE OF THE SAME DEFECT AS THE HUNT'S `Decimal`, found
    # by the name check written for that one: `sqlite3` was never
    # imported at module level, so the `except sqlite3.Error` clause
    # below raised NameError while HANDLING an error - turning a
    # function whose docstring promises it never raises into one that
    # takes discovery down the first time a write fails.
    import sqlite3

    try:
        conn.executemany(
            "INSERT OR IGNORE INTO candidate_origin "
            "(candidate_id, origin, rationale, nominated_at) "
            "VALUES (?,?,?,?)",
            [(c.id, origin, (rationales or {}).get(c.id), as_of.isoformat())
             for c in candidates])
        conn.commit()
    except sqlite3.Error:
        _log.debug("candidate origin could not be recorded", exc_info=True)


def _run_one_cycle(db_file: str, daily_state: dict | None = None):
    """Wire the live dependencies and run exactly one cycle. Thin by
    design: every piece here is constructed, none is decided.

    `daily_state` is the loop's once-a-day marker dictionary. It is
    passed in so a benchmark baseline change can force the SPY refresh
    it invalidates, rather than waiting for tomorrow's marker to lapse.
    """
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
                "Form 4: %d request(s), %d filing(s) and %d daily index(es) "
                "replayed from local storage. Cache hits cost nothing and "
                "are what keeps this inside sec.gov's fair-use limits.",
                got.requests_made, got.from_cache, got.index_days_from_cache)
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

        from catalyst.discovery.conjunctions import merge_with_form4

        # SECTOR FIRST, or the cluster cap treats every insider candidate
        # as one bet. Form 4 payloads carry no sector, so without this
        # they all key on "unknown" and max_correlated_cluster_pct caps
        # a biotech, a bank and a miner as a single wager. Measured in
        # backtest/harness.py: the cluster bound costs 30.5 points of
        # excess return, and does it by excluding the BEST weeks - the
        # ones where several clusters complete at once.
        #
        # Best-effort by construction: a company whose SIC cannot be
        # fetched keeps an unknown sector and clusters conservatively,
        # which is exactly today's behaviour. This can only ever loosen
        # the bound, never tighten it (discovery/correlation.py).
        try:
            import sqlite3 as _sq3

            from catalyst.data.sources.edgar_company import (
                enrich_form4_sectors,
            )

            _c = _sq3.connect(db_file)
            try:
                _enriched, _looked = enrich_form4_sectors(raw_events, conn=_c)
            finally:
                _c.close()
            # BOTH NUMBERS. "enriched 0 of 0" (all cached) and
            # "enriched 0 of 12" (every lookup failed) are different
            # facts and only one of them needs attention.
            _log.info("Sector enrichment: %d event(s) given a sector from "
                      "%d company lookup(s).", _enriched, _looked)
        except Exception:  # noqa: BLE001
            _log.exception(
                "Sector enrichment failed; the pass continues. Insider "
                "candidates will cluster as 'unknown', which is the "
                "conservative behaviour, not a broken one.")

        out = list(build_candidates(raw_events, as_of))
        try:
            extra, dropped = build_conjunction_candidates(raw_events, as_of)
            # One candidate per COMPANY per pass. See merge_with_form4.
            out, duplicates = merge_with_form4(out, extra)
            dropped.extend(duplicates)
            _log.info("Conjunctions: %d candidate(s) from cross-feed "
                      "agreement, %d considered and dropped.",
                      len(extra), len(dropped))
            for ticker, why in dropped[:10]:
                _log.debug("Conjunction dropped %s: %s", ticker, why)
        except Exception:  # noqa: BLE001 - never lose the graded strategy
            _log.exception(
                "Conjunction discovery failed; the Form 4 candidates from "
                "this pass are unaffected.")

        # CLAUDE'S OWN HUNT over the raw feed, once the mechanical
        # builders have had their say.
        #
        # OWNER-ASKED: "surely to make this properly agentic we want
        # claude go out and finds its own trades". The two builders
        # above only make Form 4 clusters and cross-feed conjunctions,
        # so most of what the feeds collect - EDGAR full-text hits, news
        # - is fetched, stored, paid for and then discarded. The hunt
        # reads that leftover and nominates from it.
        #
        # It runs LAST and merges, so it can never displace the graded
        # arm: the Form 4 candidates from this pass are already in `out`
        # before the hunt is asked, and a hunted duplicate of a ticker
        # the screen already found is dropped rather than the reverse.
        # A hunt that fails, is refused by the governor or returns
        # nothing leaves this list exactly as the screen built it.
        try:
            from catalyst.discovery.hunt import hunt

            if _hunt_due(daily_state, owner_cap, as_of):
                # Decimal is imported HERE because this module has no
                # top-level import of it - and this line raised
                # NameError on every hunt that came due, so Claude's
                # half of discovery had never once run. Found in the
                # owner's 2026-08-24 bundle: the single ERROR in a day
                # of 91,330 log lines, wrapped in a guard that said the
                # screened candidates were unaffected and did not say
                # that the hunt itself never happened.
                from decimal import Decimal

                from catalyst.research.boundary import CostContext
                from catalyst.risk.adaptive_params import current_values

                share = Decimal(str(current_values(conn)[
                    "governor_profit_share"]))
                known = {c.ticker for c in out}
                res = hunt(raw_events, as_of, transport,
                           CostContext(conn=conn,
                                       governor_profit_share=share,
                                       cycle_id=None, kind="scheduled",
                                       owner_monthly_cap_cents=owner_cap),
                           already_known=known)
                fresh = [c for c in res.candidates if c.ticker not in known]
                out.extend(fresh)
                _record_origin(conn, fresh, "hunt", res.rationales, as_of)
                if res.skipped_reason:
                    _log.info("Hunt did not run: %s", res.skipped_reason)
                else:
                    _log.info(
                        "Hunt: %d nomination(s), %d new candidate(s), "
                        "%d rejected against the evidence.",
                        res.nominations, len(fresh), len(res.rejected))
                    for ticker, why in res.rejected[:10]:
                        _log.info("Hunt rejected %s: %s", ticker, why)
        except Exception:  # noqa: BLE001 - never lose the graded strategy
            _log.exception(
                "The hunt failed; the mechanically screened candidates "
                "from this pass are unaffected.")

        # THE UNIVERSE RULE, APPLIED WHERE NOTHING CAN GO ROUND IT
        # (ESCALATION-4). Each builder already applies it, but this is
        # the one place every candidate from every source passes
        # through, so a source added later inherits the rule instead of
        # having to remember it. Excluded names are logged by name -
        # a symbol the bot will never trade is exactly the kind of
        # silent refusal the brief was written against.
        from catalyst.discovery.universe import excluded_reason

        kept = []
        for cand in out:
            why = excluded_reason(cand.ticker)
            if why is None:
                kept.append(cand)
            else:
                _log.info("Candidate %s excluded from the universe: %s",
                          cand.ticker, why)
        # BOTH SIDES STAMPED, or the comparison is worthless. Anything
        # the hunt did not already claim came from the mechanical
        # screen, and INSERT OR IGNORE leaves the hunt's own rows alone.
        _record_origin(conn, kept, "screen", {}, as_of)
        return kept

    conn = sqlite3.connect(db_file)
    try:
        # Whose account is this? Asked BEFORE the cycle trades, so the
        # answer is recorded even if the pass later fails, and so the
        # performance page is never quietly comparing a new account
        # against the old account's starting money.
        _sync_benchmark_baseline(conn, broker, daily_state)
        owner_cap = _owner_cap_cents((creds.settings or {}).get("monthly_budget_usd"))
        return run_cycle(conn, broker, transport, feed,
                         build_candidates_all, cluster,
                         account_mode=account_mode,
                         owner_monthly_cap_cents=owner_cap,
                         # The owner's dropdown choice. selected_model
                         # falls back to the built-in default whenever
                         # the setting is absent or names a model this
                         # bot cannot price - a stored value that has
                         # since stopped being priceable must not be
                         # able to halt the governor on start-up.
                         research_model=_selected_research_model(creds),
                         # ITS OWN DIRECTORY, not the benchmark's. The SPY
                         # cache pins a feed and adjustment basis in its
                         # metadata; writing candidate bars beside it would
                         # make that claim untrue for the directory.
                         bars_dir=os.environ.get("CATALYST_SIZING_BARS",
                                                 "data/bars_sizing"))
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
    # A pause written under the OLD five-cent rule keeps blocking every
    # call after the upgrade, because the new rule only governs new
    # reconciliations. Re-judge stale pauses against the rule now in
    # force, or the owner upgrades, sees "spending was blocked"
    # unchanged, and concludes the fix did nothing.
    try:
        from catalyst.cost.tracker import clear_pauses_that_no_longer_qualify
        _conn = init_db(path)
        cleared = clear_pauses_that_no_longer_qualify(_conn)
        _conn.close()
        if cleared:
            _log.info("cleared %d reconciliation pause(s) that no longer "
                      "qualify under the block-only-if-large rule", cleared)
    except Exception:  # noqa: BLE001 - never block startup on housekeeping
        _log.warning("could not re-judge stale reconciliation pauses",
                     exc_info=True)
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

        # Cleared BEFORE the pass, never after: credentials saved WHILE a
        # cycle is running must still shorten the sleep that follows it,
        # and clearing afterwards would throw that signal away.
        _wake.clear()
        # EACH DAILY JOB IN ITS OWN GUARD. These five are independent
        # reporting and housekeeping tasks that happened to share one
        # try block, which meant the FIRST one to raise silently
        # cancelled every job after it - for that pass and every pass
        # after, since they all run in the same order.
        #
        # Owner-reported: the SPY series stopped updating while
        # everything else carried on. A shared guard makes exactly that
        # shape of fault, and makes it invisible: the log blames "a
        # trading cycle", and the job that never ran is not mentioned.
        try:
            for _name, _job in (
                    ("nightly bill check", lambda: _maybe_reconcile_yesterday(path)),
                    ("benchmark refresh",
                     lambda: _maybe_refresh_benchmark(_daily_state, db_file=path)),
                    # Learn from yesterday BEFORE trading today, so any
                    # parameter that moved is the one this cycle uses.
                    ("parameter adaptation", lambda: _maybe_adapt(path, _daily_state)),
                    ("budget forecast", lambda: _maybe_forecast_budget(path, _daily_state)),
                    # Housekeeping LAST, and in the same
                    # name-it-then-carry-on wrapper as the rest: nothing
                    # here may cost a trading cycle.
                    ("log retention", lambda: _maybe_prune_logs(path, _daily_state)),
            ):
                try:
                    _job()
                except Exception:  # noqa: BLE001 - name it, then carry on
                    _log.exception(
                        "The %s failed this pass. Every other job still ran, "
                        "and this one is retried on the next cycle.", _name)
            report = _run_one_cycle(path, _daily_state)
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
        # Sleep until the next pass, OR until something wakes us: a
        # shutdown signal, or the owner saving new credentials on the
        # setup page. Waiting the full quarter of an hour after a key
        # swap is how "it did not notice my new account" happens - it
        # had noticed, fourteen minutes later, with nothing on screen
        # in between.
        _wake.wait(cycle_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
