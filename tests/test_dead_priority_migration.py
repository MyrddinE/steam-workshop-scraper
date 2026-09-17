"""Migration 19->20: dead items give up their queue priority.

The permanent-failure path clears `api_priority` when it marks an item dead, so
this is not an ongoing leak — it is the rows that were already dead before that
line existed. They matter because `api_priority > 0` is what every count of
"queued for a fetch" looks at, and the statistics screen reports dead items still
holding a queue flag as `stuck_work`: a few thousand of them would peg a detector
whose entire value is that it reads zero unless something has regressed.
"""

import logging

from src.database import get_connection, initialize_database, insert_or_update_item


def _age_to_v19(db_path):
    """Rewind so the next initialize_database runs only migration 19->20.

    A fresh test database is already at the terminal version and this migration
    adds no columns, so rewinding the marker is how the other migration tests
    build a pre-migration database.
    """
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 19")
    conn.commit()
    conn.close()


def _priority(db_path, workshop_id):
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT api_priority FROM workshop_items WHERE workshop_id = ?", (workshop_id,)
    ).fetchone()
    conn.close()
    return row["api_priority"]


def test_dead_items_lose_their_queue_priority(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "status": -1, "api_priority": 7})
    insert_or_update_item(db_path, {"workshop_id": 2, "status": 200, "api_priority": 3})
    _age_to_v19(db_path)

    initialize_database(db_path)

    assert _priority(db_path, 1) == 0, "a dead item is in no queue"
    assert _priority(db_path, 2) == 3, "a live item keeps its place in the queue"


def test_a_dead_item_already_at_zero_is_left_alone(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "status": -1, "api_priority": 0})
    _age_to_v19(db_path)

    initialize_database(db_path)

    assert _priority(db_path, 1) == 0


def test_the_migration_reports_what_it_cleared(db_path, caplog):
    insert_or_update_item(db_path, {"workshop_id": 1, "status": -1, "api_priority": 5})
    insert_or_update_item(db_path, {"workshop_id": 2, "status": -1, "api_priority": 0})
    _age_to_v19(db_path)

    with caplog.at_level(logging.INFO):
        initialize_database(db_path)

    # Only the row that actually held a priority is counted.
    assert "Cleared the queue priority of 1 dead items." in caplog.text


def test_the_terminal_version_is_reached(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "status": -1, "api_priority": 5})
    _age_to_v19(db_path)

    initialize_database(db_path)

    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == 21
