"""The ordered migration table that ``initialize_database`` drives.

The driver is now short: create the schema, read ``PRAGMA user_version``, then
run every entry in ``MIGRATIONS`` whose target version is above the recorded
one. These tests pin the structure the driver relies on so a future migration
cannot be added to the code and silently left out of the table, entered with a
gap or a duplicate, or attached to the wrong version -- and so a fresh database
is known to reach ``EXPECTED_VERSION``.
"""

from src.database import (
    EXPECTED_VERSION,
    MIGRATIONS,
    get_connection,
    initialize_database,
)


def test_the_table_is_contiguous_and_ascending_from_one():
    """Every version 1..EXPECTED_VERSION exactly once, in order, no gaps."""
    versions = [version for version, _ in MIGRATIONS]
    assert versions == list(range(1, EXPECTED_VERSION + 1)), (
        "MIGRATIONS must list every target version 1.."
        f"{EXPECTED_VERSION} exactly once, ascending; got {versions}"
    )


def test_every_entry_points_at_the_function_for_its_own_version():
    """`_migration_A_to_B` is the entry for B, so table and defs cannot drift."""
    for version, migrate in MIGRATIONS:
        assert migrate.__name__ == f"_migration_{version - 1}_to_{version}"


def test_a_fresh_database_reaches_the_expected_version(tmp_path):
    db_path = str(tmp_path / "fresh.db")

    initialize_database(db_path)

    conn = get_connection(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == EXPECTED_VERSION
