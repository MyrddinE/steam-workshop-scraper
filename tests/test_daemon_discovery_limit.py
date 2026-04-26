import pytest
from unittest.mock import patch, MagicMock, ANY
import json
import time
from src.daemon import Daemon
from src.database import initialize_database, insert_or_update_item, count_unscraped_items, update_app_tracking, get_app_tracking

@pytest.mark.asyncio
@patch('src.daemon.time.time')
@patch('src.daemon.discover_items_by_date_html')
@patch('src.daemon.time.sleep')
async def test_seed_database_queue_limit(mock_sleep, mock_discover, mock_time, tmp_path):
    """Test that seed_database stops when the unscraped queue reaches the target size."""
    db_path = str(tmp_path / "limit_test.db")
    initialize_database(db_path)
    
    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)
    
    now = 1700000000
    mock_time.return_value = now
    
    # Setup tracking to force discovery (last scanned 30 days ago)
    update_app_tracking(db_path, 1062090, now - (30 * 86400), 3600*24*30)
    daemon.last_filters[1062090] = {"hash": json.dumps({"text": "", "req_tags": [], "excl_tags": []}), "start_time": now - (30 * 86400)}
    
    # Mock _find_initial_start_date to just return a fixed value
    # and mock discover_items_by_date_html for the actual seeding
    with patch.object(Daemon, '_find_initial_start_date', return_value=now - (30 * 86400)):
        # 1. First call to discover returns 60 items. Queue target is 100.
        # It should call discover again for the next page/window.
        mock_discover.side_effect = [
            [i for i in range(1, 61)],  # 60 items
            [i for i in range(61, 121)], # 60 more items
            [] # Stop if it keeps going
        ]
        
        # We want it to stop discovery when queue >= 100.
        # target_new is used as the queue limit in my implementation.
        daemon.seed_database(target_new=100)
        
        # Verify discover was called twice (first page got 60, second page got 60, total 120 > 100)
        assert mock_discover.call_count == 2
        
        # Verify queue size
        assert count_unscraped_items(db_path) >= 100
        
        # 2. Reset and test that it doesn't even start if queue is already full
        mock_discover.reset_mock()
        # Queue already has ~120 items from previous step
        daemon.seed_database(target_new=100)
        mock_discover.assert_not_called()


@pytest.mark.asyncio
@patch('src.daemon.time.time')
@patch('src.daemon.discover_items_by_date_html')
@patch('src.daemon.time.sleep')
async def test_seed_database_no_tracking_update_on_early_exit(mock_sleep, mock_discover, mock_time, tmp_path):
    """
    Test that last_historical_date_scanned is NOT updated if seed_database exits early
    due to the queue filling up in the middle of a date window.
    """
    db_path = str(tmp_path / "early_exit_test.db")
    initialize_database(db_path)
    
    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)
    
    now = 1700000000
    mock_time.return_value = now
    
    initial_last_scanned = now - (100 * 86400) # 100 days ago
    update_app_tracking(db_path, 1062090, initial_last_scanned, 3600*24*30)
    
    # Correctly initialize the daemon's internal state to match the database
    daemon._load_initial_filter_state()
    
        # Mock _find_initial_start_date to just return a fixed value
    with patch.object(Daemon, '_find_initial_start_date', return_value=initial_last_scanned):
        # Simulate an exit during the FIRST window.
        # To stay in the same window, Page 1 must return 30 items.
        mock_discover.side_effect = [
            [i for i in range(1, 31)],   # Page 1: 30 items
            [i for i in range(31, 46)],  # Page 2: 15 items. Total 45.
            [] # Should not be hit
        ]
        
        # Call seed_database with a target_new that will be hit mid-window
        daemon.seed_database(target_new=40)
        
        # Verify discover was called twice (for 2 pages)
        assert mock_discover.call_count == 2
        
        # Assert that last_historical_date_scanned was NOT updated
        current_tracking = get_app_tracking(db_path, 1062090)
        assert current_tracking["last_historical_date_scanned"] == initial_last_scanned
        
        # Verify the queue has items as expected
        assert count_unscraped_items(db_path) >= 40

@pytest.mark.asyncio
@patch('src.daemon.time.time')
@patch('src.daemon.discover_items_by_date_html')
@patch('src.daemon.time.sleep')
async def test_seed_database_tracking_updates_on_second_window_exit(mock_sleep, mock_discover, mock_time, tmp_path):
    """
    Test that last_historical_date_scanned IS updated to the end of Window 1 
    if discovery is interrupted during Window 2.
    """
    db_path = str(tmp_path / "second_window_exit_test.db")
    initialize_database(db_path)
    
    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)
    
    now = 1700000000
    mock_time.return_value = now
    
    initial_last_scanned = now - (100 * 86400) # 100 days ago
    update_app_tracking(db_path, 1062090, initial_last_scanned, 3600*24*30)
    
    # Correctly initialize the daemon's internal state to match the database
    daemon._load_initial_filter_state()
    
    # Mock _find_initial_start_date to just return a fixed value
    with patch.object(Daemon, '_find_initial_start_date', return_value=initial_last_scanned):
        # Simulate Window 1 completing, and Window 2 exiting early.
        # Window 1, Page 1: < 30 items -> Window 1 finishes.
        # Window 2, Page 1: < 30 items, but reaches target -> Window 2 finishes early.
        mock_discover.side_effect = [
            [i for i in range(1, 11)],   # Window 1, Page 1: 10 items. Finishes window.
            [i for i in range(11, 26)],  # Window 2, Page 1: 15 items. Total 25. Reaches target 20.
            [] # Should not be hit
        ]
        
        # Call seed_database with a target_new that will be hit mid-window
        daemon.seed_database(target_new=20)
        
        # Assert that last_historical_date_scanned WAS updated, but only to end of Window 1
        current_tracking = get_app_tracking(db_path, 1062090)
        expected_new_date = initial_last_scanned + (3600 * 24 * 30) # End of first window (initial + 30 days)
        assert current_tracking["last_historical_date_scanned"] == expected_new_date

@pytest.mark.asyncio
@patch('src.daemon.time.time')
@patch('src.daemon.discover_items_by_date_html')
@patch('src.daemon.time.sleep')
async def test_seed_database_updates_on_empty_window(mock_sleep, mock_discover, mock_time, tmp_path):
    """
    Test that last_historical_date_scanned IS updated if seed_database processes a window
    that yields 0 items, but completes naturally (not due to queue fill).
    """
    db_path = str(tmp_path / "empty_window_test.db")
    initialize_database(db_path)
    
    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)
    
    now = 1700000000
    mock_time.return_value = now
    
    initial_last_scanned = now - (100 * 86400) # 100 days ago
    update_app_tracking(db_path, 1062090, initial_last_scanned, 3600*24*30)
    daemon.last_filters[1062090] = {"hash": json.dumps({"text": "", "req_tags": [], "excl_tags": []}), "start_time": initial_last_scanned}
    
    window_end_time = initial_last_scanned + (30 * 24 * 3600) # One 30-day window
    
    with patch.object(Daemon, '_find_initial_start_date', return_value=initial_last_scanned):
        # Simulate discover returning no items for the window
        mock_discover.side_effect = [
            [], # First page, no items
            [] # If it asks for more, stop
        ]
        
        # Call seed_database to process this empty window
        daemon.seed_database(target_new=10) # Target a queue that won't be filled
        
        # Verify discover was called at least once for the window
        assert mock_discover.call_count >= 1
        
        # Assert that last_historical_date_scanned WAS updated to the end of the empty window
        current_tracking = get_app_tracking(db_path, 1062090)
        assert current_tracking["last_historical_date_scanned"] >= window_end_time
