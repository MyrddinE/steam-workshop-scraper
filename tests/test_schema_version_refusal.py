"""A build older than its database must refuse it, and write nothing.

`initialize_database` read `PRAGMA user_version` and only compared it inside the
`MIGRATIONS` loop (`db_version < version`), so a build whose `EXPECTED_VERSION`
was *lower* than the file's recorded version ran to completion: it built the
legacy schema, applied no migrations, and then read and wrote a schema it did
not understand. Batch 6a measured the cost of that silence -- an older build
against a renamed database resurrects its own tables beside the real ones and
keeps using them, so creator joins blank and progress made in that window is
lost. The guard now refuses, before WAL and before any schema statement.

These tests pin four things:

* a database at ``EXPECTED_VERSION + 1`` and one at ``+10`` both raise, with a
  message naming the path and both versions;
* the refusal leaves the file byte-for-byte untouched -- same bytes, same
  ``user_version``, same ``sqlite_master``, same columns, no committed WAL;
* a database *at* ``EXPECTED_VERSION`` is still a no-op, and one below still
  migrates;
* each entry point surfaces the sentence instead of a traceback.

The Windows-only daemonization path (`_daemonize`) cannot be exercised on this
machine; the refusal happens well after it in `daemon_runner.main`, so the tested
seam is the one that matters. Everything else here runs on any platform.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from unittest.mock import MagicMock

import pytest

from src.database import (
    EXPECTED_VERSION,
    SchemaVersionError,
    initialize_database,
)


def _stamp_newer(db_path, offset: int) -> None:
    """A real current-schema database whose marker is ahead of this build.

    Built by this build's own driver and then stamped forward. That is the
    simulation the guard is about: a future migration ran, so the file is a
    valid schema that this build simply does not know. A hand-made stub would
    only prove the guard fires and would hide what "unchanged" has to mean.
    """
    initialize_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {EXPECTED_VERSION + offset}")
    conn.commit()
    conn.close()


def _snapshot(db_path):
    """`user_version` plus every object and its columns, for before/after."""
    conn = sqlite3.connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        objects = sorted(
            (row[0], row[1], row[2])
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
        )
        columns = {}
        for kind, name, _ in objects:
            if kind != "table":
                continue
            columns[name] = tuple(
                (r[1], r[2], r[3], r[4], r[5])
                for r in conn.execute(f"PRAGMA table_info({name})")
            )
        return version, objects, columns
    finally:
        conn.close()


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sidecars(db_path):
    """State of the WAL sidecars, so "no new content" is comparable."""
    return {
        suffix: (os.path.exists(p), os.path.getsize(p) if os.path.exists(p) else 0)
        for suffix, p in (("-wal", db_path + "-wal"), ("-shm", db_path + "-shm"))
    }


# ── the refusal itself ───────────────────────────────────────────────────────


@pytest.mark.parametrize("offset", [1, 10], ids=["plus-1", "plus-10"])
def test_a_newer_database_is_refused(tmp_path, offset):
    db_path = str(tmp_path / "newer.db")
    _stamp_newer(db_path, offset)

    with pytest.raises(SchemaVersionError) as caught:
        initialize_database(db_path)

    message = str(caught.value)
    assert db_path in message
    assert str(EXPECTED_VERSION + offset) in message
    assert str(EXPECTED_VERSION) in message
    # The remedy is part of the contract, not just the diagnosis.
    assert "not corrupt" in message
    assert "Rolling back" in message


@pytest.mark.parametrize("offset", [1, 10], ids=["plus-1", "plus-10"])
def test_the_refusal_leaves_the_database_unchanged(tmp_path, offset):
    """Same version, same objects, same columns, same bytes, no committed WAL.

    The byte hash is the strongest form of "untouched": it also catches a write
    that changed no object (a journal-mode switch, a checkpoint, a header
    bump).
    """
    db_path = str(tmp_path / "newer.db")
    _stamp_newer(db_path, offset)
    before = _snapshot(db_path)
    before_hash = _sha256(db_path)
    before_sidecars = _sidecars(db_path)

    with pytest.raises(SchemaVersionError):
        initialize_database(db_path)

    assert _snapshot(db_path) == before
    assert _sha256(db_path) == before_hash
    assert _sidecars(db_path) == before_sidecars, (
        "a refused start must not commit WAL content")


def test_a_database_at_expected_version_is_a_noop(tmp_path):
    """The matching build still opens and closes it without complaint."""
    db_path = str(tmp_path / "current.db")
    initialize_database(db_path)
    before = _snapshot(db_path)

    initialize_database(db_path)  # must not raise

    after = _snapshot(db_path)
    assert after[0] == EXPECTED_VERSION
    assert after[1] == before[1]
    assert after[2] == before[2]


def test_a_database_below_expected_version_still_migrates(tmp_path):
    """The forward path is untouched: an old marker still climbs the chain.

    A fresh file starts at ``user_version = 0``, which is below
    ``EXPECTED_VERSION``, so the legacy path replays every migration. A marker
    rewound on an already-current file is not used here: the historical
    migration SQL names the pre-rename tables, and `restore_pre_rename_table_names`
    in `tests/conftest.py` exists precisely because that reconstruction is a
    fixture, not a normal startup.
    """
    db_path = str(tmp_path / "old.db")

    initialize_database(db_path, legacy_chain=True)  # must not raise

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == EXPECTED_VERSION
    finally:
        conn.close()


# ── entry points ─────────────────────────────────────────────────────────────


def test_daemon_runner_exits_2_with_the_sentence(tmp_path, monkeypatch, capsys):
    """`daemon_runner.main()` logs the message and exits 2, not a traceback."""
    from src import daemon_runner

    db_path = str(tmp_path / "newer.db")
    _stamp_newer(db_path, 1)
    monkeypatch.setattr("sys.argv", ["daemon_runner.py", "config.yaml"])
    monkeypatch.setattr(daemon_runner, "load_config",
                        lambda path: {"database": {"path": db_path}, "logging": {}})
    # The runner takes its PID file before it reads the schema, so the path is
    # isolated here: otherwise a daemon running in the project directory holds
    # .daemon.pid, the runner refuses with exit 3, and this test never reaches
    # the schema guard it is about.
    monkeypatch.setattr(daemon_runner, "PID_FILE", str(tmp_path / ".daemon.pid"))
    # So that, if the guard is ever removed, this test fails on the missing
    # SystemExit instead of starting a real daemon and hanging.
    daemon = MagicMock()
    monkeypatch.setattr(daemon_runner, "Daemon", daemon)

    with pytest.raises(SystemExit) as caught:
        daemon_runner.main()

    assert caught.value.code == 2
    daemon.return_value.run.assert_not_called()
    err = capsys.readouterr().err
    assert db_path in err
    assert "newer build" in err
    assert "Traceback" not in err


def test_web_runner_exits_2_with_the_sentence(tmp_path, monkeypatch, capsys, caplog):
    """`web_runner.main()` reports the sentence and never starts the server."""
    import waitress

    from src import web_runner

    db_path = str(tmp_path / "newer.db")
    _stamp_newer(db_path, 1)
    monkeypatch.setattr("sys.argv", ["web_runner.py", "config.yaml"])
    monkeypatch.setattr(web_runner, "load_config",
                        lambda path: {"database": {"path": db_path}})
    started = []
    monkeypatch.setattr(web_runner, "init_webserver",
                        lambda *a, **k: started.append(True))
    # As above: a missing guard must fail the test, not serve a real site.
    monkeypatch.setattr(waitress, "serve", lambda *a, **k: started.append("served"))

    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as caught:
            web_runner.main()

    assert caught.value.code == 2
    assert started == [], "the web server must not start against a newer schema"
    assert "newer build" in caplog.text
    assert "Traceback" not in capsys.readouterr().err


def test_tui_refuses_to_construct_with_the_sentence(tmp_path, monkeypatch, capsys):
    """The TUI's startup raises SystemExit(2) with the sentence on stderr.

    `ScraperApp.__init__` calls `initialize_database`; the app is never mounted,
    so there is no degraded screen to reach. The later startup steps are stubbed
    so that, with the guard removed, the test fails on the missing SystemExit
    rather than opening sockets or a daemon controller.
    """
    from src import tui

    db_path = str(tmp_path / "newer.db")
    _stamp_newer(db_path, 1)
    monkeypatch.setattr(tui, "load_config",
                        lambda path: {"database": {"path": db_path}})
    monkeypatch.setattr(tui, "DaemonController", MagicMock())
    monkeypatch.setattr(tui.workshop_folders, "WorkshopFolders", MagicMock())
    monkeypatch.setattr(tui.ScraperApp, "_start_webserver", lambda self: None)

    with pytest.raises(SystemExit) as caught:
        tui.ScraperApp("config.yaml")

    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert db_path in err
    assert "newer build" in err
    assert "Traceback" not in err
