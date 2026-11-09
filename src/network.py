"""Outbound HTTP proxy resolution, credentials, and the 407 record.

Every Steam request this project makes goes through :mod:`requests`, which
honours the proxy the operating system or environment supplies -- the Windows
Internet Settings registry on the production host, the ``HTTP(S)_PROXY`` and
``NO_PROXY`` variables elsewhere. That is normally invisible and correct. The
failure it can produce is not: an authenticating proxy answers the CONNECT with
``407 Proxy Authentication Required``, ``requests`` raises ``ProxyError``, and
without this module the caller sees an unattributable transport failure. The
operator sees a login warning and thinks the cookie is dead, while the cookie is
fine and every request is being stopped before it reaches Steam.

This module owns the one rule for that:

* resolve the proxy for a URL (explicit config first, then the OS/environment);
* attach credentials saved in ``config.yaml``;
* recognise a 407 and name the proxy, without ever echoing a password;
* record the condition in the daemon state file so a front end can ask for the
  credentials, and clear it once a request gets through.

The config shape is::

    proxy:
      url: "http://proxy.example:8080"   # optional; detected when absent
      username: "operator"
      password: "secret"
      no_proxy: "localhost,127.0.0.1"    # optional

``url`` is optional on purpose. A proxy supplied by the OS is still used; the
value is written once the app has detected it, so the prompt can show it and the
operator can correct it. Credentials are plain text because ``config.yaml`` is
already the file that holds the Steam login cookie; the crash writer registers
the password as a secret so it cannot leak into a dump.
"""

from __future__ import annotations

import logging
import os
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from requests.utils import get_environ_proxies, should_bypass_proxies

from src.daemon_state import StateStore, state_path_for

# The top-level key this module owns in the daemon state file.
SECTION = "proxy"

# The config section that holds the proxy settings.
CONFIG_SECTION = "proxy"

# The sentence the two front ends show when no more specific reason is known.
AUTH_REQUIRED_DETAIL = (
    "The HTTP proxy refused the request with 407 Proxy Authentication Required. "
    "Enter the proxy username and password so the app can authenticate to it."
)

# The one place the process's config path is kept. The entry points set it; the
# default is the old relative name, so a test or a script that never calls the
# setter still reads the same file it always did.
_config_path = "config.yaml"


def set_config_path(path: str) -> None:
    """Point the network helpers at the config file the app was started with."""
    global _config_path
    _config_path = path or "config.yaml"


def config_path() -> str:
    """The config file proxy credentials are read from and saved to."""
    return _config_path


def load_current_config() -> dict:
    """The current config, or ``{}`` when it cannot be read.

    Proxy credentials are saved from a different process than the daemon that
    uses them, so this is read per call rather than captured at startup: a
    credential the operator has just entered has to take effect without a
    restart. A missing or unreadable config means "no configured proxy"; the
    environment proxy still applies.
    """
    try:
        from src.config import load_config
        return load_config(config_path())
    except Exception as exc:
        logging.debug("Could not read config for proxy settings: %s", exc)
        return {}


def _section(config: dict | None) -> dict:
    section = (config or {}).get(CONFIG_SECTION, {})
    return section if isinstance(section, dict) else {}


def _configured_url(config: dict | None) -> str:
    return str(_section(config).get("url") or "").strip()


def _configured_credentials(config: dict | None) -> tuple[str, str]:
    section = _section(config)
    return (str(section.get("username") or ""),
            str(section.get("password") or ""))


def _normalise_proxy_url(value: str) -> str:
    """A proxy URL with a scheme. A bare ``host:port`` means HTTP."""
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    return value


def _host_port(parts) -> str:
    """``host[:port]`` with IPv6 brackets, from a ``SplitResult``."""
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    return host


def _split_userinfo(value: str) -> tuple[str, str, str]:
    """``(url_without_userinfo, username, password)`` for a proxy URL."""
    value = _normalise_proxy_url(value)
    if not value:
        return "", "", ""
    parts = urlsplit(value)
    username = unquote(parts.username or "")
    password = unquote(parts.password or "")
    clean = urlunsplit((parts.scheme, _host_port(parts), parts.path, parts.query,
                        parts.fragment))
    return clean, username, password


def _with_credentials(value: str, username: str, password: str) -> str:
    """Attach URL-quoted credentials to a proxy URL, replacing any it had."""
    value = _normalise_proxy_url(value)
    if not value:
        return ""
    if not username and not password:
        return value
    clean, _, _ = _split_userinfo(value)
    parts = urlsplit(clean)
    userinfo = ""
    if username:
        userinfo = quote(username, safe="")
        if password:
            userinfo += ":" + quote(password, safe="")
        userinfo += "@"
    return urlunsplit((parts.scheme, userinfo + _host_port(parts), parts.path,
                       parts.query, ""))


def display_proxy(value: str | None) -> str:
    """A proxy URL with any userinfo removed, for logs and the UI."""
    try:
        clean, _, _ = _split_userinfo(value or "")
    except ValueError:
        # A malformed port is still worth showing without its userinfo; the
        # caller's next request will report the real problem.
        return _normalise_proxy_url(value or "")
    return clean


def _proxy_from_map(proxies: dict, url: str) -> str:
    """The proxy in a ``get_environ_proxies`` map that applies to ``url``."""
    url = str(url or "")
    scheme = urlsplit(url).scheme.lower()
    return (proxies.get(scheme) or proxies.get("all") or "").strip()


def _no_proxy(config: dict | None) -> str:
    """The config's ``no_proxy`` plus the environment's, comma separated."""
    parts = [str(_section(config).get("no_proxy") or "")]
    for name in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(name)
        if value:
            parts.append(value)
    return ",".join(part for part in parts if part)


def proxies_for(url: str, config: dict | None = None) -> dict:
    """The ``requests`` ``proxies`` mapping for ``url``.

    A dict with ``http`` and ``https`` keys is returned even when the answer is
    "no proxy": requests merges the environment's proxy back in for a scheme the
    caller left unset, so an explicit ``None`` is what makes a bypass a bypass.
    """
    url = str(url or "")
    config = load_current_config() if config is None else config
    no_proxy = _no_proxy(config)
    try:
        if should_bypass_proxies(url, no_proxy=no_proxy or None):
            return {"http": None, "https": None}
    except Exception:
        # A malformed no_proxy is not a reason to fail a request; use the proxy.
        logging.debug("Could not evaluate no_proxy %r", no_proxy, exc_info=True)

    configured = _configured_url(config)
    if configured:
        username, password = _configured_credentials(config)
        try:
            value = _with_credentials(configured, username, password)
        except ValueError as exc:
            logging.warning("Ignoring proxy credentials for a malformed URL: %s", exc)
            value = configured
        return {"http": value, "https": value}

    try:
        detected = get_environ_proxies(url)
    except Exception as exc:
        logging.debug("Could not read environment proxies: %s", exc)
        detected = {}
    value = _proxy_from_map(detected, url)
    if not value:
        return {"http": None, "https": None}

    username, password = _configured_credentials(config)
    try:
        value = _with_credentials(value, username, password)
    except ValueError as exc:
        logging.warning("Ignoring proxy credentials for a malformed URL: %s", exc)
    return {"http": value, "https": value}


def proxy_url_for(url: str, config: dict | None = None) -> str:
    """The effective proxy URL for ``url`` without credentials, or ``""``."""
    url = str(url or "")
    configured = _configured_url(config if config is not None else load_current_config())
    if configured:
        return display_proxy(configured)
    try:
        detected = get_environ_proxies(url)
    except Exception:
        return ""
    return display_proxy(_proxy_from_map(detected, url))


def proxy_url_with_credentials(url: str, config: dict | None = None) -> str:
    """The effective proxy URL with credentials, for a client that takes one."""
    settings = proxies_for(url, config)
    return settings.get("https") or settings.get("http") or ""


# CA bundle paths already warned about; verify_setting runs per request.
_WARNED_CA_PATHS: set[str] = set()


def ca_bundle(config: dict | None = None) -> str:
    """The PEM bundle that should verify the proxy's TLS certificates, or ``""``.

    Config wins, then the standard requests/OpenSSL variables. The e2guardian
    interception CA is not in certifi, so without one of these a MITM'd Steam
    request fails with ``CERTIFICATE_VERIFY_FAILED`` even though the browser
    trusts the proxy.
    """
    section = _section(config if config is not None else load_current_config())
    configured = str(section.get("ca_bundle") or "").strip()
    if configured:
        return configured
    for name in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        value = os.environ.get(name)
        if value:
            return value
    return ""


def verify_setting(config: dict | None = None):
    """The ``verify`` value for ``requests``/``httpx``: a CA path or ``True``."""
    path = ca_bundle(config)
    if not path:
        return True
    if path not in _WARNED_CA_PATHS and not os.path.exists(path):
        _WARNED_CA_PATHS.add(path)
        logging.warning(
            "Configured CA bundle does not exist: %s. TLS verification will fail "
            "until it does.", path)
    return path


def has_credentials(config: dict | None = None) -> bool:
    """Whether a saved proxy username and password are available.

    Diagnostic only: a 407 beside this being true means the proxy rejected the
    credentials or their authentication scheme, not that they were absent.
    """
    username, password = _configured_credentials(
        config if config is not None else load_current_config())
    return bool(username and password)


def proxy_error(exc: BaseException) -> dict | None:
    """What a 407 exception says, or ``None`` when it is not one.

    ``requests`` wraps the proxy's refusal in ``ProxyError``; the status lives in
    the string, not in a structured field, so the two spellings the proxy and
    urllib3 use are matched. Any other proxy failure (connection refused,
    timeout) is deliberately not treated as an authentication problem: asking
    for credentials would not fix it.
    """
    text = str(exc or "")
    if "407" not in text and "Proxy Authentication Required" not in text:
        return None
    request = getattr(exc, "request", None)
    request_url = getattr(request, "url", "") or "https://steamcommunity.com/"
    proxy_url = proxy_url_for(request_url)
    return {
        "proxy_url": proxy_url,
        "detail": AUTH_REQUIRED_DETAIL,
    }


def record_auth_required(db_path: str, proxy_url: str | None = None,
                         detail: str | None = None) -> bool:
    """Record that the proxy wants credentials, for the front ends to ask for.

    The first sighting's ``detected_at`` is kept while the same proxy is still
    refusing: every later attempt would otherwise move the timestamp, and a page
    that auto-opens or re-prompts on a new timestamp would open the dialog every
    retry instead of once per problem.
    """
    if not db_path:
        return False
    store = StateStore(state_path_for(db_path))
    url = display_proxy(proxy_url or "")
    detail = detail or AUTH_REQUIRED_DETAIL
    previous = store.load().get(SECTION)
    if isinstance(previous, dict) and previous.get("url") == url:
        detected_at = previous.get("detected_at")
        if isinstance(detected_at, (int, float)) and not isinstance(detected_at, bool):
            return store.save({SECTION: {
                "url": url,
                "detail": detail,
                "detected_at": int(detected_at),
            }})
    return store.save({SECTION: {
        "url": url,
        "detail": detail,
        "detected_at": int(time.time()),
    }})


def clear_auth_required(db_path: str) -> bool:
    """Clear the proxy problem, because a request has now got through."""
    if not db_path:
        return False
    return StateStore(state_path_for(db_path)).remove(SECTION)


def read_auth_required(db_path: str) -> dict | None:
    """The recorded proxy problem, or ``None`` when there is not one."""
    if not db_path:
        return None
    section = StateStore(state_path_for(db_path)).load().get(SECTION)
    if not isinstance(section, dict):
        return None
    url = str(section.get("url") or "")
    detail = str(section.get("detail") or AUTH_REQUIRED_DETAIL)
    detected_at = section.get("detected_at")
    if not url and not detail:
        return None
    return {
        "url": url,
        "detail": detail,
        "detected_at": (int(detected_at)
                        if isinstance(detected_at, (int, float))
                        and not isinstance(detected_at, bool) else None),
    }


def record_auth_required_from_config(proxy_url: str | None = None,
                                     detail: str | None = None,
                                     config: dict | None = None) -> bool:
    """Record a 407 using the database path in the current config.

    ``src.steam_api`` has no database path of its own; it is called from the
    daemon's fetch loop, which does. Reading the path here keeps every 407
    reported through one function instead of adding a parameter to each API
    call.
    """
    config = load_current_config() if config is None else config
    db_path = str((config.get("database") or {}).get("path") or "")
    return record_auth_required(db_path, proxy_url, detail)


def save_credentials(config_path_value: str, url: str, username: str,
                     password: str, config: dict | None = None) -> dict:
    """Persist proxy credentials to ``config.yaml`` and return the saved section.

    Only the ``proxy`` section is passed to ``save_config``. That function
    deep-merges what it is given over the file on disk, so handing it the whole
    in-memory config would rewrite every key the caller happens to hold -- the
    failure issue 91 records. The section itself is the one this function owns.

    The URL is saved without userinfo; the username and password are separate
    keys so the prompt can edit one without re-typing the other. A URL that
    arrived with credentials embedded is split into those keys rather than
    written back into ``url``, so the password has one home.
    """
    clean_url, url_user, url_password = _split_userinfo(url)
    if not clean_url:
        raise ValueError("a proxy URL is required")
    if not os.path.exists(config_path_value):
        raise FileNotFoundError(f"Config file not found: {config_path_value}")
    username = str(username or "") or url_user
    password = str(password or "") or url_password
    section = {"url": clean_url}
    if username:
        section["username"] = username
    if password:
        section["password"] = password

    from src.config import save_config
    save_config(config_path_value, {CONFIG_SECTION: section})
    if config is not None:
        config[CONFIG_SECTION] = dict(section)

    # A password in the config must be scrubbed from a crash dump like the Steam
    # cookie is. Registration is best-effort and must not fail the save.
    try:
        from src import crash
        if password:
            crash.register_secret(password)
        if section.get("username") and password:
            crash.register_secret(
                f"{section['username']}:{password}")
    except Exception:
        logging.debug("Could not register proxy credentials for crash scrubbing",
                      exc_info=True)
    logging.info("Proxy credentials saved for %s", display_proxy(clean_url) or "the detected proxy")
    return section
