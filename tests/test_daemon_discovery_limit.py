import pytest
import json
from unittest.mock import patch, MagicMock
from src.daemon import Daemon
from src.database import initialize_database, count_never_fetched_items, update_app_tracking_cursor

@patch('src.daemon.query_workshop_newest_page')
@patch('src.daemon.time.sleep')
def test_seed_database_fetches_multiple_cursors(mock_sleep, mock_query, tmp_path):
    """Test that seed_database continues while next_cursor is provided."""
    db_path = str(tmp_path / "multi_cursor.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "api_batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    mock_query.side_effect = [
        {"total": 200, "items": [{"publishedfileid": str(i)} for i in range(1, 61)],
         "next_cursor": "cur1"},
        {"total": 200, "items": [{"publishedfileid": str(i)} for i in range(61, 121)],
         "next_cursor": ""},
    ]

    daemon.seed_database(fill_target=100)

    assert mock_query.call_count == 2
    assert count_never_fetched_items(db_path) == 120


@patch('src.daemon.query_workshop_newest_page')
@patch('src.daemon.time.sleep')
def test_seed_database_stops_after_enough_items(mock_sleep, mock_query, tmp_path):
    """Test that seed_database stops after discovering >= fill_target items."""
    db_path = str(tmp_path / "enough.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "api_batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    mock_query.return_value = {
        "total": 500,
        "items": [{"publishedfileid": str(i)} for i in range(1, 111)],
        "next_cursor": "cur1",
    }

    daemon.seed_database(fill_target=100)

    assert mock_query.call_count == 1
    assert count_never_fetched_items(db_path) == 110


@patch('src.daemon.query_workshop_newest_page')
@patch('src.daemon.time.sleep')
def test_seed_database_resumes_from_cursor(mock_sleep, mock_query, tmp_path):
    """Test that seed_database resumes from the stored last_cursor."""
    db_path = str(tmp_path / "resume.db")
    initialize_database(db_path)

    update_app_tracking_cursor(db_path, 1062090, "saved_cursor")

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "api_batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    mock_query.return_value = {
        "total": 500,
        "items": [{"publishedfileid": str(i)} for i in range(1, 121)],
        "next_cursor": "",
    }

    daemon.seed_database(fill_target=100)

    mock_query.assert_called_once_with(1062090, cursor="saved_cursor", api_key="test_key",
                                       keep_running=daemon._keep_running)
    assert count_never_fetched_items(db_path) == 120


@patch('src.daemon.query_workshop_newest_page')
@patch('src.daemon.time.sleep')
def test_seed_database_starts_from_star(mock_sleep, mock_query, tmp_path):
    """Test that seed_database starts with cursor='*' when nothing stored."""
    db_path = str(tmp_path / "fresh.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "api_batch_size": 10, "request_delay_seconds": 0}
    }
    daemon = Daemon(config)

    mock_query.return_value = {
        "total": 10,
        "items": [{"publishedfileid": str(i)} for i in range(1, 11)],
        "next_cursor": "",
    }

    daemon.seed_database(fill_target=100)

    mock_query.assert_called_once_with(1062090, cursor="*", api_key="test_key",
                                       keep_running=daemon._keep_running)
