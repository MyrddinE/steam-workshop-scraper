"""The debug web-download capture keeps everything while it is on.

It exists to answer what a *working* exchange looks like -- for the item page,
the subscriptions page and the server-side subscribe -- so the signed-in markup
and the subscribe path can be judged from real captures. It is deliberately
unbounded and keeps the body whole: it is a switch that is on for a session or
two, and thinning a sample before anyone has looked at it just means collecting
the evidence twice. The failure capture is the opposite case -- it runs for
weeks, so its caps stay.

It is also an instrument for a signed-in session, so nothing in it may carry a
credential value; ``elide_secrets`` is the barrier, and the leak test at the
bottom is what holds it.
"""

import json
import os
from unittest.mock import patch

import pytest

from src import capture

# Distinctive enough that a substring match cannot be a coincidence, and long
# enough to clear the whole-record scrub floor.
SECRET = "KNOWN-SECRET-VALUE-0123456789"
SESSION_SECRET = "SESSION-SECRET-VALUE-0123456789"
AUTH_SECRET = "AUTH-SECRET-VALUE-0123456789"


@pytest.fixture
def outbox(tmp_path):
    capture.configure(str(tmp_path))
    yield str(tmp_path)
    capture.configure(None)


@pytest.fixture(autouse=True)
def _state_file_in_tmp(tmp_path, monkeypatch):
    """Keep the worker's session record out of the checkout.

    A worker that scrapes a signed-out page records that fact beside its
    database, and these tests build workers on a literal "test.db". The path is
    redirected into tmp rather than each call site being changed, so the
    behaviour under test stays exactly as it is in production -- and the tests
    that assert the record still read it through `session_health.read`.
    """
    monkeypatch.setattr(
        "src.session_health.state_path_for",
        lambda db_path: str(tmp_path / (os.path.basename(str(db_path)) + ".state.yaml")),
    )


def _scrape(ok=True, body=None):
    return {
        "description": "text" if ok else None,
        "body": body if body is not None else "<html><body>plain</body></html>",
        "http_status": 200,
        "final_url": "https://steamcommunity.com/sharedfiles/filedetails/?id=1",
        "request": {
            "method": "GET",
            "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=1",
            "headers": {"User-Agent": "UA"},
            "cookies": {"steamLoginSecure": SECRET,
                        "browserid": "BROWSER-ID-0123456789"},
            "data": None,
        },
    }


def _record_item_page(workshop_id=1, ok=True, body=None):
    """Record one item-page pull through the generic entry point."""
    data = _scrape(ok=ok, body=body)
    return capture.record_web_download(
        capture.ITEM_PAGE_KIND, workshop_id, data["request"]["url"], data, ok=ok)


def _records(outbox):
    directory = os.path.join(outbox, "web_downloads")
    return [json.load(open(os.path.join(directory, f)))
            for f in sorted(os.listdir(directory)) if f.endswith(".json")]


def test_off_unless_configured(outbox):
    assert capture.web_download_capture_active() is False
    data = _scrape()
    assert capture.record_web_download(
        capture.ITEM_PAGE_KIND, 1, data["request"]["url"], data) is False
    assert not os.path.isdir(os.path.join(outbox, "web_downloads"))


def test_on_without_an_outbox_is_still_off():
    capture.configure(None, web_download_capture=True)
    assert capture.web_download_capture_active() is False


def test_every_pull_is_saved(tmp_path):
    """No cap: a debugging switch that stops early is worse than none."""
    capture.configure(str(tmp_path), web_download_capture=True)
    try:
        assert capture.web_download_capture_active() is True
        for i in range(25):
            assert _record_item_page(i, ok=bool(i % 2)) is True
        assert len(_records(str(tmp_path))) == 25
    finally:
        capture.configure(None)


def test_successes_are_recorded_as_well_as_misses(tmp_path):
    """The failure capture only ever holds misses, which is the whole gap."""
    capture.configure(str(tmp_path), web_download_capture=True)
    try:
        _record_item_page(1, ok=True)
        _record_item_page(2, ok=False)
        flags = sorted(r["ok"] for r in _records(str(tmp_path)))
        assert flags == [False, True]
    finally:
        capture.configure(None)


def test_the_body_is_kept_whole(tmp_path):
    """Scripts carry g_steamID; stripping them would drop the decisive signal."""
    capture.configure(str(tmp_path), web_download_capture=True)
    try:
        body = '<html><script>var g_steamID = "76561198000000000";</script>' \
               '<div class="account_pulldown">me</div></html>'
        _record_item_page(1, body=body)
        record = _records(str(tmp_path))[0]
        assert record["body_complete"] is True
        saved = open(os.path.join(str(tmp_path), "web_downloads",
                                  os.path.basename(record["body_file"]))).read()
        assert "g_steamID" in saved
        assert "account_pulldown" in saved
    finally:
        capture.configure(None)


def test_a_record_carries_both_sides_of_the_exchange(tmp_path):
    """The request and the answer, not just the page that came back."""
    capture.configure(str(tmp_path), web_download_capture=True)
    try:
        data = _scrape()
        data["response_headers"] = {"Content-Type": "text/html"}
        capture.record_web_download(capture.ITEM_PAGE_KIND, 7, data["request"]["url"],
                                    data, ok=True)
        record = _records(str(tmp_path))[0]
        assert record["kind"] == "item_page"
        assert record["request"]["method"] == "GET"
        assert record["request"]["url"] == data["request"]["url"]
        assert record["request"]["headers"] == {"User-Agent": "UA"}
        assert record["request"]["cookies"] == {
            "steamLoginSecure": "***", "browserid": "***"}
        assert record["http_status"] == 200
        assert record["final_url"] == data["final_url"]
        assert record["response_headers"] == {"Content-Type": "text/html"}
        assert record["body_file"] and record["body_bytes"] > 0
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
    capture.configure(str(tmp_path), web_download_capture=True)
    try:
        _record_item_page(1, body='<div class="account_pulldown">me</div>')
        markers = _records(str(tmp_path))[0]["auth_markers"]
        assert markers["account_pulldown"] is True
        assert markers["Sign In"] is False
    finally:
        capture.configure(None)


# --- the elider --------------------------------------------------------------
#
# The capture is the instrument for finishing a fix on the subscribe path, so it
# has to describe a credentialed request in enough detail to be useful -- which
# cookie names were sent, which form fields -- without ever writing the values.

def test_elide_secrets_keeps_names_and_removes_values():
    elided_cookies, elided_data, elided_headers, secrets = capture.elide_secrets(
        cookies={"steamLoginSecure": SECRET, "browserid": "BROWSER-ID-0123456789"},
        data={"id": "1", "sessionid": SESSION_SECRET},
        headers={"Cookie": f"steamLoginSecure={SECRET}",
                 "Authorization": f"Bearer {AUTH_SECRET}",
                 "Accept": "*/*"})

    assert elided_cookies == {"steamLoginSecure": "***", "browserid": "***"}
    assert elided_data == {"id": "1", "sessionid": "***"}
    assert elided_headers["Cookie"] == "***"
    assert elided_headers["Authorization"] == "***"
    assert elided_headers["Accept"] == "*/*", "an ordinary header is left alone"
    for value in (SECRET, SESSION_SECRET, AUTH_SECRET):
        assert value in secrets, "the value must be available for the whole-record scrub"


def test_elide_secrets_never_raises():
    """A diagnostic that can break the request it describes is not a diagnostic."""
    _cookies, _data, _headers, _secrets = capture.elide_secrets(
        cookies=object(), data=object(), headers=object())
    # The point is that the call returned instead of raising; whatever it could
    # describe safely, it did.
    assert True


def test_no_credential_value_reaches_the_outbox(tmp_path):
    """Put the secret in the jar, the form, a header and the response body.

    Then assert the value is in no file the recorder wrote -- record, body or
    manifest -- while the *name* survives, because which cookie was sent is the
    diagnostic point.
    """
    outbox = tmp_path / "outbox"
    capture.configure(str(outbox), web_download_capture=True)
    try:
        capture.record_web_download(
            capture.SUBSCRIBE_KIND, 1, "https://steamcommunity.com/subscribe",
            {
                "request": {
                    "method": "POST",
                    "url": "https://steamcommunity.com/subscribe?token=" + SECRET,
                    "headers": {"Cookie": f"steamLoginSecure={SECRET}",
                                "Authorization": f"Bearer {AUTH_SECRET}"},
                    "cookies": {"steamLoginSecure": SECRET,
                                "browserid": "BROWSER-ID-0123456789"},
                    "data": {"id": "1", "sessionid": SESSION_SECRET},
                },
                "http_status": 200,
                "final_url": "https://steamcommunity.com/done?sessionid=" + SESSION_SECRET,
                "response_headers": {
                    "Set-Cookie": f"steamLoginSecure={SECRET}; Path=/; HttpOnly"},
                "body": f"<html>echo {SECRET} {SESSION_SECRET} {AUTH_SECRET}</html>",
            })
    finally:
        capture.configure(None)

    written = [path for path in outbox.rglob("*") if path.is_file()]
    assert written, "the switch was on, so the capture must have been written"
    for path in written:
        text = path.read_text(encoding="utf-8", errors="replace")
        for value in (SECRET, SESSION_SECRET, AUTH_SECRET):
            assert value not in text, f"{value!r} leaked into {path.name}"

    records = _records(str(outbox))
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "subscribe"
    assert record["request"]["cookies"] == {
        "steamLoginSecure": "***", "browserid": "***"}
    assert set(record["request"]["cookies"]) == {"steamLoginSecure", "browserid"}
    assert record["request"]["data"]["sessionid"] == "***"
    assert record["request"]["data"]["id"] == "1"
    assert record["request"]["headers"]["Cookie"] == "***"
    assert record["request"]["headers"]["Authorization"] == "***"
    assert record["response_headers"]["Set-Cookie"] == "***"
    body = (outbox / record["body_file"]).read_text(encoding="utf-8")
    assert "echo" in body, "the body must still be the body"


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


def test_a_definitive_404_is_not_treated_as_a_gate():
    """A gone item cannot be fixed by a fresher cookie, so it costs no request."""
    from src.web_worker import WebScraperThread
    page = {"description": None, "http_status": 404,
            "body": "<title>Steam Community :: Error</title>"}
    called = []
    with patch("src.web_worker.scrape_extended_details") as scrape:
        WebScraperThread("test.db", "nope.lock", {}, None,
                         lambda: called.append(1) or True)._retry_if_gated(
                             {"workshop_id": 1}, "u", page)
    assert scrape.call_count == 0, "a gone item is not worth a retry"
    assert called == [], "and it is not a session problem to refresh for"


def test_a_throttled_item_is_not_decayed():
    """The item is fine; only the budget is spent.

    A throttle must not reach the per-item handling: it is not evidence against
    the item, so it must neither clear its queue flag nor be counted as an
    unattributable failure. It does move the *rate* -- a throttle is an
    unambiguous refusal and halves the request rate at once -- which is the
    change from the fixed pause it used to serve instead. The behavioural
    coverage is in tests/test_workers.py; this pins the classification and the
    branch shape.
    """
    from src.web_worker import ScrapeOutcome, classify_scrape

    throttled = {"description": None, "body": "<h1>too many requests</h1>"}
    assert classify_scrape(throttled) is ScrapeOutcome.RATE_LIMITED

    src = __import__("pathlib").Path("src/web_worker.py").read_text(encoding="utf-8")
    branch = src.index("elif outcome is ScrapeOutcome.RATE_LIMITED")
    following = src.index("elif outcome is ScrapeOutcome.ITEM_MISSING", branch)
    body = src[branch:following]
    assert "_record_web_failure" not in body, "a throttle is not an item failure"
    assert "_handle_unknown" not in body, "and it must not blame the item"
    assert "pacing.backoff" in body, "a throttle is a refusal: it slows the scraper"


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


# --- the login warning the worker raises -------------------------------------
#
# The reconcile is not the only place a dead cookie surfaces: a scrape that comes
# back signed out is the more common one, and the only one that fires while the
# queue is busy. The worker records the same fact through the same module, so the
# warning says the same thing whichever path found it.
#
# The predicate is `looks_signed_out`, which reads the absent signed-in markers.
# It is a heuristic and is documented as one: a withheld page that arrived
# without Steam's header (an error shell, an age check) reads the same way. That
# cost is accepted because the warning names the remedy and Recheck clears it
# from the token alone -- a false alarm is one click.

def test_a_signed_out_scrape_records_the_problem_for_the_ui():
    from src import session_health
    from src.web_worker import WebScraperThread
    miss = {"description": None, "body": "<html>no account dropdown here</html>"}

    with patch("src.web_worker.scrape_extended_details", return_value=miss):
        WebScraperThread("test.db", "nope.lock", {}, None,
                         lambda: False)._retry_if_gated({"workshop_id": 1}, "u", miss)

    problem = session_health.read("test.db")
    assert problem is not None
    assert "sign-in page" in problem["detail"]


def test_a_retry_that_works_clears_the_problem():
    from src import session_health
    from src.web_worker import WebScraperThread
    session_health.record_rejected("test.db", "the login cookie expired", now=1000)
    miss = {"description": None, "body": "<html>no account dropdown here</html>"}
    good = {"description": "found", "body": "<div class='account_pulldown'>me</div>"}

    with patch("src.web_worker.scrape_extended_details", return_value=good):
        WebScraperThread("test.db", "nope.lock", {}, None,
                         lambda: True)._retry_if_gated({"workshop_id": 1}, "u", miss)

    assert session_health.read("test.db") is None


def test_a_signed_in_page_leaves_a_healthy_session_alone():
    from src import session_health
    from src.web_worker import WebScraperThread
    miss = {"description": None, "body": "<div class='account_pulldown'>me</div>"}

    with patch("src.web_worker.scrape_extended_details", return_value=miss):
        WebScraperThread("test.db", "nope.lock", {}, None,
                         lambda: True)._retry_if_gated({"workshop_id": 1}, "u", miss)

    assert session_health.read("test.db") is None


def test_a_throttled_page_makes_no_claim_about_the_cookie():
    """A throttle is not evidence about the login, so it must neither raise the
    warning nor clear one that is already up."""
    from src import session_health
    from src.web_worker import WebScraperThread
    session_health.record_rejected("test.db", "the login cookie expired", now=1000)
    throttled = {"description": None, "body": "<h1>Too many requests</h1>"}

    WebScraperThread("test.db", "nope.lock", {}, None,
                     lambda: True)._retry_if_gated({"workshop_id": 1}, "u", throttled)

    assert session_health.read("test.db") == {
        "detail": "the login cookie expired", "detected_at": 1000}
