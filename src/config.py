import os
import yaml
from pathlib import Path

def load_config(path: str) -> dict:
    """
    Loads configuration from a YAML file and applies environment variable overrides.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # Environment variable overrides
    env_key = os.environ.get("STEAM_API_KEY")
    if env_key:
        if "api" not in config:
            config["api"] = {}
        config["api"]["key"] = env_key

    openai_env_key = os.environ.get("OPENAI_API_KEY")
    if openai_env_key:
        if "openai" not in config:
            config["openai"] = {}
        config["openai"]["api_key"] = openai_env_key

    return config

def save_config(path: str, config: dict):
    """
    Saves the configuration to a YAML file. To avoid writing secrets to disk, 
    it strips out keys that were provided by environment variables.
    """
    if not os.path.exists(path):
        return

    # Load existing to preserve any structure not in the current dictionary
    try:
        with open(path, "r", encoding="utf-8") as f:
            disk_config = yaml.safe_load(f) or {}
    except Exception:
        disk_config = {}

    # Deep update disk_config with new config
    def deep_update(d, u):
        for k, v in u.items():
            if isinstance(v, dict):
                d[k] = deep_update(d.get(k, {}), v)
            else:
                d[k] = v
        return d
    
    deep_update(disk_config, config)

    # Do not save environment variable secrets
    if os.environ.get("STEAM_API_KEY") and "api" in disk_config and "key" in disk_config["api"]:
        if disk_config["api"]["key"] == os.environ.get("STEAM_API_KEY"):
             disk_config["api"].pop("key", None)
             
    if os.environ.get("OPENAI_API_KEY") and "openai" in disk_config and "api_key" in disk_config["openai"]:
        if disk_config["openai"]["api_key"] == os.environ.get("OPENAI_API_KEY"):
            disk_config["openai"].pop("api_key", None)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(disk_config, f, default_flow_style=False)
