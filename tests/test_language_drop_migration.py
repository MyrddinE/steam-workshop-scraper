"""Migration 23->24: the `language` column is dropped.

`language` was added expecting the Steam API to supply it, and no response this
project consumes can. `GetPublishedFileDetails` has no language field in its
response message; `language` exists in the request protocol only as the
*viewer's* localization parameter, which the client sets and never reads back.
The recorded response body in `tests/test_steam_api.py` carries none, and every
row in the live database is NULL. The column therefore backed a permanently
"N/A" tooltip line and a "Language ID" filter that could never match, so it is
removed rather than left as a field that advertises data Steam does not provide.

The tests below pin the two properties the migration has to guarantee: a fresh
database never grows the column, and an upgraded one loses it (with the index
that depended on it) without disturbing its rows.
"""

from src.database import (
    EXPECTED_VERSION,
    WORKSHOP_ITEM_COLUMNS,
    get_connection,
    initialize_database,
    insert_or_update_item,
)


def _table_info(db_path):
    conn = get_connection(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(workshop_items)")}
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='workshop_items'"
        )
    }
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return columns, indexes, version


def _regress_to_v23(db_path, language=6):
    """Put the pre-24 shape back: the column, its index, and the version marker."""
    conn = get_connection(db_path)
    conn.execute("ALTER TABLE workshop_items ADD COLUMN language INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_language ON workshop_items (language)")
    conn.execute(
        "UPDATE workshop_items SET language = ? WHERE workshop_id = 1", (language,)
    )
    conn.execute("PRAGMA user_version = 23")
    conn.commit()
    conn.close()


def test_migration_drops_the_language_column_and_its_index(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト", "status": 200})
    _regress_to_v23(db_path)

    initialize_database(db_path)

    columns, indexes, version = _table_info(db_path)
    assert version == EXPECTED_VERSION
    assert "language" not in columns
    assert "idx_language" not in indexes
    # The rest of the row is untouched.
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT title, status FROM workshop_items WHERE workshop_id = 1"
    ).fetchone()
    conn.close()
    assert dict(row) == {"title": "テスト", "status": 200}


def test_fresh_database_never_has_the_column(db_path):
    columns, indexes, _ = _table_info(db_path)
    assert "language" not in columns
    assert "idx_language" not in indexes
    assert set(columns) == set(WORKSHOP_ITEM_COLUMNS)


def test_migration_is_idempotent(db_path):
    _regress_to_v23(db_path)
    initialize_database(db_path)
    columns, _, _ = _table_info(db_path)
    assert "language" not in columns

    # A second rewind finds no column and must not fail on the missing index.
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 23")
    conn.commit()
    conn.close()
    initialize_database(db_path)

    columns, indexes, version = _table_info(db_path)
    assert version == EXPECTED_VERSION
    assert "language" not in columns
    assert "idx_language" not in indexes


def test_word_language_id_is_no_longer_a_filter_alias():
    """The alias pointed at a column that never held a value; it is gone with it."""
    from src.database import FIELD_NAME_MAP

    assert "Language ID" not in FIELD_NAME_MAP
    assert "language" not in FIELD_NAME_MAP.values()
