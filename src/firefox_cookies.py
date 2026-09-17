"""Read the Steam login cookie from a local Firefox profile.

`steamLoginSecure` is HttpOnly, so a userscript running in the page can never see
it through `document.cookie`, and Tampermonkey only exposes HttpOnly cookies on
BETA builds. Firefox keeps it in plaintext in `cookies.sqlite` inside the
profile directory, and the daemon runs as the same user who owns that profile,
so it can be read with no browser plugin at all — and, because the browser
refreshes it during ordinary use, without anyone copying it by hand.

Profiles are discovered, never hardcoded: the directory name carries a random
component (`n2qm8s2i.default-release`) that changes when a profile is rebuilt,
and a machine may hold several. The newest store that actually contains a
`cookies.sqlite` wins.
"""

import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

# Firefox holds the store open in WAL mode, so the newest write may be in the
# -wal rather than the .sqlite itself. Copying the set and reading the copy
# avoids both the lock and a stale read.
_SIDECARS = ("", "-wal", "-shm")

COOKIE_STORE_NAME = "cookies.sqlite"
COMPATIBILITY_FILE = "compatibility.ini"
STEAM_HOST = "%steamcommunity.com"
LOGIN_COOKIE = "steamLoginSecure"

# Firefox writes `LastVersion` as `<version>_<buildid>`; a User-Agent carries the
# dotted version only, so the build id after the first `_` is dropped. The
# build id is not always 14 digits, so the version is matched rather than the
# suffix split on a fixed offset.
_LAST_VERSION_RE = re.compile(r"^LastVersion\s*=\s*(\d+(?:\.\d+)*)", re.MULTILINE)

# There is no refresh timer. The cookie is valid for days, so the config holds a
# copy and the browser is consulted when there is no copy yet, or when a scrape
# looks gated and the copy is the likely culprit. Polling on a clock would copy
# a file every few minutes to learn nothing.

_cache_lock = threading.Lock()
_cache = {"cookies": {}, "store": None, "announced": None}


def default_profiles_root() -> Path | None:
    """Where Firefox keeps its profiles on this platform, if it exists."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidate = Path(appdata) / "Mozilla" / "Firefox" / "Profiles"
        if candidate.is_dir():
            return candidate
    candidate = Path.home() / ".mozilla" / "firefox"
    if candidate.is_dir():
        return candidate
    return None


def find_cookie_store(profiles_root: Path | None = None) -> Path | None:
    """The most recently used profile's cookie store, or None.

    Discovery rather than a fixed name: the profile directory includes a random
    component, and a machine can hold several profiles. A profile without a
    store is skipped rather than failing, so a fresh or unused profile cannot
    mask the one that is actually in use.
    """
    root = profiles_root if profiles_root is not None else default_profiles_root()
    if root is None or not Path(root).is_dir():
        return None

    candidates = []
    for entry in Path(root).iterdir():
        store = entry / COOKIE_STORE_NAME
        try:
            if store.is_file():
                candidates.append((store.stat().st_mtime, store))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def firefox_version(profiles_root: Path | None = None) -> str | None:
    """The Firefox version recorded by the profile in use, or None.

    `compatibility.ini` in the profile directory carries `LastVersion`, the
    version that last used that profile. The directory name holds only a random
    salt and no version, so this file is the one local source for the version a
    scraped request should claim. The profile is located with the same discovery
    the cookie read uses, so the version and the cookies describe the same
    browser.

    Returns None rather than raising when there is no profile or no readable
    file: the caller falls back to a constant, and a missing profile is an
    ordinary anonymous configuration, not an error.
    """
    store = find_cookie_store(profiles_root)
    if store is None:
        return None
    compatibility = Path(store).parent / COMPATIBILITY_FILE
    try:
        text = compatibility.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _LAST_VERSION_RE.search(text)
    return match.group(1) if match else None


def _copy_store(store: Path, dest_dir: Path) -> Path:
    """Copy the store and its sidecars, so a live browser cannot block the read."""
    for suffix in _SIDECARS:
        source = Path(str(store) + suffix)
        if source.is_file():
            shutil.copy2(source, dest_dir / (COOKIE_STORE_NAME + suffix))
    return dest_dir / COOKIE_STORE_NAME


def read_steam_cookies(store_path: Path) -> dict:
    """Every steamcommunity.com cookie in the store, as ``{name: value}``.

    Returns an empty dict rather than raising when the store cannot be read: a
    missing or unreadable cookie is a condition to report, not to crash on.
    """
    if not Path(store_path).is_file():
        return {}

    with tempfile.TemporaryDirectory(prefix="ffcookies-") as tmp:
        copied = _copy_store(Path(store_path), Path(tmp))
        try:
            con = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            logging.warning("Could not open the Firefox cookie store: %s", exc)
            return {}
        try:
            found = {}
            for name, value in con.execute(
                "SELECT name, value FROM moz_cookies WHERE host LIKE ?", (STEAM_HOST,)
            ):
                if name and value:
                    found[name] = value
            return found
        except sqlite3.Error as exc:
            # A schema change is the realistic cause, and it must be visible:
            # silently returning {} would look exactly like "not logged in".
            logging.warning(
                "Could not read moz_cookies from %s (%s). Firefox may have changed "
                "its schema; the login cookie is unavailable from this source.",
                store_path, exc,
            )
            return {}
        finally:
            con.close()


def browser_cookies(refresh: bool = False, profiles_root: Path | None = None) -> dict:
    """Every steamcommunity.com cookie in the profile, or {} if unavailable.

    The login cookie is the credential, but `sessionid` lives here too — it is
    not HttpOnly, which is why the userscript could always supply it. Reading the
    set from one place means the two cannot come from different sessions.
    """
    global _cache
    with _cache_lock:
        if not refresh and _cache["cookies"]:
            return dict(_cache["cookies"])

    store = find_cookie_store(profiles_root)
    found = read_steam_cookies(store) if store is not None else {}

    with _cache_lock:
        _cache["cookies"] = found
        _cache["store"] = store
    if not found:
        logging.warning(
            "No Steam cookies found in any Firefox profile under %s. Scrapes will "
            "run anonymously unless cookies are supplied another way.",
            profiles_root or default_profiles_root() or "the default location",
        )
    elif found.get(LOGIN_COOKIE) != _cache.get("announced"):
        with _cache_lock:
            _cache["announced"] = found.get(LOGIN_COOKIE)
        logging.info(
            "Read %d Steam cookies from the Firefox profile (%s), including %s.",
            len(found), store, LOGIN_COOKIE,
        )
    return dict(found)


def steam_login_secure(refresh: bool = False,
                       profiles_root: Path | None = None) -> str | None:
    """The current `steamLoginSecure`, or None if it cannot be found.

    The last value read is remembered; pass ``refresh=True`` to look again. This
    function has no cache lifetime of its own, because the caller decides when
    looking again is worth a file copy: a gate-shaped scrape failure is the
    signal, not a clock.

    The value it returns does expire, though -- Steam reissues `steamLoginSecure`
    about daily, and a profile nobody has opened keeps the stale one. Callers can
    read that expiry off the value with :func:`src.session_cookie.parse` rather
    than inferring it from a request that came back as the sign-in page.
    """
    return browser_cookies(refresh=refresh, profiles_root=profiles_root).get(LOGIN_COOKIE)


def clear_cache() -> None:
    """Forget the cached cookie. For tests, and for a forced re-read."""
    with _cache_lock:
        _cache["cookies"] = {}
        _cache["store"] = None
        _cache["announced"] = None
