"""The debug scrape capture keeps everything while it is on.

It exists to answer what a *working* page looks like, so the signed-in markup can
be identified. It is deliberately unbounded and keeps the body whole: it is a
switch that is on for a session or two, and thinning a sample before anyone has
looked at it just means collecting the evidence twice. The failure capture is the
opposite case — it runs for weeks, so its caps stay.
"""

import json
import os
from unittest.mock import patch

import pytest

from src import capture


@pytest.fixture
def outbox(tmp_path):
    capture.configure(str(tmp_path))
    yield str(tmp_path)
    capture.configure(None)


def _scrape(ok=True, body=None):
    return {
        "description": "text" if ok else None,
        "body": body if body is not None else "<html><body>plain</body></html>",
        "http_status": 200,
        "final_url": "https://steamcommunity.com/sharedfiles/filedetails/?id=1",
    }


def _records(outbox):
    directory = os.path.join(outbox, "scrapes")
    return [json.load(open(os.path.join(directory, f)))
            for f in sorted(os.listdir(directory)) if f.endswith(".json")]


def test_off_unless_configured(outbox):
    assert capture.web_scrape_capture_active() is False
    assert capture.record_web_scrape(1, "u", _scrape()) is False
    assert not os.path.isdir(os.path.join(outbox, "scrapes"))


def test_on_without_an_outbox_is_still_off():
    capture.configure(None, web_scrape_capture=True)
    assert capture.web_scrape_capture_active() is False


def test_every_scrape_is_saved(tmp_path):
    """No cap: a debugging switch that stops early is worse than none."""
    capture.configure(str(tmp_path), web_scrape_capture=True)
    try:
        assert capture.web_scrape_capture_active() is True
        for i in range(25):
            assert capture.record_web_scrape(i, "u", _scrape(ok=bool(i % 2))) is True
        assert len(_records(str(tmp_path))) == 25
    finally:
        capture.configure(None)


def test_successes_are_recorded_as_well_as_misses(tmp_path):
    """The failure capture only ever holds misses, which is the whole gap."""
    capture.configure(str(tmp_path), web_scrape_capture=True)
    try:
        capture.record_web_scrape(1, "u", _scrape(ok=True))
        capture.record_web_scrape(2, "u", _scrape(ok=False))
        flags = sorted(r["scraped_ok"] for r in _records(str(tmp_path)))
        assert flags == [False, True]
    finally:
        capture.configure(None)


def test_the_body_is_kept_whole(tmp_path):
    """Scripts carry g_steamID; stripping them would drop the decisive signal."""
    capture.configure(str(tmp_path), web_scrape_capture=True)
    try:
        body = '<html><script>var g_steamID = "76561198000000000";</script>' \
               '<div class="account_pulldown">me</div></html>'
        capture.record_web_scrape(1, "u", _scrape(body=body))
        record = _records(str(tmp_path))[0]
        assert record["body_complete"] is True
        saved = open(os.path.join(str(tmp_path), "scrapes",
                                  os.path.basename(record["body_file"]))).read()
        assert "g_steamID" in saved
        assert "account_pulldown" in saved
    finally:
        capture.configure(None)


@pytest.mark.parametrize("value,expected", [
    ('var g_steamID = "76561198000000000";', "76561198000000000"),
    ("var g_steamID = false;", "false"),
    ("<html>no variable here</html>", None),
])
def test_the_steam_id_is_extracted(value, expected):
    assert capture.steam_id_from(value) == expected


def test_markers_are_recorded_not_interpreted(tmp_path):
    capture.configure(str(tmp_path), web_scrape_capture=True)
    try:
        capture.record_web_scrape(1, "u", _scrape(
            body='<div class="account_pulldown">me</div>'))
        markers = _records(str(tmp_path))[0]["auth_markers"]
        assert markers["account_pulldown"] is True
        assert markers["Sign In"] is False
    finally:
        capture.configure(None)


# --- the throttle page ------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    ("<h1>You have made too many requests</h1>", True),
    ("<p>Too Many Requests</p>", True),
    ("<div class='workshopItemDescription' id='highlightContent'>x</div>", False),
    ("", False),
])
def test_looks_rate_limited(body, expected):
    from src.web_scraper import looks_rate_limited
    assert looks_rate_limited(body) is expected


def test_a_throttled_page_is_not_mistaken_for_a_stale_cookie():
    """It also lacks the signed-in markers, so the order of the checks matters."""
    from src.web_worker import WebScraperThread
    item = {"workshop_id": 1}
    throttled = {"description": None, "body": "<h1>Too many requests</h1>"}
    called = []
    with patch("src.web_worker.scrape_extended_details") as scrape:
        WebScraperThread("test.db", "nope.lock", {}, None,
                         lambda: called.append(1) or True)._retry_if_gated(item, "u", throttled)
        assert scrape.call_count == 0, "must not retry against an empty budget"
        assert called == [1], "but the cookie is still re-read: a local read costs no budget"


def test_a_throttled_item_is_not_decayed():
    """The item is fine; only the budget is spent."""
    src = __import__("pathlib").Path("src/web_worker.py").read_text(encoding="utf-8")
    throttle = src.index("looks_rate_limited(scrape_data.get(\"body\") or \"\")")
    decay = src.index("self._handle_selector_miss(item, url, scrape_data)", throttle)
    assert "continue" in src[throttle:decay], "the throttle branch must skip the decay"


def test_a_throttled_page_does_not_shadow_the_auth_check():
    """They overlap by construction, so both must be evaluated.

    A throttle page is not the item page, so it lacks the signed-in markers too.
    Reading that as "signed out" would refresh and retry; skipping the refresh
    leaves the next request carrying an older cookie than it needs to.
    """
    from src.web_worker import WebScraperThread
    calls = []
    throttled = {"description": None, "body": "<h1>Too many requests</h1>"}
    with patch("src.web_worker.scrape_extended_details") as scrape:
        WebScraperThread("test.db", "nope.lock", {}, None,
                         lambda: calls.append("refresh") or True)._retry_if_gated(
                             {"workshop_id": 1}, "u", throttled)
    assert calls == ["refresh"], "the auth reaction still fires"
    assert scrape.call_count == 0, "the network retry does not"


def test_a_signed_out_page_without_throttling_still_retries():
    """The other direction: no throttle, so the retry is allowed."""
    from src.web_worker import WebScraperThread
    page = {"description": None, "body": "<html>no account dropdown here</html>"}
    with patch("src.web_worker.scrape_extended_details",
               return_value={"description": "found"}) as scrape:
        out = WebScraperThread("test.db", "nope.lock", {}, None,
                               lambda: True)._retry_if_gated({"workshop_id": 1}, "u", page)
    assert scrape.call_count == 1
    assert out["description"] == "found"
