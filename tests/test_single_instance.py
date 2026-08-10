"""Two Catalysts must never trade one account.

Owner bug report (2026-08-10): "catalyst.scheduler could not open the
setup page on 0.0.0.0". Reproduced: the port was already bound. The
message is a nuisance on its own - but the code then CARRIED ON
TRADING, and the most likely reason for that port to be taken is
another Catalyst already running. Two schedulers against one Alpaca
account means duplicated orders and double the intended exposure, with
that single log line as the only symptom.

So the port clash is the canary, not the defect. The defect is that
nothing stopped the second instance.

Sabotage log (house rule 4):
- lock released before the loop (acquire-then-close): caught by
  test_second_instance_refuses_to_start, which then acquired happily.
  Restored, green.
- lock acquired AFTER the first cycle: caught by
  test_lock_is_held_before_any_trading_can_happen. Restored, green.
- duplicate SERVICE exits instead of standing by (risk review finding
  1): initially NOT caught - the test read poll() immediately after the
  log line and raced the exiting process. Rewritten to wait(timeout=8)
  and expect TimeoutExpired; sabotage then caught. Restored, green.
- every flock errno treated as "held" (risk review finding 2): caught
  by test_only_would_block_counts_as_another_instance. Restored, green.
- _fail_open returning None, i.e. failing CLOSED (risk review finding
  5 - the previous fail-open test never ran that branch at all):
  caught by both fail-open tests. Restored, green.
"""

import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from catalyst.orchestrator import instance_lock as il


class TestInstanceLock:
    def test_second_instance_refuses_to_start(self, tmp_path):
        lock_file = str(tmp_path / "catalyst.lock")
        first = il.acquire(lock_file)
        assert first is not None
        # a second acquisition IN ANOTHER PROCESS must fail: flock is
        # per-open-file-description, so a same-process retry can succeed
        # and would prove nothing
        probe = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {os.getcwd()!r})
            from catalyst.orchestrator import instance_lock as il
            print("ACQUIRED" if il.acquire({lock_file!r}) else "REFUSED")
        """)
        out = subprocess.run([sys.executable, "-c", probe],
                             capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == "REFUSED", out.stderr

    def test_lock_frees_when_the_holder_dies(self, tmp_path):
        """No stale-lock trap: a killed bot must not lock its successor
        out forever. flock is released by the kernel on process exit -
        a pidfile would not be."""
        lock_file = str(tmp_path / "catalyst.lock")
        holder = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {os.getcwd()!r})
            from catalyst.orchestrator import instance_lock as il
            assert il.acquire({lock_file!r})
            print("HELD", flush=True)
            time.sleep(30)
        """)
        proc = subprocess.Popen([sys.executable, "-c", holder],
                                stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "HELD"
        assert il.acquire(lock_file) is None      # genuinely held
        proc.kill()
        proc.wait(timeout=30)
        assert il.acquire(lock_file) is not None  # and free once it dies

    def test_same_process_reacquiring_is_not_a_second_instance(self, tmp_path):
        """flock is per open-file-description, so a naive re-acquire in
        one process collides with its OWN held fd and the process
        refuses to start against itself. Found by the suite: two
        in-process scheduler.main() calls, the second stood down."""
        lock_file = str(tmp_path / "catalyst.lock")
        first = il.acquire(lock_file)
        assert first is not None
        assert il.acquire(lock_file) is first     # same lock, not a refusal

    def test_guard_failing_on_its_own_errors_does_not_block_trading(
            self, tmp_path, monkeypatch):
        """The guard must fail OPEN on its own errors - refusing to trade
        because the lock broke would be the guard causing the outage it
        exists to prevent.

        Risk review finding 5: the previous version of this test passed
        an unwritable-looking path, but acquire() creates missing parent
        directories, so it took a REAL lock and never ran the fail-open
        branch at all. It had to break flock itself."""
        import errno as _errno
        import fcntl

        def broken_flock(fd, flags):
            raise OSError(_errno.ENOLCK, "lock table full")

        monkeypatch.setattr(fcntl, "flock", broken_flock)
        monkeypatch.setattr(il, "_held", None)
        monkeypatch.setattr(il, "_held_path", None)
        assert il.acquire(str(tmp_path / "l.lock")) is not None

    def test_only_would_block_counts_as_another_instance(
            self, tmp_path, monkeypatch):
        """Risk review finding 2: every flock errno was read as 'another
        process holds it'. ENOLCK/EINTR are the GUARD failing and must
        fail open; only EWOULDBLOCK/EAGAIN is a real duplicate."""
        import errno as _errno
        import fcntl

        def flock_raising(err):
            def _f(fd, flags):
                raise OSError(err, "x")
            return _f

        for err in (_errno.ENOLCK, _errno.EINTR, _errno.EBADF):
            monkeypatch.setattr(fcntl, "flock", flock_raising(err))
            monkeypatch.setattr(il, "_held", None)
            monkeypatch.setattr(il, "_held_path", None)
            assert il.acquire(str(tmp_path / "l.lock")) is not None, \
                f"errno {err} must fail OPEN, not look like a duplicate"

        for err in (_errno.EWOULDBLOCK, _errno.EAGAIN):
            monkeypatch.setattr(fcntl, "flock", flock_raising(err))
            monkeypatch.setattr(il, "_held", None)
            monkeypatch.setattr(il, "_held_path", None)
            assert il.acquire(str(tmp_path / "l.lock")) is None, \
                f"errno {err} IS a duplicate and must refuse"


class TestSchedulerRefusesToDoubleTrade:
    def _run_scheduler(self, tmp_path, extra_env=None):
        env = dict(os.environ)
        env.update({
            "CATALYST_DB": str(tmp_path / "c.db"),
            "CATALYST_CREDENTIALS": str(tmp_path / "creds.json"),
            "CATALYST_LOCK": str(tmp_path / "catalyst.lock"),
            "CATALYST_PORT": "8791",
            "CATALYST_CYCLE_SECONDS": "1",
        })
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, "-m", "catalyst.orchestrator.scheduler", "--once"],
            capture_output=True, text=True, timeout=120, env=env, cwd=os.getcwd())

    def test_lock_is_held_before_any_trading_can_happen(self, tmp_path):
        """A second scheduler must exit BEFORE a cycle runs - not merely
        log about the web page and carry on. Exit code 0, because a
        duplicate that stops cleanly is correct behaviour, not a crash
        for systemd to restart in a loop."""
        lock_file = str(tmp_path / "catalyst.lock")
        holder = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {os.getcwd()!r})
            from catalyst.orchestrator import instance_lock as il
            assert il.acquire({lock_file!r})
            print("HELD", flush=True)
            time.sleep(60)
        """)
        proc = subprocess.Popen([sys.executable, "-c", holder],
                                stdout=subprocess.PIPE, text=True)
        try:
            assert proc.stdout.readline().strip() == "HELD"
            out = self._run_scheduler(tmp_path)
            combined = out.stdout + out.stderr
            assert out.returncode == 0, combined
            assert "already running" in combined.lower(), combined
            # and it must NOT have gone on to do a cycle
            assert "Database ready" in combined          # it did start up
            assert "Setup page listening" not in combined
        finally:
            proc.kill()
            proc.wait(timeout=30)

    def test_service_stands_by_instead_of_exiting_when_locked_out(
            self, tmp_path):
        """Risk review finding 1, the worst one: exiting 0 here would
        have systemd treat the duplicate as a deliberate stop and leave
        it down. When the OTHER copy then died, nothing would be running
        and nothing would restart it - an account with open positions,
        no re-placed DAY stops and dark kill switches, while systemctl
        reported success. The service copy must WAIT and take over."""
        lock_file = str(tmp_path / "catalyst.lock")
        holder = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {os.getcwd()!r})
            from catalyst.orchestrator import instance_lock as il
            assert il.acquire({lock_file!r})
            print("HELD", flush=True)
            time.sleep(60)
        """)
        proc = subprocess.Popen([sys.executable, "-c", holder],
                                stdout=subprocess.PIPE, text=True)
        env = dict(os.environ)
        env.update({
            "CATALYST_DB": str(tmp_path / "c.db"),
            "CATALYST_CREDENTIALS": str(tmp_path / "creds.json"),
            "CATALYST_LOCK": lock_file,
            "CATALYST_PORT": "8793",
            "CATALYST_CYCLE_SECONDS": "1",
        })
        # no --once: this is the SERVICE path
        svc = subprocess.Popen(
            [sys.executable, "-m", "catalyst.orchestrator.scheduler"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=env, cwd=os.getcwd())
        try:
            assert proc.stdout.readline().strip() == "HELD"
            deadline = time.time() + 60
            saw_standby = False
            while time.time() < deadline:
                line = svc.stdout.readline()
                if not line:
                    break
                if "will NOT trade" in line:
                    saw_standby = True
                    break
            assert saw_standby, "service copy did not stand by"
            # and crucially it is STILL ALIVE, not exited. wait() with a
            # timeout, not poll(): poll() right after the log line races
            # a process that IS exiting and reads as healthy (caught by
            # sabotage A - reverting to exit-on-duplicate passed).
            with pytest.raises(subprocess.TimeoutExpired):
                svc.wait(timeout=8)
        finally:
            proc.kill(); proc.wait(timeout=30)
            svc.kill(); svc.wait(timeout=30)

    def test_lock_stays_held_while_the_scheduler_runs(self, tmp_path):
        """Risk review finding 7: the lock survives only because of a
        module global holding the fd. If that is ever tidied away, GC
        closes it and the lock frees MID-RUN while trading continues."""
        env = dict(os.environ)
        lock_file = str(tmp_path / "catalyst.lock")
        env.update({
            "CATALYST_DB": str(tmp_path / "c.db"),
            "CATALYST_CREDENTIALS": str(tmp_path / "creds.json"),
            "CATALYST_LOCK": lock_file,
            "CATALYST_PORT": "8794",
            "CATALYST_CYCLE_SECONDS": "1",
        })
        svc = subprocess.Popen(
            [sys.executable, "-m", "catalyst.orchestrator.scheduler"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=env, cwd=os.getcwd())
        try:
            deadline = time.time() + 60
            started = False
            while time.time() < deadline:
                line = svc.stdout.readline()
                if not line:
                    break
                if "Setup page listening" in line or "waiting" in line.lower():
                    started = True
                    break
            assert started, "scheduler never reached its running state"
            time.sleep(2)             # well into the run, not at startup
            probe = textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {os.getcwd()!r})
                from catalyst.orchestrator import instance_lock as il
                print("ACQUIRED" if il.acquire({lock_file!r}) else "REFUSED")
            """)
            out = subprocess.run([sys.executable, "-c", probe],
                                 capture_output=True, text=True, timeout=60)
            assert out.stdout.strip() == "REFUSED", (
                "the lock was released while the scheduler was still running")
        finally:
            svc.kill(); svc.wait(timeout=30)

    def test_single_instance_still_runs_normally(self, tmp_path):
        """The guard must not break the ordinary case: one instance,
        no lock contention, starts and reaches its waiting-for-setup
        state (no credentials in this temp dir, so it does not trade)."""
        out = self._run_scheduler(tmp_path)
        combined = out.stdout + out.stderr
        assert out.returncode == 0, combined
        assert "already running" not in combined.lower()
        assert "Database ready" in combined


class TestPortClashIsExplained:
    """The owner's actual report. A busy port must produce a message
    that says what to DO, naming the command that finds the culprit -
    'the usual cause is another program' is a dead end for someone who
    is not a developer."""

    def test_message_names_the_diagnostic_command(self, tmp_path, caplog):
        import logging

        from catalyst.orchestrator.scheduler import start_setup_server
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            os.environ["CATALYST_BIND"] = "127.0.0.1"
            os.environ["CATALYST_PORT"] = str(port)
            os.environ["CATALYST_DB"] = str(tmp_path / "c.db")
            with caplog.at_level(logging.ERROR):
                assert start_setup_server() is None
            msg = caplog.text
            assert "already in use" in msg.lower() or "address" in msg.lower()
            assert "ss -ltnp" in msg          # the command to run
            assert str(port) in msg
        finally:
            s.close()
            os.environ.pop("CATALYST_BIND", None)
            os.environ.pop("CATALYST_PORT", None)


@pytest.mark.parametrize("name", ["acquire"])
def test_lock_module_is_offline_and_tiny(name):
    """This guard sits in front of all trading; it must stay boring."""
    assert hasattr(il, name)
    src = open("catalyst/orchestrator/instance_lock.py").read()
    for forbidden in ("requests", "httpx", "socket", "urllib"):
        assert forbidden not in src, f"lock module reaches for {forbidden}"
