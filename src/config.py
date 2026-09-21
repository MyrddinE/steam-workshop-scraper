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



# Config-key warnings name a stale spelling once per process. The daemon log is
# never rotated and a key can be read on every scrape or subscribe, so a warning
# repeated per read is unbounded noise; the first read of a `(section, legacy)`
# key is enough to tell the operator.
_WARNED_CONFIG_KEYS: set[tuple[str, str]] = set()


def reset_warned_keys() -> None:
    """Forget which config-key warnings have been logged (tests only)."""
    _WARNED_CONFIG_KEYS.clear()


def _is_first_warning(section_name: str, legacy_key: str) -> bool:
    token = (section_name, legacy_key)
    if token in _WARNED_CONFIG_KEYS:
        return False
    _WARNED_CONFIG_KEYS.add(token)
    return True


def _key_where(section_name: str, key: str) -> str:
    return f"{section_name}.{key}" if section_name else key


def warn_retired_key(section_name: str, legacy_key: str,
                     current_key: str | None = None) -> None:
    """Name a config key that is no longer read, and the key that replaced it.

    A renamed key is retired once its old value stops being read. A config file
    already on disk is still a contract we do not control, so the operator gets
    one line saying the spelling they wrote no longer does anything -- silence
    would leave a dead key in place forever. The shape matches the retired
    ``openai.batch_items`` warning in :mod:`src.translator`.

    ``current_key`` is ``None`` for a key that was **removed** rather than
    renamed -- the pacing delays moved to the daemon state file and no config
    spelling replaced them. The warning then says the value is no longer read
    and names no successor, because telling the operator to rename it to a key
    that does not exist would be worse than saying nothing.

    ``section_name`` is only for the warning, so it can name the key the way the
    operator wrote it (``daemon.batch_size``, not ``batch_size``).
    """
    if not _is_first_warning(section_name, legacy_key):
        return
    if current_key is None:
        logging.warning(
            "Config key '%s' is no longer used and its value is no longer read. "
            "Remove the key.",
            _key_where(section_name, legacy_key),
        )
        return
    logging.warning(
        "Config key '%s' is no longer used; it was renamed to '%s'. Remove the key.",
        _key_where(section_name, legacy_key), _key_where(section_name, current_key),
    )


def warn_still_honoured_key(section_name: str, legacy_key: str, current_key: str) -> None:
    """Name a config key that still works under an old spelling.

    The opposite of :func:`warn_retired_key`: the value is still read, so an
    operator must not be told to remove it, only to rename it. Used for the
    aliases the log has not yet shown unused.
    """
    if not _is_first_warning(section_name, legacy_key):
        return
    logging.warning(
        "Config key '%s' is deprecated and still honoured; rename it to '%s'.",
        _key_where(section_name, legacy_key), _key_where(section_name, current_key),
    )


def configured_outbox_dir(daemon_config: dict) -> str | None:
    """``daemon.outbox_dir``, with the still-honoured ``daemon.backup_dir`` fallback.

    Three processes read the same location -- the daemon, the web server and the
    crash writer -- and each used to read the legacy name inline and silently.
    The lookup lives here so they agree, and so the one warning for the old
    spelling fires once per process rather than once per reader.

    ``backup_dir`` was renamed to ``outbox_dir`` when the outbox came to hold
    more than database backups. It is retained: the value still works, and the
    warning asks for the rename. The current key wins when both are present.
    """
    daemon_config = daemon_config or {}
    if "backup_dir" in daemon_config:
        warn_still_honoured_key("daemon", "backup_dir", "outbox_dir")
    return daemon_config.get("outbox_dir") or daemon_config.get("backup_dir") or None


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
