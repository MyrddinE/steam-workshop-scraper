"""Tests for verified database backups and the outbox manifest."""
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from src.backup import (
    BackupError,
    BackupThread,
    build_db_manifest_entry,
    snapshot_database,
    update_manifest,
    verify_snapshot,
)
from src.database import get_connection, initialize_database, insert_or_update_item


def _dest(tmp_path) -> str:
    return str(tmp_path / "outbox" / "db" / "workshop-backup.db")


# ── snapshot_database ────────────────────────────────────────────────────────

def test_snapshot_of_populated_db_succeeds_and_verifies(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "A", "dt_updated": 111})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "B", "dt_updated": 222})

    dest = _dest(tmp_path)
    meta = snapshot_database(db_path, dest)

    assert os.path.isfile(dest)
    assert meta["bytes"] == os.path.getsize(dest)
    assert meta["bytes"] > 0
    assert meta["rows"] == 2
    assert meta["max_dt_updated"] == 222
    assert meta["sha256"] == hashlib.sha256(open(dest, "rb").read()).hexdigest()
    # taken_at is an ISO-8601 timestamp
    assert datetime.fromisoformat(meta["taken_at"]) is not None

    # The snapshot is independently usable and passes the same verification.
    assert verify_snapshot(dest, db_path) == {"rows": 2, "max_dt_updated": 222}
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


def test_snapshot_verifier_rejects_row_count_mismatch(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "dt_updated": 1})

    other_db = str(tmp_path / "other.db")
    initialize_database(other_db)  # same schema, zero rows
    dest = str(tmp_path / "snap.db")
    snapshot_database(other_db, dest)

    with pytest.raises(BackupError, match="row count mismatch"):
        verify_snapshot(dest, db_path)


def test_failed_verification_leaves_previous_destination_untouched(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "dt_updated": 10})
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
    insert_or_update_item(db_path, {"workshop_id": 7, "dt_updated": 1})
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

    insert_or_update_item(db_path, {"workshop_id": 1, "dt_updated": 100})
    meta1 = snapshot_database(db_path, dest)
    update_manifest(outbox, build_db_manifest_entry(outbox, dest, meta1))

    insert_or_update_item(db_path, {"workshop_id": 2, "dt_updated": 200})
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


# ── BackupThread ─────────────────────────────────────────────────────────────

def test_backup_thread_run_now_swallows_failure(tmp_path):
    worker = BackupThread(str(tmp_path / "test.db"), str(tmp_path / "outbox"), 60)

    with patch("src.backup.snapshot_database", side_effect=RuntimeError("boom")):
        assert worker.run_now() is None  # logged and swallowed, no exception


def test_backup_thread_run_now_publishes_snapshot_and_manifest(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "dt_updated": 5})
    outbox = str(tmp_path / "outbox")
    worker = BackupThread(db_path, outbox, 60)

    meta = worker.run_now()

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
        "daemon": {"batch_size": 1, "target_appids": [1]},
    }
    daemon = Daemon(config, config_path=str(tmp_path / "config.yaml"))
    assert daemon._backup_worker is None

    with patch("src.daemon.get_next_items_to_scrape",
               return_value=[{"workshop_id": 1, "api_priority": 0, "status": 200}]) as mock_batch, \
         patch("src.daemon.get_workshop_details_api",
               return_value={"title": "T", "status": 200, "publishedfileid": 1}) as mock_api, \
         patch("src.daemon.flag_for_web_scrape"), \
         patch("src.daemon.flag_for_image"), \
         patch("src.daemon.get_user", return_value=None):
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
            "batch_size": 1,
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
    insert_or_update_item(db, {"workshop_id": 1, "dt_updated": 42})
    outbox = str(tmp_path / "outbox")
    config = {
        "database": {"path": db},
        "api": {"key": "K"},
        "daemon": {
            "batch_size": 1,
            "target_appids": [1],
            "outbox_dir": outbox,
            "backup_interval_seconds": 3600,
        },
    }
    # Replace the real network workers with no-op mocks; the final snapshot is
    # taken after they are joined, which is what we assert.
    with patch("src.daemon.TranslatorThread"), \
         patch("src.daemon.WebScraperThread"), \
         patch("src.daemon.ImageScraperThread"):
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
