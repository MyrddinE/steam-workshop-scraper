r"""The shape of Steam's ``steamLoginSecure`` cookie value.

The value is ``<steamid>||<token>``, and the token half is a JWT whose payload
carries an ``exp`` claim. Two properties of that shape are easy to get wrong, so
both are handled here rather than at each call site:

* **The separator travels encoded.** A browser stores the value as
  ``<steamid>%7C%7C<token>``, and :func:`src.config.login_secure_value` writes
  the same form when the config lists the components separately. Code that split
  on a literal ``||`` therefore read the whole value as the steamid and logged
  "unknown" for a cookie it was holding.
* **The token expires, and quickly.** The one deployed cookie measured here had
  ``exp - iat`` of 86,954 seconds (24.1 hours): Steam mints a fresh
  ``steamLoginSecure`` roughly daily, and the browser renews it silently each
  time it is used. A profile nobody has opened therefore goes stale within a
  day, and a request sent with that cookie is answered with Steam's sign-in page
  instead of the page that was asked for. Decoding ``exp`` locally turns that
  into a precise, actionable sentence instead of a puzzling empty page.

Nothing here raises. Every field is best-effort and ``None`` means "the value
does not say", never "no": a value whose expiry cannot be read must not be
treated as an expired one, because declining to try is worse than trying and
failing.
"""

from __future__ import annotations

import base64
import json
import time
from typing import NamedTuple

# The separator between the steamid and the token. Steam hands the cookie out in
# the encoded form; the raw form is what a config written by hand tends to
# contain. Both are read, encoded first because the raw separator never occurs
# inside the encoded one.
RAW_SEPARATOR = "||"
ENCODED_SEPARATOR = "%7C%7C"
SEPARATORS = (ENCODED_SEPARATOR, RAW_SEPARATOR)


class LoginCookie(NamedTuple):
    """What a ``steamLoginSecure`` value says about itself.

    ``token`` is the credential half and must never be logged; it is kept so a
    caller can pass the value on whole rather than re-splitting it.
    """

    steamid: str | None
    token: str | None
    expires_at: int | None


def split(value: str | None) -> tuple[str | None, str | None]:
    """The ``(steamid, token)`` halves of a cookie value.

    Either half is ``None`` when the value is empty or carries no separator; a
    value with a separator and an empty tail still yields its steamid, because
    logging which account a cookie belongs to is useful even when the credential
    is unusable.
    """
    for separator in SEPARATORS:
        if separator in (value or ""):
            steamid, _, token = value.partition(separator)
            return steamid.strip() or None, token.strip() or None
    return None, None


def decode_expiry(token: str | None) -> int | None:
    """The ``exp`` claim of a JWT, or ``None`` if it has none we can read.

    Unverified by design: the signature is Steam's to check, and the only use
    here is to decide whether asking is worth a round trip. Anything unexpected
    -- not three segments, not base64, not JSON, no numeric ``exp`` -- is
    reported as unknown rather than guessed at.
    """
    segments = (token or "").split(".")
    if len(segments) < 2:
        return None
    payload = segments[1]
    try:
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict):
        return None
    expires_at = claims.get("exp")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return None
    return int(expires_at)


def parse(value: str | None) -> LoginCookie:
    """Read everything this module knows out of a cookie value."""
    steamid, token = split(value)
    return LoginCookie(steamid=steamid, token=token, expires_at=decode_expiry(token))


def duration(seconds: int) -> str:
    """A compact human duration: ``45s``, ``12m``, ``14h``, ``3d``.

    Truncated rather than rounded, so "in 1h" is never printed for something
    less than an hour away.
    """
    seconds = max(0, int(seconds))
    for limit, divisor, unit in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h")):
        if seconds < limit:
            return f"{seconds // divisor}{unit}"
    return f"{seconds // 86400}d"


def describe_expiry(expires_at: int | None, now: int | None = None) -> str | None:
    """A phrase for a log line, or ``None`` when no expiry was stated.

    Both directions are useful: a reconcile that is about to be skipped wants
    "expired 14h ago", and the same sentence in the past tense is what makes a
    cookie that is still good obvious in the log without a second run.
    """
    if expires_at is None:
        return None
    now = int(time.time()) if now is None else int(now)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at))
    if expires_at <= now:
        return f"expired {duration(now - expires_at)} ago (at {stamp})"
    return f"expires in {duration(expires_at - now)} (at {stamp})"
