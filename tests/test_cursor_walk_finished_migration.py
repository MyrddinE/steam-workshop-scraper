"""Migration 36->37: an AppID's cursor walk can be recorded as finished.

Issue 68: `seed_database`'s cursor walk stopped only on `fill_target` new items
or an empty `next_cursor`. Once the pages it walked were all already known,
neither was reachable, so the walk paged an exhausted catalogue until the API
refused, pass after pass. The walk now stops after five consecutive pages that
add nothing and records the conclusion in
`app_discovery.cursor_walk_finished`, so a restart cannot re-enable the deep
march. This migration adds that column.

`0` on every existing row: under the new rule no AppID's walk has finished yet,
and defaulting them to finished would disable cursor discovery outright. The
column is also declared by both schema builders, so on the ordinary upgrade the
builder adds it and the step records the version; the guarded `ALTER` is what
makes a database that reaches the step without the column gain it. The stop rule
itself is pinned by `tests/test_cursor_walk_stall.py`.
"""

from src import database
from src.database import (
    EXPECTED_VERSION,
    get_app_tracking,
    get_connection,
    initialize_database,
    update_app_tracking_cursor,
)

APPID = 431960


def test_the_expected_version_is_37():
    assert EXPECTED_VERSION == 38


def test_migration_36_to_37_adds_the_column_defaulting_to_zero(db_path):
    """A database at 36 gains the column at 37, and every existing AppID's walk
    is unfinished."""
    conn = get_connection(db_path)
    conn.execute("ALTER TABLE app_discovery DROP COLUMN cursor_walk_finished")
    conn.execute(
        "INSERT INTO app_discovery (appid, last_cursor) VALUES (?, ?)",
        (APPID, "saved"))
    conn.execute("PRAGMA user_version = 36")
    conn.commit()

    database._migration_36_to_37(conn.cursor(), conn, db_path)
    conn.commit()

    columns = {row[1] for row in conn.execute("PRAGMA table_info(app_discovery)")}
    assert "cursor_walk_finished" in columns
    row = conn.execute(
        "SELECT last_cursor, cursor_walk_finished FROM app_discovery WHERE appid = ?",
        (APPID,)).fetchone()
    assert dict(row) == {"last_cursor": "saved", "cursor_walk_finished": 0}
    # The step's own target, not the build's EXPECTED_VERSION: it is called
    # directly here, so a later version bump must not move this literal.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 37
    conn.close()


def test_an_existing_v36_database_reaches_37_with_the_unfinished_default(db_path):
    """The whole driver, not just the step: a v36 database ends at v37 with the
    column and the `0` default."""
    conn = get_connection(db_path)
    conn.execute("ALTER TABLE app_discovery DROP COLUMN cursor_walk_finished")
    conn.execute(
        "INSERT INTO app_discovery (appid, last_cursor) VALUES (?, ?)",
        (APPID, "saved"))
    conn.execute("PRAGMA user_version = 36")
    conn.commit()
    conn.close()

    initialize_database(db_path)

    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == EXPECTED_VERSION == 38
        row = conn.execute(
            "SELECT last_cursor, cursor_walk_finished FROM app_discovery WHERE appid = ?",
            (APPID,)).fetchone()
        assert dict(row) == {"last_cursor": "saved", "cursor_walk_finished": 0}
    finally:
        conn.close()


def test_a_fresh_database_declares_the_flag_unfinished(tmp_path):
    """The current-schema path mirrors the migration, so a fresh database has
    the column and defaults every AppID's walk to unfinished."""
    path = str(tmp_path / "fresh.db")
    initialize_database(path)

    conn = get_connection(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(app_discovery)")}
        assert "cursor_walk_finished" in columns
        conn.execute(
            "INSERT INTO app_discovery (appid, last_cursor) VALUES (?, ?)",
            (APPID, "saved"))
        conn.commit()
        row = conn.execute(
            "SELECT cursor_walk_finished FROM app_discovery WHERE appid = ?",
            (APPID,)).fetchone()
        assert row[0] == 0
    finally:
        conn.close()


def test_the_finished_flag_keeps_the_cursor(db_path):
    """The marker and the cursor are independent: the flag decides resumption,
    the cursor records position."""
    update_app_tracking_cursor(db_path, APPID, "the-cursor")
    database.mark_cursor_walk_finished(db_path, APPID)

    row = get_app_tracking(db_path, APPID)
    assert row["last_cursor"] == "the-cursor"
    assert row["cursor_walk_finished"] == 1
