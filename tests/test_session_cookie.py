"""The shape of the `steamLoginSecure` value, and what it says about expiry.

Two properties of this value caused a live failure and are pinned here:

* the separator arrives encoded (`%7C%7C`) from a browser and from the config's
  list form, and splitting on a raw `||` silently read the whole value as the
  steamid;
* the token states its own `exp`, about a day out, so a reconcile can know the
  cookie is dead without asking Steam a question it will answer with a sign-in
  page.

The rule the tests protect most carefully is the fail-open one: an unreadable
value is *unknown*, never expired, because a caller that refuses to try on an
unreadable value would strand a working cookie.
"""

import base64
import json

from src import session_cookie
from src.config import login_secure_value


def _token(exp=None, header=b'{"alg":"EdDSA","typ":"JWT"}', extra=None):
    """A three-segment token, with an optional `exp` in its payload."""
    claims = {} if exp is None and extra is None else dict(extra or {})
    if exp is not None:
        claims["exp"] = exp
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    head = base64.urlsafe_b64encode(header).decode().rstrip("=")
    return f"{head}.{payload}.c2lnbmF0dXJl"


# --- splitting the value -----------------------------------------------------

def test_the_raw_separator_splits_the_value():
    assert session_cookie.split("76561198000000000||tok") == ("76561198000000000", "tok")


def test_the_encoded_separator_splits_the_value():
    """A browser stores the encoded form; so does the config's list form."""
    assert session_cookie.split("76561198000000000%7C%7Ctok") == \
        ("76561198000000000", "tok")


def test_the_encoded_form_is_read_before_the_raw_one():
    """`%7C%7C` contains no `||`, so the order only has to be decided, not guessed."""
    assert session_cookie.split("a%7C%7Cb||c") == ("a", "b||c")


def test_a_value_with_no_separator_has_no_halves():
    assert session_cookie.split("no-separator") == (None, None)
    assert session_cookie.split("") == (None, None)
    assert session_cookie.split(None) == (None, None)


def test_a_separator_with_an_empty_tail_still_names_the_account():
    """The steamid is worth logging even when the credential half is unusable."""
    assert session_cookie.split("76561198000000000||") == ("76561198000000000", None)


def test_surrounding_space_does_not_leak_into_either_half():
    assert session_cookie.split(" 76561198000000000 || tok ") == \
        ("76561198000000000", "tok")


def test_the_config_list_form_round_trips_through_the_parser():
    """The two modules must not disagree about the separator they share."""
    config = {"session": {"login_secure": ["76561198000000000", "tok", "x"]}}
    value = login_secure_value(config)

    assert session_cookie.split(value) == ("76561198000000000", "tok%7C%7Cx")


# --- reading the expiry ------------------------------------------------------

def test_the_expiry_is_read_from_the_payload():
    assert session_cookie.decode_expiry(_token(exp=1789623139)) == 1789623139


def test_the_expiry_is_decoded_without_padding():
    """JWT payloads drop their base64 padding, so the decoder has to restore it."""
    assert session_cookie.decode_expiry(_token(exp=1789623139)) == 1789623139


def test_unreadable_tokens_state_no_expiry():
    for value in (
        None,
        "",
        "tok",                       # not even two segments
        "a.b",                       # two segments, payload is not base64 JSON
        "a.!!!.c",                   # invalid base64
        f"a.{base64.urlsafe_b64encode(b'not json').decode().rstrip('=')}.c",
        f"a.{base64.urlsafe_b64encode(b'[1, 2]').decode().rstrip('=')}.c",
    ):
        assert session_cookie.decode_expiry(value) is None, value


def test_a_payload_without_a_numeric_expiry_states_none():
    assert session_cookie.decode_expiry(_token(extra={"sub": "1"})) is None
    assert session_cookie.decode_expiry(_token(extra={"exp": "soon"})) is None
    assert session_cookie.decode_expiry(_token(extra={"exp": None})) is None


def test_a_boolean_is_not_an_expiry():
    """`True` is an int in Python; it must not become the timestamp 1."""
    assert session_cookie.decode_expiry(_token(exp=True)) is None


def test_parse_reports_all_three_parts():
    cookie = session_cookie.parse(f"76561198000000000%7C%7C{_token(exp=1789623139)}")

    assert cookie.steamid == "76561198000000000"
    assert cookie.expires_at == 1789623139
    assert cookie.token and cookie.token.count(".") == 2


def test_parse_of_a_plain_value_is_all_unknown():
    assert session_cookie.parse("opaque") == session_cookie.LoginCookie(None, None, None)
    assert session_cookie.parse(None) == session_cookie.LoginCookie(None, None, None)


# --- describing it for a log line -------------------------------------------

def test_durations_are_truncated_into_readable_units():
    assert session_cookie.duration(45) == "45s"
    assert session_cookie.duration(90) == "1m"
    assert session_cookie.duration(3599) == "59m"
    assert session_cookie.duration(3600) == "1h"
    assert session_cookie.duration(86399) == "23h"
    assert session_cookie.duration(86400) == "1d"


def test_an_expiry_in_the_past_says_how_long_ago():
    phrase = session_cookie.describe_expiry(1000, now=1000 + 14 * 3600)

    assert phrase.startswith("expired 14h ago")
    assert "at " in phrase, "the absolute time is the part an operator can act on"


def test_an_expiry_in_the_future_says_how_long_is_left():
    phrase = session_cookie.describe_expiry(1000 + 9 * 3600, now=1000)

    assert phrase.startswith("expires in 9h")


def test_an_expiry_exactly_now_reads_as_expired():
    assert session_cookie.describe_expiry(1000, now=1000).startswith("expired 0s ago")


def test_no_expiry_describes_nothing():
    assert session_cookie.describe_expiry(None, now=1000) is None
