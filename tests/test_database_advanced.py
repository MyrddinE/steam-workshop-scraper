import pytest
from src.database import (
    search_items,
    get_all_creator_ids,
    get_connection,
)


def _title_text(r):
    return ((r.get("title") or "") + " " + (r.get("title_en") or "")).lower()


def test_search_multiple_positive_terms(deterministic_db):
    results = search_items(deterministic_db, title_query="lorem ipsum")
    assert len(results) > 0
    for r in results:
        t = _title_text(r)
        assert "lorem" in t and "ipsum" in t


def test_search_negative_terms(deterministic_db):
    all_lorem = search_items(deterministic_db, title_query="lorem")
    filtered = search_items(deterministic_db, title_query="lorem -dolor")
    assert len(filtered) > 0
    assert len(filtered) < len(all_lorem)
    for r in filtered:
        t = _title_text(r)
        assert "lorem" in t
        assert "dolor" not in t


def test_search_quoted_phrases_and_exclusions(deterministic_db):
    conn = get_connection(deterministic_db)
    row = conn.execute(
        "SELECT title FROM workshop_items WHERE title LIKE '%lorem ipsum%' AND title LIKE '%dolor%' LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        pytest.skip("No title with 'lorem ipsum dolor' found")
    with_phrase = search_items(deterministic_db, title_query='"lorem ipsum"')
    without_word = search_items(deterministic_db, title_query='"lorem ipsum" -dolor')
    assert len(with_phrase) > 0
    assert len(without_word) < len(with_phrase)
    for r in without_word:
        t = _title_text(r)
        assert "lorem ipsum" in t
        assert "dolor" not in t


def test_search_combined_descriptions(deterministic_db):
    results = search_items(deterministic_db, desc_query="lorem ipsum dolor")
    assert len(results) > 0
    for r in results:
        short = (r.get("short_description") or "").lower()
        short_en = (r.get("short_description_en") or "").lower()
        ext = (r.get("extended_description") or "").lower()
        ext_en = (r.get("extended_description_en") or "").lower()
        combined = short + " " + short_en + " " + ext + " " + ext_en
        assert "lorem" in combined and "ipsum" in combined and "dolor" in combined


def test_search_filename_and_tags(deterministic_db):
    all_items = search_items(deterministic_db)
    with_tag = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "contains", "value": "mod"}
    ])
    mod_lorem = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "contains", "value": "mod"},
        {"field": "Title", "op": "contains", "value": "lorem"},
    ])
    assert len(with_tag) > 0
    assert len(with_tag) < len(all_items)
    assert len(mod_lorem) > 0
    assert len(mod_lorem) <= len(with_tag)
    tag_sets = _get_tag_sets(deterministic_db, [r["workshop_id"] for r in mod_lorem])
    for r, tags in zip(mod_lorem, tag_sets):
        assert "mod" in tags
        assert "lorem" in _title_text(r)


def test_search_numeric_inequalities(deterministic_db):
    results_subs = search_items(deterministic_db, filters=[
        {"field": "Subs", "op": "gte", "value": 1000}
    ])
    assert len(results_subs) > 0
    for r in results_subs:
        assert (r["subscriptions"] or 0) >= 1000

    results_views = search_items(deterministic_db, filters=[
        {"field": "Views", "op": "lt", "value": 100}
    ])
    assert len(results_views) > 0
    for r in results_views:
        assert (r["views"] or 0) < 100

    conn = get_connection(deterministic_db)
    sample = conn.execute(
        "SELECT workshop_id, subscriptions FROM workshop_items WHERE subscriptions > 0 LIMIT 1"
    ).fetchone()
    conn.close()
    if sample:
        results_exact = search_items(deterministic_db, filters=[
            {"field": "Subs", "op": "is", "value": sample["subscriptions"]}
        ])
        found = any(r["subscriptions"] == sample["subscriptions"] for r in results_exact)
        assert found


def test_search_by_author(deterministic_db):
    conn = get_connection(deterministic_db)
    author = conn.execute(
        "SELECT creator, COUNT(*) as cnt FROM workshop_items GROUP BY creator ORDER BY cnt DESC LIMIT 1"
    ).fetchone()["creator"]
    conn.close()
    results = search_items(deterministic_db, filters=[
        {"field": "Author ID", "op": "is", "value": author}
    ])
    assert len(results) > 0
    for r in results:
        assert r["creator"] == author


def test_get_all_creator_ids_advanced(deterministic_db):
    authors = get_all_creator_ids(deterministic_db)
    assert len(authors) > 0
    conn = get_connection(deterministic_db)
    expected = conn.execute(
        "SELECT COUNT(DISTINCT creator) FROM workshop_items WHERE creator IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    assert len(authors) == expected


def _get_tag_sets(db_path, wids):
    conn = get_connection(db_path)
    tag_sets = []
    for wid in wids:
        rows = conn.execute(
            "SELECT t.tag_name FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id=?",
            (wid,)
        ).fetchall()
        tag_sets.append({r["tag_name"] for r in rows})
    conn.close()
    return tag_sets
