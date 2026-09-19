"""The API refresh is the change detector for the stages hanging off an item.

The API is the cheapest call and the only stage that goes stale on a timer. When
it observes an unchanged `steam_updated_at` the dependent work -- web scrape and
image download -- is already current and must not be re-queued. Per-queue
staleness sweeps are deliberately not the model.

Regression: the image was re-flagged on every successful fetch whether or not the
revision had changed, so unchanged previews were downloaded again on each
staleness cycle.
"""

from unittest.mock import patch

import pytest

from src.daemon import Daemon


def _daemon(db_path):
    return Daemon({
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"target_appids": [1], "api_batch_size": 1, "request_delay_seconds": 0},
    })


def _item(**over):
    item = {
        "workshop_id": 1,
        "steam_updated_at": 1000,
        "extended_description": "Stored description",
        "image_extension": "jpg",
        "preview_url": "https://example.invalid/preview.jpg",
        "api_priority": 5,
    }
    item.update(over)
    return item


def _flag(daemon, existing, merged):
    with patch("src.daemon.raise_image_priority") as img, \
         patch("src.daemon.raise_web_scrape_priority") as web:
        daemon._raise_scrape_and_image_priorities(merged, existing, 1, 5)
    return img, web


def test_unchanged_revision_requeues_nothing(db_path):
    """Same revision and both artefacts present: no work is created."""
    img, web = _flag(_daemon(db_path), _item(), _item())
    img.assert_not_called()
    web.assert_not_called()


def test_changed_revision_requeues_both_stages(db_path):
    img, web = _flag(_daemon(db_path), _item(), _item(steam_updated_at=2000))
    img.assert_called_once()
    web.assert_called_once()


def test_unchanged_revision_still_fetches_a_missing_image(db_path):
    """Change detection must not suppress work that has never been done."""
    img, _ = _flag(_daemon(db_path), _item(image_extension=None), _item(image_extension=None))
    img.assert_called_once()


def test_unchanged_revision_still_scrapes_a_missing_description(db_path):
    _, web = _flag(_daemon(db_path), _item(extended_description=None),
                   _item(extended_description=None))
    web.assert_called_once()


def test_no_preview_url_never_flags_an_image(db_path):
    img, _ = _flag(_daemon(db_path), _item(preview_url=None, image_extension=None),
                   _item(preview_url=None, image_extension=None))
    img.assert_not_called()


def test_unknown_revision_counts_as_changed(db_path):
    """With no stored revision, no change can be ruled out."""
    img, web = _flag(_daemon(db_path), _item(steam_updated_at=None), _item(steam_updated_at=1000))
    img.assert_called_once()
    web.assert_called_once()


# --- an answer from the server is final ------------------------------------
#
# image_extension holds the answer, not only a file type: a 404 or a non-image
# content type is stored there as well. These pin the consequence, which is the
# fix for a preview that was fetched forever: the re-flag gate must treat those
# values as settled, so an item the server has already refused leaves the queue
# for good.

def test_a_missing_preview_is_never_fetched_again(db_path):
    img, _ = _flag(_daemon(db_path), _item(image_extension="404"), _item(image_extension="404"))
    img.assert_not_called()


def test_a_gone_preview_is_never_fetched_again(db_path):
    img, _ = _flag(_daemon(db_path), _item(image_extension="410"), _item(image_extension="410"))
    img.assert_not_called()


def test_a_missing_preview_is_not_revived_by_a_new_revision(db_path):
    """A revision change re-fetches a real image, but never revives a refusal.

    The preview may legitimately be replaced when an item is updated, which is
    why a present image is re-fetched -- but a 404 is an answer about the item,
    and re-asking is the loop being closed here.
    """
    img, _ = _flag(_daemon(db_path), _item(image_extension="404", steam_updated_at=1000),
                   _item(image_extension="404", steam_updated_at=2000))
    img.assert_not_called()


def test_a_non_image_content_type_is_never_fetched_again(db_path):
    """A served text/html is what will be served next time too."""
    img, _ = _flag(_daemon(db_path), _item(image_extension="html"), _item(image_extension="html"))
    img.assert_not_called()


def test_a_transient_status_is_still_fetched(db_path):
    """A 503 is not a fact about the preview, so it must stay retryable."""
    img, _ = _flag(_daemon(db_path), _item(image_extension="503"), _item(image_extension="503"))
    img.assert_called_once()


def test_a_real_image_is_still_refetched_when_the_revision_changes(db_path):
    """The gate must not become a blanket refusal to refresh anything."""
    img, _ = _flag(_daemon(db_path), _item(image_extension="jpg"), _item(steam_updated_at=2000))
    img.assert_called_once()


# --- the unenriched path had no change detection at all ---------------------
#
# `_should_enrich` false means the item does not match its AppID's enrichment
# filters. That branch queued a scrape unconditionally, so every API refresh
# re-queued one for items whose stored description was already at the item's
# current revision -- measured live at 120 of the 570 items queued for a scrape.

def _flag_unenriched(daemon, existing, merged, inherited_priority=10):
    """Drive `_raise_scrape_and_image_priorities` down the non-matching branch."""
    with patch.object(daemon, "_should_enrich", return_value=False), \
         patch("src.daemon.raise_image_priority") as img, \
         patch("src.daemon.raise_web_scrape_priority") as web:
        daemon._raise_scrape_and_image_priorities(merged, existing, 1, inherited_priority)
    return img, web


def test_an_unenriched_item_with_a_current_description_is_not_rescraped(db_path):
    """The regression: no revision test on this path, so the queue never drained."""
    _img, web = _flag_unenriched(_daemon(db_path), _item(), _item())
    web.assert_not_called()


def test_an_unenriched_item_with_no_description_is_still_scraped(db_path):
    """The test is the revision, not a reason to stop scraping altogether."""
    _img, web = _flag_unenriched(_daemon(db_path), _item(extended_description=None),
                                 _item(extended_description=None))
    web.assert_called_once()


def test_an_unenriched_item_is_scraped_again_when_the_revision_changes(db_path):
    """A changed item may have gained a description the stored one lacks."""
    _img, web = _flag_unenriched(_daemon(db_path), _item(steam_updated_at=1000),
                                 _item(steam_updated_at=2000))
    web.assert_called_once()


def test_a_user_request_still_reaches_the_scrape_queue_on_that_path(db_path):
    """An item someone opened is fetched now, whatever the filters say.

    The two queues share one number, so a user-requested priority (10 = open in
    the detail pane) is inherited by the scrape rather than being flattened to
    the scrape queue's own lowest level. What changed is which priorities count
    as a request: see `test_the_discovery_priority_is_not_a_user_request`.
    """
    daemon = _daemon(db_path)
    with patch.object(daemon, "_should_enrich", return_value=False), \
         patch("src.daemon.raise_image_priority"), \
         patch("src.daemon.raise_web_scrape_priority") as web:
        daemon._raise_scrape_and_image_priorities(_item(extended_description=None),
                                      _item(extended_description=None), 1, 10)
    web.assert_called_once_with(daemon.db_path, 1, 10)


# --- which priorities are a request -----------------------------------------
#
# The queue vocabulary is shared with the API queue, and its lower half is the
# daemon's own bookkeeping: 1 backlog, 2 retry after a stage failure, 3 newly
# discovered. Only 5 (shown in a list) and 10 (open in the detail pane) are a
# person asking for this item. Inheriting the bookkeeping ones is what put items
# the enrichment filters excluded into the same band as the ones they selected
# -- measured live, 760,782 web entries from excluded items queued
# ahead of 107,365 selected ones.

@pytest.mark.parametrize("daemon_priority", [0, 1, 2, 3])
def test_the_daemon_priorities_are_not_requests(daemon_priority):
    from src.daemon import user_requested_priority
    assert user_requested_priority(daemon_priority) == 0


@pytest.mark.parametrize("user_priority", [5, 10])
def test_the_user_priorities_are_requests(user_priority):
    from src.daemon import user_requested_priority
    assert user_requested_priority(user_priority) == user_priority


def test_the_discovery_priority_is_not_a_user_request(db_path):
    """The one-line bug: a filter-excluded new item was queued at 3, not 1.

    Both queues are asserted, because both inherited the discovery priority. The
    item is given no image yet so the image branch is exercised rather than
    skipped as already current.
    """
    daemon = _daemon(db_path)
    item = _item(extended_description=None, image_extension=None)
    with patch.object(daemon, "_should_enrich", return_value=False), \
         patch("src.daemon.raise_image_priority") as img, \
         patch("src.daemon.raise_web_scrape_priority") as web:
        daemon._raise_scrape_and_image_priorities(item, item, 1, 3)
    web.assert_called_once_with(daemon.db_path, 1, 1)
    img.assert_called_once_with(daemon.db_path, 1, 1)


def test_the_retry_priority_is_not_a_user_request_either(db_path):
    """2 is `api_priority` only, per the scale, and belongs to the daemon."""
    daemon = _daemon(db_path)
    with patch.object(daemon, "_should_enrich", return_value=False), \
         patch("src.daemon.raise_image_priority"), \
         patch("src.daemon.raise_web_scrape_priority") as web:
        daemon._raise_scrape_and_image_priorities(_item(extended_description=None),
                                      _item(extended_description=None), 1, 2)
    web.assert_called_once_with(daemon.db_path, 1, 1)

