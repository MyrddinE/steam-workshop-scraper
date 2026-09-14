"""The full-text index must survive every write, and be repaired on migration.

Migration 4->5 created ``workshop_fts`` and populated it once, with no triggers
and no later rebuild, so on the production database it held 640,471 documents
against 1,725,544 items. Migration 14->15 rebuilds it and installs the sync
triggers. These tests pin both halves: the repair, and the maintenance.
"""
import pytest

from src.database import get_connection, initialize_database, insert_or_update_item

INDEXED_COLUMNS = (
    "title", "title_en", "short_description", "short_description_en",
    "extended_description", "extended_description_en",
)


def _fts_count(db_path, term, column="workshop_fts"):
    """Number of indexed rows matching ``term``."""
    conn = get_connection(db_path)
    try:
        return conn.execute(
            f"SELECT count(*) FROM {column} WHERE {column} MATCH ?", (term,)
        ).fetchone()[0]
    finally:
        conn.close()


def _doc_count(db_path):
    conn = get_connection(db_path)
    try:
        return conn.execute("SELECT count(*) FROM workshop_fts_docsize").fetchone()[0]
    finally:
        conn.close()


def _item_count(db_path):
    conn = get_connection(db_path)
    try:
        return conn.execute("SELECT count(*) FROM workshop_items").fetchone()[0]
    finally:
        conn.close()


def _trigger_names(db_path):
    conn = get_connection(db_path)
    try:
        return {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
                " AND name LIKE 'workshop_items_fts_%'"
            ).fetchall()
        }
    finally:
        conn.close()


def _drop_fts_triggers(db_path):
    conn = get_connection(db_path)
    try:
        for name in ("workshop_items_fts_insert", "workshop_items_fts_delete",
                     "workshop_items_fts_update"):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.commit()
    finally:
        conn.close()


# ── migration 15 ─────────────────────────────────────────────────────────────

def test_chain_reaches_terminal_schema_version(db_path):
    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
    finally:
        conn.close()


def test_migration_15_installs_all_three_triggers(db_path):
    assert _trigger_names(db_path) == {
        "workshop_items_fts_insert",
        "workshop_items_fts_delete",
        "workshop_items_fts_update",
    }


def test_migration_15_repairs_rows_missing_from_the_index(db_path):
    """The regression that matters: rows written while triggers were absent."""
    _drop_fts_triggers(db_path)
    # Raw SQL, so nothing but the (now missing) triggers could have indexed these.
    conn = get_connection(db_path)
    try:
        for wid in range(1, 26):
            conn.execute(
                "INSERT INTO workshop_items (workshop_id, title) VALUES (?, ?)",
                (wid, "orphaned"),
            )
        conn.commit()
    finally:
        conn.close()

    assert _doc_count(db_path) == 0, "precondition: index was not populated"

    # Rewind the version marker and re-run the initializer, which is what deploy does.
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA user_version = 14")
        conn.commit()
    finally:
        conn.close()

    initialize_database(db_path)

    assert _doc_count(db_path) == 25, "migration 15 did not rebuild the index"
    assert _fts_count(db_path, "orphaned") == 25


def test_migration_15_is_idempotent(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "stable"})
    before = _doc_count(db_path)

    initialize_database(db_path)

    assert _doc_count(db_path) == before
    assert _trigger_names(db_path) == {
        "workshop_items_fts_insert",
        "workshop_items_fts_delete",
        "workshop_items_fts_update",
    }


# ── trigger maintenance ──────────────────────────────────────────────────────

def test_insert_is_indexed(db_path):
    insert_or_update_item(db_path, {"workshop_id": 7, "title": "wallpaper engine"})
    assert _fts_count(db_path, "wallpaper") == 1


def test_update_replaces_the_old_tokens(db_path):
    insert_or_update_item(db_path, {"workshop_id": 7, "title": "alpha"})
    assert _fts_count(db_path, "alpha") == 1

    insert_or_update_item(db_path, {"workshop_id": 7, "title": "beta"})

    assert _fts_count(db_path, "beta") == 1, "updated value is not searchable"
    assert _fts_count(db_path, "alpha") == 0, "stale tokens left in the index"


def test_update_of_a_non_indexed_column_leaves_the_index_intact(db_path):
    insert_or_update_item(db_path, {"workshop_id": 7, "title": "gamma"})
    # api_priority is not one of the six indexed columns; the scoped UPDATE OF
    # trigger must not fire, and must not disturb the entry.
    insert_or_update_item(db_path, {"workshop_id": 7, "api_priority": 10})
    assert _fts_count(db_path, "gamma") == 1


@pytest.mark.parametrize("column", INDEXED_COLUMNS)
def test_every_indexed_column_is_searchable(db_path, column):
    token = f"{column}token"
    insert_or_update_item(db_path, {"workshop_id": 7, column: token})
    assert _fts_count(db_path, token) == 1


def test_delete_removes_the_entries(db_path):
    insert_or_update_item(db_path, {"workshop_id": 7, "title": "delta"})
    insert_or_update_item(db_path, {"workshop_id": 8, "title": "delta"})
    assert _fts_count(db_path, "delta") == 2

    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM workshop_items WHERE workshop_id = 7")
        conn.commit()
    finally:
        conn.close()

    assert _fts_count(db_path, "delta") == 1
    assert _doc_count(db_path) == 1


def test_doc_count_tracks_item_count(db_path):
    for wid in range(1, 11):
        insert_or_update_item(db_path, {"workshop_id": wid, "title": f"item{wid}"})
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM workshop_items WHERE workshop_id <= 4")
        conn.commit()
    finally:
        conn.close()

    assert _doc_count(db_path) == _item_count(db_path) == 6


def test_index_does_not_grow_unbounded_on_repeated_updates(db_path):
    """A rewritten title must replace its entry, not append a second one."""
    for i in range(20):
        insert_or_update_item(db_path, {"workshop_id": 7, "title": f"revision{i}"})

    assert _doc_count(db_path) == 1
    assert _fts_count(db_path, "revision19") == 1
    assert _fts_count(db_path, "revision0") == 0
