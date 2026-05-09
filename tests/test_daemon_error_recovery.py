import pytest
from unittest.mock import patch
from src.daemon import Daemon
from src.database import initialize_database

def test_process_batch_with_db_locked(tmp_path):
    """Test that process_batch handles potential database locking issues."""
    db_path = str(tmp_path / "locked.db")
    initialize_database(db_path)

    config = {
        "database": {"path": db_path},
        "daemon": {"target_appids": [1], "batch_size": 1}
    }
    daemon = Daemon(config)

    import sqlite3
    with patch("src.daemon.get_next_items_to_scrape", side_effect=sqlite3.OperationalError("database is locked")):
        daemon.process_batch()
