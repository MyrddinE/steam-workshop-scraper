"""Timer-driven database reads that tolerate a transient SQLite lock.

The TUI reads the database from Textual timer callbacks: the detail pane every
two seconds, the subscription-marker poll on its own one-shot tick, the
statistics screen on its scheduler tick. An exception raised in one of those
callbacks does not fail that refresh -- Textual reports it and ends the session,
which is how a lock held by the daemon for a moment took the whole TUI down on
2026-09-18 (issue 43).

The first half of the fix removed the unnecessary lock: ``get_connection`` no
longer runs ``PRAGMA journal_mode=WAL``, because the mode is a persistent
property of the file and is now set once by ``initialize_database``. That
removes the specific statement the busy timeout could not wait out. This module
is the second half: an unattended reader that still meets a lock -- SQLite's
``database is locked`` can outlive the connection's 15 s timeout -- skips that
tick and tries again on the next one instead of ending the session.

**Only a lock is tolerated.** ``sqlite3.OperationalError`` is caught, and nothing
else, so a genuine defect still surfaces. A user-initiated action (a search, a
button press, a queue toggle) is not decorated with this guard: its failure is
reported to the person who asked for it, which is the whole point of the
difference.

**A lock that will not clear does not fill the log.** The daemon log is already
hundreds of megabytes and unrotated (issue 37), and a two-second poll against a
persistent lock would otherwise write a line every tick, forever. The first
failure of a run is a warning; repeats are debug until a read succeeds, at which
point the next failure warns again.

The guard is a decorator so a new poll cannot reintroduce the crash by pasting a
slightly different ``try/except`` -- or by forgetting one. The TUI tests walk
every ``set_interval``/``set_timer`` callback in ``src/tui.py`` and require each
one either to carry :func:`guard_db_poll` or to be listed, with its reason, as a
timer that does not read the database.
"""

from __future__ import annotations

import functools
import inspect
import logging
import sqlite3

#: Attribute the decorator sets on the wrapper. The TUI test that walks the
#: timer callbacks reads it, so the marker is part of this module's contract.
_TOLERATES_DB_LOCK = "_tolerates_db_lock"


class RepeatFailureLog:
    """Report one failure per run, then stay quiet until it works again.

    ``failed(key, ...)`` logs at warning the first time a key is seen to fail and
    at debug afterwards; ``succeeded(key)`` clears it, so a failure *after* a
    success is reported again. The key is normally the reader's name, which keeps
    two different polls from suppressing each other.
    """

    def __init__(self, logger: logging.Logger | None = None):
        self._logger = logger or logging.getLogger()
        self._failing: set[str] = set()

    def failed(self, key: str, message: str, *args) -> None:
        if key in self._failing:
            self._logger.debug(message + " (still failing)", *args)
        else:
            self._failing.add(key)
            self._logger.warning(message, *args)

    def succeeded(self, key: str) -> None:
        self._failing.discard(key)


#: One reporter for every guarded poll. Keyed by reader name, so each poll's
#: streak is independent.
_lock_log = RepeatFailureLog()


def guard_db_poll(reader: str, log: RepeatFailureLog | None = None):
    """Decorate a TUI timer callback so a transient lock skips this tick.

    Both plain and ``async`` callbacks are supported. On
    ``sqlite3.OperationalError`` the callback returns without raising: for a
    ``set_interval`` callback the next tick comes by itself, and a one-shot poll
    that re-arms itself does so from the rendered state, which a skipped read
    did not change.

    ``reader`` names the reader in the log line. ``log`` is injectable so a test
    can watch one reporter without the module-level state.
    """
    reporter = log or _lock_log

    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                try:
                    result = await fn(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    reporter.failed(reader, "%s: database unavailable; skipping "
                                           "this tick and trying again (%s)", reader, exc)
                    return None
                reporter.succeeded(reader)
                return result
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                try:
                    result = fn(*args, **kwargs)
                except sqlite3.OperationalError as exc:
                    reporter.failed(reader, "%s: database unavailable; skipping "
                                           "this tick and trying again (%s)", reader, exc)
                    return None
                reporter.succeeded(reader)
                return result

        setattr(wrapper, _TOLERATES_DB_LOCK, True)
        wrapper.reader_name = reader
        return wrapper

    return decorate
