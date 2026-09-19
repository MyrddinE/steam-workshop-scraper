"""Migration 17->18: requeue items dequeued without ever yielding a description.

Until ``17894f7`` the web worker tested the *dict* the scraper returned rather
than the description inside it. A page whose description selector did not match
comes back as a truthy dict with ``description: None``, so the item was written
with ``extended_description = NULL`` and ``web_scrape_priority = 0``: recorded as a
finished scrape and permanently out of the queue. The migration puts those rows
back at backlog priority; the worker change stops a page that was never the
item's from being cleared the same way.
"""

import logging

from src.database import get_connection, initialize_database, insert_or_update_item, EXPECTED_VERSION
from tests.conftest import restore_pre_rename_table_names


def _age_to_v17(db_path):
    """Rewind the version marker so the next initialize_database runs 17->18.

    A fresh test database is already at the terminal version and the migration
    adds no columns, so rewinding is how the other migration tests build a
    pre-migration database.
    """
    conn = get_connection(db_path)
    restore_pre_rename_table_names(conn)
    conn.execute("PRAGMA user_version = 17")
    conn.commit()
    conn.close()


def _row(db_path, workshop_id):
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT web_scrape_priority, image_priority, api_priority, translation_priority, "
        "extended_description FROM workshop_items WHERE workshop_id = ?",
        (workshop_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def _translation_queue_row(db_path, workshop_id):
    """Give the translation mirror a queue row, so it means "queued".

    Migration 22->23 clears a `translation_priority` with nothing in
    `translation_queue`, so this fixture has to seed both halves of the pair.
    """
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO translation_queue (entity_type, entity_id, field, original_text, priority, queued_at) "
        "VALUES ('item', ?, 'title_en', 'テスト', 3, 1)",
        (workshop_id,),
    )
    conn.commit()
    conn.close()


def test_migration_18_requeues_descriptionless_done_rows(db_path):
    """A row marked done with no description is work that was never done."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": 200, "web_scrape_priority": 0,
        "extended_description": None,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "fetch_status": 200, "web_scrape_priority": 0,
        "extended_description": "",
    })
    _age_to_v17(db_path)

    initialize_database(db_path)

    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == EXPECTED_VERSION
    assert _row(db_path, 1)["web_scrape_priority"] == 1
    assert _row(db_path, 2)["web_scrape_priority"] == 1


def test_migration_18_leaves_rows_that_have_a_description_alone(db_path):
    """A done row that produced a description is finished, not stranded."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": 200, "web_scrape_priority": 0,
        "extended_description": "a real description",
    })
    _age_to_v17(db_path)

    initialize_database(db_path)

    assert _row(db_path, 1)["web_scrape_priority"] == 0


def test_migration_18_leaves_dead_rows_alone(db_path):
    """A dead item can never complete, so it must not re-enter the queue."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": -1, "web_scrape_priority": 0,
        "extended_description": None,
    })
    _age_to_v17(db_path)

    initialize_database(db_path)

    assert _row(db_path, 1)["web_scrape_priority"] == 0


def test_migration_18_leaves_rows_still_queued_alone(db_path):
    """A row already waiting in the queue keeps the priority a producer gave it."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": 200, "web_scrape_priority": 5,
        "extended_description": None,
    })
    _age_to_v17(db_path)

    initialize_database(db_path)

    assert _row(db_path, 1)["web_scrape_priority"] == 5


def test_migration_18_touches_no_other_queue(db_path):
    """Only web_scrape_priority is requeued; the other queues are separate work."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": 200, "web_scrape_priority": 0,
        "image_priority": 10, "api_priority": 3, "translation_priority": 3,
        "extended_description": None,
    })
    _translation_queue_row(db_path, 1)
    _age_to_v17(db_path)

    initialize_database(db_path)

    row = _row(db_path, 1)
    assert row["web_scrape_priority"] == 1
    assert row["image_priority"] == 10
    assert row["api_priority"] == 3
    assert row["translation_priority"] == 3


def test_migration_18_logs_the_number_of_rows_it_requeued(db_path, caplog):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": 200, "web_scrape_priority": 0,
        "extended_description": None,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "fetch_status": 200, "web_scrape_priority": 0,
        "extended_description": None,
    })
    _age_to_v17(db_path)

    with caplog.at_level(logging.INFO):
        initialize_database(db_path)

    assert "Requeued 2 description-less items." in caplog.text


def test_migration_18_is_idempotent(db_path):
    """Re-running finds nothing to do: a requeued row is no longer at zero."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": 200, "web_scrape_priority": 0,
        "extended_description": None,
    })
    _age_to_v17(db_path)
    initialize_database(db_path)

    _age_to_v17(db_path)
    initialize_database(db_path)

    assert _row(db_path, 1)["web_scrape_priority"] == 1
