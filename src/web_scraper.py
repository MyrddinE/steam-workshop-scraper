from requests_html import HTMLSession
import requests
import re
import sys
import threading
import time
import requests.utils
import logging
from src.config import load_config, login_secure_value
from src.firefox_cookies import browser_cookies, firefox_version

_last_web_call = 0.0
_WEB_DELAY = 5.0

# The selectors scrape_extended_details depends on. Named so a selector miss can
# be captured against the exact selector that failed.
DESCRIPTION_SELECTOR = '.workshopItemDescription#highlightContent'

# requests_html ships a macOS Safari string from around 2017. Steam serves the
# anonymous shell to it even when a valid login cookie is present, so every
# scrape came back as a ~325 KB generic page with no item markup.
#
# Measured against a HAR of a signed-in load: the same URL and the same cookie
# return the real item page with a browser User-Agent and the generic page
# without one. The cookie was never the problem.
#
# The cookies come from a Firefox profile, so the User-Agent must be Firefox:
# a Chrome string beside Firefox cookies is itself a contradiction a bot
# detector can read. The version is taken from the profile that supplies the
# cookies when `compatibility.ini` is readable, so the claim tracks the browser
# actually signed in, and falls back to a recent stable Firefox when there is no
# readable profile. Only the version is derived; the platform in the string is
# the one the HAR captured, because `compatibility.ini` records no platform.
FIREFOX_USER_AGENT_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0"
)


def _firefox_user_agent() -> str:
    """A Firefox User-Agent whose version comes from the profile when possible."""
    version = firefox_version()
    if not version:
        return FIREFOX_USER_AGENT_FALLBACK
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}) "
        f"Gecko/20100101 Firefox/{version}"
    )


USER_AGENT = _firefox_user_agent()


def _module_available(name: str) -> bool:
    """Whether a codec module can actually be imported in this interpreter."""
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def _accept_encoding() -> str:
    """`Accept-Encoding` restricted to codecs this interpreter can decode.

    `gzip` and `deflate` are always available. `br` and `zstd` are advertised
    only when their decoder is importable, because offering a codec nothing here
    can decode would leave compressed bytes in `response.text` -- a body that
    looks broken for a reason unrelated to the page. No new dependency is added;
    this reports what the environment already has.
    """
    encodings = ["gzip", "deflate"]
    if _module_available("brotli") or _module_available("brotlicffi"):
        encodings.append("br")
    if _module_available("zstandard"):
        encodings.append("zstd")
    return ", ".join(encodings)


# The headers a Firefox top-level navigation sends, measured from a HAR of a
# real, successful item-page load. Both request sites use this one mapping so
# they cannot drift apart.
#
#   * Content negotiation (`Accept`, `Accept-Language`): a navigation asks for
#     HTML in a human language. `requests` defaults `Accept` to `*/*`, which no
#     browser navigation ever sends; it reads as a client that does not care
#     what it gets, and it is the single clearest giveaway that the request is a
#     script rather than a page load.
#   * Fetch metadata (`Sec-Fetch-*`): Firefox attaches these to every request,
#     and their complete absence is a strong non-browser signal. `Sec-Fetch-Site`
#     is `none` for a direct navigation, which is what this scraper does -- it
#     fetches a URL itself, with no referring page -- so `none` is the honest
#     value here, not the `cross-site` the capture shows (that navigation was
#     launched from a local page, which is a different request).
#   * Navigation hints (`Upgrade-Insecure-Requests`, `Priority`): sent for a
#     top-level navigation and harmless when there is nothing to upgrade.
#   * Reuse (`Connection`): asks the server to keep the connection open, which
#     is only worth saying because the shared session below reuses it.
#
# `Accept-Encoding` is built from the codecs this interpreter can decode rather
# than copied from the capture, which advertised `br` and `zstd` that may not be
# importable here; see `_accept_encoding`. `Referer` is deliberately absent: a
# direct fetch has no referring page, and inventing one would claim a navigation
# this request did not make.
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": _accept_encoding(),
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Priority": "u=0, i",
}

# One session for every request, so the TCP connection and its TLS handshake are
# reused the way a browser reuses them; building a session per call forced a
# fresh handshake each time, which is itself a non-browser signal.
#
# Created lazily under a lock. `requests.Session` is not documented as
# thread-safe, but it is safe for concurrent use in practice: it keeps no
# per-request state on the session beyond the cookie jar, and this code passes
# cookies and headers per call. The lock therefore guards only construction, so
# two worker threads cannot both build a session.
#
# The cached instance is keyed to the class it was built from. In production
# that class never changes, so this is one instance for the process lifetime; it
# also means replacing `HTMLSession` (as a test does) yields the replacement
# rather than an instance of the class it replaced.
_session_lock = threading.Lock()
_session = None
_session_built_from = None


def _get_session() -> HTMLSession:
    """The shared session, created on first use."""
    global _session, _session_built_from
    factory = HTMLSession
    if _session is None or _session_built_from is not factory:
        with _session_lock:
            if _session is None or _session_built_from is not factory:
                _session = factory()
                _session_built_from = factory
    return _session


# Markers of a page Steam withheld rather than one whose layout changed: an error
# page, an age check, or a sign-in wall. Used only for a body that has already
# failed to yield the item template, so the "Sign In" link every normal page
# carries cannot trigger it.
_GATE_MARKERS = (
    "Steam Community :: Error",
    "AgeCheck", "agecheck", "age_gate",
    "apphub_Login", "Please sign in",
)
TAGS_SELECTOR = '.workshopTags a'


def set_web_delay(seconds: float):
    global _WEB_DELAY
    _WEB_DELAY = seconds


def _rate_limit():
    global _last_web_call
    elapsed = time.time() - _last_web_call
    if 0 < elapsed < _WEB_DELAY:
        time.sleep(_WEB_DELAY - elapsed)
    _last_web_call = time.time()


# Steam answers an over-budget request with HTTP 200 and its ordinary Workshop
# shell carrying this wording, so the status code cannot be trusted and the
# request looks exactly like a content miss.
_RATE_LIMIT_MARKERS = ("too many requests", "you have made too many requests")


def looks_rate_limited(body: str) -> bool:
    """Whether Steam served its throttle page rather than the item.

    Worth separating from a miss: the item is fine and only the budget is spent,
    so decaying the item is wrong and retrying immediately is worse.
    """
    if not body:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def looks_signed_out(body: str) -> bool:
    """Whether the page was served to an anonymous visitor.

    The header carries an account dropdown once Steam recognises a session, so
    its absence is the most direct sign that the request was anonymous — which is
    what a stale login cookie produces, and what the captured pages showed:
    present in every signed-in capture, absent in all of the anonymous ones.
    `g_steamID` is the cross-check, since it is `false` when signed out.
    """
    if not body:
        return False
    if "account_pulldown" in body:
        return False
    match = re.search(r"g_steamID\s*=\s*\"?([^\";\s]+)", body, re.IGNORECASE)
    return match is None or match.group(1).lower() == "false"


def looks_gated(body: str) -> bool:
    """Whether a failed scrape looks like Steam withholding the page.

    A gated page and a changed layout both present as "the selector did not
    match", but only one of them can be fixed by a fresher login cookie. This is
    a heuristic on purpose: it decides whether re-reading the cookie is worth a
    file copy, not whether the answer is right.
    """
    if not body:
        return False
    if "workshopItem" in body or "highlightContent" in body:
        return False
    return any(marker in body for marker in _GATE_MARKERS)


def _session_id(config: dict) -> str:
    """The CSRF token, from the browser when that source is enabled.

    Taken from the same read as the login cookie on purpose: a sessionid from a
    different session than the credential is worse than none, because Steam
    rejects the mismatch in a way that looks like an ordinary failure.
    """
    if config.get("session", {}).get("read_firefox_cookies"):
        value = browser_cookies().get("sessionid")
        if value:
            return value
    return config.get("session", {}).get("id", "") or ""


def _resolve_login_secure(config: dict) -> str:
    """The login cookie: the browser's own store when enabled, else the config.

    Firefox keeps `steamLoginSecure` in plaintext and the daemon runs as the user
    who owns that profile, so the browser is both authoritative and
    self-updating — it is the copy the operator is actually using. The configured
    value is the fallback: it goes stale and nothing refreshes it.

    Reading another application's credential store is a deliberate choice, so it
    happens only when `session.read_firefox_cookies` is set. When it is enabled
    and nothing is found, the configured value still applies, and the lookup
    logs why it came up empty.
    """
    if config.get("session", {}).get("read_firefox_cookies"):
        cookie = browser_cookies().get("steamLoginSecure")
        if cookie:
            return cookie
    return login_secure_value(config)


def _build_workshop_cookies(config: dict) -> dict:
    """Cookies for Steam Workshop requests, matching what the browser holds.

    With `session.read_firefox_cookies` enabled this sends the profile's whole
    steamcommunity.com cookie set, exactly as Firefox does. The old code kept
    two hand-picked names and hardcoded a third; a real navigation sends all ten
    the HAR capture shows, and the extra names -- timezone, `steamCountry`,
    `browserid` -- are part of what a signed-in browser actually presents.
    Nothing is invented: a name the profile does not have is simply not sent.

    `steamLoginSecure` is the cookie that authenticates the session --
    `sessionid` is a CSRF token and does nothing on its own, which the captures
    confirmed: a current sessionid beside a dead credential still fetched
    anonymously. Without it every request is anonymous, so items Steam only
    serves to signed-in users come back as an error page or an age check rather
    than the item.

    When the Firefox source is off, the configured `sessionid` and
    `login_secure` are used instead, so an anonymous or config-driven install
    sends exactly what it did before, and the login cookie is omitted entirely
    when unset.
    """
    if config.get("session", {}).get("read_firefox_cookies"):
        profile_cookies = browser_cookies()
        if profile_cookies:
            return dict(profile_cookies)
        # The source is enabled but empty: no profile, or a profile with no
        # Steam cookies. Fall through to the configured values rather than
        # sending nothing, which is what this source has always meant; no
        # profile cookie is invented here.
    cookies = {
        'workshop_preferences_v2': '%7B%22bOptedIn%22%3Atrue%7D',
        'sessionid': _session_id(config),
    }
    login_secure = _resolve_login_secure(config)
    if login_secure:
        cookies['steamLoginSecure'] = login_secure
    return cookies

def _build_browse_url_params(appid: int, start_date: int, end_date: int, page: int,
                              search_text: str = "", required_tags: list[str] = None,
                              excluded_tags: list[str] = None,
                              appids_required_for_use: list[int] = None) -> list[str]:
    """Builds query parameters for the Steam Workshop browse page."""
    params = [
        f"appid={appid}", "browsesort=mostrecent", "section=readytouseitems",
        f"p={page}",
        f"updated_date_range_filter_start={start_date}",
        f"updated_date_range_filter_end={end_date}"
    ]
    if search_text:
        params.append(f"searchtext={requests.utils.quote(search_text)}")
    if required_tags:
        for tag in required_tags:
            params.append(f"requiredtags[]={requests.utils.quote(tag)}")
    if excluded_tags:
        for tag in excluded_tags:
            params.append(f"excludedtags[]={requests.utils.quote(tag)}")
    if appids_required_for_use:
        for rid in appids_required_for_use:
            params.append(f"appids_required_for_use[]={rid}")
    return params

def _extract_item_ids_from_page(response) -> list[int]:
    """Extracts workshop item IDs from a Steam Workshop browse page response.
    Tries SSR JSON blob first, falls back to HTML hrefs."""
    ids = []
    item_id_pattern = re.compile(r'\\\"publishedfileid\\\":\\\"(\d+)\\\"')
    matches = item_id_pattern.findall(response.text)
    if not matches:
        item_id_pattern = re.compile(r'"publishedfileid":"(\d+)"')
        matches = item_id_pattern.findall(response.text)
    for item_id in matches:
        ids.append(int(item_id))
    if not ids:
        links = response.html.find('a[href*="sharedfiles/filedetails/?id="]')
        for link in links:
            href = link.attrs.get('href', '')
            match = re.search(r'id=(\d+)', href)
            if match:
                ids.append(int(match.group(1)))
    return list(set(ids))

def _extract_total_pages(response) -> int:
    """Extracts total_pages from a Steam Workshop browse page SSR JSON."""
    page_pattern = re.compile(r'\\\\\\\"total_pages\\\\\\\":(\d+)')
    page_match = page_pattern.search(response.text)
    return int(page_match.group(1)) if page_match else 1

def _workshop_cookies_or_empty() -> dict:
    """Cookies for the current config, or none if it cannot be read.

    Read per call rather than cached: the login cookie is short-lived and is
    refreshed by the userscript pushing to `/api/sessionid`, which persists it.
    Caching it for the process lifetime would mean a refreshed cookie never took
    effect until the daemon restarted. An unreadable config yields an anonymous
    request rather than a failed scrape.
    """
    try:
        return _build_workshop_cookies(load_config("config.yaml"))
    except Exception as exc:
        logging.debug("Scraping without cookies: %s", exc)
        return {}


def scrape_extended_details(item_url: str, keep_body: bool = False) -> dict | None:
    """
    Scrapes the extended description and tags from a Steam Workshop page.

    Returns None when the request itself failed. When the request succeeded but
    the description selector did not match, returns a dict whose "description" is
    None - the caller must treat that as a miss, not as a completed scrape, and
    "body" carries the response so it can be captured as evidence.
    """
    session = _get_session()
    _rate_limit()
    try:
        response = session.get(item_url, timeout=10, cookies=_workshop_cookies_or_empty(),
                               headers=BROWSER_HEADERS)
        response.raise_for_status()

        description_element = response.html.find(DESCRIPTION_SELECTOR, first=True)
        description = description_element.text if description_element else None

        tag_elements = response.html.find(TAGS_SELECTOR)
        tags = [tag.text for tag in tag_elements] if tag_elements else []

        return {
            "description": description,
            "tags": tags,
            # Retained only on a miss: the caller captures it, and there is no
            # reason to carry a few hundred KB of HTML around on the happy path.
            # Kept on a miss, and on demand: the scrape capture needs the page
            # even when it worked, to see the signed-in markup in the header.
            "body": response.text if (description is None or keep_body) else None,
            "http_status": response.status_code,
            "final_url": str(getattr(response, "url", item_url)),
        }
    except requests.exceptions.RequestException:
        return None

def discover_items_by_date_html(appid: int, start_date: int, end_date: int, page: int = 1, search_text: str = "", required_tags: list[str] = None, excluded_tags: list[str] = None, appids_required_for_use: list[int] = None) -> tuple[list[int], int]:
    """
    Scrapes the Steam Workshop browse page using date filters.
    Returns a tuple of (list_of_ids, total_pages).
    """
    try:
        config = load_config("config.yaml")
    except FileNotFoundError:
        logging.error("Configuration file not found: config.yaml")
        sys.exit(1)

    cookies = _build_workshop_cookies(config)
    url_params = _build_browse_url_params(appid, start_date, end_date, page,
        search_text=search_text, required_tags=required_tags,
        excluded_tags=excluded_tags, appids_required_for_use=appids_required_for_use)
    url = "https://steamcommunity.com/workshop/browse?" + "&".join(url_params)
    logging.info(url)

    session = _get_session()
    try:
        response = session.get(url, cookies=cookies, timeout=15,
                               headers=BROWSER_HEADERS)
        response.raise_for_status()
        ids = _extract_item_ids_from_page(response)
        total_pages = _extract_total_pages(response)
        return ids, total_pages
    except Exception:
        logging.warning("Page discovery for appid %s failed", appid)
        return [], -1
