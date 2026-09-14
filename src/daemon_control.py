"""Start, stop, and inspect the background daemon process.

Both the TUI's daemon manager and the embedded web UI drive the daemon through
this class. The handshake is the PID file: ``src.daemon_runner`` writes it
relative to its own working directory, and the daemon treats its disappearance
as the shutdown signal, so the path stays relative here too.
"""

import logging
import os
import platform
import signal
import subprocess
import sys
import time

# How long a graceful stop waits before escalating to a forced kill.
STOP_TIMEOUT_SECONDS = 15.0


class DaemonController:
    """Owns the detached daemon's process handle and PID-file protocol.

    An instance may be shared by the TUI and the web server running inside it:
    ``proc`` is the one Popen handle either UI can fall back on for liveness
    when the PID file has not appeared yet.
    """

    def __init__(self, config_path: str = "config.yaml", pid_file: str = ".daemon.pid",
                 proc=None, config: dict | None = None):
        self.config_path = config_path
        self.pid_file = pid_file
        self._proc = proc
        self._config = config

    @property
    def proc(self):
        """The Popen handle when this process started the daemon, else None."""
        return self._proc

    @property
    def config(self) -> dict:
        # Callers that already hold the parsed config pass it in; standalone
        # users read it from disk lazily, and a missing file only costs us the
        # log path, not process control.
        if self._config is None:
            try:
                from src.config import load_config
                self._config = load_config(self.config_path) or {}
            except Exception:
                self._config = {}
        return self._config

    def log_file(self) -> str | None:
        logging_config = self.config.get("logging") or {}
        return logging_config.get("file") or None

    def read_pid(self) -> int | None:
        try:
            with open(self.pid_file) as f:
                return int(f.read().strip())
        # Missing, empty, or non-numeric PID file all mean "no known PID".
        except Exception:
            return None

    def is_running(self) -> bool:
        if self._proc and self._proc.poll() is None:
            return True
        pid = self.read_pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            # Signal 0 cannot probe a detached Windows process reliably, so a
            # live PID file is taken at face value there.
            return sys.platform == 'win32'

    def status(self) -> dict:
        if self.is_running():
            pid = self.read_pid() or (self._proc.pid if self._proc else None)
            return {"running": True, "pid": pid}
        if self._proc is not None:
            self._proc = None
        return {"running": False, "pid": None}

    def start(self) -> tuple[bool, str]:
        if self.is_running():
            pid = self.read_pid() or (self._proc.pid if self._proc else None)
            return False, f"Already running (PID: {pid})"
        try:
            kwargs = {}
            if sys.platform == 'win32':
                kwargs["creationflags"] = subprocess.DETACHED_PROCESS
            else:
                # Close stdio so the daemon outlives the process that launched it.
                kwargs["stdout"] = subprocess.DEVNULL
                kwargs["stderr"] = subprocess.DEVNULL
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "src.daemon_runner", self.config_path, "--daemon"],
                **kwargs,
            )
            return True, f"Daemon started (PID: {self._proc.pid})"
        except Exception as exc:
            logging.warning("Failed to start daemon: %s", exc)
            return False, f"Failed to start daemon: {exc}"

    def stop(self) -> tuple[bool, str]:
        pid = self.read_pid()

        if not self.is_running():
            if self._proc is not None:
                self._proc = None
            return False, "Daemon not running"

        # Graceful shutdown: signal the daemon first, then delete the PID file
        # as a fallback the daemon also observes.
        if platform.system() == 'Windows':
            try:
                os.remove(self.pid_file)
            # Best-effort stop signal; an absent PID file is the goal, and the
            # graceful-wait/force-kill path below follows regardless.
            except OSError:
                pass
        else:
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                # Best-effort graceful signal; if the process is already gone the
                # wait loop observes it, and SIGKILL is the fallback.
                except OSError:
                    pass
            try:
                os.remove(self.pid_file)
            # Best-effort fallback after SIGTERM; removing an already-absent PID
            # file is a no-op success.
            except OSError:
                pass

        deadline = time.time() + STOP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if self._proc and self._proc.poll() is not None:
                self._proc = None
                return True, "Daemon stopped"
            if pid:
                try:
                    os.kill(pid, 0)
                except OSError:
                    self._proc = None
                    return True, "Daemon stopped"
            time.sleep(0.5)

        # Timeout: escalate to a forced kill.
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                # Last-resort force kill on our own Popen handle; nothing further
                # can be attempted in-process and the handle is cleared below.
                except Exception:
                    pass
        elif pid and platform.system() == 'Windows':
            try:
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(handle, 0)
                    ctypes.windll.kernel32.CloseHandle(handle)
            # Best-effort Windows force-kill of a PID this process does not own;
            # no further in-process remedy exists.
            except Exception:
                pass
        elif pid:
            try:
                os.kill(pid, signal.SIGKILL)
            # Final Unix escalation; if SIGKILL is refused there is nothing else
            # the controller can do for this PID.
            except Exception:
                pass

        self._proc = None
        return True, "Daemon stopped"

    def restart(self) -> tuple[bool, str]:
        self.stop()
        return self.start()

    def tail_log(self, since: int = 0) -> dict:
        """Return log lines written after the caller's byte offset.

        Reads in binary so ``since`` and the returned ``offset`` are real byte
        positions, and stops at the last newline so a half-written line is
        returned once rather than duplicated on the next poll.
        """
        log_file = self.log_file()
        if not log_file:
            return {"lines": [], "offset": since, "reset": False}
        try:
            size = os.path.getsize(log_file)
        # Missing or unreadable log is not an error for a poller.
        except OSError:
            return {"lines": [], "offset": since, "reset": False}

        reset = False
        start = max(0, since)
        if size < start:
            # The file was rotated or truncated; restart from the top.
            reset = True
            start = 0

        try:
            with open(log_file, "rb") as f:
                f.seek(start)
                data = f.read()
        # Unreadable between stat and open; report nothing and let the caller retry.
        except OSError:
            return {"lines": [], "offset": since, "reset": False}

        consumed = data.rfind(b"\n")
        if consumed == -1:
            return {"lines": [], "offset": start, "reset": reset}
        lines = data[:consumed].decode("utf-8", errors="replace").splitlines()
        return {"lines": lines, "offset": start + consumed + 1, "reset": reset}
