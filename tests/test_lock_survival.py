"""A lock that outlasts the busy timeout loses one iteration, not the daemon.

Every connection is opened with a 15 s busy timeout, so a `sqlite3.OperationalError`
means a write lock was held longer than that. The daemon's loops were unevenly
guarded: `DiscoveryThread` and `TranslatorThread` caught per pass, the web worker
had no loop-level `try`, the image worker's queue read and no-URL write sat outside
its request `try`, and the main loop called `process_batch()` bare. A lock escaping
any of them ended the daemon (four production occurrences) or silently killed the
image thread while the daemon ran on.

These tests inject the lock at the database call each loop makes and pin the one
property that matters: the loop retries. A lock is a log line and a retry -- the row
is deliberately left alone, because the pass that never reached it wrote nothing.
"""

import logging
import sqlite3
from unittest.mock import patch

from src.daemon import Daemon
from src.database import initialize_database

# A page the web worker treats as a clean success: the selector matched and the
# item's description is stored.
FOUND = {
    "description": "scraped text",
    "tags": [],
    "body": None,
    "http_status": 200,
    "final_url": "https://example.invalid/?id=1",
}


class _StubWorker:
    """A worker stand-in that starts and joins instantly.

    The daemon's loop is the subject here, not its threads; stubbing them keeps
    the test hermetic and stops discovery reaching the network.
    """

    def __init__(self, *args, **kwargs):
        self.running = True

    def start(self):
        pass

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def test_the_daemon_loop_survives_a_lock_and_retries(tmp_path, caplog):
    """A locked ``process_batch`` is retried; it must not propagate out of run()."""
    db = str(tmp_path / "daemon-lock.db")
    initialize_database(db)
    daemon = Daemon(
        {
            "database": {"path": db},
            "api": {"key": "TEST_KEY"},
            "daemon": {"target_appids": [1], "api_batch_size": 1,
                       "request_delay_seconds": 0},
        },
        config_path=str(tmp_path / "config.yaml"),
    )
    daemon.translator = _StubWorker()

    calls = []

    def process_batch():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        daemon.running = False

    with patch("src.daemon.DiscoveryThread", _StubWorker), \
         patch("src.daemon.WebScraperThread", _StubWorker), \
         patch("src.daemon.ImageDownloadThread", _StubWorker), \
         patch.object(Daemon, "process_batch", side_effect=process_batch), \
         patch("src.daemon.pacing.wait") as wait, \
         caplog.at_level(logging.WARNING):
        daemon.run()

    assert calls == [1, 1], (
        "the pass that lost the lock must be retried, not end the daemon"
    )
    assert wait.called, "a persistent lock must not become a hot loop"
    assert any("locked" in r.getMessage() for r in _warnings(caplog)), (
        "the lost iteration must be a warning naming the exception"
    )


def test_a_stop_reaches_the_daemon_while_the_lock_keeps_failing(
        tmp_path, monkeypatch):
    """The PID-file stop is checked every iteration, a locked one included.

    `_pid_file_removed()` is what turns a removed PID file into
    ``self.running = False``. If the lock path skipped it -- the first version
    of this guard paused and then ``continue``d -- a persistent lock would
    postpone a graceful stop until a pass happened to succeed, leaving the
    controller's SIGTERM escalation as the only way out.
    """
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "daemon-stop.db")
    initialize_database(db)
    daemon = Daemon(
        {
            "database": {"path": db},
            "api": {"key": "TEST_KEY"},
            "daemon": {"target_appids": [1], "api_batch_size": 1,
                       "request_delay_seconds": 0},
        },
        config_path=str(tmp_path / "config.yaml"),
        # The runner leaves the file behind before the daemon starts; here it is
        # already gone, so the first check is a stop even though none was seen.
        expect_pid_file=True,
    )
    daemon.translator = _StubWorker()

    passes = []

    def process_batch():
        passes.append(1)
        # A regression that never checks the stop flag must still terminate.
        if len(passes) > 5:
            daemon.running = False
        raise sqlite3.OperationalError("database is locked")

    with patch("src.daemon.DiscoveryThread", _StubWorker), \
         patch("src.daemon.WebScraperThread", _StubWorker), \
         patch("src.daemon.ImageDownloadThread", _StubWorker), \
         patch.object(Daemon, "process_batch", side_effect=process_batch), \
         patch("src.daemon.pacing.wait"):
        daemon.run()

    assert len(passes) == 1, (
        "the stop check must run on the iteration that lost the lock; run() "
        f"needed {len(passes)} passes to notice the missing PID file"
    )
    assert daemon.running is False


def test_the_web_worker_survives_a_lock_and_keeps_working(db_path, caplog):
    """The web worker's loop had no guard at all: a lock ended the thread."""
    from src.web_worker import WebScraperThread

    worker = WebScraperThread(db_path, ".pauselock")
    asked = []

    def next_item(*args, **kwargs):
        asked.append(1)
        if len(asked) == 1:
            raise sqlite3.OperationalError("database is locked")
        if len(asked) >= 3:
            worker.running = False
            return None
        return {"workshop_id": 1, "steam_updated_at": 1}

    with patch("src.web_worker.get_next_web_scrape_item", side_effect=next_item), \
         patch("src.web_worker.scrape_extended_details",
               return_value=FOUND) as scrape, \
         patch("src.web_worker.pacing.wait") as wait, \
         caplog.at_level(logging.WARNING):
        worker.start()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert scrape.call_count == 1, (
        "the worker must return to the queue after the lock and scrape on"
    )
    assert len(asked) >= 3, "it must keep taking work, not stop at the lock"
    assert wait.called, "the retry must be paused"
    assert any("locked" in r.getMessage() for r in _warnings(caplog))


def test_the_image_worker_survives_a_lock_reading_the_queue(db_path, caplog):
    """The observed silent death: get_next_image_item escaped the request try."""
    from src.image_worker import ImageDownloadThread

    worker = ImageDownloadThread(db_path, ".pauselock")
    asked = []

    def next_item(*args, **kwargs):
        asked.append(1)
        if len(asked) == 1:
            raise sqlite3.OperationalError("database is locked")
        worker.running = False
        return None

    with patch("src.image_worker.get_next_image_item", side_effect=next_item), \
         patch("src.image_worker.pacing.wait") as wait, \
         caplog.at_level(logging.WARNING):
        worker.start()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(asked) == 2, (
        "the worker must go back to the image queue after the lock"
    )
    assert wait.called, "the retry must be paused"
    assert any("locked" in r.getMessage() for r in _warnings(caplog))


def test_the_image_worker_survives_a_lock_clearing_a_row_without_a_url(
        db_path, caplog):
    """The other write outside the request try: clearing a URL-less row's flag."""
    from src.image_worker import ImageDownloadThread

    worker = ImageDownloadThread(db_path, ".pauselock")
    asked = []

    def next_item(*args, **kwargs):
        asked.append(1)
        if len(asked) == 1:
            return {"workshop_id": 7, "preview_url": None, "image_priority": 5}
        worker.running = False
        return None

    class _LockedConnection:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            pass

        def close(self):
            pass

    with patch("src.image_worker.get_next_image_item", side_effect=next_item), \
         patch.object(worker, "_get_conn", return_value=_LockedConnection()), \
         patch("src.image_worker.pacing.wait") as wait, \
         caplog.at_level(logging.WARNING):
        worker.start()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(asked) == 2, (
        "the worker must survive the locked flag write and take the next item"
    )
    assert wait.called, "the retry must be paused"
    assert any("locked" in r.getMessage() and "7" in r.getMessage()
               for r in _warnings(caplog)), (
        "the warning must name the item whose row was left queued"
    )
