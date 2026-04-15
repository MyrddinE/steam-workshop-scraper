import os
import yaml

def load_config(path: str) -> dict:
    """
    Loads configuration from a YAML file and applies environment variable overrides.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Environment variable overrides
    env_key = os.environ.get("STEAM_API_KEY")
    if env_key:
        if "api" not in config:
            config["api"] = {}
        config["api"]["key"] = env_key

    return config
