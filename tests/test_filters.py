import pytest
import json
import sqlite3
from src.database import (
    _evaluate_single_filter, _evaluate_filters, _evaluate_tag_filter,
    initialize_database, insert_or_update_item, save_enrichment_filters, get_app_tracking,
    get_connection
)

# ── _evaluate_single_filter: text fields ────────────────────────────────────

def test_evaluate_text_contains_match():
    assert _evaluate_single_filter({"title": "Hello World"}, "title", "contains", "hello") is True

def test_evaluate_text_contains_no_match():
    assert _evaluate_single_filter({"title": "Hello World"}, "title", "contains", "xyz") is False

def test_evaluate_text_contains_null():
    assert _evaluate_single_filter({"title": None}, "title", "contains", "hello") is False

def test_evaluate_text_does_not_contain_match():
    assert _evaluate_single_filter({"title": "Hello World"}, "title", "does_not_contain", "xyz") is True

def test_evaluate_text_does_not_contain_no_match():
    assert _evaluate_single_filter({"title": "Hello World"}, "title", "does_not_contain", "hello") is False

def test_evaluate_text_is():
    assert _evaluate_single_filter({"title": "Exact"}, "title", "is", "Exact") is True

def test_evaluate_text_is_not():
    assert _evaluate_single_filter({"title": "Exact"}, "title", "is_not", "Other") is True

def test_evaluate_text_is_empty():
    assert _evaluate_single_filter({"title": ""}, "title", "is_empty", None) is True
    assert _evaluate_single_filter({"title": None}, "title", "is_empty", None) is True

def test_evaluate_text_is_not_empty():
    assert _evaluate_single_filter({"title": "Hello"}, "title", "is_not_empty", None) is True

# ── _evaluate_single_filter: numeric fields ──────────────────────────────────

def test_evaluate_numeric_gt():
    assert _evaluate_single_filter({"subscriptions": 100}, "subscriptions", "gt", 50) is True

def test_evaluate_numeric_gt_string_val():
    assert _evaluate_single_filter({"subscriptions": 100}, "subscriptions", "gt", "50") is True

def test_evaluate_numeric_gte():
    assert _evaluate_single_filter({"subscriptions": 100}, "subscriptions", "gte", 100) is True

def test_evaluate_numeric_lt():
    assert _evaluate_single_filter({"subscriptions": 100}, "subscriptions", "lt", 200) is True

def test_evaluate_numeric_lte():
    assert _evaluate_single_filter({"subscriptions": 100}, "subscriptions", "lte", 100) is True

def test_evaluate_numeric_fails():
    assert _evaluate_single_filter({"subscriptions": 10}, "subscriptions", "gte", 100) is False

def test_evaluate_numeric_default_zero():
    assert _evaluate_single_filter({"subscriptions": None}, "subscriptions", "lte", 500) is True

def test_evaluate_numeric_is():
    assert _evaluate_single_filter({"file_size": 1024}, "file_size", "is", "1024") is True

def test_evaluate_numeric_is_not():
    assert _evaluate_single_filter({"file_size": 1024}, "file_size", "is_not", "2048") is True

def test_evaluate_numeric_is_empty():
    assert _evaluate_single_filter({"subscriptions": None}, "subscriptions", "is_empty", None) is True

# ── _evaluate_tag_filter ─────────────────────────────────────────────────────

def test_evaluate_tag_contains():
    assert _evaluate_tag_filter({"tags": '["Mod", "Tool"]'}, "contains", "Mod") is True

def test_evaluate_tag_contains_missing():
    assert _evaluate_tag_filter({"tags": '["Mod"]'}, "contains", "Tool") is False

def test_evaluate_tag_does_not_contain():
    assert _evaluate_tag_filter({"tags": '["Mod"]'}, "does_not_contain", "Tool") is True

def test_evaluate_tag_empty():
    assert _evaluate_tag_filter({"tags": None}, "is_empty", None) is True
    assert _evaluate_tag_filter({"tags": '[]'}, "is_empty", None) is True

def test_evaluate_tag_is_not_empty():
    assert _evaluate_tag_filter({"tags": '["Mod"]'}, "is_not_empty", None) is True

def test_evaluate_tag_invalid_json():
    assert _evaluate_tag_filter({"tags": "not-json"}, "contains", "anything") is False

# ── _evaluate_filters: compound ──────────────────────────────────────────────

def test_evaluate_filters_empty_list():
    assert _evaluate_filters({"title": "Any"}, []) is True

def test_evaluate_filters_single_pass():
    assert _evaluate_filters({"title": "Hello", "subscriptions": 500}, [
        {"field": "Title", "op": "contains", "value": "Hello"}
    ]) is True

def test_evaluate_filters_single_fail():
    assert _evaluate_filters({"title": "Hello"}, [
        {"field": "Title", "op": "contains", "value": "World"}
    ]) is False

def test_evaluate_filters_multiple_all_pass():
    assert _evaluate_filters({"title": "Mod", "subscriptions": 500, "tags": '["Mod", "Utility"]'}, [
        {"field": "Title", "op": "contains", "value": "Mod"},
        {"field": "Subs", "op": "gte", "value": 100},
        {"field": "Tags", "op": "contains", "value": "Utility"},
    ]) is True

def test_evaluate_filters_one_fails():
    assert _evaluate_filters({"title": "Mod", "subscriptions": 50, "tags": '["Mod"]'}, [
        {"field": "Title", "op": "contains", "value": "Mod"},
        {"field": "Subs", "op": "gte", "value": 100},
    ]) is False

def test_evaluate_filters_numeric_with_all_operators():
    filters = [
        {"field": "Subs", "op": "gte", "value": 100},
        {"field": "Subs", "op": "lte", "value": 1000},
        {"field": "File Size", "op": "lt", "value": 10000},
    ]
    assert _evaluate_filters({"subscriptions": 500, "file_size": 5000}, filters) is True

def test_evaluate_filters_file_size_too_large():
    assert _evaluate_filters({"subscriptions": 500, "file_size": 20000}, [
        {"field": "File Size", "op": "lte", "value": 10000},
    ]) is False

def test_evaluate_filters_missing_field():
    assert _evaluate_filters({"title": "Hello"}, [
        {"field": "Title", "op": "contains", "value": "Hello"},
        {"field": "Subs", "op": "gte", "value": 100},
    ]) is False

def test_evaluate_filters_is_not_operator():
    assert _evaluate_filters({"creator": "111"}, [
        {"field": "Author ID", "op": "is_not", "value": "999"},
    ]) is True

# ── save_enrichment_filters / get_app_tracking roundtrip ─────────────────────────────

def test_save_and_load_enrichment_filters(db_path):
    filters = [
        {"field": "Title", "op": "contains", "value": "Vampire"},
        {"field": "Tags", "op": "contains", "value": "Translation"},
        {"field": "Subs", "op": "gte", "value": 100},
    ]
    save_enrichment_filters(db_path, 294100, enrichment_filters=json.dumps(filters))
    tracking = get_app_tracking(db_path, 294100)
    assert tracking is not None
    loaded = json.loads(tracking["enrichment_filters"])
    assert loaded == filters

def test_save_and_load_empty_filters(db_path):
    save_enrichment_filters(db_path, 294100, enrichment_filters=json.dumps([]))
    tracking = get_app_tracking(db_path, 294100)
    assert tracking["enrichment_filters"] == '[]'

def test_save_still_sets_legacy_columns(db_path):
    save_enrichment_filters(db_path, 294100, filter_text="test", required_tags=["mod"], excluded_tags=["broken"])
    tracking = get_app_tracking(db_path, 294100)
    assert tracking["filter_text"] == "test"
    assert tracking["required_tags"] == json.dumps(["mod"])
    assert tracking["excluded_tags"] == json.dumps(["broken"])

def test_legacy_filter_migration(tmp_path):
    """Verify legacy filter columns are migrated to enrichment_filters on startup."""
    db_path = str(tmp_path / "migrate.db")
    # Create old-style DB manually (without enrichment_filters column)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE app_tracking (
            appid INTEGER PRIMARY KEY,
            filter_text TEXT DEFAULT '',
            required_tags TEXT DEFAULT '[]',
            excluded_tags TEXT DEFAULT '[]'
        )
    """)
    conn.execute("""
        INSERT INTO app_tracking (appid, filter_text, required_tags, excluded_tags)
        VALUES (294100, 'Vampire', '["Translation"]', '["Broken"]')
    """)
    conn.commit()
    conn.close()

    initialize_database(db_path)

    tracking = get_app_tracking(db_path, 294100)
    assert tracking is not None
    filters = json.loads(tracking["enrichment_filters"])
    assert len(filters) == 3
    assert {"field": "Title", "op": "contains", "value": "Vampire"} in filters
    assert {"field": "Tags", "op": "contains", "value": "Translation"} in filters
    assert {"field": "Tags", "op": "does_not_contain", "value": "Broken"} in filters

def test_legacy_filter_migration_no_legacy_data(db_path):
    """Migration should not create enrichment_filters if there are no legacy filters."""
    save_enrichment_filters(db_path, 294100)
    tracking = get_app_tracking(db_path, 294100)
    assert tracking["enrichment_filters"] == '[]'


# ── what the filters do to the queue priority ───────────────────────────────
#
# An item that fails the filters is still scraped -- the filters choose priority,
# not membership -- but it must not outrank an item they selected. It did: the
# dependent queues were handed the item's whole pre-fetch `api_priority`, and a
# newly discovered item carries 3, so every new item the filters excluded landed
# in the same band as the ones they chose; migration 21->22 repairs the rows that
# wrote. These tests are the pair that says so, evaluated through the real filter
# list rather than a patch.

def _daemon_for(db_path, appid=294100):
    from src.daemon import Daemon
    return Daemon({
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"target_appids": [appid], "batch_size": 1, "request_delay_seconds": 0},
    })


def _flag(daemon, item, api_priority):
    """Drive the flagging with the item's pre-fetch api_priority, as the fetch does."""
    from unittest.mock import patch
    with patch("src.daemon.raise_web_scrape_priority") as web, \
         patch("src.daemon.raise_image_priority") as img:
        daemon._raise_scrape_and_image_priorities(item, item, item["workshop_id"], api_priority)
    return web, img


def _item(**over):
    item = {
        "workshop_id": 1,
        "consumer_appid": 294100,
        "title": "A wallpaper",
        "steam_updated_at": 1000,
        "extended_description": None,
        "preview_url": "https://example.invalid/p.jpg",
        "tags": ["Video"],
    }
    item.update(over)
    return item


def test_an_item_that_fails_the_filters_is_queued_at_backlog(db_path):
    """A discovered item's priority 3 must not reach this queue.

    Discovery is where an item starts, not something anyone asked for, so it is
    not inherited: the scrape is queued at the branch's own floor. Before the fix
    this asserted 3, which is the same band as an item the filters selected.
    """
    save_enrichment_filters(db_path, 294100, enrichment_filters=json.dumps(
        [{"field": "Tags", "op": "contains", "value": "Mature"}]))
    daemon = _daemon_for(db_path)

    web, img = _flag(daemon, _item(tags=["Video"]), api_priority=3)

    web.assert_called_once_with(db_path, 1, 1)
    img.assert_called_once_with(db_path, 1, 1)


def test_an_item_that_passes_the_filters_is_queued_at_new_item_priority(db_path):
    save_enrichment_filters(db_path, 294100, enrichment_filters=json.dumps(
        [{"field": "Tags", "op": "contains", "value": "Mature"}]))
    daemon = _daemon_for(db_path)

    web, img = _flag(daemon, _item(tags=["Mature"]), api_priority=3)

    web.assert_called_once_with(db_path, 1, 3)
    img.assert_called_once_with(db_path, 1, 3)


def test_a_user_request_still_lifts_an_item_the_filters_exclude(db_path):
    """Opening a filtered-out item is a request to fetch it now, not a bug."""
    save_enrichment_filters(db_path, 294100, enrichment_filters=json.dumps(
        [{"field": "Tags", "op": "contains", "value": "Mature"}]))
    daemon = _daemon_for(db_path)

    web, _img = _flag(daemon, _item(tags=["Video"]), api_priority=10)

    web.assert_called_once_with(db_path, 1, 10)
