import pytest
import json
from src.database import normalize_tags, initialize_database, insert_or_update_item, search_items

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test_tags.db")
    initialize_database(path)
    return path

@pytest.fixture
def tagged_db(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Apple", "tags": normalize_tags(["Food", "Fruit"])})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "Cereal", "tags": normalize_tags(["Cereal", "Breakfast"])})
    insert_or_update_item(db_path, {"workshop_id": 3, "title": "Banana", "tags": normalize_tags(["Food", "Fruit"])})
    insert_or_update_item(db_path, {"workshop_id": 4, "title": "Cool Mod", "tags": normalize_tags(["Very Cool Mod", "Gameplay"])})
    insert_or_update_item(db_path, {"workshop_id": 5, "title": "No tags here"})
    insert_or_update_item(db_path, {"workshop_id": 6, "title": "Bracket Tag", "tags": normalize_tags(["[TEST]", "Utility"])})
    insert_or_update_item(db_path, {"workshop_id": 7, "title": "Tangy", "tags": normalize_tags(["FruitLoops", "Tropical"])})
    return db_path

# ── normalize_tags unit tests ────────────────────────────────────────────────

def test_normalize_tags_api_format():
    assert normalize_tags([{"tag": "Addon"}, {"tag": "Tool"}]) == '["Addon", "Tool"]'

def test_normalize_tags_list_of_strings():
    assert normalize_tags(["Mod", "Tool"]) == '["Mod", "Tool"]'

def test_normalize_tags_json_string():
    assert normalize_tags('["Mod", "Tool"]') == '["Mod", "Tool"]'

def test_normalize_tags_deduplicates():
    assert normalize_tags(["Mod", "Mod", "Tool"]) == '["Mod", "Tool"]'

def test_normalize_tags_sorts():
    assert normalize_tags(["Tool", "Apple"]) == '["Apple", "Tool"]'

def test_normalize_tags_empty_list():
    assert normalize_tags([]) == '[]'

def test_normalize_tags_none():
    assert normalize_tags(None) == '[]'

# ── Tag query safety: no false positives from JSON syntax ────────────────────

def test_tags_structural_safety_bracket_open(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "contains", "value": "["}])
    assert len(results) == 0

def test_tags_structural_safety_comma(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "contains", "value": ","}])
    assert len(results) == 0

def test_tags_structural_safety_quote(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "contains", "value": '"'}])
    assert len(results) == 0

# ── Tag contains / does_not_contain (exact tag value match) ──────────────────

def test_tags_contains_exact_match(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "contains", "value": "Fruit"}])
    ids = {r["workshop_id"] for r in results}
    assert 1 in ids
    assert 3 in ids

def test_tags_contains_no_substring_false_positive(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "contains", "value": "Fruit"}])
    ids = {r["workshop_id"] for r in results}
    assert 7 not in ids

def test_tags_contains_excludes_other(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "contains", "value": "Fruit"}])
    ids = {r["workshop_id"] for r in results}
    assert 2 not in ids
    assert 4 not in ids
    assert 5 not in ids

def test_tags_multi_word_tag_contains(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "contains", "value": "Very Cool Mod"}])
    ids = {r["workshop_id"] for r in results}
    assert 4 in ids
    assert 1 not in ids

def test_tags_contains_module(tagged_db):
    insert_or_update_item(tagged_db, {"workshop_id": 100, "title": "Module Demo", "tags": normalize_tags(["Module", "Tutorial"])})
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "contains", "value": "Module"}])
    ids = {r["workshop_id"] for r in results}
    assert 100 in ids
    assert 4 not in ids

def test_tags_contains_mod_does_not_match_module(tagged_db):
    insert_or_update_item(tagged_db, {"workshop_id": 100, "title": "Module Demo", "tags": normalize_tags(["Module", "Tutorial"])})
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "contains", "value": "Mod"}])
    ids = {r["workshop_id"] for r in results}
    assert 100 not in ids

def test_tags_does_not_contain(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "does_not_contain", "value": "Cereal"}])
    ids = {r["workshop_id"] for r in results}
    assert 2 not in ids

def test_tags_does_not_contain_exact(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "does_not_contain", "value": "Fruit"}])
    ids = {r["workshop_id"] for r in results}
    assert 1 not in ids
    assert 2 in ids
    assert 7 in ids  # "FruitLoops" does not equal "Fruit"

# ── Compound filters with AND/OR on tag field ────────────────────────────────

def test_tags_and_filters(tagged_db):
    results = search_items(tagged_db, filters=[
        {"field": "Tags", "op": "contains", "value": "Food"},
        {"logic": "AND", "field": "Tags", "op": "contains", "value": "Fruit"},
    ])
    ids = {r["workshop_id"] for r in results}
    assert 1 in ids
    assert 3 in ids

def test_tags_and_filters_excludes_partial(tagged_db):
    results = search_items(tagged_db, filters=[
        {"field": "Tags", "op": "contains", "value": "Food"},
        {"logic": "AND", "field": "Tags", "op": "contains", "value": "Fruit"},
    ])
    ids = {r["workshop_id"] for r in results}
    assert 7 not in ids

def test_tags_or_filters(tagged_db):
    results = search_items(tagged_db, filters=[
        {"field": "Tags", "op": "contains", "value": "Cereal"},
        {"logic": "OR", "field": "Tags", "op": "contains", "value": "Gameplay"},
    ])
    ids = {r["workshop_id"] for r in results}
    assert 2 in ids
    assert 4 in ids
    assert 1 not in ids

def test_tags_does_not_contain_plus_contains_or(tagged_db):
    results = search_items(tagged_db, filters=[
        {"field": "Tags", "op": "contains", "value": "Food"},
        {"logic": "OR", "field": "Tags", "op": "contains", "value": "Cereal"},
    ])
    ids = {r["workshop_id"] for r in results}
    assert 1 in ids
    assert 2 in ids
    assert 5 not in ids

# ── Tag emptiness checks ─────────────────────────────────────────────────────

def test_tags_is_empty(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "is_empty"}])
    ids = {r["workshop_id"] for r in results}
    assert 5 in ids
    assert 1 not in ids

def test_tags_is_not_empty(tagged_db):
    results = search_items(tagged_db, filters=[{"field": "Tags", "op": "is_not_empty"}])
    ids = {r["workshop_id"] for r in results}
    assert 5 not in ids

# ── Legacy / other query paths ───────────────────────────────────────────────

def test_tags_legacy_tags_param(tagged_db):
    results = search_items(tagged_db, tags="Fruit")
    ids = {r["workshop_id"] for r in results}
    assert 1 in ids
    assert 3 in ids

def test_tags_global_query_no_false_hits(tagged_db):
    results = search_items(tagged_db, query="[")
    assert len(results) == 0

def test_tags_global_query_tag_syntax_safe(tagged_db):
    results = search_items(tagged_db, query='"')
    assert len(results) == 0
