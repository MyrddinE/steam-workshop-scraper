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

The daemon registers `signal.SIGINT` and `signal.SIGTERM` handlers, so a `SIGTERM` from the process that launched it is a graceful stop. On Windows a `SIGTERM` is not available for inter-process communication — `os.kill(pid, SIGTERM)` calls `TerminateProcess` (a hard kill that bypasses signal handlers) — so the PID file is the portable channel.

### PID file protocol

The cross-platform solution uses the PID file as a shutdown indicator:

1. The runner **creates** `.daemon.pid` with an exclusive create (`O_CREAT|O_EXCL`, the portable `os.open` flags) and writes its own PID into it before it constructs the daemon, and the daemon removes it via `atexit`. Existence is the rule, not liveness: an existing file — live or left behind by a crash — refuses the start before the logging reconfiguration, before `initialize_database` and before any migration, logging the file and the PID inside it and exiting non-zero, so a second daemon cannot overwrite a live file and migrate under the first. A create failure for any other reason (a missing directory, a read-only filesystem, permissions) is refused the same way with its own reason. The stale-file trade is deliberate and documented in [threading.md](threading.md#the-pid-file-refuses-a-second-start) — a stale file blocks the start until the operator removes it, and the message says how to tell it from a live daemon.
2. The runner tells the daemon the file is expected (`expect_pid_file=True`), so the file's absence counts as a stop from the daemon's first check; a stop landing during config load, migrations or thread startup is not lost. A daemon constructed without that flag (tests) is unaffected by a file it never had.
3. The daemon checks `os.path.exists(".daemon.pid")` at several points: at the top of `process_batch` before the housekeeping, after the batch read, per item, per details chunk, before the creator refresh, per discovery or subscription page, and every second in the idle wait. If the file is missing, it initiates graceful shutdown.
4. The TUI's Stop button deletes the PID file (which the daemon detects at its next checkpoint).
5. The controller signals a process directly only when it started that process itself and holds its `Popen` handle — on Windows that is the daemon; on Unix the `--daemon` double-fork makes the daemon a grandchild, so the file is the channel. A PID merely read out of the file is never signalled: a stale or corrupted file can name an unrelated process, and killing it would report the daemon stopped while it kept running.
6. It waits up to `STOP_TIMEOUT_SECONDS` (40 s, derived in `src/daemon_control.py` as the longest single main-thread block, 15 s, plus the daemon's 20 s join budget and a 5 s margin). If it owns the process it then force-kills it through the handle; if it does not, it removes the file and reports that the daemon did not exit rather than killing an unknown PID.
7. A `start()` that spawned a daemon the guard refused does not report success: the exclusive create happens before the `--daemon` fork, so the refusal is the spawn's own non-zero exit, and `DaemonController.start()` reads that exit code and returns the refusal, naming the PID file and the PID inside it. On Windows, where there is no fork, the same wait treats a live PID published in the file as the success signal instead.
8. The same protocol covers a UI start that finds a pending schema migration: the TUI and the standalone web runner stop the daemon through `DaemonController.stop()` before `initialize_database` runs, refuse to migrate if that stop reported failure, and restart the daemon afterwards. The stop is the same cross-platform channel as the Stop button — the PID file everywhere, the process handle only where the controller owns it — so the platform split does not change. A routine relaunch on the current schema never enters this path. See [schema-migrations.md](schema-migrations.md) and [threading.md](threading.md#a-pending-migration-and-a-running-daemon).

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

## Timestamp Representation

All daemon-managed timestamps are Unix epoch integers (seconds since 1970-01-01). This avoids platform-specific datetime string parsing and makes comparisons simple integer arithmetic. The conversion from ISO 8601 TEXT to INTEGER was done in migration v6→v7 using SQLite's `strftime('%s', col)`, which works identically on all platforms. The current columns and their meanings are in [timestamps.md](timestamps.md).

---

## File Paths

The project uses relative paths for database, config, images, PID file, and pause lock. No absolute paths are hardcoded. The working directory is wherever the process is launched — typically the project root or the config file location.

---

## The downloaded-item marker and "Open Folder" (Windows only)

Finding Steam's downloaded copy of a subscribed item is the one feature that is
deliberately absent everywhere but Windows, and "absent" is meant literally: off
Windows there is no `o` binding (`src.tui.app_bindings`), no `Open Folder`
button in the TUI detail pane or in the web page, and the scan is a no-op that
reads nothing. The web `POST /api/open_folder/<id>` route still exists and
refuses with a clear message, because a route cannot be un-registered per
platform, but nothing in either front end advertises it.

The reason is that every input is Windows-only: Steam's install path comes from
`HKCU\Software\Valve\Steam`'s `SteamPath` read with `winreg`, and the folder is
opened with `os.startfile`, which exists only on Windows. `src/workshop_folders`
isolates both behind injectable seams (`read_steam_path`, the launcher) so the
whole feature is testable on Linux, and degrades to nothing — no error, one
startup line naming the reason — when there is no registry entry, no Steam
install, or no readable `libraryfolders.vdf`. On a non-Windows platform, or a
Steam install that cannot be read, every item keeps its ordinary subscription
marker.

`steam.workshop_content_dirs` in the config is the escape hatch for a library
that discovery cannot see (a network drive, a moved folder); its entries are
added to whatever discovery found, never instead of it. See
[config-security.md](config-security.md).

---

## `tail -f` for Log Viewing

The TUI's `DaemonManagerScreen` used to spawn `tail -f <logfile>` and pipe it into a RichLog; it was disabled as too slow for large logs, and `tail -f` does not exist on Windows natively (WSL or Git Bash only). The pane now polls `DaemonController.tail_log` in-process every two seconds (`src/tui.py:593`), which reads a bounded 64 KiB window on every platform and needs no external utility.

---

## `os.kill(pid, 0)` for Process Existence

The deleted `_is_running` method used `os.kill(pid, 0)` to check if a PID is alive. This works on Unix (signal 0 is a no-op that returns an error if the process doesn't exist) but has subtle behavior on Windows (signal 0 is not defined). Replaced with `subprocess.Popen.poll()` which works cross-platform.
