"""Migration 35->36: a dead item's translation queue rows go with it.

Issue 66: `_settle_api_failure` marked an item dead (`fetch_status = -1`) and
cleared the four item-level queue flags, but left the item's rows in
`translation_queue`. The translation poll hands out every row of that table with
no dead-item guard, so a dead item's fields were still translated and paid for.
The producer now deletes those rows in the same transaction as the status write;
this migration removes the rows that were already stranded when it did not.

Data-only: no table, column or index changes, so `_create_current_schema` needs
no mirror -- but `EXPECTED_VERSION` moves to 36 and
`tests/test_fresh_schema_path.py::test_schema_equivalence` still has to see both
paths report the same version.
"""

import logging

from src.database import (
    EXPECTED_VERSION,
    get_connection,
    initialize_database,
    insert_or_update_item,
)


def _age_to_v35(db_path):
    """Rewind the version marker so the next initialize runs 35->36.

    35->36 changes no schema, so unlike the tests for earlier migrations there
    are no renamed or dropped columns to restore: the marker alone reconstructs
    the pre-migration state.
    """
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 35")
    conn.commit()
    conn.close()


def _queue_row(db_path, entity_type, entity_id, field="title_en"):
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO translation_queue "
        "(entity_type, entity_id, field, original_text, priority, queued_at) "
        "VALUES (?, ?, ?, 'テスト', 3, 1)",
        (entity_type, entity_id, field),
    )
    conn.commit()
    conn.close()


def _queue_rows(db_path):
    """Every queue row as (entity_type, entity_id, field), in a stable order."""
    conn = get_connection(db_path)
    try:
        return [
            (row["entity_type"], row["entity_id"], row["field"])
            for row in conn.execute(
                "SELECT entity_type, entity_id, field FROM translation_queue "
                "ORDER BY entity_type, entity_id, field"
            )
        ]
    finally:
        conn.close()


def test_the_expected_version_is_37(db_path):
    """The data-only step still moves the version marker, so it is pinned."""
    conn = get_connection(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == EXPECTED_VERSION == 37


def test_migration_36_deletes_a_dead_items_queue_rows(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "fetch_status": -1, "api_priority": 0,
    })
    _queue_row(db_path, "item", 1, "title_en")
    _queue_row(db_path, "item", 1, "short_description_en")
    _age_to_v35(db_path)

    initialize_database(db_path)

    assert _queue_rows(db_path) == []


def test_migration_36_leaves_a_live_items_queue_rows(db_path):
    """A live item's queue row is outstanding work, not debris."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "live", "fetch_status": 200, "api_priority": 0,
    })
    _queue_row(db_path, "item", 1, "title_en")
    _age_to_v35(db_path)

    initialize_database(db_path)

    assert _queue_rows(db_path) == [("item", 1, "title_en")]


def test_migration_36_keeps_a_creators_row_whose_id_matches_a_dead_item(db_path):
    """`entity_type` separates the entities, not the numeric id.

    A creator's steamid can equal a dead item's workshop_id. The dead item's
    work is its own `entity_type = 'item'` row; the creator's row is a different
    entity and a different queue entry.
    """
    insert_or_update_item(db_path, {
        "workshop_id": 7, "title": "gone", "fetch_status": -1, "api_priority": 0,
    })
    _queue_row(db_path, "item", 7, "title_en")
    _queue_row(db_path, "user", 7, "personaname_en")
    _age_to_v35(db_path)

    initialize_database(db_path)

    assert _queue_rows(db_path) == [("user", 7, "personaname_en")]


def test_migration_36_logs_the_number_of_rows_it_removed(db_path, caplog):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "fetch_status": -1, "api_priority": 0,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "title": "gone too", "fetch_status": -1, "api_priority": 0,
    })
    _queue_row(db_path, "item", 1, "title_en")
    _queue_row(db_path, "item", 2, "title_en")
    _queue_row(db_path, "item", 2, "short_description_en")
    _age_to_v35(db_path)

    with caplog.at_level(logging.INFO):
        initialize_database(db_path)

    assert "Removed 3 translation queue rows of dead items." in caplog.text


def test_migration_36_is_idempotent(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "fetch_status": -1, "api_priority": 0,
    })
    _queue_row(db_path, "item", 1, "title_en")
    _age_to_v35(db_path)
    initialize_database(db_path)

    _age_to_v35(db_path)
    initialize_database(db_path)

    assert _queue_rows(db_path) == []
