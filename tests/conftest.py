import pytest

@pytest.fixture
def mock_config():
    return {
        "database": {"path": "test.db"},
        "logging": {"level": "INFO"}
    }
