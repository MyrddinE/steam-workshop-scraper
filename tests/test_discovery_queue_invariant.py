"""A newly discovered item must be queued on a fresh database and a migrated one.

Cursor discovery used to insert only ``{"workshop_id": wid}`` and let the
``api_priority`` column default decide whether the row was queued. That default
depends on the database's history: ``CREATE TABLE`` declares ``DEFAULT 3``, but
the ``ALTER TABLE`` in migration 11->12 that adds the column to an older database
uses ``DEFAULT 0``. The fetch queue selects ``api_priority > 0``, so on a
migrated database -- which production is -- every discovered item was stranded.

These tests run the real discovery path against both histories. The migrated
database is built from the pre-v12 table shape, so migration 11->12 really does
add the column with ``DEFAULT 0``; rewinding the version marker of a fresh
database would not, because its column already came from ``CREATE TABLE``.
"""

import sqlite3
from unittest.mock import patch

from src.daemon import Daemon
from src.database import (
    get_connection,
    get_next_items_to_scrape,
    initialize_database,
)


def _config(db_path: str) -> dict:
    return {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0},
    }


def _make_migrated_db(path: str) -> None:
    """Build a database whose api_priority column came from migration 11->12.

    The table below is the pre-v12 shape: it still carries the historical
    ``dt_*``/``time_*`` names the later migrations rename, and it has no
    ``api_priority`` column. Letting ``initialize_database`` migrate it forward
    is what gives the column the migrated history's ``DEFAULT 0``.
    """
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE workshop_items (
            workshop_id INTEGER PRIMARY KEY,
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
            extended_description TEXT,
            language INTEGER,
            lifetime_subscriptions INTEGER,
            lifetime_favorited INTEGER,
            title_en TEXT,
            short_description_en TEXT,
            extended_description_en TEXT,
            translation_priority INTEGER DEFAULT 0,
            is_queued_for_subscription INTEGER DEFAULT 0,
            wilson_favorite_score REAL DEFAULT NULL,
            wilson_subscription_score REAL DEFAULT NULL,
            needs_web_scrape INTEGER DEFAULT 0,
            image_extension TEXT DEFAULT NULL,
            needs_image INTEGER DEFAULT 0,
            dt_found INTEGER,
            dt_updated INTEGER,
            dt_attempted INTEGER,
            dt_translated INTEGER
        )
    """)
    conn.execute("PRAGMA user_version = 11")
    conn.commit()
    conn.close()
    initialize_database(path)


def _api_priority_default(db_path: str) -> str:
    conn = get_connection(db_path)
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'workshop_items'"
    ).fetchone()[0]
    conn.close()
    return ddl


def _discover_one_item(db_path: str) -> int:
    """Run the cursor-discovery path and return the one workshop_id it found."""
    with patch("src.daemon.query_workshop_files") as mock_query, \
            patch("src.daemon.time.sleep"):
        mock_query.return_value = {
            "total": 1,
            "items": [{"publishedfileid": "4242"}],
            "next_cursor": "",
        }
        Daemon(_config(db_path)).seed_database(target_new=100)
    return 4242


def _queued_ids(db_path: str) -> list[int]:
    return [item["workshop_id"] for item in get_next_items_to_scrape(db_path)]


def test_the_two_database_histories_really_do_differ(tmp_path):
    """The invariant is only worth testing because the column defaults diverge.

    If a future change made both histories declare the same default, this test
    would fail and force the migration test above to be reconsidered rather than
    silently becoming unable to catch the regression.
    """
    fresh = str(tmp_path / "fresh_default.db")
    initialize_database(fresh)
    migrated = str(tmp_path / "migrated_default.db")
    _make_migrated_db(migrated)

    assert "api_priority INTEGER NOT NULL DEFAULT 3" in _api_priority_default(fresh)
    assert "api_priority INTEGER NOT NULL DEFAULT 0" in _api_priority_default(migrated)


def test_cursor_discovery_queues_on_a_fresh_database(tmp_path):
    db = str(tmp_path / "fresh.db")
    initialize_database(db)

    wid = _discover_one_item(db)

    conn = get_connection(db)
    priority = conn.execute(
        "SELECT api_priority FROM workshop_items WHERE workshop_id = ?", (wid,)
    ).fetchone()[0]
    conn.close()
    assert priority > 0
    assert _queued_ids(db) == [wid], "the API fetch queue must hand the item out"


def test_cursor_discovery_queues_on_a_migrated_database(tmp_path):
    """The regression: on a migrated database the discovered item landed at 0."""
    db = str(tmp_path / "migrated.db")
    _make_migrated_db(db)

    wid = _discover_one_item(db)

    conn = get_connection(db)
    priority = conn.execute(
        "SELECT api_priority FROM workshop_items WHERE workshop_id = ?", (wid,)
    ).fetchone()[0]
    conn.close()
    assert priority > 0
    assert _queued_ids(db) == [wid], "the API fetch queue must hand the item out"
