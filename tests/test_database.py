import sqlite3
import os
import threading
import time
import pytest
import json
from datetime import datetime, timezone, timedelta
from src.database import (
    insert_or_update_item,
    get_next_items_to_fetch,
    search_items,
    get_connection,
    count_never_fetched_items,
    delete_never_fetched_items,
    raise_web_scrape_priority,
    raise_image_priority,
    raise_api_priority_for_list,
    raise_api_priority_for_detail,
    clear_subscription_queue,
    get_subscription_queue_items,
    EXPECTED_VERSION,
)

def test_count_never_fetched_items(db_path):
    """Test counting items that have never been attempted."""
    assert count_never_fetched_items(db_path) == 0
    
    insert_or_update_item(db_path, {"workshop_id": 1}) # Unscraped
    insert_or_update_item(db_path, {"workshop_id": 2}) # Unscraped
    insert_or_update_item(db_path, {"workshop_id": 3, "api_fetched_at": 1672531200}) # Scraped
    
    assert count_never_fetched_items(db_path) == 2

def test_initialize_database(db_path):
    """Tests that the database and table are created correctly."""
    assert os.path.exists(db_path)

    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='workshop_items'")
    assert cursor.fetchone() is not None, "Table 'workshop_items' was not created."

    # Verify journal mode is WAL
    cursor.execute("PRAGMA journal_mode")
    assert cursor.fetchone()[0].lower() == "wal", "WAL mode was not enabled."
    conn.close()

def test_insert_or_update_item(db_path):
    """Tests that items can be inserted and then updated on conflict."""
    item = {
        "workshop_id": 123,
        "title": "Test Item",
        "scrape_version": 1696104000
    }
    # First insert should return True
    assert insert_or_update_item(db_path, item) is True
    
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT title FROM workshop_items WHERE workshop_id=123")
    assert cursor.fetchone()[0] == "Test Item"
    
    # Update existing item should return False
    item["title"] = "Updated Item"
    assert insert_or_update_item(db_path, item) is False
    cursor.execute("SELECT title FROM workshop_items WHERE workshop_id=123")
    assert cursor.fetchone()[0] == "Updated Item"
    conn.close()

def test_get_next_items_to_fetch(db_path):
    """Tests that items are fetched in order of oldest api_fetched_at (NULLs first)."""
    insert_or_update_item(db_path, {"workshop_id": 1, "fetch_status": 200, "api_fetched_at": 1696118400})
    insert_or_update_item(db_path, {"workshop_id": 2, "fetch_status": None}) # NULL fetch_status, should come first
    insert_or_update_item(db_path, {"workshop_id": 3, "fetch_status": 200, "api_fetched_at": 1696204800})
    
    items = get_next_items_to_fetch(db_path, limit=3)
    assert len(items) == 3
    assert isinstance(items[0], dict)
    
    # Extract IDs to check order
    item_ids = [item['workshop_id'] for item in items]
    assert item_ids[0] == 2
    assert item_ids[1] == 1
    assert item_ids[2] == 3

def test_search_items(db_path):
    """Tests search capabilities over title and description, and filtering by appid."""
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Apple Mod", "consumer_appid": 100})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "Banana Mod", "short_description": "Apple inside", "consumer_appid": 100})
    insert_or_update_item(db_path, {"workshop_id": 3, "title": "Apple Map", "consumer_appid": 200})
    
    # Text search
    results = search_items(db_path, query="Apple")
    assert len(results) == 3
    
    # Filter search
    results_appid = search_items(db_path, query="Apple", appid=100)
    assert len(results_appid) == 2
    ids = [r["workshop_id"] for r in results_appid]
    assert 1 in ids and 2 in ids

    # Filter by tags
    insert_or_update_item(db_path, {"workshop_id": 4, "title": "Mango Mod", "tags": "['fruit', 'sweet']"})
    results_tags = search_items(db_path, tags="fruit")
    assert len(results_tags) == 1
    assert results_tags[0]["workshop_id"] == 4

def test_delete_never_fetched_items(db_path):
    """Test clearing pending items (fetch_status NULL or 404 AND api_fetched_at NULL)."""
    # 1. Pending (fetch_status NULL, api_fetched_at NULL) - Should be removed
    insert_or_update_item(db_path, {"workshop_id": 1, "fetch_status": None, "api_fetched_at": None})
    # 2. Pending (fetch_status 404, api_fetched_at NULL) - Should be removed
    insert_or_update_item(db_path, {"workshop_id": 2, "fetch_status": 404, "api_fetched_at": None})
    # 3. Not Pending (fetch_status 200) - Should NOT be removed
    insert_or_update_item(db_path, {"workshop_id": 3, "fetch_status": 200, "api_fetched_at": None})
    # 4. Not Pending (has api_fetched_at) - Should NOT be removed
    insert_or_update_item(db_path, {"workshop_id": 4, "fetch_status": None, "api_fetched_at": 1672531200})

    insert_or_update_item(db_path, {"workshop_id": 5, "fetch_status": 200, "api_fetched_at": 1672531200})

    deleted_count = delete_never_fetched_items(db_path)
    assert deleted_count == 2
    
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT workshop_id FROM workshop_items ORDER BY workshop_id")
    ids = [row["workshop_id"] for row in cursor.fetchall()]
    conn.close()
    
    assert ids == [3, 4, 5]

def test_creator_table_operations(db_path):
    """Tests basic CRUD for the creators table."""
    from src.database import insert_or_update_creator, get_creator
    user_data = {"steamid": 12345, "personaname": "Test User"}
    insert_or_update_creator(db_path, user_data)
    
    user = get_creator(db_path, 12345)
    assert user["personaname"] == "Test User"
    
    # Update
    user_data["personaname"] = "Updated Name"
    insert_or_update_creator(db_path, user_data)
    user = get_creator(db_path, 12345)
    assert user["personaname"] == "Updated Name"

def test_creator_join_in_queries(db_path):
    """Verifies that queries return joined creator information."""
    from src.database import insert_or_update_creator, insert_or_update_item, search_items, get_item_details
    
    steamid = 76561198000000000
    insert_or_update_creator(db_path, {
        "steamid": steamid, 
        "personaname": "ModderOne",
        "personaname_en": "TranslatedModder"
    })
    
    insert_or_update_item(db_path, {
        "workshop_id": 999,
        "title": "Awesome Mod",
        "creator_steamid": steamid,
        "fetch_status": 200
    })
    
    # Test search_items join
    results = search_items(db_path, query="Awesome")
    assert len(results) == 1
    assert results[0]["personaname"] == "ModderOne"
    assert results[0]["personaname_en"] == "TranslatedModder"
    
    # Test get_item_details join
    details = get_item_details(db_path, 999)
    assert details["personaname"] == "ModderOne"
    assert details["personaname_en"] == "TranslatedModder"

def test_concurrent_read_write(db_path):
    """
    Tests that WAL mode allows simultaneous read and write without locking the DB.
    """
    def writer():
        for i in range(20):
            insert_or_update_item(db_path, {"workshop_id": i + 1000, "title": f"Item {i}"})
            time.sleep(0.005)

    def reader():
        for _ in range(20):
            search_items(db_path, query="Item")
            time.sleep(0.005)
            
    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    
    t1.start()
    t2.start()
    
    t1.join()
    t2.join()
    
    conn = get_connection(db_path)
    count = conn.execute("SELECT COUNT(*) as c FROM workshop_items").fetchone()["c"]
    conn.close()
    assert count == 20

def test_search_items_advanced_queries(db_path):
    from src.database import insert_or_update_item, search_items
    
    insert_or_update_item(db_path, {
        "workshop_id": 1, 
        "title": "Apple Banana", 
        "short_description": "Fruit mod", 
        "filename": "apple.zip", 
        "tags": "['fruit']",
        "creator_steamid": 123,
        "file_size": 1000
    })
    
    insert_or_update_item(db_path, {
        "workshop_id": 2, 
        "title": "Orange Mod", 
        "short_description": "Fruit mod too", 
        "filename": "orange.zip", 
        "tags": "['fruit', 'citrus']",
        "creator_steamid": 456,
        "file_size": 2000
    })

    # Test negative token
    results = search_items(db_path, query="Fruit -Banana")
    assert len(results) == 1
    assert results[0]["workshop_id"] == 2

    # Test mismatched quotes (fallback to split)
    results = search_items(db_path, query='Fruit "Mod')
    # Because shlex fails, it splits to ['Fruit', '"Mod']. Neither item has '"Mod' with a literal quote.
    assert len(results) == 0

    # Test matched quotes
    results = search_items(db_path, query='Fruit "Mod"')
    # shlex splits to ['Fruit', 'Mod']. Both items have 'mod' in short_description.
    assert len(results) == 2

    # Test summary_only
    results = search_items(db_path, query="Fruit", summary_only=True)
    assert len(results) == 2
    assert "short_description" not in results[0]  # Only essential columns returned

    # Test specific fields
    results = search_items(db_path, title_query="Apple", desc_query="Fruit", filename_query="apple", creator=123)
    assert len(results) == 1
    assert results[0]["workshop_id"] == 1

    # Test numeric_filters
    results = search_items(db_path, numeric_filters={"file_size": "> 1500"})
    assert len(results) == 1
    assert results[0]["workshop_id"] == 2

def test_get_all_creator_ids(db_path):
    from src.database import insert_or_update_item, get_all_creator_ids
    insert_or_update_item(db_path, {"workshop_id": 1, "creator_steamid": 999})
    insert_or_update_item(db_path, {"workshop_id": 2, "creator_steamid": 888})
    
    authors = get_all_creator_ids(db_path)
    assert 999 in authors
    assert 888 in authors
    assert len(authors) >= 2

def test_parse_query_empty():
    from src.database import _parse_query
    assert _parse_query("") == ([], [])
    assert _parse_query(None) == ([], [])

def test_search_items_pagination(db_path):
    from src.database import insert_or_update_item, search_items
    
    for i in range(1, 11):
        insert_or_update_item(db_path, {"workshop_id": 100 + i, "title": f"Page Item {i}"})
        
    results = search_items(db_path, query="Page Item", limit=5, sort_by="workshop_id", sort_order="ASC")
    assert len(results) == 5
    assert results[0]["workshop_id"] == 101
    
    results = search_items(db_path, query="Page Item", limit=5, offset=5, sort_by="workshop_id", sort_order="ASC")
    assert len(results) == 5
    assert results[0]["workshop_id"] == 106

def test_app_tracking(db_path):
    from src.database import get_app_tracking, update_app_tracking, save_enrichment_filters
    
    # Initially should be None
    assert get_app_tracking(db_path, 4000) is None
    
    # Test update_app_tracking (last_historical_date_scanned)
    update_app_tracking(db_path, 4000, 1600000000, 3600*24*30)
    tracking = get_app_tracking(db_path, 4000)
    assert tracking["last_historical_date_scanned"] == 1600000000
    assert tracking["window_size"] == 3600*24*30
    assert tracking["filter_text"] == ''
    assert tracking["required_tags"] == '[]'
    assert tracking["excluded_tags"] == '[]'
    
    # Update again
    update_app_tracking(db_path, 4000, 1700000000, 3600*24*30*2)
    tracking = get_app_tracking(db_path, 4000)
    assert tracking["last_historical_date_scanned"] == 1700000000
    assert tracking["window_size"] == 3600*24*30*2

    # Test save_enrichment_filters
    save_enrichment_filters(db_path, 4000, "test search", ["tag1", "tag2"], ["excl1"])
    tracking = get_app_tracking(db_path, 4000)
    assert tracking["filter_text"] == "test search"
    assert tracking["required_tags"] == json.dumps(["tag1", "tag2"])
    assert tracking["excluded_tags"] == json.dumps(["excl1"])
    
    # Ensure last_historical_date_scanned is NOT updated by save_enrichment_filters
    assert tracking["last_historical_date_scanned"] == 1700000000

    # Test saving only some filters
    save_enrichment_filters(db_path, 4000, required_tags=["new_tag"])
    tracking = get_app_tracking(db_path, 4000)
    assert tracking["filter_text"] == "" # Should revert to default if not provided
    assert tracking["required_tags"] == json.dumps(["new_tag"])
    assert tracking["excluded_tags"] == '[]'


def test_get_next_items_to_fetch_priority(db_path):
    from src.database import get_next_items_to_fetch, insert_or_update_item
    import time
    
    # 1. Successfully scraped items, stalest first (fetch_status = 200)
    insert_or_update_item(db_path, {"workshop_id": 1, "fetch_status": 200, "api_fetched_at": 1672531200})
    # Item 2 is recent, so it should be excluded from re-scraping
    recent_epoch = int(time.time()) - 86400
    insert_or_update_item(db_path, {"workshop_id": 2, "fetch_status": 200, "api_fetched_at": recent_epoch})
    
    # 2. Partially failed items (fetch_status = 206) - with different subscription counts
    insert_or_update_item(db_path, {"workshop_id": 3, "fetch_status": 206, "api_fetched_at": 1735689600, "subscriptions": 100})
    insert_or_update_item(db_path, {"workshop_id": 4, "fetch_status": 206, "api_fetched_at": 1738368000, "subscriptions": 500})
    
    # 3. Unscraped new items (fetch_status IS NULL)
    insert_or_update_item(db_path, {"workshop_id": 5})
    insert_or_update_item(db_path, {"workshop_id": 6})
    
    # 4. Old items (older than 7 days)
    insert_or_update_item(db_path, {"workshop_id": 7, "fetch_status": 200, "api_fetched_at": 1640995200})

    items = get_next_items_to_fetch(db_path, limit=7)
    item_ids = [item['workshop_id'] for item in items]
    
    # All items have api_priority=3 (default), ordered by api_fetched_at ASC
    assert len(item_ids) == 7
    # NULL api_fetched_at sorts first, then oldest first
    assert item_ids[0:2] == [5, 6]      # NULL api_fetched_at
    assert item_ids[2:5] == [7, 1, 3]   # oldest api_fetched_at
    assert item_ids[5:7] == [4, 2]      # newer

def test_get_creator_not_found(db_path):
    from src.database import get_creator
    assert get_creator(db_path, 99999) is None

def test_get_item_details_missing_user(db_path):
    from src.database import insert_or_update_item, get_item_details
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Orphan Item", "creator_steamid": 99999})
    details = get_item_details(db_path, 1)
    assert details is not None
    assert details["title"] == "Orphan Item"
    assert details.get("personaname") is None

def test_toggle_and_query_queued_items(db_path):
    from src.database import toggle_subscription_queue, get_subscription_queue_items
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Item A"})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "Item B"})

    toggle_subscription_queue(db_path, 1)
    queued = get_subscription_queue_items(db_path)
    assert len(queued) == 1
    assert queued[0]["workshop_id"] == 1
    assert queued[0]["title"] == "Item A"

    toggle_subscription_queue(db_path, 1)
    queued = get_subscription_queue_items(db_path)
    assert len(queued) == 0

def test_get_db_stats_empty(db_path):
    from src.database import get_db_stats
    stats = get_db_stats(db_path)
    assert stats["status_counts"] == []
    assert "translation_status" in stats

def test_get_db_stats_with_data(db_path):
    from src.database import get_db_stats, insert_or_update_item
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Test", "fetch_status": 200, "tags": json.dumps([{"tag": "mod"}])})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": None, "fetch_status": None})
    stats = get_db_stats(db_path)
    assert len(stats["status_counts"]) == 2
    assert stats["tag_counts"].get("mod", 0) >= 1

def test_get_db_stats_tracking_missing(db_path):
    from src.database import get_db_stats, get_app_tracking
    stats = get_db_stats(db_path)
    assert stats["app_stats"] == []

def test_get_app_tracking_missing(db_path):
    from src.database import get_app_tracking
    assert get_app_tracking(db_path, 999) is None


def test_translation_priority_set_on_flag(db_path):
    """Bug #1 regression: queue_field_for_translation must set translation_priority
    on the parent item so the web UI detail poll detects queued work."""
    from src.database import insert_or_update_item, queue_field_for_translation, get_connection

    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト", "fetch_status": 200})

    queue_field_for_translation(db_path, "item", 1, "title_en", "テスト", 10)

    conn = get_connection(db_path)
    prio = conn.execute(
        "SELECT translation_priority FROM workshop_items WHERE workshop_id = 1"
    ).fetchone()[0]
    conn.close()
    assert prio == 10, f"Expected translation_priority=10, got {prio}"


def test_classify_translation_queued(db_path):
    """Bug #3: _classify_translation_status must show 'Queued' for items with
    non-ASCII text that have been flagged for translation."""
    from src.database import insert_or_update_item, queue_field_for_translation, get_db_stats

    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト", "fetch_status": 200})
    queue_field_for_translation(db_path, "item", 1, "title_en", "テスト", 5)

    stats = get_db_stats(db_path)
    assert stats["translation_status"]["Queued"] >= 1


def test_wilson_cutoffs_with_full_text(db_path):
    """Issue #5: compute_wilson_cutoffs must handle Full Text filters.
    A Full Text filter should not crash or return empty cutoffs."""
    from src.database import compute_wilson_cutoffs, insert_or_update_item, get_connection

    for i in range(1, 101):
        insert_or_update_item(db_path, {
            "workshop_id": i, "title": f"item {i}",
            "subscriptions": i * 100, "lifetime_subscriptions": 500,
            "favorited": i * 10, "views": 1000, "fetch_status": 200,
            "wilson_subscription_score": min(1.0, i / 100), "wilson_favorite_score": min(1.0, i / 200),
        })

    conn = get_connection(db_path)
    conn.execute("INSERT INTO workshop_fts(workshop_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()

    cutoffs = compute_wilson_cutoffs(db_path, [
        {"field": "Full Text", "op": "contains", "value": "item"}
    ])
    assert "wilson_subscription_p50" in cutoffs
    assert cutoffs["wilson_subscription_p50"] > 0


def test_ensure_tag_ids_new_and_existing(db_path):
    from src.database import _ensure_tag_ids, get_connection

    ids1 = _ensure_tag_ids(db_path, ["Anime", "Wallpaper", "4K"])
    assert len(ids1) == 3
    assert sorted(ids1) == [1, 2, 3]

    # Re-call with mix of new and existing; IDs must be consistent
    ids2 = _ensure_tag_ids(db_path, ["Anime", "NewTag"])
    assert len(ids2) == 2
    assert sorted(ids2) == [1, 4]

    # Verify tags table
    conn = get_connection(db_path)
    names = {r[0] for r in conn.execute("SELECT tag_name FROM tags").fetchall()}
    conn.close()
    assert names == {"Anime", "Wallpaper", "4K", "NewTag"}


def test_swap_tag_ids_preserves_associations(db_path):
    from src.database import insert_or_update_item, _ensure_tag_ids, swap_tag_ids, get_connection

    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Mod A", "fetch_status": 200})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "Mod B", "fetch_status": 200})

    ids = _ensure_tag_ids(db_path, ["Common", "Rare"])
    assert len(ids) == 2
    common_id, rare_id = sorted(ids)

    conn = get_connection(db_path)
    conn.executemany(
        "INSERT INTO workshop_tags (workshop_id, tag_id) VALUES (?, ?)",
        [(1, common_id), (1, rare_id), (2, common_id)]
    )
    conn.commit()
    conn.close()

    swap_tag_ids(db_path, common_id, rare_id)

    conn = get_connection(db_path)
    tags_for_1 = {r[0] for r in conn.execute(
        "SELECT t.tag_name FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id = 1").fetchall()}
    tags_for_2 = {r[0] for r in conn.execute(
        "SELECT t.tag_name FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id = 2").fetchall()}
    # Mod A should still have both tags
    assert tags_for_1 == {"Common", "Rare"}
    # Mod B should still have Common only
    assert tags_for_2 == {"Common"}
    conn.close()


def test_compact_tag_ids_moves_common_to_low_ids(db_path):
    from src.database import _ensure_tag_ids, compact_tag_ids, get_connection

    ids = _ensure_tag_ids(db_path, ["RareA", "RareB", "Common", "RareC"])
    conn = get_connection(db_path)
    # Common has ID 3, give it higher frequency
    for wid in range(1, 101):
        conn.execute(
            "INSERT INTO workshop_tags (workshop_id, tag_id) VALUES (?, ?)",
            (wid, ids[2])
        )
    conn.commit()
    conn.close()

    compact_tag_ids(db_path)

    conn = get_connection(db_path)
    # Common tag should now have a lower ID (<= 3 since there are 4 tags)
    common_id = conn.execute("SELECT tag_id FROM tags WHERE tag_name = 'Common'").fetchone()[0]
    assert common_id <= 3
    conn.close()


def test_build_fts_clause_operators(db_path):
    """All FTS5 operators must produce valid MATCH clauses."""
    from src.database import _build_fts_clause, insert_or_update_item, get_connection, search_items

    for i in range(1, 20):
        insert_or_update_item(db_path, {
            "workshop_id": i, "title": f"sword mod {i}",
            "short_description": "test", "extended_description": "test",
            "subscriptions": 100, "lifetime_subscriptions": 200,
            "favorited": 10, "views": 1000, "fetch_status": 200,
            "wilson_subscription_score": 0.5, "wilson_favorite_score": 0.1,
        })

    conn = get_connection(db_path)
    conn.execute("INSERT INTO workshop_fts(workshop_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()

    # contains should find items
    results = search_items(db_path, filters=[
        {"field": "Full Text", "op": "contains", "value": "sword"}])
    assert len(results) > 0

    # does_not_contain should exclude
    results = search_items(db_path, filters=[
        {"field": "Full Text", "op": "does_not_contain", "value": "sword"}])
    assert len(results) == 0

    # is_empty / is_not_empty
    results = search_items(db_path, filters=[
        {"field": "Full Text", "op": "is_not_empty", "value": ""}])
    assert len(results) > 0

def test_save_enrichment_filters_defaults(db_path):
    from src.database import save_enrichment_filters, get_app_tracking
    save_enrichment_filters(db_path, 5000)
    tracking = get_app_tracking(db_path, 5000)
    assert tracking["filter_text"] == ""
    assert tracking["required_tags"] == "[]"
    assert tracking["excluded_tags"] == "[]"


def test_search_on_large_dataset(deterministic_db):
    """Basic search on the 10k-item deterministic database works end-to-end."""
    results = search_items(deterministic_db)
    assert len(results) == 10000
    # Filtered search returns proper subset
    filtered = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "contains", "value": "mod"}
    ])
    assert 0 < len(filtered) < len(results)


def test_stats_with_real_data(deterministic_db):
    """get_db_stats returns sensible counts on 10k-item dataset."""
    from src.database import get_db_stats
    stats = get_db_stats(deterministic_db)
    total_from_status = sum(s["count"] for s in stats["status_counts"])
    assert total_from_status == 10000
    assert len(stats["tag_counts"]) == 10
    assert sum(stats["tag_counts"].values()) > 0


def test_get_next_items_to_fetch_priority_order(db_path):
    """Higher api_priority items are returned first."""
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 1, "api_fetched_at": 100})
    insert_or_update_item(db_path, {"workshop_id": 2, "api_priority": 10, "api_fetched_at": 200})
    insert_or_update_item(db_path, {"workshop_id": 3, "api_priority": 5, "api_fetched_at": 300})
    items = get_next_items_to_fetch(db_path, limit=3)
    assert [i["workshop_id"] for i in items] == [2, 3, 1]


def test_get_next_items_to_fetch_excludes_dead(db_path):
    """Items with fetch_status=-1 are not returned."""
    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 10, "fetch_status": -1})
    insert_or_update_item(db_path, {"workshop_id": 2, "api_priority": 5, "fetch_status": 200})
    items = get_next_items_to_fetch(db_path, limit=2)
    assert [i["workshop_id"] for i in items] == [2]


def test_raise_web_scrape_priority_sets_priority(db_path):
    """raise_web_scrape_priority updates the web_scrape_priority column."""
    insert_or_update_item(db_path, {"workshop_id": 1})
    raise_web_scrape_priority(db_path, 1, 7)
    conn = get_connection(db_path)
    val = conn.execute("SELECT web_scrape_priority FROM workshop_items WHERE workshop_id=1").fetchone()[0]
    conn.close()
    assert val == 7


def test_raise_image_priority_max_semantics(db_path):
    """raise_image_priority uses MAX — never downgrades."""
    insert_or_update_item(db_path, {"workshop_id": 1, "image_priority": 10})
    raise_image_priority(db_path, 1, 3)
    conn = get_connection(db_path)
    val = conn.execute("SELECT image_priority FROM workshop_items WHERE workshop_id=1").fetchone()[0]
    conn.close()
    assert val == 10  # not downgraded to 3


def test_raise_api_priority_for_list_and_detail(db_path):
    """Bump functions set correct priority and skip dead items."""
    insert_or_update_item(db_path, {"workshop_id": 1})  # unscraped
    insert_or_update_item(db_path, {"workshop_id": 2, "api_priority": 8})  # already high
    insert_or_update_item(db_path, {"workshop_id": 3, "fetch_status": -1, "api_priority": 0})  # dead

    raise_api_priority_for_list(db_path, 1)
    raise_api_priority_for_list(db_path, 2)
    raise_api_priority_for_list(db_path, 3)

    conn = get_connection(db_path)
    p1 = conn.execute("SELECT api_priority FROM workshop_items WHERE workshop_id=1").fetchone()[0]
    p2 = conn.execute("SELECT api_priority FROM workshop_items WHERE workshop_id=2").fetchone()[0]
    p3 = conn.execute("SELECT api_priority FROM workshop_items WHERE workshop_id=3").fetchone()[0]
    conn.close()
    assert p1 == 5   # bumped from default 3 -> 5
    assert p2 == 8   # already high, not downgraded
    assert p3 == 0   # dead, not bumped

    # Detail bump
    raise_api_priority_for_detail(db_path, 1)
    conn = get_connection(db_path)
    p1 = conn.execute("SELECT api_priority FROM workshop_items WHERE workshop_id=1").fetchone()[0]
    conn.close()
    assert p1 == 10  # bumped to 10


def test_clear_subscription_queue(db_path):
    """clear_subscription_queue sets flag to 0."""
    insert_or_update_item(db_path, {"workshop_id": 1, "is_queued_for_subscription": 1})
    clear_subscription_queue(db_path, 1)
    queued = get_subscription_queue_items(db_path)
    assert not any(q["workshop_id"] == 1 for q in queued)


# ── v13 -> v14 migration cleanup ─────────────────────────────────────────────

def _make_v13_db(path: str):
    """Build a schema-v13 database (historical column names, all v13 columns)."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE workshop_items (
            workshop_id INTEGER PRIMARY KEY,
            status INTEGER,
            title TEXT,
            creator INTEGER,
            creator_appid INTEGER,
            consumer_appid INTEGER,
            filename TEXT,
            file_size INTEGER,
            preview_url TEXT,
            hcontent_file TEXT,
            hcontent_preview TEXT,
            short_description TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            visibility INTEGER,
            banned INTEGER,
            ban_reason TEXT,
            app_name TEXT,
            file_type INTEGER,
            subscriptions INTEGER,
            favorited INTEGER,
            views INTEGER,
            extended_description TEXT,
            language INTEGER,
            lifetime_subscriptions INTEGER,
            lifetime_favorited INTEGER,
            title_en TEXT,
            short_description_en TEXT,
            extended_description_en TEXT,
            translation_priority INTEGER DEFAULT 0,
            is_queued_for_subscription INTEGER DEFAULT 0,
            wilson_favorite_score REAL DEFAULT NULL,
            wilson_subscription_score REAL DEFAULT NULL,
            needs_web_scrape INTEGER DEFAULT 0,
            image_extension TEXT DEFAULT NULL,
            needs_image INTEGER DEFAULT 0,
            dt_found INTEGER,
            dt_updated INTEGER,
            dt_attempted INTEGER,
            dt_translated INTEGER,
            api_priority INTEGER NOT NULL DEFAULT 3
        )
    """)
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()


def test_migration_14_renames_and_cleans_data(tmp_path):
    """Migration 13->14 renames the clocks and cleans the misleading contents."""
    from src.database import initialize_database

    db = str(tmp_path / "v13.db")
    _make_v13_db(db)
    conn = sqlite3.connect(db)
    # 1: succeeded once (has Steam payload)
    conn.execute("INSERT INTO workshop_items (workshop_id, status, dt_found, dt_updated, dt_attempted, time_updated)"
                 " VALUES (1, 200, 100, 111, 222, 999)")
    # 2: failed/no payload (steam_updated_at NULL) but was attempted
    conn.execute("INSERT INTO workshop_items (workshop_id, status, dt_found, dt_updated, dt_attempted, time_updated)"
                 " VALUES (2, 500, 300, 333, 444, NULL)")
    # 3: anomalous row with dt_found NULL but a successful fetch
    conn.execute("INSERT INTO workshop_items (workshop_id, status, dt_found, dt_updated, dt_attempted, time_updated)"
                 " VALUES (3, 200, NULL, 555, 666, 888)")
    conn.commit()
    conn.close()

    initialize_database(db)

    conn = get_connection(db)
    # The chain runs to the terminal version; this test is about 13->14's effects.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == EXPECTED_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(workshop_items)")}
    assert {"first_seen_at", "api_fetched_at", "last_fetch_attempted_at",
            "scrape_version", "translate_version", "steam_created_at",
            "steam_updated_at"}.issubset(cols)

    rows = {r["workshop_id"]: dict(r) for r in conn.execute("SELECT * FROM workshop_items")}
    conn.close()

    # Attempt clock backfilled from the old dt_updated for every row that had one.
    assert rows[1]["last_fetch_attempted_at"] == 111
    assert rows[2]["last_fetch_attempted_at"] == 333
    # api_fetched_at keeps the value only where Steam content actually arrived.
    assert rows[1]["api_fetched_at"] == 111
    assert rows[2]["api_fetched_at"] is None
    # The pre-rename artefact is cleared where there is no Steam payload.
    assert rows[2]["scrape_version"] is None
    assert rows[1]["scrape_version"] == 222
    # The anomalous first_seen_at row is repaired from api_fetched_at.
    assert rows[3]["first_seen_at"] == 555


def test_migration_14_is_idempotent(tmp_path):
    from src.database import initialize_database

    db = str(tmp_path / "v13.db")
    _make_v13_db(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO workshop_items (workshop_id, status, dt_found, dt_updated, time_updated)"
                 " VALUES (1, 200, 100, 111, 999)")
    conn.commit()
    conn.close()

    initialize_database(db)
    conn = get_connection(db)
    cols_after_first = {r[1] for r in conn.execute("PRAGMA table_info(workshop_items)")}
    conn.close()

    initialize_database(db)  # must not raise or re-add legacy columns

    conn = get_connection(db)
    cols_after_second = {r[1] for r in conn.execute("PRAGMA table_info(workshop_items)")}
    conn.close()
    assert cols_after_first == cols_after_second
    assert "dt_updated" not in cols_after_second
    assert "api_fetched_at" in cols_after_second
    assert "last_fetch_attempted_at" in cols_after_second


# ── first_seen_at guard (bug fix) ────────────────────────────────────────────

def test_insert_first_seen_at_defaults_when_explicitly_none(db_path):
    """A caller passing first_seen_at=None must not suppress the default."""
    insert_or_update_item(db_path, {"workshop_id": 1, "first_seen_at": None})
    conn = get_connection(db_path)
    val = conn.execute("SELECT first_seen_at FROM workshop_items WHERE workshop_id=1").fetchone()[0]
    conn.close()
    assert val is not None

    # An explicit value is preserved.
    insert_or_update_item(db_path, {"workshop_id": 2, "first_seen_at": 123})
    conn = get_connection(db_path)
    val2 = conn.execute("SELECT first_seen_at FROM workshop_items WHERE workshop_id=2").fetchone()[0]
    conn.close()
    assert val2 == 123


# ── translation_queue.queued_at ──────────────────────────────────────────────

def test_queue_field_for_translation_stamps_queued_at(db_path):
    """New queue rows record our queue time; legacy NULLs are not fabricated."""
    from src.database import queue_field_for_translation

    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト", "fetch_status": 200})
    queue_field_for_translation(db_path, "item", 1, "title_en", "テスト", 5)

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT queued_at FROM translation_queue WHERE entity_id=1"
    ).fetchone()
    conn.close()
    assert row["queued_at"] is not None
    assert abs(row["queued_at"] - int(time.time())) < 60


def test_translation_queue_legacy_null_queued_at_sorts_first(db_path):
    """A NULL queued_at (unknown/legacy) must be served before a dated row at the
    same priority, so newly queued work cannot jump the backlog."""
    from src.database import get_next_batch_for_translation

    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO translation_queue (entity_type, entity_id, field, original_text, priority, queued_at) "
        "VALUES ('item', 1, 'title_en', 'legacy', 5, NULL)"
    )
    conn.execute(
        "INSERT INTO translation_queue (entity_type, entity_id, field, original_text, priority, queued_at) "
        "VALUES ('item', 2, 'title_en', 'dated', 5, ?)",
        (int(time.time()),)
    )
    conn.commit()
    conn.close()

    batch = get_next_batch_for_translation(db_path, limit=2)
    assert [row["entity_id"] for row in batch] == [1, 2]


# ── get_db_stats classify fix ─────────────────────────────────────────────────

def test_get_db_stats_fetch_recency_uses_last_fetch_attempted_at(db_path):
    """The fresh/stale/unknown breakdown measures OUR fetch recency, not Steam age."""
    from src.database import get_db_stats

    now = int(time.time())
    insert_or_update_item(db_path, {"workshop_id": 1, "fetch_status": 200,
                                    "last_fetch_attempted_at": now})
    insert_or_update_item(db_path, {"workshop_id": 2, "fetch_status": 200,
                                    "last_fetch_attempted_at": now - 40 * 86400})
    # A Steam-version value must NOT influence the fetch-recency breakdown.
    insert_or_update_item(db_path, {"workshop_id": 3, "fetch_status": 200,
                                    "scrape_version": now - 40 * 86400})

    stats = get_db_stats(db_path)
    assert stats["fetch_recency_counts"] == {"fresh": 1, "stale": 1, "unknown": 1}
    assert "dt_updated_counts" not in stats
    assert "highest_api_fetched_at" in stats


def test_the_dead_search_arguments_are_gone():
    """An argument with no reader reads as a control that does not exist."""
    import inspect

    from src.database import _build_tag_clause, get_next_items_to_fetch, search_items

    assert "staleness_days" not in inspect.signature(get_next_items_to_fetch).parameters
    search_params = inspect.signature(search_items).parameters
    for dead in ("tags_query", "required_tags", "excluded_tags"):
        assert dead not in search_params, f"{dead} has no reader in the body"
    assert "db_col" not in inspect.signature(_build_tag_clause).parameters
