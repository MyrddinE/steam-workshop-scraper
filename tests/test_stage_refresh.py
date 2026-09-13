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
