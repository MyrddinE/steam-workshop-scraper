"""Contract tests for the item-column vocabulary.

These lock the relationship between three things that were previously maintained
by hand and had already drifted apart:

  * the real columns of a freshly migrated database,
  * ``database.WORKSHOP_ITEM_COLUMNS`` -- the set of insertable columns,
  * ``daemon.MERGE_ITEM_KEYS`` / ``MERGE_EXCLUDED_KEYS`` -- the API merge allow-list.

The merge allow-list is now derived from the column set with one deliberate
exception (``tags``) and one deliberate exclusion (the queue-owned columns), so
adding a column no longer means remembering to edit a second literal set in
``daemon.py``.
"""

import sqlite3

import pytest

from src.database import WORKSHOP_ITEM_COLUMNS, initialize_database, get_connection
from src.daemon import MERGE_ITEM_KEYS, MERGE_EXCLUDED_KEYS, MERGE_IGNORED_KEYS

ITEM_TABLE = "workshop_items"


@pytest.fixture(scope="module")
def fresh_columns(tmp_path_factory):
    """The columns a brand-new database actually ends up with (all migrations run)."""
    db_path = str(tmp_path_factory.mktemp("schema") / "fresh.db")
    initialize_database(db_path)
    conn = get_connection(db_path)
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({ITEM_TABLE})")}
    conn.close()
    return columns


def test_schema_matches_insert_whitelist(fresh_columns):
    """Every real column is insertable, and the whitelist invents nothing."""
    assert fresh_columns == set(WORKSHOP_ITEM_COLUMNS)


def test_merge_keys_are_derived_from_columns(fresh_columns):
    """The merge allow-list stays in step with the column set automatically."""
    expected = (set(WORKSHOP_ITEM_COLUMNS) - MERGE_EXCLUDED_KEYS) | {"tags"}
    assert MERGE_ITEM_KEYS == expected


def test_tags_survive_the_merge_but_are_not_a_column(fresh_columns):
    """tags lives in the workshop_tags junction table now, but the merge must keep
    it because insert_or_update_item reads it to populate that table."""
    assert "tags" in MERGE_ITEM_KEYS
    assert "tags" not in WORKSHOP_ITEM_COLUMNS
    assert "tags" not in fresh_columns


def test_queue_owned_columns_never_survive_a_merge(fresh_columns):
    """flag_for_web_scrape / flag_for_image set these between the merge and the
    insert, so a stale value carried through the merge would clobber the flag."""
    for column in MERGE_EXCLUDED_KEYS:
        assert column not in MERGE_ITEM_KEYS, f"{column} must not survive the API merge"
        assert column in WORKSHOP_ITEM_COLUMNS, f"{column} should still be a real column"


def test_ignored_keys_are_the_excluded_ones_plus_result(fresh_columns):
    """`result` is the one API field dropped without a warning log."""
    assert MERGE_IGNORED_KEYS == MERGE_EXCLUDED_KEYS | {"result"}


def test_the_downloaded_latch_is_a_column_but_not_a_merge_key(fresh_columns):
    """`downloaded_at` is local state, not a Steam field.

    Like `own_subscribed` it is a real column the schema carries, but unlike
    every API-provided column it must never be written by the merge: only
    `src.workshop_folders` stamps it and only the subscription walk clears it, so
    a stray API key of that name could otherwise claim the green star.
    """
    assert "downloaded_at" in WORKSHOP_ITEM_COLUMNS
    assert "downloaded_at" in fresh_columns
    assert "downloaded_at" in MERGE_EXCLUDED_KEYS
    assert "downloaded_at" not in MERGE_ITEM_KEYS


def test_migrated_from_old_schema_matches_fresh(fresh_columns, tmp_path):
    """A database upgraded from the pre-rename schema ends up with the exact same
    workshop_items columns as a brand-new one.

    The old schema uses the historical column names (and TEXT timestamps) so the
    whole migration chain, including 13->14, has to run.
    """
    old_path = str(tmp_path / "old_schema.db")
    conn = sqlite3.connect(old_path)
    conn.execute("""
        CREATE TABLE workshop_items (
            workshop_id INTEGER PRIMARY KEY,
            dt_found TEXT,
            dt_updated TEXT,
            dt_attempted TEXT,
            status INTEGER,
            title TEXT,
            creator INTEGER,
            creator_appid INTEGER,
            consumer_appid INTEGER,
            filename TEXT,
            file_size INTEGER,
            preview_url TEXT,
            hcontent_file TEXT,
            hcontent_preview TEXT,
            short_description TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            visibility INTEGER,
            banned INTEGER,
            ban_reason TEXT,
            app_name TEXT,
            file_type INTEGER,
            subscriptions INTEGER,
            favorited INTEGER,
            views INTEGER,
            tags TEXT,
            extended_description TEXT
        )
    """)
    conn.execute(
        "INSERT INTO workshop_items (workshop_id, title, status) VALUES (1, 'pre-existing', 200)"
    )
    conn.commit()
    conn.close()

    initialize_database(old_path)

    conn = get_connection(old_path)
    migrated_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({ITEM_TABLE})")}
    conn.close()

    assert migrated_columns == fresh_columns == set(WORKSHOP_ITEM_COLUMNS)
    assert "last_fetch_attempted_at" in migrated_columns
    assert "dt_found" not in migrated_columns
    assert "time_created" not in migrated_columns
