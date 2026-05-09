import pytest
import time
import json
from unittest.mock import patch, MagicMock
from src.daemon import Daemon
from src.database import initialize_database, get_connection, count_unscraped_items, update_app_tracking, get_app_tracking

@patch('src.daemon.time.time')
@patch('src.daemon.discover_items_by_date_html')
@patch('src.daemon.time.sleep')
def test_seed_database_halts_on_error(mock_sleep, mock_discover, mock_time, tmp_path):
    """Test that seed_database halts window progression if an error occurs."""
    db_path = str(tmp_path / "halt_error.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    now = 1700000000
    mock_time.return_value = now

    initial_scanned = now - (30 * 86400)
    update_app_tracking(db_path, 1062090, initial_scanned, 3600*24*30)
    daemon.last_filters[1062090] = {"hash": json.dumps({"text": "", "req_tags": [], "excl_tags": []}), "start_time": initial_scanned}

    with patch.object(Daemon, '_find_initial_start_date', return_value=initial_scanned):
        mock_discover.side_effect = [
            ([i for i in range(1, 31)], 2), # Page 1: OK
            ([], -1)                        # Page 2: Error
        ]

        daemon.seed_database(target_new=100)
        
        # Verify it halted and didn't advance last_scanned_date
        tracking = get_app_tracking(db_path, 1062090)
        assert tracking["last_historical_date_scanned"] == initial_scanned

@patch('src.daemon.time.time')
@patch('src.daemon.discover_items_by_date_html')
@patch('src.daemon.time.sleep')
def test_seed_database_halts_on_partial_page(mock_sleep, mock_discover, mock_time, tmp_path):
    """Test that seed_database halts window progression if a non-last page returns < 30 items."""
    db_path = str(tmp_path / "halt_partial.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    now = 1700000000
    mock_time.return_value = now

    initial_scanned = now - (30 * 86400)
    update_app_tracking(db_path, 1062090, initial_scanned, 3600*24*30)
    daemon.last_filters[1062090] = {"hash": json.dumps({"text": "", "req_tags": [], "excl_tags": []}), "start_time": initial_scanned}

    with patch.object(Daemon, '_find_initial_start_date', return_value=initial_scanned):
        mock_discover.side_effect = [
            ([i for i in range(1, 15)], 2),  # Page 1: Partial (< 30) but total pages is 2
            ([i for i in range(15, 45)], 2)  # Page 2: OK
        ]

        daemon.seed_database(target_new=100)
        
        # Verify it fetched both pages, but halted window progression
        assert mock_discover.call_count == 2
        tracking = get_app_tracking(db_path, 1062090)
        assert tracking["last_historical_date_scanned"] == initial_scanned

@patch('src.daemon.time.time')
@patch('src.daemon.discover_items_by_date_html')
@patch('src.daemon.time.sleep')
def test_process_batch_with_db_locked(mock_sleep, mock_discover, mock_time, tmp_path):
    """Test that process_batch handles potential database locking issues (though WAL should prevent it)."""
    db_path = str(tmp_path / "locked.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "daemon": {"target_appids": [1], "batch_size": 1}
    }
    daemon = Daemon(config)

    # Mocking get_next_items_to_scrape to raise a sqlite3 error
    import sqlite3
    with patch("src.daemon.get_next_items_to_scrape", side_effect=sqlite3.OperationalError("database is locked")):
        # Should log and return, not crash
        daemon.process_batch()

