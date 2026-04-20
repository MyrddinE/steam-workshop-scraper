import pytest
import os

@pytest.fixture
def mock_config():
    return {
        "database": {"path": "test.db"},
        "logging": {"level": "INFO"}
    }

@pytest.fixture(autouse=True)
def cleanup_tui_state():
    """Ensure tui_state.yaml is removed after each test to prevent side effects."""
    yield
    if os.path.exists(".tui_state.yaml"):
        os.remove(".tui_state.yaml")
