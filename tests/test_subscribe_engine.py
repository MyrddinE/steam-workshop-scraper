"""The browser-free subscribe engine: parse, decide, verify, record.

Every fact this module relies on was measured against production (see the
brief's confirmed section): the button is server-rendered, ``{"success": 1}`` is
returned for an already-subscribed item too, and the page's ``toggled`` state is
the only authority. These tests therefore never touch the network: the item-page
fetch and the shared session are patched at
``src.web_scraper.scrape_extended_details`` and ``src.web_scraper._get_session``,
which are the same seams ``tests/test_webserver.py`` uses for the route.

The parser and the already-subscribed no-op are exercised against the real
signed-in captures in ``/root/.dsh/live/scrapes`` (both states are present
there); the tests skip if that directory is not on this machine, so the suite
still runs where the captures were never synced.
"""

import base64
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from src import capture, pacing, session_health, subscribe_engine as engine, web_scraper
from src import web_worker
from src.database import (
    get_connection,
    initialize_database,
    insert_or_update_item,
    toggle_subscription_queue_status,
)

LIVE_CAPTURES = Path("/root/.dsh/live/scrapes")

# Fixed expiry on the right side of any plausible run date; the same shape
# `tests/test_webserver.py` builds.
_FUTURE_EXPIRY = 4_102_444_800  # 2100-01-01


def _login_cookie(expires_at):
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode()).decode().rstrip("=")
    return f"76561198000000000%7C%7CeyJhbGciOiJub25lIn0.{payload}.c2ln"


# --- fixtures from the real captures ----------------------------------------

def _capture_bodies():
    if not LIVE_CAPTURES.is_dir():
        return []
    return sorted(LIVE_CAPTURES.glob("*.body"))


def _capture_with_state(state):
    for path in _capture_bodies():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if engine.parse_button_state(text) == state:
            return text
    pytest.skip(f"no local capture reads as {state!r}")


# --- the parser --------------------------------------------------------------

NOT_TOGGLED = (
    '<a id="SubscribeItemBtn" class="btn_green_white_innerfade btn_border_2px '
    'btn_medium ">Subscribe</a>'
)
TOGGLED = (
    '<a id="SubscribeItemBtn" class="btn_green_white_innerfade btn_border_2px '
    'btn_medium toggled">Subscribed</a>'
)


def _synthetic_page(session_id=None, *, authenticated=True, toggled=False):
    """A hand-built item page -- never a captured one.

    The local mirror's captures carry real `g_sessionID` values and real login
    cookies, so no capture is ever committed as a test fixture. This page carries
    the account marker when `authenticated` and a **fake** `g_sessionID` only
    when one is asked for.
    """
    classes = 'class="btn toggled"' if toggled else 'class="btn"'
    account = '<div id="account_pulldown">signed in</div>' if authenticated else ""
    token = f'<script>var g_sessionID = "{session_id}";</script>' if session_id else ""
    return (f'<html><body>{account}'
            f'<a id="SubscribeItemBtn" {classes}>Subscribe</a>{token}</body></html>')


def test_the_parser_reads_the_two_rendered_states():
    assert engine.parse_button_state(NOT_TOGGLED) == engine.BUTTON_NOT_SUBSCRIBED
    assert engine.parse_button_state(TOGGLED) == engine.BUTTON_SUBSCRIBED


def test_the_parser_ignores_toggled_outside_the_button():
    """`toggled` is read from the one element's own class list, nowhere else."""
    page = f'<div class="toggled">{NOT_TOGGLED}</div>'
    assert engine.parse_button_state(page) == engine.BUTTON_NOT_SUBSCRIBED
    assert engine.parse_button_state('<div class="toggled"></div>') == engine.BUTTON_UNKNOWN


def test_the_parser_does_not_match_the_id_inside_script_text():
    """A literal in a script is not the rendered element."""
    page = "<script>var s = 'a < b id=\"SubscribeItemBtn\"';</script>"
    assert engine.parse_button_state(page) == engine.BUTTON_UNKNOWN


def test_the_parser_says_unknown_when_the_button_is_absent():
    assert engine.parse_button_state("<html><body>error</body></html>") == engine.BUTTON_UNKNOWN
    assert engine.parse_button_state("") == engine.BUTTON_UNKNOWN
    assert engine.parse_button_state(None) == engine.BUTTON_UNKNOWN


def test_the_session_id_parser_reads_a_quoted_g_session_id():
    """The parser for issue 38, against synthetic pages only."""
    assert engine.parse_session_id(
        "<script>var g_sessionID = 'PAGE_TOKEN';</script>") == "PAGE_TOKEN"
    assert engine.parse_session_id(
        '<script>var g_sessionID="X";</script>') == "X"
    assert engine.parse_session_id(
        '<script>g_sessionID = "SPACED";</script>') == "SPACED"


def test_the_session_id_parser_says_empty_when_the_page_has_no_g_session_id():
    """An error, throttle or anonymous page carries no token; never guess one."""
    for page in ("<html><body>Steam error page</body></html>",
                 "<html>You have made too many requests</html>",
                 "<script>var g_sessionID = '';</script>",
                 "", None):
        assert engine.parse_session_id(page) == ""


def test_the_token_fingerprint_is_stable_and_different_for_another_token():
    """The diagnostic property: equal tokens compare equal, others do not."""
    token = "A-FAKE-SESSION-TOKEN"
    fingerprint = engine.token_fingerprint(token)
    assert fingerprint
    assert fingerprint != token
    assert len(fingerprint) == 12
    assert all(c in "0123456789abcdef" for c in fingerprint)
    assert engine.token_fingerprint(token) == fingerprint, "stable for one token"
    assert engine.token_fingerprint(token + "x") != fingerprint, "different for another"


def test_the_parser_reads_both_states_from_the_local_signed_in_captures():
    bodies = _capture_bodies()
    if not bodies:
        pytest.skip("the local signed-in captures are not on this machine")
    states = {}
    for path in bodies:
        text = path.read_text(encoding="utf-8", errors="replace")
        if "SubscribeItemBtn" not in text:
            continue
        states[path.name] = engine.parse_button_state(text)
    assert states, "the captures should include signed-in item pages"
    assert engine.BUTTON_SUBSCRIBED in states.values()
    assert engine.BUTTON_NOT_SUBSCRIBED in states.values()


# --- the engine's seams ------------------------------------------------------


class _Fetcher:
    """A `scrape_extended_details` stand-in replaying bodies in order."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = []

    def __call__(self, url, keep_body=False):
        index = min(len(self.calls), len(self.bodies) - 1)
        self.calls.append(url)
        return {
            "description": "d", "tags": [], "body": self.bodies[index],
            "http_status": 200, "final_url": url,
            "request": {"method": "GET", "url": url},
        }


class _Session:
    """A shared-session stand-in recording the subscribe POST."""

    def __init__(self, payload=None, text=None, json_ok=True, status_code=200):
        self.payload = payload
        self.text = text
        self.json_ok = json_ok
        self.status_code = status_code
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        resp = MagicMock()
        resp.status_code = self.status_code
        resp.url = url
        resp.headers = {}
        resp.text = self.text if self.text is not None else json.dumps(self.payload)
        if self.json_ok:
            resp.json.return_value = self.payload
        else:
            resp.json.side_effect = ValueError("not json")
        return resp


@pytest.fixture
def engine_env(tmp_path, monkeypatch):
    """A queued item, healthy cookies, and both network seams replaced."""
    db_path = str(tmp_path / "engine.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 7, "title": "T", "status": 200,
                                    "consumer_appid": 294100})
    toggle_subscription_queue_status(db_path, 7)
    config = {"database": {"path": db_path}, "session": {"id": "TOK"}}
    monkeypatch.setattr(
        web_scraper, "_build_workshop_cookies",
        lambda config: {"sessionid": "TOK",
                        "steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)})
    # Capture is off unless a test turns it on, so no test writes to an outbox
    # another test configured.
    monkeypatch.setattr(capture, "_web_download_capture", False)
    # The interval is real (a page read waits it); tests must not actually
    # sleep, so the wait itself is stubbed. The pacing tests below replace this
    # with a recorder.
    monkeypatch.setattr(pacing, "wait", lambda seconds, keep_running=None: True)
    return db_path, config


def _row(db_path, wid=7):
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM workshop_items WHERE workshop_id=?", (wid,)).fetchone()
    conn.close()
    return dict(row)


def _queued(db_path, wid=7):
    return _row(db_path, wid)["is_queued_for_subscription"] == 1


# --- the already-subscribed no-op -------------------------------------------

def test_an_already_subscribed_item_sends_no_request(engine_env, monkeypatch):
    """`toggled` short-circuits before any POST -- the confirmed semantics."""
    db_path, config = engine_env
    body = _capture_with_state(engine.BUTTON_SUBSCRIBED)
    monkeypatch.setattr(web_scraper, "scrape_extended_details", _Fetcher([body]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.ALREADY
    assert outcome.subscribed is True
    assert session.calls == [], "an already-subscribed item must not be POSTed"
    assert _row(db_path, 7)["own_subscribed"] == 0, "no request, so nothing to record"


def test_the_already_subscribed_no_op_reads_a_local_capture(engine_env, monkeypatch):
    """The same no-op, driven by the real signed-in page rather than a string."""
    db_path, config = engine_env
    body = _capture_with_state(engine.BUTTON_SUBSCRIBED)
    fetcher = _Fetcher([body])
    monkeypatch.setattr(web_scraper, "scrape_extended_details", fetcher)
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.ALREADY
    assert len(fetcher.calls) == 1, "the no-op path reads the page exactly once"
    assert session.calls == []


# --- the happy path ----------------------------------------------------------

def test_a_not_subscribed_item_is_subscribed_and_verified(engine_env, monkeypatch):
    db_path, config = engine_env
    not_toggled = _capture_with_state(engine.BUTTON_NOT_SUBSCRIBED)
    monkeypatch.setattr(web_scraper, "scrape_extended_details",
                        _Fetcher([not_toggled, TOGGLED]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.SUBSCRIBED
    assert outcome.subscribed is True
    assert len(session.calls) == 1
    row = _row(db_path, 7)
    assert row["own_subscribed"] == 1
    assert row["own_first_subscribed_at"] is not None
    assert row["is_queued_for_subscription"] == 0, "success clears the queue"


def test_a_failed_post_is_reported_and_leaves_the_item_queued(engine_env, monkeypatch):
    db_path, config = engine_env
    monkeypatch.setattr(web_scraper, "scrape_extended_details",
                        _Fetcher([NOT_TOGGLED]))
    session = _Session(payload={"success": 25})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.FAILED
    assert outcome.subscribed is False
    assert _queued(db_path, 7) is True


def test_a_sources_disagreeing_is_reported_and_nothing_is_recorded(engine_env, monkeypatch):
    """Steam says success but the page still says not subscribed: no guess."""
    db_path, config = engine_env
    monkeypatch.setattr(web_scraper, "scrape_extended_details",
                        _Fetcher([NOT_TOGGLED, NOT_TOGGLED]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.DISAGREEMENT
    assert outcome.subscribed is False
    assert _row(db_path, 7)["own_subscribed"] == 0, "the page is the authority"
    assert _queued(db_path, 7) is True


def test_the_confirmation_step_can_be_retired_without_touching_the_click(engine_env,
                                                                        monkeypatch):
    """The switch is the whole removal: one pre-read, one POST, no third read."""
    db_path, config = engine_env
    monkeypatch.setattr(engine, "VERIFY_AFTER_SUBSCRIBE", False)
    fetcher = _Fetcher([NOT_TOGGLED])
    monkeypatch.setattr(web_scraper, "scrape_extended_details", fetcher)
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.SUBSCRIBED
    assert len(fetcher.calls) == 1, "the confirmation read is gone"
    assert len(session.calls) == 1, "the pre-read and the click are untouched"
    assert _row(db_path, 7)["own_subscribed"] == 1
    assert _queued(db_path, 7) is False


# --- the POST's CSRF token (issue 38) ---------------------------------------

def test_the_post_uses_the_pages_token_when_the_configured_one_differs(engine_env,
                                                                      monkeypatch):
    """The regression for issue 38: the page's own `g_sessionID` is posted.

    The configured/pushed token belongs to whatever session pushed it, and a
    `sessionid` off the Firefox profile is impossible -- it is a session cookie.
    The page the attempt just read carries the token that authenticates *it*,
    and the form field and the cookie must both carry that value.
    """
    db_path, config = engine_env
    page = _synthetic_page("PAGE_SESSION_TOKEN", authenticated=True)
    after = _synthetic_page("PAGE_SESSION_TOKEN", authenticated=True, toggled=True)
    monkeypatch.setattr(web_scraper, "scrape_extended_details", _Fetcher([page, after]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.SUBSCRIBED
    call = session.calls[0]
    assert call["data"]["sessionid"] == "PAGE_SESSION_TOKEN"
    assert call["data"]["sessionid"] != "TOK", "the configured token lost"
    assert call["cookies"]["sessionid"] == "PAGE_SESSION_TOKEN", \
        "the form field and the cookie must agree"


def test_the_configured_token_is_the_fallback_when_the_page_has_none(engine_env,
                                                                    monkeypatch):
    """A page with no `g_sessionID` still posts the cookie set's token."""
    db_path, config = engine_env
    page = _synthetic_page(None, authenticated=True)
    after = _synthetic_page(None, authenticated=True, toggled=True)
    monkeypatch.setattr(web_scraper, "scrape_extended_details", _Fetcher([page, after]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    engine.subscribe_item(7, config=config, db_path=db_path)

    assert session.calls[0]["data"]["sessionid"] == "TOK"


@pytest.mark.parametrize("payload,status", [({"success": 2}, 200),
                                            ({"success": 15}, 200),
                                            ({"success": 2}, 401)])
def test_a_refused_token_with_an_authenticated_page_is_not_a_session_problem(
        engine_env, monkeypatch, payload, status):
    """The refusal is about the token when this attempt's page read was signed in.

    The credential just authenticated a page, so recording a session problem
    would tell the operator to sign in again for nothing -- the misleading half
    of issue 38. A 401 and Steam's `success: 2`/`15` are the same refusal.
    """
    db_path, config = engine_env
    page = _synthetic_page("PAGE_SESSION_TOKEN", authenticated=True)
    monkeypatch.setattr(web_scraper, "scrape_extended_details", _Fetcher([page]))
    session = _Session(payload=payload, status_code=status)
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.TOKEN_REFUSED
    assert outcome.queued is True
    assert outcome.steam_success == payload["success"]
    assert session_health.read(db_path) is None, "the login was proven good"
    assert _queued(db_path, 7) is True


def test_an_http_401_with_an_unreadable_body_is_still_a_token_refusal(engine_env,
                                                                     monkeypatch):
    """A 401 whose body is not JSON is refused, not reported as a failure."""
    db_path, config = engine_env
    page = _synthetic_page("PAGE_SESSION_TOKEN", authenticated=True)
    monkeypatch.setattr(web_scraper, "scrape_extended_details", _Fetcher([page]))
    session = _Session(text="<html>unauthorized</html>", json_ok=False, status_code=401)
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.TOKEN_REFUSED
    assert session_health.read(db_path) is None


def test_a_refused_token_with_an_anonymous_page_still_records_a_session_problem(
        engine_env, monkeypatch):
    """Nothing authenticated, so the refusal is the session's problem as before."""
    db_path, config = engine_env
    # No account marker and no page token: the cookie set's token is posted and
    # the refusal has nothing authenticated to contradict it.
    page = _synthetic_page(None, authenticated=False)
    monkeypatch.setattr(web_scraper, "scrape_extended_details", _Fetcher([page]))
    session = _Session(payload={"success": 2})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.SESSION_PROBLEM
    recorded = session_health.read(db_path)
    assert recorded and recorded["detail"] == engine.SUBSCRIBE_SESSION_REJECTED_DETAIL


def test_the_post_log_carries_a_fingerprint_and_never_the_token(engine_env,
                                                                monkeypatch, caplog):
    """One line tells the next occurrence apart without ever printing a token."""
    db_path, config = engine_env
    token = "PAGE_SESSION_TOKEN"
    page = _synthetic_page(token, authenticated=True)
    after = _synthetic_page(token, authenticated=True, toggled=True)
    monkeypatch.setattr(web_scraper, "scrape_extended_details", _Fetcher([page, after]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    with caplog.at_level(logging.INFO):
        engine.subscribe_item(7, config=config, db_path=db_path)

    line = [r.getMessage() for r in caplog.records if "POSTing to Steam" in r.getMessage()]
    assert line, "the POST must be logged"
    assert token not in caplog.text, "the token itself must never be logged"
    assert engine.token_fingerprint(token) in line[0]
    assert "sessionid_fp=" in line[0]


# --- the refusals ------------------------------------------------------------

def test_a_missing_button_refuses(engine_env, monkeypatch):
    """No button means cannot tell; it is never read as 'not subscribed'."""
    db_path, config = engine_env
    monkeypatch.setattr(web_scraper, "scrape_extended_details",
                        _Fetcher(["<html><body>Steam error page</body></html>"]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.REFUSED
    assert outcome.button_before == engine.BUTTON_UNKNOWN
    assert session.calls == [], "cannot tell means no request"
    assert _queued(db_path, 7) is True


def test_a_throttle_page_leaves_the_item_queued(engine_env, monkeypatch):
    db_path, config = engine_env
    monkeypatch.setattr(web_scraper, "scrape_extended_details",
                        _Fetcher(["<html>You have made too many requests</html>"]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.THROTTLED
    assert outcome.queued is True
    assert session.calls == []
    assert _queued(db_path, 7) is True


def test_a_throttle_answer_to_the_post_leaves_the_item_queued(engine_env, monkeypatch):
    """The non-JSON throttle shell is not a failure: the item was never tried."""
    db_path, config = engine_env
    monkeypatch.setattr(web_scraper, "scrape_extended_details",
                        _Fetcher([NOT_TOGGLED]))
    session = _Session(text="<html>Too Many Requests</html>", json_ok=False)
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.THROTTLED
    assert outcome.queued is True
    assert _queued(db_path, 7) is True


@pytest.mark.parametrize("success", [2, 15])
def test_an_expiry_answer_records_a_session_problem(engine_env, monkeypatch, success):
    """`NOT_TOGGLED` carries no account marker, so the page read was anonymous.

    The refusal is therefore the session's problem, exactly as before the token
    fix; the authenticated branch is covered separately above.
    """
    db_path, config = engine_env
    monkeypatch.setattr(web_scraper, "scrape_extended_details",
                        _Fetcher([NOT_TOGGLED]))
    session = _Session(payload={"success": success})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.SESSION_PROBLEM
    recorded = session_health.read(db_path)
    assert recorded, "the expiry answer must leave a problem for the banner"
    assert recorded["detail"] == engine.SUBSCRIBE_SESSION_REJECTED_DETAIL
    assert _row(db_path, 7)["own_subscribed"] == 0
    assert _queued(db_path, 7) is True


def test_an_unusable_credential_refuses_before_the_request(engine_env, monkeypatch):
    db_path, config = engine_env
    monkeypatch.setattr(web_scraper, "_build_workshop_cookies",
                        lambda config: {"steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)})
    monkeypatch.setattr(web_scraper, "scrape_extended_details",
                        _Fetcher([NOT_TOGGLED]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config={"session": {}}, db_path=db_path)

    assert outcome.status == engine.REFUSED
    assert outcome.message == engine.NO_SESSION_MESSAGE
    assert session.calls == []


# --- capture -----------------------------------------------------------------

def test_the_engine_captures_the_item_page_and_the_subscribe(engine_env,
                                                             monkeypatch, tmp_path):
    db_path, config = engine_env
    outbox = tmp_path / "outbox"
    capture.configure(str(outbox), web_download_capture=True)
    try:
        monkeypatch.setattr(web_scraper, "scrape_extended_details",
                            _Fetcher([NOT_TOGGLED, TOGGLED]))
        session = _Session(payload={"success": 1})
        monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

        engine.subscribe_item(7, config=config, db_path=db_path)

        directory = Path(capture.web_downloads_dir(str(outbox)))
        records = [json.loads(p.read_text(encoding="utf-8"))
                   for p in directory.glob("*.json")]
    finally:
        capture.configure(None)

    kinds = [r["kind"] for r in records]
    assert kinds.count(capture.ITEM_PAGE_KIND) == 2, "both page reads are captured"
    assert kinds.count(capture.SUBSCRIBE_KIND) == 1, "the POST is captured"
    for record in records:
        if record["kind"] == capture.SUBSCRIBE_KIND:
            assert record["request"]["cookies"]["steamLoginSecure"] == capture.REDACTED
            assert record["request"]["data"]["sessionid"] in (capture.REDACTED, "***")
        else:
            assert record["request"]["method"] == "GET"
            assert "filedetails/?id=7" in record["request"]["url"]


# --- the pass and the pause --------------------------------------------------

def test_the_pause_is_taken_and_released_even_when_the_engine_raises(tmp_path, monkeypatch):
    """A raising engine must not leave the daemon paused for good."""
    lock = tmp_path / ".pauselock"
    seen = {}

    def explode(*args, **kwargs):
        seen["locked_during"] = lock.exists()
        raise RuntimeError("engine blew up")

    monkeypatch.setattr(engine, "subscribe_item", explode)

    with pytest.raises(RuntimeError):
        engine.run_subscription_pass(
            [{"workshop_id": 1, "title": "T"}],
            config={}, db_path=str(tmp_path / "x.db"), pause_lock_file=str(lock))

    assert seen["locked_during"] is True, "the pass must hold the pause"
    assert not lock.exists(), "the pause must be released in a finally"


def test_the_pass_releases_the_pause_and_reports_each_result(tmp_path, monkeypatch):
    lock = tmp_path / ".pauselock"
    outcomes = []
    monkeypatch.setattr(
        engine, "subscribe_item",
        lambda wid, **kwargs: engine.SubscribeOutcome(wid, engine.ALREADY,
                                                      subscribed=True))

    result = engine.run_subscription_pass(
        [{"workshop_id": 1}, {"workshop_id": 2}],
        config={}, db_path=str(tmp_path / "x.db"), pause_lock_file=str(lock),
        on_result=outcomes.append)

    assert [o.workshop_id for o in result] == [1, 2]
    assert [o.workshop_id for o in outcomes] == [1, 2]
    assert not lock.exists()


# --- the shared web interval -------------------------------------------------

def test_the_interval_is_read_fresh_from_the_config_and_floored():
    assert web_worker.configured_web_delay({"daemon": {"web_delay_seconds": 9.5}}) == 9.5
    assert web_worker.configured_web_delay({"web_delay_seconds": 7.0}) == 7.0
    assert web_worker.configured_web_delay({}) == web_worker.WEB_DELAY_DEFAULT
    assert web_worker.configured_web_delay(
        {"daemon": {"web_delay_seconds": 0.1}}) == web_worker.WEB_DELAY_FLOOR
    assert web_worker.configured_web_delay(
        {"daemon": {"web_delay_seconds": "nonsense"}}) == web_worker.WEB_DELAY_DEFAULT


def test_every_page_read_waits_the_interval_and_the_post_does_not(engine_env, monkeypatch):
    """Reads are page loads, so they wait; the click is a XHR, so it does not."""
    db_path, config = engine_env
    config["daemon"] = {"web_delay_seconds": 12.0}
    events = []
    monkeypatch.setattr(
        pacing, "wait",
        lambda seconds, keep_running=None: events.append(("wait", seconds)) or True)

    bodies = [NOT_TOGGLED, TOGGLED]
    seen = {"n": 0}

    def fetch(url, keep_body=False):
        events.append(("get", url))
        body = bodies[min(seen["n"], len(bodies) - 1)]
        seen["n"] += 1
        return {"description": "d", "tags": [], "body": body, "http_status": 200,
                "final_url": url, "request": {"method": "GET", "url": url}}

    monkeypatch.setattr(web_scraper, "scrape_extended_details", fetch)

    session = _Session(payload={"success": 1})
    post = session.post

    def recording_post(url, **kwargs):
        events.append(("post", url))
        return post(url, **kwargs)

    session.post = recording_post
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path)

    assert outcome.status == engine.SUBSCRIBED
    assert [event[0] for event in events] == ["wait", "get", "post", "wait", "get"]
    # The clean read decays the delay by a hair (elapsed is microseconds), so
    # the second wait is the first one to within floating point.
    assert events[0][1] == 12.0
    assert events[3][1] == pytest.approx(12.0)


def test_a_wall_grows_and_persists_the_shared_interval(engine_env, monkeypatch, tmp_path):
    """A throttle backs the shared delay off and writes it where the daemon reads."""
    db_path, config = engine_env
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"daemon": {"web_delay_seconds": 6.0}}),
                           encoding="utf-8")
    config["daemon"] = {"web_delay_seconds": 6.0}
    monkeypatch.setattr(pacing, "wait", lambda seconds, keep_running=None: True)
    monkeypatch.setattr(web_scraper, "scrape_extended_details",
                        _Fetcher(["<html>You have made too many requests</html>"]))
    session = _Session(payload={"success": 1})
    monkeypatch.setattr(web_scraper, "_get_session", lambda: session)

    outcome = engine.subscribe_item(7, config=config, db_path=db_path,
                                    config_path=str(config_path))

    assert outcome.status == engine.THROTTLED
    assert config["daemon"]["web_delay_seconds"] == 12.0
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["daemon"][
        "web_delay_seconds"] == 12.0


def test_the_pass_spaces_every_item(tmp_path, monkeypatch):
    """One interval spans the pass, so the second item waits behind the first."""
    waits = []
    monkeypatch.setattr(
        pacing, "wait",
        lambda seconds, keep_running=None: waits.append(seconds) or True)
    monkeypatch.setattr(web_scraper, "scrape_extended_details", _Fetcher([TOGGLED]))

    outcomes = engine.run_subscription_pass(
        [{"workshop_id": 1}, {"workshop_id": 2}],
        config={"daemon": {"web_delay_seconds": 8.0}},
        db_path=str(tmp_path / "x.db"),
        pause_lock_file=str(tmp_path / ".pauselock"))

    assert [o.status for o in outcomes] == [engine.ALREADY, engine.ALREADY]
    assert waits[0] == 8.0
    assert waits[1] == pytest.approx(8.0)
