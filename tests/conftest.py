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
    os.environ["CATALYST_LOCK"] = os.path.join(sandbox, "catalyst.lock")
    os.environ.setdefault("CATALYST_DB", os.path.join(sandbox, "catalyst.db"))
    os.environ.setdefault("CATALYST_CREDENTIALS",
                          os.path.join(sandbox, "credentials.json"))


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
