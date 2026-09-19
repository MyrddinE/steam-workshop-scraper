"""The discovery guard must measure work the fetch queue can actually hand out.

Regression cover for a defect that stopped discovery permanently on production:
the guard compared against items that had never been fetched successfully, a
population disjoint from the fetch queue. Production read 890 "unscraped" while
`get_next_items_to_fetch` could return exactly 1 item, so every discovery pass
was skipped and the queue could never refill.
"""

from unittest.mock import patch

from src.daemon import Daemon
from src.database import (
    get_connection,
    initialize_database,
    count_fetchable_items,
    count_never_fetched_items,
    EXPECTED_VERSION,
)


def _config(db_path):
    return {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0},
    }


def _insert(db_path, rows):
    """rows: (workshop_id, status, api_priority, api_fetched_at)"""
    conn = get_connection(db_path)
    conn.executemany(
        "INSERT INTO workshop_items (workshop_id, status, api_priority, api_fetched_at) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


# --- the counter itself ----------------------------------------------------

def test_count_fetchable_items_matches_the_fetch_query(db_path):
    _insert(db_path, [
        (1, 200, 5, 100),    # queued, healthy -> fetchable
        (2, None, 3, None),  # discovered, queued -> fetchable
        (3, -1, 10, None),   # dead, still holding priority -> NOT fetchable
        (4, 200, 0, 100),    # not queued -> NOT fetchable
        (5, 500, 0, None),   # failed and dequeued -> NOT fetchable
    ])
    assert count_fetchable_items(db_path) == 2


def test_count_fetchable_items_is_disjoint_from_unscraped(db_path):
    """The two populations can be completely disjoint -- that was the whole bug."""
    _insert(db_path, [(i, 500, 0, None) for i in range(1, 51)])
    assert count_never_fetched_items(db_path) == 50
    assert count_fetchable_items(db_path) == 0


def test_count_never_fetched_items_keeps_its_meaning(db_path):
    """Pinned: this still means 'never successfully fetched', not 'queued'."""
    _insert(db_path, [
        (1, 200, 5, 100),   # fetched -> not unscraped
        (2, 500, 0, None),  # never succeeded -> unscraped
    ])
    assert count_never_fetched_items(db_path) == 1


# --- the guard -------------------------------------------------------------

@patch("src.daemon.query_workshop_files")
@patch("src.daemon.time.sleep")
def test_guard_runs_discovery_when_unscraped_backlog_is_unfetchable(mock_sleep, mock_query, db_path):
    """The regression: a large never-fetched, unqueued backlog must not suppress discovery."""
    _insert(db_path, [(i, 500, 0, None) for i in range(1, 151)])
    assert count_never_fetched_items(db_path) == 150
    assert count_fetchable_items(db_path) == 0

    mock_query.return_value = {"total": 10, "items": [{"publishedfileid": "999"}], "next_cursor": ""}

    Daemon(_config(db_path)).seed_database(fill_target=100)

    assert mock_query.called, "discovery must run when the fetch queue cannot supply work"


@patch("src.daemon.query_workshop_files")
@patch("src.daemon.time.sleep")
def test_guard_skips_discovery_when_fetchable_queue_is_deep(mock_sleep, mock_query, db_path):
    """A genuine backlog still suppresses discovery -- the guard's purpose is intact."""
    _insert(db_path, [(i, 200, 1, 100) for i in range(1, 151)])
    assert count_fetchable_items(db_path) == 150

    mock_query.return_value = {"total": 10, "items": [{"publishedfileid": "999"}], "next_cursor": ""}

    Daemon(_config(db_path)).seed_database(fill_target=100)

    assert not mock_query.called, "discovery should be skipped when the queue is genuinely full"


@patch("src.daemon.query_workshop_files")
@patch("src.daemon.time.sleep")
def test_guard_ignores_dead_items_holding_priority(mock_sleep, mock_query, db_path):
    """Dead rows keep api_priority > 0; they are not work and must not fill the queue."""
    _insert(db_path, [(i, -1, 10, None) for i in range(1, 201)])

    mock_query.return_value = {"total": 10, "items": [{"publishedfileid": "999"}], "next_cursor": ""}

    Daemon(_config(db_path)).seed_database(fill_target=100)

    assert mock_query.called, "dead items must not be counted as outstanding work"


@patch("src.daemon.query_workshop_files")
@patch("src.daemon.time.sleep")
def test_default_target_still_discovers_a_queue_above_the_old_buffer(mock_sleep, mock_query, db_path):
    """Pins the default: 150 fetchable items must leave it below the threshold.

    `seed_database` is called with no argument, so this exercises the default
    `DISCOVERY_FILL_TARGET` rather than a value the caller passed. Under the old
    default of 100 this queue was already "full" and the pass returned without a
    request; the raised default has to keep the same queue below the threshold
    so the pass fetches.
    """
    _insert(db_path, [(i, 200, 1, 100) for i in range(1, 151)])
    assert count_fetchable_items(db_path) == 150

    mock_query.return_value = {"total": 10, "items": [{"publishedfileid": "999"}], "next_cursor": ""}

    Daemon(_config(db_path)).seed_database()

    assert mock_query.called, "the default buffer must not be satisfied by 150 fetchable items"


# --- migration 16 recovery -------------------------------------------------

def _age_to_v15(db_path):
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 15")
    conn.commit()
    conn.close()


def test_migration_16_requeues_stranded_transient_failures(db_path):
    _insert(db_path, [
        (1, 500, 0, None),      # failed, never fetched -> requeue
        (2, 500, 0, 12345),     # failed after an earlier success -> requeue
        (3, None, 0, None),     # discovered but never attempted -> requeue
        (4, -1, 0, None),       # dead -> stays dequeued
        (5, 200, 3, 12345),     # healthy, queued -> untouched
    ])
    _age_to_v15(db_path)

    initialize_database(db_path)

    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    prio = {r["workshop_id"]: r["api_priority"]
            for r in conn.execute("SELECT workshop_id, api_priority FROM workshop_items")}
    conn.close()

    assert version == EXPECTED_VERSION
    assert prio[1] == 1
    assert prio[2] == 1
    assert prio[3] == 1
    assert prio[4] == 0, "permanent failures are correctly dequeued and must stay there"
    assert prio[5] == 3, "an already-queued item must not be disturbed"


def test_migration_16_leaves_successful_items_alone(db_path):
    """A status 200 item sitting at priority 0 is resting, not stranded."""
    _insert(db_path, [(1, 200, 0, 12345)])
    _age_to_v15(db_path)

    initialize_database(db_path)

    conn = get_connection(db_path)
    prio = conn.execute("SELECT api_priority FROM workshop_items WHERE workshop_id = 1").fetchone()[0]
    conn.close()
    assert prio == 0


def test_migration_16_is_idempotent(db_path):
    """Re-running finds nothing: the recovery cannot re-queue what is already queued."""
    _insert(db_path, [(1, 500, 0, None)])
    _age_to_v15(db_path)
    initialize_database(db_path)

    conn = get_connection(db_path)
    first = conn.execute("SELECT api_priority FROM workshop_items WHERE workshop_id = 1").fetchone()[0]
    conn.execute("PRAGMA user_version = 15")
    conn.commit()
    conn.close()

    initialize_database(db_path)

    conn = get_connection(db_path)
    second = conn.execute("SELECT api_priority FROM workshop_items WHERE workshop_id = 1").fetchone()[0]
    conn.close()
    assert first == second == 1
