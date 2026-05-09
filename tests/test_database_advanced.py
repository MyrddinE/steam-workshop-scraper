import pytest
from src.database import (
    insert_or_update_item,
    search_items,
    get_all_authors
)

@pytest.fixture
def db_path(tmp_path):
    from src.database import initialize_database
    path = str(tmp_path / "test_adv_search.db")
    initialize_database(path)
    # Populate with diverse data for advanced search
    items = [
        {"workshop_id": 1, "title": "Apple Mod", "short_description": "A simple apple.", "creator": "auth1", "filename": "apple.zip", "tags": '["Fruit", "Food"]', "subscriptions": 100, "views": 1000},
        {"workshop_id": 2, "title": "Banana Mod", "short_description": "Apple inside.", "creator": "auth2", "filename": "banana.vpk", "tags": '["Fruit"]', "subscriptions": 500, "views": 2000},
        {"workshop_id": 3, "title": "Apple Map", "short_description": "No fruit here.", "creator": "auth1", "filename": "map.bsp", "tags": '["Map"]', "subscriptions": 10, "views": 50},
        {"workshop_id": 4, "title": "Mickey Mouse Clubhouse", "short_description": "Disney stuff", "creator": "auth3", "filename": "mickey.zip", "tags": '["Toon"]', "subscriptions": 10000, "views": 50000},
        {"workshop_id": 5, "title": "Apple Mickey", "short_description": "Fruit mouse", "creator": "auth1", "filename": "applemic.zip", "tags": '["Toon", "Fruit"]', "subscriptions": 50, "views": 200},
        {"workshop_id": 6, "title": "Bad Script", "extended_description": "Contains evil script.", "creator": "auth2", "filename": "script.lua", "tags": '["Code"]', "subscriptions": 0, "views": 5}
    ]
    for item in items:
        insert_or_update_item(path, item)
    return path

def test_search_multiple_positive_terms(db_path):
    # Should match items containing BOTH "Apple" and "Mod"
    results = search_items(db_path, title_query="Apple Mod")
    assert len(results) == 1
    assert results[0]["workshop_id"] == 1

def test_search_negative_terms(db_path):
    # Title has Apple, but exclude Map
    results = search_items(db_path, title_query="Apple -Map")
    ids = [r["workshop_id"] for r in results]
    assert 3 not in ids # Apple Map should be excluded
    assert 1 in ids # Apple Mod should be included

def test_search_quoted_phrases_and_exclusions(db_path):
    # Title has Apple, exclude exact phrase "Mickey Mouse"
    results = search_items(db_path, title_query="Apple -\"Mickey Mouse\"")
    ids = [r["workshop_id"] for r in results]
    assert 5 in ids # "Apple Mickey" should be included

def test_search_combined_descriptions(db_path):
    # Tests that desc_query searches both short and extended desc at once
    results = search_items(db_path, desc_query="evil script")
    assert len(results) == 1
    assert results[0]["workshop_id"] == 6

def test_search_filename_and_tags(db_path):
    # File name has .zip, tags have Fruit
    results = search_items(db_path, filename_query=".zip", tags_query="Fruit")
    assert len(results) == 2
    ids = [r["workshop_id"] for r in results]
    assert 1 in ids
    assert 5 in ids

def test_search_numeric_inequalities(db_path):
    # Subscriptions >= 500
    results_subs = search_items(db_path, numeric_filters={"subscriptions": ">=500"})
    assert len(results_subs) == 2
    ids = [r["workshop_id"] for r in results_subs]
    assert 2 in ids
    assert 4 in ids
    
    # Views < 1000
    results_views = search_items(db_path, numeric_filters={"views": "< 1000"})
    assert len(results_views) == 3
    ids_views = [r["workshop_id"] for r in results_views]
    assert 3 in ids_views
    assert 5 in ids_views
    assert 6 in ids_views

    # Exact match fallback (no operator = exact)
    results_exact = search_items(db_path, numeric_filters={"subscriptions": "100"})
    assert len(results_exact) == 1
    assert results_exact[0]["workshop_id"] == 1

def test_search_by_author(db_path):
    # Filter strictly by creator ID
    results = search_items(db_path, creator="auth1")
    assert len(results) == 3
    for r in results:
        assert r["creator"] == "auth1"

def test_get_all_authors(db_path):
    """Test retrieving a list of unique authors for the TUI combo box."""
    authors = get_all_authors(db_path)
    assert len(authors) == 3
    assert "auth1" in authors
