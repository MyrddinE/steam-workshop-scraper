"""Whether the saved Steam login is still accepted, and what to do about it.

The daemon discovers a dead login cookie deep inside a worker thread -- a
reconcile that could not authenticate, a scrape that came back as the sign-in
page -- and until this module existed the operator learned about it only by
noticing that nothing had a star. The fact is recorded here so the web UI can
say it out loud, and cleared the moment anything authenticated works again.

It is the same kind of fact as a backoff, and lives in the same place for the
same reasons (see :mod:`src.daemon_state`): transient runtime state, describing
a condition that will pass and that nobody chose, kept in a file beside the
database rather than in the versioned, migrated library. Every write is
best-effort, because losing a warning costs a puzzled operator while letting a
diagnostic exception escape would stop the work that produced the fact.

**Steam is the only authority, but it is not the only witness.** The cookie
states its own expiry, so a doomed request can be recognised before it is made
and a freshly saved cookie can be judged without a round trip; that is what
:func:`evaluate_login` is for. It is never the last word -- a session can be
revoked while its token still looks valid -- so a sign-in page that arrives
anyway is recorded too, and it is the *only* thing that clears the record.

Losing a race here is harmless in both directions: the writer is whichever
thread saw the problem, the clearer is whichever thread saw a healthy page, and
a stale warning is corrected by the next scrape.
"""

from __future__ import annotations

import time

from src import session_cookie
from src.daemon_state import StateStore, state_path_for

# The top-level key this module owns in the daemon state file.
SECTION = "session"

# Where an operator signs in again. This is deliberately the page this project
# scrapes: signed out it redirects to the login form, signed in it lands on the
# profile, so one URL is right either way -- and signing in there is what mints
# the `steamLoginSecure` cookie the daemon reads.
LOGIN_URL = "https://steamcommunity.com/my/"

# What the operator is told when a page came back signed out. It names the
# remedy, not the code path, because the person reading it is holding a browser.
NOT_ACCEPTED_DETAIL = (
    "Steam answered a Workshop page with its sign-in page, so the saved login "
    "cookie is no longer accepted"
)


def record_rejected(db_path: str, detail: str, now: int | None = None) -> bool:
    """Record that Steam is not accepting the login cookie.

    ``detail`` is shown to the operator, so it has to say what happened and what
    fixes it rather than which branch noticed.
    """
    store = StateStore(state_path_for(db_path))
    return store.save({SECTION: {
        "detail": str(detail),
        "detected_at": int(time.time()) if now is None else int(now),
    }})


def record_accepted(db_path: str) -> bool:
    """Clear the record, because an authenticated request has just succeeded.

    Returns whether anything was written; a healthy session with nothing
    recorded writes nothing, so this is cheap enough to call on every success
    rather than only on the transition.
    """
    return StateStore(state_path_for(db_path)).remove(SECTION)


def read(db_path: str) -> dict | None:
    """The recorded problem, or ``None`` when the last word was a working login.

    A section that is present but carries no sentence is reported as absent: it
    is not something the UI can explain, and an unexplained warning is worse
    than none.
    """
    section = StateStore(state_path_for(db_path)).load().get(SECTION)
    if not isinstance(section, dict) or not section.get("detail"):
        return None
    detected_at = section.get("detected_at")
    return {
        "detail": str(section["detail"]),
        "detected_at": (int(detected_at)
                        if isinstance(detected_at, (int, float))
                        and not isinstance(detected_at, bool) else None),
    }


def evaluate_login(value: str | None, now: int | None = None) -> str | None:
    """Why this cookie value cannot be used, or ``None`` if it might work.

    The one thing that is knowable without asking Steam, and the reason this is
    a function rather than an inline check: the recheck route uses it to decide
    whether a freshly read cookie is worth clearing the warning for, and the
    reconcile uses the same rule in the opposite direction.

    An unreadable expiry is ``None`` -- "might work" -- because refusing to try
    on a value we cannot parse would strand a working cookie, and Steam's answer
    is what settles it either way.
    """
    if not value:
        return "no login cookie is available"
    expires_at = session_cookie.parse(value).expires_at
    if expires_at is None:
        return None
    reference = int(time.time()) if now is None else int(now)
    if expires_at <= reference:
        return "the saved login cookie %s" % session_cookie.describe_expiry(
            expires_at, now=reference)
    return None
