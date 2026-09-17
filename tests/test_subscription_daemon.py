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

from src.daemon import Daemon, SUBSCRIPTION_RECONCILE_INTERVAL_SECONDS


@pytest.fixture
def daemon(mock_config_with_api, monkeypatch):
    monkeypatch.setattr('src.daemon.save_config', lambda *a, **k: None)
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
         patch('src.daemon.get_next_items_to_scrape', return_value=[]), \
         patch.object(Daemon, '_wait_for_work'):
        daemon.process_batch()
    assert reconcile.called


def test_a_failing_reconcile_never_reaches_the_scrape_loop(daemon):
    """A Steam page that did not load must not stop scraping."""
    with patch('src.daemon.reconcile_own_subscriptions',
               side_effect=RuntimeError("steam is down")) as reconcile, \
         patch('src.daemon.get_next_items_to_scrape', return_value=[]), \
         patch.object(Daemon, '_wait_for_work'):
        # The exception is swallowed per appid, so process_batch still returns.
        daemon.process_batch()

    assert reconcile.call_count == len(daemon.target_appids)


def test_one_bad_appid_does_not_stop_the_others(daemon):
    daemon.target_appids = [1, 2, 3]
    calls = []

    def reconcile(db_path, appid, config):
        calls.append(appid)
        if appid == 2:
            raise RuntimeError("boom")

    with patch('src.daemon.reconcile_own_subscriptions', side_effect=reconcile):
        daemon.reconcile_subscriptions()

    assert calls == [1, 2, 3]
