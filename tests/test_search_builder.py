import pytest
from src.database import initialize_database, insert_or_update_item, search_items

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test_search.db")
    initialize_database(path)
    # Add some sample data
    items = [
        {"workshop_id": 1, "title": "Alpha Item", "file_size": 100, "subscriptions": 10, "tags": "['tag1']"},
        {"workshop_id": 2, "title": "Beta Item", "file_size": 200, "subscriptions": 20, "tags": "['tag2']"},
        {"workshop_id": 3, "title": "Gamma Item", "file_size": 300, "subscriptions": 30, "tags": "['tag1', 'tag2']"},
        {"workshop_id": 4, "title": None, "file_size": 400, "subscriptions": 40},
        {"workshop_id": 5, "title": "", "file_size": 500, "subscriptions": 50},
    ]
    for item in items:
        insert_or_update_item(path, item)
    return path

def test_search_contains(db_path):
    filters = [{"field": "title", "op": "contains", "value": "Alpha"}]
    results = search_items(db_path, filters=filters)
    assert len(results) == 1
    assert results[0]["workshop_id"] == 1

def test_search_is(db_path):
    filters = [{"field": "title", "op": "is", "value": "Beta Item"}]
    results = search_items(db_path, filters=filters)
    assert len(results) == 1
    assert results[0]["workshop_id"] == 2

def test_search_greater_than(db_path):
    filters = [{"field": "file_size", "op": "gt", "value": 250}]
    results = search_items(db_path, filters=filters)
    assert len(results) == 3
    assert {r["workshop_id"] for r in results} == {3, 4, 5}

def test_search_less_than_or_equal(db_path):
    filters = [{"field": "subscriptions", "op": "lte", "value": 20}]
    results = search_items(db_path, filters=filters)
    assert len(results) == 2
    assert {r["workshop_id"] for r in results} == {1, 2}

def test_search_is_empty(db_path):
    filters = [{"field": "title", "op": "is_empty"}]
    results = search_items(db_path, filters=filters)
    # 4 is None, 5 is ""
    assert len(results) == 2
    assert {r["workshop_id"] for r in results} == {4, 5}

def test_search_is_not_empty(db_path):
    filters = [{"field": "title", "op": "is_not_empty"}]
    results = search_items(db_path, filters=filters)
    assert len(results) == 3
    assert {r["workshop_id"] for r in results} == {1, 2, 3}

def test_search_combined_and(db_path):
    filters = [
        {"field": "file_size", "op": "gt", "value": 150},
        {"field": "subscriptions", "op": "lt", "value": 35},
        {"logic": "AND"}
    ]
    # Logic in list means combine previous terms with this logic?
    # Actually a better structure might be:
    # filters = [
    #   {"field": "file_size", "op": "gt", "value": 150},
    #   {"logic": "AND", "field": "subscriptions", "op": "lt", "value": 35}
    # ]
    # Or just assume AND for now and support OR explicitly.
    # The user said: "trailing buttons for 'and' 'or' and 'x'"
    # This implies a sequence: [Term1] [AND] [Term2] [OR] [Term3]
    filters = [
        {"field": "file_size", "op": "gt", "value": 150},
        {"logic": "AND", "field": "subscriptions", "op": "lt", "value": 35}
    ]
    results = search_items(db_path, filters=filters)
    # file_size > 150: 2, 3, 4, 5
    # subscriptions < 35: 1, 2, 3
    # AND: 2, 3
    assert len(results) == 2
    assert {r["workshop_id"] for r in results} == {2, 3}

def test_search_combined_or(db_path):
    filters = [
        {"field": "workshop_id", "op": "is", "value": 1},
        {"logic": "OR", "field": "workshop_id", "op": "is", "value": 5}
    ]
    results = search_items(db_path, filters=filters)
    assert len(results) == 2
    assert {r["workshop_id"] for r in results} == {1, 5}

def test_search_is_not(db_path):
    filters = [{"field": "workshop_id", "op": "is_not", "value": 1}]
    results = search_items(db_path, filters=filters)
    assert len(results) == 4
    assert 1 not in {r["workshop_id"] for r in results}

def test_search_gte(db_path):
    filters = [{"field": "file_size", "op": "gte", "value": 300}]
    results = search_items(db_path, filters=filters)
    assert len(results) == 3
    assert {r["workshop_id"] for r in results} == {3, 4, 5}

def test_search_invalid_filter(db_path):
    # Missing op or field should be ignored
    filters = [{"field": "title", "op": "", "value": "Alpha"}, {"field": "", "op": "is", "value": "Alpha"}]
    results = search_items(db_path, filters=filters)
    # Should ignore the filter and return all 5
    assert len(results) == 5

def test_search_sorting(db_path):
    # Sort by file_size descending
    results = search_items(db_path, sort_by="file_size", sort_order="DESC")
    assert results[0]["workshop_id"] == 5
    assert results[-1]["workshop_id"] == 1

    # Sort by title ascending
    # None/empty might be at the start or end depending on SQLite
    results = search_items(db_path, sort_by="title", sort_order="ASC")
    # In SQLite, NULL is smallest.
    assert results[0]["workshop_id"] in (4, 5)
