"""Migration 30->31: `workshop_items.status` -> `fetch_status`.

`status` in a 47-column `workshop_items` table is unqualified: it competes with
the HTTP status code, the subscribe outcome, the controller status and the
`status_counts` metric. The column holds this app's synthetic fetch outcome, not
an HTTP response code, so it becomes `fetch_status`; the stored values are
unchanged.

SQLite rewrites an index *definition* on `RENAME COLUMN` but keeps the index
*name*, so the three indexes named after the old column must be recreated under a
name that matches what they index:

* `idx_status` -> `idx_fetch_status`
* `idx_appid_status` -> `idx_appid_fetch_status`
* `idx_status_scraped_version` -> `idx_fetch_status_scraped_version`

These tests pin the fresh path, the v30 upgrade (row counts and values
preserved), the re-initialisation and the "already renamed under the old marker"
crash window.
"""

from src.database import (
    EXPECTED_VERSION,
    WORKSHOP_ITEM_COLUMNS,
    get_connection,
    initialize_database,
    insert_or_update_item,
)
from tests.conftest import restore_pre_rename_table_names

OLD_INDEXES = {"idx_status", "idx_appid_status", "idx_status_scraped_version"}
NEW_INDEXES = {
    "idx_fetch_status", "idx_appid_fetch_status", "idx_fetch_status_scraped_version",
}

#: The five states the column carries, one row each: fetched, dead, the legacy
#: not-found, a transient failure and discovered-but-never-fetched.
SEED = (
    (1, 200),
    (2, -1),
    (3, 404),
    (4, 500),
    (5, None),
)


def _columns(db_path) -> set[str]:
    conn = get_connection(db_path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(workshop_items)")}
    conn.close()
    return columns


def _index_names(db_path) -> set[str]:
    conn = get_connection(db_path)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'workshop_items'")}
    conn.close()
    return names


def _version(db_path) -> int:
    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return version


def _values_by_id(db_path) -> dict[int, object]:
    conn = get_connection(db_path)
    rows = {r["workshop_id"]: r["fetch_status"]
            for r in conn.execute("SELECT workshop_id, fetch_status FROM workshop_items")}
    conn.close()
    return rows


def _seed(db_path) -> None:
    for workshop_id, value in SEED:
        insert_or_update_item(db_path, {
            "workshop_id": workshop_id,
            "title": f"item {workshop_id}",
            "fetch_status": value,
        })


def _regress_to_v30(db_path) -> None:
    """Reconstruct the v30 shape: the old column name, the old index names, v30.

    `restore_pre_rename_table_names` also reverts the 29->30 table renames and
    the column rename; the new-named indexes follow the column name, so they are
    dropped and the old-named ones recreated.
    """
    conn = get_connection(db_path)
    restore_pre_rename_table_names(conn)
    for name in NEW_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON workshop_items (status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_appid_status "
        "ON workshop_items (consumer_appid, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_status_scraped_version "
        "ON workshop_items (status, scrape_version)"
    )
    conn.execute("PRAGMA user_version = 30")
    conn.commit()
    conn.close()


def test_a_fresh_database_names_the_column_fetch_status(db_path):
    assert EXPECTED_VERSION == 32
    assert _version(db_path) == EXPECTED_VERSION
    assert "fetch_status" in _columns(db_path)
    assert "status" not in _columns(db_path)
    assert "fetch_status" in WORKSHOP_ITEM_COLUMNS
    assert "status" not in WORKSHOP_ITEM_COLUMNS
    # The fresh path also creates the renamed indexes, since `_ensure_indexes`
    # runs on every startup.
    assert NEW_INDEXES <= _index_names(db_path)
    assert not (OLD_INDEXES & _index_names(db_path))


def test_upgrading_a_v30_database_renames_the_column_and_keeps_every_row(db_path):
    _seed(db_path)
    values_before = _values_by_id(db_path)
    count_before = len(values_before)
    assert count_before == len(SEED)

    _regress_to_v30(db_path)

    # The regression really is the v30 shape, or the upgrade proves nothing.
    assert "status" in _columns(db_path)
    assert "fetch_status" not in _columns(db_path)
    assert OLD_INDEXES <= _index_names(db_path)
    assert _version(db_path) == 30

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert "fetch_status" in _columns(db_path)
    assert "status" not in _columns(db_path)

    indexes = _index_names(db_path)
    assert NEW_INDEXES <= indexes, "the three indexes were not recreated under their new names"
    assert not (OLD_INDEXES & indexes), "an index named for the old column survived"

    # Every row survived, with its value untouched.
    values_after = _values_by_id(db_path)
    assert values_after == values_before
    assert values_after == {workshop_id: value for workshop_id, value in SEED}


def test_the_recreated_indexes_name_the_column_they_index(db_path):
    _seed(db_path)
    _regress_to_v30(db_path)
    initialize_database(db_path)

    conn = get_connection(db_path)
    definitions = {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'workshop_items'")}
    conn.close()

    assert definitions["idx_fetch_status"] == \
        "CREATE INDEX idx_fetch_status ON workshop_items (fetch_status)"
    assert "consumer_appid, fetch_status" in definitions["idx_appid_fetch_status"]
    assert "fetch_status, scrape_version" in definitions["idx_fetch_status_scraped_version"]
    for old_name in OLD_INDEXES:
        assert old_name not in definitions


def test_reinitialising_a_v31_database_changes_nothing(db_path):
    _seed(db_path)
    values_before = _values_by_id(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert NEW_INDEXES <= _index_names(db_path)
    assert not (OLD_INDEXES & _index_names(db_path))
    assert _values_by_id(db_path) == values_before


def test_the_rename_is_a_no_op_when_already_renamed_under_the_old_marker(db_path):
    """A crash can commit the rename but not the version bump.

    SQLite DDL is transactional, so the column rename lands; the marker then
    still says 30 and the three indexes still carry their old names (their
    definitions following the column). The guard on the column that is present
    must make the next startup a no-op rename rather than raise
    "no such column: status", and the index recreation must still run.
    """
    _seed(db_path)
    values_before = _values_by_id(db_path)
    _regress_to_v30(db_path)

    conn = get_connection(db_path)
    conn.execute("ALTER TABLE workshop_items RENAME COLUMN status TO fetch_status")
    conn.commit()
    conn.close()
    assert OLD_INDEXES <= _index_names(db_path), "the crash window keeps the old index names"

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert "fetch_status" in _columns(db_path)
    assert "status" not in _columns(db_path)
    assert NEW_INDEXES <= _index_names(db_path)
    assert not (OLD_INDEXES & _index_names(db_path))
    assert _values_by_id(db_path) == values_before
