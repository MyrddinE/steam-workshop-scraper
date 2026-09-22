"""The browser-free subscribe engine: one page read, one "click", one answer.

The owner's subscription is a fact only Steam knows, and the project learned --
by measuring it against production -- exactly how little of it can be learned
from an answer body:

* The subscribe control is **server-rendered** as one element,
  ``id="SubscribeItemBtn"``, whose class list carries ``toggled`` when and only
  when this account is currently subscribed. No JavaScript has to run to read
  it; it is in the plain fetched HTML.
* ``POST https://steamcommunity.com/sharedfiles/subscribe`` answers
  ``{"success": 1}`` **both** when an item was newly subscribed and when it was
  already subscribed. The body confirms that the request was accepted; it never
  says whether anything changed. Steam's JSON is therefore *corroboration*, and
  the page's ``toggled`` state is the only authority.
* A missing ``#SubscribeItemBtn`` means "cannot tell", never "not subscribed".
  An error page, a signed-out page and a throttle page all omit it, and reading
  any of them as "not subscribed" would queue a request against an item whose
  state is unknown.
* Steam answers ``2`` (session no longer accepted) and ``15`` (not permitted)
  when it will not take the request. **That is not on its own a session
  problem.** Every attempt reads the item page before it posts, and that page
  carries ``g_sessionID`` -- the CSRF token belonging to the session that served
  it, which a Firefox profile read can never supply because ``sessionid`` is a
  session cookie Firefox keeps in memory and never writes to ``cookies.sqlite``.
  A refusal beside an *authenticated* page read therefore means the credential
  works and the token was stale: it is reported as a token refusal and no
  session problem is recorded. Only a refusal beside an anonymous page read is
  recorded as a session problem, exactly as ``/api/subscribe/<id>`` does, so the
  banner can say so.

This module holds the engine those facts dictate. It is deliberately shared:
the TUI's subscription queue drives it, and the web UI drives it through
``/api/subscribe`` for both the Subscribe button and its queue drain (see
``docs/future-plans.md``).
The request *shape* lives here too, and ``/api/subscribe`` builds its POST from
:func:`resolve_subscribe_credentials`, :func:`subscribe_headers`,
:func:`subscribe_form` and :func:`unsubscribe_form` rather than keeping a second
copy.

**The queue carries both directions.** The queue flag is a boolean with no
direction of its own, so :func:`queued_direction` derives it: a queued row that
is subscribed is a removal, and every other queued row is the addition it always
was. :func:`subscribe_item` chooses from that row state rather than taking a
direction argument, so the TUI pass and the web drain cannot disagree about what
a queued row means.

What decides an attempt
-----------------------

The engine never trusts ``{"success": 1}`` as proof that the *state* changed.
On the default path the pre-read decides what is asked, and the POST's own answer
is the record of what was accepted:

* For an addition, ``toggled`` on the pre-read -> the item is already subscribed.
  :func:`mark_own_subscribed` is recorded (which also clears the queue flag) and
  **no request is sent at all**.
* For a removal, the mirror: a pre-read with no ``toggled`` -> the item is
  already unsubscribed, :func:`mark_own_unsubscribed` is recorded and **no
  request is sent at all**. The guard matters more here than for an addition: a
  blind removal POST against an item the account is not subscribed to is exactly
  the toggle hazard the pre-read exists for.
* Otherwise the POST is sent, and with :data:`VERIFY_AFTER_SUBSCRIBE` ``False``
  Steam's ``{"success": 1}`` is the record:
  :func:`record_confirmed_subscription` is called and the outcome is
  :data:`SUBSCRIBED` -- or, for a removal,
  :func:`record_confirmed_unsubscription` and :data:`UNSUBSCRIBED`; any other
  answer is a :data:`FAILED` worded by :func:`_steam_failure_message`, or a
  :data:`REFUSED` worded by :func:`_steam_unsubscribe_failure_message` for a
  removal, because Steam publishes no removal failure codes: its own
  ``SubscribeItem`` script checks only ``success == 1`` on the removal branch,
  with no ``15``/``25`` cases. That is the read-click shape -- one page read,
  one click, and the POST's own answer as the record. The JSON cannot
  distinguish "newly subscribed" from "already subscribed" (nor "newly
  unsubscribed" from "already unsubscribed"); it says only that the request was
  accepted, which is why the pre-read stays in front of it rather than the
  engine posting blind. The confirmation switch is subscribe-only: a removal is
  always read-click, because the removed page is the authority and Steam's
  removal answer has nothing else to corroborate.

**The confirmation read is retired by default.** Read-click-read costs three
requests per item -- twice the gated page reads of read-click -- and the
production run measured why the third can go: the endpoint is safe on an
already-subscribed item (the POST returned ``{"success": 1}`` and the page stayed
``toggled``, byte-identical), the response body cannot distinguish the two
cases, and the owner has since run the browser-free flow in production and found
the results consistent. Setting :data:`VERIFY_AFTER_SUBSCRIBE` to ``True``
restores it unchanged: :func:`confirm_subscription` reads the page again and the
button, not the JSON, decides -- ``toggled`` records the subscription;
``toggled`` absent beside ``success: 1`` is :data:`DISAGREEMENT` and records
nothing; a button-less re-read is :data:`THROTTLED` when it is Steam's "too many
requests" shell, otherwise :data:`REFUSED`. The step lives alone in
:func:`confirm_subscription`, so the switch cannot perturb the pre-read, the
click, the recording or the capture. The pre-read is kept deliberately: it both
guards against the endpoint ever turning out to be a toggle and skips the POST
for an item that is already subscribed, which is what makes re-running a queue
cheap. That skip records the observation the confirmed path records -- the
pre-read's ``toggled`` is the same page authority -- so an item already
subscribed drains from the queue through :func:`mark_own_subscribed` instead of
being read again on every pass. The retirement order is in
``docs/future-plans.md``.

Every page read and every POST is captured through
:func:`src.capture.record_web_download` under ``item_page`` and ``subscribe``,
using the same ``web_downloads`` switch as the rest of the scraper. The switch's
credential elision is the mechanism that keeps the login cookie out of the
outbox; nothing here bypasses it.

**Pacing.** Every page read is a page load, so it honours the web scraper's
adaptive interval through :class:`WebInterval` -- the same persisted web delay
the daemon's worker moves, held in the daemon state file beside the database and
read fresh on every pass rather than snapshotted, decayed on a clean read and
doubled on a throttle page. On the default path an item pays that once; with
:data:`VERIFY_AFTER_SUBSCRIBE` on, the confirmation read pays it too. The
subscribe POST is the button click, a browser-initiated XHR rather than a page
load, so it is deliberately exempt and never waits.

No browser tab is involved at any point. The engine sends the subscribe POST for
an addition and the unsubscribe POST for a removal; which one it sends is
derived from the row, never guessed, and the pre-read guards both so the endpoint
is never used as a blind toggle.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass

from src import activity, capture, pacing, session_health, web_scraper
from src.config import warn_retired_key
from src.database import (
    get_connection,
    get_subscription_states,
    mark_own_subscribed,
    mark_own_unsubscribed,
)
from src.web_worker import (
    WEB_DELAY_FLOOR,
    configured_web_delay,
    web_delay_store_for,
)

# --- the two endpoints and the one button -----------------------------------

SUBSCRIBE_URL = "https://steamcommunity.com/sharedfiles/subscribe"

# Verified from Steam's own shipped page script, not guessed. The captured item
# page's `sharedfiles_functions_logged_in.js` defines `SubscribeItem`, which
# branches on the button's `toggled` class; the unsubscribe branch posts this
# URL with `{id, appid, sessionid}` -- the subscribe form minus
# `include_dependencies` -- and treats `data.success == 1` as the only success.
# `SubscribeInlineItem` and `SubscribeCollectionItem` in the same file post the
# same URL with the same three fields.
UNSUBSCRIBE_URL = "https://steamcommunity.com/sharedfiles/unsubscribe"

ITEM_PAGE_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}"


def item_page_url(workshop_id: int) -> str:
    """The item page URL, built in one place for the fetch and the Referer."""
    return ITEM_PAGE_URL.format(workshop_id=workshop_id)


# Button states. ``UNKNOWN`` is a real answer -- "cannot tell" -- and is never
# collapsed into ``NOT_TOGGLED``.
BUTTON_TOGGLED = "subscribed"
BUTTON_NOT_TOGGLED = "not_subscribed"
BUTTON_UNKNOWN = "unknown"

# --- outcome vocabulary ------------------------------------------------------

ALREADY_SUBSCRIBED = "already_subscribed"
SUBSCRIBED = "subscribed"
ALREADY_UNSUBSCRIBED = "already_unsubscribed"
UNSUBSCRIBED = "unsubscribed"
DISAGREEMENT = "disagreement"
THROTTLED = "throttled"
SESSION_PROBLEM = "session_problem"
TOKEN_REFUSED = "token_refused"
REFUSED = "refused"
FAILED = "failed"

# The two directions a queue entry can mean. The flag is a boolean with no
# direction of its own; `queued_direction` derives one from the row.
ADD = "add"
REMOVE = "remove"

# The messages the route already shows. Kept here so the engine and the route
# say the same thing, and so a session problem is recorded with the same
# sentence in both paths.
SUBSCRIBE_REMEDY = (
    "Sign in to Steam in the browser the daemon reads cookies from, or "
    "configure session.login_secure."
)
NO_SESSION_MESSAGE = "No Steam session configured. " + SUBSCRIBE_REMEDY
NO_LOGIN_MESSAGE = "No Steam login cookie is available. " + SUBSCRIBE_REMEDY
SUBSCRIBE_SESSION_REJECTED_DETAIL = (
    "Steam refused the subscribe request, so the saved login cookie is no "
    "longer accepted. " + SUBSCRIBE_REMEDY
)
# The refusal that is *not* about the login. The item page the attempt read was
# authenticated, so the credential is proven good; Steam turned down the CSRF
# token. Recording a session problem here is what told the owner to sign in
# again while the login was fine, so this sentence deliberately does not.
TOKEN_REFUSED_DETAIL = (
    "Steam refused the subscribe request, but the item page read in the same "
    "attempt was served to your signed-in account, so the login cookie is "
    "working: the CSRF token was refused. The item stays queued and is retried "
    "with the token from its own page; no session problem was recorded."
)

_NO_BUTTON_MESSAGE = (
    "The item page carries no SubscribeItemBtn, so its subscription state "
    "cannot be read; nothing was sent."
)
_NO_BUTTON_ON_VERIFY_MESSAGE = (
    "The page read back after the subscribe carries no SubscribeItemBtn, so "
    "the result cannot be verified; nothing was recorded."
)
_THROTTLED_MESSAGE = (
    "Steam served its throttle page, so the item's state is unknown; it stays "
    "queued and is retried once the budget refills."
)
_THROTTLED_VERIFY_MESSAGE = (
    "Steam served its throttle page for the confirmation read, so the result "
    "cannot be confirmed; the item stays queued."
)
_DISAGREEMENT_MESSAGE = (
    "Steam answered success but the item page still shows not subscribed, so "
    "the two sources disagree; nothing was recorded and the item stays queued."
)


# The confirmation read, on a switch, **off by default**. The production run
# proved the subscribe endpoint is safe on an already-subscribed item -- it
# returned ``{"success": 1}`` and the page stayed ``toggled``, byte-identical --
# and the owner has run the browser-free read-click flow in production since and
# found the results consistent, so the third read is no longer wanted. With this
# ``False``, :func:`subscribe_item` records from Steam's own answer (which cannot
# distinguish "newly subscribed" from "already subscribed", and is corroboration
# only) and never reads the page a second time; setting it ``True`` restores
# :func:`confirm_subscription` unchanged. The pre-read and the "already toggled
# -> no request at all" short-circuit are deliberately *not* behind this switch;
# only the confirmation is. The order in which the reads are retired is recorded
# in ``docs/future-plans.md``.
VERIFY_AFTER_SUBSCRIBE = False


def _steam_failure_message(steam_success) -> str:
    """A short explanation for a non-success, non-expiry Steam answer."""
    if steam_success == 25:
        return "Steam refused the subscribe: subscription limit reached (15,000)."
    return f"Steam refused the subscribe (success={steam_success!r})."


def _steam_unsubscribe_failure_message(steam_success) -> str:
    """A refusal for a removal answer that is not ``success: 1``.

    Steam publishes no removal failure codes -- its own ``SubscribeItem`` script
    checks only ``data.success == 1`` on the unsubscribe branch, with no
    ``15``/``25`` cases as the subscribe branch has -- so the raw value is named
    rather than translated into a cause that may not apply.
    """
    return f"Steam refused the unsubscribe (success={steam_success!r})."


def queued_direction(item: dict | None) -> str:
    """Which way a queue entry points, derived from its own row.

    ``is_queued_for_subscription`` is only a membership flag, so the direction
    has to come from somewhere: with the flag set, ``own_subscribed`` decides --
    set means a removal is queued, clear means an addition. Every row that
    predates this feature keeps the meaning it had, and no schema change is
    needed.
    """
    if item and item.get("is_queued_for_subscription") and item.get("own_subscribed"):
        return REMOVE
    return ADD


def queued_direction_for(db_path: str, workshop_id: int) -> str:
    """The direction of one row's queue entry, read from the database.

    A missing row is an addition: there is nothing to remove for an item the
    database does not know.
    """
    return queued_direction(
        get_subscription_states(db_path, [workshop_id]).get(workshop_id))


@dataclass
class SubscribeOutcome:
    """What one engine run concluded, in a shape both front ends can render.

    ``status`` is one of the module's outcome constants. ``subscribed`` is the
    authoritative state after the run -- true only when the page (or the
    already-toggled read) said so, never from Steam's JSON alone. ``stays_queued``
    is true for the outcomes that must leave ``is_queued_for_subscription`` alone;
    both settled outcomes -- a subscription and a removal -- clear it.
    """

    workshop_id: int
    status: str
    message: str = ""
    subscribed: bool = False
    button_before: str | None = None
    button_after: str | None = None
    steam_success: int | None = None

    @property
    def is_subscribed(self) -> bool:
        """Whether the item is subscribed now, verified or already."""
        return self.status in (ALREADY_SUBSCRIBED, SUBSCRIBED)

    @property
    def is_unsubscribed(self) -> bool:
        """Whether the item is unsubscribed now, verified or already."""
        return self.status in (ALREADY_UNSUBSCRIBED, UNSUBSCRIBED)

    @property
    def stays_queued(self) -> bool:
        """Whether the item's queue entry should survive this outcome."""
        return not (self.is_subscribed or self.is_unsubscribed)


# The engine's own summary line for the TUI, one short phrase per outcome.
# ``REFUSED`` is deliberately not "cannot tell": the engine records it for the
# cases it determined -- no session, no login, no item row, no AppID, and a
# button-less page whose state it could not read -- so the phrase must not claim
# the result is unknown. The cause differs per outcome and is written to the log
# by the caller, so the phrase points there rather than guessing one of them.
_OUTCOME_LABELS = {
    ALREADY_SUBSCRIBED: "already subscribed",
    SUBSCRIBED: "subscribed",
    ALREADY_UNSUBSCRIBED: "already unsubscribed",
    UNSUBSCRIBED: "unsubscribed",
    DISAGREEMENT: "unverified (sources disagree)",
    THROTTLED: "left queued (throttled)",
    SESSION_PROBLEM: "session problem",
    TOKEN_REFUSED: "refused (stale CSRF token)",
    REFUSED: "refused (see log)",
    FAILED: "failed",
}


def status_label(status: str) -> str:
    """The human phrase for an outcome status."""
    return _OUTCOME_LABELS.get(status, status)


# --- the parser --------------------------------------------------------------

# One element, found by its id, wherever its attributes sit. The class list is
# read from that element's own tag -- a `toggled` on any other element (or in
# some unrelated script text) must not count. The tag has to start like a tag
# (`<a`, `<div`, ...), so a bare `<` in script text cannot open a match that
# swallows the finder's own literal on the far side of some later `>`.
_BUTTON_TAG_RE = re.compile(
    r"<[a-zA-Z][^>]*\bid\s*=\s*[\"']SubscribeItemBtn[\"'][^>]*>", re.IGNORECASE
)
_CLASS_ATTR_RE = re.compile(r"\bclass\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_TOGGLED_CLASS = "toggled"


def parse_button_state(html: str | bytes | None) -> str:
    """Read ``#SubscribeItemBtn``'s state from a server-rendered item page.

    Returns :data:`BUTTON_TOGGLED` when the element carries ``toggled``,
    :data:`BUTTON_NOT_TOGGLED` when it is present without it, and
    :data:`BUTTON_UNKNOWN` when the element is absent. The three are never
    conflated: "absent" is what a throttle page, an error page and a
    signed-out page all look like, and none of them is a statement that the
    account is not subscribed.
    """
    if not html:
        return BUTTON_UNKNOWN
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    match = _BUTTON_TAG_RE.search(html)
    if match is None:
        return BUTTON_UNKNOWN
    class_match = _CLASS_ATTR_RE.search(match.group(0))
    classes = class_match.group(1).split() if class_match else []
    return BUTTON_TOGGLED if _TOGGLED_CLASS in classes else BUTTON_NOT_TOGGLED


# The CSRF token Steam injects into every page it serves, always quoted. It is
# the value Steam's own JavaScript posts back, so it belongs to the session that
# served *this* page -- unlike a `sessionid` off the cookie store, which can only
# ever be the profile's stale copy (Firefox keeps the real one in memory).
_CSRF_TOKEN_RE = re.compile(r"""g_sessionID\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def parse_csrf_token(html: str | bytes | None) -> str:
    """The CSRF token Steam injected into a page, or ``""`` when it has none.

    Read from the page the attempt actually fetched, because ``sessionid`` is a
    session cookie: Firefox holds it in memory and never writes it to
    ``cookies.sqlite``, so a profile read can never supply the token belonging
    to the credential that authenticated the page. An error or throttle page
    carries no usable token, and an empty answer is left to the caller's
    fallback rather than guessed at.
    """
    if not html:
        return ""
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    match = _CSRF_TOKEN_RE.search(html)
    return match.group(1).strip() if match else ""


# How much of the digest is kept. Long enough that two tokens collide only by
# accident, short enough to read in a log line.
_FINGERPRINT_LENGTH = 12


def token_fingerprint(token: str | None) -> str:
    """A short, stable, non-reversible fingerprint for a secret token.

    The log has to be able to say "the same token as last time" without the
    token reaching disk. A SHA-256 prefix compares equal for one token and
    different for another, and cannot be turned back into a credential.
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:_FINGERPRINT_LENGTH]


def token_log_note(token: str, page_token: str, fallback_token: str) -> str:
    """The one-line fingerprint note both call sites log for a subscribe POST.

    Never the token itself. When the page's ``g_sessionID`` differs from the
    fallback token it replaced, both fingerprints are named: that
    difference is exactly what a stale CSRF token looks like from the log alone,
    and it is what took an afternoon to find the first time.
    """
    note = f"sessionid_fp={token_fingerprint(token) or 'none'}"
    if page_token and page_token != fallback_token:
        note += (
            f", page_fp={token_fingerprint(page_token)}, "
            f"fallback_fp={token_fingerprint(fallback_token) or 'none'}"
        )
    return note


def page_read_authenticated(page_html: str | bytes | None) -> bool:
    """Whether a page read was served to a signed-in account.

    This is the evidence that separates a refused token from a genuinely
    expired session: account markers on the page mean the login cookie just
    authenticated a request, so a refusal that follows is about the CSRF token.
    A missing body or an anonymous page is not evidence of anything good.
    """
    if not page_html:
        return False
    if isinstance(page_html, bytes):
        page_html = page_html.decode("utf-8", "replace")
    return not web_scraper.looks_like_signed_out(page_html)


# --- the shared request shape ------------------------------------------------


def resolve_subscribe_credentials(config: dict, token_fallback: str = ""):
    """The cookie set, the fallback CSRF token, and the login cookie.

    The cookies and the login come from one read of the cookie source, so the
    credential and the cookie set cannot belong to different sessions. The CSRF
    token is deliberately *not* taken from that read: ``sessionid`` is a session
    cookie Firefox never persists, so the profile read cannot supply the current
    one. :func:`resolve_subscribe_token` prefers the page's own ``g_sessionID``;
    the value returned here is only an explicit ``token_fallback`` or the
    configured ``session.csrf_token``, for a page that carries no token.

    Returns ``(cookies, fallback_token, login)``; the fallbacks may be empty, and
    the caller decides whether that is a refusal.
    """
    cookies = web_scraper._build_workshop_cookies(config)
    session = config.get("session", {})
    if "id" in session:
        warn_retired_key("session", "id", "csrf_token")
    token = (
        token_fallback
        or session.get("csrf_token")
        or ""
    )
    login = cookies.get("steamLoginSecure", "")
    return cookies, token, login


def resolve_subscribe_token(page_html: str | bytes | None, cookies: dict,
                            fallback_token: str = "") -> tuple[str, str]:
    """Pick the CSRF token for one POST, from the page the attempt fetched first.

    The precedence is the whole fix for a refused subscribe: the token Steam
    injected into the page this attempt already read (``g_sessionID``), then a
    ``sessionid`` already in the cookie set, then the configured fallback.
    Whichever wins is put back into the cookie jar, so the form field
    and the cookie agree -- Steam answers a mismatch between them exactly as it
    answers a stale one.

    ``page_html`` is the fetched page's body. Returns ``(token, page_token)``;
    ``page_token`` is exposed so the caller can log both fingerprints when the
    page's token differs from the fallback it replaced.
    """
    page_token = parse_csrf_token(page_html)
    token = page_token or cookies.get("sessionid") or fallback_token or ""
    if token:
        cookies["sessionid"] = token
    return token, page_token


def subscribe_headers(workshop_id: int) -> dict:
    """The headers for a subscription POST, in either direction.

    The UA is the project's, derived from the installed Firefox, because the
    cookies in the jar came from that browser; the fetch metadata describes a
    same-origin XHR/form POST, not a top-level navigation. Steam's own
    ``SubscribeItem`` posts both branches from the same page through jQuery, so
    the identity is the same for the subscribe and the unsubscribe.
    """
    return {
        "User-Agent": web_scraper.USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": web_scraper.BROWSER_HEADERS["Accept-Language"],
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://steamcommunity.com",
        "Referer": item_page_url(workshop_id),
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        # Inferred, not measured: Steam's community front-end posts this
        # endpoint through jQuery, which sets this header, but no capture of
        # this request exists in this repository. Named as inferred so it is
        # not read as an observation.
        "X-Requested-With": "XMLHttpRequest",
    }


def subscribe_form(workshop_id: int, appid, token: str) -> dict:
    """The form body of one subscribe POST."""
    return {
        "id": str(workshop_id),
        "appid": str(appid),
        "include_dependencies": "false",
        "sessionid": token,
    }


def unsubscribe_form(workshop_id: int, appid, token: str) -> dict:
    """The form body of one unsubscribe POST.

    Steam's own ``SubscribeItem`` script builds this as the same shape as the
    subscribe form **minus** ``include_dependencies``, which the removal has no
    use for; ``SubscribeInlineItem`` posts exactly this three-field form. Kept a
    separate builder rather than a flag on ``subscribe_form`` so the one-field
    difference is stated in one place and cannot be dropped by accident.
    """
    return {
        "id": str(workshop_id),
        "appid": str(appid),
        "sessionid": token,
    }


def _post_captured(url: str, form: dict, cookies: dict, workshop_id: int,
                   session=None):
    """POST one of the two subscription requests, captured before parsing.

    Both directions share the request identity and the capture: the values
    captured are the ones actually handed to the session -- the jar, the form and
    the headers are the same objects -- rather than a reconstruction, and a
    non-JSON answer is captured too because the capture happens before
    ``resp.json()`` is called by the caller. The capture kind stays ``subscribe``
    for both: it is the one button surface, and the captured URL says which way
    it went.
    """
    session = session if session is not None else web_scraper._get_session()
    headers = subscribe_headers(workshop_id)
    resp = session.post(url, data=form, cookies=cookies, headers=headers, timeout=15)
    if capture.web_download_capture_active():
        capture.record_web_download(
            capture.SUBSCRIBE_KIND, workshop_id, url,
            {
                "request": {"method": "POST", "url": url,
                            "headers": headers, "cookies": cookies, "data": form},
                "http_status": getattr(resp, "status_code", None),
                "final_url": getattr(resp, "url", "") or url,
                "response_headers": getattr(resp, "headers", None),
                "body": getattr(resp, "text", None),
            },
        )
    return resp


def post_subscribe_request(cookies: dict, token: str, appid, workshop_id: int,
                           session=None):
    """POST the subscribe and capture it before the body is parsed."""
    return _post_captured(
        SUBSCRIBE_URL, subscribe_form(workshop_id, appid, token), cookies,
        workshop_id, session=session)


def post_unsubscribe_request(cookies: dict, token: str, appid, workshop_id: int,
                             session=None):
    """POST the unsubscribe and capture it before the body is parsed."""
    return _post_captured(
        UNSUBSCRIBE_URL, unsubscribe_form(workshop_id, appid, token), cookies,
        workshop_id, session=session)


def lookup_consumer_appid(db_path: str, workshop_id: int):
    """``(found, appid)`` for one item, so a refusal can name the real reason.

    The subscribe POST cannot be built without an AppID, and a missing row is a
    different answer from a row whose AppID is NULL; the caller shows one as a
    404 and the other as a 400, exactly as the route always has.
    """
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT consumer_appid FROM workshop_items WHERE workshop_id=?",
        (workshop_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return False, None
    return True, row["consumer_appid"]


# --- recording the two answers ----------------------------------------------


def record_confirmed_subscription(db_path: str, workshop_id: int) -> None:
    """Stamp a confirmed subscription, exactly as ``/api/subscribed`` does.

    ``mark_own_subscribed`` sets ``own_subscribed``, stamps the sticky
    first-seen time and clears the queue flag in one write; the session problem
    a previous refusal recorded is cleared because an authenticated request has
    just succeeded.
    """
    mark_own_subscribed(db_path, workshop_id)
    session_health.record_accepted(db_path)


def record_confirmed_unsubscription(db_path: str, workshop_id: int) -> None:
    """Record a confirmed removal, exactly as ``/api/unsubscribed`` does.

    ``mark_own_unsubscribed`` clears ``own_subscribed`` and the queue flag in one
    write and leaves the sticky first-seen time alone, so the marker becomes
    ``previously`` rather than losing the only evidence of the subscription. The
    session problem a previous refusal recorded is cleared because an
    authenticated request has just succeeded.
    """
    mark_own_unsubscribed(db_path, workshop_id)
    session_health.record_accepted(db_path)


def record_expired_session(db_path: str) -> None:
    """Record Steam's session-expiry answer with the route's own wording."""
    session_health.record_rejected(db_path, SUBSCRIBE_SESSION_REJECTED_DETAIL)


def refusal_outcome(workshop_id: int, *, page_authenticated: bool, db_path: str,
                          button_before: str | None = None,
                          steam_success: int | None = None) -> SubscribeOutcome:
    """What a refused subscribe means, given what the attempt's own read saw.

    A refusal beside an authenticated page read is about the CSRF token: the
    credential just fetched a signed-in page, so recording a session problem
    would tell the operator to sign in again while the login is fine -- the
    misleading half of this defect. A refusal beside an anonymous (or absent)
    page read still records the session problem, because nothing authenticated.
    """
    if page_authenticated:
        logging.warning(
            "[Subscribe] Steam refused the CSRF token for %s, but the page read "
            "in the same attempt was authenticated; the login is good, so no "
            "session problem was recorded.", workshop_id)
        return SubscribeOutcome(
            workshop_id, TOKEN_REFUSED, TOKEN_REFUSED_DETAIL,
            button_before=button_before, steam_success=steam_success)
    record_expired_session(db_path)
    return SubscribeOutcome(
        workshop_id, SESSION_PROBLEM, SUBSCRIBE_SESSION_REJECTED_DETAIL,
        button_before=button_before, steam_success=steam_success)


# --- the engine --------------------------------------------------------------


def page_body(page) -> str:
    """The decoded body of a fetched page, or ``""`` when there is none."""
    if not page:
        return ""
    body = page.get("body")
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body


def _always_running() -> bool:
    """The default ``keep_running`` for a wait with nothing to cancel it."""
    return True


class WebInterval:
    """The web scraper's adaptive interval, for the engine's page reads.

    A page read is a page load, so it honours the same interval the daemon's web
    worker does: :func:`pacing.wait` before the request, the delay decaying on a
    clean read and doubling on a detected throttle page. **The subscribe POST is
    deliberately exempt** -- it is the button click, a browser-initiated XHR, not
    a page load -- so ``post_subscribe_request`` never touches this.

    The working delay is in memory for the pass and the persisted copy is the
    restart point, exactly as the worker's ``web_delay`` is: the delay is a float
    and only the copy on disk is rounded (``pacing.persistable``). It is daemon
    **state**, so the starting value is read from the daemon state file beside
    the database and every move is written back there -- not into ``config.yaml``
    -- so the engine, the worker and the TUI all share the one delay.
    """

    def __init__(self, config: dict | None = None, *, keep_running=None, clock=None):
        self.config = config if isinstance(config, dict) else {}
        self._store = web_delay_store_for(self.config)
        self._keep_running = keep_running or _always_running
        self._clock = clock or pacing.Clock()
        self.delay = configured_web_delay(self.config)
        self._persisted_delay = self.delay
        self._elapsed = 0.0

    def before_read(self) -> None:
        """Start timing this read's healthy run, then wait the interval.

        The clock is ticked before the wait so the elapsed period spans the
        previous interval, the same way the worker measures it at the top of an
        iteration and sleeps at the bottom.
        """
        self._elapsed = self._clock.since()
        pacing.wait(self.delay, self._keep_running)

    def after_read(self, button_state: str, body: str) -> None:
        """Feed one page read's outcome back into the shared delay.

        A read that carried a button was a clean page, so the healthy time since
        the previous read decays the delay. A throttle page doubles it and
        persists immediately, because a restart during a wall must not resume at
        the pace that was just refused. Anything else (an error page with no
        button) leaves the delay alone: it is the item's answer, not a rate
        signal.
        """
        if button_state != BUTTON_UNKNOWN:
            self._set_delay(pacing.decay(self.delay, self._elapsed, WEB_DELAY_FLOOR))
        elif web_scraper.looks_like_rate_limited(body):
            self._set_delay(pacing.backoff(self.delay), force=True)

    def _set_delay(self, value: float, *, force: bool = False) -> None:
        self.delay = max(WEB_DELAY_FLOOR, value)
        if not force and not pacing.needs_persist(self.delay, self._persisted_delay):
            return
        self._persisted_delay = self.delay
        # The one write the worker makes too: the delay lives in the state file
        # beside the database, in the web worker's own section, which is what
        # lets the daemon's worker and the front ends share it across processes.
        pacing.save_delay(self._store, pacing.WEB_DELAY_SECTION, self.delay)


def fetch_item_page(workshop_id: int, *, interval: WebInterval,
                    keep_body: bool = True) -> dict | None:
    """Fetch the item page with the scraper's own request shape, and capture it.

    The page is always asked for whole (``keep_body=True``) because the whole
    point is the server-rendered button, and a successful fetch is captured just
    like the worker's. The capture's ``ok`` is whether the button could be read,
    which is the question this module asks of the page.

    ``interval`` is required: it gates the request on the shared web interval and
    feeds the read back into it, and a page read that skipped it would be asking
    Steam for the page without honouring the rate the rest of the scraper is
    keeping. Only the subscribe POST is exempt.
    """
    url = item_page_url(workshop_id)
    interval.before_read()
    data = web_scraper.scrape_extended_details(url, keep_body=keep_body)
    body = page_body(data)
    state = parse_button_state(body)
    interval.after_read(state, body)
    if capture.web_download_capture_active() and data:
        capture.record_web_download(
            capture.ITEM_PAGE_KIND, workshop_id, url, data,
            succeeded=state != BUTTON_UNKNOWN,
        )
    return data


def _unknown_button_outcome(workshop_id: int, page_html: str,
                            button_before: str) -> SubscribeOutcome:
    """A pre-read with no button: a throttle if the shell says so, else a refusal.

    Shared by both directions. A missing button is "cannot tell" and is never
    read as a statement either way, so nothing is sent and the queue entry
    survives.
    """
    if web_scraper.looks_like_rate_limited(page_html):
        logging.warning(
            "[Subscribe] Throttled while reading item %s; left queued.", workshop_id)
        return SubscribeOutcome(
            workshop_id, THROTTLED, _THROTTLED_MESSAGE, button_before=button_before)
    logging.warning(
        "[Subscribe] No subscribe button on item %s; refusing.", workshop_id)
    return SubscribeOutcome(
        workshop_id, REFUSED, _NO_BUTTON_MESSAGE, button_before=button_before)


def subscribe_item(workshop_id: int, *, config: dict, db_path: str,
                   token_fallback: str = "", interval: WebInterval | None = None,
                   keep_running=None) -> SubscribeOutcome:
    """Subscribe -- or unsubscribe -- one item without a browser.

    The direction comes from the queued row's own state (:func:`queued_direction`):
    a queued row that is subscribed is a removal, every other queued row is an
    addition. The exact flow is in the module docstring. ``config`` supplies the
    cookie source (``web_scraper._build_workshop_cookies``) and the configured
    session id; ``db_path`` is where the subscription, the removal and any
    session problem are recorded. ``token_fallback`` is an explicit CSRF token
    for a caller that already has one; the TUI and the web route pass nothing and
    rely on the config and the page's own token.

    ``interval`` is the shared web interval the page read honours -- and the
    confirmation read too, for an addition, when :data:`VERIFY_AFTER_SUBSCRIBE`
    re-enables it. A pass builds one and passes it down so the delay spans every
    item; a single standalone call builds its own from ``config``, whose
    persisted delay it reads and moves in the daemon state file. The submit POST
    is not gated on it.
    """
    if interval is None:
        interval = WebInterval(config, keep_running=keep_running)
    # The direction is derived from the row, not passed in, so the TUI's pass and
    # the web drain cannot disagree about what a queued row means.
    removing = queued_direction_for(db_path, workshop_id) == REMOVE
    page = fetch_item_page(workshop_id, interval=interval)
    page_html = page_body(page)
    button_before = parse_button_state(page_html)
    # The evidence a later refusal is judged against: this attempt's own page
    # read, authenticated or not, observed before the POST is sent.
    page_authenticated = page_read_authenticated(page_html)

    if removing:
        if button_before == BUTTON_NOT_TOGGLED:
            # The mirror of the already-subscribed short-circuit: the page says
            # there is nothing to remove, so the queue entry settles through the
            # removal write with no request. A blind removal POST against an item
            # the account is not subscribed to is exactly the toggle hazard the
            # pre-read exists to prevent.
            mark_own_unsubscribed(db_path, workshop_id)
            return SubscribeOutcome(
                workshop_id, ALREADY_UNSUBSCRIBED,
                "The item page already shows it unsubscribed; no request was sent.",
                button_before=button_before,
            )
        if button_before == BUTTON_UNKNOWN:
            return _unknown_button_outcome(workshop_id, page_html, button_before)
        # button_before == BUTTON_TOGGLED: the page agrees there is a removal to
        # send, so the shared credential and POST block below runs.
    else:
        if button_before == BUTTON_TOGGLED:
            # The browser plugin never clicked an item it could see was already
            # subscribed, and neither does this: no request, no toggle question.
            # The page is the same authority the confirmed path trusts, so the
            # observation is recorded with the same write -- `mark_own_subscribed`
            # sets `own_subscribed`, clears `is_queued_for_subscription` and stamps
            # the sticky first-seen time. Recording nothing here is what left an
            # item that was already subscribed in the queue for every later pass
            # to read and skip again.
            mark_own_subscribed(db_path, workshop_id)
            return SubscribeOutcome(
                workshop_id, ALREADY_SUBSCRIBED,
                "The item page already shows it subscribed; no request was sent.",
                subscribed=True, button_before=button_before,
            )
        if button_before == BUTTON_UNKNOWN:
            return _unknown_button_outcome(workshop_id, page_html, button_before)
        # button_before == BUTTON_NOT_TOGGLED: an addition to send.

    cookies, fallback_token, login = resolve_subscribe_credentials(
        config, token_fallback)
    # The page's own `g_sessionID` first: it belongs to the session that served
    # the page above, and `sessionid` off the cookie store cannot be current.
    token, page_token = resolve_subscribe_token(page_html, cookies, fallback_token)
    if not token:
        return SubscribeOutcome(
            workshop_id, REFUSED, NO_SESSION_MESSAGE, button_before=button_before)
    if not login:
        return SubscribeOutcome(
            workshop_id, REFUSED, NO_LOGIN_MESSAGE, button_before=button_before)

    problem = session_health.evaluate_login(login)
    if problem:
        session_health.record_rejected(db_path, problem)
        return SubscribeOutcome(
            workshop_id, SESSION_PROBLEM, problem, button_before=button_before)

    found, appid = lookup_consumer_appid(db_path, workshop_id)
    if not found:
        return SubscribeOutcome(
            workshop_id, REFUSED, "Item not found.", button_before=button_before)
    if not appid:
        return SubscribeOutcome(
            workshop_id, REFUSED, "Item has no AppID.", button_before=button_before)

    verb = "unsubscribe" if removing else "subscribe"
    logging.info(
        "[Subscribe] POSTing to Steam: id=%s, appid=%s, %s, login=%s, direction=%s",
        workshop_id, appid, token_log_note(token, page_token, fallback_token),
        "set" if login else "missing", "remove" if removing else "add")
    resp = None
    try:
        if removing:
            resp = post_unsubscribe_request(cookies, token, appid, workshop_id)
        else:
            resp = post_subscribe_request(cookies, token, appid, workshop_id)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - the engine reports, never raises
        # A body that is not JSON can be Steam's throttle shell, which is not a
        # failure of the item. A 401 with an unreadable body is still a refusal.
        if getattr(resp, "status_code", None) == 401:
            return refusal_outcome(
                workshop_id, page_authenticated=page_authenticated,
                db_path=db_path, button_before=button_before)
        if web_scraper.looks_like_rate_limited(getattr(resp, "text", "") or ""):
            logging.warning(
                "[Subscribe] Throttled on the %s POST for %s; left queued.",
                verb, workshop_id)
            return SubscribeOutcome(
                workshop_id, THROTTLED, _THROTTLED_MESSAGE, button_before=button_before)
        logging.warning("[Subscribe] Request failed for workshop_id=%s: %s",
                        workshop_id, exc)
        return SubscribeOutcome(
            workshop_id, FAILED, f"{verb.capitalize()} request failed: {exc}",
            button_before=button_before)

    steam_success = data.get("success") if isinstance(data, dict) else None
    if getattr(resp, "status_code", None) == 401 or steam_success in (2, 15):
        # Steam's "the session is gone / not permitted" answers. Whether that is
        # a session problem at all depends on the page this attempt read. The
        # removal path keeps the same handling: a 2/15 here is the same refusal,
        # and the raw value is not translated for the removal because Steam
        # publishes no removal codes.
        return refusal_outcome(
            workshop_id, page_authenticated=page_authenticated, db_path=db_path,
            button_before=button_before, steam_success=steam_success)

    if removing:
        if steam_success == 1:
            record_confirmed_unsubscription(db_path, workshop_id)
            return SubscribeOutcome(
                workshop_id, UNSUBSCRIBED,
                "Steam accepted the unsubscribe.",
                button_before=button_before, steam_success=steam_success)
        # Steam publishes no removal failure codes -- its own script checks only
        # `success == 1` -- so the raw answer is logged rather than translated
        # into a cause that may not apply.
        logging.warning(
            "[Unsubscribe] Steam refused the removal for %s: success=%r",
            workshop_id, steam_success)
        return SubscribeOutcome(
            workshop_id, REFUSED, _steam_unsubscribe_failure_message(steam_success),
            button_before=button_before, steam_success=steam_success)

    if not VERIFY_AFTER_SUBSCRIBE:
        # The confirmation step has been retired (see the switch above). Steam's
        # answer is all there is, so it is recorded the way the route records it.
        if steam_success == 1:
            record_confirmed_subscription(db_path, workshop_id)
            return SubscribeOutcome(
                workshop_id, SUBSCRIBED,
                "Steam accepted the subscribe; the confirmation read is disabled.",
                subscribed=True, button_before=button_before, steam_success=steam_success)
        return SubscribeOutcome(
            workshop_id, FAILED, _steam_failure_message(steam_success),
            button_before=button_before, steam_success=steam_success)

    return confirm_subscription(
        workshop_id, steam_success, db_path=db_path, button_before=button_before,
        interval=interval)


def confirm_subscription(workshop_id: int, steam_success, *, db_path: str,
                         button_before: str, interval: WebInterval) -> SubscribeOutcome:
    """The confirmation step, on its own: read the page and decide from it.

    This is the third request of the read-click-read flow and it is deliberately
    isolated here so its later removal is one call site and one switch
    (:data:`VERIFY_AFTER_SUBSCRIBE`), touching neither the pre-read that guards
    the POST nor the recording of a refusal. The production run measured that
    the endpoint is safe on an already-subscribed item -- the POST returned
    ``{"success": 1}`` and the page stayed ``toggled``, byte-identical -- so this
    read is evidence-gathering for the testing phase, not a permanent third
    request. See ``docs/future-plans.md`` for the order in which the reads are
    retired.

    The page is the authority; ``success`` from Steam's JSON only corroborates.
    """
    after_page = fetch_item_page(workshop_id, interval=interval)
    button_after = parse_button_state(page_body(after_page))
    logging.info(
        "[Subscribe] Confirmation for %s: before=%s after=%s steam_success=%r",
        workshop_id, button_before, button_after, steam_success)

    if button_after == BUTTON_TOGGLED:
        record_confirmed_subscription(db_path, workshop_id)
        if steam_success == 1:
            message = "Subscribed and verified from the item page."
        else:
            message = (
                "Subscribed -- the item page shows it -- but Steam answered "
                f"success={steam_success!r}, which disagrees."
            )
        return SubscribeOutcome(
            workshop_id, SUBSCRIBED, message, subscribed=True,
            button_before=button_before, button_after=button_after, steam_success=steam_success)

    if button_after == BUTTON_NOT_TOGGLED:
        if steam_success == 1:
            return SubscribeOutcome(
                workshop_id, DISAGREEMENT, _DISAGREEMENT_MESSAGE,
                button_before=button_before, button_after=button_after,
                steam_success=steam_success)
        return SubscribeOutcome(
            workshop_id, FAILED, _steam_failure_message(steam_success),
            button_before=button_before, button_after=button_after, steam_success=steam_success)

    # No button on the confirmation read: cannot tell. A throttle page stays
    # queued.
    if web_scraper.looks_like_rate_limited(page_body(after_page)):
        logging.warning(
            "[Subscribe] Throttled on the confirmation read for %s; left queued.",
            workshop_id)
        return SubscribeOutcome(
            workshop_id, THROTTLED, _THROTTLED_VERIFY_MESSAGE,
            button_before=button_before, button_after=button_after, steam_success=steam_success)
    return SubscribeOutcome(
        workshop_id, REFUSED, _NO_BUTTON_ON_VERIFY_MESSAGE,
        button_before=button_before, button_after=button_after, steam_success=steam_success)


# --- the pass: the pause, and one item after another -------------------------


class PauseLock:
    """The ``.pauselock`` file as a context manager.

    The daemon's web and image workers poll this path; holding it is what makes
    a subscribe pass run against a quiet account. ``__exit__`` removes the file
    whatever happened, so an engine that raises still releases the daemon.

    ``db_path`` is optional but should be given by a caller that has it: the
    interval the lock is held for is recorded beside the database so the drain
    estimate can subtract paused time (``src/activity.py``). The file's own
    absent/present edge is the signal, so this nests under the TUI screen's lock
    without opening a second interval.
    """

    def __init__(self, path: str, db_path: str | None = None,
                 source: str = "subscribe_engine"):
        self.path = path
        self.db_path = db_path
        self.source = source

    def __enter__(self):
        activity.begin_pause(self.path, self.db_path, source=self.source)
        return self

    def __exit__(self, exc_type, exc, tb):
        activity.end_pause(self.path, self.db_path)
        return False


def run_subscription_pass(items, *, config: dict, db_path: str,
                          pause_lock_file: str, on_result=None,
                          keep_running=None) -> list:
    """Subscribe every queued ``item``, holding the pause for the pass.

    The lock is taken before the first item and released in a ``finally`` --
    via :class:`PauseLock` -- so an exception from the engine cannot leave the
    daemon paused forever. ``on_result`` is called after each outcome for a live
    progress display; it must not raise (the pass does not catch it).

    One :class:`WebInterval` is built for the whole pass and threaded through
    every item, so each item's page reads are spaced by the shared interval
    and the delay the pass learns is persisted once for the daemon to pick up.
    It is built *after* the pause is taken, so recording the pause interval (a
    small state-file write) is not measured as the first read's elapsed time and
    charged against the shared delay.
    """
    outcomes = []
    with PauseLock(pause_lock_file, db_path=db_path):
        interval = WebInterval(config, keep_running=keep_running)
        for item in items:
            outcome = subscribe_item(
                item["workshop_id"], config=config, db_path=db_path,
                interval=interval)
            outcomes.append(outcome)
            if on_result is not None:
                on_result(outcome)
    return outcomes
