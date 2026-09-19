import logging
import signal
import atexit
import sys
import os
from src.daemon import Daemon
from src.config import ConfigError, load_config
from src.database import initialize_database
from src import crash


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
    """A log file handler that pins UTF-8 rather than the platform default.

    Without this the file is written in the locale encoding, which on Windows is
    cp1252. That corrupts every non-ASCII character the moment the file is read
    back as UTF-8 -- the em dash in the "enriching" marker became a lone 0x97
    byte, read back as the replacement character -- and, worse, a log record the
    encoding cannot represent at all is *dropped*: cp1252 has no CJK, and
    `logging.raiseExceptions = False` below means `handleError` discards the
    record in silence, so every line naming a Japanese or Chinese item was never
    written. Pinning UTF-8 removes both problems at the writer.
    """
    return logging.FileHandler(log_file, encoding="utf-8")


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


def main():
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

    if should_daemonize:
        _daemonize()

    # Write PID file for TUI daemon manager
    pid_file = ".daemon.pid"
    with open(pid_file, "w") as f:
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
    initialize_database(db_path)
    
    daemon = Daemon(config, config_path)
    daemon.run()

if __name__ == "__main__":
    main()
