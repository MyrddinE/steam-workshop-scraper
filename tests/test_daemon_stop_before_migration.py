"""A pending schema migration must not run under a live daemon.

The daemon is detached, so closing the TUI leaves it running. Relaunching a UI
after an update used to call ``initialize_database`` straight away, so migration
34->35 -- whose ``DROP COLUMN`` rewrites ``workshop_items`` and took 283 s in
production -- ran against a database a live daemon was writing to, and the
daemon then kept running old code against the new schema. The fix is a gate:
``initialize_database_with_daemon_stopped`` reads the recorded ``user_version``
first, stops a running daemon through the existing ``DaemonController.stop()``
only when a migration is actually pending, refuses to migrate when the stop did
not succeed, and restarts the daemon only after the migration applied cleanly.

No test here spawns a process. The controller is a fake that records every call
into a shared event log, and the version read and the migrating step are
monkeypatched onto the helper's own module, so the tests pin the helper's
*order of operations* while nothing touches a database or a subprocess.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock

import pytest

import src.daemon_control as daemon_control
import src.database as database
from src.daemon_control import (
    DaemonController,
    DaemonStillRunningError,
    SchemaMigrationFailedError,
    initialize_database_with_daemon_stopped,
)
from src.database import EXPECTED_VERSION, SchemaVersionError


class FakeController:
    """A ``DaemonController`` stand-in that records calls into a shared log.

    ``stop_result`` is what ``stop()`` returns; the failing form is the real
    controller's "did not exit, the PID file was removed" refusal, after which
    the daemon is by construction still running.
    """

    def __init__(self, events, running=True, stop_result=(True, "Daemon stopped")):
        self.events = events
        self.running = running
        self.stop_result = stop_result

    def is_running(self):
        self.events.append("is_running")
        return self.running

    def stop(self):
        self.events.append("stop")
        if self.stop_result[0]:
            self.running = False
        return self.stop_result

    def start(self):
        self.events.append("start")
        self.running = True
        return True, "Daemon started (PID: 4242)"


def _pending(monkeypatch, events, *, raises=None):
    """A pending migration whose migrating step appends to the same event log."""
    monkeypatch.setattr(daemon_control, "read_schema_version",
                        lambda path: EXPECTED_VERSION - 1)

    def migrate(path):
        events.append("initialize_database")
        if raises is not None:
            raise raises

    monkeypatch.setattr(daemon_control, "initialize_database", migrate)


# ── the ordering the gate exists to create ──────────────────────────────────


def test_the_migration_is_bracketed_by_the_stop_and_the_restart(monkeypatch):
    """The whole point: stop -> migrate -> start, in that order.

    Asserting the exact sequence rather than "stop was called" is what fails if
    a later edit moves the stop after the migration or drops the restart.
    """
    events = []
    _pending(monkeypatch, events)
    controller = FakeController(events, running=True)

    initialize_database_with_daemon_stopped("workshop.db", controller)

    assert events == ["is_running", "stop", "initialize_database", "start"], (
        "a pending migration must be bracketed by the daemon's stop and its "
        f"restart, in that order; got {events}")


def test_a_current_schema_leaves_a_running_daemon_alone(monkeypatch):
    """The common relaunch: nothing pending, so nothing is stopped or started."""
    events = []
    monkeypatch.setattr(daemon_control, "read_schema_version",
                        lambda path: EXPECTED_VERSION)
    monkeypatch.setattr(daemon_control, "initialize_database",
                        lambda path: events.append("initialize_database"))
    controller = FakeController(events, running=True)

    initialize_database_with_daemon_stopped("workshop.db", controller)

    assert events == ["initialize_database"], (
        "a UI relaunch on the current schema must not touch the daemon at all; "
        f"got {events}")


def test_a_pending_migration_with_no_daemon_stops_nothing(monkeypatch):
    """Nothing to stop and nobody to restart: migrate directly."""
    events = []
    _pending(monkeypatch, events)
    controller = FakeController(events, running=False)

    initialize_database_with_daemon_stopped("workshop.db", controller)

    assert events == ["is_running", "initialize_database"]
    assert "stop" not in events and "start" not in events


# ── the gate ────────────────────────────────────────────────────────────────


def test_a_refused_stop_blocks_the_migration(monkeypatch):
    """A live writer is the defect, so a stop that did not succeed is fatal."""
    events = []
    _pending(monkeypatch, events)
    controller = FakeController(
        events, running=True,
        stop_result=(False, "Daemon did not exit; PID 4242 was not started by "
                            "this controller, so it was left alone."))

    with pytest.raises(DaemonStillRunningError) as caught:
        initialize_database_with_daemon_stopped("workshop.db", controller)

    assert "initialize_database" not in events, (
        "migrating while the daemon is still running is exactly what the gate "
        f"must prevent; got {events}")
    assert "start" not in events
    message = str(caught.value)
    assert "still running" in message
    assert "was not attempted" in message


def test_a_failed_migration_does_not_restart_the_daemon(monkeypatch):
    """The daemon was stopped for the migration; a failure must say so."""
    events = []
    _pending(monkeypatch, events, raises=RuntimeError("disk full"))
    controller = FakeController(events, running=True)

    with pytest.raises(SchemaMigrationFailedError) as caught:
        initialize_database_with_daemon_stopped("workshop.db", controller)

    assert "start" not in events, (
        "the daemon must not come back up onto a migration that failed")
    assert isinstance(caught.value.__cause__, RuntimeError)
    message = str(caught.value)
    assert "disk full" in message
    assert "stopped" in message
    assert "not been restarted" in message


def test_a_newer_database_is_refused_before_the_daemon_is_touched(monkeypatch):
    """A refused start must not take the service down on its way out.

    ``initialize_database`` raises ``SchemaVersionError`` for a database newer
    than the build, and the entry points must see that sentence -- without the
    daemon having been stopped for a migration that is never going to run.
    """
    events = []
    monkeypatch.setattr(daemon_control, "read_schema_version",
                        lambda path: EXPECTED_VERSION + 1)
    monkeypatch.setattr(daemon_control, "initialize_database",
                        lambda path: pytest.fail(
                            "a newer database must not be migrated"))
    controller = FakeController(events, running=True)

    with pytest.raises(SchemaVersionError) as caught:
        initialize_database_with_daemon_stopped("workshop.db", controller)

    assert events == [], (
        f"a newer database must be refused with the daemon untouched; got {events}")
    assert "newer build" in str(caught.value)


# ── the log says why ────────────────────────────────────────────────────────


def test_the_stop_and_the_restart_are_logged_as_migration_steps(monkeypatch, caplog):
    events = []
    _pending(monkeypatch, events)
    controller = FakeController(events, running=True)

    with caplog.at_level(logging.INFO):
        initialize_database_with_daemon_stopped("workshop.db", controller)

    text = caplog.text.lower()
    assert "migration" in text
    assert "stopping the running daemon" in text
    assert "restart" in text


# ── the read-only version probe ─────────────────────────────────────────────


def test_read_schema_version_reports_what_initialize_recorded(tmp_path):
    db_path = str(tmp_path / "workshop.db")
    database.initialize_database(db_path)
    assert database.read_schema_version(db_path) == EXPECTED_VERSION


def test_read_schema_version_of_a_missing_file_is_zero(tmp_path):
    """A brand-new path reads as 0 -- pending, because it builds a schema."""
    missing = str(tmp_path / "missing.db")
    assert database.read_schema_version(missing) == 0
    assert not os.path.exists(missing), (
        "deciding whether a migration is pending must not create the database")


# ── both UI entry points go through the helper ──────────────────────────────


def test_the_web_runner_starts_through_the_helper(tmp_path, monkeypatch):
    import waitress

    from src import web_runner

    db_path = str(tmp_path / "workshop.db")
    monkeypatch.setattr("sys.argv", ["web_runner.py", "config.yaml"])
    monkeypatch.setattr(web_runner, "load_config",
                        lambda path: {"database": {"path": db_path}})
    calls = []
    monkeypatch.setattr(
        web_runner, "initialize_database_with_daemon_stopped",
        lambda path, controller: calls.append((path, controller)))
    monkeypatch.setattr(web_runner, "init_webserver", lambda *a, **k: None)
    monkeypatch.setattr(waitress, "serve", lambda *a, **k: None)

    web_runner.main()

    assert len(calls) == 1, "the web runner must reach the database through the gate"
    called_path, controller = calls[0]
    assert called_path == db_path
    assert isinstance(controller, DaemonController)


def test_the_tui_starts_through_the_helper(tmp_path, monkeypatch):
    from src import tui

    db_path = str(tmp_path / "workshop.db")
    monkeypatch.setattr(tui, "load_config",
                        lambda path: {"database": {"path": db_path}})
    controller = MagicMock()
    monkeypatch.setattr(tui, "DaemonController", MagicMock(return_value=controller))
    monkeypatch.setattr(tui.workshop_folders, "WorkshopFolders", MagicMock())
    monkeypatch.setattr(tui.ScraperApp, "_start_webserver", lambda self: None)
    calls = []
    monkeypatch.setattr(tui, "initialize_database_with_daemon_stopped",
                        lambda path, ctrl: calls.append((path, ctrl)))

    tui.ScraperApp("config.yaml")

    assert calls == [(db_path, controller)], (
        "the TUI must hand its single controller to the gate")
