"""The bridge version is declared twice, in two files, by hand.

`templates/index.html` tells the browser which userscript version it expects, and
the userscript declares its own. Nothing linked the two literals, so bumping one
alone ships a page that tells every user their script is out of date. These
tests pin them together, and pin the grant the login cookie depends on.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USERSCRIPT = ROOT / "userscripts" / "steam_subscribe.user.js"
TEMPLATE = ROOT / "templates" / "index.html"


def _userscript_version() -> int:
    text = USERSCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^//\s*@version\s+(\d+)\s*$", text, re.M)
    assert match, "no @version line in the userscript header"
    return int(match.group(1))


def _template_version() -> int:
    text = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r'name="userscript-version"\s+content="(\d+)"', text)
    assert match, "no userscript-version meta tag in the template"
    return int(match.group(1))


def test_template_and_userscript_declare_the_same_version():
    assert _template_version() == _userscript_version(), (
        "templates/index.html and userscripts/steam_subscribe.user.js disagree "
        "about the bridge version; bump both together"
    )


def test_userscript_grants_gm_cookie():
    """steamLoginSecure is HttpOnly, so document.cookie can never read it.

    Only GM_cookie can, so the grant has to be declared or the login cookie is
    silently unavailable and the backend logs "login_secure: missing".
    """
    text = USERSCRIPT.read_text(encoding="utf-8")
    assert re.search(r"^//\s*@grant\s+GM_cookie\s*$", text, re.M), (
        "the userscript must declare @grant GM_cookie to read steamLoginSecure"
    )


def test_push_sends_the_login_cookie():
    """It was captured and never sent, so the server always saw it missing."""
    text = USERSCRIPT.read_text(encoding="utf-8")
    assert re.search(r"login_secure:\s*login", text), (
        "pushSessionToBackend must include login_secure in the payload"
    )
