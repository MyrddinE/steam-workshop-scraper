"""The login cookie can come from the browser's own store.

`steamLoginSecure` is HttpOnly, so no userscript can read it from the page on a
stable Tampermonkey. Firefox keeps it in plaintext and the daemon runs as the
same user who owns the profile, so the store is the one source that needs
neither a plugin nor a hand-copied value.
"""

import base64
import json
import os
import sqlite3
from unittest.mock import patch

import pytest

from src import firefox_cookies
from src.firefox_cookies import (
    clear_cache,
    find_cookie_store,
    firefox_version,
    read_steam_cookies,
    steam_login_secure,
)
from src.web_scraper import _resolve_login_secure, _csrf_token


@pytest.fixture(autouse=True)
def _state_file_in_tmp(tmp_path, monkeypatch):
    """Keep the worker's session record out of the checkout.

    The refresh-and-retry tests below build workers on a literal "test.db", and a
    signed-out scrape records that fact beside its database. Redirecting the path
    keeps the behaviour under test identical while leaving the checkout clean.
    """
    monkeypatch.setattr(
        "src.session_health.state_path_for",
        lambda db_path: str(tmp_path / (os.path.basename(str(db_path)) + ".state.yaml")),
    )


def _make_store(path, rows, table=True):
    con = sqlite3.connect(str(path))
    if table:
        con.execute(
            "CREATE TABLE IF NOT EXISTS moz_cookies "
            "(id INTEGER PRIMARY KEY, name TEXT, value TEXT, host TEXT)"
        )
        con.execute("DELETE FROM moz_cookies")
        con.executemany("INSERT INTO moz_cookies (name, value, host) VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return path


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


# --- discovery -------------------------------------------------------------

def test_picks_the_newest_profile_that_has_a_store(tmp_path):
    old = tmp_path / "aaa.default-release"
    old.mkdir()
    _make_store(old / "cookies.sqlite", [])
    newest = tmp_path / "zzz.default-release"
    newest.mkdir()
    _make_store(newest / "cookies.sqlite", [])

    # mtimes decide, not names: the random component in a profile name is not sortable
    import os
    os.utime(old / "cookies.sqlite", (1_000_000, 1_000_000))
    os.utime(newest / "cookies.sqlite", (2_000_000, 2_000_000))

    assert find_cookie_store(tmp_path) == newest / "cookies.sqlite"


def test_a_profile_without_a_store_is_skipped(tmp_path):
    """An unused profile must not mask the one actually in use."""
    empty = tmp_path / "brand.new"
    empty.mkdir()
    used = tmp_path / "used.default-release"
    used.mkdir()
    _make_store(used / "cookies.sqlite", [])
    import os
    os.utime(used / "cookies.sqlite", (1_000_000, 1_000_000))

    assert find_cookie_store(tmp_path) == used / "cookies.sqlite"


def test_no_profiles_at_all_is_not_an_error(tmp_path):
    assert find_cookie_store(tmp_path) is None
    assert find_cookie_store(tmp_path / "does-not-exist") is None


# --- the version the request should claim ----------------------------------

def test_reads_the_firefox_version_from_the_profile(tmp_path):
    """The UA version is taken from the profile that supplies the cookies.

    `compatibility.ini` writes `LastVersion` as `<version>_<buildid>`; the UA
    carries the dotted version only.
    """
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    _make_store(profile / "cookies.sqlite", [])
    (profile / "compatibility.ini").write_text(
        "[Compatibility]\n"
        "LastVersion=141.0.3_20251001120000/20251001120000\n"
        "LastOSABI=Linux_x86_64-gcc3\n",
        encoding="utf-8",
    )

    assert firefox_version(tmp_path) == "141.0.3"


def test_no_compatibility_file_yields_none(tmp_path):
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    _make_store(profile / "cookies.sqlite", [])

    assert firefox_version(tmp_path) is None


def test_an_unparseable_last_version_yields_none(tmp_path):
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    _make_store(profile / "cookies.sqlite", [])
    (profile / "compatibility.ini").write_text(
        "[Compatibility]\nLastVersion=unknown\n", encoding="utf-8")

    assert firefox_version(tmp_path) is None


def test_no_profile_yields_none(tmp_path):
    assert firefox_version(tmp_path) is None


# --- reading ---------------------------------------------------------------

def test_reads_the_steam_cookies(tmp_path):
    store = _make_store(tmp_path / "cookies.sqlite", [
        ("steamLoginSecure", "LOGIN_VALUE", ".steamcommunity.com"),
        ("sessionid", "SID", "steamcommunity.com"),
        ("unrelated", "X", ".example.com"),
    ])
    found = read_steam_cookies(store)
    assert found["steamLoginSecure"] == "LOGIN_VALUE"
    assert found["sessionid"] == "SID"
    assert "unrelated" not in found


def test_a_schema_change_is_reported_not_swallowed(tmp_path, caplog):
    """Silently returning {} would look identical to 'not logged in'."""
    store = _make_store(tmp_path / "cookies.sqlite", [], table=False)
    with caplog.at_level("WARNING"):
        assert read_steam_cookies(store) == {}
    assert any("moz_cookies" in r.message for r in caplog.records)


def test_a_missing_store_reads_as_empty(tmp_path):
    assert read_steam_cookies(tmp_path / "absent.sqlite") == {}


# --- the accessor ----------------------------------------------------------

def test_returns_the_cookie_and_caches_it(tmp_path):
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    _make_store(profile / "cookies.sqlite", [
        ("steamLoginSecure", "FROM_BROWSER", ".steamcommunity.com"),
    ])
    assert steam_login_secure(profiles_root=tmp_path) == "FROM_BROWSER"

    # a second call must not re-read: delete the store and it still answers
    (profile / "cookies.sqlite").unlink()
    assert steam_login_secure(profiles_root=tmp_path) == "FROM_BROWSER"

    clear_cache()
    assert steam_login_secure(profiles_root=tmp_path) is None


def test_finding_it_is_announced(tmp_path, caplog):
    """Silence on success would leave 'is this working?' unanswerable from the log."""
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    _make_store(profile / "cookies.sqlite", [
        ("steamLoginSecure", "FROM_BROWSER", ".steamcommunity.com"),
    ])
    with caplog.at_level("INFO"):
        steam_login_secure(profiles_root=tmp_path)
    assert any("Read 1 Steam cookies from the Firefox profile" in r.message
               for r in caplog.records)


def test_nothing_found_is_reported(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        assert steam_login_secure(profiles_root=tmp_path) is None
    assert any("No Steam cookies found" in r.message for r in caplog.records)


# --- how the scraper chooses ----------------------------------------------

def test_the_configured_value_is_used_when_the_lookup_is_off():
    config = {"session": {"login_secure": "FROM_CONFIG"}}
    with patch("src.web_scraper.browser_cookies") as lookup:
        assert _resolve_login_secure(config) == "FROM_CONFIG"
    lookup.assert_not_called()


def test_the_browser_wins_over_a_stale_config_value():
    """The regression: a stale config value shadowed a working browser session.

    Presence of a string is not evidence of authentication, and treating it as
    such meant every scrape ran anonymously while the store held a good cookie.
    """
    config = {"session": {"login_secure": "STALE", "read_firefox_cookies": True}}
    with patch("src.web_scraper.browser_cookies",
               return_value={"steamLoginSecure": "FROM_BROWSER"}):
        assert _resolve_login_secure(config) == "FROM_BROWSER"


def test_the_config_is_the_fallback_when_the_browser_has_nothing():
    config = {"session": {"login_secure": "FROM_CONFIG", "read_firefox_cookies": True}}
    with patch("src.web_scraper.browser_cookies", return_value={}):
        assert _resolve_login_secure(config) == "FROM_CONFIG"


def test_an_empty_config_without_the_flag_stays_empty():
    with patch("src.web_scraper.browser_cookies", return_value={"steamLoginSecure": "X"}) as lookup:
        assert _resolve_login_secure({"session": {}}) == ""
    lookup.assert_not_called()


def test_the_session_id_comes_from_the_same_read():
    """A sessionid from a different session than the credential is worse than none."""
    config = {"session": {"id": "CONFIG_SID", "read_firefox_cookies": True}}
    with patch("src.web_scraper.browser_cookies",
               return_value={"steamLoginSecure": "X", "sessionid": "BROWSER_SID"}):
        assert _csrf_token(config) == "BROWSER_SID"


def test_the_session_id_falls_back_to_the_config():
    with patch("src.web_scraper.browser_cookies", return_value={}):
        assert _csrf_token({"session": {"id": "CONFIG_SID", "read_firefox_cookies": True}}) == "CONFIG_SID"


@pytest.mark.parametrize("body,expected", [
    ('<div class="account_pulldown">me</div>', False),          # signed in: dropdown present
    ('<html>nothing here</html>', True),                        # anonymous
    ('<script>var g_steamID = false;</script>', True),          # explicitly signed out
    ('<script>var g_steamID = "76561198000000000";</script>', False),  # a real id is signed in
    ("", False),
])
def test_looks_signed_out(body, expected):
    from src.web_scraper import looks_signed_out
    assert looks_signed_out(body) is expected


def test_refresh_forces_a_re_read(tmp_path):
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    _make_store(profile / "cookies.sqlite", [("steamLoginSecure", "FIRST", ".steamcommunity.com")])
    assert steam_login_secure(profiles_root=tmp_path) == "FIRST"
    _make_store(profile / "cookies.sqlite", [("steamLoginSecure", "SECOND", ".steamcommunity.com")])
    assert steam_login_secure(profiles_root=tmp_path) == "FIRST", "cached until asked"
    assert steam_login_secure(refresh=True, profiles_root=tmp_path) == "SECOND"


# --- the cache and the expiry its own value carries -------------------------

def _login_cookie(expires_at=None, steamid="76561198000000000"):
    """A `steamLoginSecure` value whose token states an expiry, or states none.

    `%7C%7C` is the separator form the browser stores and the config writes; the
    token is a real three-segment shape because that is what the decoder reads.
    """
    claims = {} if expires_at is None else {"exp": expires_at}
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJub25lIn0.{payload}.c2ln"
    return f"{steamid}%7C%7C{token}"


def test_a_cached_cookie_past_its_own_expiry_is_read_again(tmp_path):
    """The cache must not outlive the credential it holds.

    A daemon runs for weeks while Steam reissues this cookie about daily, so a
    value whose token states that it has already died is evidence the profile
    may hold a newer one. The read has to happen on the next call, not wait for
    a gate-shaped failure to ask for it.
    """
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    dead = _login_cookie(expires_at=1000)
    _make_store(profile / "cookies.sqlite",
                [("steamLoginSecure", dead, ".steamcommunity.com")])
    assert steam_login_secure(profiles_root=tmp_path) == dead

    live = _login_cookie(expires_at=9_999_999_999)
    _make_store(profile / "cookies.sqlite",
                [("steamLoginSecure", live, ".steamcommunity.com")])
    assert steam_login_secure(profiles_root=tmp_path) == live, \
        "an expired cached value must be re-read, not served"


@pytest.mark.parametrize("value", [
    "76561198000000000%7C%7Cnot-a-jwt",   # not three segments at all
    _login_cookie(expires_at=None),        # a token that states no expiry
    "76561198000000000",                   # no separator: nothing to decode
])
def test_a_cookie_without_a_readable_expiry_is_served_from_cache(tmp_path, value):
    """`unknown` must not be read as `expired`: refusing to try is worse.

    The contract in `src/session_cookie.py` is that an unreadable expiry is
    `None`, never "no", and the cache must hold the same rule.
    """
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    _make_store(profile / "cookies.sqlite",
                [("steamLoginSecure", value, ".steamcommunity.com")])
    assert steam_login_secure(profiles_root=tmp_path) == value

    (profile / "cookies.sqlite").unlink()
    assert steam_login_secure(profiles_root=tmp_path) == value, \
        "an unreadable expiry must not force a re-read"


def test_a_live_cookie_is_served_from_cache_without_a_second_read(tmp_path):
    """The browser read is not free, so the ordinary case must stay cached."""
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    live = _login_cookie(expires_at=9_999_999_999)
    _make_store(profile / "cookies.sqlite",
                [("steamLoginSecure", live, ".steamcommunity.com")])
    assert steam_login_secure(profiles_root=tmp_path) == live

    (profile / "cookies.sqlite").unlink()
    assert steam_login_secure(profiles_root=tmp_path) == live, "no second read"


def test_an_empty_read_is_still_not_cached(tmp_path):
    """A missing credential is re-read on every call; only a value is cached.

    The asymmetry the entry names: an empty read was never pinned, and that
    property has to survive the expiry check.
    """
    profile = tmp_path / "abc.default-release"
    profile.mkdir()
    _make_store(profile / "cookies.sqlite", [])
    assert steam_login_secure(profiles_root=tmp_path) is None

    live = _login_cookie(expires_at=9_999_999_999)
    _make_store(profile / "cookies.sqlite",
                [("steamLoginSecure", live, ".steamcommunity.com")])
    assert steam_login_secure(profiles_root=tmp_path) == live


# --- telling a gate from a layout change ----------------------------------

def test_a_withheld_page_looks_gated():
    from src.web_scraper import looks_gated
    assert looks_gated('<title>Steam Community :: Error</title>') is True
    assert looks_gated('<div id="AgeCheck">verify</div>') is True


def test_an_item_page_never_looks_gated():
    """Every normal page carries a Sign In link, so markup must veto the markers."""
    from src.web_scraper import looks_gated
    body = '<a>Sign In</a><div class="workshopItemTitle">T</div>'
    assert looks_gated(body) is False


def test_an_empty_body_cannot_be_judged():
    from src.web_scraper import looks_gated
    assert looks_gated("") is False


# --- telling "no description" from "not the item page" ---------------------

@pytest.mark.parametrize("body,expected", [
    ('<div class="workshopItem">x</div>', True),                 # the item page, no description
    ('<div class="workshopItem"></div><div id="highlightContent">d</div>', False),
    ('<title>Steam Community :: Error</title>', False),          # never the item page
    ('<html><h1>Too many requests</h1></html>', False),          # a throttle the marker missed
    ("", False),
])
def test_item_page_without_description(body, expected):
    from src.web_scraper import looks_like_item_page_without_description
    assert looks_like_item_page_without_description(body) is expected


def test_a_description_marker_without_the_template_is_not_a_genuine_absence():
    """The template is the evidence the page is the item's; a stray description
    marker on a page that is not the item's must not clear the row."""
    from src.web_scraper import looks_like_item_page_without_description
    assert looks_like_item_page_without_description('<div id="highlightContent">d</div>') is False


# --- the worker's login-cookie refresh on a gated miss ---------------------

def _worker_with(refresh):
    from src.web_worker import WebScraperThread
    thread = WebScraperThread("test.db", "nope.lock", {}, None, refresh)
    return thread


def test_a_gated_miss_refreshes_the_cookie_and_makes_no_second_request():
    """The refresh is a local file read; the request itself is never repeated.

    A gated page and a changed layout are indistinguishable from the selector
    alone, and only the cookie can be refreshed for free, so that is all this
    does. The miss goes through `classify_scrape` and the queue retries it under
    the worker's own adaptive delay, which is the only spacing a request pays.
    """
    item = {"workshop_id": 1}
    miss = {"description": None, "body": "<title>Steam Community :: Error</title>"}
    refreshed = []
    with patch("src.web_worker.scrape_extended_details") as scrape:
        _worker_with(lambda: refreshed.append(1) or True)._refresh_login_cookie_if_gated_or_signed_out(item, miss)
    assert refreshed == [1], "the gated miss must refresh the login cookie"
    assert scrape.call_count == 0, "and must not re-scrape"


def test_an_unchanged_cookie_does_not_earn_a_request_either():
    """Neither the refresh's answer nor a broken page buys a second request."""
    item = {"workshop_id": 1}
    miss = {"description": None, "body": "<title>Steam Community :: Error</title>"}
    with patch("src.web_worker.scrape_extended_details") as scrape:
        _worker_with(lambda: False)._refresh_login_cookie_if_gated_or_signed_out(item, miss)
        assert scrape.call_count == 0


def test_a_normal_miss_refreshes_nothing_and_requests_nothing():
    item = {"workshop_id": 1}
    miss = {"description": None, "body": "<div class='account_pulldown'>me</div>"}
    refreshed = []
    with patch("src.web_worker.scrape_extended_details") as scrape:
        _worker_with(lambda: refreshed.append(1) or True)._refresh_login_cookie_if_gated_or_signed_out(item, miss)
    assert refreshed == [], "a signed-in page is not evidence of a stale cookie"
    assert scrape.call_count == 0


def test_no_configured_source_means_no_refresh_and_no_retry():
    item = {"workshop_id": 1}
    miss = {"description": None, "body": "<title>Steam Community :: Error</title>"}
    with patch("src.web_worker.scrape_extended_details") as scrape:
        _worker_with(None)._refresh_login_cookie_if_gated_or_signed_out(item, miss)
        assert scrape.call_count == 0
