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
         patch("src.daemon.get_workshop_details_api") as mock_api, \
         patch("src.daemon.get_user") as mock_get_user, \
         patch("src.daemon.insert_or_update_user") as mock_ins_user, \
         patch("src.daemon.flag_for_web_scrape") as mock_flag_web, \
         patch("time.sleep"):

        mock_api.return_value = {
            "title": "Pipeline Mod", "creator": 200, "tags": [{"tag": "test"}]
        }
        mock_get_user.return_value = {"steamid": 200, "dt_updated": "2026-01-01T00:00:00"}

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
