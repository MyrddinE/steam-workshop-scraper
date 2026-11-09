"""One cooldown for the automatic sign-in window, shared by both front ends.

The TUI and the web UI run in different processes and both watch the same session
problem. Either could open a sign-in page, and the two together used to mean two
windows per problem even after each process had its own cooldown. A window steals
focus, so the permission to open one lives in a file beside the database that both
processes can read and write.

The file holds a Unix timestamp. Claiming an open is a read-check-write under a
short-lived lock file, so two processes asking at the same moment cannot both be
told "yes". The lock is created with ``O_EXCL`` and is removed in ``finally``; a
lock older than the stale threshold is broken so a crashed process cannot block
sign-in forever. This is best-effort like the rest of the daemon state: if the
file cannot be written, claiming is refused (skipping a window is safer than
stealing focus every five seconds).
"""

from __future__ import annotations

import logging
import os
import time

#: Ten minutes. Long enough not to interrupt the operator, short enough that a
#: genuinely new problem is still surfaced during the same session.
COOLDOWN_SECONDS = 10 * 60.0

STAMP_NAME = ".signin_opened_at"
LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT_SECONDS = 1.0
STALE_LOCK_SECONDS = 10.0


def stamp_path(db_path: str) -> str:
    """The shared timestamp file, beside the database."""
    directory = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(directory, STAMP_NAME)


def _read(path: str) -> float | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return float(handle.read().strip())
    except (OSError, ValueError):
        return None


def _write(path: str, now: float) -> None:
    """Publish the timestamp atomically, so a reader never sees a half file."""
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(f"{float(now):.6f}")
    os.replace(temporary, path)


def _acquire_lock(lock_path: str) -> bool:
    """Take the short-lived lock, breaking a stale one left by a crash."""
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(descriptor)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > STALE_LOCK_SECONDS:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        except OSError as exc:
            logging.debug("Could not create the sign-in cooldown lock: %s", exc)
            return False


def _release_lock(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except OSError:
        pass


def opened_at(db_path: str) -> float | None:
    """When the last sign-in window was opened, or ``None``."""
    return _read(stamp_path(db_path))


def cooldown_remaining(db_path: str, now: float | None = None,
                       cooldown: float = COOLDOWN_SECONDS) -> float:
    """Seconds until another automatic window is allowed (0 when allowed)."""
    last = opened_at(db_path)
    if last is None:
        return 0.0
    now = time.time() if now is None else now
    return max(0.0, cooldown - (now - last))


def claim_open(db_path: str, now: float | None = None,
               cooldown: float = COOLDOWN_SECONDS) -> bool:
    """Claim the right to open one automatic sign-in window.

    Returns ``True`` only when no window has been opened within ``cooldown``.
    The claim writes the timestamp, so a second front end asking immediately
    after is told ``False``.
    """
    now = time.time() if now is None else now
    path = stamp_path(db_path)
    lock_path = path + LOCK_SUFFIX
    if not _acquire_lock(lock_path):
        # Do not open when we cannot serialise the decision: two processes
        # racing here is exactly what would steal focus twice.
        return False
    try:
        last = _read(path)
        if last is not None and now - last < cooldown:
            return False
        try:
            _write(path, now)
        except OSError as exc:
            logging.warning("Could not record the sign-in window cooldown: %s", exc)
            return False
        return True
    finally:
        _release_lock(lock_path)


def record_open(db_path: str, now: float | None = None) -> float:
    """Record a user-initiated open, so the other front end does not follow it."""
    now = time.time() if now is None else now
    path = stamp_path(db_path)
    lock_path = path + LOCK_SUFFIX
    acquired = _acquire_lock(lock_path)
    try:
        _write(path, now)
    except OSError as exc:
        logging.warning("Could not record the manual sign-in open: %s", exc)
    finally:
        if acquired:
            _release_lock(lock_path)
    return now
