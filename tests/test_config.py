import os
import yaml
import pytest
from src.config import load_config

def test_load_config_yaml(tmp_path):
    """Tests that config correctly loads from a YAML file."""
    config_data = {
        "api": {"key": "test_key"},
        "database": {"path": "test.db"},
        "daemon": {
            "batch_size": 5,
            "request_delay_seconds": 2.0,
            "target_appids": [123, 456]
        },
        "logging": {"level": "DEBUG", "file": "test.log"}
    }
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    config = load_config(str(config_file))
    
    assert config["api"]["key"] == "test_key"
    assert config["database"]["path"] == "test.db"
    assert config["daemon"]["batch_size"] == 5
    assert config["daemon"]["request_delay_seconds"] == 2.0
    assert config["daemon"]["target_appids"] == [123, 456]
    assert config["logging"]["level"] == "DEBUG"

def test_load_config_env_override(tmp_path):
    """Tests that environment variables override YAML settings."""
    config_data = {
        "api": {"key": "original_key"},
        "database": {"path": "original.db"}
    }
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    os.environ["STEAM_API_KEY"] = "overridden_key"
    try:
        config = load_config(str(config_file))
        assert config["api"]["key"] == "overridden_key"
    finally:
        del os.environ["STEAM_API_KEY"]

def test_load_config_openai_env_override(tmp_path):
    """Tests that OPENAI_API_KEY environment variable overrides YAML settings."""
    config_data = {
        "openai": {"api_key": "original_openai_key"}
    }
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    os.environ["OPENAI_API_KEY"] = "overridden_openai_key"
    try:
        config = load_config(str(config_file))
        assert config["openai"]["api_key"] == "overridden_openai_key"
    finally:
        del os.environ["OPENAI_API_KEY"]

def test_load_config_env_override_missing_section(tmp_path):
    """Tests that environment variables create the 'api' section if missing."""
    config_data = {
        "database": {"path": "original.db"}
    }
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    os.environ["STEAM_API_KEY"] = "new_key"
    try:
        config = load_config(str(config_file))
        assert "api" in config
        assert config["api"]["key"] == "new_key"
    finally:
        del os.environ["STEAM_API_KEY"]

def test_load_config_missing_file():
    """Tests behavior when the config file is missing (should probably use defaults or fail gracefully)."""
    with pytest.raises(FileNotFoundError):
        load_config("non_existent_file.yaml")
