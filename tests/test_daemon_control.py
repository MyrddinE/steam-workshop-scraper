"""DaemonController: the start/stop logic shared by the TUI and the web UI.

No test here spawns a real daemon: ``subprocess.Popen`` is faked and liveness
probes ("is this PID alive?") are answered by a fake ``os.kill``.
"""

import os
import signal

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


def test_stop_sends_sigterm_and_removes_pid_file(tmp_path, monkeypatch):
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("4321")
    controller = DaemonController(pid_file=pid_file)
    kill = FakeKill(alive_probes=1)
    monkeypatch.setattr(daemon_control.os, "kill", kill)
    monkeypatch.setattr(daemon_control.platform, "system", lambda: "Linux")

    changed, message = controller.stop()

    assert changed is True
    assert (4321, signal.SIGTERM) in kill.calls
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


def test_stop_escalates_to_sigkill_after_timeout(tmp_path, monkeypatch):
    pid_file = _pid_file(tmp_path)
    with open(pid_file, "w") as f:
        f.write("9999")
    controller = DaemonController(pid_file=pid_file)
    kill = FakeKill(alive_probes=1000)
    monkeypatch.setattr(daemon_control.os, "kill", kill)
    monkeypatch.setattr(daemon_control.platform, "system", lambda: "Linux")
    # Zero wait so the escalation path runs without a real 15-second sleep.
    monkeypatch.setattr(daemon_control, "STOP_TIMEOUT_SECONDS", 0)

    changed, _ = controller.stop()

    assert changed is True
    assert (9999, signal.SIGKILL) in kill.calls


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

    log.write_text("one\npartial\n")
    second = controller.tail_log(first["offset"])
    assert second["lines"] == ["partial"]
    assert second["reset"] is False


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
