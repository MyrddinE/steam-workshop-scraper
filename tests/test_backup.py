"""Tests for verified database backups and the outbox manifest."""
import hashlib
import json
import logging
import os
import sqlite3
import types
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import yaml

from src import backup
from src.backup import (
    BackupError,
    BackupThread,
    build_db_manifest_entry,
    snapshot_database,
    update_manifest,
    verify_snapshot,
)
from src.daemon_state import StateStore, state_path_for
from src.database import get_connection, initialize_database, insert_or_update_item


def _dest(tmp_path) -> str:
    return str(tmp_path / "outbox" / "db" / "workshop-backup.db")


# ── snapshot_database ────────────────────────────────────────────────────────

def test_snapshot_of_populated_db_succeeds_and_verifies(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "A", "api_fetched_at": 111})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "B", "api_fetched_at": 222})

    dest = _dest(tmp_path)
    meta = snapshot_database(db_path, dest)

    assert os.path.isfile(dest)
    assert meta["bytes"] == os.path.getsize(dest)
    assert meta["bytes"] > 0
    assert meta["rows"] == 2
    assert meta["max_api_fetched_at"] == 222
    assert meta["sha256"] == hashlib.sha256(open(dest, "rb").read()).hexdigest()
    # taken_at is an ISO-8601 timestamp
    assert datetime.fromisoformat(meta["taken_at"]) is not None

    # The snapshot is independently usable and passes the same verification.
    assert verify_snapshot(dest, db_path) == {"rows": 2, "max_api_fetched_at": 222}
    conn = sqlite3.connect(dest)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM workshop_items").fetchone()[0] == 2
    finally:
        conn.close()


def test_snapshot_verifier_rejects_empty_file(db_path, tmp_path):
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with pytest.raises(BackupError):
        verify_snapshot(str(empty), db_path)


def test_snapshot_verifier_rejects_an_empty_snapshot_of_a_populated_source(db_path, tmp_path):
    """The residual of the old row-count check, without the part that raced.

    An empty snapshot of a database that has rows is wrong in a way no amount of
    live writing explains, so it still fails; a snapshot that is merely *behind*
    does not, which is the next test.
    """
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 1})

    other_db = str(tmp_path / "other.db")
    initialize_database(other_db)  # same schema, zero rows
    dest = str(tmp_path / "snap.db")
    snapshot_database(other_db, dest)

    with pytest.raises(BackupError, match="no rows but the source has 1"):
        verify_snapshot(dest, db_path)


def test_a_snapshot_taken_under_a_live_writer_still_verifies(db_path, tmp_path):
    """The bug this replaced: a valid snapshot discarded for being a few rows old.

    The daemon writes throughout the copy, so the source is always read later
    than the snapshot was taken and can hold more rows. That says nothing about
    whether the snapshot is valid -- ``VACUUM INTO`` guarantees a consistent
    point-in-time copy -- and treating it as a failure discarded a good backup
    on most runs. Observed live: 2,107,917 against 2,108,115, and 54 failures to
    25 successes in one window.
    """
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "A", "api_fetched_at": 1})
    dest = str(tmp_path / "snap.db")
    snapshot_database(db_path, dest)

    # The daemon carries on while the copy is validated.
    for wid in range(2, 12):
        insert_or_update_item(db_path, {"workshop_id": wid, "api_fetched_at": wid})

    # It verifies, and it reports what *it* holds, not what the source holds now.
    assert verify_snapshot(dest, db_path) == {"rows": 1, "max_api_fetched_at": 1}


def test_snapshot_verifier_rejects_a_different_schema_version(db_path, tmp_path):
    """Schema is stable while the daemon runs, so this comparison is fair.

    It is the check that answers "is this a copy of our database" without racing
    the writer, and it replaces the count equality that could not.
    """
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 1})
    dest = str(tmp_path / "snap.db")
    snapshot_database(db_path, dest)

    conn = sqlite3.connect(dest)
    try:
        conn.execute("PRAGMA user_version = 3")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(BackupError, match="schema version 3 does not match"):
        verify_snapshot(dest, db_path)


def test_failed_verification_leaves_previous_destination_untouched(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 10})
    dest = _dest(tmp_path)
    snapshot_database(db_path, dest)

    before_bytes = open(dest, "rb").read()
    before_mtime = os.path.getmtime(dest)

    with patch("src.backup.verify_snapshot", side_effect=BackupError("simulated failure")):
        with pytest.raises(BackupError, match="simulated failure"):
            snapshot_database(db_path, dest)

    # Previous backup is byte-identical, mtime untouched, and no temp left behind.
    assert open(dest, "rb").read() == before_bytes
    assert os.path.getmtime(dest) == before_mtime
    assert not os.path.exists(dest + ".tmp")
    assert os.listdir(os.path.dirname(dest)) == [os.path.basename(dest)]


def test_snapshot_database_handles_pre_existing_stale_temp(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 7, "api_fetched_at": 1})
    dest = _dest(tmp_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest + ".tmp", "wb") as handle:
        handle.write(b"stale garbage from a previous crash")

    meta = snapshot_database(db_path, dest)

    assert meta["rows"] == 1
    assert os.path.isfile(dest)
    assert not os.path.exists(dest + ".tmp")


# ── outbox manifest ──────────────────────────────────────────────────────────

def test_manifest_entry_updated_in_place_and_others_preserved(db_path, tmp_path):
    outbox = str(tmp_path / "outbox")
    dest = _dest(tmp_path)

    # An unrelated producer's entry must survive untouched.
    update_manifest(outbox, {"path": "logs/daemon.log", "kind": "log", "bytes": 5})

    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 100})
    meta1 = snapshot_database(db_path, dest)
    update_manifest(outbox, build_db_manifest_entry(outbox, dest, meta1))

    insert_or_update_item(db_path, {"workshop_id": 2, "api_fetched_at": 200})
    meta2 = snapshot_database(db_path, dest)
    update_manifest(outbox, build_db_manifest_entry(outbox, dest, meta2))

    with open(os.path.join(outbox, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)

    assert manifest["generated_at"]
    entries = {entry["path"]: entry for entry in manifest["artifacts"]}
    assert set(entries) == {"logs/daemon.log", "db/workshop-backup.db"}
    assert entries["logs/daemon.log"]["kind"] == "log"

    db_entry = entries["db/workshop-backup.db"]
    assert db_entry["kind"] == "db"
    assert db_entry["path"] == "db/workshop-backup.db"
    assert "\\" not in db_entry["path"]
    assert db_entry["bytes"] == meta2["bytes"] == os.path.getsize(dest)
    assert db_entry["sha256"] == meta2["sha256"]
    assert db_entry["sha256"] == hashlib.sha256(open(dest, "rb").read()).hexdigest()
    assert db_entry["rows"] == 2
    assert db_entry["taken_at"] == meta2["taken_at"]
    assert db_entry["mtime"]
    # Updated in place, not appended as a duplicate.
    paths = [entry["path"] for entry in manifest["artifacts"]]
    assert paths.count("db/workshop-backup.db") == 1
    assert not os.path.exists(os.path.join(outbox, "manifest.json.tmp"))


def test_update_manifest_recovers_from_corrupt_manifest(tmp_path):
    outbox = str(tmp_path / "outbox")
    os.makedirs(outbox, exist_ok=True)
    with open(os.path.join(outbox, "manifest.json"), "w", encoding="utf-8") as handle:
        handle.write("{not json")

    update_manifest(outbox, {"path": "db/x.db", "kind": "db"})

    with open(os.path.join(outbox, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert [entry["path"] for entry in manifest["artifacts"]] == ["db/x.db"]


def test_remove_manifest_entries_drops_only_the_named_paths(tmp_path):
    outbox = str(tmp_path / "outbox")
    update_manifest(outbox, {"path": "web_downloads/a.json", "kind": "web_download"})
    update_manifest(outbox, {"path": "web_downloads/a.body", "kind": "web_download"})
    update_manifest(outbox, {"path": "failures/g/a-1.json", "kind": "failure"})

    removed = backup.remove_manifest_entries(
        outbox, ["web_downloads/a.json", "web_downloads/a.body"])

    assert sorted(removed) == ["web_downloads/a.body", "web_downloads/a.json"]
    with open(os.path.join(outbox, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert [entry["path"] for entry in manifest["artifacts"]] == ["failures/g/a-1.json"]
    assert not os.path.exists(os.path.join(outbox, "manifest.json.tmp"))


def test_remove_manifest_entries_is_silent_for_unknown_paths(tmp_path):
    outbox = str(tmp_path / "outbox")
    update_manifest(outbox, {"path": "db/x.db", "kind": "db"})

    assert backup.remove_manifest_entries(outbox, ["web_downloads/never-existed.json"]) == []
    assert backup.remove_manifest_entries(outbox, []) == []
    with open(os.path.join(outbox, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert [entry["path"] for entry in manifest["artifacts"]] == ["db/x.db"]


def test_remove_manifest_entries_tolerates_a_missing_manifest(tmp_path):
    assert backup.remove_manifest_entries(str(tmp_path / "outbox"), ["a"]) == []


# ── BackupThread ─────────────────────────────────────────────────────────────

def test_backup_thread_snapshot_now_swallows_failure(tmp_path):
    worker = BackupThread(str(tmp_path / "test.db"), str(tmp_path / "outbox"), 60)

    with patch("src.backup.snapshot_database", side_effect=RuntimeError("boom")):
        assert worker.snapshot_now() is None  # logged and swallowed, no exception


def test_backup_thread_snapshot_now_publishes_snapshot_and_manifest(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 5})
    outbox = str(tmp_path / "outbox")
    worker = BackupThread(db_path, outbox, 60)

    meta = worker.snapshot_now()

    assert meta is not None
    assert os.path.isfile(os.path.join(outbox, "db", "workshop-backup.db"))
    with open(os.path.join(outbox, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["artifacts"][0]["path"] == "db/workshop-backup.db"


# ── daemon wiring ────────────────────────────────────────────────────────────

def test_daemon_runs_batch_normally_with_backup_config_absent(tmp_path):
    """Feature-off: wiring must not change default daemon behaviour."""
    from src.daemon import Daemon

    db = str(tmp_path / "test.db")
    initialize_database(db)
    config = {
        "database": {"path": db},
        "api": {"key": "K"},
        "daemon": {"api_batch_size": 1, "target_appids": [1]},
    }
    daemon = Daemon(config, config_path=str(tmp_path / "config.yaml"))
    assert daemon._backup_worker is None

    with patch("src.daemon.get_next_items_to_fetch",
               return_value=[{"workshop_id": 1, "api_priority": 0, "fetch_status": 200}]) as mock_batch, \
         patch("src.daemon.get_workshop_details_batch",
               return_value={1: {"title": "T", "status": 200, "publishedfileid": 1}}) as mock_api, \
         patch("src.daemon.raise_web_scrape_priority"), \
         patch("src.daemon.raise_image_priority"), \
         patch("src.daemon.get_creator", return_value=None):
        daemon.process_batch()

    mock_batch.assert_called_once()
    mock_api.assert_called_once()
    conn = get_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM workshop_items").fetchone()[0] == 1
    finally:
        conn.close()


def test_daemon_constructs_backup_worker_when_configured(tmp_path):
    from src.daemon import Daemon

    db = str(tmp_path / "test.db")
    initialize_database(db)
    config = {
        "database": {"path": db},
        "api": {"key": "K"},
        "daemon": {
            "api_batch_size": 1,
            "target_appids": [1],
            "outbox_dir": str(tmp_path / "outbox"),
            "backup_interval_seconds": 30,
        },
    }
    daemon = Daemon(config, config_path=str(tmp_path / "config.yaml"))

    assert isinstance(daemon._backup_worker, BackupThread)
    assert daemon._backup_worker.running is True

    daemon.handle_shutdown(None, None)
    assert daemon._backup_worker.running is False


def test_daemon_run_takes_final_snapshot_on_shutdown(tmp_path):
    from src.daemon import Daemon

    db = str(tmp_path / "test.db")
    initialize_database(db)
    insert_or_update_item(db, {"workshop_id": 1, "api_fetched_at": 42})
    outbox = str(tmp_path / "outbox")
    config = {
        "database": {"path": db},
        "api": {"key": "K"},
        "daemon": {
            "api_batch_size": 1,
            "target_appids": [1],
            "outbox_dir": outbox,
            "backup_interval_seconds": 3600,
        },
    }
    # Replace the real network workers with no-op mocks; the final snapshot is
    # taken after they are joined, which is what we assert. The mocks must
    # answer ``is_alive()`` with False -- the daemon now asks each worker
    # whether it really stopped before it will trust the database for a
    # snapshot, and a bare MagicMock's truthy answer reads as "still running".
    with patch("src.daemon.TranslatorThread") as translator_cls, \
         patch("src.daemon.WebScraperThread") as web_cls, \
         patch("src.daemon.ImageDownloadThread") as image_cls:
        for worker_cls in (translator_cls, web_cls, image_cls):
            worker_cls.return_value.is_alive.return_value = False
        daemon = Daemon(config, config_path=str(tmp_path / "config.yaml"))
        with patch.object(Daemon, "process_batch",
                          side_effect=lambda: setattr(daemon, "running", False)):
            daemon.run()

    assert daemon._backup_worker.running is False
    dest = os.path.join(outbox, "db", "workshop-backup.db")
    assert os.path.isfile(dest)
    with open(os.path.join(outbox, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert [entry["path"] for entry in manifest["artifacts"]] == ["db/workshop-backup.db"]
    assert manifest["artifacts"][0]["rows"] == 1


# ── artifacts this build does not manage ─────────────────────────────────────

def _plant_stale_artifact(dest: str) -> str:
    """An artifact from a previous layout, beside the current snapshot."""
    stale = os.path.join(os.path.dirname(dest), "workshop-backup.db.gz")
    with open(stale, "wb") as handle:
        handle.write(b"x" * 2048)
    return stale


def test_a_file_this_build_does_not_manage_is_reported(monkeypatch, db_path, tmp_path, caplog):
    """Silence is how a file that size goes unnoticed.

    `<outbox>/db/workshop-backup.db.gz` is left from a build that compressed the
    snapshot onto the same path. No code path writes, reads or removes it, and
    nothing prunes the outbox, so it stays until someone deletes it by hand --
    measured live at 672.9 MB against a 1.9 GB current snapshot.
    """
    monkeypatch.setattr(backup, "_stale_artefacts_warned", False)
    dest = _dest(tmp_path)
    snapshot_database(db_path, dest)
    stale = _plant_stale_artifact(dest)

    monkeypatch.setattr(backup, "_stale_artefacts_warned", False)
    with caplog.at_level(logging.WARNING):
        snapshot_database(db_path, dest)

    assert "does not manage" in caplog.text
    assert "workshop-backup.db.gz" in caplog.text
    assert os.path.isfile(stale), "reported, never deleted -- it is a backup"


def test_a_clean_snapshot_directory_reports_nothing(monkeypatch, db_path, tmp_path, caplog):
    monkeypatch.setattr(backup, "_stale_artefacts_warned", False)
    dest = _dest(tmp_path)
    snapshot_database(db_path, dest)

    monkeypatch.setattr(backup, "_stale_artefacts_warned", False)
    with caplog.at_level(logging.WARNING):
        snapshot_database(db_path, dest)

    assert "does not manage" not in caplog.text, (
        "the snapshot's own file and its temp must not be reported as foreign")


def test_the_stale_artifact_report_is_said_once(monkeypatch, db_path, tmp_path, caplog):
    """It runs after every snapshot, and the answer does not change."""
    monkeypatch.setattr(backup, "_stale_artefacts_warned", False)
    dest = _dest(tmp_path)
    snapshot_database(db_path, dest)
    _plant_stale_artifact(dest)

    monkeypatch.setattr(backup, "_stale_artefacts_warned", False)
    with caplog.at_level(logging.WARNING):
        snapshot_database(db_path, dest)
        snapshot_database(db_path, dest)

    assert caplog.text.count("does not manage") == 1


# ── free space: refuse before starting a copy that cannot fit ─────────────────

def _usage_with_free(free: int):
    """A stand-in for ``shutil.disk_usage``'s named tuple with ``free`` set."""
    return types.SimpleNamespace(total=free, used=0, free=free)


def _needed(db_path) -> int:
    return os.path.getsize(db_path) + backup._FREE_SPACE_HEADROOM


def test_an_unreadable_volume_does_not_block_the_snapshot(monkeypatch, db_path, tmp_path):
    """No evidence proceeds: an unavailable reading must not refuse a backup."""
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 10})
    dest = _dest(tmp_path)

    def _unavailable(_dir):
        raise OSError("statvfs unavailable")

    monkeypatch.setattr(backup.shutil, "disk_usage", _unavailable)

    meta = snapshot_database(db_path, dest)

    assert meta["rows"] == 1
    assert os.path.isfile(dest)


def test_an_unusable_free_space_reading_does_not_block_the_snapshot(monkeypatch, db_path, tmp_path):
    """A reading that cannot be turned into a number is no evidence either.

    The pre-change comparison raised ``TypeError`` here, which is not an
    ``OSError`` and so escaped the guard; the snapshot must not be refused, or
    aborted, on a reading that says nothing.
    """
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 10})
    dest = _dest(tmp_path)
    monkeypatch.setattr(
        backup.shutil, "disk_usage",
        lambda _dir: types.SimpleNamespace(total=None, used=None, free=None))

    meta = snapshot_database(db_path, dest)

    assert meta["rows"] == 1
    assert os.path.isfile(dest)


def test_exactly_enough_free_space_proceeds(monkeypatch, db_path, tmp_path):
    """The boundary is inclusive: exactly source size + headroom is enough."""
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 10})
    dest = _dest(tmp_path)
    free = _needed(db_path)
    monkeypatch.setattr(backup.shutil, "disk_usage",
                        lambda _dir: _usage_with_free(free))

    meta = snapshot_database(db_path, dest)

    assert meta["rows"] == 1
    assert os.path.isfile(dest)


def test_a_snapshot_the_volume_cannot_hold_is_refused(monkeypatch, db_path, tmp_path):
    """Positive evidence of no room refuses; the previous snapshot survives.

    A snapshot is written as a complete second copy in the destination directory
    before ``os.replace`` publishes it, so one run needs room for roughly two
    copies. Starting the write anyway spends the run and then fails partway; the
    owner's answer is to refuse up front and leave the last good copy in place.
    """
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 10})
    dest = _dest(tmp_path)
    snapshot_database(db_path, dest)  # the previous, known-good snapshot

    before = open(dest, "rb").read()
    before_mtime = os.path.getmtime(dest)
    source_size = os.path.getsize(db_path)
    free = source_size + backup._FREE_SPACE_HEADROOM - 1  # one byte short
    monkeypatch.setattr(backup.shutil, "disk_usage",
                        lambda _dir: _usage_with_free(free))

    with pytest.raises(BackupError) as excinfo:
        snapshot_database(db_path, dest)

    message = str(excinfo.value)
    assert str(free) in message           # the free bytes
    assert str(source_size) in message    # the source size
    assert str(source_size + backup._FREE_SPACE_HEADROOM) in message  # the total
    # The previous snapshot is byte-identical, its mtime untouched, and nothing
    # in the destination directory was added, removed or replaced.
    assert open(dest, "rb").read() == before
    assert os.path.getmtime(dest) == before_mtime
    assert os.listdir(os.path.dirname(dest)) == [os.path.basename(dest)]


def test_one_byte_short_of_the_boundary_refuses(monkeypatch, db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 10})
    dest = _dest(tmp_path)
    monkeypatch.setattr(backup.shutil, "disk_usage",
                        lambda _dir: _usage_with_free(_needed(db_path) - 1))

    with pytest.raises(BackupError, match="refusing to snapshot"):
        snapshot_database(db_path, dest)


def test_a_refusal_leaves_a_stale_temp_file_untouched(monkeypatch, db_path, tmp_path):
    """The guard runs before the stale-temp cleanup, so a refusal changes nothing."""
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 10})
    dest = _dest(tmp_path)
    snapshot_database(db_path, dest)
    stale = dest + ".tmp"
    with open(stale, "wb") as handle:
        handle.write(b"stale garbage from a previous crash")

    monkeypatch.setattr(backup.shutil, "disk_usage",
                        lambda _dir: _usage_with_free(_needed(db_path) - 1))
    with pytest.raises(BackupError):
        snapshot_database(db_path, dest)

    assert os.path.isfile(stale), "a refusal must not tidy the destination"
    assert open(stale, "rb").read() == b"stale garbage from a previous crash"


def test_snapshot_now_refusal_returns_none_and_logs_once(monkeypatch, db_path, tmp_path, caplog):
    """One line, from ``snapshot_now``; the guard itself must not also warn."""
    insert_or_update_item(db_path, {"workshop_id": 1, "api_fetched_at": 10})
    outbox = str(tmp_path / "outbox")
    worker = BackupThread(db_path, outbox, 60)
    monkeypatch.setattr(backup.shutil, "disk_usage",
                        lambda _dir: _usage_with_free(_needed(db_path) - 1))

    with caplog.at_level(logging.ERROR):
        assert worker.snapshot_now() is None

    assert caplog.text.count("Database backup failed (scrape loop unaffected)") == 1
    assert not os.path.exists(worker.dest_path)


# ── the schedule survives a restart ──────────────────────────────────────────
#
# `_next_due` used to live only in memory, so every daemon start deferred the
# first snapshot by a fresh `backup_interval_seconds`; a daemon restarted more
# often than that never snapshotted at all. The moment a snapshot verified and
# was published is now recorded in the daemon state file beside the database,
# and the next due time is derived from it on start. The clock is driven here
# rather than slept on, so these pin timing without waiting for it.

# Read back through literal section/key names, not the module's constants, so
# the test states the on-disk contract instead of echoing the implementation.
_BACKUP_STATE_SECTION = "backup"
_BACKUP_STATE_KEY = "last_snapshot_at"
_STARTUP_GRACE_SECONDS = 5.0


class _FakeClock:
    """A clock the test owns; ``sleep`` advances it instead of blocking."""

    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _state_file(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), ".daemon_state.yaml")


def _stored_record(db_path: str):
    """The recorded last-snapshot time, read straight from the YAML file."""
    path = _state_file(db_path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    section = document.get(_BACKUP_STATE_SECTION) or {}
    return section.get(_BACKUP_STATE_KEY) if isinstance(section, dict) else None


def _seed_record(db_path: str, timestamp: float) -> None:
    StateStore(state_path_for(db_path)).save(
        {_BACKUP_STATE_SECTION: {_BACKUP_STATE_KEY: _iso(timestamp)}})


def _drive(worker: BackupThread, clock: _FakeClock, until: float) -> None:
    """Run the worker's loop on ``clock`` until the clock reaches ``until``."""
    original_sleep = clock.sleep

    def sleep_and_stop(seconds: float) -> None:
        clock.now += seconds
        if clock.now >= until:
            worker.running = False

    clock.sleep = sleep_and_stop
    try:
        worker.run()
    finally:
        clock.sleep = original_sleep


def _count_snapshots(monkeypatch, clock: _FakeClock) -> list:
    """Wrap the real snapshot call, recording the fake-clock moment of each."""
    moments: list = []
    real = backup.snapshot_database

    def counted(db, dest):
        moments.append(clock.now)
        return real(db, dest)

    monkeypatch.setattr(backup, "snapshot_database", counted)
    return moments


def _populate(db_path) -> None:
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "A", "api_fetched_at": 1})


def _raise_boom(*_args, **_kwargs):
    raise RuntimeError("boom")


def test_a_successful_snapshot_records_when_it_happened(monkeypatch, db_path, tmp_path):
    """The record is the schedule's memory, so a success must leave one."""
    _populate(db_path)
    clock = _FakeClock()
    monkeypatch.setattr(backup, "time", clock)
    worker = BackupThread(db_path, str(tmp_path / "outbox"), 3600)

    assert worker.snapshot_now() is not None

    record = _stored_record(db_path)
    assert record is not None
    assert datetime.fromisoformat(record).timestamp() == clock.now


def test_a_record_one_interval_old_snapshots_shortly_after_start(monkeypatch, db_path, tmp_path):
    """A record that is already overdue must not wait a fresh interval.

    This is the defect: `_next_due` was `start + interval`, ignoring the record,
    so this thread would not snapshot until a full interval after start.
    """
    _populate(db_path)
    clock = _FakeClock()
    monkeypatch.setattr(backup, "time", clock)
    interval = 3600
    _seed_record(db_path, clock.now - interval)
    outbox = str(tmp_path / "outbox")
    worker = BackupThread(db_path, outbox, interval)

    _drive(worker, clock, clock.now + 10)

    assert os.path.isfile(os.path.join(outbox, "db", "workshop-backup.db")), (
        "the record is one interval old, so the snapshot is due at once (after the grace), "
        "not a fresh interval after start")
    assert _stored_record(db_path) is not None


def test_a_recent_record_waits_out_the_remainder(monkeypatch, db_path, tmp_path):
    """Inside the interval the daemon waits the remainder; it does not snapshot at once."""
    _populate(db_path)
    clock = _FakeClock()
    monkeypatch.setattr(backup, "time", clock)
    interval = 3600
    last = clock.now - 100
    _seed_record(db_path, last)
    outbox = str(tmp_path / "outbox")
    moments = _count_snapshots(monkeypatch, clock)

    early = BackupThread(db_path, outbox, interval)
    _drive(early, clock, clock.now + 20)
    assert moments == [], "a recent record is not a reason to take a snapshot at start"

    later = BackupThread(db_path, outbox, interval)
    _drive(later, clock, last + interval + 5)
    assert moments, "once the interval since the record passes, the remainder is all that is left"
    assert moments[0] <= last + interval + _STARTUP_GRACE_SECONDS


def test_a_missing_record_is_overdue(monkeypatch, db_path, tmp_path):
    """No record at all is the first-run case and must snapshot after the grace."""
    _populate(db_path)
    clock = _FakeClock()
    monkeypatch.setattr(backup, "time", clock)
    outbox = str(tmp_path / "outbox")
    worker = BackupThread(db_path, outbox, 3600)

    _drive(worker, clock, clock.now + 10)

    assert os.path.isfile(os.path.join(outbox, "db", "workshop-backup.db"))


def test_a_corrupt_record_is_treated_as_no_record(monkeypatch, db_path, tmp_path):
    """An unparseable value is corruption, not an error: it means "overdue"."""
    _populate(db_path)
    clock = _FakeClock()
    monkeypatch.setattr(backup, "time", clock)
    with open(_state_file(db_path), "w", encoding="utf-8") as handle:
        handle.write(f"{_BACKUP_STATE_SECTION}:\n  {_BACKUP_STATE_KEY}: not-a-timestamp\n")
    outbox = str(tmp_path / "outbox")
    worker = BackupThread(db_path, outbox, 3600)

    _drive(worker, clock, clock.now + 10)

    assert os.path.isfile(os.path.join(outbox, "db", "workshop-backup.db"))


def test_an_unreadable_record_is_treated_as_no_record(monkeypatch, db_path, tmp_path):
    """A state file that cannot even be parsed behaves like a missing one."""
    _populate(db_path)
    clock = _FakeClock()
    monkeypatch.setattr(backup, "time", clock)
    with open(_state_file(db_path), "wb") as handle:
        handle.write(b"\x00\xffnot yaml at all: [")
    outbox = str(tmp_path / "outbox")
    worker = BackupThread(db_path, outbox, 3600)

    _drive(worker, clock, clock.now + 10)

    assert os.path.isfile(os.path.join(outbox, "db", "workshop-backup.db"))


def test_a_far_future_record_is_treated_as_corruption(monkeypatch, db_path, tmp_path):
    """A timestamp beyond the interval cannot be a real snapshot; ignore it."""
    _populate(db_path)
    clock = _FakeClock()
    monkeypatch.setattr(backup, "time", clock)
    _seed_record(db_path, clock.now + 10 * 365 * 24 * 3600)
    outbox = str(tmp_path / "outbox")
    worker = BackupThread(db_path, outbox, 3600)

    _drive(worker, clock, clock.now + 10)

    assert os.path.isfile(os.path.join(outbox, "db", "workshop-backup.db"))


def test_a_failed_snapshot_leaves_the_record_untouched(monkeypatch, db_path, tmp_path):
    """Only a published snapshot may move the record; a failure must not look like one."""
    _populate(db_path)
    clock = _FakeClock()
    monkeypatch.setattr(backup, "time", clock)
    interval = 3600
    outbox = str(tmp_path / "outbox")
    worker = BackupThread(db_path, outbox, interval)

    # A real success first, so the record is known to be written on success.
    assert worker.snapshot_now() is not None
    assert _stored_record(db_path) is not None

    # Now make it overdue and let the snapshot fail. The record must stay put,
    # so the next start still sees the backup as due.
    seeded = _iso(clock.now - interval)
    _seed_record(db_path, clock.now - interval)
    monkeypatch.setattr(backup, "snapshot_database", _raise_boom)
    failing = BackupThread(db_path, outbox, interval)
    _drive(failing, clock, clock.now + 20)

    assert _stored_record(db_path) == seeded


def test_a_restart_loop_takes_one_snapshot_per_interval(monkeypatch, db_path, tmp_path):
    """Restarts shorter than the interval must neither starve nor multiply copies.

    The old code took none at all here (every restart waited a fresh interval
    that never arrived); the record makes the interval absolute across restarts,
    while a restart the moment it elapses waits out only the remainder.
    """
    _populate(db_path)
    clock = _FakeClock()
    monkeypatch.setattr(backup, "time", clock)
    interval = 3600
    outbox = str(tmp_path / "outbox")
    moments = _count_snapshots(monkeypatch, clock)

    start = clock.now
    span = 4 * interval
    while clock.now < start + span:
        worker = BackupThread(db_path, outbox, interval)
        _drive(worker, clock, clock.now + 100)  # restarted every 100 s

    # At least one per interval (no starvation) and no two closer than the interval.
    assert len(moments) >= span // interval
    assert all(b - a >= interval for a, b in zip(moments, moments[1:])), moments
    assert moments[0] <= start + _STARTUP_GRACE_SECONDS
    assert _stored_record(db_path) is not None

