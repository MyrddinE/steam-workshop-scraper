"""Manual log rotation, shared by the daemon, the TUI and the web panel.

The daemon log grows without bound -- *measured live* on 2026-09-18 it was 594 MB
across 5.63 M lines, gaining about 115 MB a day -- but the owner keeps a
persistent ``tail`` in another window and an automatic rotation would disrupt it
without warning. Rotation is therefore **wholly manual**: nothing here runs on a
timer, on a size threshold, or at startup. The operator presses a button on the
daemon page of either front end and this module does the work.

Two processes hold the log file open -- the daemon through
``daemon_runner._log_file_handler`` and the TUI through its own handler -- so a
rename on its own would leave a handler writing into the renamed inode, silently,
until the process restarted; once that inode was compressed those records would
be gone from the live log. Every writer must notice and reopen. The chosen
mechanism is a **generation marker**:

* the rotator writes the archive's name to ``<log>.generation`` beside the log;
* every :class:`RotationAwareFileHandler` caches the marker's identity and value
  and reopens when it changes.

It is deterministic and cross-platform. ``logging.handlers.WatchedFileHandler``
would be the usual answer on POSIX but it does not reopen on Windows, which is
where production runs, and a coordination marker would need both processes to
poll and rotate in step. The per-record cost here is one ``os.stat`` on a tiny
marker file, not a read of the log.

The sequence is ordered so no writer can be surprised by a half-rotated state:
the log is renamed and a fresh one created **before** the new generation is
published, so any handler that reacts to the marker always finds the fresh file.
A handler that already passed its check can still append one record to the old
inode; the compressor keeps reading the renamed file to EOF and gives a straggler
a short, bounded window to land before it finishes, so that record is archived
rather than lost.
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

#: The subfolder the archives live in, beside the configured log file.
ARCHIVE_DIR_NAME = "logs"

#: Suffix of the generation marker written beside the log.
GENERATION_SUFFIX = ".generation"

#: Name of the lock file that serialises rotation across processes.
LOCK_NAME = ".rotate.lock"

#: The button's wording, shared by both front ends so they cannot drift.
ROTATE_BUTTON_LABEL = "Rotate Log"

#: What the readout says while a compression is running.
ROTATING_LABEL = "Rotating…"

#: A lock older than this is treated as debris from a crashed rotation.
STALE_LOCK_SECONDS = 600.0

#: Copy size, and the short window a straggler write is given after EOF.
_CHUNK_BYTES = 1024 * 1024
_DRAIN_ATTEMPTS = 3
_DRAIN_PAUSE_SECONDS = 0.05

#: How hard the marker publish tries the atomic replace before falling back.
_REPLACE_ATTEMPTS = 3
_REPLACE_PAUSE_SECONDS = 0.01

# Windows: ``io.open()``/``os.open()`` request only FILE_SHARE_READ |
# FILE_SHARE_WRITE, so a file a handler holds **cannot be renamed by another
# process** -- ``os.replace`` fails with a sharing violation for as long as the
# daemon or the TUI is running, which is exactly when a rotation is wanted.
# (CPython issue 15244; ``logging.handlers.WatchedFileHandler`` has the mirror
# problem on Windows, which is why it is not the mechanism here either.) The
# handler therefore opens the log itself on Windows with the delete share the
# rename needs, and reads the marker the same way so the marker's own
# ``os.replace`` is not blocked by a reader.
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_GENERIC_WRITE = 0x40000000
_WINDOWS_SHARE_READ = 0x00000001
_WINDOWS_SHARE_WRITE = 0x00000002
_WINDOWS_SHARE_DELETE = 0x00000004
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_OPEN_ALWAYS = 4
_WINDOWS_ATTRIBUTE_NORMAL = 0x00000080

_state_lock = threading.Lock()
#: Per-log rotation state: ``{"in_progress", "bytes", "result", "thread"}``.
_states: dict[str, dict] = {}


# ── paths ────────────────────────────────────────────────────────────────────

def archive_dir(log_file: str) -> str:
    """The ``logs`` folder beside ``log_file``."""
    return os.path.join(os.path.dirname(log_file), ARCHIVE_DIR_NAME)


def generation_path(log_file: str) -> str:
    return log_file + GENERATION_SUFFIX


def lock_path(log_file: str) -> str:
    return os.path.join(archive_dir(log_file), LOCK_NAME)


def display_path(path: str) -> str:
    """A path as the operator should read it: relative when that is shorter."""
    try:
        relative = os.path.relpath(path)
    except ValueError:
        return path
    if relative.startswith(os.pardir):
        return path
    return relative.replace(os.sep, "/")


def format_size(num_bytes) -> str:
    """Bytes as the size readout writes them; the same string in both UIs."""
    if num_bytes is None:
        return "unavailable"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # unreachable; the loop returns


# ── the generation marker ────────────────────────────────────────────────────

def _windows_fd(path: str, access: int, creation: int, flags: int) -> int:
    """A CRT descriptor for ``path`` opened with FILE_SHARE_DELETE.

    Only called on Windows. ``msvcrt.open_osfhandle`` takes ownership of the
    handle, so it is closed by hand if the conversion fails.
    """
    import _winapi
    import msvcrt

    handle = _winapi.CreateFile(
        path, access,
        _WINDOWS_SHARE_READ | _WINDOWS_SHARE_WRITE | _WINDOWS_SHARE_DELETE,
        None, creation, _WINDOWS_ATTRIBUTE_NORMAL, None)
    try:
        return msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        _winapi.CloseHandle(handle)
        raise


def _windows_append_stream(path: str, encoding, errors):
    """The append stream the handler uses on Windows (delete-sharing)."""
    binary_flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
    descriptor = _windows_fd(path, _WINDOWS_GENERIC_READ | _WINDOWS_GENERIC_WRITE,
                             _WINDOWS_OPEN_ALWAYS, binary_flags)
    binary = io.open(descriptor, "ab", closefd=True)
    return io.TextIOWrapper(binary, encoding=encoding or "utf-8", errors=errors)


def _windows_read_text(path: str):
    binary_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    descriptor = _windows_fd(path, _WINDOWS_GENERIC_READ,
                             _WINDOWS_OPEN_EXISTING, binary_flags)
    return io.open(descriptor, "r", encoding="utf-8", closefd=True)


def write_generation(log_file: str, value: str) -> None:
    """Publish a new generation atomically.

    Written to a temporary file and ``os.replace``d so a reader never sees a
    half-written value, and so the marker's inode changes: that is what makes the
    handler's cached stat reliable even on a filesystem with coarse mtimes.

    On Windows a reader can hold the marker open without delete sharing, which
    makes the replace fail for an instant; it is retried, and if the OS still
    refuses, the marker is rewritten in place. That is not atomic, but the value
    only has to differ from the previous one, and a handler that reads it
    half-written merely reopens once more than necessary.
    """
    target = generation_path(log_file)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    payload = value + "\n"
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".generation-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(payload)
        last_error = None
        for _ in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, target)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(_REPLACE_PAUSE_SECONDS)
        try:
            os.remove(temporary)
        except OSError:
            pass
        try:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(payload)
        except OSError:
            raise last_error
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def read_generation(log_file: str) -> str:
    path = generation_path(log_file)
    try:
        if sys.platform == "win32":
            with _windows_read_text(path) as fh:
                return fh.read().strip()
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    # A missing or unreadable marker means "no rotation recorded yet".
    except OSError:
        return ""


def _generation_identity(marker: str):
    """Stat tuple that changes on every :func:`write_generation`."""
    try:
        info = os.stat(marker)
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


class RotationAwareFileHandler(logging.FileHandler):
    """A ``FileHandler`` that reopens the log when its generation changes.

    The check is a cached ``os.stat`` of the marker on every record; only when
    that identity moves is the small marker read and the stream reopened. This is
    the cross-platform half of the design: on Windows nothing else notices that
    the file under the handler was replaced.
    """

    def __init__(self, filename, mode="a", encoding=None, delay=False, errors=None):
        self._log_path = filename
        self._marker_path = generation_path(filename)
        self._marker_identity = None
        self._generation = None
        super().__init__(filename, mode=mode, encoding=encoding, delay=delay,
                         errors=errors)
        self._remember_generation()

    def _open(self):
        """On Windows, open with delete sharing so the rotator can rename the log.

        ``io.open`` requests only read/write sharing, so a plain ``FileHandler``
        holds the log in a way that makes ``os.replace`` fail while this process
        is running -- the feature would work on POSIX and never on the platform
        it is for. Any failure here falls back to the ordinary stream: logging
        keeps working, and a rotation that the OS then refuses reports its
        failure rather than losing a record.
        """
        if sys.platform == "win32":
            try:
                return _windows_append_stream(self.baseFilename, self.encoding,
                                              self.errors)
            except Exception:
                logging.getLogger(__name__).debug(
                    "Log opened without delete sharing; a manual rotation may be "
                    "refused by the OS", exc_info=True)
        return super()._open()

    def _remember_generation(self) -> None:
        self._marker_identity = _generation_identity(self._marker_path)
        self._generation = read_generation(self._log_path)

    def _rotation_happened(self) -> bool:
        identity = _generation_identity(self._marker_path)
        if identity == self._marker_identity:
            return False
        self._marker_identity = identity
        generation = read_generation(self._log_path)
        if generation == self._generation:
            return False
        self._generation = generation
        return True

    def emit(self, record) -> None:
        if self._rotation_happened():
            self.reopen()
        super().emit(record)

    def reopen(self) -> None:
        """Drop the old descriptor and reopen the path it names."""
        self.acquire()
        try:
            if self.stream:
                self.stream.close()
            self.stream = self._open()
        finally:
            self.release()
        self._remember_generation()


def log_file_handler(log_file: str) -> RotationAwareFileHandler:
    """The handler both front ends install; UTF-8 pinned, rotation-aware."""
    return RotationAwareFileHandler(log_file, encoding="utf-8")


# ── the rotation ─────────────────────────────────────────────────────────────

def _unique_archive(log_file: str, when: datetime) -> tuple[str, str]:
    """``(archive, raw)`` paths that do not collide with anything on disk.

    ``archive`` is the gzip the operator ends up with; ``raw`` is the renamed log
    while it is being compressed, and survives a compression failure so the bytes
    are not lost.
    """
    directory = archive_dir(log_file)
    os.makedirs(directory, exist_ok=True)
    stem = os.path.splitext(os.path.basename(log_file))[0] or "log"
    stamp = when.strftime("%Y%m%d-%H%M%S")
    suffix = 1
    while True:
        tail = "" if suffix == 1 else f"-{suffix}"
        archive = os.path.join(directory, f"{stem}-{stamp}{tail}.log.gz")
        raw = archive[: -len(".gz")]
        if not os.path.exists(archive) and not os.path.exists(raw):
            return archive, raw
        suffix += 1


def _acquire_lock(log_file: str):
    """Create the cross-process rotation lock, or return None if one is held."""
    path = lock_path(log_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            age = 0.0
        if age < STALE_LOCK_SECONDS:
            return None
        try:
            os.remove(path)
        except OSError:
            return None
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
    try:
        os.write(descriptor, f"{os.getpid()} {time.time():.3f}\n".encode("ascii"))
    finally:
        os.close(descriptor)
    return path


def _release_lock(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _gzip_into(source: str, destination: str) -> int:
    """Compress ``source`` into ``destination``, returning the bytes consumed.

    A handler that had already passed its generation check when the rotation
    began can still append one record to the renamed inode. After reaching EOF
    the reader gives that straggler a short bounded window, so its bytes are
    archived instead of lost; the common case reads to EOF once.
    """
    consumed = 0
    with open(source, "rb") as src, gzip.open(destination, "wb") as dst:
        for attempt in range(_DRAIN_ATTEMPTS + 1):
            while True:
                chunk = src.read(_CHUNK_BYTES)
                if not chunk:
                    break
                dst.write(chunk)
                consumed += len(chunk)
            if attempt == _DRAIN_ATTEMPTS:
                break
            try:
                if os.path.getsize(source) <= consumed:
                    break
            except OSError:
                break
            time.sleep(_DRAIN_PAUSE_SECONDS)
    return consumed


def _finish_rotation(key: str, raw: str, archive: str, lock: str) -> None:
    try:
        archived_bytes = _gzip_into(raw, archive)
        try:
            os.remove(raw)
        except OSError:
            # The archive is complete even if the temporary raw file lingers.
            logging.warning("Rotated log left at %s", display_path(raw))
        message = (f"Rotated: {display_path(archive)} "
                   f"({format_size(os.path.getsize(archive))})")
        result = {
            "ok": True,
            "started": False,
            "archive": archive,
            "archive_size": os.path.getsize(archive),
            "bytes": archived_bytes,
            "message": message,
            "finished_at": time.time(),
        }
    except Exception as exc:
        # The rename already happened, so the bytes are in `raw`; naming it is
        # what lets the operator recover them rather than lose the log. A
        # half-written gzip would look like a good archive, so it is removed.
        try:
            os.remove(archive)
        except OSError:
            pass
        result = {
            "ok": False,
            "started": False,
            "archive": archive,
            "archive_size": None,
            "bytes": None,
            "message": (f"Rotation failed: {exc}. The renamed log is at "
                        f"{display_path(raw)}"),
            "finished_at": time.time(),
        }
    finally:
        _release_lock(lock)
    with _state_lock:
        state = _states.get(key)
        if state is not None:
            state["in_progress"] = False
            state["result"] = result
            state["bytes"] = None


def rotate_log(log_file, *, now: datetime | None = None,
               background: bool = True) -> dict:
    """Rotate ``log_file`` now: rename it, publish the generation, compress it.

    The rename and the fresh log are done before this returns, so the live path
    is already new; the gzip runs on a background thread by default, because the
    production log is hundreds of megabytes and the caller is a UI. The returned
    dict is the immediate outcome; the finished outcome is read through
    :func:`rotation_status`.
    """
    if not log_file:
        return {"ok": False, "started": False,
                "message": "No log file is configured."}
    # Checked before the size, so a press while a large compression runs is told
    # what is actually happening rather than that the (already fresh) log is empty.
    if _lock_is_held(log_file):
        return {"ok": False, "started": False,
                "message": "A rotation is already in progress."}
    try:
        size = os.path.getsize(log_file)
    except OSError:
        return {"ok": True, "started": False,
                "message": "Nothing to rotate: the log file does not exist."}
    if size == 0:
        return {"ok": True, "started": False,
                "message": "Nothing to rotate: the log is empty."}

    lock = None
    key = os.path.abspath(log_file)
    try:
        lock = _acquire_lock(log_file)
        if lock is None:
            return {"ok": False, "started": False,
                    "message": "A rotation is already in progress."}

        archive, raw = _unique_archive(log_file, now or datetime.now(timezone.utc))
        # Rename first, so the live log is fresh immediately, then create the new
        # one, and only then publish the generation. A handler that reacts to the
        # marker therefore always finds the fresh file at the path.
        os.replace(log_file, raw)
        try:
            with open(log_file, "a", encoding="utf-8"):
                pass
            write_generation(log_file, os.path.basename(archive))
        except Exception:
            # Nothing has seen the new generation yet, so the old log can be put
            # back rather than leaving the path missing.
            try:
                if os.path.getsize(log_file) == 0:
                    os.remove(log_file)
                os.replace(raw, log_file)
            except OSError:
                pass
            raise
    except Exception as exc:
        if lock:
            _release_lock(lock)
        return {"ok": False, "started": False,
                "message": f"Rotation failed: {exc}"}

    started = {
        "ok": True,
        "started": True,
        "archive": archive,
        "archive_size": None,
        "bytes": size,
        "message": f"{ROTATING_LABEL} ({format_size(size)})",
        "finished_at": None,
    }
    if not background:
        _finish_rotation(key, raw, archive, lock)
        with _state_lock:
            return dict(_states[key]["result"])

    thread = threading.Thread(target=_finish_rotation,
                              args=(key, raw, archive, lock),
                              name="log-rotation", daemon=True)
    with _state_lock:
        _states[key] = {
            "in_progress": True,
            "bytes": size,
            "result": None,
            "thread": thread,
        }
    thread.start()
    return started


def wait_for_rotation(log_file: str, timeout: float = 30.0) -> bool:
    """Block until a background rotation of ``log_file`` finishes."""
    with _state_lock:
        state = _states.get(os.path.abspath(log_file))
        thread = state.get("thread") if state else None
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


# ── status for the two front ends ────────────────────────────────────────────

def _lock_is_held(log_file: str) -> bool:
    try:
        age = time.time() - os.path.getmtime(lock_path(log_file))
    except OSError:
        return False
    return age < STALE_LOCK_SECONDS


def rotation_status(log_file) -> dict:
    """``{in_progress, bytes, result}`` for ``log_file``.

    The lock file is consulted as well as this process's own state, so a panel in
    one process sees a rotation started by the other and disables its button.
    """
    if not log_file:
        return {"in_progress": False, "bytes": None, "result": None}
    state = _states.get(os.path.abspath(log_file))
    if state is not None:
        if state["in_progress"]:
            return {"in_progress": True, "bytes": state.get("bytes"),
                    "result": None}
        return {"in_progress": False, "bytes": None, "result": state.get("result")}
    if _lock_is_held(log_file):
        return {"in_progress": True, "bytes": None, "result": None}
    return {"in_progress": False, "bytes": None, "result": None}


def live_log_size(log_file) -> int | None:
    try:
        return os.path.getsize(log_file)
    except OSError:
        return None


def log_readout(log_file) -> str:
    """The one line the size readout shows, identical in both front ends."""
    if not log_file:
        return "Log size: not configured"
    status = rotation_status(log_file)
    if status["in_progress"]:
        if status.get("bytes"):
            return f"{ROTATING_LABEL} ({format_size(status['bytes'])})"
        return ROTATING_LABEL
    size = live_log_size(log_file)
    if size is None:
        return "Log size: no log file"
    return f"Log size: {format_size(size)}"


def log_status(log_file) -> dict:
    """Everything the daemon page needs to draw the log readout and button."""
    status = rotation_status(log_file)
    result = status.get("result") or {}
    return {
        "log_file": log_file,
        "log_size": live_log_size(log_file) if log_file else None,
        "log_readout": log_readout(log_file),
        "can_rotate": bool(log_file),
        "rotating": bool(status["in_progress"]),
        "rotation_ok": bool(result.get("ok")),
        "rotation_message": result.get("message") or "",
    }
