"""Migration 37->38: a dead item keeps no queue flag and no queue row.

Issue 74: three writers raised a queue priority on an item that already existed
without guarding the dead flag -- the image worker's and the web worker's
"the item changed" bump, and the two discovery call sites. The API poll excludes
dead rows, so ``_settle_api_failure`` never runs for such a row again and
nothing cleared what they wrote. *Measured live* 2026-09-21: ``dead_queued`` and
``dead_items_by_queue`` both read 4, all four dead rows holding only
``api_priority`` (2, 3, 5 and 5).

The writers now guard (the first half of the fix); this step clears the rows
already stranded. It is data-only, the shape of 16->17 and 19->20 plus 35->36's
translation-queue delete, and it resolves the renamed columns because a rewound
version marker presents the historical names.
"""

import logging

from src import database
from src import metrics
from src.database import (
    EXPECTED_VERSION,
    get_connection,
    initialize_database,
    insert_or_update_item,
)
from tests.conftest import restore_pre_rename_table_names

FLAG_COLUMNS = (
    "api_priority", "web_scrape_priority", "image_priority", "translation_priority",
)


def _age_to_v37(db_path):
    """Rewind the marker so the next initialize runs only 37->38.

    37->38 changes no schema, so the marker alone reconstructs the
    pre-migration state. The current column names have to stay, though: the
    driver's ``_ensure_indexes`` step names them, so unlike the direct-step
    test below this path does not restore the historical spellings.
    """
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 37")
    conn.commit()
    conn.close()


def _flags(db_path, workshop_id):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT {', '.join(FLAG_COLUMNS)} FROM workshop_items "
            "WHERE workshop_id = ?", (workshop_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


def _queue_rows(db_path):
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


# ── the dead rows are cleared ────────────────────────────────────────────────


def test_every_queue_flag_is_cleared_on_a_dead_item(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "fetch_status": -1,
        "api_priority": 3, "web_scrape_priority": 5,
        "image_priority": 2, "translation_priority": 4,
    })
    _age_to_v37(db_path)

    initialize_database(db_path)

    assert _flags(db_path, 1) == {column: 0 for column in FLAG_COLUMNS}


def test_a_live_queued_item_is_untouched(db_path):
    """Only ``fetch_status = -1`` rows are the migration's population."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "live", "fetch_status": 200,
        "api_priority": 3, "web_scrape_priority": 5,
        "image_priority": 2, "translation_priority": 4,
    })
    _age_to_v37(db_path)

    initialize_database(db_path)

    assert _flags(db_path, 1) == {
        "api_priority": 3, "web_scrape_priority": 5,
        "image_priority": 2, "translation_priority": 4,
    }


def test_a_dead_items_translation_rows_go_but_a_creators_row_stays(db_path):
    """``entity_type`` separates the entities, not the numeric id."""
    insert_or_update_item(db_path, {
        "workshop_id": 7, "title": "gone", "fetch_status": -1,
        "api_priority": 5, "translation_priority": 3,
    })
    _queue_row(db_path, "item", 7, "title_en")
    _queue_row(db_path, "item", 7, "short_description_en")
    _queue_row(db_path, "user", 7, "personaname_en")
    _age_to_v37(db_path)

    initialize_database(db_path)

    assert _queue_rows(db_path) == [("user", 7, "personaname_en")]


def test_the_migration_reports_what_it_cleared(db_path, caplog):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "fetch_status": -1, "api_priority": 5,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "title": "gone too", "fetch_status": -1, "api_priority": 0,
    })
    _queue_row(db_path, "item", 1)
    _age_to_v37(db_path)

    with caplog.at_level(logging.INFO):
        initialize_database(db_path)

    assert "Cleared queue flags on 1 dead item(s) and removed 1 translation queue row(s)" \
        in caplog.text


def test_it_is_idempotent(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "fetch_status": -1,
        "api_priority": 3, "translation_priority": 2,
    })
    _queue_row(db_path, "item", 1)
    _age_to_v37(db_path)
    initialize_database(db_path)
    first = _flags(db_path, 1)
    first_queue = _queue_rows(db_path)

    _age_to_v37(db_path)
    initialize_database(db_path)

    assert _flags(db_path, 1) == first == {column: 0 for column in FLAG_COLUMNS}
    assert _queue_rows(db_path) == first_queue == []


def test_the_driver_reaches_the_expected_version(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": -1, "api_priority": 5,
    })
    _age_to_v37(db_path)

    initialize_database(db_path)

    conn = get_connection(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == EXPECTED_VERSION


# ── the metric this exists to move ───────────────────────────────────────────


def test_the_two_dead_queue_metrics_read_zero_afterwards(db_path):
    """The measured live shape: dead rows holding only api_priority."""
    insert_or_update_item(db_path, {"workshop_id": 1, "fetch_status": -1, "api_priority": 2})
    insert_or_update_item(db_path, {"workshop_id": 2, "fetch_status": -1, "api_priority": 3})
    insert_or_update_item(db_path, {"workshop_id": 3, "fetch_status": -1, "api_priority": 5})
    insert_or_update_item(db_path, {"workshop_id": 4, "fetch_status": -1, "api_priority": 5})
    _age_to_v37(db_path)

    initialize_database(db_path)

    values = metrics.values(metrics.compute(
        db_path, ["dead_queued", "dead_items_by_queue"]))
    assert values["dead_queued"] == 0
    assert values["dead_items_by_queue"] == {
        "web": 0, "image": 0, "translation": 0, "api": 0,
    }


# ── the historical column names ──────────────────────────────────────────────


def test_the_step_resolves_the_historical_column_names(db_path):
    """A marker rewound to 37 presents the pre-rename spellings.

    Only the steps above the marker replay, so 30->31's ``status``, 32->33's
    ``needs_web_scrape``/``needs_image`` and 33->34's ``item_type``/``item_id``
    renames do not run again. The step must resolve them rather than name the
    current spelling, exactly as 36->37 resolves its table.
    """
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "fetch_status": -1,
        "api_priority": 3, "web_scrape_priority": 5,
        "image_priority": 2, "translation_priority": 4,
    })
    _queue_row(db_path, "item", 1, "title_en")

    conn = get_connection(db_path)
    restore_pre_rename_table_names(conn)
    conn.execute("PRAGMA user_version = 37")
    conn.commit()

    database._migration_37_to_38(conn.cursor(), conn, db_path)
    conn.commit()

    row = conn.execute(
        "SELECT status, api_priority, needs_web_scrape, needs_image, "
        "translation_priority FROM workshop_items WHERE workshop_id = 1"
    ).fetchone()
    assert dict(row) == {
        "status": -1, "api_priority": 0, "needs_web_scrape": 0,
        "needs_image": 0, "translation_priority": 0,
    }
    assert conn.execute(
        "SELECT COUNT(*) FROM translation_queue WHERE item_type = 'item'"
    ).fetchone()[0] == 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 38
    conn.close()
