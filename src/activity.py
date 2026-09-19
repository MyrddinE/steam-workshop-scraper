"""Active time and inflow: what a queue rate must account for.

A rate is completions divided by time. Two things make plain wall-clock time the
wrong denominator for a work queue, and both are recorded here:

* a **pause** stops the queues while the clock keeps running, so a rate measured
  over wall-clock falls every time the daemon is switched off. The pause is the
  ``.pauselock`` file the daemon's web and image workers poll; this module records
  the intervals the lock was held for, so the drain estimate can divide by the
  **active** time instead.
* the **staleness sweep** pushes items back into the API queue -- work arriving,
  not work completing -- so the API queue's completion count is reduced by the
  sweep's own recorded rowcount before a rate is taken from it.

The facts live in the same restart-surviving store the session warning and the
pacing backoff use (``src/daemon_state.py``, ``.daemon_state.yaml`` beside the
database), for the same reason: they describe a condition that passes and that
nobody chose, rather than configuration. Each writer owns a top-level section, so
one cannot clobber another's; every write is best-effort, because losing a pause
interval costs a slightly wrong rate while letting a diagnostic exception escape
would stop the work the record exists to measure.

**The file is the signal, not the caller.** ``begin_pause`` opens an interval only
on the absent -> present transition and ``end_pause`` closes it on present ->
absent, so the three writers of ``.pauselock`` -- the TUI's subscription screen,
``POST /api/pause``, and the subscribe engine's own :class:`PauseLock` -- can
nest without subtracting the same paused time twice or ending an interval another
holder still wants. An interval that is still open is read up to "now", so a
pause *in progress* still lets the rate be computed from the active time before
it.

What is deliberately **not** recorded here is the discovery inflow into the API
queue. ``first_seen_at`` is our clock, but it is not indexed on ``workshop_items``
and a window count over it would be a full scan of a multi-million-row table;
rather than buy that with an index this change does not need, the docs state that
the API queue's net rate subtracts the staleness sweep only.
"""

from __future__ import annotations

import logging
import os
import time

from src.daemon_state import StateStore, state_path_for

# The top-level keys this module owns in the daemon state file.
PAUSE_SECTION = "queue_pauses"
SWEEP_SECTION = "queue_sweeps"

#: How long pause intervals and sweep entries are kept. The drain metric's window
#: is one day, so a week is comfortably more than any caller asks for and keeps
#: the document small.
KEEP_SECONDS = 7 * 86400


def _now(now: int | float | None) -> int:
    return int(time.time() if now is None else now)


def _as_int(value) -> int | None:
    """``int(value)`` for a value from a file a person may have edited."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _prune(intervals: list, cutoff: int) -> list:
    """Drop intervals that ended before ``cutoff``; keep the rest in order."""
    return [entry for entry in intervals if len(entry) == 2 and entry[1] >= cutoff]


def _merge(intervals: list[tuple[int, int]], window_start: int, now: int) -> float:
    """Merged, window-clamped length of the intervals, in seconds.

    Overlapping intervals are merged before their lengths are added: the three
    pause writers can overlap, and summing them raw would subtract the same
    paused second twice. Each interval is clipped to ``[window_start, now]`` so a
    pause that began before the window only contributes the part inside it.
    """
    spans = []
    for start, end in intervals:
        try:
            begin = max(int(start), window_start)
            finish = min(int(end), now)
        except (TypeError, ValueError):
            # A hand-edited or truncated entry is skipped, not fatal.
            continue
        if finish > begin:
            spans.append((begin, finish))
    spans.sort()
    total = 0
    current_start = current_end = None
    for begin, finish in spans:
        if current_end is None or begin > current_end:
            if current_end is not None:
                total += current_end - current_start
            current_start, current_end = begin, finish
        else:
            current_end = max(current_end, finish)
    if current_end is not None:
        total += current_end - current_start
    return float(total)


# --------------------------------------------------------------------------
# the pause lock
# --------------------------------------------------------------------------


def begin_pause(lock_path: str, db_path: str | None = None, *,
                source: str = "pauselock", now: int | None = None) -> bool:
    """Create the pause lock, recording an interval on the absent -> present edge.

    Returns whether an interval was opened. Called by every writer of
    ``.pauselock`` in place of the bare ``open(lock_path, "w")``; a lock that is
    already held opens nothing, so a nested holder does not double-count.
    """
    existed = os.path.exists(lock_path)
    try:
        directory = os.path.dirname(lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(lock_path, "w"):
            pass
    except OSError as exc:
        logging.warning("Failed to create pause lock file %s: %s", lock_path, exc)
        return False
    if existed or db_path is None:
        return False
    try:
        return _open_interval(db_path, source, now)
    except Exception as exc:
        # Best-effort: losing one interval costs a slightly wrong rate, while
        # letting the recording exception escape would stop the work it paces.
        logging.warning("Could not record pause start: %s", exc)
        return False


def end_pause(lock_path: str, db_path: str | None = None, *,
              now: int | None = None) -> bool:
    """Remove the pause lock, closing any open interval.

    Returns whether an interval was closed. The close is attempted even when the
    file is already gone: an interval left open in the record while the file is
    absent would otherwise be subtracted until it swallowed the whole window.
    """
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError as exc:
        logging.warning("Failed to remove pause lock file %s: %s", lock_path, exc)
    if db_path is None:
        return False
    try:
        return _close_interval(db_path, now)
    except Exception as exc:
        logging.warning("Could not record pause end: %s", exc)
        return False


def _open_interval(db_path: str, source: str, now: int | None) -> bool:
    store = StateStore(state_path_for(db_path))
    document = store.load()
    section = document.get(PAUSE_SECTION)
    section = section if isinstance(section, dict) else {}
    if isinstance(section.get("open"), dict):
        return False  # already inside an interval; the file edge is the signal
    timestamp = _now(now)
    section["open"] = {"at": timestamp, "source": str(source)}
    section["closed"] = _prune(section.get("closed") or [], timestamp - KEEP_SECONDS)
    return store.save({PAUSE_SECTION: section})


def _close_interval(db_path: str, now: int | None) -> bool:
    store = StateStore(state_path_for(db_path))
    document = store.load()
    section = document.get(PAUSE_SECTION)
    section = section if isinstance(section, dict) else {}
    opened = section.get("open")
    if not isinstance(opened, dict) or "at" not in opened:
        return False
    timestamp = _now(now)
    closed = list(section.get("closed") or [])
    closed.append([int(opened["at"]), timestamp])
    section["open"] = None
    section["closed"] = _prune(closed, timestamp - KEEP_SECONDS)
    return store.save({PAUSE_SECTION: section})


def paused_seconds(db_path: str, window_start: int, now: int | None = None) -> float:
    """Paused seconds inside ``[window_start, now]``, overlaps merged.

    An interval that is still open counts up to ``now``: a pause in progress is
    paused time, and the rate before it must still be computable.
    """
    if db_path is None:
        return 0.0
    reference = _now(now)
    try:
        section = StateStore(state_path_for(db_path)).load().get(PAUSE_SECTION)
    except Exception as exc:
        logging.warning("Could not read the pause record: %s", exc)
        section = None
    if not isinstance(section, dict):
        return _merge([], window_start, reference)
    intervals = [entry for entry in (section.get("closed") or [])
                 if isinstance(entry, (list, tuple)) and len(entry) == 2]
    opened = section.get("open")
    if isinstance(opened, dict) and "at" in opened:
        intervals.append((opened["at"], reference))
    return _merge(intervals, window_start, reference)


def active_seconds(db_path: str, window_start: int, now: int | None = None) -> float:
    """Seconds of ``[window_start, now]`` the queues were not paused."""
    reference = _now(now)
    wall = max(0, reference - int(window_start))
    return max(0.0, wall - paused_seconds(db_path, window_start, reference))


# --------------------------------------------------------------------------
# the staleness sweep's inflow into the API queue
# --------------------------------------------------------------------------


def record_sweep_inflow(db_path: str, rows: int, now: int | None = None) -> bool:
    """Record one staleness sweep's rowcount, timestamped.

    ``rows`` is the number of items the sweep pushed back into the API queue, as
    the UPDATE's own rowcount reports it. A zero-row sweep writes nothing: it
    changed no inflow, and the document stays small.
    """
    rows = int(rows or 0)
    if rows <= 0:
        return False
    timestamp = _now(now)
    try:
        store = StateStore(state_path_for(db_path))
        section = store.load().get(SWEEP_SECTION)
        section = section if isinstance(section, dict) else {}
        entries = [entry for entry in (section.get("entries") or [])
                   if isinstance(entry, dict) and _as_int(entry.get("at")) is not None]
        entries.append({"at": timestamp, "rows": rows})
        entries = [entry for entry in entries
                   if _as_int(entry["at"]) >= timestamp - KEEP_SECONDS]
        return store.save({SWEEP_SECTION: {"entries": entries}})
    except Exception as exc:
        logging.warning("Could not record staleness-sweep inflow: %s", exc)
        return False


def sweep_inflow(db_path: str, window_start: int, now: int | None = None) -> int:
    """Rows the staleness sweep pushed into the API queue inside the window."""
    if db_path is None:
        return 0
    reference = _now(now)
    try:
        section = StateStore(state_path_for(db_path)).load().get(SWEEP_SECTION)
    except Exception as exc:
        logging.warning("Could not read the staleness-sweep record: %s", exc)
        return 0
    if not isinstance(section, dict):
        return 0
    total = 0
    for entry in section.get("entries") or []:
        at = _as_int(entry.get("at")) if isinstance(entry, dict) else None
        if at is None or not (window_start <= at <= reference):
            continue
        total += _as_int(entry.get("rows")) or 0
    return total
