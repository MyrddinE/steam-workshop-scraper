"""DaemonController: the start/stop logic shared by the TUI and the web UI.

No test here spawns a real daemon: ``subprocess.Popen`` is faked and liveness
probes ("is this PID alive?") are answered by a fake ``os.kill``.
"""

import os
import signal
import time

import pytest

import src.daemon_control as daemon_control
from src.daemon_control import DaemonController


class FakeProc:
    """Minimal Popen stand-in; ``alive`` flips when it is signalled."""

    def __init__(self, pid=4242, alive=True):
        self.pid = pid
        self.alive = alive
        self.terminated = False
        self.killed = False
        self.waits = []

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.alive = False
        return 0


class FakeKill:
    """Records signals and lets the first ``alive_probes`` signal-0 probes pass."""

    def __init__(self, alive_probes=1):
        self.calls = []
        self.alive_probes = alive_probes
        self._probes = 0

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))
        if sig == 0:
            self._probes += 1
            if self._probes > self.alive_probes:
                raise ProcessLookupError(pid)


def _pid_file(tmp_path, name=".daemon.pid"):
    return str(tmp_path / name)


def test_read_pid_handles_missing_empty_and_garbage(tmp_path):
    pid_file = _pid_file(tmp_path)
    controller = DaemonController(pid_file=pid_file)
    assert controller.read_pid() is None

    with open(pid_file, "w") as f:
        f.write("")
    assert controller.read_pid() is None

    with open(pid_file, "w") as f:
        f.write("not-a-pid")
    assert controller.read_pid() is None

    with open(pid_file, "w") as f:
        f.write(" 12345 \n")
    assert controller.read_pid() == 12345


def test_is_running_false_without_pid_file_or_proc(tmp_path):
    controller = DaemonController(pid_file=_pid_file(tmp_path))
    assert controller.is_running() is False


def test_is_running_true_from_live_pid_file(tmp_path, monkeypatch):
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("321")
    controller = DaemonController(pid_file=pid_file)
    monkeypatch.setattr(daemon_control.os, "kill", lambda pid, sig: None)
    assert controller.is_running() is True


def test_is_running_windows_never_signals_the_process(tmp_path, monkeypatch):
    """Regression: a status poll used to be able to kill the daemon.

    On Windows ``os.kill(pid, 0)`` is not a probe -- any signal other than the
    two console events is passed to ``TerminateProcess``, so the old liveness
    check terminated the process it was asked about and then reported it as
    running. The web UI polls status every two seconds, which would have made
    that fatal rather than merely wrong.
    """
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("321")
    controller = DaemonController(pid_file=pid_file)

    def forbidden(pid, sig):
        raise AssertionError(f"os.kill({pid}, {sig}) must not be used as a Windows probe")

    monkeypatch.setattr(daemon_control.os, "kill", forbidden)
    monkeypatch.setattr(daemon_control.sys, "platform", "win32")
    monkeypatch.setattr(daemon_control, "_windows_pid_alive", lambda pid: True)
    assert controller.is_running() is True

    monkeypatch.setattr(daemon_control, "_windows_pid_alive", lambda pid: False)
    assert controller.is_running() is False


def test_is_running_trusts_the_pid_file_when_windows_will_not_say(tmp_path, monkeypatch):
    """Access denied is not evidence the process is gone."""
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("321")
    controller = DaemonController(pid_file=pid_file)
    monkeypatch.setattr(daemon_control.sys, "platform", "win32")
    monkeypatch.setattr(daemon_control, "_windows_pid_alive", lambda pid: None)
    assert controller.is_running() is True


def test_is_running_on_unix_still_uses_the_signal_probe(tmp_path, monkeypatch):
    """The Unix path is unchanged: signal 0 is a real probe there."""
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("321")
    controller = DaemonController(pid_file=pid_file)
    monkeypatch.setattr(daemon_control.sys, "platform", "linux")
    monkeypatch.setattr(daemon_control.os, "kill", lambda pid, sig: None)
    assert controller.is_running() is True

    def dead(pid, sig):
        raise OSError("no such process")

    monkeypatch.setattr(daemon_control.os, "kill", dead)
    assert controller.is_running() is False


class FakeKernel32:
    """Stand-in for kernel32, so the Windows probe is testable off Windows."""

    def __init__(self, handle=1234, wait_result=None, last_error=0):
        self._handle = handle
        self._wait_result = wait_result
        self.last_error = last_error
        self.terminated = []
        self.closed = []
        self.opened = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return self._handle

    def WaitForSingleObject(self, handle, timeout):
        return self._wait_result

    def TerminateProcess(self, handle, code):
        self.terminated.append((handle, code))
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def GetLastError(self):
        return self.last_error


def test_windows_probe_reports_a_running_process(monkeypatch):
    fake = FakeKernel32(wait_result=daemon_control._WAIT_TIMEOUT)
    monkeypatch.setattr(daemon_control, "_kernel32", lambda: fake)
    assert daemon_control._windows_pid_alive(4321) is True
    assert fake.closed == [1234], "the process handle must always be closed"


def test_windows_probe_reports_an_exited_process(monkeypatch):
    fake = FakeKernel32(wait_result=0x00000000)  # WAIT_OBJECT_0: already signalled
    monkeypatch.setattr(daemon_control, "_kernel32", lambda: fake)
    assert daemon_control._windows_pid_alive(4321) is False
    assert fake.closed == [1234]


def test_windows_probe_treats_access_denied_as_alive(monkeypatch):
    fake = FakeKernel32(handle=0, last_error=daemon_control._ERROR_ACCESS_DENIED)
    monkeypatch.setattr(daemon_control, "_kernel32", lambda: fake)
    assert daemon_control._windows_pid_alive(4321) is True
    assert fake.closed == [], "there is no handle to close when OpenProcess failed"


def test_windows_probe_treats_other_errors_as_gone(monkeypatch):
    fake = FakeKernel32(handle=0, last_error=87)  # ERROR_INVALID_PARAMETER
    monkeypatch.setattr(daemon_control, "_kernel32", lambda: fake)
    assert daemon_control._windows_pid_alive(4321) is False


def test_is_running_uses_proc_handle_before_pid_file(tmp_path):
    proc = FakeProc(pid=99, alive=True)
    controller = DaemonController(pid_file=_pid_file(tmp_path), proc=proc)
    assert controller.is_running() is True

    dead = FakeProc(pid=99, alive=False)
    stopped = DaemonController(pid_file=_pid_file(tmp_path, ".other.pid"), proc=dead)
    assert stopped.is_running() is False


def test_status_reports_stopped_then_running(tmp_path, monkeypatch):
    pid_file = _pid_file(tmp_path)
    controller = DaemonController(pid_file=pid_file)
    assert controller.status() == {"running": False, "pid": None}

    with open(pid_file, "w") as f:
        f.write("77")
    monkeypatch.setattr(daemon_control.os, "kill", lambda pid, sig: None)
    assert controller.status() == {"running": True, "pid": 77}


def test_status_prefers_pid_file_and_clears_dead_proc(tmp_path):
    controller = DaemonController(pid_file=_pid_file(tmp_path), proc=FakeProc(pid=5, alive=False))
    assert controller.status() == {"running": False, "pid": None}
    assert controller.proc is None


def test_start_when_stopped_spawns_detached_daemon(tmp_path, monkeypatch):
    controller = DaemonController(config_path="cfg.yaml", pid_file=_pid_file(tmp_path))
    proc = FakeProc(pid=7777)
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(daemon_control.subprocess, "Popen", fake_popen)
    changed, message = controller.start()

    assert changed is True
    assert captured["argv"] == [
        daemon_control.sys.executable, "-m", "src.daemon_runner", "cfg.yaml", "--daemon",
    ]
    if daemon_control.sys.platform == "win32":
        assert captured["kwargs"]["creationflags"] == daemon_control.subprocess.DETACHED_PROCESS
    else:
        assert captured["kwargs"]["stdout"] == daemon_control.subprocess.DEVNULL
        assert captured["kwargs"]["stderr"] == daemon_control.subprocess.DEVNULL
    assert controller.proc is proc
    assert "7777" in message


def test_start_is_noop_when_already_running(tmp_path, monkeypatch):
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("8888")
    controller = DaemonController(pid_file=pid_file)
    monkeypatch.setattr(daemon_control.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(daemon_control.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not spawn while running"))

    changed, message = controller.start()
    assert changed is False
    assert "Already running" in message
    assert "8888" in message


def test_start_reports_spawn_failure(tmp_path, monkeypatch):
    controller = DaemonController(pid_file=_pid_file(tmp_path))

    def boom(*args, **kwargs):
        raise OSError("cannot exec")

    monkeypatch.setattr(daemon_control.subprocess, "Popen", boom)
    changed, message = controller.start()
    assert changed is False
    assert "Failed to start daemon" in message


def test_stop_removes_the_pid_file_without_signalling_a_pid_it_did_not_start(
        tmp_path, monkeypatch):
    """The PID file is the graceful channel for a daemon this instance did not launch.

    With no Popen handle there is no way to know the number in the file is the
    daemon's, so stop asks through the file and signals nothing.
    """
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("4321")
    controller = DaemonController(pid_file=pid_file)
    kill = FakeKill(alive_probes=1)
    monkeypatch.setattr(daemon_control.os, "kill", kill)
    monkeypatch.setattr(daemon_control.platform, "system", lambda: "Linux")

    changed, message = controller.stop()

    assert changed is True
    assert [sig for _pid, sig in kill.calls] == [0, 0], (
        "the file names a PID this controller did not start, so only liveness "
        f"probes are allowed: {kill.calls}")
    assert not os.path.exists(pid_file)
    assert controller.proc is None


def test_stop_is_idempotent_when_not_running(tmp_path, monkeypatch):
    controller = DaemonController(pid_file=_pid_file(tmp_path))
    kill = FakeKill()
    monkeypatch.setattr(daemon_control.os, "kill", kill)

    changed, message = controller.stop()
    assert changed is False
    assert kill.calls == []
    assert "not running" in message.lower()


def test_stop_reports_failure_instead_of_killing_a_pid_it_did_not_start(
        tmp_path, monkeypatch):
    """A PID that outlives the grace is not this controller's to force-kill.

    Regression: this used to SIGKILL whatever the file named and report the
    daemon stopped.
    """
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("9999")
    controller = DaemonController(pid_file=pid_file)
    kill = FakeKill(alive_probes=1000)
    monkeypatch.setattr(daemon_control.os, "kill", kill)
    monkeypatch.setattr(daemon_control.platform, "system", lambda: "Linux")
    # Zero wait so the escalation decision runs without a real 15-second sleep.
    monkeypatch.setattr(daemon_control, "STOP_TIMEOUT_SECONDS", 0)

    changed, message = controller.stop()

    assert changed is False
    assert "not started by this controller" in message
    assert not any(sig in (signal.SIGTERM, signal.SIGKILL)
                   for _pid, sig in kill.calls), (
        f"a PID from the file is not ours to signal: {kill.calls}")
    assert not os.path.exists(pid_file)


def test_stop_does_not_kill_the_process_named_by_a_wrong_pid_file(
        tmp_path, monkeypatch):
    """The exact hazard: ``.daemon.pid`` overwritten with an unrelated process.

    The controller holds no Popen handle for the daemon (it did not start it),
    so the file is its only evidence and the file can name anything. It must
    remove the file -- the protocol's own graceful channel -- and report that
    the daemon did not exit, instead of SIGTERM-ing the bystander and claiming
    success.
    """
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("31337")  # an unrelated, long-lived process
    controller = DaemonController(pid_file=pid_file)
    kill = FakeKill(alive_probes=1000)  # the named process stays alive
    monkeypatch.setattr(daemon_control.os, "kill", kill)
    monkeypatch.setattr(daemon_control.platform, "system", lambda: "Linux")
    monkeypatch.setattr(daemon_control, "STOP_TIMEOUT_SECONDS", 0)

    changed, message = controller.stop()

    assert changed is False
    assert "did not exit" in message
    assert all(sig == 0 for _pid, sig in kill.calls), (
        f"only a liveness probe may touch a PID from the file: {kill.calls}")
    assert not os.path.exists(pid_file), (
        "the file must still be removed so the real daemon notices the stop")


def test_stop_does_not_signal_when_its_popen_handle_is_the_exited_fork_parent(
        tmp_path, monkeypatch):
    """On Unix the ``--daemon`` double-fork leaves the Popen handle on a corpse.

    The real daemon is a grandchild whose PID only the file knows, so after the
    fork the controller owns no process it can signal. It must not fall back to
    the file -- that is the wrong-PID hazard -- and must report the truth.
    """
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("31337")
    dead_fork_parent = FakeProc(pid=111, alive=False)
    controller = DaemonController(pid_file=pid_file, proc=dead_fork_parent)
    kill = FakeKill(alive_probes=1000)
    monkeypatch.setattr(daemon_control.os, "kill", kill)
    monkeypatch.setattr(daemon_control.platform, "system", lambda: "Linux")
    monkeypatch.setattr(daemon_control, "STOP_TIMEOUT_SECONDS", 0)

    changed, message = controller.stop()

    assert changed is False
    assert "not started by this controller" in message
    assert dead_fork_parent.terminated is False, (
        "terminating the exited fork parent would not reach the daemon")
    assert all(sig == 0 for _pid, sig in kill.calls)


def test_stop_escalates_on_the_popen_handle(tmp_path, monkeypatch):
    proc = FakeProc(pid=1234, alive=True)
    controller = DaemonController(pid_file=_pid_file(tmp_path), proc=proc)
    monkeypatch.setattr(daemon_control, "STOP_TIMEOUT_SECONDS", 0)

    changed, _ = controller.stop()

    assert changed is True
    assert proc.terminated is True
    assert proc.waits == [3]
    assert controller.proc is None


def test_restart_stops_then_starts(tmp_path, monkeypatch):
    controller = DaemonController(config_path="cfg.yaml", pid_file=_pid_file(tmp_path))
    calls = []
    monkeypatch.setattr(controller, "stop",
                        lambda: calls.append("stop") or (True, "Daemon stopped"))
    proc = FakeProc(pid=5555)

    def fake_popen(argv, **kwargs):
        calls.append("start")
        return proc

    monkeypatch.setattr(daemon_control.subprocess, "Popen", fake_popen)

    changed, message = controller.restart()
    assert calls == ["stop", "start"]
    assert changed is True
    assert "5555" in message


def test_tail_log_returns_new_lines_only(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("one\ntwo\n")
    controller = DaemonController(config={"logging": {"file": str(log)}})

    first = controller.tail_log(0)
    assert first == {"lines": ["one", "two"], "offset": len("one\ntwo\n"), "reset": False}

    with open(log, "a") as f:
        f.write("three\n")
    second = controller.tail_log(first["offset"])
    assert second == {"lines": ["three"], "offset": len("one\ntwo\nthree\n"), "reset": False}


def test_tail_log_waits_for_a_complete_line(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("one\npart")
    controller = DaemonController(config={"logging": {"file": str(log)}})

    first = controller.tail_log(0)
    assert first["lines"] == ["one"]
    assert first["offset"] == len("one\n")

    # The line is still half-written: the offset must not move, so the partial
    # text is not returned a second time when it is finally complete.
    still_partial = controller.tail_log(first["offset"])
    assert still_partial["lines"] == []
    assert still_partial["offset"] == first["offset"]

    log.write_text("one\npartial\n")
    second = controller.tail_log(first["offset"])
    assert second["lines"] == ["partial"]
    assert second["reset"] is False


def test_tail_log_first_call_on_a_small_file_returns_everything(tmp_path):
    """A log that fits in the window is not truncated and is not a gap."""
    log = tmp_path / "daemon.log"
    log.write_text("one\ntwo\nthree\n")
    controller = DaemonController(config={"logging": {"file": str(log)}})

    result = controller.tail_log(0)

    assert result == {
        "lines": ["one", "two", "three"],
        "offset": log.stat().st_size,
        "reset": False,
    }


class _ReadRecorder:
    """Binary-file stand-in that records the arguments of every ``read`` call."""

    def __init__(self, handle, reads):
        self._handle = handle
        self._reads = reads

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def seek(self, *args):
        return self._handle.seek(*args)

    def read(self, *args):
        self._reads.append(args)
        return self._handle.read(*args)


def test_tail_log_first_call_reads_only_the_tail_of_a_large_file(tmp_path, monkeypatch):
    """Regression guard: the first call must never read the whole log.

    The old ``f.read()`` pulled hundreds of megabytes of production log through
    memory and JSON before the UI trimmed the display. This file is several
    megabytes, so a reintroduced unbounded read is caught two ways: the recorded
    ``read`` call has no bound, and the elapsed time blows past the poll budget.
    """
    log = tmp_path / "daemon.log"
    lines = ["first-line-must-not-appear"] + [f"filler-{i:06d}" for i in range(400_000)]
    log.write_text("\n".join(lines) + "\n")
    assert log.stat().st_size > 4 * 1024 * 1024

    reads = []
    real_open = open

    def recording_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if os.fspath(path) == str(log):
            return _ReadRecorder(handle, reads)
        return handle

    monkeypatch.setattr(daemon_control, "open", recording_open, raising=False)
    controller = DaemonController(config={"logging": {"file": str(log)}})

    started = time.monotonic()
    result = controller.tail_log(0)
    elapsed = time.monotonic() - started

    assert reads == [(daemon_control.TAIL_BYTES + 1,)], \
        "tail_log must issue one bounded read, never an unbounded f.read()"
    assert elapsed < 1.0, f"first call took {elapsed:.3f}s; the read is not bounded"
    assert result["reset"] is True
    assert len(result["lines"]) == daemon_control.TAIL_LINES
    assert lines[0] not in result["lines"], "the file's beginning was returned"
    assert result["lines"][-1] == lines[-1]
    assert result["offset"] == log.stat().st_size


def test_tail_log_resets_when_the_caller_fell_behind(tmp_path):
    log = tmp_path / "daemon.log"
    controller = DaemonController(config={"logging": {"file": str(log)}})
    log.write_text("head\n")
    first = controller.tail_log(0)
    assert first["offset"] == len("head\n")

    with open(log, "a") as f:
        for i in range(2000):
            f.write(f"line-{i:06d}\n")

    result = controller.tail_log(first["offset"], max_bytes=1024, max_lines=10)

    assert result["reset"] is True
    assert len(result["lines"]) == 10
    assert result["lines"][-1] == f"line-{1999:06d}"
    assert result["offset"] == log.stat().st_size


def test_tail_log_max_lines_keeps_the_most_recent(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("".join(f"line-{i}\n" for i in range(100)))
    controller = DaemonController(config={"logging": {"file": str(log)}})

    result = controller.tail_log(0, max_lines=5)

    assert result["lines"] == [f"line-{i}" for i in range(95, 100)]


def test_tail_log_resume_keeps_its_first_line(tmp_path):
    """A resume from a real offset lands on a line boundary, so it drops nothing."""
    log = tmp_path / "daemon.log"
    log.write_text("one\ntwo\n")
    controller = DaemonController(config={"logging": {"file": str(log)}})
    first = controller.tail_log(0)

    with open(log, "a") as f:
        f.write("three\nfour\n")

    result = controller.tail_log(first["offset"])

    assert result["reset"] is False
    assert result["lines"] == ["three", "four"]
    assert result["offset"] == log.stat().st_size


def test_tail_log_replaces_invalid_utf8(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_bytes(b"ok\n\xff\xfe\n")
    controller = DaemonController(config={"logging": {"file": str(log)}})

    result = controller.tail_log(0)

    assert result["lines"][0] == "ok"
    assert "\ufffd" in result["lines"][1]


def test_tail_log_resets_after_truncation(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("one\ntwo\n")
    controller = DaemonController(config={"logging": {"file": str(log)}})
    first = controller.tail_log(0)
    assert first["reset"] is False

    log.write_text("x\n")
    second = controller.tail_log(first["offset"])
    assert second["reset"] is True
    assert second["lines"] == ["x"]
    assert second["offset"] == len("x\n")


def test_tail_log_missing_file_returns_empty(tmp_path):
    controller = DaemonController(config={"logging": {"file": str(tmp_path / "nope.log")}})
    assert controller.tail_log(0) == {"lines": [], "offset": 0, "reset": False}
    assert controller.tail_log(42) == {"lines": [], "offset": 42, "reset": False}


def test_tail_log_without_configured_file_returns_empty(tmp_path):
    controller = DaemonController(config={})
    assert controller.log_file() is None
    assert controller.tail_log(7) == {"lines": [], "offset": 7, "reset": False}
