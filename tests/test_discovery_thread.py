"""The fetch queue is refilled while the loop is still draining it.

Discovery used to run *inside* the fetch loop and only once that loop had drained
the queue to nothing: the daemon starved, blocked on paging until enough new items
had appeared, and only then resumed fetching. Batching the details calls made
fetching fast enough that the stall became a visible share of the daemon's time,
which is what moved the refill onto its own thread.

The thread buys no extra API budget — `steam_api._rate_limit` is one schedule
shared by every caller, so it waits its turn exactly as the fetch loop does. What
is tested here is that it runs at all, that it wakes the fetch loop instead of
leaving it to poll for ten minutes, that a failed pass does not kill it, and that
its requests now feed the adaptive backoff, which was blind to this traffic while
it was serialised behind the loop.
"""

import time
from unittest.mock import patch

import pytest

from src import pacing
from src.daemon import Daemon, DiscoveryThread
from src.database import initialize_database


def _daemon(db_path) -> Daemon:
    return Daemon({
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"target_appids": [1], "batch_size": 1, "request_delay_seconds": 0},
    })


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "discovery.db")
    initialize_database(path)
    return path


# --- the fetch loop no longer discovers -------------------------------------

def test_an_empty_queue_returns_empty_instead_of_blocking(db):
    """The regression: this call used to run a whole discovery pass inline."""
    daemon = _daemon(db)
    with patch("src.daemon.get_next_items_to_scrape", return_value=[]), \
         patch.object(Daemon, "seed_database") as seed, \
         patch.object(Daemon, "_run_page_discovery") as page:
        assert daemon._acquire_batch() == []
    seed.assert_not_called()
    page.assert_not_called()


# --- the idle wait wakes on the signal --------------------------------------

def test_the_idle_wait_returns_as_soon_as_work_is_signalled(db):
    """Waiting out the full ten minutes would make a refill invisible."""
    daemon = _daemon(db)
    daemon._work_available.set()
    started = time.monotonic()
    daemon._wait_for_work()
    assert time.monotonic() - started < 1.0
    assert not daemon._work_available.is_set(), "the flag is consumed, not left set"


def test_the_idle_wait_gives_up_after_ten_minutes_of_silence(db):
    """It still returns so the outer loop re-checks, as it did before."""
    daemon = _daemon(db)
    with patch("src.daemon.time.sleep") as sleep:
        daemon._wait_for_work()
    assert sleep.call_count == 600


# --- the thread -------------------------------------------------------------

def test_the_thread_runs_a_pass_and_wakes_the_fetch_loop(db):
    daemon = _daemon(db)
    daemon._cursor_exhausted = True          # skip the eligibility count
    with patch.object(daemon, "seed_database", return_value=5) as seed, \
         patch.object(daemon, "_run_page_discovery"):
        thread = DiscoveryThread(daemon, interval=0.05)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not seed.called and time.monotonic() < deadline:
                time.sleep(0.01)
            assert seed.called, "the thread must run a discovery pass"
            assert daemon._work_available.is_set(), "and say so, rather than staying silent"
        finally:
            thread.stop()
            thread.join(timeout=5)
    assert not thread.is_alive()


def test_a_failed_pass_does_not_kill_the_thread(db):
    """A discovery fault is a log line; the fetch loop keeps draining."""
    daemon = _daemon(db)
    daemon._cursor_exhausted = True
    with patch.object(daemon, "seed_database", side_effect=RuntimeError("boom")) as seed, \
         patch.object(daemon, "_run_page_discovery"):
        thread = DiscoveryThread(daemon, interval=0.05)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while seed.call_count < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert seed.call_count >= 3, "it must keep trying after a failure"
            assert thread.is_alive()
        finally:
            thread.stop()
            thread.join(timeout=5)


def test_the_thread_stops_when_the_daemon_does(db):
    """Shutdown must not wait out a discovery nap."""
    daemon = _daemon(db)
    daemon._cursor_exhausted = True
    with patch.object(daemon, "seed_database", return_value=0), \
         patch.object(daemon, "_run_page_discovery"):
        thread = DiscoveryThread(daemon, interval=30.0)
        thread.start()
        daemon.running = False
        thread.join(timeout=5)
    assert not thread.is_alive()


# --- discovery now feeds the adaptive backoff -------------------------------

def test_a_refused_discovery_page_slows_the_api_delay(db):
    """Discovery traffic is the same key, so the controller must see it."""
    daemon = _daemon(db)
    daemon.api_delay = 1.0
    with patch("src.daemon.query_workshop_files", return_value={"error": "refused"}), \
         patch("src.daemon.time.sleep"), patch("src.pacing.wait"):
        daemon.seed_database(target_new=100)
    assert daemon.api_delay == 2.0


def test_a_healthy_discovery_page_decays_the_api_delay(db):
    daemon = _daemon(db)
    daemon.api_delay = 1.0
    daemon._api_clock._at = pacing.now() - pacing.HALF_LIFE_SECONDS
    with patch("src.daemon.query_workshop_files",
               return_value={"total": 0, "items": [], "next_cursor": ""}), \
         patch("src.daemon.time.sleep"), patch("src.pacing.wait"):
        daemon.seed_database(target_new=100)
    assert daemon.api_delay == pytest.approx(0.5, rel=1e-4)


def test_an_abandoned_page_is_not_counted_as_a_refusal(db):
    """A shutdown is not evidence about the request rate."""
    daemon = _daemon(db)
    daemon.api_delay = 1.0
    with patch("src.daemon.query_workshop_files", return_value={"abandoned": True}), \
         patch("src.daemon.time.sleep"), patch("src.pacing.wait"):
        daemon.seed_database(target_new=100)
    assert daemon.api_delay == 1.0, "abandoning a wait must not move the delay"


def test_the_discovery_walk_reports_how_much_it_found(db):
    """The thread signals on the strength of this, so it has to be real."""
    daemon = _daemon(db)
    items = [{"publishedfileid": str(i)} for i in range(1, 4)]
    with patch("src.daemon.query_workshop_files",
               return_value={"total": 3, "items": items, "next_cursor": ""}), \
         patch("src.daemon.time.sleep"), patch("src.pacing.wait"):
        found = daemon.seed_database(target_new=100)
    assert found == 3
