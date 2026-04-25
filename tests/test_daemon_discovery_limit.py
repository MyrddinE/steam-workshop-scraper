import pytest
from unittest.mock import patch, MagicMock, ANY
import json
import time
from src.daemon import Daemon
from src.database import initialize_database, insert_or_update_item, count_unscraped_items, update_app_tracking

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
    update_app_tracking(db_path, 1062090, now - (30 * 86400))
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
