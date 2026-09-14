"""Tests for web scraper, image download, and translator worker threads."""
import pytest
from unittest.mock import patch


# ── Web worker ───────────────────────────────────────────────────────────────

def test_web_worker_thread_lifecycle(tmp_path):
    """Web worker starts, runs, and stops cleanly."""
    from src.web_worker import WebScraperThread

    db_path = str(tmp_path / "test.db")
    worker = WebScraperThread(db_path, '.pauselock')
    assert worker.running is True
    worker.running = False
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_web_worker_failure_sets_api_priority(db_path):
    """Web scrape failure updates api_priority to 2 via the else branch."""
    from src.web_worker import WebScraperThread
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5, "api_priority": 0})

    worker = WebScraperThread(db_path, '.pauselock')

    with patch('src.web_worker.get_next_web_scrape_item') as mock_next, \
         patch('src.web_worker.scrape_extended_details', return_value=None), \
         patch('time.sleep'):  # don't actually sleep
        mock_next.return_value = {"workshop_id": 1, "steam_updated_at": 123456}
        worker.start()
        worker.running = False  # stop after current iteration
        worker.join(timeout=2)

    conn = get_connection(db_path)
    prio = conn.execute(
        "SELECT api_priority FROM workshop_items WHERE workshop_id=1"
    ).fetchone()[0]
    conn.close()
    assert prio == 2


# ── Image worker ─────────────────────────────────────────────────────────────


def test_image_worker_thread_lifecycle(tmp_path):
    """Image worker starts, runs, and stops cleanly."""
    from src.image_worker import ImageScraperThread

    db_path = str(tmp_path / "test.db")
    worker = ImageScraperThread(db_path, '.pauselock')
    assert worker.running is True
    worker.running = False
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_image_worker_failure_sets_api_priority(db_path):
    """Image download failure bumps api_priority to 2."""
    from src.image_worker import ImageScraperThread
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {
        "workshop_id": 1, "needs_image": 5, "api_priority": 0,
        "preview_url": "http://example.com/img.jpg"
    })

    worker = ImageScraperThread(db_path, '.pauselock')

    call_count = [0]
    def fail_on_first(*args, **kwargs):
        call_count[0] += 1
        raise Exception("Connection refused")

    with patch('src.image_worker.get_next_image_item') as mock_next, \
         patch('src.image_worker.requests.get', side_effect=fail_on_first), \
         patch('src.image_worker.time.sleep'):  # don't actually sleep
        mock_next.return_value = {
            "workshop_id": 1, "preview_url": "http://example.com/img.jpg",
            "needs_image": 5, "steam_updated_at": 123456,
        }
        worker.start()
        import time as _time
        deadline = _time.time() + 3
        while call_count[0] < 1 and _time.time() < deadline:
            _time.sleep(0.05)
        worker.running = False
        worker.join(timeout=2)

    conn = get_connection(db_path)
    prio = conn.execute(
        "SELECT api_priority FROM workshop_items WHERE workshop_id=1"
    ).fetchone()[0]
    conn.close()
    assert prio == 2


# ── Web worker: selector miss ────────────────────────────────────────────────
# A selector miss returns {"description": None, "tags": []}, which is truthy. It
# used to be taken as success, writing extended_description = NULL and
# needs_web_scrape = 0, so the item was recorded as permanently scraped with
# nothing to show for it and was never retried. A miss now distinguishes a page
# that was never the item's (left queued) from the item page with no description
# (a permanent absence, so the item leaves the queue).

def _run_web_worker(db_path, item, scrape_data):
    from src.web_worker import WebScraperThread

    worker = WebScraperThread(db_path, ".pauselock")
    with patch("src.web_worker.get_next_web_scrape_item", return_value=item), \
         patch("src.web_worker.scrape_extended_details", return_value=scrape_data), \
         patch("time.sleep"):
        worker.start()
        worker.running = False
        worker.join(timeout=5)


def _web_scrape_priority(db_path, workshop_id=1):
    from src.database import get_connection
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT needs_web_scrape FROM workshop_items WHERE workshop_id=?",
            (workshop_id,)).fetchone()[0]
    finally:
        conn.close()


MISS = {"description": None, "tags": [], "body": "<html>no selector</html>",
        "http_status": 200, "final_url": "https://example.invalid/?id=1"}

# The item template is present but the description element is not: the page really
# is the item's, and it simply has no extended description.
ITEM_PAGE_WITHOUT_DESCRIPTION = dict(
    MISS, body='<html><div class="workshopItem">x</div></html>')


def test_selector_miss_without_the_item_page_leaves_the_item_queued(db_path):
    """An error, wall or throttle page is not the item's fault, so the item keeps
    its place in the queue rather than decaying out of it."""
    from src.database import insert_or_update_item

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1}, MISS)

    assert _web_scrape_priority(db_path) == 5, \
        "the item must not be blamed for a page it never got"


def test_selector_miss_on_the_item_page_clears_the_queue(db_path):
    """The item page loaded and has no description: retrying cannot change that,
    so the item leaves the queue, which is what lets the queue drain."""
    from src.database import insert_or_update_item

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                    ITEM_PAGE_WITHOUT_DESCRIPTION)

    assert _web_scrape_priority(db_path) == 0, \
        "a genuine, permanent absence must not stay queued"


def test_selector_miss_leaves_existing_description_untouched(db_path):
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {
        "workshop_id": 1, "needs_web_scrape": 3,
        "extended_description": "previously scraped text"})

    _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                    ITEM_PAGE_WITHOUT_DESCRIPTION)

    conn = get_connection(db_path)
    try:
        stored = conn.execute(
            "SELECT extended_description FROM workshop_items WHERE workshop_id=1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == "previously scraped text", "a miss must not blank the description"


def test_selector_miss_does_not_touch_api_priority(db_path):
    """The request succeeded, so this is not a network failure."""
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 2, "api_priority": 0})

    _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1}, MISS)

    conn = get_connection(db_path)
    try:
        prio = conn.execute(
            "SELECT api_priority FROM workshop_items WHERE workshop_id=1").fetchone()[0]
    finally:
        conn.close()
    assert prio == 0


def test_selector_miss_is_captured(db_path, tmp_path):
    import json
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 42, "needs_web_scrape": 5})
        _run_web_worker(db_path, {"workshop_id": 42, "steam_updated_at": 1}, MISS)
    finally:
        capture.configure(None)

    from src.web_scraper import DESCRIPTION_SELECTOR
    group = capture.group_id("web_selector_miss", DESCRIPTION_SELECTOR, "web_scrape")
    group_dir = outbox / "failures" / group
    records = [p for p in group_dir.glob("*.json") if p.name != "_group.json"]
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["workshop_id"] == 42
    assert record["stage"] == "web_scrape"
    assert record["http_status"] == 200
    assert "workshopItemDescription" in record["selector"]


def test_selector_miss_records_the_page_shape(db_path, tmp_path):
    """The capture must describe the page well enough to recognise the break."""
    import json
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 7, "needs_web_scrape": 5})
        _run_web_worker(db_path, {"workshop_id": 7, "steam_updated_at": 1},
                        dict(MISS, body='<html><head><title>Workshop Error</title></head>'
                                        '<body><div class="errorPageBlock">x</div></body></html>'))
    finally:
        capture.configure(None)

    from src.web_scraper import DESCRIPTION_SELECTOR
    group = capture.group_id("web_selector_miss", DESCRIPTION_SELECTOR, "web_scrape")
    group_dir = outbox / "failures" / group
    record = json.loads(next(p for p in group_dir.glob("*.json")
                             if p.name != "_group.json").read_text())
    assert record["shape"]["title_tag"] == "Workshop Error"
    assert record["shape"]["class_count"] == 1


# ── Translator worker ────────────────────────────────────────────────────────

def test_translator_thread_lifecycle(tmp_path):
    """Translator starts and stops cleanly."""
    from src.translator import TranslatorThread
    from src.database import initialize_database

    db_path = str(tmp_path / "test.db")
    initialize_database(db_path)
    config = {
        "database": {"path": db_path},
        "openai": {"api_key": "sk-test", "endpoint": "https://test/v1", "model": "gpt-test"},
    }
    thread = TranslatorThread(config)
    assert thread.running is True
    thread.running = False
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
