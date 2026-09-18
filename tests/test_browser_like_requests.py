"""The scraper's requests must look like a Firefox navigation, not a script.

Steam answered many requests with an HTTP 200 page reporting "too many
requests". Whether that is genuine throttling or bot deterrence is unproven, but
the request was measurably not what a browser sends — Chrome UA beside Firefox
cookies, `Accept: */*`, no fetch metadata, a fresh session per call — so making
it browser-faithful is right either way. These tests pin the browser shape: the
header set from the HAR capture, the profile's whole cookie set, one reused
session, and an `Accept-Encoding` that only offers codecs this stack can actually
decode.
"""

import builtins
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
import responses

import src.web_scraper as web_scraper
from src.web_scraper import BROWSER_HEADERS, USER_AGENT

DETAILS_URL = "https://steamcommunity.com/sharedfiles/filedetails/"
BROWSE_URL = "https://steamcommunity.com/workshop/browse"
ITEM_URL = DETAILS_URL + "?id=2872938263"

ITEM_HTML = """
<html><body>
  <div class="workshopItemDescription" id="highlightContent">Desc</div>
</body></html>
"""

# The ten steamcommunity.com cookies the HAR capture shows Firefox sending.
PROFILE_COOKIES = {
    "timezoneOffset": "-18000,0",
    "wants_mature_content_apps": "2694490",
    "recentlyVisitedAppHubs": "2694490%2C4000%2C431960",
    "browserid": "109916648136252788",
    "timezoneName": "America%2FChicago",
    "workshop_preferences_v2": "%7B%22bOptedIn%22%3Atrue%7D",
    "sessionid": "BROWSER_SID",
    "steamCountry": "US%7C7009a7521bb7e4d1538f0abd7887dbcd",
    "steamLoginSecure": "BROWSER_LOGIN",
    "app_impressions": "431960@2_100100_100101_100103",
}


@pytest.fixture(autouse=True)
def _no_web_delay(monkeypatch):
    """Keep the scraper's inter-request pacing out of the test runtime."""
    monkeypatch.setattr(web_scraper, "_WEB_DELAY", 0.0)


def _assert_browser_headers(prepared):
    for name, value in BROWSER_HEADERS.items():
        assert prepared.headers.get(name) == value, f"{name} was not the browser's"
    # `requests` defaults to this and a navigation never sends it; it is the
    # clearest single giveaway that a request is a script.
    assert prepared.headers.get("Accept") != "*/*"


# --- both request sites send the browser header set ------------------------

@responses.activate
def test_item_request_sends_the_browser_header_set():
    responses.add(responses.GET, DETAILS_URL, body=ITEM_HTML, status=200,
                  content_type="text/html")
    with patch("src.web_scraper.load_config", return_value={"session": {"id": "x"}}):
        details = web_scraper.scrape_extended_details(ITEM_URL)

    assert details is not None
    _assert_browser_headers(responses.calls[0].request)


@responses.activate
def test_browse_request_sends_the_browser_header_set():
    responses.add(responses.GET, BROWSE_URL, body="<html></html>", status=200,
                  content_type="text/html")
    with patch("src.web_scraper.load_config", return_value={"session": {"id": "x"}}):
        web_scraper.discover_items_by_date_html(294100, 0, 0)

    _assert_browser_headers(responses.calls[0].request)


def test_the_user_agent_is_firefox():
    """A Chrome UA beside Firefox cookies is itself a contradiction."""
    assert "Firefox/" in USER_AGENT
    assert "Chrome" not in USER_AGENT


# --- the cookie set matches the browser ------------------------------------

def test_the_whole_profile_cookie_set_is_sent_when_enabled(monkeypatch):
    monkeypatch.setattr(web_scraper, "browser_cookies",
                        lambda *args, **kwargs: dict(PROFILE_COOKIES))

    cookies = web_scraper._build_workshop_cookies({
        "session": {"read_firefox_cookies": True,
                    "id": "CONFIG_SID", "login_secure": "CONFIG_LOGIN"},
    })

    assert cookies == PROFILE_COOKIES


def test_the_profile_cookie_set_invents_nothing(monkeypatch):
    """Only what the profile has is sent; missing names are not backfilled."""
    profile = {"sessionid": "BROWSER_SID", "steamLoginSecure": "BROWSER_LOGIN"}
    monkeypatch.setattr(web_scraper, "browser_cookies",
                        lambda *args, **kwargs: dict(profile))

    cookies = web_scraper._build_workshop_cookies(
        {"session": {"read_firefox_cookies": True}})

    assert cookies == profile
    assert "workshop_preferences_v2" not in cookies


def test_only_the_config_pair_is_sent_when_the_setting_is_off(monkeypatch):
    consulted = []

    def forbidden(*args, **kwargs):
        consulted.append(True)
        return {"sessionid": "BROWSER_SID", "steamLoginSecure": "BROWSER_LOGIN"}

    monkeypatch.setattr(web_scraper, "browser_cookies", forbidden)

    cookies = web_scraper._build_workshop_cookies(
        {"session": {"id": "CONFIG_SID", "login_secure": "CONFIG_LOGIN"}})

    assert cookies["sessionid"] == "CONFIG_SID"
    assert cookies["steamLoginSecure"] == "CONFIG_LOGIN"
    assert cookies["workshop_preferences_v2"]
    assert consulted == [], "the profile must not be consulted when the setting is off"


@responses.activate
def test_the_request_carries_the_whole_profile_cookie_set(monkeypatch):
    monkeypatch.setattr(web_scraper, "browser_cookies",
                        lambda *args, **kwargs: dict(PROFILE_COOKIES))
    responses.add(responses.GET, DETAILS_URL, body=ITEM_HTML, status=200,
                  content_type="text/html")

    with patch("src.web_scraper.load_config",
               return_value={"session": {"read_firefox_cookies": True}}):
        web_scraper.scrape_extended_details(ITEM_URL)

    sent = responses.calls[0].request.headers.get("Cookie", "")
    for name in PROFILE_COOKIES:
        assert name in sent, f"{name} was not sent"


def test_the_scrape_reports_the_request_it_actually_sent(monkeypatch):
    """The capture records the values handed to the session, not a guess at them.

    A debug capture that re-derived the request would describe what the code
    *meant* to send; the instrument is only useful if it describes what was sent.
    """
    cookies = {"steamLoginSecure": "SENT-LOGIN", "sessionid": "SENT-SID"}
    monkeypatch.setattr(web_scraper, "_workshop_cookies_or_empty", lambda: dict(cookies))
    sent = {}

    response = MagicMock()
    response.status_code = 200
    response.url = ITEM_URL
    response.text = ITEM_HTML
    response.html.find.return_value = []

    class _Session:
        def get(self, url, **kwargs):
            sent["url"] = url
            sent.update(kwargs)
            return response

    monkeypatch.setattr(web_scraper, "_get_session", lambda: _Session())

    details = web_scraper.scrape_extended_details(ITEM_URL)

    assert details["request"] == {
        "method": "GET",
        "url": ITEM_URL,
        "headers": BROWSER_HEADERS,
        "cookies": cookies,
        "data": None,
    }
    assert sent["cookies"] is details["request"]["cookies"], \
        "the recorded jar must be the object the session received"
    assert sent["headers"] is BROWSER_HEADERS


# --- one session, reused ----------------------------------------------------

def test_one_session_is_reused_across_calls():
    assert web_scraper._get_session() is web_scraper._get_session()


def test_both_request_sites_share_one_session(monkeypatch):
    mock_session_class = MagicMock()
    monkeypatch.setattr(web_scraper, "HTMLSession", mock_session_class)
    monkeypatch.setattr(web_scraper, "_session", None)
    monkeypatch.setattr(web_scraper, "_session_built_from", None)

    response = MagicMock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.text = "<html></html>"
    response.url = ITEM_URL
    response.html.find.return_value = []
    mock_session_class.return_value.get.return_value = response

    with patch("src.web_scraper.load_config", return_value={"session": {"id": "x"}}):
        web_scraper.scrape_extended_details(ITEM_URL)
        web_scraper.discover_items_by_date_html(1, 0, 0)

    assert mock_session_class.call_count == 1, "a new session was built per call"
    assert mock_session_class.return_value.get.call_count == 2


# --- Accept-Encoding only offers decodable codecs ---------------------------
#
# The gate asks urllib3 what it can decompress rather than probing for the
# decoder modules. A module can be installed and still be useless to the urllib3
# in use -- urllib3 1.x has no zstd decoder at all -- and advertising a codec
# that is never decompressed leaves compressed bytes in `response.text`.


def _encodings(monkeypatch, urllib3_reports: str) -> list[str]:
    monkeypatch.setattr(web_scraper, "_urllib3_accept_encoding", lambda: urllib3_reports)
    return [e.strip() for e in web_scraper._accept_encoding().split(",")]


def test_accept_encoding_always_offers_gzip_and_deflate(monkeypatch):
    encodings = _encodings(monkeypatch, "gzip,deflate")
    assert "gzip" in encodings
    assert "deflate" in encodings


def test_accept_encoding_never_offers_a_codec_urllib3_cannot_decode(monkeypatch):
    """Regression: an importable `zstandard` is not evidence urllib3 can use it."""
    monkeypatch.setitem(sys.modules, "zstandard", types.ModuleType("zstandard"))
    assert "zstd" not in _encodings(monkeypatch, "gzip,deflate")


def test_accept_encoding_carries_brotli_when_urllib3_reports_it(monkeypatch):
    encodings = _encodings(monkeypatch, "gzip,deflate,br")
    assert "br" in encodings
    assert "zstd" not in encodings


def test_accept_encoding_passes_through_whatever_urllib3_supports(monkeypatch):
    """Nothing is filtered out; if urllib3 can do it, we may ask for it."""
    assert _encodings(monkeypatch, "gzip, deflate, br, zstd") == [
        "gzip", "deflate", "br", "zstd",
    ]


def test_the_real_urllib3_gate_names_only_codecs_this_stack_decodes():
    """The live probe, not a stubbed one: the answer must be usable as-is."""
    from urllib3.util.request import ACCEPT_ENCODING

    supported = {e.strip() for e in ACCEPT_ENCODING.split(",") if e.strip()}
    supported.add("br")  # urllib3 1.x decodes brotli without listing it
    for encoding in web_scraper._accept_encoding().split(","):
        assert encoding.strip() in supported, (
            f"{encoding!r} is advertised but urllib3 cannot decode it here"
        )


# --- the User-Agent tracks the profile --------------------------------------

def test_the_user_agent_uses_the_profile_firefox_version(monkeypatch):
    monkeypatch.setattr(web_scraper, "firefox_version", lambda *a, **k: "155.0")
    assert web_scraper._firefox_user_agent() == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) "
        "Gecko/20100101 Firefox/155.0"
    )


def test_the_user_agent_falls_back_without_a_readable_profile(monkeypatch):
    monkeypatch.setattr(web_scraper, "firefox_version", lambda *a, **k: None)
    assert web_scraper._firefox_user_agent() == web_scraper.FIREFOX_USER_AGENT_FALLBACK
    assert "Firefox/" in web_scraper.FIREFOX_USER_AGENT_FALLBACK
