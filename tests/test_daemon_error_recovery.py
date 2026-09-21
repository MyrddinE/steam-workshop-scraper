import pytest
from unittest.mock import patch
from src.daemon import Daemon
from src.database import initialize_database, insert_or_update_item, get_connection


@pytest.fixture(autouse=True)
def _no_discovery_side_effects():
    """Keep these tests hermetic.

    If a failure path wrongly dequeues its item, the next _read_batch finds an
    empty queue, falls through to seed_database (real network) and then sits in
    _wait_for_work for ten minutes. Both are stubbed so a regression fails fast
    instead of hanging the suite.
    """
    with patch("src.daemon.query_workshop_newest_page"), patch.object(Daemon, "_wait_for_work"):
        yield


def _daemon(db_path, **overrides):
    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"target_appids": [1], "api_batch_size": 1, "api_delay_seconds": 0.01},
    }
    config["daemon"].update(overrides)
    return Daemon(config)


def _priority(db_path, wid):
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT api_priority, fetch_status FROM workshop_items WHERE workshop_id = ?", (wid,)
    ).fetchone()
    conn.close()
    return (row["api_priority"], row["fetch_status"]) if row else (None, None)


def test_process_batch_with_db_locked(tmp_path):
    """Test that process_batch handles potential database locking issues."""
    db_path = str(tmp_path / "locked.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "daemon": {"target_appids": [1], "api_batch_size": 1}
    }
    daemon = Daemon(config)

    import sqlite3
    with patch("src.daemon.get_next_items_to_fetch", side_effect=sqlite3.OperationalError("database is locked")), \
         patch("time.sleep") as mock_sleep:
        daemon.process_batch()
        mock_sleep.assert_called_once_with(5)


# --- failure classification -------------------------------------------------

def test_transient_failure_keeps_the_item_queued(tmp_path):
    """A 500 must leave the item in the queue, one priority level lower.

    Regression: the 500 path cleared api_priority and _promote_stale_items only
    promotes fetch_status 200, so a transient failure left every queue permanently.
    """
    db_path = str(tmp_path / "transient.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 5, "fetch_status": 200})

    daemon = _daemon(db_path)
    with patch("src.daemon.get_workshop_details_batch", return_value=None):
        daemon.process_batch()

    priority, status = _priority(db_path, 1)
    assert status == 500
    assert priority == 4, "expected one step down from the pre-fetch priority, still queued"


def test_transient_failure_floors_at_priority_one(tmp_path):
    """Repeated failures keep stepping down but never leave the queue."""
    db_path = str(tmp_path / "floor.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 1, "fetch_status": 200})

    daemon = _daemon(db_path)
    with patch("src.daemon.get_workshop_details_batch", return_value=None):
        for _ in range(3):
            daemon.process_batch()

    priority, status = _priority(db_path, 1)
    assert priority == 1, "priority 0 means 'not queued'; a transient failure must not reach it"
    assert status == 500


def test_permanent_failure_dequeues_and_marks_dead(tmp_path):
    """A 404 is recorded and the item is dequeued for good."""
    db_path = str(tmp_path / "permanent.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 5, "fetch_status": 200})

    daemon = _daemon(db_path)
    with patch("src.daemon.get_workshop_details_batch",
               return_value={1: {"status": 404, "publishedfileid": 1}}):
        daemon.process_batch()

    priority, status = _priority(db_path, 1)
    assert status == -1, "a permanent failure is marked dead"
    assert priority == 0, "and removed from the queue"


def test_unhandled_status_is_treated_as_temporary_not_success(tmp_path):
    """An unhandled code must not fall through and be counted as a success."""
    db_path = str(tmp_path / "unhandled.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 5, "fetch_status": 200})

    daemon = _daemon(db_path)
    with patch("src.daemon.get_workshop_details_batch",
               return_value={1: {"status": 403, "publishedfileid": 1}}), \
         patch("src.daemon.capture.record_failure") as mock_capture:
        daemon.process_batch()

    priority, status = _priority(db_path, 1)
    assert mock_capture.called, "the evidence must be recorded"
    assert mock_capture.call_args.kwargs["kind"] == "api_unhandled_status"
    assert status == 403, "the observed code is persisted, not overwritten to 200"
    assert priority == 4, "and the item stays queued for retry"


def test_transport_exception_is_temporary(tmp_path):
    """A failed batch request settles every id as a temporary 500, so they retry."""
    db_path = str(tmp_path / "transport.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 3, "fetch_status": 200})

    daemon = _daemon(db_path)
    # What the API helper returns for requests.exceptions.RequestException.
    with patch("src.daemon.get_workshop_details_batch", return_value=None):
        daemon.process_batch()

    priority, status = _priority(db_path, 1)
    assert status == 500
    assert priority == 2
