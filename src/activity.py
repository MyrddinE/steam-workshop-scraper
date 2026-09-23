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

**The lock names its owner.** The file is one small JSON record --
``{"owner", "pid", "acquired_at", "source"}`` -- written atomically (a temp file
in the same directory, then :func:`os.replace`), so a reader never sees a
half-written record. Release is scoped: ``end_pause`` removes the file and closes
the interval only when the caller owns it, so one pass cannot free another's
pause, while a *legacy* file -- missing, empty or unparseable, and therefore
unnamed -- stays releasable by anyone, because an upgrade must not lock anyone
out. ``begin_pause`` never steals a held lock; it self-heals one whose recorded
pid is known dead, exactly as the worker polls do through
:func:`reclaim_if_owner_gone`, so a holder that crashed cannot stop the daemon's
web and image work for good. Liveness comes from the platform-correct seam in
``src.daemon_control`` rather than ``os.kill`` here: on Windows a signal-0 probe
terminates the process it asks about.

What is deliberately **not** recorded here is the discovery inflow into the API
queue. ``first_seen_at`` is our clock, but it is not indexed on ``workshop_items``
and a window count over it would be a full scan of a multi-million-row table;
rather than buy that with an index this change does not need, the docs state that
the API queue's net rate subtracts the staleness sweep only.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

from src.daemon_state import StateStore, state_path_for

# The top-level keys this module owns in the daemon state file.
PAUSE_SECTION = "queue_pauses"
SWEEP_SECTION = "queue_sweeps"

#: How long pause intervals and sweep entries are kept. The drain metric's window
#: is one day, so a week is comfortably more than any caller asks for and keeps
#: the document small.
RETENTION_SECONDS = 7 * 86400


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


def _merged_seconds(intervals: list[tuple[int, int]], window_start: int, now: int) -> float:
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


def _pid_alive(pid: int) -> bool | None:
    """Whether a pid is alive, through the platform-correct seam.

    ``src.daemon_control._pid_alive`` owns the single implementation --
    ``OpenProcess`` on Windows, ``os.kill`` on POSIX -- and is imported lazily
    here so this module keeps its light import set and the seam can be replaced
    in a test without touching ``os.kill``, which the Windows branch never
    calls and must not.
    """
    from src.daemon_control import _pid_alive as daemon_pid_alive
    return daemon_pid_alive(pid)


def lock_owner(lock_path: str) -> dict | None:
    """The lock's record, or None when no record can be read.

    A tolerant reader: the file may be missing, empty (the pre-owner format), a
    half-written leftover, or hand-edited. A result of ``None``, a record with
    no ``owner``, and a record with no ``pid`` are all *legacy* for the callers
    here -- unnamed, so releasable by anyone and never reclaimed on liveness.
    """
    try:
        with open(lock_path, encoding="utf-8") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    owner = record.get("owner")
    acquired_at = record.get("acquired_at")
    source = record.get("source")
    return {
        "owner": owner if isinstance(owner, str) and owner else None,
        "pid": _as_int(record.get("pid")),
        "acquired_at": acquired_at if isinstance(acquired_at, (int, float)) else None,
        "source": source if isinstance(source, str) else None,
    }


def _write_lock(lock_path: str, owner: str | None, source: str,
                now: int | None) -> bool:
    """Write the lock record atomically; False when the filesystem refuses it."""
    directory = os.path.dirname(lock_path)
    payload = {
        "owner": owner,
        "pid": os.getpid(),
        "acquired_at": _now(now),
        "source": str(source),
    }
    temp_path = None
    try:
        if directory:
            os.makedirs(directory, exist_ok=True)
        handle_fd, temp_path = tempfile.mkstemp(
            dir=directory or ".", prefix=".pauselock.", suffix=".tmp")
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        # Match the mode a plain `open(path, "w")` used to leave, so the lock
        # does not become owner-only just because it is now written via mkstemp.
        try:
            os.chmod(temp_path, 0o644)
        except OSError:
            pass
        os.replace(temp_path, lock_path)
        return True
    except OSError as exc:
        logging.warning("Failed to create pause lock file %s: %s", lock_path, exc)
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False


def reclaim_if_owner_gone(lock_path: str, db_path: str | None = None, *,
                          now: int | None = None) -> bool:
    """Remove the lock and close its interval when the recorded pid is dead.

    A holder that crashed must not stop the daemon's web and image work for
    good, so both worker polls call this before sleeping. The lock is left alone
    when the pid is alive *or* liveness is unknown (``_pid_alive`` returns
    ``None``), and a legacy lock -- one with no pid to judge -- is never
    reclaimed. Each reclaim logs at WARNING, naming the dead pid and the owner:
    a stranded lock is a bug worth seeing.
    """
    record = lock_owner(lock_path)
    if record is None or record.get("pid") is None:
        return False
    pid = record["pid"]
    if _pid_alive(pid) is not False:
        return False
    logging.warning(
        "Reclaiming pause lock %s: owner %s (pid %s) is no longer running",
        lock_path, record.get("owner") or "unnamed", pid)
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except OSError as exc:
        logging.warning("Failed to remove stale pause lock file %s: %s",
                        lock_path, exc)
        return False
    if db_path is not None:
        try:
            _close_interval(db_path, now)
        except Exception as exc:
            logging.warning("Could not record pause end: %s", exc)
    return True


def begin_pause(lock_path: str, db_path: str | None = None, *,
                source: str = "pauselock", owner: str | None = None,
                now: int | None = None) -> bool:
    """Create the pause lock, recording an interval on the absent -> present edge.

    Returns whether an interval was opened. Called by every writer of
    ``.pauselock`` in place of the bare ``open(lock_path, "w")``; a lock that is
    already held opens nothing, so a nested holder -- the engine inside the TUI
    screen -- does not double-count. A held lock is never stolen; the one
    exception is a lock whose recorded pid is *known dead*, which is reclaimed
    first so the acquisition self-heals exactly as the worker polls do.
    """
    existed = os.path.exists(lock_path)
    if existed:
        record = lock_owner(lock_path)
        # No record, no owner or no pid means a legacy (unnamed) lock: it is
        # held and left alone. Two writers must not silently swap owners.
        if record is None or record.get("owner") is None or record.get("pid") is None:
            return False
        if not reclaim_if_owner_gone(lock_path, db_path, now=now):
            return False
        # The dead holder's lock is gone and its interval closed; this call is
        # now the absent -> present edge and opens the one interval it owns.
    if not _write_lock(lock_path, owner, source, now):
        return False
    if db_path is None:
        return False
    try:
        return _open_interval(db_path, source, now)
    except Exception as exc:
        # Best-effort: losing one interval costs a slightly wrong rate, while
        # letting the recording exception escape would stop the work it paces.
        logging.warning("Could not record pause start: %s", exc)
        return False


def end_pause(lock_path: str, db_path: str | None = None, *,
              owner: str | None = None, now: int | None = None) -> bool:
    """Remove the pause lock, closing any open interval.

    Release is scoped to the caller's owner: a named lock belonging to someone
    else is left in place and the refusal is logged at INFO naming both owners,
    so an investigation has a trail. A legacy (unnamed) lock is releasable by
    anyone, and a missing file stays an idempotent success -- the close is
    attempted even then, because an interval left open while the file is absent
    would otherwise be subtracted until it swallowed the whole window.
    """
    record = lock_owner(lock_path)
    if record is not None:
        held = record.get("owner")
        if held is not None and held != owner:
            logging.info(
                "Pause lock %s is owned by %s, not %s; leaving it in place",
                lock_path, held, owner or "unnamed")
            return False
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


def _open_value(section: dict):
    """The open interval, under the current or the legacy key.

    Batch 7 renamed the state-file keys ``open`` and ``closed`` to
    ``open_interval`` and ``closed_intervals``; a file written by an older
    build is still read under the old spelling.
    """
    if "open_interval" in section:
        return section["open_interval"]
    return section.get("open")


def _closed_value(section: dict) -> list:
    """The closed intervals, under the current or the legacy key."""
    if "closed_intervals" in section:
        return list(section["closed_intervals"] or [])
    return list(section.get("closed") or [])


def _open_interval(db_path: str, source: str, now: int | None) -> bool:
    store = StateStore(state_path_for(db_path))
    document = store.load()
    section = document.get(PAUSE_SECTION)
    section = section if isinstance(section, dict) else {}
    if isinstance(_open_value(section), dict):
        return False  # already inside an interval; the file edge is the signal
    timestamp = _now(now)
    section["open_interval"] = {"at": timestamp, "source": str(source)}
    section["closed_intervals"] = _prune(
        _closed_value(section), timestamp - RETENTION_SECONDS)
    # Drop the legacy spelling so the file converges on the current keys.
    section.pop("open", None)
    section.pop("closed", None)
    return store.save({PAUSE_SECTION: section})


def _close_interval(db_path: str, now: int | None) -> bool:
    store = StateStore(state_path_for(db_path))
    document = store.load()
    section = document.get(PAUSE_SECTION)
    section = section if isinstance(section, dict) else {}
    opened = _open_value(section)
    if not isinstance(opened, dict) or "at" not in opened:
        return False
    timestamp = _now(now)
    closed = _closed_value(section)
    closed.append([int(opened["at"]), timestamp])
    section["open_interval"] = None
    section["closed_intervals"] = _prune(closed, timestamp - RETENTION_SECONDS)
    section.pop("open", None)
    section.pop("closed", None)
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
        return _merged_seconds([], window_start, reference)
    intervals = [entry for entry in _closed_value(section)
                 if isinstance(entry, (list, tuple)) and len(entry) == 2]
    opened = _open_value(section)
    if isinstance(opened, dict) and "at" in opened:
        intervals.append((opened["at"], reference))
    return _merged_seconds(intervals, window_start, reference)


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
                   if _as_int(entry["at"]) >= timestamp - RETENTION_SECONDS]
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
