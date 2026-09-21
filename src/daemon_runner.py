import logging
import signal
import atexit
import sys
import os
from src.daemon import Daemon
from src.config import ConfigError, load_config
from src.database import initialize_database, SchemaVersionError
from src import crash
from src import log_rotation


class _SafeStreamHandler(logging.StreamHandler):
    """Catches UnicodeEncodeError that occurs on Windows when the console
    encoding (cp1252) rejects CJK characters, even though the terminal
    renders them correctly.  The log line still appears; this just
    prevents the daemon from crashing on a spurious encoding error."""

    def emit(self, record):
        try:
            msg = self.format(record)
            self.stream.write(msg + self.terminator)
            self.flush()
        # The console cannot encode CJK when its code page is not UTF-8. Note
        # that this copy is the *only* one dropped: the file handler is pinned to
        # UTF-8 (`_log_file_handler`), so the record is still written where it
        # matters. Before that pin, this comment was wrong -- the file handler had
        # the same cp1252 problem and lost the record instead.
        except UnicodeEncodeError:
            pass
        except Exception:
            self.handleError(record)


def _log_file_handler(log_file: str) -> logging.FileHandler:
    """A log file handler that pins UTF-8 and follows a manual rotation.

    Without the UTF-8 pin the file is written in the locale encoding, which on
    Windows is cp1252. That corrupts every non-ASCII character the moment the
    file is read back as UTF-8 -- the em dash in the "enriching" marker became a
    lone 0x97 byte, read back as the replacement character -- and, worse, a log
    record the encoding cannot represent at all is *dropped*: cp1252 has no CJK,
    and `logging.raiseExceptions = False` below means `handleError` discards the
    record in silence, so every line naming a Japanese or Chinese item was never
    written. Pinning UTF-8 removes both problems at the writer.

    The rotation half is the other invisible failure: the daemon's own handler is
    one of the two that hold the log open, so when the operator rotates it the
    handler must reopen the fresh file rather than keep writing to the renamed
    inode. ``RotationAwareFileHandler`` watches the generation marker the rotator
    publishes and reopens; see ``src/log_rotation.py``.
    """
    return log_rotation.log_file_handler(log_file)


def _fix_windows_encoding():
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    # Best-effort code-page probe; the stream reconfigure just below establishes
    # UTF-8 regardless.
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    # If reconfigure is unavailable, _SafeStreamHandler.emit (above) absorbs any
    # later UnicodeEncodeError.
    except Exception:
        pass


def _daemonize():
    """Double-fork to detach from terminal and become a background process.
    On Windows, os.fork() is not available — skip daemonization."""
    if sys.platform == 'win32':
        return
    if os.fork():
        sys.exit(0)  # parent exits
    os.setsid()
    if os.fork():
        sys.exit(0)  # first child exits
    # Redirect std* to /dev/null
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    os.dup2(devnull, sys.stdout.fileno())
    os.dup2(devnull, sys.stderr.fileno())


# The PID file is both the record of who is running and, since issue 65, the
# guard that keeps a second daemon from starting over the first. The path is
# relative to the daemon's working directory and is the same file
# ``src/daemon.py`` watches for its absence (`Daemon._pid_file_removed`).
PID_FILE = ".daemon.pid"

# A refused start -- the PID file already exists, or could not be created -- gets
# its own exit code, distinct from the config refusals (1 and 2), so a launcher
# can tell "a daemon is already running" from "this build will not run".
PID_FILE_REFUSED_EXIT_CODE = 3


def _read_existing_pid(pid_file: str) -> int | None:
    """The PID inside ``pid_file``, or None when it cannot be read.

    Diagnostic only. The refusal below is decided by the file's *existence*,
    never by whether the PID inside it is alive: a liveness probe would revive
    the stale/recycled-PID hazard the controller was fixed to avoid, and the
    owner's chosen rule is that existence blocks the start.
    """
    try:
        with open(pid_file, encoding="utf-8") as f:
            return int(f.read().strip())
    # Missing between the failed create and this read, empty, or not a number:
    # all mean there is no PID to name, not that the start may proceed.
    except (OSError, ValueError):
        return None


def _refuse_start(message: str) -> None:
    """Log why the daemon did not start and exit non-zero.

    The PID file is taken before the logging reconfiguration on purpose, so at
    this point only the minimal stderr configuration below exists -- the same
    shape the config-load refusals use. The line names the file and, when it can
    be read, the PID inside it, and says what the operator can do about it.
    """
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')
    logging.error(message)
    sys.exit(PID_FILE_REFUSED_EXIT_CODE)


def _acquire_pid_file(pid_file: str) -> int:
    """Create ``pid_file`` exclusively and return its open descriptor.

    ``O_CREAT|O_EXCL`` is the guard: two starts racing here cannot both win, and
    the loser is refused before it has touched the database. The descriptor is
    held open across ``_daemonize`` and written once the final daemon process
    exists. Any other failure -- a missing directory, a read-only filesystem,
    permissions -- is raised to the caller and refused with its own reason.
    """
    return os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)


def _refuse_existing_pid_file(pid_file: str) -> None:
    """Refuse the start, naming the file and the PID in it when readable."""
    pid = _read_existing_pid(pid_file)
    if pid is not None:
        message = (
            f"Refusing to start: PID file {pid_file} already exists "
            f"(PID {pid} in it). If PID {pid} is a running daemon, stop it "
            f"first; if the daemon crashed and left the file behind, remove "
            f"{pid_file} and start again.")
    else:
        message = (
            f"Refusing to start: PID file {pid_file} already exists but no PID "
            f"could be read from it. If a daemon is running, stop it first; if "
            f"the file is left over from a crash, remove {pid_file} and start "
            f"again.")
    _refuse_start(message)


def main():
    # Before anything can fail: a crash while `load_config` runs or while the
    # logging below is being configured has no other destination -- the file
    # handler is the thing being built and this is its only process. The hooks
    # do not need logging; `crash.install` below adds the ring buffer once
    # `basicConfig` has run, so a forced configuration cannot drop it.
    crash.install_hooks("daemon")
    _fix_windows_encoding()
    config_path = "config.yaml"
    args = [a for a in sys.argv[1:] if a != "--daemon"]
    should_daemonize = "--daemon" in sys.argv
    if args:
        config_path = args[0]
        
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        logging.error(f"Configuration file not found: {config_path}")
        sys.exit(1)
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        logging.error("%s", exc)
        sys.exit(2)

    # Take the PID file before anything else -- before the logging
    # reconfiguration, before initialize_database and its migrations -- so a
    # refused start has not touched the database. Existence is the rule: a
    # second daemon must not overwrite a live PID file and migrate under the
    # first, and a stale file left by a crash refuses the start until the
    # operator removes it (the message says how to tell the two apart). The
    # file is created here, before the fork, so the process the UI spawned is
    # the one that exits non-zero on a refusal; the PID is written after the
    # fork, once the final daemon process exists.
    pid_file = PID_FILE
    try:
        pid_fd = _acquire_pid_file(pid_file)
    except FileExistsError:
        _refuse_existing_pid_file(pid_file)
    except OSError as exc:
        _refuse_start(
            f"Refusing to start: cannot create PID file {pid_file}: {exc}")

    if should_daemonize:
        _daemonize()

    # The descriptor was opened before the fork; write the daemon's own PID now
    # that the final process exists. Still before initialize_database, so the
    # stop-then-migrate order is unchanged and the write is the guard as well as
    # the record.
    with os.fdopen(pid_fd, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.remove(pid_file) if os.path.exists(pid_file) else None)
        
    log_config = config.get("logging", {})
    level_str = log_config.get("level", "INFO").upper()
    log_level = getattr(logging, level_str, logging.INFO)
    log_file = log_config.get("file")

    handlers = []
    if log_file:
        handlers.append(_log_file_handler(log_file))
    if not should_daemonize:
        stdout_handler = _SafeStreamHandler(sys.stdout)
        stdout_handler.setLevel(log_level)
        handlers.append(stdout_handler)
    handlers.append(_SafeStreamHandler(sys.stderr))
    handlers[-1].setLevel(logging.ERROR)

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True
    )
    logging.raiseExceptions = False

    # After logging is configured, so the ring-buffer handler is not dropped by
    # the forced basicConfig above. The daemon's worker threads already report a
    # killed thread through `threading.excepthook`; this gives that traceback a
    # destination on disk. It changes no handler, exit code or control flow.
    crash.install("daemon", config, config_path=config_path)

    # Verify handler types at startup
    root = logging.getLogger()
    handler_types = [type(h).__name__ for h in root.handlers]
    logging.info(f"Daemon starting — handlers: {handler_types}")
    
    db_path = config.get("database", {}).get("path", "workshop.db")
    try:
        initialize_database(db_path)
    except SchemaVersionError as exc:
        # The same shape as the ConfigError refusal above: the operator gets the
        # sentence, not a traceback, and the process exits 2 without starting the
        # daemon against a schema it cannot understand. Logging is configured by
        # now, so the line reaches both stderr and the log file.
        logging.error("%s", exc)
        sys.exit(2)

    # The PID file was written above, before the (possibly slow) migrations, so
    # this daemon must treat its absence as a stop from the very first check.
    # Inferring "a PID file is expected" from having observed the file loses a
    # stop that lands in the window before the first observation; see
    # Daemon._saw_pid_file.
    daemon = Daemon(config, config_path, expect_pid_file=True)
    daemon.run()

if __name__ == "__main__":
    main()
