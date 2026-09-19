"""The daemon's own side of the PID-file stop protocol.

The controller's half has had tests since it was written, but nothing covered
the daemon's: the presence guard that keeps a test-startup daemon from mistaking
a missing file for a stop request, the ordering of the stop flags against the
joins, and the *joint* budget the joins now share. The ordering is the fix for a
real failure: the workers used to be signalled and joined one after another, so
a later worker was not told to stop until every earlier join had returned, five
5-second joins added up to 25 seconds worst case, and the controller's 15 s
grace expired while the workers were still logging.
"""

import logging
import os
import time
from unittest.mock import patch

from src.daemon import Daemon
from src.database import initialize_database

WORKER_NAMES = ("Discovery", "Web scraper", "Image download", "Translator", "Backup")


def _daemon(tmp_path, expect_pid_file=False) -> Daemon:
    db = str(tmp_path / "shutdown.db")
    initialize_database(db)
    return Daemon(
        {
            "database": {"path": db},
            "api": {"key": "TEST_KEY"},
            "daemon": {
                "target_appids": [1],
                "api_batch_size": 1,
                "request_delay_seconds": 0,
            },
        },
        config_path=str(tmp_path / "config.yaml"),
        expect_pid_file=expect_pid_file,
    )


class _RecordingWorker:
    """A duck-typed stand-in for a worker thread.

    Records the order in which it is told to stop and joined. A ``stubborn``
    worker pretends the stop flag never reached it, so the budget can be
    exercised without a real thread that would have to burn five seconds to
    prove the same point.
    """

    def __init__(self, name, events, stubborn=False):
        self.name = name
        self.events = events
        self._running = True
        self._stubborn = stubborn
        self._alive = False
        self.snapshots = 0

    @property
    def running(self):
        return self._running

    @running.setter
    def running(self, value):
        self._running = value
        if value is False:
            self.events.append(("stop", self.name))

    def join(self, timeout=None):
        self.events.append(("join", self.name, timeout))
        if self._stubborn:
            deadline = time.monotonic() + (timeout or 0.0)
            while time.monotonic() < deadline:
                time.sleep(0.002)
            self._alive = True

    def is_alive(self):
        return self._alive

    def snapshot_now(self):
        self.snapshots += 1


def _attach(daemon, workers):
    daemon._discovery_thread = workers["Discovery"]
    daemon._web_worker = workers["Web scraper"]
    daemon._image_worker = workers["Image download"]
    daemon.translator = workers["Translator"]
    daemon._backup_worker = workers["Backup"]


# --- _pid_file_removed: the guard and the trigger ----------------------------


def test_pid_file_removal_is_ignored_until_the_file_has_been_seen(tmp_path, monkeypatch):
    """A daemon that has never seen the file was not told to stop.

    The guard is what keeps a daemon started by a test -- or one whose PID file
    was never written -- from reading "no file" as "the controller deleted it".
    """
    monkeypatch.chdir(tmp_path)
    daemon = _daemon(tmp_path)

    assert not os.path.exists(".daemon.pid")
    assert daemon._pid_file_removed() is False
    assert daemon.running is True
    assert daemon._saw_pid_file is False


def test_pid_file_removal_triggers_the_stop_once(tmp_path, monkeypatch, caplog):
    """Seen, then removed: stop, return True, and say it exactly once."""
    monkeypatch.chdir(tmp_path)
    daemon = _daemon(tmp_path)
    (tmp_path / ".daemon.pid").write_text("4242", encoding="utf-8")

    # Present: the daemon now knows the file is part of the protocol.
    assert daemon._pid_file_removed() is False
    assert daemon._saw_pid_file is True
    assert daemon.running is True

    (tmp_path / ".daemon.pid").unlink()
    with caplog.at_level(logging.INFO):
        assert daemon._pid_file_removed() is True
        assert daemon.running is False
        # Asking again is a no-op, not a second shutdown line.
        assert daemon._pid_file_removed() is False

    assert caplog.text.count("PID file removed") == 1


# --- the race: a stop before the first observation ---------------------------


def test_a_pid_file_that_is_expected_is_a_stop_before_it_is_ever_seen(
        tmp_path, monkeypatch):
    """A daemon launched by the runner knows a PID file is part of the protocol.

    This is the production race: the file is written by the runner and the
    controller deletes it during config load, migrations, construction or thread
    startup. The old "have I seen the file yet?" guard then ignored the absence
    for ever and the daemon ran on. ``expect_pid_file`` says the file is real
    from the first check, without requiring an observation first.
    """
    monkeypatch.chdir(tmp_path)
    daemon = _daemon(tmp_path, expect_pid_file=True)
    assert not os.path.exists(".daemon.pid")

    assert daemon._pid_file_removed() is True
    assert daemon.running is False


def test_a_stop_that_lands_before_the_first_check_is_not_lost(tmp_path, monkeypatch):
    """The loop must shut down on that early stop, not run another batch.

    The file exists when the daemon is constructed -- as the runner leaves it --
    and is gone before the first batch, so no housekeeping or queue read may
    happen.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".daemon.pid").write_text("1", encoding="utf-8")
    daemon = _daemon(tmp_path, expect_pid_file=True)
    (tmp_path / ".daemon.pid").unlink()

    with patch.object(Daemon, "_maybe_promote_stale_items") as sweep, \
         patch.object(Daemon, "_maybe_reconcile_subscriptions") as reconcile, \
         patch.object(Daemon, "_maybe_scan_downloaded_items") as scan, \
         patch.object(Daemon, "_acquire_batch") as acquire:
        daemon.run()

    assert daemon.running is False
    sweep.assert_not_called()
    reconcile.assert_not_called()
    scan.assert_not_called()
    acquire.assert_not_called()


# --- ordering: every flag before any join ------------------------------------


def test_every_worker_is_signalled_before_any_join(tmp_path):
    """The property that pins the fix: no join until all workers are told.

    Against the old serialised sequence the first join lands right after the
    first stop, so the later workers' stop events appear after it.
    """
    daemon = _daemon(tmp_path)
    events = []
    workers = {name: _RecordingWorker(name, events) for name in WORKER_NAMES}
    _attach(daemon, workers)

    daemon._shutdown_workers()

    first_join = min(i for i, event in enumerate(events) if event[0] == "join")
    signalled_before_first_join = {event[1] for event in events[:first_join]
                                   if event[0] == "stop"}
    assert signalled_before_first_join == set(WORKER_NAMES), (
        "every worker's stop flag must be set before the first join is "
        f"attempted; signalled={sorted(signalled_before_first_join)}"
    )


# --- budget: one deadline for the whole join phase ---------------------------


def test_a_stubborn_worker_cannot_stretch_the_shutdown_past_the_budget(
        tmp_path, monkeypatch, caplog):
    """One worker ignoring its flag must not hold the phase, or the others, open.

    The old code gave every worker its own 5 s join in turn, so a single
    deaf worker cost the full 5 s and a second one cost another 5 s. Here the
    budget is 0.25 s for the *whole* phase: the stubborn worker's join gets no
    more than that, the joins behind it are skipped, and every other worker is
    still reported stopped.
    """
    monkeypatch.setattr("src.daemon.SHUTDOWN_BUDGET_SECONDS", 0.25)
    daemon = _daemon(tmp_path)
    events = []
    workers = {name: _RecordingWorker(name, events) for name in WORKER_NAMES}
    workers["Web scraper"] = _RecordingWorker("Web scraper", events, stubborn=True)
    _attach(daemon, workers)

    started = time.monotonic()
    with caplog.at_level(logging.INFO):
        daemon._shutdown_workers()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "the phase is bounded by the budget, not 5 s per worker"
    web_join_timeout = next(event[2] for event in events
                            if event[0] == "join" and event[1] == "Web scraper")
    assert web_join_timeout <= 0.25 + 1e-6
    # The workers behind the stubborn one get no join call at all, because the
    # shared deadline is already gone; they are still reported as stopped.
    assert not any(event[0] == "join" and event[1] in
                   ("Image download", "Translator", "Backup") for event in events)
    assert "still running: Web scraper" in caplog.text
    for name in ("Discovery", "Image download", "Translator", "Backup"):
        assert f"{name} thread stopped." in caplog.text


def test_final_snapshot_is_skipped_when_a_writer_survived(
        tmp_path, monkeypatch, caplog):
    """A writer that outlived the joins voids the snapshot's premise."""
    monkeypatch.setattr("src.daemon.SHUTDOWN_BUDGET_SECONDS", 0.25)
    daemon = _daemon(tmp_path)
    events = []
    workers = {name: _RecordingWorker(name, events) for name in WORKER_NAMES}
    workers["Image download"] = _RecordingWorker("Image download", events,
                                                 stubborn=True)
    _attach(daemon, workers)

    with caplog.at_level(logging.WARNING):
        daemon._shutdown_workers()

    assert workers["Backup"].snapshots == 0, (
        "the closing snapshot must not be taken while a writer is still running")
    assert "Skipping final database snapshot" in caplog.text
    assert "Image download" in caplog.text


def test_final_snapshot_runs_when_every_writer_stopped(tmp_path):
    """The complement: with nothing left running the snapshot is taken once."""
    daemon = _daemon(tmp_path)
    events = []
    workers = {name: _RecordingWorker(name, events) for name in WORKER_NAMES}
    _attach(daemon, workers)

    daemon._shutdown_workers()

    assert workers["Backup"].snapshots == 1


# --- the chunk loop is a stop point too --------------------------------------


def test_fetch_details_stops_between_chunks(tmp_path):
    """A stop between chunked requests must not post the rest of the batch."""
    daemon = _daemon(tmp_path)
    calls = []

    def fake_batch(ids, api_key):
        calls.append(ids)
        daemon.running = False  # the stop arrives after the first request
        return {item_id: {"status": 200, "publishedfileid": item_id}
                for item_id in ids}

    items = [{"workshop_id": i} for i in range(250)]  # three 100-id chunks
    with patch("src.daemon.get_workshop_details_batch", side_effect=fake_batch):
        result = daemon._fetch_details(items)

    assert len(calls) == 1
    assert len(result) == 100


def test_creator_refresh_is_skipped_once_stopping(tmp_path):
    """No persona request after the daemon already knows it is stopping.

    The item loop breaks on a stop but leaves the creators it gathered before
    the break; refreshing them would add a request (up to the transport
    timeout) between the stop being noticed and the shutdown sequence starting.
    """
    daemon = _daemon(tmp_path)
    daemon.running = False

    with patch("src.daemon.get_player_summaries") as summaries:
        daemon._refresh_creators([42])

    summaries.assert_not_called()
