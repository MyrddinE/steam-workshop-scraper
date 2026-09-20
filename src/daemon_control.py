"""Start, stop, and inspect the background daemon process.

Both the TUI's daemon manager and the embedded web UI drive the daemon through
this class. The handshake is the PID file: ``src.daemon_runner`` writes it
relative to its own working directory, and the daemon treats its disappearance
as the shutdown signal, so the path stays relative here too.

This module also owns the UI's startup gate, ``initialize_database_with_daemon_
stopped``: a pending schema migration rewrites tables, and the daemon is a
detached process that may still be writing to them, so the gate stops it first
and refuses to migrate if it will not stop.
"""

import logging
import os
import platform
import subprocess
import sys
import time

from src.database import (
    EXPECTED_VERSION,
    SchemaVersionError,
    initialize_database,
    newer_schema_error,
    read_schema_version,
)


# How long a graceful stop waits before escalating to a forced kill. It is
# derived from the daemon's documented worst case, not guessed:
#
#   the longest single blocking call the daemon's *main thread* can be inside
#   when the stop is requested          15 s
#     -- the SQLite busy timeout on every connection (src/database.py) and the
#        subscriptions page fetch (src/subscription_sync._fetch_page), both 15 s;
#        the Steam API calls on that path are 10 s (src/steam_api.py), and the
#        15 s workers (image download, web scrape, page discovery) run on their
#        own threads, so the largest single main-thread block is 15 s.
#   + the daemon's own join budget                5 s  (SHUTDOWN_BUDGET_SECONDS in src/daemon.py)
#   + margin                                      5 s  (the up-to-1 s PID-file
#        tick in Daemon._wait_for_work, the failure-capture flush, this
#        controller's own half-second poll and process teardown)
#   = 25 s
#
# The request/DB block and the daemon's join budget are therefore tied to this
# number: raising either without raising STOP_TIMEOUT_SECONDS means the
# controller force-kills a daemon that is still unwinding. The join budget is
# written out rather than imported so this module does not drag the daemon's
# whole import graph into the TUI and web processes; a test pins the two
# together.
_LONGEST_MAIN_THREAD_BLOCK_SECONDS = 15.0
_DAEMON_JOIN_BUDGET_SECONDS = 5.0
_SHUTDOWN_MARGIN_SECONDS = 5.0

STOP_TIMEOUT_SECONDS = (
    _LONGEST_MAIN_THREAD_BLOCK_SECONDS
    + _DAEMON_JOIN_BUDGET_SECONDS
    + _SHUTDOWN_MARGIN_SECONDS
)

# The log view is a preview, not an export. The production log runs to hundreds
# of megabytes, so every read is bounded and the caller is told when its view has
# a gap rather than being handed the difference.
TAIL_BYTES = 64 * 1024
TAIL_LINES = 500

# Constants for the Windows liveness probe.
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x00000102
_ERROR_ACCESS_DENIED = 5


def _kernel32():
    """Windows kernel32 with prototypes set.

    Isolated in a function so the probe below can be exercised on a machine that
    is not Windows, and so the handle-returning calls get a real return type:
    ctypes assumes ``c_int`` otherwise, which truncates a 64-bit HANDLE.
    """
    import ctypes
    from ctypes import wintypes

    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetLastError.restype = wintypes.DWORD
    return k


def _windows_pid_alive(pid: int) -> bool | None:
    """Whether a PID is alive on Windows, or None when Windows will not say.

    Deliberately not ``os.kill(pid, 0)``. On Windows any signal other than the
    two console events is handed to ``TerminateProcess``, so a signal-0 "probe"
    *kills* the process it was asked about and then reports success -- turning a
    status poll into a daemon shutdown. A process handle stays signalled once
    its process has exited, so a zero-timeout wait answers the question without
    touching it.
    """
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        # Access denied means the process exists but is not ours to inspect;
        # anything else means there is no process with that id.
        return True if kernel32.GetLastError() == _ERROR_ACCESS_DENIED else False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool | None:
    """Whether a PID is alive, or None when the platform cannot determine it."""
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
        alive = _pid_alive(pid)
        if alive is None:
            # The platform will not say. A PID file is the only evidence there
            # is, so a live one is taken at face value.
            return True
        return alive

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

    def _owned_pid(self) -> int | None:
        """The PID of a process this controller started, or None.

        The Popen handle is the only evidence that a PID is ours to signal. A
        PID read out of the file may be stale, hand-edited, or belong to a
        different process altogether, so it is never treated as authorisation to
        send a signal.
        """
        if self._proc is None or self._proc.poll() is not None:
            return None
        return self._proc.pid

    def stop(self) -> tuple[bool, str]:
        pid = self.read_pid()
        owned_pid = self._owned_pid()

        if not self.is_running():
            if self._proc is not None:
                self._proc = None
            return False, "Daemon not running"

        # Graceful shutdown. The PID file removal is the channel that reaches a
        # daemon regardless of who launched it; the process handle is used only
        # when the controller actually started the process and therefore knows
        # the signal is going to the daemon. On Windows a terminate is a hard
        # kill, so the file is removed first and the handle is held for the
        # escalation below.
        if owned_pid is not None and platform.system() != 'Windows':
            try:
                self._proc.terminate()
            # Best-effort graceful signal; if the process is already gone the
            # wait loop observes it.
            except Exception:
                pass
        try:
            os.remove(self.pid_file)
        # Best-effort fallback; removing an already-absent PID file is a no-op
        # success.
        except OSError:
            pass

        deadline = time.time() + STOP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if owned_pid is not None and self._proc is not None \
                    and self._proc.poll() is not None:
                self._proc = None
                return True, "Daemon stopped"
            if pid:
                if _pid_alive(pid) is False:
                    self._proc = None
                    return True, "Daemon stopped"
            time.sleep(0.5)

        # Timeout. Force-kill only the process this controller started. The PID
        # file may name a process it did not launch, and signalling that would
        # take down something that is not the daemon while reporting success.
        if owned_pid is not None and self._proc is not None:
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
            self._proc = None
            return True, "Daemon stopped"

        self._proc = None
        return False, (
            f"Daemon did not exit; PID {pid} was not started by this controller, "
            "so it was left alone. The PID file was removed.")

    def restart(self) -> tuple[bool, str]:
        self.stop()
        return self.start()

    def tail_log(self, since_offset: int = 0, max_bytes: int = TAIL_BYTES,
                 max_lines: int = TAIL_LINES) -> dict:
        """Return a bounded preview of the log, and the offset to resume from.

        Reads in binary so ``since_offset`` and the returned ``offset`` are real byte
        positions, and stops at the last newline so a half-written line is
        returned once rather than duplicated on the next poll.

        No read is ever larger than ``max_bytes``. On a first call (``since_offset`` at
        or below zero) that means the *tail* of the file rather than the whole of
        it: this is a preview pane, and the production log is hundreds of
        megabytes, so returning all of it would cost more memory than the process
        has to spare and take longer than the poll interval. A caller that has
        fallen further behind than ``max_bytes`` is likewise given the tail and
        told ``reset``, so it knows its view has a gap in it rather than silently
        believing it saw everything.
        """
        floor = max(0, since_offset)
        log_file = self.log_file()
        if not log_file:
            return {"lines": [], "offset": floor, "reset": False}
        try:
            size = os.path.getsize(log_file)
        # Missing or unreadable log is not an error for a poller.
        except OSError:
            return {"lines": [], "offset": floor, "reset": False}

        if since_offset <= 0:
            # First look: a preview of the end, not the whole archive.
            start = max(0, size - max_bytes)
            reset = start > 0
        elif size < since_offset:
            # Rotated or truncated under us.
            start = max(0, size - max_bytes)
            reset = True
        elif size - since_offset > max_bytes:
            # The caller fell further behind than we are willing to send.
            start = max(0, size - max_bytes)
            reset = True
        else:
            start = since_offset
            reset = False

        try:
            with open(log_file, "rb") as f:
                f.seek(start)
                data = f.read(max_bytes + 1)
        # Unreadable between stat and open; report nothing and let the caller retry.
        except OSError:
            return {"lines": [], "offset": floor, "reset": False}

        last_newline = data.rfind(b"\n")
        if last_newline == -1:
            # No complete line in what was read; leave the offset where it was.
            return {"lines": [], "offset": start, "reset": reset}

        lines = data[:last_newline].decode("utf-8", errors="replace").splitlines()
        if start > 0 and start != since_offset:
            # We jumped backwards to the tail, so the first line is the tail end
            # of a line whose beginning we never read. Resuming from a real
            # offset always lands on a line boundary, so it is only dropped here.
            lines = lines[1:]
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return {"lines": lines, "offset": start + last_newline + 1, "reset": reset}


class DaemonStillRunningError(RuntimeError):
    """A pending migration needed the daemon stopped, and the stop did not succeed.

    Not a ``SchemaVersionError``: the database is fine, the obstacle is the live
    writer. The entry points report the sentence and refuse to start rather than
    migrating under it, because that is the defect the gate exists to prevent.
    """


class SchemaMigrationFailedError(RuntimeError):
    """A migration raised after the daemon had been stopped for it.

    The daemon is deliberately left down: restarting it onto a failed or
    half-applied migration would be worse than an honest outage, and the
    underlying error does not mention the process that was taken down for it.
    """


def initialize_database_with_daemon_stopped(db_path: str,
                                            controller: DaemonController) -> None:
    """Bring ``db_path`` to ``EXPECTED_VERSION`` with the daemon stopped first.

    The daemon is detached -- closing the TUI leaves it running -- and it writes
    to this database. A migration is DDL, and migration 34->35's ``DROP COLUMN``
    rewrites ``workshop_items``: 283 s in production measured from the live log.
    During that rewrite a live daemon would be writing to a table SQLite is
    rebuilding, and afterwards it would keep running old code against the new
    schema. So a *pending* migration is applied only with the daemon stopped,
    and only when the stop actually succeeded.

    The name is the operation and its precondition -- initialise the database
    with the daemon stopped -- rather than the controller it drives, so a caller
    reads what it gets rather than which object does the work.

    The order is:

    1. read the recorded ``user_version`` read-only (see
       :func:`src.database.read_schema_version`) and refuse a database *newer*
       than this build with :func:`src.database.newer_schema_error` -- before the
       daemon is touched, so a refused start does not take the service down on
       its way out;
    2. when the recorded version already equals ``EXPECTED_VERSION`` nothing is
       pending: call :func:`initialize_database` and return without touching the
       daemon, which is the common UI relaunch and must stay free;
    3. when a migration is pending and the daemon is running, log the migration
       as the reason and stop it through ``controller.stop()``;
    4. when that stop did not succeed, raise :class:`DaemonStillRunningError`
       *without* migrating -- this is a gate, not best-effort;
    5. migrate, then restart the daemon if it had been running and log that;
    6. when the migration raised, do not restart, and raise
       :class:`SchemaMigrationFailedError` saying the daemon was stopped and has
       not been restarted.

    ``controller`` is an argument rather than something this function builds, so
    a test can hand in a fake; the entry points pass the single controller the
    process shares with its UI (the TUI's daemon screen and the web runner's
    daemon panel both drive it).
    """
    recorded = read_schema_version(db_path)
    if recorded > EXPECTED_VERSION:
        raise newer_schema_error(db_path, recorded)

    if recorded == EXPECTED_VERSION:
        # Still called: this is where the journal mode is established and the
        # indexes ensured. It will not apply a migration, so a running daemon is
        # irrelevant to it and is left alone.
        initialize_database(db_path)
        return

    was_running = controller.is_running()
    if was_running:
        logging.info(
            "Schema migration pending for %s (version %s -> %s): stopping the "
            "running daemon so the migration does not run under a live writer.",
            db_path, recorded, EXPECTED_VERSION)
        stopped, message = controller.stop()
        if not stopped:
            # A failed stop can also mean the daemon exited between the check and
            # the request, but refuse either way: acting on a stop the
            # controller reported as failed is the defect, not the exception.
            raise DaemonStillRunningError(
                f"The daemon is still running, so the schema migration pending "
                f"for {db_path} (version {recorded} -> {EXPECTED_VERSION}) was "
                f"not attempted: {message}. Stop the daemon and start again.")

    try:
        initialize_database(db_path)
    except SchemaVersionError:
        # Only reachable if another process advanced the database past this
        # build between the read above and the migration. The refusal keeps its
        # own type so the entry points still report it as the schema error it is.
        if was_running:
            logging.error(
                "The daemon was stopped to migrate %s, but the database is newer "
                "than this build; the daemon has not been restarted.", db_path)
        raise
    except Exception as exc:
        if was_running:
            logging.error(
                "Applying pending migrations to %s failed: %s. The daemon was "
                "stopped for the migration and has not been restarted.",
                db_path, exc)
            raise SchemaMigrationFailedError(
                f"Applying pending migrations to {db_path} failed: {exc}. The "
                f"daemon was stopped for the migration and has not been "
                f"restarted; it is not running now.") from exc
        raise

    if was_running:
        started, message = controller.start()
        if started:
            logging.info(
                "Schema migration applied to %s; the daemon was restarted: %s",
                db_path, message)
        else:
            logging.warning(
                "Schema migration applied to %s, but the daemon could not be "
                "restarted: %s", db_path, message)
