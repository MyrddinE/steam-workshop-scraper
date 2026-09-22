"""A persisted cache of the web UI's Wilson percentile cutoffs.

``/api/cutoffs`` answers with ten percentiles from
:func:`src.database.compute_wilson_cutoffs`, which takes seconds on a large
database -- 28.3 s on the owner's live one. The page asks once per browser
session, because its own cache is JavaScript module state that a reload throws
away, so the server paid that cost again on every reload. The values only move
when the query moves or a migration rewrites the score columns, so the web
server keeps the last answer in a small JSON file beside the database -- the
same place, and the same atomic-write discipline, as the daemon's
``.daemon_state.yaml`` (:mod:`src.daemon_state`).

The key is a hash of the canonical ``(filters, overlay, user_version)``, so a
schema change drops the entry. The value is the ten cutoff values and the time
they were computed. An entry older than ``daemon.cutoffs_cache_seconds``
(default 86400 s, the owner's number) is recomputed; ``0`` (or any value at or
below zero) disables the cache entirely.

Nothing here raises for a missing, unreadable or corrupt file: all three mean
"no usable entry", so the caller recomputes and rewrites. A failed write is
logged and ignored -- the entry is a shortcut, and losing it costs one
recompute, not a request.

Concurrency is **last-writer-wins on an atomic write**. Each write builds a
unique same-directory temp file and publishes it with ``os.replace``, so a
concurrent writer can replace the entry but never interleave with it, and a
reader sees the whole old document or the whole new one. Only the write itself
takes a short process-local lock (two threads must not share one temp file);
nothing is locked across the computation, so two different queries are not
serialized behind each other and every request is answered from its own query.
The cost of a collision is a second computation, not a wrong answer, because
the value is never read from the file being written.
"""

import hashlib
import json
import logging
import os
import tempfile
import threading
import time

# Beside the database, named for what writes it. A dotfile because it is state,
# not something the operator is expected to edit.
DEFAULT_CACHE_NAME = ".cutoffs_cache.json"

#: The owner's TTL: a day. Long enough that a reload is never a recompute,
#: short enough that settled-status churn in the population is re-read daily.
DEFAULT_TTL_SECONDS = 86400

# Serializes the temp-file-plus-replace within this process. Cross-process
# safety comes from the unique temp name and the atomic replace, not this lock.
_write_lock = threading.Lock()


def cache_path_for(db_path: str) -> str:
    """The cache file that belongs to ``db_path``'s installation."""
    directory = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(directory, DEFAULT_CACHE_NAME)


def configured_ttl_seconds(daemon_config) -> float:
    """``daemon.cutoffs_cache_seconds`` as a float, defaulting to a day.

    A missing key, an explicit ``null`` or a value that is not a number all mean
    the default; a bad value is warned about rather than silently ignored,
    because it names a setting the operator thought was in force. A value at or
    below zero is returned as-is and disables caching at the call site.
    """
    daemon_config = daemon_config if isinstance(daemon_config, dict) else {}
    value = daemon_config.get("cutoffs_cache_seconds", DEFAULT_TTL_SECONDS)
    if value is None:
        return float(DEFAULT_TTL_SECONDS)
    try:
        return float(value)
    except (TypeError, ValueError):
        logging.warning(
            "Ignoring daemon.cutoffs_cache_seconds=%r: expected a number", value)
        return float(DEFAULT_TTL_SECONDS)


def query_key(filters, subscribed_overlay, user_version: int) -> str:
    """A stable hash of the query the percentiles describe.

    Canonical means ``sort_keys``: the same filters in another dict key order are
    the same query, as the client's own ``JSON.stringify`` key is not. The
    schema version is folded in so a migration that rewrites the score columns
    cannot leave a stale entry usable.
    """
    canonical = json.dumps(
        {
            "filters": filters or [],
            "subscribed": subscribed_overlay,
            "user_version": user_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load(path: str, key: str, ttl_seconds: float):
    """Return ``(cutoffs, age_seconds)`` for a fresh entry matching ``key``.

    ``None`` means there is no usable entry -- absent, unreadable, malformed, a
    different key, or older than the TTL. None of those is an error.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logging.warning("Ignoring unreadable cutoffs cache %s: %s", path, exc)
        return None

    if not isinstance(document, dict) or document.get("key") != key:
        return None
    cutoffs = document.get("cutoffs")
    if not isinstance(cutoffs, dict) or not cutoffs:
        return None
    try:
        computed_at = float(document["computed_at"])
    except (KeyError, TypeError, ValueError):
        return None

    age = _now() - computed_at
    if age < 0:
        # A clock that moved backwards: the entry cannot be older than the
        # request, so treat it as just written rather than discarding it.
        age = 0.0
    if ttl_seconds > 0 and age > ttl_seconds:
        return None
    return cutoffs, age


def store(path: str, key: str, cutoffs: dict) -> bool:
    """Publish ``cutoffs`` under ``key``. Never raises; returns whether it wrote."""
    document = {"key": key, "computed_at": _now(), "cutoffs": cutoffs}
    try:
        payload = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
        _write_atomic(path, payload)
        return True
    except Exception as exc:
        logging.warning("Could not write cutoffs cache %s: %s", path, exc)
        return False


def _write_atomic(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` through a unique same-directory temp file.

    The temp file must share the directory for ``os.replace`` to be an atomic
    rename rather than a copy, and it is created with ``mkstemp`` so two writers
    -- in this process or another -- never share one.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with _write_lock:
        handle_fd, temp_path = tempfile.mkstemp(
            dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
        try:
            with os.fdopen(handle_fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise


def _now() -> float:
    """The clock, behind one function so a test can move it deliberately."""
    return time.time()
