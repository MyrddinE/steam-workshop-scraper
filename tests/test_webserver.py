import pytest
import json
from src.webserver import app, init_webserver
from src.database import initialize_database, insert_or_update_item, normalize_tags


@pytest.fixture
def web_client(tmp_path):
    db_path = str(tmp_path / "test_web.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "daemon": {"target_appids": [294100]}}
    init_webserver(db_path, config)
    return app.test_client(), db_path


def test_index_returns_html(web_client):
    client, _ = web_client
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'<!DOCTYPE html>' in resp.data


def test_layout_constrains_viewport(web_client):
    """Verify CSS rules that prevent page from expanding beyond viewport."""
    client, _ = web_client
    resp = client.get('/')
    html = resp.data.decode()
    assert 'height: 100vh' in html
    assert 'overflow: hidden' in html
    assert 'overflow-y: auto' in html
    assert 'flex-direction: column' in html
    assert 'min-height: 0' in html


def test_results_scroll_container_exists(web_client):
    """Verify the results list wraps in a dedicated scroll container."""
    client, _ = web_client
    resp = client.get('/')
    html = resp.data.decode()
    assert 'id="results-scroll"' in html
    assert 'id="results-pane"' in html
    assert 'id="results-list"' in html
    # results-list must be INSIDE results-scroll, not inside results-pane directly
    import re
    # Check order: results-scroll contains results-list
    match = re.search(r'id="results-scroll".*?id="results-list"', html, re.DOTALL)
    assert match is not None


def test_search_builder_not_in_scroll(web_client):
    """Verify search builder is outside the scroll container (pinned)."""
    client, _ = web_client
    resp = client.get('/')
    html = resp.data.decode()
    sb_pos = html.index('id="search-builder"')
    rs_pos = html.index('id="results-scroll"')
    assert sb_pos < rs_pos


def test_show_detail_has_desc_variable(web_client):
    """Regression: desc variable must be declared before use in showDetail."""
    import re
    client, _ = web_client
    resp = client.get('/')
    html = resp.data.decode()
    js = re.search(r'<script>(.*?)</script>', html, re.DOTALL)
    assert js, "No <script> block found"
    code = js.group(1)
    sd_start = code.index('function showDetail')
    sd_body = code[sd_start:]
    desc_decl = re.search(r'\blet desc\b|\bvar desc\b|\bconst desc\b', sd_body)
    assert desc_decl, "desc variable not declared in showDetail"
    # desc must be declared before the final pane.innerHTML (the template literal)
    inners = [m.start() for m in re.finditer(r'pane\.innerHTML', sd_body)]
    assert len(inners) >= 2, "expected at least 2 pane.innerHTML calls in showDetail"
    template_pos = inners[-1]  # the last one is the template literal
    assert desc_decl.start() < template_pos, "desc declared AFTER template literal"


def test_show_detail_has_try_catch(web_client):
    """Regression: showDetail must wrap fetch in try/catch."""
    client, _ = web_client
    resp = client.get('/')
    html = resp.data.decode()
    import re
    js = re.search(r'<script>(.*?)</script>', html, re.DOTALL).group(1)
    # showDetail function should have try/catch around the fetch
    sd_start = js.index('function showDetail')
    sd_body = js[sd_start:sd_start + 2000]
    assert 'try {' in sd_body
    assert 'catch' in sd_body


def test_search_returns_json(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Test Mod", "creator": 100, "status": 200})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "Other Mod", "creator": 200, "status": 200})

    resp = client.post('/api/search', json={"sort_by": "title", "sort_order": "ASC", "limit": 10})
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert len(data) == 2


def test_search_with_filters(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Apple Mod", "subscriptions": 500, "status": 200})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "Banana Mod", "subscriptions": 10, "status": 200})

    resp = client.post('/api/search', json={
        "filters": [{"field": "Subs", "op": "gte", "value": 100}],
        "sort_by": "title"
    })
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert len(data) == 1
    assert data[0]["workshop_id"] == 1


def test_search_pagination(web_client):
    client, db_path = web_client
    for i in range(1, 11):
        insert_or_update_item(db_path, {"workshop_id": i, "title": f"Item {i}", "status": 200})

    resp = client.post('/api/search', json={"limit": 5, "offset": 0, "sort_by": "workshop_id"})
    data = json.loads(resp.data)
    assert len(data) == 5

    resp2 = client.post('/api/search', json={"limit": 5, "offset": 5, "sort_by": "workshop_id"})
    data2 = json.loads(resp2.data)
    assert len(data2) == 5
    assert data[0]["workshop_id"] != data2[0]["workshop_id"]


def test_item_detail(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {
        "workshop_id": 99, "title": "Detail Mod", "creator": 111,
        "subscriptions": 100, "views": 1000, "status": 200,
        "tags": normalize_tags(["Mod", "Test"]),
    })

    resp = client.get('/api/item/99')
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["title"] == "Detail Mod"
    assert data["subscriptions"] == 100
    assert data["views"] == 1000


def test_item_not_found(web_client):
    client, _ = web_client
    resp = client.get('/api/item/99999')
    assert resp.status_code == 404


def test_authors(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "creator": 123, "title": "A", "status": 200})
    insert_or_update_item(db_path, {"workshop_id": 2, "creator": 456, "title": "B", "status": 200})

    resp = client.get('/api/authors')
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "123" in data or 123 in data


def test_tags(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "Tagged", "status": 200,
        "tags": normalize_tags(["RTS", "Sci-Fi"]),
    })

    resp = client.get('/api/tags')
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "RTS" in data


def test_stats(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Stats Mod", "status": 200})

    resp = client.get('/api/stats')
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "status_counts" in data
    assert "translation_status" in data


def test_analysis(web_client):
    client, db_path = web_client
    import time
    now = int(time.time())
    for i in range(20):
        insert_or_update_item(db_path, {
            "workshop_id": i + 1,
            "time_created": now - i * 86400,
            "views": 100,
        })

    resp = client.get('/api/analysis?bucket_days=7')
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["items_analyzed"] == 20
    assert len(data["buckets"]) > 0


def test_save_filter(web_client):
    client, _ = web_client
    resp = client.post('/api/save_filter', json={
        "filters": [
            {"field": "Title", "op": "contains", "value": "Test"},
            {"field": "Subs", "op": "gte", "value": 100},
        ]
    })
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["ok"] is True
    assert data["appid"] == 294100


def test_save_filter_no_appid(web_client):
    client, db_path = web_client
    config = {"database": {"path": db_path}, "daemon": {}}
    init_webserver(db_path, config)
    resp = client.post('/api/save_filter', json={"filters": [{"field": "Title", "op": "contains", "value": "X"}]})
    assert resp.status_code == 400


def test_image_serve_missing(web_client):
    client, _ = web_client
    resp = client.get('/images/nonexistent.jpg')
    assert resp.status_code == 404


def test_bump_image_list(web_client):
    client, _ = web_client
    resp = client.post('/api/bump_image_list/1')
    assert resp.status_code == 200


def test_bump_image_detail(web_client):
    client, _ = web_client
    resp = client.post('/api/bump_image_detail/1')
    assert resp.status_code == 200


def test_detail_pane_has_image_markup(web_client):
    client, _ = web_client
    resp = client.get('/')
    html = resp.data.decode()
    assert 'image_extension' in html
    assert 'detail-image' in html


def test_sse_endpoint_returns_stream(web_client):
    client, _ = web_client
    resp = client.get('/api/events')
    assert resp.status_code == 200
    assert resp.mimetype == 'text/event-stream'
    assert b'data: ' in next(resp.response)


def test_notify_pushes_event(web_client):
    from src.webserver import _notify_web_clients, _event_queues
    import queue
    q = queue.Queue()
    _event_queues.append(q)
    try:
        _notify_web_clients("test", {"id": 1})
        msg = q.get(timeout=2)
        assert '"type": "test"' in msg
        assert '"id": 1' in msg
    finally:
        _event_queues.remove(q)
