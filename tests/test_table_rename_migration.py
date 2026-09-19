"""Migration 29->30: `users` -> `creators`, `app_tracking` -> `app_discovery`.

`users` holds Steam creators and there are no application users, so the table
becomes `creators`; `app_tracking`'s live columns are the discovery cursor and
the enrichment filters, so it becomes `app_discovery`.

The rename is the one migration whose two sides `_create_legacy_schema` sees on every
startup, and it has to satisfy three constraints at once:

* a fresh database must still be *built* with the historical names, because the
  chain replayed from version 0 names them at 13->14, 22->23 and 27->28;
* an already-renamed database must not get `users`/`app_tracking` resurrected
  by `CREATE TABLE IF NOT EXISTS`;
* `_safe_add_columns` re-raises anything that is not a duplicate-column error,
  so it too must be pointed at whichever table exists.

`_create_legacy_schema` resolves each name with `_current_table_name` and routes every
reference through it. These tests pin the fresh path, the v29 upgrade, the
re-initialisation and the "already renamed under the old marker" crash window.
"""

from src.database import (
    EXPECTED_VERSION,
    get_app_tracking,
    get_connection,
    get_creator,
    initialize_database,
    insert_or_update_creator,
    save_enrichment_filters,
    update_app_tracking_cursor,
)
from tests.conftest import restore_pre_rename_table_names

CREATOR_COLUMNS = {
    "steamid", "personaname", "personaname_en",
    "api_fetched_at", "translated_at", "translation_priority",
}
APP_DISCOVERY_COLUMNS = {
    "appid", "last_historical_date_scanned", "filter_text", "required_tags",
    "excluded_tags", "window_size", "enrichment_filters", "last_cursor",
}


def _table_names(db_path) -> set[str]:
    conn = get_connection(db_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    return names


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


def _seed_both_tables(db_path) -> None:
    insert_or_update_creator(db_path, {"steamid": 7, "personaname": "Author"})
    save_enrichment_filters(db_path, 294100, enrichment_filters='[{"field": "Title"}]')
    update_app_tracking_cursor(db_path, 294100, "saved_cursor")


def test_a_fresh_database_reaches_the_expected_version_under_the_new_names(db_path):
    assert EXPECTED_VERSION == 34
    assert _version(db_path) == EXPECTED_VERSION

    names = _table_names(db_path)
    assert "creators" in names and "users" not in names
    assert "app_discovery" in names and "app_tracking" not in names
    assert _columns(db_path, "creators") == CREATOR_COLUMNS
    assert _columns(db_path, "app_discovery") == APP_DISCOVERY_COLUMNS


def test_the_renamed_accessors_work_against_the_renamed_tables(db_path):
    _seed_both_tables(db_path)

    assert get_creator(db_path, 7)["personaname"] == "Author"
    assert get_app_tracking(db_path, 294100)["last_cursor"] == "saved_cursor"
    assert get_app_tracking(db_path, 294100)["enrichment_filters"] == \
        '[{"field": "Title"}]'


def test_upgrading_a_v29_database_renames_both_tables_and_keeps_the_rows(db_path):
    _seed_both_tables(db_path)

    # Reconstruct a v29 database: the tables still carry the historical names
    # and the marker says 29.
    conn = get_connection(db_path)
    restore_pre_rename_table_names(conn)
    conn.execute("PRAGMA user_version = 29")
    conn.commit()
    conn.close()
    assert {"users", "app_tracking"} <= _table_names(db_path)

    initialize_database(db_path)

    names = _table_names(db_path)
    assert "creators" in names and "users" not in names
    assert "app_discovery" in names and "app_tracking" not in names
    assert _version(db_path) == EXPECTED_VERSION
    # The rows moved with the tables, not just the names.
    assert get_creator(db_path, 7)["personaname"] == "Author"
    assert get_app_tracking(db_path, 294100)["last_cursor"] == "saved_cursor"


def test_reinitialising_a_v30_database_changes_nothing(db_path):
    _seed_both_tables(db_path)

    initialize_database(db_path)

    names = _table_names(db_path)
    assert "users" not in names and "app_tracking" not in names
    assert _version(db_path) == EXPECTED_VERSION
    # `_safe_add_columns` runs again on every startup; the resolved table must
    # not have accumulated a second copy of any column.
    assert _columns(db_path, "creators") == CREATOR_COLUMNS
    assert _columns(db_path, "app_discovery") == APP_DISCOVERY_COLUMNS
    assert get_creator(db_path, 7)["personaname"] == "Author"
    assert get_app_tracking(db_path, 294100)["last_cursor"] == "saved_cursor"


def test_initialising_twice_does_not_resurrect_the_old_names(db_path):
    """The resurrection trap: `CREATE TABLE IF NOT EXISTS users` must not run
    once the table has been renamed, or every startup grows an empty `users`
    and an empty `app_tracking` beside the live tables."""
    initialize_database(db_path)
    initialize_database(db_path)

    names = _table_names(db_path)
    assert "users" not in names, "an empty `users` was resurrected"
    assert "app_tracking" not in names, "an empty `app_tracking` was resurrected"


def test_the_rename_is_a_no_op_when_already_renamed_under_the_old_marker(db_path):
    """A crash can commit the rename but not the version bump.

    SQLite DDL is transactional, so the two renames land together; the marker
    then still says 29. The guard on "old exists and new does not" must make the
    next startup a no-op rather than raise "no such table: users".
    """
    _seed_both_tables(db_path)
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 29")
    conn.commit()
    conn.close()

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert "users" not in _table_names(db_path)
    assert get_creator(db_path, 7)["personaname"] == "Author"
    assert get_app_tracking(db_path, 294100)["last_cursor"] == "saved_cursor"
