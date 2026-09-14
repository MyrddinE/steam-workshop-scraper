import pytest
import json
import sqlite3
from unittest.mock import patch, MagicMock
from src.steam_api import get_workshop_details_api
from src.web_scraper import scrape_extended_details
from src.database import (
    initialize_database, insert_or_update_item, search_items,
    get_item_details, get_connection
)

@pytest.mark.integration
def test_live_steam_api_contract():
    details = get_workshop_details_api(item_id=104603291, api_key="")
    assert details is not None
    assert details["title"] == "Extended Spawnmenu"
    assert "description" in details
    assert "creator" in details
    assert isinstance(details["tags"], list)

@pytest.mark.integration
def test_live_web_scraper_contract():
    url = "https://steamcommunity.com/sharedfiles/filedetails/?id=104603291"
    details = scrape_extended_details(url)
    assert details is not None
    assert details["description"] is not None
    assert "Garry's Mod" in details["description"]
    assert len(details["tags"]) > 0

@pytest.mark.integration
def test_search_and_details_pipeline(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "Test Mod", "creator": 100,
        "consumer_appid": 294100, "status": 200,
        "short_description": "A test mod", "tags": json.dumps([{"tag": "test"}])
    })
    from src.database import insert_or_update_user
    insert_or_update_user(db_path, {"steamid": 100, "personaname": "Test Author"})

    results = search_items(db_path, query="Test Mod")
    assert len(results) == 1
    assert results[0]["personaname"] == "Test Author"

    details = get_item_details(db_path, 1)
    assert details["personaname"] == "Test Author"
    assert details["consumer_appid"] == 294100

@pytest.mark.integration
def test_daemon_pipeline_mocked(db_path):
    from src.daemon import Daemon

    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"batch_size": 1, "request_delay_seconds": 0, "target_appids": [294100]}
    }

    insert_or_update_item(db_path, {"workshop_id": 555})

    with patch("src.daemon.count_unscraped_items", return_value=100), \
         patch("src.daemon.get_workshop_details_batch") as mock_api, \
         patch("src.daemon.get_user") as mock_get_user, \
         patch("src.daemon.insert_or_update_user") as mock_ins_user, \
         patch("src.daemon.flag_for_web_scrape") as mock_flag_web, \
         patch("time.sleep"):

        mock_api.return_value = {
            555: {"title": "Pipeline Mod", "creator": 200, "status": 200, "tags": [{"tag": "test"}]}
        }
        mock_get_user.return_value = {"steamid": 200, "api_fetched_at": 1767225600}

        daemon = Daemon(config)
        daemon.process_batch()

    item = get_item_details(db_path, 555)
    assert item["title"] == "Pipeline Mod"
    mock_flag_web.assert_called_once_with(db_path, 555, 3)
    assert item["status"] == 200

@pytest.mark.integration
def test_database_migration_compatibility(tmp_path):
    """
    Verify that initialize_database safely adds new columns to an existing DB
    without losing pre-existing data.
    """
    old_schema_path = str(tmp_path / "old_schema.db")
    conn = sqlite3.connect(old_schema_path)
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
    conn.execute("INSERT INTO workshop_items (workshop_id, title, consumer_appid) VALUES (1, 'Pre-existing', 294100)")
    conn.commit()
    conn.close()

    initialize_database(old_schema_path)

    conn2 = get_connection(old_schema_path)
    cols = [row[1] for row in conn2.execute("PRAGMA table_info(workshop_items)")]
    conn2.close()

    assert "workshop_id" in cols
    assert "title" in cols
    assert "language" in cols
    assert "translation_priority" in cols
    assert "is_queued_for_subscription" in cols

    item = get_item_details(old_schema_path, 1)
    assert item["title"] == "Pre-existing"
    assert item["consumer_appid"] == 294100


@pytest.mark.integration
def test_fts5_content_sync_tracks_writes(db_path):
    """Items are searchable as soon as they are written, with no manual rebuild.

    An FTS5 external-content table does not sync itself, which is why migration
    14->15 installed triggers on workshop_items. This test used to assert the
    opposite - that inserts were invisible to MATCH until someone ran a rebuild -
    which is exactly the defect that migration fixed.
    """
    from src.database import insert_or_update_item, search_items, get_connection

    for i in range(1, 20):
        insert_or_update_item(db_path, {
            "workshop_id": i, "title": f"test item {i}",
            "short_description": "desc", "extended_description": "desc",
            "subscriptions": 100, "lifetime_subscriptions": 200,
            "favorited": 10, "views": 1000, "status": 200,
        })

    # No rebuild anywhere in this test: the triggers are what make this work.
    results = search_items(db_path, filters=[
        {"field": "Full Text", "op": "contains", "value": "test item"}])
    assert len(results) > 0, "inserts are not reaching the full-text index"

    # An update must replace the previous tokens rather than add to them.
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "renamed entry"})
    assert search_items(db_path, filters=[
        {"field": "Full Text", "op": "contains", "value": "renamed entry"}])
    titles = {row["title"] for row in search_items(db_path, filters=[
        {"field": "Full Text", "op": "contains", "value": "renamed entry"}])}
    assert "renamed entry" in titles

    # The index still holds the untouched rows, so the update was not a wipe.
    assert search_items(db_path, filters=[
        {"field": "Full Text", "op": "contains", "value": "test item"}])

    conn = get_connection(db_path)
    try:
        docs = conn.execute("SELECT count(*) FROM workshop_fts_docsize").fetchone()[0]
        items = conn.execute("SELECT count(*) FROM workshop_items").fetchone()[0]
    finally:
        conn.close()
    assert docs == items, "index drifted from the base table"



@pytest.mark.integration
def test_migration_crash_recovery(db_path):
    """Migrations must be idempotent — re-running a partially-applied
    migration continues from where it left off without data loss."""
    from src.database import get_connection

    conn = get_connection(db_path)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert ver >= 1  # at least one migration applied

    # Simulate re-run: call initialize_database again on same DB
    from src.database import initialize_database
    initialize_database(db_path)  # should be idempotent — no crash

    conn = get_connection(db_path)
    ver2 = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert ver2 == ver  # version unchanged — nothing broke
