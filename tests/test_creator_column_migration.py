"""Migration 31->32: `workshop_items.creator` -> `creator_steamid`.

`creator` in `workshop_items` joins `creators.steamid` and holds a SteamID64, but
the bare name reads as a display name or an object rather than the id it is, so
the column becomes `creator_steamid`. The stored values are unchanged.

SQLite rewrites an index *definition* on `RENAME COLUMN` but keeps the index
*name*, so the two indexes named after the old column must be recreated under a
name that matches what they index:

* `idx_creator` -> `idx_creator_steamid`
* `idx_creator_api_fetched_at` -> `idx_creator_steamid_api_fetched_at`

These tests pin the fresh path, the v31 upgrade (row counts and values
preserved), the re-initialisation and the "already renamed under the old marker"
crash window.
"""

import hashlib

from src.database import (
    EXPECTED_VERSION,
    FILTER_FIELD_TO_COLUMN,
    WORKSHOP_ITEM_COLUMNS,
    get_connection,
    initialize_database,
    insert_or_update_item,
)

OLD_INDEXES = {"idx_creator", "idx_creator_api_fetched_at"}
NEW_INDEXES = {"idx_creator_steamid", "idx_creator_steamid_api_fetched_at"}

#: The values the column carries: a real SteamID64, duplicates, a small id, a
#: zero and two NULLs, so a checksum over the column covers every shape.
SEED = (
    (1, 76561197996891752),
    (2, None),
    (3, 42),
    (4, 42),
    (5, 76561198000000001),
    (6, None),
    (7, 1),
    (8, 0),
)
EXPECTED_NULLS = 2
EXPECTED_DISTINCT = 5


def _columns(db_path, table: str = "workshop_items") -> set[str]:
    conn = get_connection(db_path)
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
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


def _column_profile(db_path, column: str) -> dict:
    """Row count, NULL count, distinct count and a checksum over one column.

    The checksum is over the ordered values, so it catches a rename that
    reorders or rewrites anything, not only a row-count change.
    """
    conn = get_connection(db_path)
    values = [r[0] for r in conn.execute(
        f"SELECT {column} FROM workshop_items ORDER BY workshop_id")]
    conn.close()
    digest = hashlib.sha256(
        "\x1f".join("" if v is None else str(v) for v in values).encode("utf-8")
    ).hexdigest()
    return {
        "rows": len(values),
        "nulls": sum(1 for v in values if v is None),
        "distinct": len({v for v in values if v is not None}),
        "sha256": digest,
    }


def _seed(db_path) -> None:
    for workshop_id, value in SEED:
        insert_or_update_item(db_path, {
            "workshop_id": workshop_id,
            "title": f"item {workshop_id}",
            "creator_steamid": value,
        })


def _regress_to_v31(db_path) -> None:
    """Reconstruct the v31 shape: the old column name, the old index names, v31.

    Only the 31->32 rename is undone -- the tables and `fetch_status` keep the
    names v31 has -- so this is the one-step-back shape the migration upgrades.
    """
    conn = get_connection(db_path)
    conn.execute("ALTER TABLE workshop_items RENAME COLUMN creator_steamid TO creator")
    for name in NEW_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_creator ON workshop_items (creator)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_creator_api_fetched_at "
        "ON workshop_items (creator, api_fetched_at)"
    )
    conn.execute("PRAGMA user_version = 31")
    conn.commit()
    conn.close()


def test_a_fresh_database_names_the_column_creator_steamid(db_path):
    assert EXPECTED_VERSION == 38
    assert _version(db_path) == EXPECTED_VERSION
    assert "creator_steamid" in _columns(db_path)
    assert "creator" not in _columns(db_path)
    assert "creator_appid" in _columns(db_path)
    assert "creator_steamid" in WORKSHOP_ITEM_COLUMNS
    assert "creator" not in WORKSHOP_ITEM_COLUMNS
    # The filter schema names the column too, but its field label stays "Author ID".
    assert FILTER_FIELD_TO_COLUMN["Author ID"] == "creator_steamid"
    # The fresh path also creates the renamed indexes, since `_ensure_indexes`
    # runs on every startup.
    assert NEW_INDEXES <= _index_names(db_path)
    assert not (OLD_INDEXES & _index_names(db_path))


def test_upgrading_a_v31_database_renames_the_column_and_keeps_every_row(db_path):
    _seed(db_path)
    before = _column_profile(db_path, "creator_steamid")
    assert before["rows"] == len(SEED)
    assert before["nulls"] == EXPECTED_NULLS
    assert before["distinct"] == EXPECTED_DISTINCT

    _regress_to_v31(db_path)

    # The regression really is the v31 shape, or the upgrade proves nothing.
    assert "creator" in _columns(db_path)
    assert "creator_steamid" not in _columns(db_path)
    assert OLD_INDEXES <= _index_names(db_path)
    assert _version(db_path) == 31
    assert _column_profile(db_path, "creator") == before

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert "creator_steamid" in _columns(db_path)
    assert "creator" not in _columns(db_path)
    # The unrelated neighbouring column was not touched by the rename.
    assert "creator_appid" in _columns(db_path)

    indexes = _index_names(db_path)
    assert NEW_INDEXES <= indexes, "the two indexes were not recreated under their new names"
    assert not (OLD_INDEXES & indexes), "an index named for the old column survived"

    # Every row survived with its value untouched -- same count, NULLs, distinct
    # values and checksum as before the regression.
    assert _column_profile(db_path, "creator_steamid") == before
    assert _column_profile(db_path, "creator_steamid")["sha256"] == before["sha256"]


def test_the_recreated_indexes_name_the_column_they_index(db_path):
    _seed(db_path)
    _regress_to_v31(db_path)
    initialize_database(db_path)

    conn = get_connection(db_path)
    definitions = {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'workshop_items'")}
    conn.close()

    assert definitions["idx_creator_steamid"] == \
        "CREATE INDEX idx_creator_steamid ON workshop_items (creator_steamid)"
    assert "creator_steamid, api_fetched_at" in \
        definitions["idx_creator_steamid_api_fetched_at"]
    for old_name in OLD_INDEXES:
        assert old_name not in definitions


def test_reinitialising_a_v32_database_changes_nothing(db_path):
    _seed(db_path)
    before = _column_profile(db_path, "creator_steamid")

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert NEW_INDEXES <= _index_names(db_path)
    assert not (OLD_INDEXES & _index_names(db_path))
    assert _column_profile(db_path, "creator_steamid") == before


def test_the_rename_is_a_no_op_when_already_renamed_under_the_old_marker(db_path):
    """A crash can commit the rename but not the version bump.

    SQLite DDL is transactional, so the column rename lands; the marker then
    still says 31 and the two indexes still carry their old names (their
    definitions following the column). The guard on the column that is present
    must make the next startup a no-op rename rather than raise
    "no such column: creator", and the index recreation must still run.
    """
    _seed(db_path)
    before = _column_profile(db_path, "creator_steamid")
    _regress_to_v31(db_path)

    conn = get_connection(db_path)
    conn.execute("ALTER TABLE workshop_items RENAME COLUMN creator TO creator_steamid")
    conn.commit()
    conn.close()
    assert OLD_INDEXES <= _index_names(db_path), "the crash window keeps the old index names"

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert "creator_steamid" in _columns(db_path)
    assert "creator" not in _columns(db_path)
    assert NEW_INDEXES <= _index_names(db_path)
    assert not (OLD_INDEXES & _index_names(db_path))
    assert _column_profile(db_path, "creator_steamid") == before
