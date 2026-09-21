"""Verified SQLite database backups and the pull-outbox manifest.

The daemon writes continuously from several threads into a WAL-mode SQLite
database. This module produces *verified* snapshots of that live database and
publishes them into a configurable "outbox" directory, from which a separate
process (typically running on another machine/OS) can pull the newest backup.

A snapshot is only allowed to replace the previous one after it has been proven
good (opens, ``PRAGMA quick_check`` returns ``ok``, non-empty, and its
``workshop_items`` row count matches the source). A failed backup therefore can
never destroy the last known-good copy.

Stdlib only, and deliberately cross-platform: this runs on Windows in
production, so there are no POSIX-only calls, no ``fcntl`` and no ``SIGALRM``.
"""

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone

# Name of the snapshot inside the outbox, relative to ``<outbox_dir>/db``.
DB_SNAPSHOT_REL_PATH = "db/workshop-backup.db"

# Suffix appended to a destination path to build the same-directory temp file.
_TEMP_SUFFIX = ".tmp"

# Serialises the manifest read-modify-write. The backup thread and the failure
# capture writer are both threads of the daemon and both publish into the same
# manifest, which update_manifest's single-writer assumption does not allow.
# A process-level lock is enough for that; two separate processes writing one
# outbox would still need a real file lock, which is not implemented.
_MANIFEST_LOCK = threading.Lock()

# How much headroom beyond the source size we want before starting, best-effort.
_FREE_SPACE_HEADROOM = 16 * 1024 * 1024

# Whether the stale-artifact warning has already been emitted in this process.
# The check runs after every snapshot and the answer does not change, so it is
# said once rather than every hour.
_stale_artefacts_warned = False


class BackupError(Exception):
    """Raised when a snapshot cannot be produced or fails verification.

    A distinct type lets callers tell a backup failure apart from an unrelated
    error without catching a bare ``Exception``.
    """


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _remove_quietly(path: str) -> None:
    """Delete ``path`` if present, ignoring races and permission errors."""
    try:
        os.remove(path)
    # FileNotFoundError means the temp file is already gone — that is the desired
    # end state; other OSErrors are logged just below.
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - platform/AV dependent
        logging.warning("Could not remove temporary file %s: %s", path, exc)


def _sha256_file(path: str) -> str:
    """Streaming SHA-256 of a file's contents."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_source_stats(db_path: str) -> dict:
    """Read a few facts about the source with a read-only view.

    ``rows`` is the live row count and moves under the reader whenever the
    daemon is running; it is reported, never enforced. ``schema_version`` is the
    schema version, which does *not* move while the daemon runs -- migrations
    all happen before the backup thread starts -- so it is the one thing here a
    snapshot can fairly be checked against.

    The connection is marked ``PRAGMA query_only = ON`` so this can never write
    to the live database, and it is short-lived so it does not hold a read lock
    longer than necessary. Using the plain path (rather than a ``file:`` URI)
    keeps this working for Windows drive letters and UNC paths.
    """
    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        conn.execute("PRAGMA query_only = ON;")
        rows = conn.execute("SELECT COUNT(*) FROM workshop_items").fetchone()[0]
        max_api_fetched_at = conn.execute("SELECT MAX(api_fetched_at) FROM workshop_items").fetchone()[0]
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
        return {"rows": rows, "max_api_fetched_at": max_api_fetched_at,
                "schema_version": schema_version}
    finally:
        conn.close()


def verify_snapshot(snapshot_path: str, source_db_path: str) -> dict:
    """Prove a candidate snapshot is a usable copy of the source database.

    Checks, in order: the file exists and is non-empty; it opens; ``PRAGMA
    quick_check`` returns exactly ``ok``; it contains a readable
    ``workshop_items`` table; its schema version matches the source's; and it is
    not empty while the source is not.

    **The row count is deliberately not compared for equality.** The daemon
    writes throughout the copy, so a snapshot taken by ``VACUUM INTO`` is
    legitimately a few hundred rows behind by the time the source is read: 2,107,917
    against 2,108,115 on one observed run, and 54 such failures against 25
    successes in one window of the live log. The snapshot is still a consistent
    point-in-time copy -- that is what ``VACUUM INTO`` guarantees -- so its age
    relative to a moving source says nothing about whether it is valid, and
    failing on it discarded a perfectly good backup for most of the day. The
    count is reported in the log instead, where it is genuinely informative.

    What remains catches a snapshot that is actually wrong rather than merely
    older: a corrupt file, a different schema, or one with no rows where the
    source has them.

    Returns ``{"rows": int, "max_api_fetched_at": ...}`` on success and raises
    :class:`BackupError` on any failure. Tests monkeypatch this function to
    simulate a bad snapshot.
    """
    try:
        size = os.path.getsize(snapshot_path)
    except OSError as exc:
        raise BackupError(f"snapshot {snapshot_path} is not readable: {exc}") from exc
    if size == 0:
        raise BackupError(f"snapshot {snapshot_path} is empty")

    try:
        conn = sqlite3.connect(snapshot_path, timeout=15.0)
        try:
            quick_check = conn.execute("PRAGMA quick_check").fetchall()
            if quick_check != [("ok",)]:
                raise BackupError(f"quick_check failed for {snapshot_path}: {quick_check}")
            counts = conn.execute("SELECT COUNT(*), MAX(api_fetched_at) FROM workshop_items").fetchone()
            snapshot_version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"snapshot {snapshot_path} is not a usable database: {exc}") from exc

    snapshot_rows = counts[0]
    try:
        source = read_source_stats(source_db_path)
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"could not read source database {source_db_path}: {exc}") from exc

    if snapshot_version != source["schema_version"]:
        # The schema does not change while the daemon runs, so this is a fair
        # comparison, and it is the check that answers "is this a copy of our
        # database" without racing the writer.
        raise BackupError(
            f"snapshot schema version {snapshot_version} does not match the "
            f"source's {source['schema_version']}"
        )

    if snapshot_rows == 0 and source["rows"] > 0:
        raise BackupError(
            f"snapshot has no rows but the source has {source['rows']}"
        )

    if snapshot_rows != source["rows"]:
        logging.debug(
            "Snapshot holds %d rows; the source, read later, holds %d (%+d while "
            "the copy ran). A live writer makes this expected.",
            snapshot_rows, source["rows"], source["rows"] - snapshot_rows,
        )

    return {"rows": snapshot_rows, "max_api_fetched_at": counts[1]}


def _warn_about_stale_artefacts(dest_dir: str, dest_path: str) -> None:
    """Report files beside the snapshot that this build does not manage.

    The snapshot layout has changed before. ``<outbox>/db/workshop-backup.db.gz``
    is left over from a build that compressed the snapshot onto the same path;
    no code path writes, reads or removes it, and the debug-capture prune is
    scoped to ``web_downloads/``, ``image_downloads/`` and ``scrapes/`` -- never
    ``db/`` -- so it sits there until someone deletes it by hand -- measured live
    at 672.9 MB against a 1.9 GB current snapshot. Silence about it is how a file
    that size goes unnoticed.

    Only reports. Deleting a file in someone's outbox is their call, not this
    module's, and a backup artifact is exactly the kind of thing they may have
    kept on purpose.

    Warned once per process, because this runs after every snapshot and the
    answer does not change.
    """
    global _stale_artefacts_warned
    if _stale_artefacts_warned:
        return
    _stale_artefacts_warned = True

    base = os.path.basename(dest_path)
    ours = {base}
    # The temp file and whatever SQLite leaves beside it while VACUUM INTO runs,
    # plus the destination's own journal if one is ever created.
    for suffix in (_TEMP_SUFFIX, _TEMP_SUFFIX + "-journal",
                   _TEMP_SUFFIX + "-wal", _TEMP_SUFFIX + "-shm", "-journal"):
        ours.add(base + suffix)

    try:
        entries = [entry for entry in os.scandir(dest_dir) if entry.is_file()]
    except OSError as exc:
        logging.debug("Could not list the snapshot directory %s: %s", dest_dir, exc)
        return

    stale = []
    for entry in entries:
        if entry.name in ours:
            continue
        try:
            stale.append((entry.name, entry.stat().st_size))
        except OSError:
            stale.append((entry.name, 0))
    if not stale:
        return

    total_mb = sum(size for _name, size in stale) / (1024 * 1024)
    logging.warning(
        "The snapshot directory %s holds %d file(s) this build does not manage "
        "(%.1f MB total): %s. Nothing writes, reads or removes them, so they stay "
        "until deleted by hand.",
        dest_dir, len(stale), total_mb,
        ", ".join(name for name, _size in stale),
    )


def _check_free_space(source_db_path: str, dest_dir: str) -> None:
    """Best-effort warning when the destination volume is too tight.

    Never raises: a wrong or unavailable reading must not block an otherwise
    valid backup.
    """
    try:
        source_size = os.path.getsize(source_db_path)
        usage = shutil.disk_usage(dest_dir)
    except OSError as exc:
        logging.debug("Could not check free space for backup: %s", exc)
        return
    if usage.free < source_size + _FREE_SPACE_HEADROOM:
        logging.warning(
            "Low disk space for database backup: %s bytes free, source is %s bytes "
            "(need roughly %s)",
            usage.free, source_size, source_size + _FREE_SPACE_HEADROOM,
        )


def snapshot_database(db_path: str, dest_path: str) -> dict:
    """Snapshot ``db_path`` into ``dest_path``, verifying before replacing.

    ``VACUUM INTO`` writes a consistent, non-locking snapshot of the live WAL
    database, but refuses to overwrite an existing file. We therefore write to a
    temp file *in the same directory as the destination* (deleting a stale temp
    from a previous crash first), verify it, and only then ``os.replace`` it
    into place. ``os.replace`` is atomic only on the same volume, which is why
    the temp file lives next to the destination rather than in the system temp
    directory.

    On any failure the previous ``dest_path`` is left completely untouched, the
    temp file is removed, and :class:`BackupError` is raised. On success the
    metadata dict is returned: ``bytes``, ``sha256``, ``taken_at`` (ISO UTC),
    ``rows`` and ``max_api_fetched_at``.
    """
    dest_dir = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(dest_dir, exist_ok=True)
    temp_path = dest_path + _TEMP_SUFFIX

    # VACUUM INTO refuses to overwrite, so clear any stale temp file from a
    # previous crashed/failed run before starting.
    _remove_quietly(temp_path)
    _check_free_space(db_path, dest_dir)

    taken_at = _utc_now_iso()

    try:
        # isolation_level=None (autocommit) because VACUUM may not run inside a
        # transaction.
        source_conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
        try:
            source_conn.execute("VACUUM INTO ?", (temp_path,))
        finally:
            source_conn.close()
        stats = verify_snapshot(temp_path, db_path)
        size = os.path.getsize(temp_path)
        sha256 = _sha256_file(temp_path)
    except Exception as exc:
        _remove_quietly(temp_path)
        if isinstance(exc, BackupError):
            logging.error("Database snapshot to %s failed verification: %s", dest_path, exc)
            raise
        logging.error("Database snapshot to %s failed: %s", dest_path, exc)
        raise BackupError(f"snapshot of {db_path} failed: {exc}") from exc

    try:
        # Same-directory temp + os.replace => atomic replacement on one volume.
        os.replace(temp_path, dest_path)
    except OSError as exc:
        _remove_quietly(temp_path)
        logging.error("Could not publish database snapshot to %s: %s", dest_path, exc)
        raise BackupError(f"could not publish snapshot to {dest_path}: {exc}") from exc

    logging.info("Database snapshot written to %s (%s bytes, %s rows)", dest_path, size, stats["rows"])
    _warn_about_stale_artefacts(dest_dir, dest_path)
    return {
        "bytes": size,
        "sha256": sha256,
        "taken_at": taken_at,
        "rows": stats["rows"],
        "max_api_fetched_at": stats["max_api_fetched_at"],
    }


def build_db_manifest_entry(outbox_dir: str, dest_path: str, metadata: dict) -> dict:
    """Build the manifest entry describing a freshly written database snapshot.

    ``path`` is relative to ``outbox_dir`` and uses forward slashes so the
    manifest is portable to the Linux-side puller regardless of how the daemon
    represented the path on Windows.
    """
    rel_path = os.path.relpath(dest_path, outbox_dir).replace(os.sep, "/")
    mtime = datetime.fromtimestamp(os.path.getmtime(dest_path), timezone.utc).isoformat()
    return {
        "path": rel_path,
        "kind": "db",
        "bytes": metadata["bytes"],
        "sha256": metadata["sha256"],
        "mtime": mtime,
        "taken_at": metadata["taken_at"],
        "rows": metadata["rows"],
        "max_api_fetched_at": metadata["max_api_fetched_at"],
    }


def update_manifest(outbox_dir: str, entry: dict) -> None:
    """Insert or replace one artifact ``entry`` in ``<outbox_dir>/manifest.json``.

    Serialised across threads: see ``_MANIFEST_LOCK``. Producers that are separate
    processes still need their own coordination.
    """
    with _MANIFEST_LOCK:
        _update_manifest_unlocked(outbox_dir, entry)


def _update_manifest_unlocked(outbox_dir: str, entry: dict) -> None:
    """Insert or replace one artifact ``entry`` in ``<outbox_dir>/manifest.json``.

    The manifest is ``{"generated_at": <ISO UTC>, "artifacts": [entry, ...]}``,
    where each entry is matched by its ``path`` (relative to the outbox, forward
    slashes). An existing entry with the same ``path`` is replaced in place;
    entries for other paths are preserved; a new path is appended.

    The write is atomic (temp file + ``os.replace``), so a reader never observes
    a half-written manifest.

    This helper is intentionally generic and reusable *unchanged* by future
    producers: log shipping, and later structured failure captures of
    unparseable HTTP responses, each write their payload into their own sibling
    subdirectory of ``outbox_dir`` (e.g. ``logs/``, ``failures/``) and then call
    ``update_manifest(outbox_dir, {...})`` with a different ``kind``. They do not
    need to know anything about databases.

    Concurrency is handled by :func:`update_manifest`, which wraps this in
    ``_MANIFEST_LOCK``; call that, not this.
    """
    os.makedirs(outbox_dir, exist_ok=True)
    manifest_path = os.path.join(outbox_dir, "manifest.json")

    manifest = None
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict):
            raise ValueError("manifest root is not an object")
    except FileNotFoundError:
        manifest = None
    except (OSError, ValueError) as exc:
        logging.warning("Could not read manifest %s (%s); rebuilding it", manifest_path, exc)
        manifest = None

    if manifest is None:
        manifest = {"generated_at": _utc_now_iso(), "artifacts": []}

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []

    entry_path = entry.get("path")
    remaining = [
        existing for existing in artifacts
        if not (isinstance(existing, dict) and existing.get("path") == entry_path)
    ]
    remaining.append(entry)
    manifest["artifacts"] = remaining
    manifest["generated_at"] = _utc_now_iso()

    temp_path = manifest_path + _TEMP_SUFFIX
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, manifest_path)
    except OSError as exc:
        _remove_quietly(temp_path)
        logging.error("Could not write manifest %s: %s", manifest_path, exc)
        raise


def remove_manifest_entries(outbox_dir: str, paths) -> list:
    """Drop every manifest entry whose ``path`` is in ``paths``.

    The mirror of :func:`update_manifest` for the outbox lifecycle: a file
    removed from the outbox must lose its entry in the same operation, because
    the puller transfers one file per entry and an entry left behind would make
    the next pull fail on a path that no longer exists. That is the rule the
    owner's pull tool now follows when it fetches by moving, and the rule this
    module follows when housekeeping prunes a debug capture.

    Returns the relative paths actually dropped, so a caller can report them. An
    absent manifest, a corrupt one, an unknown path or an empty ``paths`` is not
    an error: there is simply nothing to drop. Serialised across threads by
    ``_MANIFEST_LOCK``, and written atomically like :func:`update_manifest`, so a
    reader never sees a half-written manifest.
    """
    wanted = {path for path in paths if path}
    if not wanted:
        return []
    with _MANIFEST_LOCK:
        return _remove_manifest_entries_unlocked(outbox_dir, wanted)


def _remove_manifest_entries_unlocked(outbox_dir: str, wanted: set) -> list:
    manifest_path = os.path.join(outbox_dir, "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if not isinstance(manifest, dict):
            raise ValueError("manifest root is not an object")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logging.warning("Could not read manifest %s to remove entries (%s)", manifest_path, exc)
        return []

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        return []

    removed = [
        entry.get("path") for entry in artifacts
        if isinstance(entry, dict) and entry.get("path") in wanted
    ]
    if not removed:
        return []
    manifest["artifacts"] = [
        entry for entry in artifacts
        if not (isinstance(entry, dict) and entry.get("path") in wanted)
    ]
    manifest["generated_at"] = _utc_now_iso()

    temp_path = manifest_path + _TEMP_SUFFIX
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, manifest_path)
    except OSError as exc:
        _remove_quietly(temp_path)
        logging.error("Could not write manifest %s: %s", manifest_path, exc)
        raise
    return removed


class BackupThread(threading.Thread):
    """Periodically snapshot the database into the outbox.

    Follows the daemon's worker convention: construct it, call ``start()``, and
    stop it by setting ``running = False`` then joining. The loop sleeps in
    short increments so a shutdown request is honoured promptly instead of
    waiting out the whole interval.
    """

    def __init__(self, db_path: str, outbox_dir: str, interval_seconds: float):
        super().__init__(daemon=True)
        self.db_path = db_path
        self.outbox_dir = outbox_dir
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.running = True
        self.dest_path = os.path.join(outbox_dir, *DB_SNAPSHOT_REL_PATH.split("/"))
        self._next_due = time.time() + self.interval_seconds

    def snapshot_now(self):
        """Take one snapshot and publish it. Never raises.

        Returns the snapshot metadata on success, ``None`` on failure. Failures
        are logged and swallowed so a backup problem can never break the caller
        (in particular the scrape loop).
        """
        try:
            metadata = snapshot_database(self.db_path, self.dest_path)
            entry = build_db_manifest_entry(self.outbox_dir, self.dest_path, metadata)
            update_manifest(self.outbox_dir, entry)
            logging.info(
                "Database backup published to %s (%s bytes, %s rows)",
                self.dest_path, metadata["bytes"], metadata["rows"],
            )
            return metadata
        except Exception as exc:
            logging.error("Database backup failed (scrape loop unaffected): %s", exc)
            return None

    def run(self):
        logging.info(
            "Backup thread started (interval=%ss, outbox=%s)",
            self.interval_seconds, self.outbox_dir,
        )
        while self.running:
            remaining = self._next_due - time.time()
            if remaining <= 0:
                self.snapshot_now()
                self._next_due = time.time() + self.interval_seconds
                continue
            time.sleep(min(1.0, remaining))
        # No "Backup thread stopped." here: the daemon logs one line per worker
        # as it confirms the join, and this thread's own copy made the owner's
        # log show the same sentence twice. See Daemon._join_workers.
