"""Migration 18->19: requeue items stranded by cursor discovery.

Cursor discovery inserted bare rows and let the ``api_priority`` column default
decide whether they were queued. That default is 3 in ``CREATE TABLE`` but 0 in
the ``ALTER TABLE`` that adds the column to an older database, and the fetch
queue selects ``api_priority > 0`` -- so on a migrated database (production)
every discovered row was stranded. Migration 15->16 requeued the rows this had
already produced, but left the cause in place, so it kept producing more; the
daemon change in the same release stops new ones. This migration requeues the
same population one more time.

The migration is an API-fetch queue repair only: it must not disturb dead rows
(``fetch_status = -1``) or the scrape, image and translation flags.
"""

import logging

from src.database import get_connection, initialize_database, insert_or_update_item, EXPECTED_VERSION
from tests.conftest import restore_pre_rename_table_names

STRANDED_WORK = {
    "web_scrape_priority": 5,
    "image_priority": 3,
    "translation_priority": 7,
}


def _age_to_v17(db_path):
    """Rewind so the next initialize_database runs 17->18 and then 18->19.

    A fresh test database is already at the terminal version and the migration
    adds no columns, so rewinding the marker is how the other migration tests
    build a pre-migration database.
    """
    conn = get_connection(db_path)
    restore_pre_rename_table_names(conn)
    conn.execute("PRAGMA user_version = 17")
    conn.commit()
    conn.close()


def _row(db_path, workshop_id):
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT api_priority, web_scrape_priority, image_priority, translation_priority "
        "FROM workshop_items WHERE workshop_id = ?",
        (workshop_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def _translation_queue_row(db_path, workshop_id):
    """Give the translation mirror a queue row, so it means "queued".

    Migration 22->23 clears a `translation_priority` with nothing in
    `translation_queue`, so a fixture that means "the translation queue is
    separate work and must survive" has to seed both halves of the pair.
    """
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO translation_queue (entity_type, entity_id, field, original_text, priority, queued_at) "
        "VALUES ('item', ?, 'title_en', 'テスト', 7, 1)",
        (workshop_id,),
    )
    conn.commit()
    conn.close()


def _version(db_path):
    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return version


def test_migration_18_to_19_requeues_never_attempted_rows(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 0})
    insert_or_update_item(db_path, {"workshop_id": 2, "api_priority": 0, "api_fetched_at": 12345})
    insert_or_update_item(db_path, {"workshop_id": 3, "fetch_status": 200, "api_priority": 0,
                                    "api_fetched_at": 12345})
    insert_or_update_item(db_path, {"workshop_id": 4, "fetch_status": -1, "api_priority": 0})
    insert_or_update_item(db_path, {"workshop_id": 5, "api_priority": 1})
    _age_to_v17(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    # 1 was discovered and never attempted: the migration's whole purpose.
    assert _row(db_path, 1)["api_priority"] == 1
    # 2 has a (if odd) fetch time, so it is not part of the stranded population.
    assert _row(db_path, 2)["api_priority"] == 0
    # 3 succeeded and is resting at 0 on purpose.
    assert _row(db_path, 3)["api_priority"] == 0
    # 4 is dead; a permanent failure is correctly dequeued and must stay there.
    assert _row(db_path, 4)["api_priority"] == 0
    # 5 is already queued and must not be disturbed.
    assert _row(db_path, 5)["api_priority"] == 1


def test_migration_18_to_19_does_not_touch_the_other_queue_flags(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 0, **STRANDED_WORK})
    _translation_queue_row(db_path, 1)
    _age_to_v17(db_path)

    initialize_database(db_path)

    assert _row(db_path, 1) == {"api_priority": 1, **STRANDED_WORK}


def test_migration_18_to_19_logs_the_number_of_rows_it_requeued(db_path, caplog):
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 0})
    insert_or_update_item(db_path, {"workshop_id": 2, "api_priority": 0})
    _age_to_v17(db_path)

    with caplog.at_level(logging.INFO):
        initialize_database(db_path)

    assert "Requeued 2 never-attempted items stranded by cursor discovery." in caplog.text


def test_migration_18_to_19_is_idempotent(db_path):
    """Re-running finds nothing: the recovery cannot re-queue what is already queued."""
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 0})
    _age_to_v17(db_path)
    initialize_database(db_path)
    assert _row(db_path, 1)["api_priority"] == 1

    _age_to_v17(db_path)
    initialize_database(db_path)

    assert _row(db_path, 1)["api_priority"] == 1
