import pytest
import os
from src.database import initialize_database

ASYNC_PAUSE = 0.25

@pytest.fixture
def mock_config():
    return {
        "database": {"path": "test.db"},
        "logging": {"level": "INFO"}
    }

@pytest.fixture
def mock_config_with_api():
    """Config with API key for daemon tests."""
    return {
        "database": {"path": "test.db"},
        "api": {"key": "TEST_KEY"},
        "daemon": {"batch_size": 2, "request_delay_seconds": 0.01, "target_appids": [123]}
    }

@pytest.fixture
def db_path(tmp_path):
    """Fixture providing a temporary initialized database."""
    path = str(tmp_path / "test_workshop.db")
    initialize_database(path)
    return path

@pytest.fixture(autouse=True)
def cleanup_tui_state():
    """Ensure tui_state.yaml is removed after each test to prevent side effects."""
    yield
    if os.path.exists(".tui_state.yaml"):
        os.remove(".tui_state.yaml")
