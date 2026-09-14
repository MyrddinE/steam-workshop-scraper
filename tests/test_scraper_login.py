"""The description scrape must carry the login cookie, not just the CSRF token.

`steamLoginSecure` is what authenticates a session; `sessionid` on its own does
nothing. The scrape sent neither, and the cookie builder — used only by the
browse path — omitted the login cookie anyway. Anything Steam serves only to
signed-in users therefore came back as an error page or an age check.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.config import login_secure_value
from src.web_scraper import _build_workshop_cookies, scrape_extended_details

ITEM_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id=1"


@pytest.fixture(autouse=True)
def _restore_webserver_globals():
    """Leave the webserver module as we found it.

    `init_webserver` mutates module globals and `_sessionid` is process state.
    Without restoring them, a test here that pushes a sessionid makes a later
    "no session configured" test see one — which is exactly how this file first
    broke `test_webserver.py::test_api_subscribe_no_session` when the two ran in
    the same session.
    """
    from src import webserver

    saved = (webserver._db_path, webserver._config, webserver._images_dir,
             webserver._config_path, webserver._sessionid)
    yield
    (webserver._db_path, webserver._config, webserver._images_dir,
     webserver._config_path, webserver._sessionid) = saved


# --- normalising the config key -------------------------------------------

def test_login_secure_raw_string():
    assert login_secure_value({"session": {"login_secure": "76561198%7C%7Ctok"}}) == \
        "76561198%7C%7Ctok"


def test_login_secure_list_form_is_joined():
    assert login_secure_value({"session": {"login_secure": ["76561198", "tok", "x"]}}) == \
        "76561198%7C%7Ctok%7C%7Cx"


@pytest.mark.parametrize("config", [{}, {"session": {}}, {"session": {"login_secure": ""}}])
def test_login_secure_absent_is_empty(config):
    assert login_secure_value(config) == ""


# --- what the request carries ---------------------------------------------

def test_cookies_include_the_login_cookie_when_configured():
    cookies = _build_workshop_cookies({"session": {"id": "abc", "login_secure": "LOGIN"}})
    assert cookies["steamLoginSecure"] == "LOGIN"
    assert cookies["sessionid"] == "abc"
    assert cookies["workshop_preferences_v2"]          # mature-content opt-in


def test_cookies_omit_the_login_cookie_when_unset():
    """An anonymous configuration must send exactly what it did before."""
    cookies = _build_workshop_cookies({"session": {"id": "abc"}})
    assert "steamLoginSecure" not in cookies
    assert cookies["sessionid"] == "abc"


def _fake_response():
    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.text = "<html><body></body></html>"
    response.url = ITEM_URL
    return response


def test_scrape_sends_the_login_cookie():
    with patch("src.web_scraper.HTMLSession") as session_cls, \
         patch("src.web_scraper.load_config",
               return_value={"session": {"id": "abc", "login_secure": "LOGIN"}}):
        session_cls.return_value.get.return_value = _fake_response()
        scrape_extended_details(ITEM_URL)
        _, kwargs = session_cls.return_value.get.call_args
        assert kwargs["cookies"]["steamLoginSecure"] == "LOGIN"
        assert kwargs["cookies"]["sessionid"] == "abc"


def test_scrape_survives_a_missing_config():
    """An unreadable config means anonymous, not a failed scrape."""
    with patch("src.web_scraper.HTMLSession") as session_cls, \
         patch("src.web_scraper.load_config", side_effect=FileNotFoundError("nope")):
        session_cls.return_value.get.return_value = _fake_response()
        scrape_extended_details(ITEM_URL)
        _, kwargs = session_cls.return_value.get.call_args
        assert kwargs["cookies"] == {}


# --- a refreshed cookie must reach the daemon ------------------------------

def test_sessionid_endpoint_persists_the_login_cookie(tmp_path):
    """The daemon is a separate process; without persisting, it never sees it."""
    from src.webserver import app, init_webserver

    config = {"session": {"id": "abc"}}
    init_webserver(":memory:", config, config_path=str(tmp_path / "config.yaml"))

    with patch("src.webserver.save_config") as save:
        with app.test_client() as client:
            response = client.post("/api/sessionid",
                                   json={"sessionid": "s1", "login_secure": "L1"})

    assert response.status_code == 200
    assert config["session"]["login_secure"] == "L1"
    save.assert_called_once()


def test_sessionid_endpoint_does_not_persist_when_the_cookie_is_absent(tmp_path):
    from src.webserver import app, init_webserver

    init_webserver(":memory:", {"session": {"id": "abc"}},
                   config_path=str(tmp_path / "config.yaml"))

    with patch("src.webserver.save_config") as save:
        with app.test_client() as client:
            response = client.post("/api/sessionid", json={"sessionid": "s1"})

    assert response.status_code == 200
    save.assert_not_called()


# --- the User-Agent trap ---------------------------------------------------

def test_the_scrape_does_not_use_the_requests_html_default_ua():
    """Steam serves the anonymous shell to it, cookie or no cookie.

    requests_html ships a macOS Safari string from around 2017; with it, the same
    URL and a valid login cookie returned a ~325 KB generic page with no item
    markup. Measured against a HAR of a signed-in load.
    """
    from requests_html import HTMLSession
    from src.web_scraper import USER_AGENT
    assert HTMLSession().headers.get("User-Agent") != USER_AGENT
    assert "Chrome" in USER_AGENT or "Firefox" in USER_AGENT


def test_both_scrape_paths_send_the_browser_ua():
    with patch("src.web_scraper.HTMLSession") as session_cls, \
         patch("src.web_scraper.load_config", return_value={"session": {"id": "x"}}):
        session_cls.return_value.get.return_value = _fake_response()
        scrape_extended_details(ITEM_URL)
        _, kwargs = session_cls.return_value.get.call_args
        assert kwargs["headers"]["User-Agent"] == __import__(
            "src.web_scraper", fromlist=["USER_AGENT"]).USER_AGENT
