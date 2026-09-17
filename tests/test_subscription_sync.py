"""The reconcile of the owner's Steam subscriptions, with every page mocked.

The shape this walks was established by a read-only probe and is not re-derived
here: a subscriptions page shows ten ids per page and states its own total, and
the ``/my/`` URL needs no steamid. What these tests pin is the behaviour that
makes the result trustworthy:

* paging reaches the declared total rather than stopping at the first page;
* a read that comes up short of the declared total is **detected and retried**,
  never applied -- unsubscribing while the pages are walked renumbers them, so a
  short read would otherwise clear the flag off items that are still subscribed;
* a failed sync changes nothing and never raises into the scrape loop;
* first-seen stamping only stamps the first time, and a queue flag on an item
  found subscribed is cleared.
"""

import base64
import json
import logging

import pytest

from src import session_health, subscription_sync
from src.database import (
    apply_own_subscriptions, get_connection, initialize_database,
    insert_or_update_item, mark_own_subscribed, own_subscription_ids,
)


def _page(ids, declared_total):
    """A subscriptions page body with the ids and the stated total the probe saw."""
    rows = "".join(
        f'<div class="workshopItem"><a href="https://steamcommunity.com'
        f'/sharedfiles/filedetails/?id={wid}"></a></div>'
        for wid in ids
    )
    return (f"<html><body>{rows}"
            f'<div class="workshopItemTitle">Showing 1-{len(ids)} of {declared_total}'
            f" entries</div></body></html>")


class _FakeResponse:
    """A response whose final URL is what the redirect check reads.

    Steam answers a signed-out request with a 200 and the sign-in page, so the
    final URL -- not the status -- is what tells the two apart.
    """

    def __init__(self, text, url=""):
        self.text = text
        self.url = url

    def raise_for_status(self):
        return None


# What Steam returns, as a URL and a body, when the login cookie is not accepted.
_SIGN_IN_URL = ("https://steamcommunity.com/login/home/"
                "?goto=%2Fmy%2Fmyworkshopfiles%2F")
_SIGN_IN_PAGE = ("<html><head><title>Sign In</title></head>"
                 "<body>Sign In</body></html>")


class _FakeSession:
    """A session that answers from a scripted page mapping and records the URLs."""

    def __init__(self, pages, sign_in=False):
        self.pages = pages
        self.sign_in = sign_in
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if self.sign_in:
            return _FakeResponse(_SIGN_IN_PAGE, url=_SIGN_IN_URL)
        page = int(url.rsplit("p=", 1)[1])
        answer = self.pages.get(page)
        if answer is None:
            return _FakeResponse(_page([], 0), url=url)
        if isinstance(answer, Exception):
            raise answer
        return _FakeResponse(answer, url=url)


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    """A database plus a patched scraper session, with the cookies/login stubbed.

    No request leaves the process: `_get_session` and `_build_workshop_cookies`
    are the project's own entry points and are replaced here, which is the only
    thing the module calls.
    """
    db_path = str(tmp_path / "sync.db")
    initialize_database(db_path)

    def configure(pages, login="76561198000000000||tok", sign_in=False):
        session = _FakeSession(pages, sign_in=sign_in)
        monkeypatch.setattr(subscription_sync.web_scraper, "_get_session", lambda: session)
        monkeypatch.setattr(subscription_sync.web_scraper, "_build_workshop_cookies",
                            lambda config: {"steamLoginSecure": login})
        monkeypatch.setattr(subscription_sync.web_scraper, "_resolve_login_secure",
                            lambda config: login)
        return session

    return db_path, configure


def _cookie(expires_at, steamid="76561198000000000", encoded=True):
    """A ``steamLoginSecure`` value whose token states an expiry.

    The token is a real three-segment shape -- header, payload, signature --
    because that is what the parser has to cope with; only the header and the
    signature are filler. ``%7C%7C`` is the form a browser stores and the config
    writes, and ``||`` is the form a hand-written config uses.
    """
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJub25lIn0.{payload}.c2ln"
    return f"{steamid}{'%7C%7C' if encoded else '||'}{token}"


def _items(db_path, *ids, appid=294100, **columns):
    for wid in ids:
        record = {"workshop_id": wid, "title": f"Item {wid}", "status": 200,
                  "consumer_appid": appid}
        record.update(columns)
        insert_or_update_item(db_path, record)


def _row(db_path, wid):
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM workshop_items WHERE workshop_id = ?", (wid,)).fetchone()
    conn.close()
    return dict(row)


# --- paging and the declared-total check ------------------------------------

def test_paging_reaches_every_page_and_stops_at_the_declared_total(sync_env):
    db_path, configure = sync_env
    ids = list(range(1000, 1025))          # 25 items over three pages of ten
    _items(db_path, *ids)
    session = configure({1: _page(ids[0:10], 25), 2: _page(ids[10:20], 25),
                         3: _page(ids[20:25], 25)})

    counts = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert counts["subscribed"] == 25
    # Three pages fetched, and no fourth: the declared total ends the walk.
    assert len(session.urls) == 3
    assert own_subscription_ids(db_path, 294100) == set(ids)


def test_a_single_page_account_is_one_request(sync_env):
    db_path, configure = sync_env
    ids = list(range(2000, 2005))
    _items(db_path, *ids)
    session = configure({1: _page(ids, 5)})

    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert len(session.urls) == 1
    assert own_subscription_ids(db_path, 294100) == set(ids)


def test_every_other_item_of_the_appid_is_cleared(sync_env):
    """A full read says what the owner is *not* subscribed to, too."""
    db_path, configure = sync_env
    _items(db_path, 1, 2, 3, 4)
    apply_own_subscriptions(db_path, 294100, {1, 2, 3, 4})
    configure({1: _page([1, 2], 2)})

    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert own_subscription_ids(db_path, 294100) == {1, 2}
    assert _row(db_path, 3)["own_subscribed"] == 0
    assert _row(db_path, 4)["own_subscribed"] == 0


def test_the_url_is_the_my_form_with_the_subscriptions_filter(sync_env):
    db_path, configure = sync_env
    _items(db_path, 1)
    session = configure({1: _page([1], 1)})

    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    url = session.urls[0]
    assert url.startswith("https://steamcommunity.com/my/myworkshopfiles/?")
    assert "appid=294100" in url
    assert "browsefilter=mysubscriptions" in url
    assert "p=1" in url


# --- the short read ---------------------------------------------------------

def test_a_short_read_is_retried_and_never_clears(sync_env, monkeypatch):
    """The declared total is the check: 19 of 20 must not be believed.

    An unsubscribe mid-walk renumbers the pages, so an item can be skipped. The
    missing id is exactly the one that would be wrongly cleared, so a short read
    is retried from page one, and when it stays short the items it *did* see are
    recorded as subscribed while nothing at all is cleared.
    """
    db_path, configure = sync_env
    _items(db_path, *range(1, 21))
    # A pre-existing subscription on id 20 that the short read never reaches.
    apply_own_subscriptions(db_path, 294100, {20})
    session = configure({})

    def get(url, **kwargs):
        session.urls.append(url)
        return _FakeResponse({1: _page(list(range(1, 11)), 20),
                              2: _page(list(range(12, 21)), 20)}.get(
                                  int(url.rsplit("p=", 1)[1]), _page([], 20)))

    monkeypatch.setattr(session, "get", get)

    counts = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert counts is not None, "the ids that were seen are still a fact"
    assert counts["cleared"] == 0, "an unverified read must clear nothing"
    assert len(session.urls) > 2, "the walk must have been retried"
    # Every id the walk actually saw is marked, and the unseen one keeps its
    # state rather than being taken as unsubscribed.
    assert own_subscription_ids(db_path, 294100) == set(range(1, 11)) | set(range(12, 21))
    assert _row(db_path, 11)["own_subscribed"] == 0, \
        "an id never seen must not be marked subscribed"
    assert _row(db_path, 20)["own_subscribed"] == 1, \
        "an id the short read never reached must keep its state"


def test_a_short_read_that_clears_on_retry_is_applied(sync_env, monkeypatch):
    """The retry is what makes an occasional renumbering survivable."""
    db_path, configure = sync_env
    _items(db_path, *range(1, 21))
    session = configure({})
    attempts = {"count": 0}

    def get(url, **kwargs):
        session.urls.append(url)
        page = int(url.rsplit("p=", 1)[1])
        attempts["count"] += 1
        if attempts["count"] <= 2:          # first attempt: short (9 on page 2)
            body = {1: _page(list(range(1, 11)), 20),
                    2: _page(list(range(12, 21)), 20)}.get(page, _page([], 20))
        else:                                # second attempt: complete
            body = {1: _page(list(range(1, 11)), 20),
                    2: _page(list(range(11, 21)), 20)}.get(page, _page([], 20))
        return _FakeResponse(body)

    monkeypatch.setattr(session, "get", get)

    counts = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert counts is not None and counts["subscribed"] == 20
    assert own_subscription_ids(db_path, 294100) == set(range(1, 21))


def test_a_page_with_no_declared_total_is_not_treated_as_complete(sync_env):
    """`could not tell` must not mean `there are none`, and must not clear."""
    db_path, configure = sync_env
    _items(db_path, 1, 2)
    apply_own_subscriptions(db_path, 294100, {1, 2})
    # A sign-in wall shape: one id, no "Showing ... of N" wording.
    configure({1: '<html><body><a href="/sharedfiles/filedetails/?id=1"></a></body></html>'})

    counts = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    # The id is a real subscription and is recorded, but with no total to verify
    # against, the unread id 2 must not be cleared.
    assert counts["cleared"] == 0
    assert own_subscription_ids(db_path, 294100) == {1, 2}


# --- one-way safety ---------------------------------------------------------

def test_a_transport_error_never_raises_and_changes_nothing(sync_env):
    db_path, configure = sync_env
    _items(db_path, 1, 2)
    apply_own_subscriptions(db_path, 294100, {1, 2})
    configure({1: RuntimeError("connection reset")})

    result = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert result is None
    assert own_subscription_ids(db_path, 294100) == {1, 2}, \
        "a failed sync must leave the previous state standing"


def test_a_later_attempt_after_an_error_succeeds(sync_env, monkeypatch):
    db_path, configure = sync_env
    _items(db_path, 1, 2)
    session = configure({})
    attempts = {"count": 0}

    def get(url, **kwargs):
        session.urls.append(url)
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("connection reset")
        return _FakeResponse(_page([1, 2], 2))

    monkeypatch.setattr(session, "get", get)

    counts = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert counts is not None and counts["subscribed"] == 2


def test_no_login_cookie_skips_without_a_request(sync_env):
    db_path, configure = sync_env
    _items(db_path, 1)
    session = configure({1: _page([1], 1)}, login="")

    result = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert result is None
    assert session.urls == [], "an anonymous request cannot verify the total"


# --- the login cookie --------------------------------------------------------
#
# Steam issues `steamLoginSecure` with a lifetime of about a day, so the copy in
# the config is expected to go stale between runs. A stale cookie is answered
# with the sign-in page and a 200 -- not an error -- and a sign-in page is no
# evidence about anyone's subscriptions, so the walk must stop rather than
# conclude anything from it. Both facts below were read off the live deployment,
# where the reconcile failed exactly this way.

def test_an_expired_cookie_skips_before_making_a_request(sync_env):
    db_path, configure = sync_env
    _items(db_path, 1)
    session = configure({1: _page([1], 1)}, login=_cookie(expires_at=1000))

    result = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {},
                                                           seen_at=2000)

    assert result is None
    assert session.urls == [], "a token that says it is dead needs no round trip"


def test_a_skipped_reconcile_leaves_the_operator_a_reason(sync_env):
    """The daemon knows why nothing was fetched; the web UI has to be able to
    say it, or the failure looks exactly like having no subscriptions."""
    db_path, configure = sync_env
    _items(db_path, 1)
    configure({1: _page([1], 1)}, login=_cookie(expires_at=1000))

    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {}, seen_at=2000)

    problem = session_health.read(db_path)
    assert problem is not None
    assert problem["detail"].startswith("the saved login cookie expired")
    assert problem["detected_at"] == 2000


def test_a_working_login_clears_a_recorded_problem(sync_env):
    """The other half of the contract: a warning that cannot clear itself is
    worse than no warning."""
    db_path, configure = sync_env
    _items(db_path, 1)
    configure({1: _page([1], 1)})
    session_health.record_rejected(db_path, "the saved login cookie expired", now=1000)

    counts = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {},
                                                           seen_at=2000)

    assert counts is not None
    assert session_health.read(db_path) is None


def test_the_sign_in_page_leaves_the_operator_a_reason(sync_env):
    db_path, configure = sync_env
    _items(db_path, 1)
    configure({}, login=_cookie(expires_at=9_999_999_999), sign_in=True)

    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {}, seen_at=2000)

    problem = session_health.read(db_path)
    assert problem is not None
    assert "sign-in page" in problem["detail"]


def test_a_live_cookie_is_not_skipped(sync_env):
    """The gate must not refuse a cookie that is still good."""
    db_path, configure = sync_env
    _items(db_path, 1)
    session = configure({1: _page([1], 1)}, login=_cookie(expires_at=3000))

    counts = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {},
                                                           seen_at=2000)

    assert counts is not None and counts["subscribed"] == 1
    assert session.urls, "a live cookie must still be used"


def test_a_cookie_without_a_readable_expiry_is_still_tried(sync_env):
    """`cannot tell` must not be read as `expired`: refusing to try is worse."""
    db_path, configure = sync_env
    _items(db_path, 1)
    session = configure({1: _page([1], 1)}, login="76561198000000000||opaque")

    counts = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {},
                                                           seen_at=2000)

    assert counts is not None and counts["subscribed"] == 1
    assert session.urls


def test_the_sign_in_page_stops_the_walk_and_changes_nothing(sync_env):
    """Steam answers a revoked session with a 200 and the sign-in page."""
    db_path, configure = sync_env
    _items(db_path, 1, 2)
    apply_own_subscriptions(db_path, 294100, {1, 2})
    session = configure({}, sign_in=True)

    result = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert result is None
    assert len(session.urls) == 1, \
        "a sign-in page ends the walk; retrying only asks Steam again"
    assert own_subscription_ids(db_path, 294100) == {1, 2}, \
        "a sign-in page is not a list, so it may clear nothing"


def test_a_sign_in_redirect_is_detected_even_when_the_token_looks_live(sync_env):
    """The local expiry is a shortcut, never the authority: Steam decides."""
    db_path, configure = sync_env
    _items(db_path, 1)
    session = configure({}, login=_cookie(expires_at=9_999_999_999), sign_in=True)

    result = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {},
                                                           seen_at=2000)

    assert result is None
    assert len(session.urls) == 1


# --- first-seen stamping and the queue flag ---------------------------------

def test_the_first_seen_timestamp_is_stamped_once(sync_env):
    db_path, configure = sync_env
    _items(db_path, 1)
    configure({1: _page([1], 1)})

    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {}, seen_at=1000)
    assert _row(db_path, 1)["own_first_subscribed_at"] == 1000

    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {}, seen_at=2000)
    assert _row(db_path, 1)["own_first_subscribed_at"] == 1000, \
        "the sticky timestamp must not move on a later sync"


def test_an_unsubscribed_item_keeps_its_first_seen_timestamp(sync_env):
    """That stickiness is the only source of the `previously` state."""
    db_path, configure = sync_env
    _items(db_path, 1)
    configure({1: _page([1], 1)})
    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {}, seen_at=1000)

    configure({1: _page([], 0)})
    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {}, seen_at=2000)

    row = _row(db_path, 1)
    assert row["own_subscribed"] == 0
    assert row["own_first_subscribed_at"] == 1000


def test_a_stale_queue_flag_is_cleared_when_the_item_is_found_subscribed(sync_env):
    """There is nothing pending for an item that is already subscribed."""
    db_path, configure = sync_env
    _items(db_path, 1, 2, is_queued_for_subscription=1)
    configure({1: _page([1, 2], 2)})

    counts = subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert counts["queued_cleared"] == 2
    assert _row(db_path, 1)["is_queued_for_subscription"] == 0
    assert _row(db_path, 2)["is_queued_for_subscription"] == 0


def test_another_appids_items_are_untouched(sync_env):
    db_path, configure = sync_env
    _items(db_path, 1, appid=294100)
    _items(db_path, 2, appid=730)
    apply_own_subscriptions(db_path, 730, {2})
    configure({1: _page([1], 1)})

    subscription_sync.reconcile_own_subscriptions(db_path, 294100, {})

    assert _row(db_path, 2)["own_subscribed"] == 1, \
        "a reconcile for one app must not clear another app's flags"


# --- the page-syntax helpers ------------------------------------------------

def test_parse_item_ids_deduplicates_and_keeps_order():
    body = _page([7, 8, 7, 9], 3)
    assert subscription_sync.parse_item_ids(body) == [7, 8, 9]


def test_parse_declared_total_reads_the_thousands_separator():
    assert subscription_sync.parse_declared_total("Showing 1-10 of 1,234 entries") == 1234


def test_parse_declared_total_is_none_when_the_page_says_nothing():
    assert subscription_sync.parse_declared_total("<html>Sign In</html>") is None


def test_page_count_covers_a_partial_last_page():
    assert subscription_sync.page_count(208) == 21
    assert subscription_sync.page_count(10) == 1
    assert subscription_sync.page_count(0) == 1


def test_the_reported_steamid_comes_from_the_cookie_it_was_handed(sync_env, caplog):
    """The log names the account, in whichever encoding the cookie arrived.

    Both forms have to work: the config writes `%7C%7C` and a hand-written one
    uses `||`, and the steamid is only ever useful when it is read out of the
    value rather than mistaken for it. The parser itself is covered in
    `tests/test_session_cookie.py`; what is pinned here is that the reconcile
    uses it.
    """
    db_path, configure = sync_env
    _items(db_path, 1)

    for login in ("76561198000000000||tok", "76561198000000000%7C%7Ctok"):
        configure({1: _page([1], 1)}, login=login)
        with caplog.at_level(logging.INFO):
            subscription_sync.reconcile_own_subscriptions(db_path, 294100, {},
                                                          seen_at=1000)
        assert "steamid 76561198000000000" in caplog.text, login
        caplog.clear()


# --- the database helper itself ---------------------------------------------

def test_mark_own_subscribed_reports_only_the_first_stamp(tmp_path):
    db_path = str(tmp_path / "mark.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 5, "title": "T", "status": 200})

    assert mark_own_subscribed(db_path, 5, seen_at=1000) is True
    assert mark_own_subscribed(db_path, 5, seen_at=2000) is False
    row = _row(db_path, 5)
    assert row["own_subscribed"] == 1
    assert row["own_first_subscribed_at"] == 1000


def test_mark_own_subscribed_on_an_unknown_item_is_a_no_op(tmp_path):
    db_path = str(tmp_path / "absent.db")
    initialize_database(db_path)
    assert mark_own_subscribed(db_path, 999, seen_at=1000) is False
