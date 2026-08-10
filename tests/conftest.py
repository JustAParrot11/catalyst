"""Test harness ground rules, enforced mechanically:

1. FULLY OFFLINE. Any attempt to open a network socket from a test
   fails that test immediately. A suite that calls live APIs on every
   install is a broken suite (data-engineer's brief; CLAUDE.md).
2. NO CREDENTIALS. Alpaca/Anthropic env vars are stripped for the whole
   session so a test cannot accidentally use or leak them.
"""

import os
import socket

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


def pytest_unconfigure(config):
    socket.socket = _REAL_SOCKET


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
