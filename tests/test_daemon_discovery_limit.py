import pytest
import json
from unittest.mock import patch, MagicMock
from src.daemon import Daemon
from src.database import initialize_database, count_unscraped_items, update_app_tracking_page

@patch('src.daemon.query_workshop_files')
@patch('src.daemon.time.sleep')
def test_seed_database_fetches_multiple_pages(mock_sleep, mock_query, tmp_path):
    """Test that seed_database fetches multiple pages if first yields < 100 new items."""
    db_path = str(tmp_path / "multi_page.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    mock_query.side_effect = [
        {"total": 200, "items": [{"publishedfileid": str(i)} for i in range(1, 61)]},   # Page 1: 60 items
        {"total": 200, "items": [{"publishedfileid": str(i)} for i in range(61, 121)]}, # Page 2: 60 items
    ]

    daemon.seed_database(target_new=100)

    assert mock_query.call_count == 2
    assert count_unscraped_items(db_path) == 120


@patch('src.daemon.query_workshop_files')
@patch('src.daemon.time.sleep')
def test_seed_database_stops_after_enough_items(mock_sleep, mock_query, tmp_path):
    """Test that seed_database stops after discovering >= target_new items."""
    db_path = str(tmp_path / "enough.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    mock_query.return_value = {
        "total": 500,
        "items": [{"publishedfileid": str(i)} for i in range(1, 111)]
    }

    daemon.seed_database(target_new=100)

    assert mock_query.call_count == 1
    assert count_unscraped_items(db_path) == 110


@patch('src.daemon.query_workshop_files')
@patch('src.daemon.time.sleep')
def test_seed_database_continues_from_last_page(mock_sleep, mock_query, tmp_path):
    """Test that seed_database continues from last_page_scanned + 1."""
    db_path = str(tmp_path / "continue.db")
    initialize_database(db_path)

    update_app_tracking_page(db_path, 1062090, 3)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    mock_query.return_value = {
        "total": 500,
        "items": [{"publishedfileid": str(i)} for i in range(1, 121)]
    }

    daemon.seed_database(target_new=100)

    mock_query.assert_called_once_with(1062090, page=4, api_key="test_key")
    assert count_unscraped_items(db_path) == 120
