"""The shared read-verify-persist rule behind Recheck and the TUI.

The point of the module is the order: read the freshest cookie, ask Steam, and
only then persist or clear the warning. These tests pin each branch without
touching the network.
"""

import base64
import json

import pytest

from src import network, session_health, session_refresh


def _cookie(expires_at):
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode()).decode().rstrip("=")
    return f"76561198000000000%7C%7CeyJhbGciOiJub25lIn0.{payload}.c2ln"


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("database:\n  path: workshop.db\n", encoding="utf-8")
    return str(path)


def test_browser_cookie_is_verified_and_saved(config_path, tmp_path, monkeypatch):
    db_path = str(tmp_path / "workshop.db")
    fresh = _cookie(9_999_999_999)
    config = {"session": {"login_secure": _cookie(1000)}}
    saved = {}
    monkeypatch.setattr(session_refresh, "steam_login_secure",
                        lambda refresh=False: fresh)
    monkeypatch.setattr(session_refresh.web_scraper, "verify_login",
                        lambda config=None: {"ok": True})
    monkeypatch.setattr(session_refresh, "save_config",
                        lambda path, cfg: saved.update(cfg))

    result = session_refresh.refresh_login(config, config_path, db_path)

    assert result == {"ok": True, "problem": False}
    assert saved == {"session": {"login_secure": fresh}}
    assert session_health.read(db_path) is None


def test_a_rejected_login_keeps_the_warning_and_saves_nothing(
        config_path, tmp_path, monkeypatch):
    db_path = str(tmp_path / "workshop.db")
    config = {"session": {"login_secure": _cookie(9_999_999_999)}}
    saved = {}
    monkeypatch.setattr(session_refresh, "steam_login_secure",
                        lambda refresh=False: None)
    monkeypatch.setattr(session_refresh.web_scraper, "verify_login",
                        lambda config=None: {
                            "ok": False,
                            "detail": "Steam answered with its sign-in page"})
    monkeypatch.setattr(session_refresh, "save_config",
                        lambda path, cfg: saved.update(cfg))

    result = session_refresh.refresh_login(config, config_path, db_path)

    assert result["ok"] is False
    assert "sign-in page" in result["detail"]
    assert saved == {}
    assert "sign-in page" in session_health.read(db_path)["detail"]


def test_a_proxy_407_is_recorded_as_a_proxy_problem(
        config_path, tmp_path, monkeypatch):
    db_path = str(tmp_path / "workshop.db")
    config = {"session": {"login_secure": _cookie(9_999_999_999)}}
    monkeypatch.setattr(session_refresh, "steam_login_secure",
                        lambda refresh=False: None)
    monkeypatch.setattr(session_refresh.web_scraper, "verify_login",
                        lambda config=None: {
                            "ok": False,
                            "proxy_auth_required": True,
                            "proxy_url": "http://proxy.example:8080",
                            "detail": network.AUTH_REQUIRED_DETAIL})

    result = session_refresh.refresh_login(config, config_path, db_path)

    assert result["ok"] is False and result["proxy_auth_required"] is True
    assert network.read_auth_required(db_path)["url"] == "http://proxy.example:8080"
    assert session_health.read(db_path) is None, "a 407 is not a bad login"


def test_no_cookie_at_all_is_a_problem(config_path, tmp_path, monkeypatch):
    db_path = str(tmp_path / "workshop.db")
    monkeypatch.setattr(session_refresh, "steam_login_secure",
                        lambda refresh=False: None)

    result = session_refresh.refresh_login({}, config_path, db_path)

    assert result == {"ok": False, "problem": True,
                      "detail": "no login cookie is available"}
    assert session_health.read(db_path)["detail"] == "no login cookie is available"


def test_the_configured_cookie_is_the_fallback_when_the_browser_is_empty(
        config_path, tmp_path, monkeypatch):
    db_path = str(tmp_path / "workshop.db")
    live = _cookie(9_999_999_999)
    config = {"session": {"login_secure": live}}
    monkeypatch.setattr(session_refresh, "steam_login_secure",
                        lambda refresh=False: None)
    monkeypatch.setattr(session_refresh.web_scraper, "verify_login",
                        lambda config=None: {"ok": True})

    result = session_refresh.refresh_login(config, config_path, db_path)

    assert result["ok"] is True
    assert config["session"]["login_secure"] == live


def test_a_save_failure_is_reported_with_its_own_flag(
        config_path, tmp_path, monkeypatch):
    db_path = str(tmp_path / "workshop.db")
    config = {"session": {"login_secure": _cookie(1000)}}
    monkeypatch.setattr(session_refresh, "steam_login_secure",
                        lambda refresh=False: _cookie(9_999_999_999))
    monkeypatch.setattr(session_refresh.web_scraper, "verify_login",
                        lambda config=None: {"ok": True})

    def explode(path, cfg):
        raise OSError("read-only file system")

    monkeypatch.setattr(session_refresh, "save_config", explode)

    result = session_refresh.refresh_login(config, config_path, db_path)

    assert result["ok"] is False and result["save_failed"] is True
    assert "could not be saved" in result["detail"]
