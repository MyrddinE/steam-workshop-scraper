import sqlite3
import os
import threading
import time
import pytest
from src.database import (
    initialize_database,
    insert_or_update_item,
    get_next_items_to_scrape,
    search_items,
    get_connection,
    get_app_page,
    update_app_page,
    count_unscraped_items
)

@pytest.fixture
def db_path(tmp_path):
    """Fixture providing a temporary database for testing."""
    path = str(tmp_path / "test_workshop.db")
    initialize_database(path)
    return path

def test_app_state_pagination(db_path):
    """Test getting and updating the current page for an appid."""
    # Should default to 1
    assert get_app_page(db_path, 294100) == 1
    
    update_app_page(db_path, 294100, 2)
    assert get_app_page(db_path, 294100) == 2
    
    update_app_page(db_path, 294100, 5)
    assert get_app_page(db_path, 294100) == 5

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
