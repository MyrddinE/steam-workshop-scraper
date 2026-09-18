"""The downloaded-star folder helper: discovery, the latch scan, and the open action.

`src/subscription.py` decides that `own_subscribed` plus `downloaded_at` draws the
green star; this module is what produces the second half. Three properties matter
and are pinned here:

* the scan stamps only subscribed, *unconfirmed* items whose folder is on disk,
  checks every candidate directory, and **never clears** -- a folder that
  disappears must not take the star away;
* the open action refuses unless the item is green, warns and changes nothing
  when the folder is gone at click time, and never launches off Windows;
* discovery reads Steam's own files (both `libraryfolders.vdf` locations, with
  VDF escaping) and never becomes a per-check registry read.

No test may launch Explorer: the launcher is injected everywhere, and the only
default (`os.startfile`) is never reached off Windows.
"""

import logging
import os

import pytest

from src import workshop_folders as wf
from src.database import get_connection, initialize_database, insert_or_update_item


@pytest.fixture(autouse=True)
def _clean_discovery_cache():
    """The discovery cache is process-wide by design; tests must not share it."""
    wf.reset_discovery_cache()
    yield
    wf.reset_discovery_cache()


def _db(tmp_path) -> str:
    path = str(tmp_path / "folders.db")
    initialize_database(path)
    return path


def _seed(db_path, wid, *, appid=294100, own_subscribed=1, downloaded_at=None,
          title=None):
    insert_or_update_item(db_path, {
        "workshop_id": wid, "title": title or f"Item {wid}", "status": 200,
        "consumer_appid": appid, "own_subscribed": own_subscribed,
        "downloaded_at": downloaded_at,
    })


def _row(db_path, wid) -> dict:
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM workshop_items WHERE workshop_id = ?",
                       (wid,)).fetchone()
    conn.close()
    return dict(row)


def _service(db_path, content_dirs=None, *, platform="win32", launcher=None,
             discover=None, config=None):
    """A locator with an injected launcher and, normally, a config override.

    ``content_dirs`` is written as the ``steam.workshop_content_dirs`` override
    so discovery (which reads the real registry) is never consulted unless a test
    injects ``discover`` explicitly.
    """
    if config is None:
        config = ({"steam": {"workshop_content_dirs": list(content_dirs)}}
                  if content_dirs is not None else {})
    calls: list[str] = []
    return wf.WorkshopFolders(
        db_path, config, platform=platform,
        launcher=launcher if launcher is not None else calls.append,
        discover=discover), calls


# --- VDF parsing ------------------------------------------------------------

def test_the_vdf_reader_unescapes_windows_paths():
    body = r'"libraryfolders" { "0" { "path" "D:\\SteamLibrary" } }'
    assert wf.parse_libraryfolders_vdf(body) == [r"D:\SteamLibrary"]


def test_the_vdf_reader_returns_every_path_in_file_order():
    body = (r'"libraryfolders"'
            r' { "0" { "path" "C:\\Program Files (x86)\\Steam" }'
            r'   "1" { "path" "E:\\Games\\Steam" } }')
    assert wf.parse_libraryfolders_vdf(body) == [
        r"C:\Program Files (x86)\Steam", r"E:\Games\Steam"]


def test_the_vdf_reader_handles_an_escaped_quote_in_a_path():
    body = r'"libraryfolders" { "0" { "path" "D:\\Steam \"beta\"" } }'
    assert wf.parse_libraryfolders_vdf(body) == ['D:\\Steam "beta"']


def test_an_unreadable_libraryfolders_file_is_empty(tmp_path):
    assert wf.read_library_paths(str(tmp_path / "absent.vdf")) == []


# --- discovery --------------------------------------------------------------

def _write_libraryfolders(steam, relative, body):
    path = os.path.join(str(steam), relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)


def test_discovery_reads_the_current_libraryfolders_location(tmp_path):
    steam = tmp_path / "Steam"
    _write_libraryfolders(steam, os.path.join("steamapps", "libraryfolders.vdf"),
                          r'"libraryfolders" { "1" { "path" "D:\\Lib" } }')

    dirs = wf.discover_content_dirs(steam_path=str(steam), platform="win32")

    assert os.path.join(str(steam), "steamapps", "workshop", "content") in dirs
    assert os.path.join(r"D:\Lib", "steamapps", "workshop", "content") in dirs


def test_discovery_reads_the_older_config_libraryfolders_location(tmp_path):
    steam = tmp_path / "Steam"
    _write_libraryfolders(steam, os.path.join("config", "libraryfolders.vdf"),
                          r'"libraryfolders" { "1" { "path" "D:\\Lib" } }')

    dirs = wf.discover_content_dirs(steam_path=str(steam), platform="win32")

    assert os.path.join(r"D:\Lib", "steamapps", "workshop", "content") in dirs


def test_discovery_unions_both_locations(tmp_path):
    steam = tmp_path / "Steam"
    _write_libraryfolders(steam, os.path.join("steamapps", "libraryfolders.vdf"),
                          r'"libraryfolders" { "1" { "path" "D:\\One" } }')
    _write_libraryfolders(steam, os.path.join("config", "libraryfolders.vdf"),
                          r'"libraryfolders" { "1" { "path" "E:\\Two" } }')

    dirs = wf.discover_content_dirs(steam_path=str(steam), platform="win32")

    assert os.path.join(r"D:\One", "steamapps", "workshop", "content") in dirs
    assert os.path.join(r"E:\Two", "steamapps", "workshop", "content") in dirs


def test_discovery_uses_the_registry_steam_path(tmp_path):
    steam = tmp_path / "Steam"
    _write_libraryfolders(steam, os.path.join("steamapps", "libraryfolders.vdf"),
                          r'"libraryfolders" { "1" { "path" "D:\\Lib" } }')

    dirs = wf.discover_content_dirs(registry=lambda: str(steam), platform="win32")

    assert os.path.join(str(steam), "steamapps", "workshop", "content") in dirs


def test_registry_steam_path_is_read_through_the_injected_reader():
    assert wf.read_steam_path(registry=lambda: r"C:\Steam") == r"C:\Steam"
    assert wf.read_steam_path(registry=lambda: None) is None


@pytest.mark.skipif(wf.is_windows(), reason="the default registry read only applies on Windows")
def test_the_default_registry_read_is_none_off_windows():
    assert wf.read_steam_path() is None


def test_discovery_is_empty_off_windows():
    assert wf.discover_content_dirs(platform="linux") == []


def test_config_dirs_are_added_to_discovery_not_instead_of_it(tmp_path):
    svc, _ = _service(
        _db(tmp_path),
        config={"steam": {"workshop_content_dirs": [str(tmp_path / "extra")]}},
        discover=lambda: [str(tmp_path / "found")])

    assert svc.content_dirs() == [str(tmp_path / "extra"), str(tmp_path / "found")]


def test_a_bare_string_config_entry_is_one_directory(tmp_path):
    svc, _ = _service(_db(tmp_path), discover=lambda: [])
    svc.config = {"steam": {"workshop_content_dirs": str(tmp_path / "one")}}
    assert svc.content_dirs() == [str(tmp_path / "one")]


def test_discovery_happens_once_while_a_lookup_succeeds(tmp_path):
    calls = []

    def discover():
        calls.append(1)
        return [str(tmp_path / "content")]

    svc, _ = _service(_db(tmp_path), config={}, discover=discover)
    svc.content_dirs()
    svc.content_dirs()

    assert len(calls) == 1, "a check must not re-read the registry"


def test_a_lookup_that_finds_nothing_re_resolves(tmp_path):
    calls = []

    def discover():
        calls.append(1)
        return [str(tmp_path / "content")]

    svc, _ = _service(_db(tmp_path), config={}, discover=discover)
    assert svc.item_folder(294100, 999) is None
    svc.content_dirs()

    assert len(calls) == 2, "a miss is the signal that a library may have appeared"


# --- the scan ---------------------------------------------------------------

def test_scan_stamps_a_subscribed_item_whose_folder_exists(tmp_path):
    db = _db(tmp_path)
    content = tmp_path / "content"
    (content / "294100" / "5").mkdir(parents=True)
    _seed(db, 5)
    svc, _ = _service(db, [str(content)])

    result = svc.scan(now=1234)

    assert result == {"checked": 1, "stamped": 1}
    assert _row(db, 5)["downloaded_at"] == 1234


def test_scan_leaves_an_item_that_is_not_subscribed_alone(tmp_path):
    db = _db(tmp_path)
    content = tmp_path / "content"
    (content / "294100" / "5").mkdir(parents=True)
    _seed(db, 5, own_subscribed=0)
    svc, _ = _service(db, [str(content)])

    assert svc.scan() == {"checked": 0, "stamped": 0}
    assert _row(db, 5)["downloaded_at"] is None


def test_scan_leaves_an_item_whose_folder_is_missing_unstamped(tmp_path):
    db = _db(tmp_path)
    _seed(db, 5)
    svc, _ = _service(db, [str(tmp_path / "content")])

    assert svc.scan() == {"checked": 1, "stamped": 0}
    assert _row(db, 5)["downloaded_at"] is None


def test_scan_checks_every_candidate_directory(tmp_path):
    db = _db(tmp_path)
    first, second = tmp_path / "one", tmp_path / "two"
    (second / "294100" / "5").mkdir(parents=True)
    _seed(db, 5)
    svc, _ = _service(db, [str(first), str(second)])

    assert svc.scan(now=9)["stamped"] == 1
    assert _row(db, 5)["downloaded_at"] == 9


def test_scan_never_revisits_a_confirmed_item(tmp_path):
    db = _db(tmp_path)
    content = tmp_path / "content"
    (content / "294100" / "5").mkdir(parents=True)
    _seed(db, 5, downloaded_at=111)
    svc, _ = _service(db, [str(content)])

    assert svc.scan() == {"checked": 0, "stamped": 0}
    assert _row(db, 5)["downloaded_at"] == 111, "the latch must not move"


def test_scan_never_clears_when_the_folder_disappears(tmp_path):
    db = _db(tmp_path)
    content = tmp_path / "content"
    folder = content / "294100" / "5"
    folder.mkdir(parents=True)
    _seed(db, 5)
    svc, _ = _service(db, [str(content)])
    svc.scan(now=42)
    assert _row(db, 5)["downloaded_at"] == 42

    # The drive is unplugged / the library moved: the folder is gone.
    os.rmdir(folder)

    assert svc.scan() == {"checked": 0, "stamped": 0}
    assert _row(db, 5)["downloaded_at"] == 42, \
        "a missing folder must never take the green star away"


def test_scan_never_clears_a_stray_latch_without_a_subscription(tmp_path):
    db = _db(tmp_path)
    _seed(db, 5, own_subscribed=0, downloaded_at=7)
    svc, _ = _service(db, [])

    svc.scan()

    assert _row(db, 5)["downloaded_at"] == 7


def test_scan_off_windows_is_a_silent_noop(tmp_path, caplog):
    db = _db(tmp_path)
    content = tmp_path / "content"
    (content / "294100" / "5").mkdir(parents=True)
    _seed(db, 5)
    svc, _ = _service(db, [str(content)], platform="linux")

    with caplog.at_level(logging.INFO):
        assert svc.scan() == {"checked": 0, "stamped": 0}
        assert svc.scan() == {"checked": 0, "stamped": 0}

    assert _row(db, 5)["downloaded_at"] is None
    assert caplog.records == [], "nothing logs per check"


def test_scan_logs_once_when_it_changed_something(tmp_path, caplog):
    db = _db(tmp_path)
    content = tmp_path / "content"
    (content / "294100" / "5").mkdir(parents=True)
    _seed(db, 5)
    svc, _ = _service(db, [str(content)])

    with caplog.at_level(logging.INFO):
        svc.scan()
        svc.scan()

    lines = [r for r in caplog.records if "Downloaded-item scan" in r.getMessage()]
    assert len(lines) == 1, "one line per changing scan, and nothing when unchanged"


def test_the_status_note_names_why_the_feature_is_off(tmp_path):
    off_platform, _ = _service(_db(tmp_path), [], platform="linux")
    assert "Windows" in off_platform.status_note()

    no_dirs, _ = _service(_db(tmp_path), [], platform="win32")
    assert "no Steam workshop content folder" in no_dirs.status_note()

    on, _ = _service(_db(tmp_path), [str(tmp_path / "content")], platform="win32")
    assert on.status_note() is None


# --- the open action --------------------------------------------------------

def _green(db, tmp_path, *, wid=5):
    content = tmp_path / "content"
    folder = content / "294100" / str(wid)
    folder.mkdir(parents=True)
    _seed(db, wid)
    conn = get_connection(db)
    conn.execute("UPDATE workshop_items SET downloaded_at = 1 WHERE workshop_id = ?", (wid,))
    conn.commit()
    conn.close()
    return content, folder


def test_open_refuses_an_item_that_is_not_downloaded(tmp_path):
    db = _db(tmp_path)
    _seed(db, 5, own_subscribed=1)
    svc, launched = _service(db, [str(tmp_path / "content")])

    result = svc.open(5)

    assert result["ok"] is False
    assert launched == []
    assert _row(db, 5)["downloaded_at"] is None


def test_open_refuses_a_stray_latch_without_a_subscription(tmp_path):
    db = _db(tmp_path)
    content, folder = _green(db, tmp_path)
    conn = get_connection(db)
    conn.execute("UPDATE workshop_items SET own_subscribed = 0 WHERE workshop_id = 5")
    conn.commit()
    conn.close()
    svc, launched = _service(db, [str(content)])

    result = svc.open(5)

    assert result["ok"] is False
    assert launched == []
    assert _row(db, 5)["downloaded_at"] is not None, "the refusal changes nothing"


def test_open_warns_naming_the_missing_folder_without_changing_state(tmp_path):
    db = _db(tmp_path)
    content, folder = _green(db, tmp_path)
    os.rmdir(folder)
    svc, launched = _service(db, [str(content)])

    result = svc.open(5)

    assert result["ok"] is False
    assert str(content) in result["message"], "the warning must name where it looked"
    assert launched == []
    assert _row(db, 5)["downloaded_at"] is not None, "a missing folder clears nothing"


def test_open_launches_the_folder_when_the_item_is_green(tmp_path):
    db = _db(tmp_path)
    content, folder = _green(db, tmp_path)
    svc, launched = _service(db, [str(content)])

    result = svc.open(5)

    assert result["ok"] is True
    assert launched == [str(folder)]


def test_open_never_launches_off_windows(tmp_path):
    db = _db(tmp_path)
    content, folder = _green(db, tmp_path)
    svc, launched = _service(db, [str(content)], platform="linux")

    result = svc.open(5)

    assert result["ok"] is False
    assert "Windows" in result["message"]
    assert launched == []
    assert folder.exists()


def test_open_reports_an_unknown_item(tmp_path):
    svc, launched = _service(_db(tmp_path), [])
    result = svc.open(12345)

    assert result["ok"] is False
    assert launched == []
