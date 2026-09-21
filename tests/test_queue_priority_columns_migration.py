"""Migration 32->33: four `workshop_items` columns renamed for accuracy.

The four names no longer said what they hold:

* `needs_web_scrape` and `needs_image` hold a 1-10 priority, not a boolean, and
  the queue predicates already call them priorities; the `needs_` prefix is a
  documented historical exception. They become `web_scrape_priority` and
  `image_priority`.
* `image_extension` holds the server's *answer* -- a real extension, an HTTP
  status or a served non-image type -- not only a file extension, and
  `images.image_state()` is already the classifier and the derived payload key,
  so the column becomes the `image_answer` that classifier reads.
* `downloaded_at` is a one-way latch stamped when the folder scan first sees
  Steam's downloaded copy on disk, not a completion clock, so it becomes
  `steam_download_seen_at`.

The stored values are unchanged; only the names move. SQLite rewrites an index
*definition* on `RENAME COLUMN` but keeps the index *name*: no index on
`workshop_items` embeds any of these four in its name, so `idx_web_scrape_queue`
and `idx_image_queue` keep their names and their definitions follow the columns
in place -- there is no index to drop or recreate.

These tests pin the fresh path, the v32 upgrade (row counts and every value
profile preserved), the in-place index rewrite, the re-initialisation and the
"already renamed under the old marker" crash window.
"""

import hashlib

from src.database import (
    EXPECTED_VERSION,
    WORKSHOP_ITEM_COLUMNS,
    get_connection,
    initialize_database,
    insert_or_update_item,
)

RENAMES = (
    ("needs_web_scrape", "web_scrape_priority"),
    ("needs_image", "image_priority"),
    ("image_extension", "image_answer"),
    ("downloaded_at", "steam_download_seen_at"),
)
OLD_NAMES = [old for old, _ in RENAMES]
NEW_NAMES = [new for _, new in RENAMES]

WEB_INDEX = "idx_web_scrape_queue"
IMAGE_INDEX = "idx_image_queue"

#: Rows carrying every shape each column has: queued bands, a backlog zero, a
#: NULL and a duplicate, so the per-column checksum covers each case.
SEED = (
    (1, 0, 0, "jpg", None),
    (2, 1, 1, "png", 1000),
    (3, 5, 5, "404", 2000),
    (4, 10, 10, "html", 2000),
    (5, 1, None, None, None),
    (6, 1, 3, "", 3000),
    (7, None, 0, "svg+xml", 4000),
    (8, 5, 5, "jpg", 1000),
)


def _columns(db_path, table: str = "workshop_items") -> set[str]:
    conn = get_connection(db_path)
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return columns


def _version(db_path) -> int:
    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return version


def _index_sql(db_path) -> dict[str, str]:
    conn = get_connection(db_path)
    definitions = {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'workshop_items'")}
    conn.close()
    return definitions


def _column_profile(db_path, column: str) -> dict:
    """Row count, NULL count, distinct count and a checksum over one column.

    The checksum is over the ordered values, so it catches a rename that
    rewrites or reorders a value, not only a row-count change.
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


def _profiles(db_path, names) -> list[dict]:
    """The profiles in the same positional order as ``names``.

    A list, not a name-keyed dict: after the regression the values are read
    through the historical names, so comparing keyed dicts would compare names
    rather than values.
    """
    return [_column_profile(db_path, name) for name in names]


def _seed(db_path) -> None:
    for workshop_id, web, image, answer, seen in SEED:
        insert_or_update_item(db_path, {
            "workshop_id": workshop_id,
            "title": f"item {workshop_id}",
            "web_scrape_priority": web,
            "image_priority": image,
            "image_answer": answer,
            "steam_download_seen_at": seen,
        })


def _regress_to_v32(db_path) -> None:
    """Reconstruct the v32 shape: only these four column names are undone.

    The tables and the earlier column renames (`fetch_status`,
    `creator_steamid`) keep the names v32 has, so this is exactly the
    one-step-back shape migration 32->33 upgrades. The two queue indexes follow
    their columns through SQLite's `RENAME COLUMN` and keep their names.
    """
    conn = get_connection(db_path)
    for old, new in RENAMES:
        conn.execute(f"ALTER TABLE workshop_items RENAME COLUMN {new} TO {old}")
    conn.execute("PRAGMA user_version = 32")
    conn.commit()
    conn.close()


def test_a_fresh_database_names_the_four_columns(db_path):
    assert EXPECTED_VERSION == 38
    assert _version(db_path) == EXPECTED_VERSION
    columns = _columns(db_path)
    assert set(NEW_NAMES) <= columns
    assert not (set(OLD_NAMES) & columns)
    assert set(NEW_NAMES) <= set(WORKSHOP_ITEM_COLUMNS)
    assert not (set(OLD_NAMES) & set(WORKSHOP_ITEM_COLUMNS))


def test_upgrading_a_v32_database_keeps_every_row_and_value(db_path):
    _seed(db_path)
    before_rows = _column_profile(db_path, "web_scrape_priority")["rows"]
    assert before_rows == len(SEED)
    before = _profiles(db_path, NEW_NAMES)

    _regress_to_v32(db_path)

    # The regression really is the v32 shape, or the upgrade proves nothing.
    columns = _columns(db_path)
    assert set(OLD_NAMES) <= columns
    assert not (set(NEW_NAMES) & columns)
    assert _version(db_path) == 32
    # The same data, read through the historical names, has the same profiles.
    assert _profiles(db_path, OLD_NAMES) == before

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    columns = _columns(db_path)
    assert set(NEW_NAMES) <= columns
    assert not (set(OLD_NAMES) & columns)

    # Every row survived with every value untouched -- same count, NULLs,
    # distinct values and checksum per column.
    after = _profiles(db_path, NEW_NAMES)
    assert after == before
    for profile in after:
        assert profile["rows"] == before_rows


def test_the_two_queue_indexes_keep_their_names_and_follow_the_columns(db_path):
    """The index names stay (they name a queue); RENAME COLUMN rewrites the SQL."""
    _seed(db_path)
    _regress_to_v32(db_path)
    assert "needs_web_scrape" in _index_sql(db_path)[WEB_INDEX]
    assert "needs_image" in _index_sql(db_path)[IMAGE_INDEX]

    initialize_database(db_path)

    definitions = _index_sql(db_path)
    assert WEB_INDEX in definitions and IMAGE_INDEX in definitions
    assert "web_scrape_priority DESC, api_fetched_at ASC" in definitions[WEB_INDEX]
    assert "WHERE web_scrape_priority > 0" in definitions[WEB_INDEX]
    assert "image_priority DESC, api_fetched_at ASC" in definitions[IMAGE_INDEX]
    assert "WHERE image_priority > 0" in definitions[IMAGE_INDEX]
    for old in OLD_NAMES:
        assert old not in definitions[WEB_INDEX]
        assert old not in definitions[IMAGE_INDEX]


def test_reinitialising_a_v33_database_changes_nothing(db_path):
    _seed(db_path)
    before = _profiles(db_path, NEW_NAMES)
    columns_before = _columns(db_path)
    indexes_before = _index_sql(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert _columns(db_path) == columns_before, (
        "the legacy builder resurrected a historical column beside the new one"
    )
    assert _index_sql(db_path) == indexes_before
    assert _profiles(db_path, NEW_NAMES) == before


def test_the_rename_is_a_no_op_when_already_renamed_under_the_old_marker(db_path):
    """A crash can commit the rename but not the version bump.

    SQLite DDL is transactional, so all four renames land together; the marker
    then still says 32. The guard on the column that is present must make the
    next startup a no-op rename rather than raise "no such column", and the
    values and index definitions must be unchanged.
    """
    _seed(db_path)
    before = _profiles(db_path, NEW_NAMES)
    _regress_to_v32(db_path)

    # Simulate the committed-but-unstamped DDL: rename forward without bumping.
    conn = get_connection(db_path)
    for old, new in RENAMES:
        conn.execute(f"ALTER TABLE workshop_items RENAME COLUMN {old} TO {new}")
    conn.commit()
    conn.close()
    assert _version(db_path) == 32
    assert set(NEW_NAMES) <= _columns(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    columns = _columns(db_path)
    assert set(NEW_NAMES) <= columns
    assert not (set(OLD_NAMES) & columns)
    assert _profiles(db_path, NEW_NAMES) == before
