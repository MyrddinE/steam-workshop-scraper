"""Re-translation must happen when the source changes, and only then.

Two defects are covered here. First, the background paths (the daemon and the web
scraper) queued every non-ASCII field on every pass without checking whether a
translation already existed, so a completed translation was re-ordered on each
30-day staleness sweep. Second, nothing compared a translation's
``translate_version`` against ``steam_updated_at``, so an edited source never
refreshed. Fixing only the first would freeze existing translations forever,
which is why they ship together.
"""
from unittest.mock import patch

import pytest

from src.database import (
    get_connection,
    insert_or_update_item,
    translation_is_current,
)
from src.daemon import Daemon


def _queue_rows(db_path):
    conn = get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT entity_id, field, priority FROM translation_queue"
        ).fetchall()]
    finally:
        conn.close()


def _daemon(db_path, tmp_path):
    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST"},
        "daemon": {"api_batch_size": 1, "target_appids": [1]},
    }
    return Daemon(config, config_path=str(tmp_path / "config.yaml"))


# ── the freshness rule itself ────────────────────────────────────────────────

@pytest.mark.parametrize("text,version,steam,expected", [
    # Nothing translated yet: always needs translation.
    (None, 100, 100, False),
    ("", 100, 100, False),
    # A translation taken at the item's current revision is current.
    ("Test", 100, 100, True),
    # Translation newer than the Steam stamp (clock skew / no-payload items).
    ("Test", 200, 100, True),
    # Source edited after the translation: stale.
    ("Test", 100, 200, False),
    # Unknown provenance counts as stale.
    ("Test", None, 200, False),
    # No Steam revision at all: a change cannot be detected, so leave it alone.
    ("Test", 100, None, True),
    ("Test", None, None, True),
])
def test_translation_is_current(text, version, steam, expected):
    assert translation_is_current(text, version, steam) is expected


# ── the daemon path ──────────────────────────────────────────────────────────

def _merged(title="テスト", title_en="Test", **overrides):
    merged = {
        "title": title,
        "title_en": title_en,
        "short_description": None,
        "short_description_en": None,
        "translate_version": 100,
        "steam_updated_at": 100,
    }
    merged.update(overrides)
    return merged


def test_daemon_does_not_requeue_a_current_translation(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト", "title_en": "Test"})
    daemon = _daemon(db_path, tmp_path)

    daemon._queue_translations(_merged(), 1, enriched=True, inherited_priority=0)

    assert _queue_rows(db_path) == []


def test_daemon_requeues_when_the_source_was_edited(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト", "title_en": "Test"})
    daemon = _daemon(db_path, tmp_path)

    daemon._queue_translations(
        _merged(translate_version=100, steam_updated_at=200), 1,
        enriched=True, inherited_priority=0)

    rows = _queue_rows(db_path)
    assert [(r["entity_id"], r["field"]) for r in rows] == [(1, "title_en")]
    assert rows[0]["priority"] == 3


def test_daemon_queues_a_field_that_has_never_been_translated(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト"})
    daemon = _daemon(db_path, tmp_path)

    daemon._queue_translations(
        _merged(title_en=None, translate_version=None), 1,
        enriched=True, inherited_priority=0)

    assert [(r["entity_id"], r["field"]) for r in _queue_rows(db_path)] == [(1, "title_en")]


def test_daemon_ignores_ascii_text(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Plain"})
    daemon = _daemon(db_path, tmp_path)

    daemon._queue_translations(
        _merged(title="Plain", title_en=None, translate_version=None), 1,
        enriched=True, inherited_priority=0)

    assert _queue_rows(db_path) == []


def test_daemon_ignores_unqueued_items(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト"})
    daemon = _daemon(db_path, tmp_path)

    daemon._queue_translations(
        _merged(title_en=None, translate_version=None), 1,
        enriched=False, inherited_priority=0)

    assert _queue_rows(db_path) == []


# ── the web scraper path ─────────────────────────────────────────────────────

def _run_web_worker_once(db_path, item, scrape_data):
    from src.web_worker import WebScraperThread

    worker = WebScraperThread(db_path, ".pauselock")
    with patch("src.web_worker.get_next_web_scrape_item", return_value=item), \
         patch("src.web_worker.scrape_extended_details", return_value=scrape_data), \
         patch("time.sleep"):
        worker.start()
        worker.running = False
        worker.join(timeout=5)


def test_web_worker_queues_an_untranslated_description(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "web_scrape_priority": 5})

    _run_web_worker_once(
        db_path,
        {"workshop_id": 1, "steam_updated_at": 999,
         "extended_description_en": None, "translate_version": None},
        {"description": "日本語のテキスト", "tags": []},
    )

    assert [(r["entity_id"], r["field"]) for r in _queue_rows(db_path)] == [
        (1, "extended_description_en")]


def test_web_worker_does_not_requeue_a_current_translation(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "web_scrape_priority": 5})

    _run_web_worker_once(
        db_path,
        {"workshop_id": 1, "steam_updated_at": 999,
         "extended_description_en": "Already translated", "translate_version": 999},
        {"description": "日本語のテキスト", "tags": []},
    )

    assert _queue_rows(db_path) == []


def test_web_worker_requeues_when_the_description_changed(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "web_scrape_priority": 5})

    _run_web_worker_once(
        db_path,
        {"workshop_id": 1, "steam_updated_at": 1000,
         "extended_description_en": "Old translation", "translate_version": 999},
        {"description": "新しいテキスト", "tags": []},
    )

    assert [(r["entity_id"], r["field"]) for r in _queue_rows(db_path)] == [
        (1, "extended_description_en")]
