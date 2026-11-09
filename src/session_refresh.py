"""Refresh and verify the Steam login, shared by both front ends.

The Recheck button used to read the browser's cookie store and clear the
warning when the value it found had not expired locally. That cannot see the
three ways a login actually fails: Steam revoked the session while the token
still looked live, the cookie came from another Firefox profile, or an HTTP
proxy is refusing every request before Steam ever sees it. The warning then
disappeared while every task kept failing -- the operator's "it says it worked"
report.

This module makes the verification the authority. It reads the freshest cookie
the configured source offers, asks Steam for the signed-in page with it, and
only then records the login as accepted and persists the cookie. A proxy 407 is
reported as a proxy problem, not as a bad login, so the UI asks for the
credential that can actually fix it.

Kept out of :mod:`src.webserver` so the TUI can use the same rule without
importing Flask, and out of :mod:`src.web_scraper` so the request shaping stays
there and the policy stays here.
"""

from __future__ import annotations

import logging

from src import network, session_health, web_scraper
from src.config import login_secure_value, save_config
from src.firefox_cookies import steam_login_secure


def _candidate(config: dict) -> tuple[str, str]:
    """``(cookie, source)`` from the browser first, then the config.

    The Recheck button is the browser's own refresh path: it must read the
    profile even when ``session.read_firefox_cookies`` is off, because that
    option controls what the *scraper* sends, not what the operator can pull
    from the browser. The configured value is the fallback, and the verification
    below decides whether either is good.
    """
    fresh = steam_login_secure(refresh=True)
    if fresh:
        return fresh, "browser"
    return login_secure_value(config), "config"


def _record_problem(db_path: str, detail: str) -> None:
    """Record a failed check without making a repeated failure look new.

    The page and the TUI key their "open the sign-in page once" logic to
    ``detected_at``. A recheck that fails the same way every five seconds must
    not move that timestamp, or each poll opens another sign-in tab.
    """
    existing = session_health.read(db_path)
    now = None
    if existing and existing.get("detail") == detail:
        now = existing.get("detected_at")
    session_health.record_rejected(db_path, detail, now=now)


def refresh_login(config: dict, config_path: str, db_path: str) -> dict:
    """Read, verify, and persist the Steam login; return the UI-facing result.

    The result is ``{"ok": True, "problem": False}`` only after Steam served a
    page that proves a session. Every failure leaves the warning in place and
    says why. A proxy 407 additionally records the proxy problem for the
    credential prompt.

    The cookie is saved only after verification: a stale value must not replace
    a good one just because a button was pressed.
    """
    configured_before = login_secure_value(config)
    cookie, source = _candidate(config)
    if not cookie:
        detail = "no login cookie is available"
        _record_problem(db_path, detail)
        return {"ok": False, "problem": True, "detail": detail}

    reason = session_health.evaluate_login(cookie)
    if reason:
        _record_problem(db_path, reason)
        return {"ok": False, "problem": True, "detail": reason}

    # Verify with the cookie that was just read. In browser mode the refresh
    # above has updated the profile cache; putting it into the in-memory config
    # as well is what makes a config-source `_build_workshop_cookies` send the
    # same value when the browser source is off.
    config.setdefault("session", {})["login_secure"] = cookie
    result = web_scraper.verify_login(config)
    if result.get("proxy_auth_required"):
        network.record_auth_required(
            db_path, result.get("proxy_url"), result.get("detail"))
        return {
            "ok": False,
            "problem": True,
            "proxy_auth_required": True,
            "proxy_url": result.get("proxy_url") or "",
            "detail": result.get("detail") or network.AUTH_REQUIRED_DETAIL,
        }
    if not result.get("ok"):
        detail = result.get("detail") or "Steam did not accept the login cookie"
        # Do not leave a rejected cookie as this process's live value: the next
        # subscribe would present it even though Steam just refused it. The
        # proxy branch above deliberately keeps the fresh value, because there
        # the login was not judged at all.
        if configured_before:
            config.setdefault("session", {})["login_secure"] = configured_before
        else:
            config.get("session", {}).pop("login_secure", None)
        _record_problem(db_path, detail)
        return {"ok": False, "problem": True, "detail": detail}

    if cookie != configured_before:
        try:
            save_config(config_path, {"session": {"login_secure": cookie}})
        except Exception as exc:
            detail = f"a fresh login cookie was found but could not be saved ({exc})"
            _record_problem(db_path, detail)
            logging.warning("[Session] %s", detail)
            return {"ok": False, "problem": True, "detail": detail,
                    "save_failed": True}

    session_health.record_accepted(db_path)
    network.clear_auth_required(db_path)
    logging.info("[Session] login verified with Steam from the %s source", source)
    return {"ok": True, "problem": False}


def save_proxy_credentials(config_path: str, url: str, username: str,
                           password: str, config: dict | None = None,
                           db_path: str | None = None) -> dict:
    """Persist proxy credentials and clear the recorded 407 problem."""
    section = network.save_credentials(
        config_path, url, username, password, config=config)
    network.clear_auth_required(db_path or "")
    return section
