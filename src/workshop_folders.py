"""Find the downloaded copy of a subscribed workshop item, and open its folder.

Steam keeps a subscribed item's content at
``<library>/steamapps/workshop/content/<consumer_appid>/<workshop_id>/``. The
marker state ``downloaded`` means "the owner is subscribed *and* this app has
seen that folder on disk", and this module owns the three things that need that
fact:

* :func:`WorkshopFolders.scan` -- the periodic walk that stamps
  ``downloaded_at`` for subscribed, unconfirmed items whose folder exists. **It
  only ever writes.** A missing folder, an unplugged drive or a moved library
  must never take the green star away, and a confirmed item is never revisited;
  the only clearer is the subscription walk in ``src.database``, for the item
  that leaves the owner's subscription list. Re-subscribing re-earns the stamp on
  the next scan, because the files are usually still on disk.
* :func:`WorkshopFolders.open` -- the click-time action behind the TUI key and
  button and the web ``POST /api/open_folder/<id>`` route. It refuses unless the
  item is in the ``downloaded`` state, and if the folder is gone at that moment
  it warns, naming the places it looked, and leaves the state alone.
* the discovery of the library folders themselves.

**Discovery** reads Steam's install path from the registry
(``HKCU\\Software\\Valve\\Steam``, ``SteamPath``), then parses that install's
``libraryfolders.vdf`` for every library ``path``. Steam has used two locations
for that file -- ``<steam>/steamapps/libraryfolders.vdf`` (current) and
``<steam>/config/libraryfolders.vdf`` (older) -- and both are read, so an old or
new install resolves either way. The VDF escaping (``\\\\`` for a backslash) is
undone here. ``steam.workshop_content_dirs`` in the config is added to whatever
discovery finds, never instead of it, for a library the reader cannot see.

**Resolution is once per process.** The discovered list is cached at module
level and shared by every :class:`WorkshopFolders` instance, so the registry is
not re-read on every check. A lookup that finds nothing drops the cache, which
is what lets a second drive that appeared later be found on the next check.

**Degrade to nothing, quietly.** Not Windows, no Steam, an unreadable
``libraryfolders.vdf``: every function here becomes a no-op, every item stays
without a ``downloaded`` marker, and ``scan`` logs nothing per check.
:meth:`WorkshopFolders.log_status` is the one startup line that says why it is
off -- the daemon log is already hundreds of megabytes (issue 37), so a line per
check would be the wrong shape.

**No new dependency.** The registry is read with ``winreg`` and the VDF with a
small hand-rolled regex; neither is imported off Windows.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time

from src.database import get_connection

#: How long the daemon and TUI wait between folder scans. The scan checks a
#: directory per subscribed item that is not yet confirmed, so a minute bounds
#: the lag between Steam finishing a download and the star turning green while
#: keeping the per-batch path free of it.
DOWNLOAD_SCAN_INTERVAL_SECONDS = 60

#: Where the config override lives. Entries are workshop *content* roots
#: (``<library>/steamapps/workshop/content``), added to discovery's own list.
CONFIG_SECTION = "steam"
CONFIG_DIRS_KEY = "workshop_content_dirs"

#: The registry key and value Steam writes its install path to.
STEAM_REGISTRY_KEY = r"Software\Valve\Steam"
STEAM_PATH_VALUE = "SteamPath"

#: The two places Steam has kept ``libraryfolders.vdf``, relative to the
#: install. Both are tried; the current location is listed first only for
#: determinism, since the results are unions either way.
LIBRARYFOLDERS_RELATIVE_PATHS = (
    os.path.join("steamapps", "libraryfolders.vdf"),
    os.path.join("config", "libraryfolders.vdf"),
)

#: Everything between a library root and an item's own folder.
WORKSHOP_CONTENT_PARTS = ("steamapps", "workshop", "content")

#: One ``"path" "..."`` value, with the value kept whole (escapes included) so a
#: backslash-escaped path is not cut at the first backslash.
_VDF_PATH_RE = re.compile(r'"path"\s*"((?:[^"\\]|\\.)*)"')


def is_windows(platform: str | None = None) -> bool:
    """Whether ``platform`` (default: this process's) is Windows.

    Injectable so the whole module is testable off Windows; the string matches
    ``sys.platform``, and ``"win32"`` is the only Windows value.
    """
    return (platform or sys.platform) == "win32"


def _unescape_vdf(value: str) -> str:
    """Undo VDF's backslash escaping in one quoted value.

    ``\\\\`` becomes ``\\`` and ``\\"`` becomes ``"``; any other backslash is
    left standing, which is what a path that carried a lone separator would have
    looked like in a hand-edited file.
    """
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            out.append(value[index + 1])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def parse_libraryfolders_vdf(text: str) -> list[str]:
    """Every library ``path`` in one ``libraryfolders.vdf`` body, unescaped.

    Order is the file's; duplicates are left to the caller. The parser is
    deliberately narrow -- only ``"path" "value"`` entries are read -- because
    that is the only field this project needs, and a full VDF parser would be a
    dependency or a lot of code for it.
    """
    return [_unescape_vdf(match.group(1)) for match in _VDF_PATH_RE.finditer(text)]


def read_library_paths(vdf_path: str) -> list[str]:
    """The library paths in ``vdf_path``, or ``[]`` when it cannot be read.

    An absent or unreadable file is not an error here: Steam may not be
    installed, the file may be mid-rewrite, and either way the feature simply
    finds nothing.
    """
    try:
        with open(vdf_path, "r", encoding="utf-8", errors="replace") as handle:
            return parse_libraryfolders_vdf(handle.read())
    except OSError:
        return []


def read_steam_path(registry=None) -> str | None:
    """Steam's install path from the registry, or None when it is not there.

    ``registry`` is injectable for tests; the default reads
    ``HKCU\\Software\\Valve\\Steam``'s ``SteamPath`` with ``winreg``. Imported
    lazily so a non-Windows process never touches it.
    """
    if registry is not None:
        return registry()
    if not is_windows():
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STEAM_REGISTRY_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, STEAM_PATH_VALUE)
            return value or None
    except (OSError, ImportError):
        # A missing key or value means Steam is not installed for this user; a
        # missing ``winreg`` means this is not really Windows (a simulated
        # platform, or a stripped build), and there is nothing to read either.
        return None


def _content_root(library: str) -> str:
    return os.path.join(library, *WORKSHOP_CONTENT_PARTS)


def _dedupe(paths) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        if not path:
            continue
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


#: Process-wide cache of the discovered content roots. Shared by every locator
#: so the registry is read once, and dropped by a lookup that found nothing so a
#: library that appeared later is picked up.
_discovered_dirs: list[str] | None = None


def reset_discovery_cache() -> None:
    """Forget the discovered roots; the next resolution re-reads Steam.

    Called when a lookup misses, and by tests that need a clean process.
    """
    global _discovered_dirs
    _discovered_dirs = None


def discover_content_dirs(steam_path: str | None = None, registry=None,
                          platform: str | None = None) -> list[str]:
    """The workshop content roots Steam's own files point at.

    ``steam_path`` short-circuits the registry read (tests and callers that
    already have it). The install's own library is always included -- the
    primary library is not reliably listed in ``libraryfolders.vdf`` -- and then
    every ``path`` the file names, in both of its known locations.
    """
    if steam_path is None:
        if not is_windows(platform):
            return []
        steam_path = read_steam_path(registry)
    if not steam_path:
        return []

    libraries: list[str] = []
    for relative in LIBRARYFOLDERS_RELATIVE_PATHS:
        libraries.extend(read_library_paths(os.path.join(steam_path, relative)))

    return _dedupe([_content_root(steam_path)]
                   + [_content_root(library) for library in libraries])


def _default_launcher(path: str) -> None:
    """Open ``path`` in the OS file manager (Explorer on Windows).

    ``os.startfile`` exists only on Windows, and this is the one place the
    feature reaches out of the process. Tests inject their own launcher so no
    test can open a real window.
    """
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise RuntimeError("the Windows shell is not available on this platform")
    startfile(path)


class WorkshopFolders:
    """Resolve Steam's workshop content roots, stamp downloads, open a folder.

    One instance per process part (the daemon, the TUI, the web server) is
    enough; :meth:`content_dirs` caches per instance on top of the module-level
    discovery cache, so a hit never re-reads the registry while a miss does.
    """

    def __init__(self, db_path: str, config: dict | None = None, *,
                 platform: str | None = None, launcher=None, registry=None,
                 discover=None):
        self.db_path = db_path
        self.config = config or {}
        self._platform = platform
        self._launcher = launcher or _default_launcher
        self._registry = registry
        self._discover = discover or self._discover_default
        self._discovered_dirs: list[str] | None = None

    # --- platform and resolution -------------------------------------------

    def enabled(self) -> bool:
        """Whether this platform can have the feature at all."""
        return is_windows(self._platform)

    def _discover_default(self) -> list[str]:
        global _discovered_dirs
        if _discovered_dirs is None:
            _discovered_dirs = discover_content_dirs(
                registry=self._registry, platform=self._platform)
        return _discovered_dirs

    def _configured_dirs(self) -> list[str]:
        """The ``steam.workshop_content_dirs`` override, always included.

        A bare string is accepted as a one-entry list so a hand-written config
        that forgot the dash still works; anything else is ignored rather than
        raising, because a bad override must not stop the feature.
        """
        value = (self.config.get(CONFIG_SECTION) or {}).get(CONFIG_DIRS_KEY)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return []
        return [str(entry) for entry in value if entry]

    def content_dirs(self, refresh: bool = False) -> list[str]:
        """Every root to look in: the config override plus discovery's own.

        Cached per instance; ``refresh`` (and a miss, see :meth:`item_folder`)
        forces discovery again.
        """
        if self._discovered_dirs is None or refresh:
            self._discovered_dirs = list(self._discover())
        return _dedupe(self._configured_dirs() + list(self._discovered_dirs))

    def expected_folders(self, consumer_appid, workshop_id) -> list[str]:
        """Every path the item's folder would have, for naming in a warning."""
        if consumer_appid in (None, ""):
            return []
        return [os.path.join(root, str(consumer_appid), str(workshop_id))
                for root in self.content_dirs()]

    @staticmethod
    def _find_folder_in(dirs, consumer_appid, workshop_id) -> str | None:
        if consumer_appid in (None, ""):
            return None
        for root in dirs:
            candidate = os.path.join(root, str(consumer_appid), str(workshop_id))
            if os.path.isdir(candidate):
                return candidate
        return None

    def item_folder(self, consumer_appid, workshop_id) -> str | None:
        """The item's folder on disk, or None.

        A miss where there *was* somewhere to look is the one reason to look
        again: the cache is dropped so a library that appeared after the last
        resolution is found on the next check. A hit never re-reads the registry,
        and neither does a miss with no candidate root at all (there is nothing
        to re-resolve toward until Steam is installed).
        """
        if not self.enabled():
            return None
        dirs = self.content_dirs()
        folder = self._find_folder_in(dirs, consumer_appid, workshop_id)
        if folder is None and dirs:
            self._discovered_dirs = None
            reset_discovery_cache()
        return folder

    # --- the scan ----------------------------------------------------------

    def scan(self, now: int | None = None) -> dict:
        """Stamp ``downloaded_at`` on subscribed, unconfirmed items on disk.

        The candidate set is exactly ``own_subscribed = 1 AND downloaded_at IS
        NULL``: items that are subscribed and not yet confirmed. It never clears
        anything and never revisits a confirmed item, so an unplugged drive
        cannot take the green star away. Returns ``{"checked", "stamped"}``
        counts; a changing scan logs one line, a scan that changed nothing logs
        nothing.
        """
        if not self.enabled():
            return {"checked": 0, "stamped": 0}
        stamp = int(time.time()) if now is None else int(now)
        dirs = self.content_dirs()
        stamped_ids: list[int] = []
        missed = 0
        rows = []
        conn = get_connection(self.db_path)
        try:
            rows = conn.execute(
                "SELECT workshop_id, consumer_appid FROM workshop_items "
                "WHERE own_subscribed = 1 AND downloaded_at IS NULL"
            ).fetchall()
            for row in rows:
                if self._find_folder_in(dirs, row["consumer_appid"], row["workshop_id"]):
                    stamped_ids.append(row["workshop_id"])
                else:
                    missed += 1
            if stamped_ids:
                placeholders = ",".join("?" * len(stamped_ids))
                # The own_subscribed guard keeps a reconcile that cleared the
                # flag mid-scan from being re-stamped by this write.
                conn.execute(
                    f"UPDATE workshop_items SET downloaded_at = ? "
                    f"WHERE downloaded_at IS NULL AND own_subscribed = 1 "
                    f"AND workshop_id IN ({placeholders})",
                    [stamp, *stamped_ids],
                )
                conn.commit()
        finally:
            conn.close()

        if missed and dirs:
            # Nothing was found for at least one candidate, so a library may have
            # appeared (or moved) since the last resolution; the next scan looks
            # again. A scan whose candidates were all found keeps the cache, and
            # so does one with no candidate root at all (nothing to re-resolve
            # toward, and re-reading the registry every minute would be churn).
            self._discovered_dirs = None
            reset_discovery_cache()
        if stamped_ids:
            logging.info(
                "Downloaded-item scan: stamped %d of %d unconfirmed subscribed "
                "item(s) as downloaded.", len(stamped_ids), len(rows))
        return {"checked": len(rows), "stamped": len(stamped_ids)}

    # --- the open action ---------------------------------------------------

    def open(self, workshop_id: int) -> dict:
        """Open the item's folder in Explorer, if it is green and still there.

        Refuses off Windows, for an unknown item, and for one that is not in the
        ``downloaded`` state. The click-time existence test changes nothing: if
        the folder is not there now (an unplugged drive, a moved library, Steam
        cleaned up), the result names the places it looked and the database is
        left exactly as it was. Never raises for a missing folder; a launcher
        failure is reported in the result.
        """
        if not self.enabled():
            return {
                "ok": False, "folder": None,
                "message": "Opening the workshop folder is only available on Windows.",
            }

        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT consumer_appid, own_subscribed, downloaded_at "
                "FROM workshop_items WHERE workshop_id = ?",
                (workshop_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return {
                "ok": False, "folder": None,
                "message": f"No workshop item with id {workshop_id}.",
            }
        if not (row["own_subscribed"] and row["downloaded_at"]):
            return {
                "ok": False, "folder": None,
                "message": ("Only a subscribed item Steam has downloaded can be opened; "
                            "this one is not there yet."),
            }
        if row["consumer_appid"] in (None, ""):
            return {
                "ok": False, "folder": None,
                "message": ("The item carries no AppID, so its workshop folder cannot "
                            "be located."),
            }

        folder = self.item_folder(row["consumer_appid"], workshop_id)
        if folder is None:
            looked = self.expected_folders(row["consumer_appid"], workshop_id)
            where = ", ".join(looked) if looked else (
                "no Steam workshop content folder is known")
            return {
                "ok": False, "folder": None,
                "message": (f"The item's folder is not on disk right now (looked in: "
                            f"{where}). Its marker was left unchanged."),
            }

        try:
            self._launcher(folder)
        except Exception as exc:
            return {
                "ok": False, "folder": folder,
                "message": f"Could not open {folder}: {exc}",
            }
        return {"ok": True, "folder": folder, "message": f"Opened {folder}"}

    # --- the startup line --------------------------------------------------

    def status_note(self) -> str | None:
        """The one line to log when the feature is off, else None.

        Off means "not Windows" or "nothing to look in"; both leave every item
        without a ``downloaded`` marker and are stated once at startup rather
        than on every check.
        """
        if not self.enabled():
            return ("Downloaded-item markers are off: opening Steam workshop folders "
                    "is only supported on Windows.")
        if not self.content_dirs():
            return ("Downloaded-item markers are off: no Steam workshop content folder "
                    "was found (no SteamPath in the registry and none configured).")
        return None

    def log_status(self) -> None:
        note = self.status_note()
        if note:
            logging.info(note)
