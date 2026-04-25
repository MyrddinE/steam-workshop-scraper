import pytest
from unittest.mock import patch, MagicMock
from src.daemon import Daemon
from src.database import initialize_database, insert_or_update_item

def test_expand_user_discovery_api_error(tmp_path):
    """Test that the daemon handles API errors in expand_user_discovery without crashing."""
    db_path = str(tmp_path / "error.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 1, "creator": "76561197996891752"})
    
    config = {
        "database": {"path": db_path},
        "daemon": {"target_appids": [1]}
    }
    daemon = Daemon(config)
    
    with patch("src.daemon.get_player_summaries", side_effect=Exception("API Down")):
        # This should not raise an exception
        daemon.expand_user_discovery()

def test_process_batch_with_db_locked(tmp_path):
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
