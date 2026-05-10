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
    assert daemon.api_delay == 1.5
    assert daemon.item_staleness_days == 30
    assert daemon.user_staleness_days == 90
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
@patch('src.daemon.get_user')
@patch('src.daemon.insert_or_update_user')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.flag_for_web_scrape')
@patch('time.sleep')
def test_daemon_process_batch_success(mock_sleep, mock_flag_web, mock_insert, mock_insert_user, mock_get_user, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 123}]
    mock_api.return_value = {"title": "Test Mod", "creator": "111"}
    mock_get_user.return_value = {"steamid": 111, "dt_updated": "2026-01-01T00:00:00"}

    daemon = Daemon(mock_config)
    daemon.process_batch()

    mock_get_items.assert_called_once_with("test.db", limit=2, staleness_days=30)
    mock_api.assert_called_once_with(123, "TEST_KEY")
    mock_flag_web.assert_called_once_with("test.db", 123, 3)

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
    mock_get_items.return_value = [{'workshop_id': 456}]
    mock_api.return_value = {"status": 500, "publishedfileid": 456} # Mock API failure with status

    daemon = Daemon(mock_config)

    daemon.process_batch()
    
    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["status"] == 500

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
    
    # Should sleep in 1-second checks for pollable shutdown (600 checks)
    mock_sleep.assert_called_with(1)
    assert mock_sleep.call_count == 600

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
def test_daemon_process_batch_exit_early(mock_api, mock_get_items, mock_count, mock_config):
    """Test behavior when shutdown signal is received mid-batch."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 1}, {'workshop_id': 2}, {'workshop_id': 3}]
    daemon = Daemon(mock_config)
    daemon.running = False # Simulate shutdown right before processing
    daemon.process_batch()
    mock_api.assert_not_called()

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.get_user')
@patch('src.daemon.insert_or_update_user')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.flag_for_web_scrape')
@patch('time.sleep')
def test_api_delay_decreases_on_success(mock_sleep, mock_flag_web, mock_insert, mock_insert_user, mock_get_user, mock_api, mock_get_items, mock_count, mock_config):
    items = [{'workshop_id': i} for i in range(100)]
    mock_count.return_value = 1000
    mock_get_items.return_value = items
    mock_api.return_value = {"title": "Mod", "creator": "111"}
    mock_get_user.return_value = {"steamid": 111, "dt_updated": "2026-01-01T00:00:00"}

    daemon = Daemon(mock_config)
    daemon.api_delay = 1.0
    initial = daemon.api_delay
    daemon.process_batch()
    assert daemon.api_delay < initial

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_api_delay_increases_on_failures(mock_sleep, mock_insert, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 11}, {'workshop_id': 12}]
    mock_api.return_value = {"status": 500, "publishedfileid": 11}

    daemon = Daemon(mock_config)
    daemon.api_delay = 1.0
    daemon.api_successes = 5
    daemon.api_had_streak = True
    initial = daemon.api_delay

    daemon.process_batch()
    assert daemon.api_delay > initial

