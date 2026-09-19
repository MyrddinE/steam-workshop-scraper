"""Migration 16->17: a dead item must not stay in any work queue.

The permanent-failure path used to clear only ``api_priority`` when it marked an
item dead. ``needs_web_scrape``, ``needs_image`` and ``translation_priority``
were left set, and those queues select on their flag alone with no dead-item
guard, so the rows were retried forever and the queues could never drain. The
migration clears those flags on the rows already in the database; the daemon
change stops new ones being created.
"""

import logging

from src.database import get_connection, initialize_database, insert_or_update_item, EXPECTED_VERSION
from tests.conftest import restore_pre_rename_table_names

DEAD_FLAGS = {
    "needs_web_scrape": 5,
    "needs_image": 10,
    "translation_priority": 3,
}


def _age_to_v16(db_path):
    """Rewind the version marker so the next initialize_database runs 16->17.

    A fresh test database is already at the terminal version and the migration
    adds no columns, so rewinding is how the other migration tests build a
    pre-migration database.
    """
    conn = get_connection(db_path)
    restore_pre_rename_table_names(conn)
    conn.execute("PRAGMA user_version = 16")
    conn.commit()
    conn.close()


def _queue_flags(db_path, workshop_id):
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT needs_web_scrape, needs_image, translation_priority "
        "FROM workshop_items WHERE workshop_id = ?",
        (workshop_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def _translation_queue_row(db_path, workshop_id):
    """Give a live mirror a queue row to mirror.

    Migration 22->23 clears a `translation_priority` with nothing in
    `translation_queue`, so a fixture that means "this item is genuinely
    queued" has to seed both halves of the pair.
    """
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO translation_queue (item_type, item_id, field, original_text, priority, queued_at) "
        "VALUES ('item', ?, 'title_en', 'テスト', 3, 1)",
        (workshop_id,),
    )
    conn.commit()
    conn.close()


def test_migration_17_clears_queue_flags_on_dead_rows(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "status": -1, "api_priority": 0, **DEAD_FLAGS,
    })
    _age_to_v16(db_path)

    initialize_database(db_path)

    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == EXPECTED_VERSION
    assert _queue_flags(db_path, 1) == {
        "needs_web_scrape": 0, "needs_image": 0, "translation_priority": 0,
    }


def test_migration_17_leaves_live_rows_queued(db_path):
    """A live item's queue flags are work, not debris: they must survive."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "status": 200, "api_priority": 0, **DEAD_FLAGS,
    })
    _translation_queue_row(db_path, 1)
    _age_to_v16(db_path)

    initialize_database(db_path)

    assert _queue_flags(db_path, 1) == DEAD_FLAGS


def test_migration_17_logs_the_number_of_rows_it_cleared(db_path, caplog):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "status": -1, "api_priority": 0, **DEAD_FLAGS,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "status": -1, "api_priority": 0, **DEAD_FLAGS,
    })
    _age_to_v16(db_path)

    with caplog.at_level(logging.INFO):
        initialize_database(db_path)

    assert "Removed 2 dead items from the work queues." in caplog.text


def test_migration_17_is_idempotent(db_path):
    """Re-running on already-cleared rows must leave them clear.

    SQLite's rowcount counts every row the UPDATE matches, not only the rows
    whose value changed, so the logged count is not asserted here; the effect is.
    """
    insert_or_update_item(db_path, {
        "workshop_id": 1, "status": -1, "api_priority": 0, **DEAD_FLAGS,
    })
    _age_to_v16(db_path)
    initialize_database(db_path)

    _age_to_v16(db_path)
    initialize_database(db_path)

    assert _queue_flags(db_path, 1) == {
        "needs_web_scrape": 0, "needs_image": 0, "translation_priority": 0,
    }
