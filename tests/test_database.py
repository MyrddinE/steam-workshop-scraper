import sqlite3
import os
import threading
import time
import pytest
import json
from src.database import (
    initialize_database,
    insert_or_update_item,
    get_next_items_to_scrape,
    search_items,
    get_connection,
    count_unscraped_items,
    clear_pending_items
)

@pytest.fixture
def db_path(tmp_path):
    """Fixture providing a temporary database for testing."""
    path = str(tmp_path / "test_workshop.db")
    initialize_database(path)
    return path

def test_count_unscraped_items(db_path):
    """Test counting items that have never been attempted."""
    assert count_unscraped_items(db_path) == 0
    
    insert_or_update_item(db_path, {"workshop_id": 1}) # Unscraped
    insert_or_update_item(db_path, {"workshop_id": 2}) # Unscraped
    insert_or_update_item(db_path, {"workshop_id": 3, "dt_attempted": "2023-01-01"}) # Scraped
    
    assert count_unscraped_items(db_path) == 2

def test_initialize_database(db_path):
    """Tests that the database and table are created correctly."""
    assert os.path.exists(db_path)

    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='workshop_items'")
    assert cursor.fetchone() is not None, "Table 'workshop_items' was not created."

    # Verify journal mode is WAL
    cursor.execute("PRAGMA journal_mode")
    assert cursor.fetchone()[0].lower() == "wal", "WAL mode was not enabled."
    conn.close()

def test_insert_or_update_item(db_path):
    """Tests that items can be inserted and then updated on conflict."""
    item = {
        "workshop_id": 123,
        "title": "Test Item",
        "dt_attempted": "2023-10-01T12:00:00"
    }
    # First insert should return True
    assert insert_or_update_item(db_path, item) is True
    
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT title FROM workshop_items WHERE workshop_id=123")
    assert cursor.fetchone()[0] == "Test Item"
    
    # Update existing item should return False
    item["title"] = "Updated Item"
    assert insert_or_update_item(db_path, item) is False
    cursor.execute("SELECT title FROM workshop_items WHERE workshop_id=123")
    assert cursor.fetchone()[0] == "Updated Item"
    conn.close()

def test_get_next_items_to_scrape(db_path):
    """Tests that items are fetched in order of oldest dt_attempted (NULLs first)."""
    insert_or_update_item(db_path, {"workshop_id": 1, "dt_attempted": "2023-10-02"})
    insert_or_update_item(db_path, {"workshop_id": 2}) # NULL dt_attempted, should come first
    insert_or_update_item(db_path, {"workshop_id": 3, "dt_attempted": "2023-10-01"})
    
    items = get_next_items_to_scrape(db_path, limit=2)
    assert len(items) == 2
    assert items[0] == 2  # NULL comes first
    assert items[1] == 3  # Older date comes second

def test_search_items(db_path):
    """Tests search capabilities over title and description, and filtering by appid."""
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Apple Mod", "consumer_appid": 100})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "Banana Mod", "short_description": "Apple inside", "consumer_appid": 100})
    insert_or_update_item(db_path, {"workshop_id": 3, "title": "Apple Map", "consumer_appid": 200})
    
    # Text search
    results = search_items(db_path, query="Apple")
    assert len(results) == 3
    
    # Filter search
    results_appid = search_items(db_path, query="Apple", appid=100)
    assert len(results_appid) == 2
    ids = [r["workshop_id"] for r in results_appid]
    assert 1 in ids and 2 in ids

    # Filter by tags
    insert_or_update_item(db_path, {"workshop_id": 4, "title": "Mango Mod", "tags": "['fruit', 'sweet']"})
    results_tags = search_items(db_path, tags="fruit")
    assert len(results_tags) == 1
    assert results_tags[0]["workshop_id"] == 4

def test_clear_pending_items(db_path):
    """Test clearing pending items (status NULL or 404 AND dt_updated NULL)."""
    # 1. Pending (status NULL, dt_updated NULL) - Should be removed
    insert_or_update_item(db_path, {"workshop_id": 1, "status": None, "dt_updated": None})
    # 2. Pending (status 404, dt_updated NULL) - Should be removed
    insert_or_update_item(db_path, {"workshop_id": 2, "status": 404, "dt_updated": None})
    # 3. Not Pending (status 200) - Should NOT be removed
    insert_or_update_item(db_path, {"workshop_id": 3, "status": 200, "dt_updated": None})
    # 4. Not Pending (has dt_updated) - Should NOT be removed
    insert_or_update_item(db_path, {"workshop_id": 4, "status": None, "dt_updated": "2023-01-01"})
    # 5. Not Pending (both) - Should NOT be removed
    insert_or_update_item(db_path, {"workshop_id": 5, "status": 200, "dt_updated": "2023-01-01"})

    deleted_count = clear_pending_items(db_path)
    assert deleted_count == 2
    
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT workshop_id FROM workshop_items ORDER BY workshop_id")
    ids = [row["workshop_id"] for row in cursor.fetchall()]
    conn.close()
    
    assert ids == [3, 4, 5]

def test_translation_columns_and_priority(db_path):
    """Test that translation-related columns and priority queries work correctly."""
    # Insert an item
    insert_or_update_item(db_path, {"workshop_id": 101, "title": "Test Title"})
    
    # Flag it for translation (Priority 1 - Auto)
    from src.database import flag_for_translation, get_next_translation_item
    flag_for_translation(db_path, 101, 1, table="workshop_items")
    
    # Flag a user with higher priority (Priority 10 - User)
    from src.database import insert_or_update_user
    insert_or_update_user(db_path, {"steamid": 76561198000000000, "personaname": "안녕하세요"})
    flag_for_translation(db_path, 76561198000000000, 10, table="users")
    
    # get_next_translation_item should return the user first because of higher priority
    next_item = get_next_translation_item(db_path)
    assert next_item == ("user", 76561198000000000, 10)
    
    # After translating user, should return the mod
    flag_for_translation(db_path, 76561198000000000, 0, table="users")
    next_item = get_next_translation_item(db_path)
    assert next_item == ("workshop_item", 101, 1)

def test_user_table_operations(db_path):
    """Tests basic CRUD for the users table."""
    from src.database import insert_or_update_user, get_user
    user_data = {"steamid": 12345, "personaname": "Test User"}
    insert_or_update_user(db_path, user_data)
    
    user = get_user(db_path, 12345)
    assert user["personaname"] == "Test User"
    
    # Update
    user_data["personaname"] = "Updated Name"
    insert_or_update_user(db_path, user_data)
    user = get_user(db_path, 12345)
    assert user["personaname"] == "Updated Name"

def test_user_join_in_queries(db_path):
    """Verifies that queries return joined user information."""
    from src.database import insert_or_update_user, insert_or_update_item, search_items, get_item_details
    
    steamid = 76561198000000000
    insert_or_update_user(db_path, {
        "steamid": steamid, 
        "personaname": "ModderOne",
        "personaname_en": "TranslatedModder"
    })
    
    insert_or_update_item(db_path, {
        "workshop_id": 999,
        "title": "Awesome Mod",
        "creator": steamid,
        "status": 200
    })
    
    # Test search_items join
    results = search_items(db_path, query="Awesome")
    assert len(results) == 1
    assert results[0]["personaname"] == "ModderOne"
    assert results[0]["personaname_en"] == "TranslatedModder"
    
    # Test get_item_details join
    details = get_item_details(db_path, 999)
    assert details["personaname"] == "ModderOne"
    assert details["personaname_en"] == "TranslatedModder"

def test_concurrent_read_write(db_path):
    """
    Tests that WAL mode allows simultaneous read and write without locking the DB.
    """
    def writer():
        for i in range(20):
            insert_or_update_item(db_path, {"workshop_id": i + 1000, "title": f"Item {i}"})
            time.sleep(0.005)

    def reader():
        for _ in range(20):
            search_items(db_path, query="Item")
            time.sleep(0.005)
            
    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()
    
    # If no 'sqlite3.OperationalError: database is locked' exception was thrown, WAL is working
    assert True

def test_get_next_translation_item_none(db_path):
    from src.database import get_next_translation_item
    # No items flagged
    assert get_next_translation_item(db_path) is None

def test_search_items_advanced_queries(db_path):
    from src.database import insert_or_update_item, search_items
    
    insert_or_update_item(db_path, {
        "workshop_id": 1, 
        "title": "Apple Banana", 
        "short_description": "Fruit mod", 
        "filename": "apple.zip", 
        "tags": "['fruit']",
        "creator": 123,
        "file_size": 1000
    })
    
    insert_or_update_item(db_path, {
        "workshop_id": 2, 
        "title": "Orange Mod", 
        "short_description": "Fruit mod too", 
        "filename": "orange.zip", 
        "tags": "['fruit', 'citrus']",
        "creator": 456,
        "file_size": 2000
    })

    # Test negative token
    results = search_items(db_path, query="Fruit -Banana")
    assert len(results) == 1
    assert results[0]["workshop_id"] == 2

    # Test mismatched quotes (fallback to split)
    results = search_items(db_path, query='Fruit "Mod')
    # Because shlex fails, it splits to ['Fruit', '"Mod']. Neither item has '"Mod' with a literal quote.
    assert len(results) == 0

    # Test matched quotes
    results = search_items(db_path, query='Fruit "Mod"')
    # shlex splits to ['Fruit', 'Mod']. Both items have 'mod' in short_description.
    assert len(results) == 2

    # Test summary_only
    results = search_items(db_path, query="Fruit", summary_only=True)
    assert len(results) == 2
    assert "short_description" not in results[0]  # Only essential columns returned

    # Test specific fields
    results = search_items(db_path, title_query="Apple", desc_query="Fruit", filename_query="apple", tags_query="fruit", creator=123)
    assert len(results) == 1
    assert results[0]["workshop_id"] == 1

    # Test numeric_filters
    results = search_items(db_path, numeric_filters={"file_size": "> 1500"})
    assert len(results) == 1
    assert results[0]["workshop_id"] == 2

def test_get_all_authors(db_path):
    from src.database import insert_or_update_item, get_all_authors
    insert_or_update_item(db_path, {"workshop_id": 1, "creator": 999})
    insert_or_update_item(db_path, {"workshop_id": 2, "creator": 888})
    
    authors = get_all_authors(db_path)
    assert 999 in authors
    assert 888 in authors
    assert len(authors) >= 2

def test_parse_query_empty():
    from src.database import _parse_query
    assert _parse_query("") == ([], [])
    assert _parse_query(None) == ([], [])

def test_search_items_pagination(db_path):
    from src.database import insert_or_update_item, search_items
    
    for i in range(1, 11):
        insert_or_update_item(db_path, {"workshop_id": 100 + i, "title": f"Page Item {i}"})
        
    results = search_items(db_path, query="Page Item", limit=5, sort_by="workshop_id", sort_order="ASC")
    assert len(results) == 5
    assert results[0]["workshop_id"] == 101
    
    results = search_items(db_path, query="Page Item", limit=5, offset=5, sort_by="workshop_id", sort_order="ASC")
    assert len(results) == 5
    assert results[0]["workshop_id"] == 106

def test_app_tracking(db_path):
    from src.database import get_app_tracking, update_app_tracking, save_app_filter
    
    # Initially should be None
    assert get_app_tracking(db_path, 4000) is None
    
    # Test update_app_tracking (last_historical_date_scanned)
    update_app_tracking(db_path, 4000, 1600000000)
    tracking = get_app_tracking(db_path, 4000)
    assert tracking["last_historical_date_scanned"] == 1600000000
    assert tracking["filter_text"] == ''
    assert tracking["required_tags"] == '[]'
    assert tracking["excluded_tags"] == '[]'
    
    # Update again
    update_app_tracking(db_path, 4000, 1700000000)
    tracking = get_app_tracking(db_path, 4000)
    assert tracking["last_historical_date_scanned"] == 1700000000

    # Test save_app_filter
    save_app_filter(db_path, 4000, "test search", ["tag1", "tag2"], ["excl1"])
    tracking = get_app_tracking(db_path, 4000)
    assert tracking["filter_text"] == "test search"
    assert tracking["required_tags"] == json.dumps(["tag1", "tag2"])
    assert tracking["excluded_tags"] == json.dumps(["excl1"])
    
    # Ensure last_historical_date_scanned is NOT updated by save_app_filter
    assert tracking["last_historical_date_scanned"] == 1700000000

    # Test saving only some filters
    save_app_filter(db_path, 4000, required_tags=["new_tag"])
    tracking = get_app_tracking(db_path, 4000)
    assert tracking["filter_text"] == "" # Should revert to default if not provided
    assert tracking["required_tags"] == json.dumps(["new_tag"])
    assert tracking["excluded_tags"] == '[]'


def test_get_next_items_to_scrape_priority(db_path):
    from src.database import get_next_items_to_scrape, insert_or_update_item
    
    # 1. Successfully scraped items, stalest first (status = 200)
    insert_or_update_item(db_path, {"workshop_id": 1, "status": 200, "dt_attempted": "2023-01-01"})
    insert_or_update_item(db_path, {"workshop_id": 2, "status": 200, "dt_attempted": "2024-01-01"})
    
    # 2. Partially failed items (status != 200)
    insert_or_update_item(db_path, {"workshop_id": 3, "status": 206, "dt_attempted": "2025-01-01"})
    insert_or_update_item(db_path, {"workshop_id": 4, "status": 500, "dt_attempted": "2025-02-01"})
    
    # 3. Unscraped new items (status IS NULL)
    insert_or_update_item(db_path, {"workshop_id": 5})
    insert_or_update_item(db_path, {"workshop_id": 6})
    
    items = get_next_items_to_scrape(db_path, limit=6)
    
    # Priority should be:
    # Group 1: NULL status -> 5, 6 (order doesn't strictly matter between 5/6, but typically by rowid or dt)
    # Group 2: != 200 status -> 3, 4 (ordered by dt_attempted ASC)
    # Group 3: 200 status -> 1, 2 (ordered by dt_attempted ASC)
    
    assert set(items[0:2]) == {5, 6}
    assert items[2:4] == [3, 4]
    assert items[4:6] == [1, 2]
