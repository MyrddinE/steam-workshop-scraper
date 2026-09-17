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

from src.daemon import Daemon


def _daemon(db_path):
    return Daemon({
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"target_appids": [1], "batch_size": 1, "request_delay_seconds": 0},
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
    with patch("src.daemon.flag_for_image") as img, \
         patch("src.daemon.flag_for_web_scrape") as web:
        daemon._flag_scrape_and_image(merged, existing, 1, 5)
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

def _flag_unenriched(daemon, existing, merged, inherited_prio=10):
    """Drive `_flag_scrape_and_image` down the non-matching branch."""
    with patch.object(daemon, "_should_enrich", return_value=False), \
         patch("src.daemon.flag_for_image") as img, \
         patch("src.daemon.flag_for_web_scrape") as web:
        daemon._flag_scrape_and_image(merged, existing, 1, inherited_prio)
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


def test_the_scrape_still_borrows_the_api_priority_on_that_path(db_path):
    """Documented, not endorsed: two queues share one number here.

    ``inherited_prio`` is the item's `api_priority`, so an item opened in the
    detail pane (api_priority 10) queues its scrape at 10 rather than at the
    scrape queue's own lowest level. That is how a redundant scrape came to be
    drawn as pending; the queue itself is now withheld when a scrape cannot
    help, but the number is still borrowed. Changing that would re-prioritise
    every scrape, so it is left as it is and written down here.
    """
    daemon = _daemon(db_path)
    with patch.object(daemon, "_should_enrich", return_value=False), \
         patch("src.daemon.flag_for_image"), \
         patch("src.daemon.flag_for_web_scrape") as web:
        daemon._flag_scrape_and_image(_item(extended_description=None),
                                      _item(extended_description=None), 1, 10)
    web.assert_called_once_with(daemon.db_path, 1, 10)

