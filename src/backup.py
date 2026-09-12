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
DB_ARTIFACT_REL_PATH = "db/workshop-backup.db"

# Suffix appended to a destination path to build the same-directory temp file.
_TEMP_SUFFIX = ".tmp"

# How much headroom beyond the source size we want before starting, best-effort.
_FREE_SPACE_HEADROOM = 16 * 1024 * 1024


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
    """Read ``(rows, max_api_fetched_at)`` from the source with a read-only view.

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
        return {"rows": rows, "max_api_fetched_at": max_api_fetched_at}
    finally:
        conn.close()


def verify_snapshot(snapshot_path: str, source_db_path: str) -> dict:
    """Prove a candidate snapshot is a usable copy of the source database.

    Checks, in order: the file exists and is non-empty; it opens; ``PRAGMA
    quick_check`` returns exactly ``ok``; it contains a readable
    ``workshop_items`` table; and its row count matches the source's, read via
    :func:`read_source_stats`.

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
            check = conn.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                raise BackupError(f"quick_check failed for {snapshot_path}: {check}")
            row = conn.execute("SELECT COUNT(*), MAX(api_fetched_at) FROM workshop_items").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"snapshot {snapshot_path} is not a usable database: {exc}") from exc

    snapshot_rows = row[0]
    try:
        source = read_source_stats(source_db_path)
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"could not read source database {source_db_path}: {exc}") from exc

    if snapshot_rows != source["rows"]:
        # The live daemon may have discovered a new item between VACUUM INTO and
        # the source read. Re-read once before declaring a mismatch, so an
        # in-flight insert does not cause a spurious backup failure.
        source = read_source_stats(source_db_path)
        if snapshot_rows != source["rows"]:
            raise BackupError(
                "snapshot row count mismatch: snapshot has "
                f"{snapshot_rows}, source has {source['rows']}"
            )

    return {"rows": snapshot_rows, "max_api_fetched_at": row[1]}


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

    Concurrency: this assumes a single writer per artifact kind, and that no two
    producers write the manifest at the exact same time. If that changes, this
    function needs a cross-process lock around the read-modify-write.
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
    kept = [
        existing for existing in artifacts
        if not (isinstance(existing, dict) and existing.get("path") == entry_path)
    ]
    kept.append(entry)
    manifest["artifacts"] = kept
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
        self.dest_path = os.path.join(outbox_dir, *DB_ARTIFACT_REL_PATH.split("/"))
        self._next_due = time.time() + self.interval_seconds

    def run_now(self):
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
                self.run_now()
                self._next_due = time.time() + self.interval_seconds
                continue
            time.sleep(min(1.0, remaining))
        logging.info("Backup thread stopped.")
