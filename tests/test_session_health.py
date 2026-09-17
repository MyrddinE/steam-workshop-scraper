"""The record of a dead Steam login, and the routes that surface it.

The state module exists because the fact is discovered by a worker thread, in
another process from the web UI that has to show it. These tests pin the three
things that make that work: the write/read/clear contract, the rule for judging
a cookie without asking Steam, and the HTTP shape the warning banner reads.

The rule most carefully protected is the direction of the judgement: an
unreadable cookie is `unknown`, never `expired`, because a route that refuses to
clear the warning on a value it cannot parse would leave the operator staring at
a stale warning with nothing wrong.
"""

import base64
import json
import logging
import os
import re

import pytest

from src import session_cookie, session_health
from src.daemon_state import state_path_for
from src.database import initialize_database


def _cookie(expires_at, steamid="76561198000000000"):
    """A `steamLoginSecure` value whose token states an expiry."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode()).decode().rstrip("=")
    return f"{steamid}%7C%7CeyJhbGciOiJub25lIn0.{payload}.c2ln"


# --- recording and clearing the fact ----------------------------------------

def test_a_recorded_problem_is_read_back_with_its_reason_and_time(tmp_path):
    db_path = str(tmp_path / "s.db")
    initialize_database(db_path)

    assert session_health.record_rejected(db_path, "the cookie expired", now=1000) is True

    assert session_health.read(db_path) == {
        "detail": "the cookie expired", "detected_at": 1000,
    }


def test_the_record_lives_beside_the_database(tmp_path):
    db_path = str(tmp_path / "s.db")
    initialize_database(db_path)

    session_health.record_rejected(db_path, "the cookie expired", now=1000)

    assert state_path_for(db_path) == os.path.join(str(tmp_path), ".daemon_state.yaml")
    assert os.path.exists(state_path_for(db_path))


def test_a_healthy_session_records_nothing(tmp_path):
    db_path = str(tmp_path / "s.db")
    initialize_database(db_path)

    assert session_health.read(db_path) is None
    assert not os.path.exists(state_path_for(db_path)), \
        "clearing a problem that was never recorded must not create the file"


def test_clearing_removes_the_record(tmp_path):
    db_path = str(tmp_path / "s.db")
    initialize_database(db_path)
    session_health.record_rejected(db_path, "the cookie expired", now=1000)

    assert session_health.record_accepted(db_path) is True

    assert session_health.read(db_path) is None


def test_a_later_problem_replaces_the_earlier_one(tmp_path):
    """The newest reason is the true one: the older may have been fixed."""
    db_path = str(tmp_path / "s.db")
    initialize_database(db_path)

    session_health.record_rejected(db_path, "expired", now=1000)
    session_health.record_rejected(db_path, "revoked", now=2000)

    assert session_health.read(db_path) == {"detail": "revoked", "detected_at": 2000}


def test_a_section_without_a_sentence_reads_as_no_problem(tmp_path):
    """An unexplained warning is worse than none, so it is not reported."""
    db_path = str(tmp_path / "s.db")
    initialize_database(db_path)
    state_path = state_path_for(db_path)
    with open(state_path, "w", encoding="utf-8") as handle:
        handle.write("session:\n  detected_at: 1000\n")

    assert session_health.read(db_path) is None


def test_a_section_of_the_wrong_shape_reads_as_no_problem(tmp_path):
    db_path = str(tmp_path / "s.db")
    initialize_database(db_path)
    with open(state_path_for(db_path), "w", encoding="utf-8") as handle:
        handle.write("session: rejected\n")

    assert session_health.read(db_path) is None


def test_an_unreadable_timestamp_is_reported_as_unknown(tmp_path):
    db_path = str(tmp_path / "s.db")
    initialize_database(db_path)
    with open(state_path_for(db_path), "w", encoding="utf-8") as handle:
        handle.write("session:\n  detail: expired\n  detected_at: yesterday\n")

    assert session_health.read(db_path) == {"detail": "expired", "detected_at": None}


def test_a_state_file_that_cannot_be_written_is_not_an_error(tmp_path, caplog):
    """Best-effort, like every other user of the state file: a lost warning must
    never stop the work that produced it."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    db_path = str(blocker / "s.db")

    with caplog.at_level(logging.WARNING):
        assert session_health.record_rejected(db_path, "expired", now=1000) is False

    assert "Could not write daemon state file" in caplog.text
    assert session_health.read(db_path) is None


# --- judging a cookie without asking Steam -----------------------------------

def test_no_value_is_reported_as_no_cookie():
    assert session_health.evaluate_login(None) == "no login cookie is available"
    assert session_health.evaluate_login("") == "no login cookie is available"


def test_an_expired_cookie_says_when_it_expired():
    reason = session_health.evaluate_login(_cookie(expires_at=1000), now=1000 + 7200)

    assert reason.startswith("the saved login cookie expired 2h ago")


def test_a_live_cookie_is_not_a_problem():
    assert session_health.evaluate_login(_cookie(expires_at=9000), now=1000) is None


def test_an_unreadable_expiry_is_not_a_problem():
    """`unknown` must not be read as `expired`: refusing to try strands a
    working cookie, and Steam's answer is what settles it either way."""
    assert session_health.evaluate_login("76561198000000000||opaque", now=1000) is None


def test_the_judgement_matches_the_parser_it_delegates_to():
    """Two modules, one rule: the expiry comparison is not written twice."""
    value = _cookie(expires_at=1000)
    assert session_health.evaluate_login(value, now=999) is None
    assert session_health.evaluate_login(value, now=1000) is not None
    assert session_cookie.parse(value).expires_at == 1000


# --- the routes the banner reads --------------------------------------------

@pytest.fixture
def web_client(tmp_path):
    from src.webserver import app, init_webserver
    db_path = str(tmp_path / "test_session.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path},
              "session": {"login_secure": _cookie(expires_at=1000)}}
    init_webserver(db_path, config, config_path=str(tmp_path / "config.yaml"))
    return app.test_client(), db_path, config


def test_the_session_route_reports_a_healthy_login(web_client):
    client, _, _ = web_client

    body = client.get('/api/session').get_json()

    assert body["problem"] is False
    assert body["detail"] is None
    assert body["login_url"] == session_health.LOGIN_URL


def test_the_session_route_reports_the_recorded_problem(web_client):
    client, db_path, _ = web_client
    session_health.record_rejected(db_path, "the saved login cookie expired 14h ago",
                                  now=1000)

    body = client.get('/api/session').get_json()

    assert body["problem"] is True
    assert body["detail"] == "the saved login cookie expired 14h ago"
    assert body["detected_at"] == 1000
    assert body["login_url"].startswith("https://steamcommunity.com/")


def test_recheck_clears_the_warning_when_the_browser_has_a_fresh_cookie(
        web_client, monkeypatch):
    """The whole point of the button: sign in, click, and the warning goes."""
    client, db_path, config = web_client
    session_health.record_rejected(db_path, "the saved login cookie expired", now=1000)
    monkeypatch.setattr("src.webserver.steam_login_secure", lambda refresh=False: _cookie(9_999_999_999))
    monkeypatch.setattr("src.webserver.save_config", lambda path, cfg: None)

    body = client.post('/api/session/recheck').get_json()

    assert body == {"ok": True, "problem": False}
    assert session_health.read(db_path) is None
    assert client.get('/api/session').get_json()["problem"] is False


def test_recheck_saves_the_fresh_cookie_for_the_daemon(web_client, monkeypatch):
    """The daemon is a separate process and re-reads config.yaml, so the value
    has to reach the file or the sign-in changes nothing."""
    client, _, config = web_client
    saved = {}
    monkeypatch.setattr("src.webserver.steam_login_secure", lambda refresh=False: _cookie(9_999_999_999))
    monkeypatch.setattr("src.webserver.save_config",
                        lambda path, cfg: saved.update({"path": path, "cfg": cfg}))

    client.post('/api/session/recheck')

    assert saved["path"].endswith("config.yaml")
    assert saved["cfg"]["session"]["login_secure"] == _cookie(9_999_999_999)


def test_recheck_writes_nothing_when_the_cookie_has_not_moved(web_client, monkeypatch):
    """A rewrite per click would serialise YAML for no change, for a value that
    is polled and pushed on a timer elsewhere in this server."""
    client, db_path, config = web_client
    live = _cookie(9_999_999_999)
    config["session"]["login_secure"] = live
    called = {"n": 0}
    monkeypatch.setattr("src.webserver.steam_login_secure", lambda refresh=False: live)
    monkeypatch.setattr("src.webserver.save_config",
                        lambda path, cfg: called.update(n=called["n"] + 1))

    body = client.post('/api/session/recheck').get_json()

    assert body["ok"] is True
    assert called["n"] == 0


def test_recheck_reports_a_browser_that_still_has_nothing_fresh(web_client, monkeypatch):
    client, db_path, config = web_client
    config["session"]["login_secure"] = _cookie(1000)
    monkeypatch.setattr("src.webserver.steam_login_secure", lambda refresh=False: None)

    body = client.post('/api/session/recheck').get_json()

    assert body["ok"] is False and body["problem"] is True
    assert body["detail"].startswith("the saved login cookie expired")
    assert session_health.read(db_path)["detail"] == body["detail"]


def test_recheck_says_so_when_there_is_no_cookie_at_all(web_client, monkeypatch):
    client, _, config = web_client
    config["session"]["login_secure"] = ""
    monkeypatch.setattr("src.webserver.steam_login_secure", lambda refresh=False: None)

    body = client.post('/api/session/recheck').get_json()

    assert body == {"ok": False, "problem": True,
                    "detail": "no login cookie is available"}


def test_recheck_reports_a_cookie_it_could_not_save(web_client, monkeypatch):
    """The warning must survive the failure, and say what failed."""
    client, db_path, _ = web_client
    monkeypatch.setattr("src.webserver.steam_login_secure", lambda refresh=False: _cookie(9_999_999_999))

    def explode(path, cfg):
        raise OSError("read-only file system")

    monkeypatch.setattr("src.webserver.save_config", explode)

    resp = client.post('/api/session/recheck')

    assert resp.status_code == 500
    assert resp.get_json()["problem"] is True
    assert "could not be saved" in session_health.read(db_path)["detail"]


# --- the banner in the page --------------------------------------------------

def test_the_page_carries_the_warning_scaffold(web_client):
    client, _, _ = web_client

    html = client.get('/').get_data(as_text=True)

    assert 'id="session-warning"' in html
    assert 'id="session-warning-text"' in html
    assert 'id="session-warning-login"' in html
    assert 'id="session-warning-recheck"' in html


def test_the_banner_is_hidden_until_a_problem_is_reported():
    """It is markup in the page, so it must start hidden and be shown by the
    poll -- otherwise every page load flashes a warning that is not there."""
    with open("templates/index.html", encoding="utf-8") as handle:
        html = handle.read()

    rule = re.search(r"#session-warning\s*\{([^}]*)\}", html)
    assert rule and "display: none" in rule.group(1)
    assert '/api/session' in html
    assert '/api/session/recheck' in html
    assert '_refreshSessionWarning' in html
