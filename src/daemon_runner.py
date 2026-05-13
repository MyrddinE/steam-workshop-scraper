import logging
import signal
import atexit
import io
import sys
import os
from src.daemon import Daemon
from src.config import load_config
from src.database import initialize_database


def _fix_windows_encoding():
    """On Windows, sys.stdout defaults to a code page that can't encode
    CJK/Unicode characters, causing UnicodeEncodeError in logging.
    Re-wrap with UTF-8 so titles like 千厮门大桥 display correctly.
    The TUI avoids this by logging to file only; the daemon logs to stdout."""
    if sys.platform == 'win32' and hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def _daemonize():
    """Double-fork to detach from terminal and become a background process."""
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
    is_daemon = "--daemon" in sys.argv
    if args:
        config_path = args[0]
        
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        logging.error(f"Configuration file not found: {config_path}")
        sys.exit(1)

    if is_daemon:
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
        handlers.append(logging.FileHandler(log_file))
    if not is_daemon:
        # stdout: everything except errors; stderr: errors only
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(log_level)
        handlers.append(stdout_handler)
    handlers.append(logging.StreamHandler(sys.stderr))
    handlers[-1].setLevel(logging.ERROR)

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True
    )
    
    db_path = config.get("database", {}).get("path", "workshop.db")
    initialize_database(db_path)
    
    daemon = Daemon(config, config_path)
    daemon.run()

if __name__ == "__main__":
    main()
