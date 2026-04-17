import pytest
from unittest.mock import patch, MagicMock
import signal
import json
from src.daemon import Daemon

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
    assert daemon.delay == 1.5
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
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_daemon_process_batch_success(mock_sleep, mock_insert, mock_scrape, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000 # Enough to skip seeding
    mock_get_items.return_value = [123]
    mock_api.return_value = {"title": "Test Mod", "creator": "111"}
    mock_scrape.return_value = {"description": "Cool mod", "tags": ["tag1"]}

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
    mock_api.return_value = None

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

@patch('src.daemon.get_app_page')
@patch('src.daemon.update_app_page')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.query_workshop_items')
@patch('src.daemon.insert_or_update_item')
def test_daemon_seed_database(mock_insert, mock_query, mock_get_items, mock_update_page, mock_get_page, mock_config):
    """Test the seeding logic correctly calls discovery and inserts into DB, looping until buffer is full."""
    # Simulate the page incrementing
    mock_get_page.side_effect = [1, 2, 3]
    
    # First page gives 60 items, second page gives 60 items (total 120, satisfying > 100)
    mock_query.side_effect = [
        [i for i in range(60)], # Page 1
        [i for i in range(60, 120)] # Page 2
    ]
    # All are new
    mock_insert.return_value = True
    
    daemon = Daemon(mock_config)
    daemon.seed_database(target_new=100)
    
    # Should have called query twice (Page 1 and Page 2)
    assert mock_query.call_count == 2
    mock_query.assert_any_call(123, "TEST_KEY", count=100, page=1)
    mock_query.assert_any_call(123, "TEST_KEY", count=100, page=2)
    
    # 120 inserts
    assert mock_insert.call_count == 120
    
    # Should have updated the page in app_state twice
    assert mock_update_page.call_count == 2
    mock_update_page.assert_any_call("test.db", 123, 2) # After page 1
    mock_update_page.assert_any_call("test.db", 123, 3) # After page 2

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
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
@patch('logging.warning')
def test_daemon_process_batch_sanitization(mock_warn, mock_sleep, mock_insert, mock_scrape, mock_api, mock_get_items, mock_count, mock_config):
    """Test that the daemon maps schema keys correctly and strips/warns on invalid keys."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [123]
    mock_api.return_value = {
        "title": "Test", 
        "creator_app_id": 4000, 
        "consumer_app_id": 4000,
        "publishedfileid": "123",
        "description": "API description",
        "result": 1,
        "future_steam_feature": "magic" # This should trigger a warning
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
    
    # 3. Verify that the unknown key triggered a warning
    mock_warn.assert_called_once_with("Discarding unknown API column: 'future_steam_feature' with value 'magic' for item 123")
