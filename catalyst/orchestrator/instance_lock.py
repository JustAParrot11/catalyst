"""One Catalyst per machine. Deliberately tiny and offline.

Owner bug report (2026-08-10): the scheduler logged that it could not
open the setup page, and carried on. The likeliest reason for that port
to be busy is that Catalyst was ALREADY RUNNING - and two schedulers
against one Alpaca account place duplicate orders and take double the
intended exposure. Nothing prevented that.

flock, not a pidfile: the kernel releases an flock when the holding
process dies, however it dies. A pidfile left behind by a killed bot
locks out its own replacement, which is the failure this guard would
otherwise introduce.

The guard fails OPEN on its own errors (an unwritable path, a platform
without flock): a lock that cannot be taken must never be the reason
the bot stops trading. It only ever refuses when another live process
demonstrably holds the lock.
"""

import errno
import os
from typing import IO

DEFAULT_LOCK_PATH = "/var/lib/catalyst/catalyst.lock"

_held: IO | None = None    # module-level: the fd must outlive acquire()
_held_path: str | None = None


def lock_path() -> str:
    return os.environ.get("CATALYST_LOCK", DEFAULT_LOCK_PATH)


def acquire(path: str | None = None) -> IO | None:
    """Take the single-instance lock.

    Returns the open file object on success (KEEP IT - closing it frees
    the lock), or None when another live process holds it.
    """
    global _held, _held_path
    target = path or lock_path()
    # Idempotent within one process: THIS process holding the lock is
    # not a second Catalyst. flock is per open-file-description, so a
    # fresh open() here would collide with our own held fd and make a
    # process refuse to start against itself.
    if _held is not None and _held_path == target:
        return _held
    try:
        import fcntl
    except ImportError:      # not Linux; fail open
        return _fail_open(target)
    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        handle = open(target, "a+")
    except OSError:
        return _fail_open(target)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        # ONLY "would block" means another process holds it (risk review
        # finding 2). ENOLCK (lock table full, NFS-backed /var/lib),
        # EINTR, EBADF are the GUARD failing, not a duplicate - reporting
        # those as "already running" would fail CLOSED in the one place
        # this module promises to fail open, and stop the bot for good.
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None      # genuinely held by another process
        return _fail_open(target)
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
    except OSError:
        pass                 # the lock is what matters, not the note
    _held = handle
    _held_path = target
    return handle


def _fail_open(target: str):
    """The guard could not run. Trading must not stop for that."""
    import logging
    logging.getLogger("catalyst.scheduler").warning(
        "Could not use the duplicate-instance lock at %s, so this copy "
        "cannot check whether another Catalyst is already running. It "
        "will carry on. If you ever see two sets of orders, stop every "
        "copy and start just one.", target)
    return _NULL_LOCK


class _NullLock:
    """Truthy stand-in so a failed guard reads as 'proceed'."""

    def close(self) -> None:
        pass


_NULL_LOCK = _NullLock()


def holder_pid(path: str | None = None) -> str:
    """Best-effort PID of the holder, for the log message only."""
    try:
        with open(path or lock_path()) as fh:
            return fh.read().strip() or "unknown"
    except OSError:
        return "unknown"
