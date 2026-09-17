import logging
import os
import yaml
from pathlib import Path

from src.session_cookie import ENCODED_SEPARATOR

def load_config(path: str) -> dict:
    """
    Loads configuration from a YAML file and applies environment variable overrides.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        try:
            config = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(describe_yaml_error(path, exc)) from exc

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

class ConfigError(Exception):
    """A config file that exists but cannot be used.

    Distinct from FileNotFoundError, which callers answer with defaults. A file
    that is present and malformed is a misconfiguration, so it is reported
    rather than swallowed: falling back to defaults would silently discard
    whatever the operator intended.
    """


_WINDOWS_PATH_HINT = (
    "If a value is a Windows path, note that inside a double-quoted YAML string a "
    'backslash starts an escape sequence: double it ("D:\\\\Temp") or use single '
    "quotes ('D:\\Temp')."
)


def describe_yaml_error(path: str, exc: "yaml.YAMLError") -> str:
    """A message an operator can act on, naming the file and the position."""
    mark = getattr(exc, "problem_mark", None)
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    return f"{path} is not valid YAML: {problem}{where}. {_WINDOWS_PATH_HINT}"


def login_secure_value(config: dict) -> str:
    """The `steamLoginSecure` cookie, normalised from either accepted form.

    The config key takes a raw cookie string or the three pipe-separated
    components as a YAML list; both mean the same cookie. Everything that sends
    it must agree on the encoding, so the rule lives here rather than being
    repeated at each call site -- and the separator itself lives in
    :mod:`src.session_cookie`, which reads both forms back out.
    """
    value = config.get("session", {}).get("login_secure", "")
    if isinstance(value, list):
        return ENCODED_SEPARATOR.join(str(part) for part in value)
    return value or ""



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
        logging.debug("Config file not found or failed to parse: %s", path)
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
