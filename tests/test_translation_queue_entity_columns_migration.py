"""Migration 33->34: the translation_queue discriminator renamed.

`translation_queue` holds one row per text field awaiting translation, and
`item_type` says which table `item_id` belongs to -- a workshop item or a
creator. The pair read as if every row described an item, and the bare `item_id`
is the same vocabulary the workers use for a workshop id throughout the
codebase, so they become `entity_type`/`entity_id`. The stored values --
including the discriminators `'item'` and `'user'` -- are unchanged; only the
names move.

`idx_translation_queue_lookup` is named for the queue rather than a column, so
SQLite rewrites its *definition* in place on `RENAME COLUMN` and it keeps its
name; there is no index to drop or recreate. The trap is the **legacy** builder:
`_create_legacy_schema` runs on every startup, before the migration loop, so it
must resolve the two names dynamically -- they are `item_type`/`item_id` on a
fresh chain database and `entity_type`/`entity_id` on a migrated one, and naming
either pair unconditionally raises "no such column" whenever the statement
actually runs (SQLite short-circuits an existing index before resolving its
columns, so the migrated side is pinned by dropping the index first).

These tests pin the fresh path, the v33 upgrade (row counts and both columns'
profiles), the in-place index rewrite, the re-initialisation, the
already-renamed-under-the-old-marker crash window, and both sides of the legacy
builder -- including a `legacy_chain=True` fresh database, which starts from the
historical names and must still reach v34.
"""

import hashlib

from src.database import (
    EXPECTED_VERSION,
    _create_legacy_schema,
    get_connection,
    initialize_database,
)

RENAMES = (
    ("item_type", "entity_type"),
    ("item_id", "entity_id"),
)
OLD_NAMES = [old for old, _ in RENAMES]
NEW_NAMES = [new for _, new in RENAMES]

INDEX = "idx_translation_queue_lookup"

#: Rows carrying both discriminator values, a repeated id, an empty field and
#: duplicates, so the per-column checksum covers each case.
SEED = (
    ("item", 10, "title_en", "a", 3),
    ("item", 10, "short_description_en", "b", 1),
    ("item", 20, "", "c", 0),
    ("user", 30, "personaname_en", "d", 5),
    ("user", 30, "personaname_en", "e", 2),
)


def _columns(db_path, table: str = "translation_queue") -> set[str]:
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
        "AND tbl_name = 'translation_queue'")}
    conn.close()
    return definitions


def _index_text(db_path) -> str:
    return " ".join(_index_sql(db_path)[INDEX].split())


def _column_profile(db_path, column: str) -> dict:
    """Row count, NULL count, distinct count and a checksum over one column.

    The checksum is over the ordered values, so it catches a rename that
    rewrites or reorders a value, not only a row-count change.
    """
    conn = get_connection(db_path)
    values = [r[0] for r in conn.execute(
        f"SELECT {column} FROM translation_queue ORDER BY id")]
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
    """The profiles in the same positional order as ``names``."""
    return [_column_profile(db_path, name) for name in names]


def _seed(db_path) -> None:
    conn = get_connection(db_path)
    conn.executemany(
        "INSERT INTO translation_queue "
        "(entity_type, entity_id, field, original_text, priority, queued_at) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        SEED,
    )
    conn.commit()
    conn.close()


def _regress_to_v33(db_path) -> None:
    """Reconstruct the v33 shape: only these two column names are undone.

    SQLite rewrites the index definition with the `RENAME COLUMN`, so the index
    follows the columns back too; that is exactly the v33 shape this step
    upgrades.
    """
    conn = get_connection(db_path)
    for old, new in RENAMES:
        conn.execute(f"ALTER TABLE translation_queue RENAME COLUMN {new} TO {old}")
    conn.execute("PRAGMA user_version = 33")
    conn.commit()
    conn.close()


def test_a_fresh_database_names_the_two_columns(db_path):
    assert EXPECTED_VERSION == 34
    assert _version(db_path) == EXPECTED_VERSION
    columns = _columns(db_path)
    assert set(NEW_NAMES) <= columns
    assert not (set(OLD_NAMES) & columns)
    assert f"ON translation_queue ({NEW_NAMES[0]}, {NEW_NAMES[1]}, field)" in _index_text(db_path)


def test_upgrading_a_v33_database_keeps_every_row_and_value(db_path):
    _seed(db_path)
    before_rows = _column_profile(db_path, "entity_id")["rows"]
    assert before_rows == len(SEED)
    before = _profiles(db_path, NEW_NAMES)

    _regress_to_v33(db_path)

    # The regression really is the v33 shape, or the upgrade proves nothing.
    columns = _columns(db_path)
    assert set(OLD_NAMES) <= columns
    assert not (set(NEW_NAMES) & columns)
    assert _version(db_path) == 33
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


def test_the_lookup_index_keeps_its_name_and_follows_the_columns(db_path):
    """The index name stays (it names a queue); RENAME COLUMN rewrites the SQL."""
    _seed(db_path)
    _regress_to_v33(db_path)
    assert "ON translation_queue (item_type, item_id, field)" in _index_text(db_path)

    initialize_database(db_path)

    definitions = _index_sql(db_path)
    assert INDEX in definitions
    assert "ON translation_queue (entity_type, entity_id, field)" in _index_text(db_path)
    for old in OLD_NAMES:
        assert old not in definitions[INDEX]


def test_reinitialising_a_v34_database_changes_nothing(db_path):
    """The legacy builder runs on every startup, including on a v34 database."""
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


def test_the_legacy_builder_recreates_a_missing_index_on_a_migrated_database(db_path):
    """A v34 database missing the lookup index must get it back on the new names.

    `_create_legacy_schema` runs on every startup and owns this index because
    issue 57 needs it before the migration loop. On a database already carrying
    `entity_type`/`entity_id`, the index must be built on those names; a
    hardcoded `item_type`/`item_id` raises "no such column" whenever the index is
    actually absent. (With `CREATE INDEX IF NOT EXISTS`, SQLite short-circuits an
    index that already exists before resolving its columns, so a plain
    re-initialisation does not reach the statement -- dropping the index is what
    exposes it.)
    """
    _seed(db_path)
    before = _profiles(db_path, NEW_NAMES)
    conn = get_connection(db_path)
    conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
    conn.commit()
    conn.close()
    assert INDEX not in _index_sql(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert INDEX in _index_sql(db_path)
    assert "ON translation_queue (entity_type, entity_id, field)" in _index_text(db_path)
    assert _profiles(db_path, NEW_NAMES) == before


def test_the_rename_is_a_no_op_when_already_renamed_under_the_old_marker(db_path):
    """A crash can commit the rename but not the version bump.

    SQLite DDL is transactional, so both renames land together; the marker then
    still says 33. The guard on the column that is present must make the next
    startup a no-op rename rather than raise "no such column", and the values and
    index definition must be unchanged.
    """
    _seed(db_path)
    before = _profiles(db_path, NEW_NAMES)
    _regress_to_v33(db_path)

    # Simulate the committed-but-unstamped DDL: rename forward without bumping.
    conn = get_connection(db_path)
    for old, new in RENAMES:
        conn.execute(f"ALTER TABLE translation_queue RENAME COLUMN {old} TO {new}")
    conn.commit()
    conn.close()
    assert _version(db_path) == 33
    assert set(NEW_NAMES) <= _columns(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    columns = _columns(db_path)
    assert set(NEW_NAMES) <= columns
    assert not (set(OLD_NAMES) & columns)
    assert _profiles(db_path, NEW_NAMES) == before


def test_the_legacy_builder_resolves_each_side_of_the_rename(tmp_path):
    """A fresh legacy build starts historical and the chain must reach v34.

    One builder, two column-name resolutions: before the chain the table still
    carries `item_type`/`item_id` (the earlier migrations name them) and the
    index must be built on those; after 33->34 it carries
    `entity_type`/`entity_id`, and the next startup must build the index on
    those. A hardcoded pair passes one half and fails the other.
    """
    path = str(tmp_path / "legacy.db")
    conn = get_connection(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    _create_legacy_schema(conn.cursor(), conn)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(translation_queue)")}
    index_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (INDEX,)
    ).fetchone()[0]
    conn.commit()
    conn.close()

    assert {"item_type", "item_id"} <= columns, columns
    assert "ON translation_queue (item_type, item_id, field)" in " ".join(index_sql.split())

    # The whole chain, starting from those historical names, reaches v34.
    initialize_database(path, legacy_chain=True)

    assert _version(path) == EXPECTED_VERSION
    columns = _columns(path)
    assert {"entity_type", "entity_id"} <= columns
    assert not ({"item_type", "item_id"} & columns)
    assert "ON translation_queue (entity_type, entity_id, field)" in _index_text(path)
