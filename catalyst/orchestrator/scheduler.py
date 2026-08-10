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

import logging
import os
import signal
import sys
import threading
import time

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


def db_path() -> str:
    return os.environ.get("CATALYST_DB", DEFAULT_DB)


def start_setup_server() -> threading.Thread | None:
    """Serve the setup/first-run page in the background.

    Returns None (having logged why) rather than raising if the port is
    taken: a bot that refuses to trade because its web page could not
    bind is the wrong trade-off.
    """
    from catalyst.setup.first_run import DEFAULT_BIND, DEFAULT_PORT, SetupApp, make_server

    host = os.environ.get("CATALYST_BIND", DEFAULT_BIND)
    port = int(os.environ.get("CATALYST_PORT", DEFAULT_PORT))
    try:
        server = make_server(SetupApp(), host, port)
    except OSError as exc:
        _log.error(
            "Could not open the setup page on %s:%s (%s). Nothing else is affected, "
            "but the browser setup form will not answer until this is fixed - the "
            "usual cause is another program already using that port.",
            host, port, exc,
        )
        return None

    thread = threading.Thread(target=server.serve_forever, name="setup-http", daemon=True)
    thread.start()
    _log.info("Setup page listening on %s:%s", host, port)
    return thread


def _handle_signal(signum, _frame) -> None:
    _log.info("Received signal %s - shutting down cleanly.", signum)
    _stop.set()


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

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handle_signal)

    start_setup_server()

    cycle_seconds = int(os.environ.get("CATALYST_CYCLE_SECONDS", DEFAULT_CYCLE_SECONDS))
    once = "--once" in args
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
            from catalyst.orchestrator.cycle import run_cycle

            run_cycle()
        except NotImplementedError:
            # Stages 3-5 land the pipeline behind this call. Until then
            # the service stays up, serves the dashboard and says plainly
            # that it is not trading, rather than crash-looping.
            _log.warning(
                "The trading pipeline is not built in this version yet, so no cycle "
                "ran. The bot is otherwise healthy and its setup page is working."
            )
        except Exception:  # noqa: BLE001 - an unattended service keeps trying
            _log.exception("A trading cycle failed. The next one will still run.")

        if once:
            return 0
        _stop.wait(cycle_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
