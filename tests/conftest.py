"""Test harness ground rules, enforced mechanically:

1. FULLY OFFLINE. Any attempt to open a network socket from a test
   fails that test immediately. A suite that calls live APIs on every
   install is a broken suite (data-engineer's brief; CLAUDE.md).
2. NO CREDENTIALS. Alpaca/Anthropic env vars are stripped for the whole
   session so a test cannot accidentally use or leak them.
3. NO PRODUCTION PATHS. Every default that points at /var/lib/catalyst
   or /etc/catalyst is redirected into a temp directory for the whole
   session.

Rule 3 exists because it was violated, on the owner's live server
(2026-08-10). upgrade.sh runs the suite as ROOT; a test called
scheduler.main() without overriding CATALYST_LOCK, so the run created
/var/lib/catalyst/catalyst.lock owned by root:root. The service user
then could not open it, the duplicate-instance guard failed open on
every start, and the owner's real duplicate Catalyst went undetected.
A test suite that writes to production paths can break the machine it
is meant to be proving healthy.
"""

import os
import socket
import tempfile

import pytest

_REAL_SOCKET = socket.socket


class _NetworkBlockedError(RuntimeError):
    pass


class _GuardedSocket(_REAL_SOCKET):
    def connect(self, address):
        raise _NetworkBlockedError(
            f"Test attempted a network connection to {address!r}. "
            "The suite is fully offline by contract - stub the source."
        )

    def connect_ex(self, address):
        raise _NetworkBlockedError(
            f"Test attempted a network connection to {address!r}. "
            "The suite is fully offline by contract - stub the source."
        )


def pytest_configure(config):
    # Block sockets for the entire test session, not per-test - imports
    # that fire requests at collection time are caught too.
    socket.socket = _GuardedSocket

    # Strip credentials so no test can read them, log them, or hit a
    # real account even if the socket guard were somehow bypassed.
    for var in list(os.environ):
        if var.startswith(("ALPACA", "APCA", "ANTHROPIC")):
            os.environ.pop(var)

    # Rule 3: no test may touch the installed system's own files, even
    # when run as root by upgrade.sh. Set for the whole session so a
    # test that forgets to override a path still cannot reach /var/lib.
    config._catalyst_tmp = tempfile.TemporaryDirectory(prefix="catalyst-tests-")
    sandbox = config._catalyst_tmp.name
    # ASSIGNMENT, NEVER setdefault. setdefault keeps whatever the
    # environment already holds, which on an installed machine is the
    # real /etc/catalyst/credentials.json - so the "fully offline" suite
    # read the owner's live credentials and its results depended on
    # which machine it ran on. Reported 2026-08-11 as a test that passed
    # here and failed on the server, and it is the same defect the bar
    # cache comment below records: a test whose result depends on the
    # machine is not a test. The stated rule directly above was already
    # right; setdefault silently exempted these two from it.
    os.environ["CATALYST_LOCK"] = os.path.join(sandbox, "catalyst.lock")
    os.environ["CATALYST_DB"] = os.path.join(sandbox, "catalyst.db")
    os.environ["CATALYST_CREDENTIALS"] = os.path.join(sandbox,
                                                      "credentials.json")
    # The bar cache too. data/ is gitignored, so whether it exists is a
    # property of the MACHINE, not of the code - and a test whose result
    # depends on that is not a test. One did: it passed on a dev box
    # that had run scripts/fetch_history.py and failed on the owner's
    # server, which failed the upgrade gate and rolled the release back.
    os.environ["CATALYST_BARS"] = os.path.join(sandbox, "bars")


def pytest_unconfigure(config):
    socket.socket = _REAL_SOCKET
    tmp = getattr(config, "_catalyst_tmp", None)
    if tmp is not None:
        tmp.cleanup()


@pytest.fixture
def tmp_db(tmp_path):
    """A fresh, isolated database per test - two tests sharing a scratch
    database produced a phantom 13.6% exposure once (test-writer's brief).
    """
    from catalyst.storage import init_db

    db_file = tmp_path / "test.db"
    conn = init_db(str(db_file))
    yield conn
    conn.close()
