import pytest
import time
import json
from unittest.mock import patch, MagicMock
from src.daemon import Daemon
from src.database import initialize_database, get_connection, count_unscraped_items, update_app_tracking

@pytest.mark.asyncio
@patch('src.daemon.time.time')
@patch('src.daemon.discover_items_by_date_html')
@patch('src.daemon.time.sleep')
async def test_seed_database_fetches_multiple_windows(mock_sleep, mock_discover, mock_time, tmp_path):
    """Test that seed_database fetches another window if the first yields < 100 results."""
    db_path = str(tmp_path / "multiple_windows.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    now = 1700000000
    mock_time.return_value = now

    # Setup tracking to force discovery
    initial_scanned = now - (100 * 86400)
    update_app_tracking(db_path, 1062090, initial_scanned, 3600*24*30)
    daemon.last_filters[1062090] = {"hash": json.dumps({"text": "", "req_tags": [], "excl_tags": []}), "start_time": initial_scanned}

    with patch.object(Daemon, '_find_initial_start_date', return_value=initial_scanned):
        mock_discover.side_effect = [
            ([i for i in range(1, 61)], 1),   # Window 1: 60 items
            ([i for i in range(61, 121)], 1), # Window 2: 60 items (Total 120 > 100)
            ([], 1) # Fallback
        ]

        daemon.seed_database(target_new=100)

        # Assert discover was called at least twice for different windows
        assert mock_discover.call_count >= 2

        # Queue should have all 120 items
        assert count_unscraped_items(db_path) == 120


@pytest.mark.asyncio
@patch('src.daemon.time.time')
@patch('src.daemon.discover_items_by_date_html')
@patch('src.daemon.time.sleep')
async def test_seed_database_stops_after_one_window(mock_sleep, mock_discover, mock_time, tmp_path):
    """Test that seed_database stops after one window if >= 100 results are found."""
    db_path = str(tmp_path / "single_window.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    now = 1700000000
    mock_time.return_value = now

    initial_scanned = now - (100 * 86400)
    update_app_tracking(db_path, 1062090, initial_scanned, 3600*24*30)
    daemon.last_filters[1062090] = {"hash": json.dumps({"text": "", "req_tags": [], "excl_tags": []}), "start_time": initial_scanned}

    with patch.object(Daemon, '_find_initial_start_date', return_value=initial_scanned):
        # One window yields 110 items across 2 pages
        mock_discover.side_effect = [
            ([i for i in range(1, 81)], 2),    # Window 1, Page 1: 80 items
            ([i for i in range(81, 111)], 2),  # Window 1, Page 2: 30 items
            ([], 1) # Fallback
        ]

        daemon.seed_database(target_new=100)

        # Called twice for the two pages of the SAME window, but shouldn't start a new window
        assert mock_discover.call_count == 2
        
        assert count_unscraped_items(db_path) == 110
