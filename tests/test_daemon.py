import pytest
from unittest.mock import patch, MagicMock, ANY
import signal
import json
from src.daemon import Daemon
from src.database import get_app_tracking, update_app_tracking

@pytest.fixture
def mock_config():
    return {
        "database": {"path": "test.db"},
        "api": {"key": "TEST_KEY"},
        "daemon": {"batch_size": 2, "request_delay_seconds": 0.01, "target_appids": [123]}
    }

def test_daemon_init_defaults():
    """Test that the Daemon correctly applies fallback defaults for missing config keys."""
    # Provide minimal valid config (only target_appids is strictly required now)
    minimal_config = {"daemon": {"target_appids": [456]}}
    daemon = Daemon(minimal_config)
    
    assert daemon.db_path == "workshop.db"
    assert daemon.api_key == ""
    assert daemon.batch_size == 10
    assert daemon.delay == 5
    assert daemon.target_appids == [456]

def test_daemon_init_missing_appids():
    """Test that the Daemon raises a ValueError if target_appids is omitted."""
    empty_config = {}
    with pytest.raises(ValueError, match="must be provided as a list"):
        Daemon(empty_config)
        
    invalid_config = {"daemon": {"target_appids": "not_a_list_just_a_string"}}
    with pytest.raises(ValueError, match="must be provided as a list"):
        Daemon(invalid_config)

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.scrape_extended_details')
@patch('src.daemon.get_user')
@patch('src.daemon.insert_or_update_user')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_daemon_process_batch_success(mock_sleep, mock_insert, mock_insert_user, mock_get_user, mock_scrape, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000 # Enough to skip seeding
    mock_get_items.return_value = [123]
    mock_api.return_value = {"title": "Test Mod", "creator": "111"}
    mock_scrape.return_value = {"description": "Cool mod", "tags": ["tag1"]}
    mock_get_user.return_value = {"steamid": 111, "dt_updated": "2026-01-01T00:00:00"} # Recent enough

    daemon = Daemon(mock_config)
    daemon.process_batch()

    mock_get_items.assert_called_once_with("test.db", limit=2)
    mock_api.assert_called_once_with(123, "TEST_KEY")
    mock_scrape.assert_called_once_with("https://steamcommunity.com/sharedfiles/filedetails/?id=123")
    
    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["workshop_id"] == 123
    assert inserted_data["title"] == "Test Mod"

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_daemon_process_batch_api_failure(mock_sleep, mock_insert, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [456]
    mock_api.return_value = {"status": 500, "publishedfileid": 456} # Mock API failure with status

    daemon = Daemon(mock_config)

    daemon.process_batch()
    
    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["status"] == 500

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.scrape_extended_details')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_daemon_process_batch_scrape_failure(mock_sleep, mock_insert, mock_scrape, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [789]
    mock_api.return_value = {"title": "Test Mod 2"}
    mock_scrape.return_value = None

    daemon = Daemon(mock_config)
    daemon.process_batch()
    
    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["status"] == 206

def test_daemon_graceful_shutdown(mock_config):
    daemon = Daemon(mock_config)
    daemon.handle_shutdown(signal.SIGINT, None)
    assert daemon.running is False

@patch('src.daemon.Daemon.process_batch')
def test_daemon_run_loop(mock_process, mock_config):
    daemon = Daemon(mock_config)
    def fake_process_batch():
        daemon.running = False
    mock_process.side_effect = fake_process_batch
    daemon.run()
    mock_process.assert_called_once()

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.Daemon.seed_database')
@patch('time.sleep')
def test_daemon_process_batch_empty(mock_sleep, mock_seed, mock_get_items, mock_count, mock_config):
    """Test behavior when no items are returned from the queue (triggers seeding)."""
    # First call returns empty, second call (after seed) also returns empty
    mock_count.return_value = 1000
    mock_get_items.return_value = []
    
    daemon = Daemon(mock_config)
    daemon.process_batch()
    
    # It should sleep for delay * 5 because it's still empty
    mock_sleep.assert_called_once_with(0.05) 

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
def test_daemon_process_batch_exit_early(mock_api, mock_get_items, mock_count, mock_config):
    """Test behavior when shutdown signal is received mid-batch."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [1, 2, 3]
    daemon = Daemon(mock_config)
    daemon.running = False # Simulate shutdown right before processing
    daemon.process_batch()
    mock_api.assert_not_called()

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.scrape_extended_details')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_daemon_process_batch_json_decode_error(mock_sleep, mock_insert, mock_scrape, mock_api, mock_get_items, mock_count, mock_config):
    """Test JSON decoding fallback when invalid tags exist."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [100]
    # Simulate API returning an invalid JSON string instead of a list
    mock_api.return_value = {"tags": "{invalid_json"}
    mock_scrape.return_value = {"tags": ["new_tag"]}

    daemon = Daemon(mock_config)
    daemon.process_batch()
    
    inserted_data = mock_insert.call_args[0][1]
    # It should fallback to empty list, then merge 'new_tag'
    assert inserted_data["tags"] == '["new_tag"]'

@patch('src.daemon_runner.initialize_database')
@patch('src.daemon_runner.load_config')
@patch('src.daemon_runner.Daemon')
def test_daemon_runner_initializes_db(mock_daemon, mock_load, mock_init):
    """Verifies that the runner calls initialize_database before running."""
    from src.daemon_runner import main
    mock_load.return_value = {"database": {"path": "dummy.db"}, "daemon": {"target_appids": [1]}}
    
    with patch('sys.argv', ['workshop-daemon']):
        main()
        
    mock_init.assert_called_once_with("dummy.db")
    mock_daemon.return_value.run.assert_called_once()

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.scrape_extended_details')
@patch('src.daemon.get_user')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
@patch('logging.info')
def test_daemon_process_batch_sanitization(mock_info, mock_sleep, mock_insert, mock_get_user, mock_scrape, mock_api, mock_get_items, mock_count, mock_config):
    """Test that the daemon maps schema keys correctly and strips/warns on invalid keys."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [123]
    mock_get_user.return_value = {"dt_updated": "2026-01-01T00:00:00"}
    mock_api.return_value = {
        "title": "Test", 
        "creator_app_id": 4000, 
        "consumer_app_id": 4000,
        "publishedfileid": "123",
        "description": "API description",
        "result": 1,
        "future_steam_feature": "magic" # This should trigger an INFO log
    }
    mock_scrape.return_value = {"description": "Web description", "tags": []}

    daemon = Daemon(mock_config)
    daemon.process_batch()
    
    inserted_data = mock_insert.call_args[0][1]
    
    # 1. Verify expected mapping occurred
    assert inserted_data["creator_appid"] == 4000
    assert inserted_data["consumer_appid"] == 4000
    assert inserted_data["short_description"] == "API description"
    
    # 2. Verify old/invalid keys were removed
    assert "creator_app_id" not in inserted_data
    assert "consumer_app_id" not in inserted_data
    assert "publishedfileid" not in inserted_data
    assert "description" not in inserted_data
    assert "result" not in inserted_data
    assert "future_steam_feature" not in inserted_data
    
    # 3. Verify that the unknown key triggered an INFO log
    mock_info.assert_any_call("Discarding unknown API column: 'future_steam_feature' with value 'magic' for item 123")

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.scrape_extended_details')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_daemon_translation_flagging(mock_sleep, mock_insert, mock_scrape, mock_api, mock_get_items, mock_count, mock_config):
    """Test that items with non-ASCII titles are flagged for translation."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [200]
    # Korean title
    mock_api.return_value = {"title": "안녕하세요", "workshop_id": 200}
    mock_scrape.return_value = {"description": "ASCII desc", "tags": []}
    
    daemon = Daemon(mock_config)
    daemon.process_batch()
    
    # Check the last call to insert_or_update_item
    # It's called multiple times (API then Scraper), we want the final one
    final_call_data = mock_insert.call_args[0][1]
    assert final_call_data["translation_priority"] == 1

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.scrape_extended_details')
@patch('src.daemon.get_player_summaries')
@patch('src.daemon.get_user')
@patch('src.daemon.insert_or_update_user')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_daemon_user_fetching(mock_sleep, mock_insert_item, mock_insert_user, mock_get_user, mock_summaries, mock_scrape, mock_api, mock_get_items, mock_count, mock_config):
    """Test that the daemon fetches user details and flags for translation."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [300]
    mock_api.return_value = {"title": "Mod", "creator": "777", "workshop_id": 300}
    mock_scrape.return_value = {"description": "Desc", "tags": []}
    mock_get_user.return_value = None # Force update
    
    # Mock Steam API for user
    mock_summaries.return_value = {777: {"personaname": "안녕하세요"}}
    
    daemon = Daemon(mock_config)
    daemon.process_batch()
    
    # Verify user was inserted with translation flag
    mock_insert_user.assert_called_once()
    user_data = mock_insert_user.call_args[0][1]
    assert user_data["steamid"] == 777
    assert user_data["personaname"] == "안녕하세요"
    assert user_data["translation_priority"] == 1

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.scrape_extended_details')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.get_user')
@patch('time.sleep')
def test_daemon_tag_normalization(mock_sleep, mock_get_user, mock_insert, mock_scrape, mock_api, mock_get_items, mock_count, mock_config):
    """Test that dictionary tags from API are correctly normalized and merged."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [101]
    mock_get_user.return_value = {"dt_updated": "2026-01-01T00:00:00"}
    # API returns list of dicts
    mock_api.return_value = {"workshop_id": 101, "tags": [{"tag": "Mod"}, {"tag": "1.5"}]}
    # Scraper returns list of strings
    mock_scrape.return_value = {"tags": ["Mod", "NewTag"]}

    daemon = Daemon(mock_config)
    daemon.process_batch()
    
    inserted_data = mock_insert.call_args[0][1]
    tags = json.loads(inserted_data["tags"])
    assert sorted(tags) == sorted(["Mod", "1.5", "NewTag"])

def test_expand_user_discovery():
    from src.daemon import Daemon
    from src.database import get_connection, insert_or_update_item, initialize_database
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        db_path = tf.name
        
    try:
        initialize_database(db_path)
        
        # Add a workshop item with a creator that is not in the users table
        insert_or_update_item(db_path, {"workshop_id": 999, "creator": "123456789"})
        
        config = {
            "database": {"path": db_path},
            "api": {"key": "test_key"},
            "daemon": {"target_appids": [1062090], "batch_size": 5}
        }
        
        daemon = Daemon(config)
        
        # Mock get_player_summaries to simulate API response
        with patch('src.daemon.get_player_summaries') as mock_summaries:
            mock_summaries.return_value = {123456789: {"personaname": "TestUser"}}
            
            daemon.expand_user_discovery()
            
            # Verify the API was called with the missing ID
            mock_summaries.assert_called_once_with([123456789], "test_key")
            
            # Verify the user was added to the users table
            conn = get_connection(db_path)
            cursor = conn.execute("SELECT personaname FROM users WHERE steamid = 123456789")
            user = cursor.fetchone()
            conn.close()
            
            assert user is not None
            assert user["personaname"] == "TestUser"
            
        # Test placeholder for missing user
        insert_or_update_item(db_path, {"workshop_id": 888, "creator": "987654321"})
        with patch('src.daemon.get_player_summaries') as mock_summaries:
            mock_summaries.return_value = {} # Return empty (user not found)
            
            daemon.expand_user_discovery()
            
            conn = get_connection(db_path)
            cursor = conn.execute("SELECT personaname FROM users WHERE steamid = 987654321")
            user = cursor.fetchone()
            conn.close()
            
            assert user is not None
            assert user["personaname"] == "SteamID:987654321"

    finally:
        os.remove(db_path)

def test_process_batch_scraper_failure(tmp_path):
    from src.daemon import Daemon
    from src.database import get_connection, initialize_database, insert_or_update_item, update_app_tracking
    
    db_path = tmp_path / "test.db"
    initialize_database(str(db_path))
    
    config = {
        "database": {"path": str(db_path)},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [999999], "batch_size": 1, "request_delay_seconds": 0}
    }
    
    daemon = Daemon(config)
    
    # Mock API to return one item, but Web Scraper to fail
    with patch('src.daemon.get_workshop_details_api') as mock_api, \
         patch('src.daemon.scrape_extended_details', return_value=None), \
         patch('src.daemon.discover_items_by_date_html', return_value=[]), \
         patch('src.daemon.get_next_items_to_scrape', return_value=[123]) as mock_get_next, \
         patch('src.daemon.count_unscraped_items', return_value=1000): # High count to prevent expansion

        mock_api.return_value = {"title": "Test Scraper Fail", "status": 200, "time_created": 1609459200, "time_updated": 1609459200}
        
        # Add the item to the database manually
        insert_or_update_item(str(db_path), {"workshop_id": 123, "status": 0, "consumer_appid": 999999})

        # This should attempt to scrape and fail
        daemon.process_batch()
        
        # Retrieve the item directly from the database to check its final status
        from src.database import get_item_details
        item = get_item_details(str(db_path), 123)
        assert item["status"] == 206 # Expecting Partial Content
        
        assert item is not None
        assert item["status"] == 206 # Partial Content
        assert item["title"] == "Test Scraper Fail"
        assert item["extended_description"] is None # Scraper failed

@patch('src.daemon.discover_items_by_date_html')
@patch('time.time')
def test_daemon_historical_forward_strategy(mock_time, mock_discover, tmp_path):
    from src.daemon import Daemon
    from src.database import initialize_database, get_app_tracking, update_app_tracking
    import time

    db_path = tmp_path / "test.db"
    initialize_database(str(db_path))

    config = {
        "database": {"path": str(db_path)},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 5, "request_delay_seconds": 0}
    }

    daemon = Daemon(config)

    now = 1700000000
    mock_time.return_value = now
    daemon.last_filters = {1062090: {"hash": None, "start_time": None}}

    # Initialize app_tracking for AppID 1062090 to prevent TypeError on initial get_app_tracking calls
    update_app_tracking(str(db_path), 1062090, 0)

    # Scenario 1: Initial run, no prior tracking, no filter
    with patch('src.daemon.Daemon._find_initial_start_date', return_value=1000000) as mock_find_initial:
        mock_discover.return_value = [1, 2, 3] # Simulate some items found
        daemon.seed_database(target_new=1)
        
        mock_find_initial.assert_called_once_with(1062090, "", [], [])
        # Tracking should be updated in DB
        tracking = get_app_tracking(str(db_path), 1062090)
        assert tracking["last_historical_date_scanned"] >= 1000000

    # Reset mocks for next scenario
    mock_discover.reset_mock()

    # Scenario 2: Filter change detected. Should reset scan date via _find_initial_start_date
    # Set an old filter in the DB manually
    from src.database import save_app_filter
    save_app_filter(str(db_path), 1062090, filter_text="old", required_tags=["old_tag"])
    # Seed daemon instance with a matching hash so it *doesn't* think it changed yet
    old_hash = json.dumps({"text": "old", "req_tags": ["old_tag"], "excl_tags": []}, sort_keys=True)
    daemon.last_filters[1062090]["hash"] = old_hash
    
    # Now update the DB filter to something "new"
    save_app_filter(str(db_path), 1062090, filter_text="new filter", required_tags=["TagA"], excluded_tags=["TagB"])

    with patch('src.daemon.Daemon._find_initial_start_date', return_value=now - (100 * 86400)) as mock_find_initial:
        mock_discover.return_value = [4, 5, 6]
        daemon.seed_database(target_new=1) # Target 1 new item
        
        mock_find_initial.assert_called_once_with(1062090, "new filter", ["TagA"], ["TagB"])
        # After filter change, last_historical_date_scanned should be updated to the new start_time
        updated_tracking = get_app_tracking(str(db_path), 1062090)
        assert updated_tracking["last_historical_date_scanned"] >= now - (100 * 86400)
        # Also verify discover_items_by_date_html was called with the new filter
        mock_discover.assert_called_with(1062090, ANY, ANY, page=1, search_text="new filter", required_tags=["TagA"], excluded_tags=["TagB"])

    # Reset mocks for next scenario
    mock_discover.reset_mock()

    # Scenario 3: No filter change, app recently scanned. Should skip discovery.
    # Update tracking to 12 hours ago
    update_app_tracking(str(db_path), 1062090, now - (12 * 3600))
    # Ensure daemon's internal hash matches DB so no change is detected
    new_tracking = get_app_tracking(str(db_path), 1062090)
    current_filter = {"text": new_tracking["filter_text"], "req_tags": sorted(json.loads(new_tracking["required_tags"])), "excl_tags": sorted(json.loads(new_tracking["excluded_tags"]))}
    daemon.last_filters[1062090]["hash"] = json.dumps(current_filter, sort_keys=True)
    
    daemon.seed_database()
    mock_discover.assert_not_called()

    # Scenario 4: No filter change, app not recently scanned. Should perform discovery.
    update_app_tracking(str(db_path), 1062090, now - (30 * 86400)) # 30 days ago
    mock_discover.return_value = [7, 8, 9] # Simulate items found
    daemon.seed_database(target_new=1)
    
    # Check that it called discover with the current filter (which is still "new filter" from Scenario 2)
    mock_discover.assert_called_with(1062090, ANY, ANY, page=1, search_text="new filter", required_tags=["TagA"], excluded_tags=["TagB"])
    assert get_app_tracking(str(db_path), 1062090)["last_historical_date_scanned"] > now - (30 * 86400)

def test_daemon_find_initial_start_date(tmp_path):
    from src.daemon import Daemon
    from src.database import initialize_database
    from unittest.mock import patch
    import time
    
    db_path = tmp_path / "test.db"
    initialize_database(str(db_path))
    
    config = {
        "database": {"path": str(db_path)},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090]}
    }
    daemon = Daemon(config)
    
    now = int(time.time())
    
    with patch('src.daemon.discover_items_by_date_html') as mock_query, \
         patch('time.time', return_value=now):
         
        # Let's say the first item was created at now - 100 days
        target_date = now - (100 * 86400)
        
        def mock_query_files(appid, start, end, page=1, search_text="", required_tags=None, excluded_tags=None):
            if end < target_date:
                return []
            return [1]
            
        mock_query.side_effect = mock_query_files
        
        found_start = daemon._find_initial_start_date(4000, search_text="test", required_tags=["tag"]) # Pass dummy filters
        
        # It should be within a couple days of the target date
        assert abs(found_start - target_date) <= 86400 * 2

def test_daemon_find_initial_start_date_no_items(tmp_path):
    """Test binary search when no items exist for the appid."""
    from src.daemon import Daemon
    from src.database import initialize_database
    from unittest.mock import patch
    import time
    
    db_path = tmp_path / "test.db"
    initialize_database(str(db_path))
    
    config = {
        "database": {"path": str(db_path)},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090]}
    }
    daemon = Daemon(config)
    
    now = int(time.time())
    
    with patch('src.daemon.discover_items_by_date_html') as mock_query, \
         patch('time.time', return_value=now):
         
        # Mock scraper returning zero items for every single query
        def mock_query_files_no_items(appid, start, end, page=1, search_text="", required_tags=None, excluded_tags=None):
            return []
        mock_query.side_effect = mock_query_files_no_items
        
        found_start = daemon._find_initial_start_date(4000, search_text="no_items_test") # Pass dummy filter
        
        # If no items ever existed, 'low' should have converged to 'now'
        # and final_start should be approximately now - 1 day.
        assert abs(found_start - (now - 86400)) <= 86400
        assert found_start >= 1317484800
