import os
import yaml
import pytest
from unittest.mock import patch
from src.config import load_config, save_config

@patch.dict(os.environ, {}, clear=True)
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

def test_load_config_openai_env_key_no_block(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("database:\n  path: 'test.db'\n")
    from unittest.mock import patch
    with patch.dict(os.environ, {"OPENAI_API_KEY": "env_key_only"}):
        config = load_config(str(config_file))
        assert config["openai"]["api_key"] == "env_key_only"

def test_save_config_does_not_strip_non_env_key(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("database:\n  path: 'test.db'\n")
    save_config(str(config_file), {"api": {"key": "yaml_key"}})
    with open(config_file) as f:
        saved = yaml.safe_load(f)
    assert saved["api"]["key"] == "yaml_key"

def test_save_config_skips_if_file_missing(tmp_path):
    missing = str(tmp_path / "nonexistent.yaml")
    save_config(missing, {"api": {"key": "x"}})
    assert not os.path.exists(missing)

def test_save_config_strips_env_secrets(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("database:\n  path: 'test.db'\n")
    with patch.dict(os.environ, {"STEAM_API_KEY": "secret_key"}):
        save_config(str(config_file), {"api": {"key": "secret_key"}})
    with open(config_file) as f:
        saved = yaml.safe_load(f)
    assert "key" not in saved.get("api", {})

def test_save_config_preserves_existing_keys(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("existing_key: value\n")
    save_config(str(config_file), {"new_key": "new_value"})
    with open(config_file) as f:
        saved = yaml.safe_load(f)
    assert saved["existing_key"] == "value"
    assert saved["new_key"] == "new_value"


# ── the shipped example config ───────────────────────────────────────────────
# Copying config.yaml.example to config.yaml is the documented first step, so the
# file itself has to parse. It did not: a stray token ended the openai block
# early and yaml.safe_load raised.

EXAMPLE_CONFIG = os.path.join(os.path.dirname(__file__), os.pardir, "config.yaml.example")


def test_example_config_is_valid_yaml():
    with open(EXAMPLE_CONFIG) as f:
        parsed = yaml.safe_load(f)
    assert isinstance(parsed, dict)


def test_example_config_has_the_documented_sections():
    with open(EXAMPLE_CONFIG) as f:
        parsed = yaml.safe_load(f)
    for section in ("api", "database", "daemon", "openai", "logging"):
        assert section in parsed, f"config.yaml.example is missing the '{section}' section"


def test_example_config_uses_the_current_delay_key():
    """The canonical key is api_delay_seconds; the daemon still accepts the old
    name as an undocumented fallback (code-issues #9), but the example must not
    advertise it."""
    with open(EXAMPLE_CONFIG) as f:
        parsed = yaml.safe_load(f)
    assert "api_delay_seconds" in parsed["daemon"]
    assert "request_delay_seconds" not in parsed["daemon"]

