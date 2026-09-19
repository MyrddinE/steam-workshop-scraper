"""The subscription reconcile as the daemon schedules it.

The reconcile is housekeeping on the per-batch path, so it follows the shape of
the staleness sweep: once per target appid on the first batch after startup, then
on a daily monotonic interval, and a failure is a log line rather than an
exception. These tests pin that scheduling without ever touching the network:
`reconcile_own_subscriptions` is patched, and what is asserted is when and for
which appids the daemon calls it.
"""

from unittest.mock import patch

import pytest

from src.daemon import (Daemon, SUBSCRIPTION_RECONCILE_INTERVAL_SECONDS,
                        SUBSCRIPTION_RECONCILE_RETRY_SECONDS)
from src.workshop_folders import DOWNLOAD_SCAN_INTERVAL_SECONDS


@pytest.fixture
def daemon(mock_config_with_api, monkeypatch):
    monkeypatch.setattr('src.daemon.save_config', lambda *a, **k: None)
    # The refresh reads the browser's cookie store: a real file, present on a
    # developer's machine and absent in most containers. The scheduling tests are
    # not about it, so it is stubbed here; the tests that are about it re-patch it.
    monkeypatch.setattr(Daemon, '_refresh_login_cookie', lambda self: False)
    return Daemon(mock_config_with_api)


def test_the_interval_is_daily():
    assert SUBSCRIPTION_RECONCILE_INTERVAL_SECONDS == 86400


def test_it_reconciles_every_target_appid_on_the_first_batch(daemon):
    daemon.target_appids = [111, 222]
    with patch('src.daemon.reconcile_own_subscriptions') as reconcile:
        daemon._maybe_reconcile_subscriptions()

    assert [call.args[1] for call in reconcile.call_args_list] == [111, 222]
    assert all(call.args[0] == daemon.db_path for call in reconcile.call_args_list)


def test_it_does_not_reconcile_again_within_the_interval(daemon):
    with patch('src.daemon.reconcile_own_subscriptions') as reconcile:
        daemon._maybe_reconcile_subscriptions()
        daemon._maybe_reconcile_subscriptions()
        daemon._maybe_reconcile_subscriptions()

    assert reconcile.call_count == len(daemon.target_appids), \
        "a batch every few seconds must not walk Steam every time"


def test_it_reconciles_again_once_the_interval_has_passed(daemon):
    with patch('src.daemon.reconcile_own_subscriptions') as reconcile:
        daemon._maybe_reconcile_subscriptions()
        first = reconcile.call_count
        # Move the monotonic clock past the interval rather than sleeping for it.
        daemon._last_subscription_reconcile -= SUBSCRIPTION_RECONCILE_INTERVAL_SECONDS + 1
        daemon._maybe_reconcile_subscriptions()

    assert reconcile.call_count == first * 2


def test_it_runs_on_the_first_batch_after_startup(daemon):
    """A daemon that has just come up must not sit on last week's markers."""
    assert daemon._last_subscription_reconcile is None
    with patch('src.daemon.reconcile_own_subscriptions') as reconcile, \
         patch('src.daemon.get_next_items_to_fetch', return_value=[]), \
         patch.object(Daemon, '_wait_for_work'):
        daemon.process_batch()
    assert reconcile.called


def test_a_failing_reconcile_never_reaches_the_scrape_loop(daemon):
    """A Steam page that did not load must not stop scraping."""
    with patch('src.daemon.reconcile_own_subscriptions',
               side_effect=RuntimeError("steam is down")) as reconcile, \
         patch('src.daemon.get_next_items_to_fetch', return_value=[]), \
         patch.object(Daemon, '_wait_for_work'):
        # The exception is swallowed per appid, so process_batch still returns.
        daemon.process_batch()

    assert reconcile.call_count == len(daemon.target_appids)


def test_one_bad_appid_does_not_stop_the_others(daemon):
    daemon.target_appids = [1, 2, 3]
    calls = []

    def reconcile(db_path, appid, config, **kwargs):
        calls.append(appid)
        if appid == 2:
            raise RuntimeError("boom")

    with patch('src.daemon.reconcile_own_subscriptions', side_effect=reconcile):
        daemon.reconcile_subscriptions()

    assert calls == [1, 2, 3]


# --- the login cookie is refreshed first -------------------------------------
#
# The reconcile runs on a daily clock of its own while its credential expires on
# Steam's, about a day out. The browser read is cached for the process lifetime,
# so a daemon that read a valid cookie at startup keeps replaying it -- even
# after the operator signs in again -- until something asks for a re-read. This
# caller has to be that something, or its one reconcile of the day is spent on a
# cookie that died overnight.

def test_the_login_cookie_is_refreshed_before_the_reconcile(daemon):
    order = []
    daemon.target_appids = [111]
    with patch.object(Daemon, '_refresh_login_cookie',
                      side_effect=lambda *a, **k: order.append("refresh")), \
         patch('src.daemon.reconcile_own_subscriptions',
               side_effect=lambda *a, **k: order.append("reconcile")):
        daemon._maybe_reconcile_subscriptions()

    assert order == ["refresh", "reconcile"]


def test_the_refresh_happens_once_per_interval_not_once_per_appid(daemon):
    """It is a cookie-store copy, and the store is the same for every appid."""
    daemon.target_appids = [111, 222]
    with patch.object(Daemon, '_refresh_login_cookie') as refresh, \
         patch('src.daemon.reconcile_own_subscriptions'):
        daemon._maybe_reconcile_subscriptions()

    assert refresh.call_count == 1


def test_the_refresh_stays_inside_the_interval_guard(daemon):
    with patch.object(Daemon, '_refresh_login_cookie') as refresh, \
         patch('src.daemon.reconcile_own_subscriptions'):
        daemon._maybe_reconcile_subscriptions()
        refresh.reset_mock()
        daemon._maybe_reconcile_subscriptions()

    assert refresh.call_count == 0, "the guard has to be outside the file copy too"


def test_a_failing_refresh_does_not_stop_the_reconcile(daemon):
    """The browser read is a convenience; the reconcile is the job. A refresh
    that raises must not cost the walk, which may still work off the config."""
    daemon.target_appids = [111]
    with patch.object(Daemon, '_refresh_login_cookie',
                      side_effect=RuntimeError("cookies.sqlite is locked")), \
         patch.object(Daemon, 'reconcile_subscriptions') as walk:
        daemon._maybe_reconcile_subscriptions()

    assert walk.called


# --- a walk that could not authenticate is retried sooner --------------------
#
# The daily interval has already elapsed for the day by the time the operator
# fixes the login the banner told them about, so waiting the full interval again
# would hold the markers wrong for another day. The recorded problem is the
# signal, and it is the same fact the web UI shows.

def test_a_broken_login_is_retried_on_the_short_interval(daemon):
    from src import session_health
    daemon.target_appids = [111]
    session_health.record_rejected(daemon.db_path, "the login cookie expired", now=1000)

    with patch.object(Daemon, '_refresh_login_cookie'), \
         patch.object(Daemon, 'reconcile_subscriptions') as walk:
        daemon._maybe_reconcile_subscriptions()
        assert walk.call_count == 1
        # Short of the daily interval but past the retry one.
        daemon._last_subscription_reconcile -= SUBSCRIPTION_RECONCILE_RETRY_SECONDS + 1
        daemon._maybe_reconcile_subscriptions()

    assert walk.call_count == 2, "a broken login must not hold the markers for a day"


def test_a_healthy_login_is_not_retried_on_the_short_interval(daemon):
    daemon.target_appids = [111]
    with patch.object(Daemon, '_refresh_login_cookie'), \
         patch.object(Daemon, 'reconcile_subscriptions') as walk:
        daemon._maybe_reconcile_subscriptions()
        daemon._last_subscription_reconcile -= SUBSCRIPTION_RECONCILE_RETRY_SECONDS + 1
        daemon._maybe_reconcile_subscriptions()

    assert walk.call_count == 1, "the daily cadence is what keeps the walk cheap"


def test_the_recorded_problem_lifts_the_short_interval_once_it_clears(daemon):
    """The retry stops as soon as the walk works, which is what clears it."""
    from src import session_health
    daemon.target_appids = [111]
    session_health.record_rejected(daemon.db_path, "the login cookie expired", now=1000)
    assert daemon._reconcile_interval() == SUBSCRIPTION_RECONCILE_RETRY_SECONDS

    session_health.record_accepted(daemon.db_path)

    assert daemon._reconcile_interval() == SUBSCRIPTION_RECONCILE_INTERVAL_SECONDS


# --- the downloaded-star scan is housekeeping on the same clock -------------

def test_the_downloaded_scan_runs_on_the_first_batch_after_startup(daemon):
    assert daemon._last_download_scan is None
    with patch.object(daemon.workshop_folders, "scan") as scan, \
         patch('src.daemon.reconcile_own_subscriptions'), \
         patch('src.daemon.get_next_items_to_fetch', return_value=[]), \
         patch.object(Daemon, '_wait_for_work'):
        daemon.process_batch()

    assert scan.called, "a daemon that just came up must not sit on old markers"


def test_the_downloaded_scan_is_not_repeated_within_its_interval(daemon):
    with patch.object(daemon.workshop_folders, "scan") as scan:
        daemon._maybe_scan_downloaded_items()
        daemon._maybe_scan_downloaded_items()
        daemon._maybe_scan_downloaded_items()

    assert scan.call_count == 1, "the per-batch path must not stat folders every pass"


def test_the_downloaded_scan_runs_again_after_its_interval(daemon):
    with patch.object(daemon.workshop_folders, "scan") as scan:
        daemon._maybe_scan_downloaded_items()
        daemon._last_download_scan -= DOWNLOAD_SCAN_INTERVAL_SECONDS + 1
        daemon._maybe_scan_downloaded_items()

    assert scan.call_count == 2


def test_a_failing_downloaded_scan_never_reaches_the_scrape_loop(daemon):
    with patch.object(daemon.workshop_folders, "scan",
                      side_effect=RuntimeError("drive went away")) as scan, \
         patch('src.daemon.reconcile_own_subscriptions'), \
         patch('src.daemon.get_next_items_to_fetch', return_value=[]), \
         patch.object(Daemon, '_wait_for_work'):
        daemon.process_batch()

    assert scan.called
