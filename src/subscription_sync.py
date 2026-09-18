r"""Reconcile the owner's Steam Workshop subscriptions into the local database.

Steam exposes no endpoint that lists an account's subscriptions in a form this
project may call: ``ISteamRemoteStorage/EnumerateUserSubscribedFiles`` is
publisher-key-only, and ``lifetime_subscriptions`` is an item-wide count that
cannot be attributed to an account. The one page that *does* list them is the
signed-in Workshop page, and the shape below was established by a read-only
probe against it:

* ``GET /my/myworkshopfiles/?appid=<appid>&browsefilter=mysubscriptions&p=<page>``
  returns the owner's subscriptions for that appid, ten per page.
* The page states its own total ("Showing 1-10 of 208"), and item ids appear as
  ``filedetails/?id=(\d+)``.
* A control probe with ``browsefilter=myfiles`` returned zero ids, which is what
  proves these are subscriptions rather than the account's own publications.
* The ``/my/`` form needs no steamid (it redirects to the profile). A
  ``/profiles/<steamid>/...`` form also works, and the steamid is derivable from
  the ``steamLoginSecure`` cookie, whose value is ``<steamid>||<token>``; see
  :mod:`src.session_cookie` for that value's shape and its expiry.
* **The cookie expires daily.** A request made with a stale cookie is not an
  error: Steam answers it with a 200 and its sign-in page. That page is
  recognised and reported as a failed session rather than as a page whose markup
  did not parse, because the two call for different responses.

That page is the only source, so this is a *reconcile*, not a history: it can
say "the owner is subscribed right now", and it can stamp the first time we
observed that, but it can never recover a subscription that ended before this
shipped. ``own_first_subscribed_at`` therefore means "first seen by us".

**A short read must not be believed.** Unsubscribing while the pages are being
walked renumbers them: an item can shift from page 3 to page 2 after page 2 was
already fetched, so it is never seen. The page's declared total is the check --
pagination is complete only when the collected set reaches it -- and a read that
does not reach the total is retried from page one rather than accepted, because
accepting it would clear ``own_subscribed`` off items that are in fact still
subscribed.

**No network in tests.** Every request here goes through the project's existing
authenticated session (``src.web_scraper._get_session`` and
``_build_workshop_cookies``), which is what the tests mock; nothing in this
module builds a session or a cookie of its own.

**The walk is paced.** Each page is a page load, so it waits the web scraper's
adaptive interval (``daemon.web_delay_seconds``, read fresh through
``configured_web_delay``) before the request. It only waits -- it does not move
the shared delay, because a reconcile is not a rate-seeking queue.
"""

from __future__ import annotations

import logging
import math
import re
import time

from src import capture, pacing, session_cookie, session_health, web_scraper
from src.database import apply_own_subscriptions
from src.web_worker import configured_web_delay

# The page's own URL shape. `/my/` is used rather than `/profiles/<steamid>/`
# because it needs no steamid, and the configured saved cookie is what makes it
# resolve to the owner.
SUBSCRIPTIONS_URL = (
    "https://steamcommunity.com/my/myworkshopfiles/"
    "?appid={appid}&browsefilter=mysubscriptions&p={page}"
)

# The page shows ten items at a time, so this is how the declared total maps to
# a page count. Named rather than inlined so the arithmetic is testable.
ITEMS_PER_PAGE = 10

# How many times a read that came up short of the declared total is retried from
# page one before it is given up on. One retry covers the ordinary case (an
# unsubscribe renumbered a page mid-walk); the rest guard a genuinely busy
# account. A failed reconcile changes nothing, so the cost of a retry is a few
# page fetches, and the cost of accepting a short read is a wrong marker.
MAX_ATTEMPTS = 3

# A page cap, so a nonsensical declared total (or a page that never repeats its
# own wording) cannot make the loop unbounded. Well above the observed 208/10.
MAX_PAGES = 1000

_ITEM_ID_PATTERN = re.compile(r"filedetails/\?id=(\d+)")

# "Showing 1-10 of 208" -- the page's own statement of how many there are. The
# count may carry thousands separators, which are stripped before parsing.
_SHOWING_PATTERN = re.compile(
    r"Showing\s+[\d,]+\s*-\s*[\d,]+\s+of\s+([\d,]+)"
)


def parse_item_ids(body: str) -> list[int]:
    """The item ids on a subscriptions page, in page order and without repeats.

    Steam renders each entry as a ``filedetails/?id=<id>`` link; the pattern is
    the one the probe observed, and duplicates (a link rendered more than once
    per row) are collapsed so the total check counts distinct items.
    """
    seen = []
    for raw in _ITEM_ID_PATTERN.findall(body or ""):
        item_id = int(raw)
        if item_id not in seen:
            seen.append(item_id)
    return seen


def parse_declared_total(body: str) -> int | None:
    """The total the page states, or ``None`` when it states none.

    ``None`` is deliberately distinct from ``0``: a page that says nothing about
    its size (a sign-in wall, an error page) cannot be verified, and the caller
    must not read "we could not tell" as "there are none".
    """
    match = _SHOWING_PATTERN.search(body or "")
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def page_count(declared_total: int) -> int:
    """How many pages a declared total implies at :data:`ITEMS_PER_PAGE`."""
    return max(1, math.ceil(declared_total / ITEMS_PER_PAGE))


class SignInRequired(RuntimeError):
    """Steam answered with its sign-in page instead of the requested page.

    Raised rather than logged at the point of the request so the caller can stop
    the whole walk: every later page would be the same sign-in page, and a
    sign-in page is not evidence about anyone's subscriptions.
    """


# Steam sends a signed-out request for any signed-in page here. The path is the
# reliable signal -- the response status is 200, and the page is a full-size
# document, so neither the status nor the length distinguishes it.
SIGN_IN_PATH = "/login/"


def _fetch_page(appid: int, page: int, config: dict, keep_running=None) -> str:
    """Fetch one subscriptions page and return its body.

    Raises :class:`SignInRequired` when the response is Steam's sign-in page,
    which is what an expired or revoked login cookie produces. Raises on a
    transport error or a non-2xx status: a page that did not arrive is not
    evidence about the owner's subscriptions, and the caller converts the raise
    into a failed sync rather than a cleared flag.

    This is a page load, so it honours the web scraper's adaptive interval
    (``daemon.web_delay_seconds``, read fresh from ``config``) before the
    request, exactly as an item-page read does. The walk is not a rate-seeking
    queue, so it only waits; it does not move the shared delay.

    The whole exchange is captured under the web-download debug switch, before
    the sign-in check: a sign-in page is exactly what the capture is for, and
    ``appid`` and ``page`` are what tell one capture from another. The request
    recorded is the one built here and handed to the session, so it is what was
    actually sent rather than a second guess at it.
    """
    url = SUBSCRIPTIONS_URL.format(appid=appid, page=page)
    session = web_scraper._get_session()
    cookies = web_scraper._build_workshop_cookies(config)
    headers = web_scraper.BROWSER_HEADERS
    pacing.wait(configured_web_delay(config),
                keep_running or (lambda: True))
    response = session.get(
        url,
        cookies=cookies,
        headers=headers,
        timeout=15,
    )
    if capture.web_download_capture_active():
        capture.record_web_download(
            capture.SUBSCRIPTIONS_PAGE_KIND, None, url,
            {
                "request": {"method": "GET", "url": url, "headers": headers,
                            "cookies": cookies, "data": None},
                "http_status": getattr(response, "status_code", None),
                "final_url": getattr(response, "url", "") or "",
                "response_headers": getattr(response, "headers", None),
                "body": response.text,
            },
            appid=appid, page=page,
        )
    response.raise_for_status()
    final_url = getattr(response, "url", "") or ""
    if SIGN_IN_PATH in final_url:
        raise SignInRequired(
            f"the request for page {page} was redirected to Steam's sign-in page"
        )
    return response.text or ""


def collect_subscribed_ids(appid: int, config: dict,
                           keep_running=None) -> tuple[set[int], int | None]:
    """Walk the subscriptions pages and return ``(ids, declared_total)``.

    Paging stops once the collected set reaches the declared total. A page that
    yields no ids, or a total that is never reached before :data:`MAX_PAGES`,
    ends the walk early: the caller compares the result with the total and
    decides whether the read is complete. This function does not itself decide
    -- it reports what it saw and what the page claimed.
    """
    collected: set[int] = set()
    declared_total: int | None = None
    pages_fetched = 0

    page_number = 1
    while page_number <= MAX_PAGES:
        pages_fetched += 1
        body = _fetch_page(appid, page_number, config, keep_running=keep_running)
        if declared_total is None and pages_fetched == 1:
            # Only the first page's total is trusted: later pages repeat it, and
            # a number read off a differently-shaped page mid-walk would be a
            # second, contradictory claim about the same list.
            declared_total = parse_declared_total(body)
        page_ids = parse_item_ids(body)
        if not page_ids:
            # An empty page is the end of the list as far as the markup says.
            break
        collected.update(page_ids)
        if declared_total is not None and len(collected) >= declared_total:
            break
        page_number += 1

    # A total of 0 is readable and means "none"; only a total we could not read
    # at all is reported as unknown, because the caller must not read "we could
    # not tell" as "there are none".
    if declared_total == 0 and collected:
        declared_total = None

    if declared_total is not None and pages_fetched < page_count(declared_total):
        logging.warning(
            "Subscription page walk for appid %s stopped after %d of the %d pages a "
            "declared total of %d implies.",
            appid, pages_fetched, page_count(declared_total), declared_total,
        )

    return collected, declared_total


def reconcile_own_subscriptions(db_path: str, appid: int, config: dict,
                                seen_at: int | None = None,
                                keep_running=None) -> dict | None:
    """Bring this app's ``own_subscribed`` flags in line with Steam.

    Returns the counters from ``apply_own_subscriptions``. The ``cleared`` count
    is non-zero only for a verified complete read; every other outcome applies
    nothing but the one-way facts.

    Completeness is established against the page's declared total: a read is
    verified when the collected set reaches it. A short read is retried from page
    one (unsubscribing mid-walk renumbers the pages), and a read whose total
    cannot be read at all is not verifiable either. When no attempt verifies, the
    collected ids are still applied with ``complete=False`` -- they were returned
    as the owner's subscriptions, so marking them subscribed cannot be wrong --
    but nothing is cleared, because a list we could not verify is no evidence
    about the items it omits. Returns ``None`` only when nothing at all was
    learned, in which case the database was not touched.

    A dead login cookie is refused before the first request rather than
    discovered from the third: the token states its own expiry, so a reconcile
    that cannot authenticate says so and stops, and a sign-in page that arrives
    anyway (a revoked session, an unreadable token) ends the walk on the spot.
    Either way the reason is recorded through :mod:`src.session_health`, which is
    what lets the web UI tell the operator instead of leaving them to notice that
    nothing has a star.

    ``seen_at`` is this run's clock: it stamps the one-way facts, and it is the
    instant the login cookie is judged against, so a caller replaying a past
    reconcile gets that reconcile's verdict rather than today's.

    Never raises into a scrape loop; a failure is a log line. The one exception
    is a programming error (a bad ``config``), which is left to surface in tests
    rather than swallowed.
    """
    if seen_at is None:
        seen_at = int(time.time())

    login = web_scraper._resolve_login_secure(config)
    if not login:
        # An anonymous request returns the sign-in shell, whose total cannot be
        # verified. Say so once rather than fetching pages that cannot succeed.
        logging.warning(
            "Subscription reconcile for appid %s skipped: no login cookie is configured.",
            appid,
        )
        return None

    cookie = session_cookie.parse(login)

    reason = session_health.evaluate_login(login, now=seen_at)
    if reason:
        # The token says it is dead, so the walk cannot succeed: Steam answers
        # every page with its sign-in shell. Declining here costs nothing and
        # names the reason, where fetching would spend three attempts to learn
        # only that the page has no declared total. The same sentence is what the
        # web UI shows, so it has to stand on its own.
        session_health.record_rejected(db_path, reason, now=seen_at)
        logging.warning(
            "Subscription reconcile for appid %s skipped: %s. Steam answers an expired "
            "cookie with its sign-in page, so no page was requested and no flag was "
            "changed. Sign in to Steam in the browser the daemon reads cookies from, "
            "then restart the daemon so it picks the refreshed cookie up.",
            appid, reason,
        )
        return None

    best: tuple[set[int], int | None] | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            ids, declared_total = collect_subscribed_ids(
                appid, config, keep_running=keep_running)
        except SignInRequired as exc:
            # Retrying cannot help and would only ask Steam again, so the walk
            # ends here. Nothing is applied: a sign-in page is not a list. The
            # local expiry was readable and still in the future, so this is the
            # case that proves Steam's answer has to be recorded, not just the
            # token's.
            session_health.record_rejected(db_path, session_health.NOT_ACCEPTED_DETAIL,
                                           now=seen_at)
            logging.warning(
                "Subscription reconcile for appid %s stopped: %s. No flag was changed. "
                "Sign in to Steam in the browser the daemon reads cookies from, then "
                "restart the daemon so it picks the refreshed cookie up.",
                appid, exc,
            )
            return None
        except Exception as exc:
            logging.warning(
                "Subscription reconcile for appid %s failed on attempt %d/%d: %s",
                appid, attempt, MAX_ATTEMPTS, exc,
            )
            continue

        if best is None or len(ids) > len(best[0]):
            best = (ids, declared_total)

        if declared_total is None:
            logging.warning(
                "Subscription reconcile for appid %s could not read the page's declared "
                "total (attempt %d/%d); the page may be a sign-in wall or an error page.",
                appid, attempt, MAX_ATTEMPTS,
            )
            continue

        # A page that states its own total is a signed-in page -- the wording is
        # rendered for the account -- so whatever problem was recorded earlier is
        # over. Cleared here rather than only on a complete read because this is
        # the first point where the session is demonstrably working.
        session_health.record_accepted(db_path)

        if len(ids) < declared_total:
            # Unsubscribing mid-walk renumbers the pages, so a short read is
            # expected occasionally and must be retried, never accepted: the
            # missing ids are exactly the ones that would be wrongly cleared.
            logging.warning(
                "Subscription reconcile for appid %s read %d of a declared %d on attempt "
                "%d/%d (a short read can mean a page was renumbered while we walked it); "
                "retrying from page 1.",
                appid, len(ids), declared_total, attempt, MAX_ATTEMPTS,
            )
            continue

        counts = apply_own_subscriptions(db_path, appid, ids, seen_at=seen_at)
        logging.info(
            "Subscription reconcile for appid %s (steamid %s): %d subscribed, %d newly "
            "stamped, %d unstamped, %d queued flags cleared, %d downloaded latches cleared.",
            appid, cookie.steamid or "unknown", counts["subscribed"], counts["stamped"],
            counts["cleared"], counts["queued_cleared"], counts["downloads_cleared"],
        )
        return counts

    if best is not None and best[0]:
        logging.warning(
            "Subscription reconcile for appid %s could not verify a complete list after %d "
            "attempts; applying the %d ids that were seen as subscribed but clearing nothing, "
            "so items omitted by the short reads keep their current state.",
            appid, MAX_ATTEMPTS, len(best[0]),
        )
        return apply_own_subscriptions(db_path, appid, best[0], seen_at=seen_at, complete=False)

    logging.warning(
        "Subscription reconcile for appid %s learned nothing after %d attempts; no flags "
        "were changed, so the previous reconcile's state stands.",
        appid, MAX_ATTEMPTS,
    )
    return None
