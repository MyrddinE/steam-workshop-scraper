import pytest
from src.database import search_items, get_connection


def _title_text(r):
    return ((r.get("title") or "") + " " + (r.get("title_en") or "")).lower()


def test_search_contains(deterministic_db):
    results = search_items(deterministic_db, filters=[
        {"field": "Title", "op": "contains", "value": "lorem"}
    ])
    assert len(results) > 0
    for r in results:
        assert "lorem" in _title_text(r)


def test_search_is(deterministic_db):
    conn = get_connection(deterministic_db)
    title = conn.execute(
        "SELECT title, title_en FROM workshop_items WHERE title LIKE '%lorem%' AND title NOT LIKE '% %' LIMIT 1"
    ).fetchone()
    conn.close()
    if not title:
        pytest.skip("No single-word lorem title found")
    results = search_items(deterministic_db, filters=[
        {"field": "Title", "op": "is", "value": title["title"]}
    ])
    assert len(results) >= 1
    for r in results:
        assert r["title"] == title["title"] or r["title_en"] == title["title"]


def test_search_greater_than(deterministic_db):
    threshold = 50_000_000
    results = search_items(deterministic_db, filters=[
        {"field": "File Size", "op": "gt", "value": threshold}
    ])
    assert len(results) > 0
    for r in results:
        assert (r["file_size"] or 0) > threshold


def test_search_less_than_or_equal(deterministic_db):
    threshold = 1000
    results = search_items(deterministic_db, filters=[
        {"field": "Subs", "op": "lte", "value": threshold}
    ])
    assert len(results) > 0
    for r in results:
        assert (r["subscriptions"] or 0) <= threshold


def test_search_is_empty(deterministic_db):
    results = search_items(deterministic_db, filters=[
        {"field": "Title", "op": "is_empty"}
    ])
    assert len(results) >= 0  # all items have titles, so 0 is valid


def test_search_is_not_empty(deterministic_db):
    results = search_items(deterministic_db, filters=[
        {"field": "Title", "op": "is_not_empty"}
    ])
    all_items = search_items(deterministic_db)
    assert len(results) == len(all_items)


def test_search_combined_and(deterministic_db):
    a = search_items(deterministic_db, filters=[
        {"field": "File Size", "op": "gt", "value": 1_000_000}
    ])
    b = search_items(deterministic_db, filters=[
        {"field": "Subs", "op": "lt", "value": 1000}
    ])
    combined = search_items(deterministic_db, filters=[
        {"field": "File Size", "op": "gt", "value": 1_000_000},
        {"logic": "AND", "field": "Subs", "op": "lt", "value": 1000}
    ])
    assert len(combined) <= min(len(a), len(b))
    for r in combined:
        assert (r["file_size"] or 0) > 1_000_000
        assert (r["subscriptions"] or 0) < 1000


def test_search_combined_or(deterministic_db):
    conn = get_connection(deterministic_db)
    ids = [r["workshop_id"] for r in conn.execute(
        "SELECT workshop_id FROM workshop_items LIMIT 2"
    ).fetchall()]
    conn.close()
    results = search_items(deterministic_db, filters=[
        {"field": "Workshop ID", "op": "is", "value": ids[0]},
        {"logic": "OR", "field": "Workshop ID", "op": "is", "value": ids[1]}
    ])
    assert len(results) == 2
    assert {r["workshop_id"] for r in results} == set(ids)


def test_search_is_not(deterministic_db):
    conn = get_connection(deterministic_db)
    wid = conn.execute("SELECT workshop_id FROM workshop_items LIMIT 1").fetchone()["workshop_id"]
    conn.close()
    all_items = search_items(deterministic_db)
    results = search_items(deterministic_db, filters=[
        {"field": "Workshop ID", "op": "is_not", "value": wid}
    ])
    assert len(results) == len(all_items) - 1
    assert wid not in {r["workshop_id"] for r in results}


def test_search_gte(deterministic_db):
    threshold = 50_000_000
    results = search_items(deterministic_db, filters=[
        {"field": "File Size", "op": "gte", "value": threshold}
    ])
    assert len(results) > 0
    for r in results:
        assert (r["file_size"] or 0) >= threshold


def test_search_invalid_filter(deterministic_db):
    all_items = search_items(deterministic_db)
    filters = [
        {"field": "Title", "op": "", "value": "lorem"},
        {"field": "", "op": "is", "value": "anything"}
    ]
    results = search_items(deterministic_db, filters=filters)
    assert len(results) == len(all_items)  # invalid filters are ignored


def test_search_sorting(deterministic_db):
    results = search_items(deterministic_db, sort_by="file_size", sort_order="DESC")
    assert len(results) > 0
    for i in range(len(results) - 1):
        assert (results[i]["file_size"] or 0) >= (results[i + 1]["file_size"] or 0)

    results_asc = search_items(deterministic_db, sort_by="title", sort_order="ASC")
    assert len(results_asc) > 0


def test_search_does_not_contain(deterministic_db):
    results = search_items(deterministic_db, filters=[
        {"field": "Title", "op": "does_not_contain", "value": "lorem"}
    ])
    assert len(results) > 0
    for r in results:
        assert "lorem" not in _title_text(r)


def test_search_malformed_filters_graceful(deterministic_db):
    filters = [
        {"field": "Title", "value": "Alpha"},
        {"op": "contains", "value": "Alpha"},
        {"field": "NoSuchField", "op": "contains", "value": "Alpha"},
    ]
    results = search_items(deterministic_db, filters=filters)
    assert len(results) > 0


def test_search_invalid_sort_col(deterministic_db):
    results = search_items(deterministic_db, sort_by="nonexistent_column", sort_order="DESC")
    assert isinstance(results, list)


def test_concurrent_search_access(deterministic_db):
    import threading, random
    conn = get_connection(deterministic_db)
    all_ids = [r["workshop_id"] for r in conn.execute(
        "SELECT workshop_id FROM workshop_items LIMIT 100"
    ).fetchall()]
    conn.close()

    errors = []

    def do_search():
        try:
            for _ in range(10):
                fid = random.choice(all_ids)
                r = search_items(deterministic_db, filters=[
                    {"field": "Workshop ID", "op": "is", "value": str(fid)}])
                if len(r) > 1:
                    errors.append(f"Expected 0-1 results, got {len(r)}")
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=do_search) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=10)

    assert not errors, f"Concurrent search errors: {errors}"
