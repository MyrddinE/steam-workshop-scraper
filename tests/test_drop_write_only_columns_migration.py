"""Migration 34->35: dropping the three write-only columns.

Three columns were written and never read (issue 30):

* ``workshop_items.scrape_version`` -- written by the web worker and, until
  issue 7, overwritten by the image worker; no runtime code ever compared it,
  and only migration 13->14's cleanup read it;
* ``app_discovery.last_historical_date_scanned`` and ``app_discovery.window_size``
  -- written only by ``update_app_tracking``, which nothing but a test called.

SQLite refuses to drop an indexed column, and the two indexes
``idx_scraped_version`` and ``idx_fetch_status_scraped_version`` are both
defined on ``scrape_version`` at v34, so the step drops them first. They are
not recreated: ``_ensure_indexes`` no longer names them, while migrations
13->14 and 30->31 keep their historical creations byte-identical.

The two traps this file pins:

* the ``window_size`` entry in ``_create_legacy_schema``'s ``_safe_add_columns``
  list runs on *every* startup, so without the dropped-name guard re-initialising
  a v35 database would add the column back and undo the migration;
* ``tests/conftest.py``'s ``restore_pre_rename_table_names`` now restores the
  three columns for the migration tests that rewind a marker below 35.

These tests pin the fresh path, the v34 upgrade (row counts and column sets),
the two index removals, the re-initialisation, the
already-dropped-under-the-old-marker crash window, the ``window_size`` guard and
a ``legacy_chain=True`` fresh database that still reaches v35.
"""

from src.database import (
    EXPECTED_VERSION,
    WORKSHOP_ITEM_COLUMNS,
    get_connection,
    initialize_database,
    insert_or_update_creator,
    insert_or_update_item,
    save_enrichment_filters,
)

WORKSHOP_COLUMNS_AFTER = 46
DISCOVERY_COLUMNS = {
    "appid", "filter_text", "required_tags",
    "excluded_tags", "enrichment_filters", "last_cursor",
    "cursor_walk_finished",
}
DROPPED_COLUMNS = {
    "workshop_items": ("scrape_version",),
    "app_discovery": ("last_historical_date_scanned", "window_size"),
}
DROPPED_INDEXES = ("idx_scraped_version", "idx_fetch_status_scraped_version")

APP_TABLES = (
    "workshop_items", "creators", "translation_queue",
    "app_discovery", "tags", "workshop_tags",
)


def _columns(db_path, table: str) -> set[str]:
    conn = get_connection(db_path)
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return columns


def _version(db_path) -> int:
    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return version


def _index_names(db_path) -> set[str]:
    conn = get_connection(db_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    conn.close()
    return names


def _row_counts(db_path) -> dict[str, int]:
    conn = get_connection(db_path)
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in APP_TABLES
    }
    conn.close()
    return counts


def _seed(db_path) -> None:
    """Rows in every app table, so 'keeps every row count' has something to keep."""
    for wid, status, updated in ((1, 200, 5000), (2, -1, None), (3, None, 7000)):
        insert_or_update_item(db_path, {
            "workshop_id": wid,
            "title": f"item {wid}",
            "fetch_status": status,
            "steam_updated_at": updated,
            "consumer_appid": 4000,
        })
    insert_or_update_creator(db_path, {"steamid": 7, "personaname": "Author"})
    save_enrichment_filters(db_path, 4000, enrichment_filters='[{"field": "Title"}]')
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO translation_queue "
        "(entity_type, entity_id, field, original_text, priority, queued_at) "
        "VALUES ('item', 1, 'title_en', 'x', 3, 1)")
    conn.commit()
    conn.close()


def _regress_to_v34(db_path) -> None:
    """Reconstruct the v34 shape: the three columns and the two indexes back, v34.

    The seed values are set here, so the upgrade has real data in the dropped
    columns to discard. The columns are appended at the end of each table; the
    migration does not care about their position.
    """
    conn = get_connection(db_path)
    conn.execute("ALTER TABLE workshop_items ADD COLUMN scrape_version INTEGER")
    conn.execute("ALTER TABLE app_discovery ADD COLUMN last_historical_date_scanned INTEGER")
    conn.execute("ALTER TABLE app_discovery ADD COLUMN window_size INTEGER DEFAULT 2592000")
    conn.execute("UPDATE workshop_items SET scrape_version = steam_updated_at")
    conn.execute(
        "UPDATE app_discovery SET last_historical_date_scanned = 123456, window_size = 2592000")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scraped_version "
        "ON workshop_items (scrape_version)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fetch_status_scraped_version "
        "ON workshop_items (fetch_status, scrape_version)")
    conn.execute("PRAGMA user_version = 34")
    conn.commit()
    conn.close()


def test_a_fresh_database_has_no_dropped_column_or_index(db_path):
    assert EXPECTED_VERSION == 37
    assert _version(db_path) == EXPECTED_VERSION
    assert len(_columns(db_path, "workshop_items")) == WORKSHOP_COLUMNS_AFTER
    assert _columns(db_path, "app_discovery") == DISCOVERY_COLUMNS
    for table, columns in DROPPED_COLUMNS.items():
        assert not (set(columns) & _columns(db_path, table))
    assert not (set(DROPPED_INDEXES) & _index_names(db_path))


def test_upgrading_a_v34_database_keeps_every_row_count(db_path):
    _seed(db_path)
    _regress_to_v34(db_path)

    # The regression really is the v34 shape, or the upgrade proves nothing.
    assert _version(db_path) == 34
    assert {"scrape_version", "last_historical_date_scanned", "window_size"} <= (
        _columns(db_path, "workshop_items") | _columns(db_path, "app_discovery"))
    assert set(DROPPED_INDEXES) <= _index_names(db_path)
    before_counts = _row_counts(db_path)
    assert before_counts["workshop_items"] == 3
    assert before_counts["app_discovery"] == 1

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert _row_counts(db_path) == before_counts, "a row count changed across the drop"

    item_columns = _columns(db_path, "workshop_items")
    assert len(item_columns) == WORKSHOP_COLUMNS_AFTER
    assert item_columns == set(WORKSHOP_ITEM_COLUMNS)
    assert _columns(db_path, "app_discovery") == DISCOVERY_COLUMNS
    for table, columns in DROPPED_COLUMNS.items():
        assert not (set(columns) & _columns(db_path, table)), (
            f"{table} kept a dropped column"
        )
    # The two indexes that embedded `scrape_version` are gone from
    # sqlite_master, not merely renamed.
    assert not (set(DROPPED_INDEXES) & _index_names(db_path))


def test_reinitialising_a_v35_database_adds_nothing_back(db_path):
    """`_create_legacy_schema` runs on every startup; the drops must survive it."""
    _seed(db_path)
    before_counts = _row_counts(db_path)
    columns_before = {
        table: _columns(db_path, table) for table in DROPPED_COLUMNS
    }
    indexes_before = _index_names(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    for table, columns in DROPPED_COLUMNS.items():
        assert _columns(db_path, table) == columns_before[table], (
            f"the legacy builder resurrected a column on {table}"
        )
        assert not (set(columns) & _columns(db_path, table))
    assert _index_names(db_path) == indexes_before
    assert _row_counts(db_path) == before_counts


def test_the_legacy_builder_does_not_resurrect_window_size(db_path):
    """The `_safe_add_columns` dropped-name guard.

    `window_size` is still in the historical safe-add list because a fresh
    chain database needs it, but `_safe_add_columns` runs on every startup, so
    on a database already past 34->35 it must be skipped rather than added back.
    The other two are in no safe-add list at all, so the guard is the only one
    that needs it -- and all three are pinned here because a regression in any
    of them is the same bug.
    """
    assert "window_size" not in _columns(db_path, "app_discovery")
    assert "last_historical_date_scanned" not in _columns(db_path, "app_discovery")
    assert "scrape_version" not in _columns(db_path, "workshop_items")

    initialize_database(db_path)

    assert "window_size" not in _columns(db_path, "app_discovery")
    assert "last_historical_date_scanned" not in _columns(db_path, "app_discovery")
    assert "scrape_version" not in _columns(db_path, "workshop_items")


def test_the_legacy_builder_seeds_an_empty_discovery_table_without_the_dropped_column(tmp_path):
    """The populate step must not name `last_historical_date_scanned` once it is gone.

    A fresh current-schema database is created with an empty `app_discovery`;
    the next startup takes the legacy path and, finding it empty, seeds the
    AppID rows from `workshop_items`. Naming the dropped column there raises
    "no such column" on a v35 database.
    """
    path = str(tmp_path / "fresh.db")
    initialize_database(path)
    insert_or_update_item(path, {
        "workshop_id": 10, "title": "seeded", "consumer_appid": 4242,
        "steam_updated_at": 999,
    })

    initialize_database(path)

    conn = get_connection(path)
    rows = conn.execute(
        "SELECT appid FROM app_discovery WHERE appid = 4242").fetchall()
    conn.close()
    assert [r["appid"] for r in rows] == [4242], "the AppID row was not seeded"
    assert "window_size" not in _columns(path, "app_discovery")


def test_the_drop_is_a_no_op_when_already_dropped_under_the_old_marker(db_path):
    """A crash can commit the DDL but not the version bump.

    SQLite DDL is transactional, so the index and column drops land together;
    the marker then still says 34. The presence guards must make the next
    startup a no-op rather than raise "no such column", and the schema and rows
    must be unchanged.
    """
    _seed(db_path)
    _regress_to_v34(db_path)

    # Simulate the committed-but-unstamped DDL: apply the migration by hand
    # without bumping the marker.
    conn = get_connection(db_path)
    for index in DROPPED_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
    for table, columns in DROPPED_COLUMNS.items():
        for column in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.commit()
    conn.close()
    assert _version(db_path) == 34
    for table, columns in DROPPED_COLUMNS.items():
        assert not (set(columns) & _columns(db_path, table))

    before_counts = _row_counts(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    for table, columns in DROPPED_COLUMNS.items():
        assert not (set(columns) & _columns(db_path, table))
    assert not (set(DROPPED_INDEXES) & _index_names(db_path))
    assert _row_counts(db_path) == before_counts


def test_a_legacy_chain_fresh_database_still_reaches_v35(tmp_path):
    path = str(tmp_path / "legacy.db")
    initialize_database(path, legacy_chain=True)

    assert _version(path) == EXPECTED_VERSION
    assert len(_columns(path, "workshop_items")) == WORKSHOP_COLUMNS_AFTER
    assert _columns(path, "app_discovery") == DISCOVERY_COLUMNS
    assert not (set(DROPPED_INDEXES) & _index_names(path))
