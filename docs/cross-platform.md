# Cross-Platform Boundaries

The project targets both Linux (primary development) and Windows. Several platform-specific code paths exist, particularly around process management, console encoding, and file I/O.

---

## Daemon Process Creation

### `_daemonize` (daemon_runner)

The `--daemon` flag triggers daemonization. On Unix, this performs a double-fork with `os.setsid()` to detach from the terminal, then redirects stdin/stdout/stderr to `/dev/null`. On Windows, `os.fork()` is unavailable, so the function returns immediately (no-op). The daemon runs in the foreground on Windows, relying on `DETACHED_PROCESS` (applied by the TUI's `subprocess.Popen`) to separate from the console.

### TUI `_start_daemon` (tui.py)

Starts the daemon process via `subprocess.Popen`. Platform-specific behavior:
- **Linux**: Redirects stdout and stderr to `subprocess.DEVNULL` (the daemon logs to a file or its own console).
- **Windows**: Uses `creationflags=subprocess.DETACHED_PROCESS` to create a new process group detached from the TUI's console. Does not redirect output (so log messages are visible).

---

## Graceful Shutdown

### Signal handling (`Daemon`)

The daemon registers `signal.SIGINT` and `signal.SIGTERM` handlers. On Unix, `SIGTERM` is sent by the TUI's `Popen.send_signal()`. On Windows, neither signal is available for inter-process communication — `os.kill(pid, SIGTERM)` calls `TerminateProcess` (a hard kill that bypasses signal handlers).

### PID file protocol

The cross-platform solution uses the PID file as a shutdown indicator:

1. The daemon writes `.daemon.pid` at startup and removes it via `atexit`.
2. The daemon's main loop checks `os.path.exists(".daemon.pid")` after each `process_batch`. If the file is missing, it initiates graceful shutdown.
3. The TUI's Stop button deletes the PID file (which the daemon detects on its next loop iteration).
4. On Linux, the TUI also sends SIGTERM for faster response.
5. On Windows, the TUI deletes the PID file and waits up to 5 seconds. If the daemon hasn't exited, `Popen.terminate()` is called as fallback (hard kill). The TUI also manually removes the PID file on Windows after forced termination, since `atexit` handlers don't fire on `TerminateProcess`.

---

## Console Encoding

### `_fix_windows_encoding` (daemon_runner)

On Windows, the default console code page (typically cp1252) can't encode CJK characters, causing `UnicodeEncodeError` when logging item titles in Chinese/Japanese. The function:
1. Sets the console output code page to 65001 (UTF-8) via `ctypes.windll.kernel32.SetConsoleOutputCP`.
2. Calls `sys.stdout.reconfigure(encoding='utf-8', errors='replace')` and `sys.stderr.reconfigure(encoding='utf-8', errors='replace')`.

This runs at the very start of `main()` before any logging is configured.

### `_SafeStreamHandler` (daemon_runner)

A custom `logging.StreamHandler` that wraps `emit()` with a `try/except UnicodeEncodeError`. When the console encoding rejects a character, the error is silently swallowed rather than crashing the daemon. This is a belt-and-suspenders fallback — it only activates if `_fix_windows_encoding` partially fails (e.g., `reconfigure()` is unavailable on older Python, or the console is a frozen executable wrapper).

The TUI avoids this issue entirely by logging only to a file (no stdout handler).

### Logger configuration

On Windows with `--daemon`, the logger is configured with `_fix_windows_encoding` applied first. On Linux with `--daemon`, the daemon redirects stdout to `/dev/null` (so encoding is irrelevant). The file handler always uses UTF-8 via Python's default.

---

## `dt_*` Timestamps

All daemon-managed timestamps are Unix epoch integers (seconds since 1970-01-01). This avoids platform-specific datetime string parsing and makes comparisons simple integer arithmetic. The conversion from ISO 8601 TEXT to INTEGER was done in migration v6→v7 using SQLite's `strftime('%s', col)` which works identically on all platforms.

---

## File Paths

The project uses relative paths for database, config, images, PID file, and pause lock. No absolute paths are hardcoded. The working directory is wherever the process is launched — typically the project root or the config file location.

---

## `tail -f` for Log Viewing

The TUI's `DaemonManagerScreen` had a `_start_tail` method that spawned `tail -f <logfile>` on Unix and piped output to a RichLog widget. This is commented out because it was too slow for large log files. On Windows, `tail -f` doesn't exist natively (available in WSL or Git Bash but not guaranteed). No replacement has been implemented.

---

## `os.kill(pid, 0)` for Process Existence

The deleted `_is_running` method used `os.kill(pid, 0)` to check if a PID is alive. This works on Unix (signal 0 is a no-op that returns an error if the process doesn't exist) but has subtle behavior on Windows (signal 0 is not defined). Replaced with `subprocess.Popen.poll()` which works cross-platform.
