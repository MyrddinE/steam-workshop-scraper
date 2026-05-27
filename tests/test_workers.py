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
        mock_next.return_value = {"workshop_id": 1, "time_updated": 123456}
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
            "needs_image": 5, "time_updated": 123456,
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
