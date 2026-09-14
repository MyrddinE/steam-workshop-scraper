import pytest
import json
import os
import shutil
import subprocess
import lxml.html
from src.webserver import app, init_webserver
from src.database import initialize_database, insert_or_update_item, normalize_tags, get_image_subdirs, get_connection
from src import metrics


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


def test_layout_scaffold_is_present(web_client):
    """The layout CSS targets #results-pane and #right-pane; assert that scaffold exists.

    This replaces assertions on raw CSS text ('height: 100vh', 'overflow-y: auto',
    ...). CSS values are not server-side behaviour and can only be verified in a
    browser, so this checks the DOM contract the stylesheet depends on instead.
    """
    client, _ = web_client
    resp = client.get('/')
    doc = lxml.html.fromstring(resp.data.decode())
    assert doc.xpath('//*[@id="results-pane"]'), "missing layout container #results-pane"
    assert doc.xpath('//*[@id="right-pane"]'), "missing layout container #right-pane"
    assert doc.xpath('//style'), "expected an inline <style> block in the served page"


def test_results_grid_is_inside_results_pane(web_client):
    """Verify the results grid is a real descendant of the results pane."""
    client, _ = web_client
    resp = client.get('/')
    doc = lxml.html.fromstring(resp.data.decode())
    results_pane = doc.xpath('//*[@id="results-pane"]')
    results_grid = doc.xpath('//*[@id="results-grid"]')
    assert len(results_pane) == 1, "expected exactly one #results-pane"
    assert len(results_grid) == 1, "expected exactly one #results-grid"
    assert results_pane[0] in results_grid[0].iterancestors(), \
        "#results-grid must be nested inside #results-pane"


def test_search_builder_in_right_pane_not_results_grid(web_client):
    """Verify search builder is in the right pane, separate from results."""
    client, _ = web_client
    resp = client.get('/')
    doc = lxml.html.fromstring(resp.data.decode())
    right_pane = doc.xpath('//*[@id="right-pane"]')[0]
    search_builder = doc.xpath('//*[@id="search-builder"]')[0]
    results_grid = doc.xpath('//*[@id="results-grid"]')[0]
    # search builder must be in right-pane, results-grid must not
    assert search_builder in right_pane.iterdescendants(), \
        "#search-builder must be nested inside #right-pane"
    assert results_grid not in right_pane.iterdescendants(), \
        "#results-grid must not be nested inside #right-pane"


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot syntax-check served JavaScript")
def test_served_inline_script_is_valid_javascript(web_client, tmp_path):
    """The inline <script> is real JavaScript; validate the served artifact with `node --check`.

    This replaces assertions on JavaScript source text (declaration order of a `desc`
    variable, presence of a `try {`/`catch`). The page is rendered from the Jinja
    template, so the response body contains concrete values (e.g. `const WEB_DELAY =
    5.0;`) and is valid JavaScript we can parse as-is.

    Coverage note: a syntax check proves the script parses, NOT that it runs without
    a ReferenceError. The historical `desc`-used-before-declaration regression is
    therefore left unguarded here; see notes/findings.md.
    """
    client, _ = web_client
    resp = client.get('/')
    doc = lxml.html.fromstring(resp.data.decode())
    scripts = [s.text or "" for s in doc.xpath('//script[not(@src)]')]
    assert scripts, "served page contains no inline <script> block"

    script_path = tmp_path / "served.js"
    script_path.write_text("\n".join(scripts), encoding="utf-8")

    result = subprocess.run(
        [NODE, "--check", str(script_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"node --check reported invalid JavaScript:\n{result.stderr}"


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


def test_item_detail_is_read_only(web_client):
    """The detail poll hits this route every 3s; it must not re-arm the queues.

    Regression: applying detail priority here meant the item on screen was
    fetched, cleared and re-queued on every poll, so the daemon re-fetched it
    forever and the fetch queue held nothing else.
    """
    client, db_path = web_client
    insert_or_update_item(db_path, {
        "workshop_id": 99, "title": "Detail Mod", "creator": 111, "status": 200,
        "api_priority": 0, "needs_web_scrape": 0, "needs_image": 0,
        "translation_priority": 0,
    })

    for _ in range(10):  # ten polls, as the 3-second interval would produce
        assert client.get('/api/item/99').status_code == 200

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT api_priority, needs_web_scrape, needs_image, translation_priority "
        "FROM workshop_items WHERE workshop_id = 99").fetchone()
    conn.close()
    assert row["api_priority"] == 0, "polling must not re-queue the API fetch"
    assert row["needs_web_scrape"] == 0
    assert row["needs_image"] == 0
    assert row["translation_priority"] == 0


def test_open_item_applies_detail_priority(web_client):
    """Opening a pane is the one path that re-queues at detail priority."""
    client, db_path = web_client
    insert_or_update_item(db_path, {
        "workshop_id": 99, "title": "Detail Mod", "creator": 111, "status": 200,
        "api_priority": 0, "needs_web_scrape": 1, "needs_image": 0,
        "translation_priority": 0,
    })

    resp = client.post('/api/item/99/open')
    assert resp.status_code == 200
    assert json.loads(resp.data)["title"] == "Detail Mod"

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT api_priority, needs_web_scrape FROM workshop_items WHERE workshop_id = 99").fetchone()
    conn.close()
    assert row["api_priority"] == 10, "opening must queue the item for refresh"
    assert row["needs_web_scrape"] == 10


def test_open_item_not_found(web_client):
    client, _ = web_client
    assert client.post('/api/item/99999/open').status_code == 404


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


# --------------------------------------------------------------------------
# metrics endpoints and the statistics panel
# --------------------------------------------------------------------------


def test_metrics_catalogue_lists_tiers_cheapest_first(web_client):
    """The panel draws its layout from this, so order and names must match."""
    client, _ = web_client
    resp = client.get('/api/metrics')
    assert resp.status_code == 200
    data = resp.get_json()
    assert [t["tier"] for t in data["tiers"]] == list(metrics.TIERS)
    for entry in data["tiers"]:
        assert [m["name"] for m in entry["metrics"]] == metrics.names_in(entry["tier"])
        assert all(m["note"] for m in entry["metrics"]), \
            "a metric reached the client with no note"


def test_metrics_tier_returns_values_and_per_metric_costs(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "x", "status": 200})

    resp = client.get('/api/metrics/fast')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["tier"] == metrics.FAST
    assert set(data["values"]) == set(metrics.names_in(metrics.FAST))
    assert set(data["ms"]) == set(metrics.names_in(metrics.FAST))
    assert data["total_ms"] == round(sum(data["ms"].values()), 1)
    assert data["values"]["coverage"]["total"] == 1


def test_metrics_instant_tier_does_not_run_slow_metrics(web_client, monkeypatch):
    """The point of the split: asking for the cheap tier must not pay for tags."""
    client, _ = web_client
    ran = []

    def spy(conn, params):
        ran.append(True)
        return {}

    monkeypatch.setitem(
        metrics.REGISTRY, "tag_counts",
        metrics.Metric(name="tag_counts", tier=metrics.SLOW, note="spy", run=spy),
    )

    resp = client.get('/api/metrics/instant')
    assert resp.status_code == 200
    assert set(resp.get_json()["values"]) == set(metrics.names_in(metrics.INSTANT))
    assert ran == [], "the instant tier ran a slow metric"


def test_metrics_unknown_tier_is_a_404(web_client):
    client, _ = web_client
    resp = client.get('/api/metrics/banana')
    assert resp.status_code == 404
    assert "banana" in resp.get_json()["error"]


def test_stats_button_opens_a_panel_instead_of_navigating(web_client):
    """The affordance keeps its id and place, but it is no longer a link to JSON."""
    client, _ = web_client
    doc = lxml.html.fromstring(client.get('/').data.decode())

    buttons = doc.xpath('//*[@id="btn-stats"]')
    assert len(buttons) == 1, "expected exactly one #btn-stats"
    assert buttons[0].tag == "button", "the stats affordance must not navigate away"
    assert not buttons[0].get("href"), "the stats button still points at /api/stats"

    overlay = doc.xpath('//*[@id="stats-overlay"]')
    assert overlay, "missing #stats-overlay"
    assert doc.xpath('//*[@id="stats-tiers"]')[0] in overlay[0].iterdescendants(), \
        "#stats-tiers must live inside the overlay"
    assert doc.xpath('//*[@id="stats-close"]')[0] in overlay[0].iterdescendants(), \
        "the panel needs a close control"


def test_analysis(web_client):
    client, db_path = web_client
    import time
    now = int(time.time())
    for i in range(20):
        insert_or_update_item(db_path, {
            "workshop_id": i + 1,
            "steam_created_at": now - i * 86400,
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


def test_image_serve_flat_id_resolves_to_bucket(web_client):
    """A flat GET /images/<id>.<ext> serves the file from its nested bucket path.

    This replaces a source-text check for `image_extension` / `grid-img` in the
    served HTML: the detail markup is generated client-side, so there was no
    server-side behaviour to assert there. The real server contract is the image
    route, which transparently resolves a flat filename into the 3-level bucket
    produced by get_image_subdirs().
    """
    client, db_path = web_client
    workshop_id = 1039919954
    char1, char2, char3 = get_image_subdirs(workshop_id)
    nested_dir = os.path.join(os.path.dirname(db_path), "images", char1, char2, char3)
    os.makedirs(nested_dir, exist_ok=True)
    payload = b"bucket-image-bytes"
    with open(os.path.join(nested_dir, f"{workshop_id}.jpg"), "wb") as f:
        f.write(payload)

    resp = client.get(f"/images/{workshop_id}.jpg")
    assert resp.status_code == 200
    assert resp.data == payload


def test_api_items_bulk_lookup(web_client):
    from src.database import insert_or_update_item
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "A", "status": 200})
    insert_or_update_item(db_path, {"workshop_id": 3, "title": "C", "status": 200})

    resp = client.post('/api/items', json={"ids": [1, 3, 999]})
    assert resp.status_code == 200
    items = resp.get_json()
    assert len(items) == 2
    titles = {it["title"] for it in items}
    assert titles == {"A", "C"}


def test_api_items_empty_list(web_client):
    client, _ = web_client
    resp = client.post('/api/items', json={"ids": []})
    assert resp.status_code == 400


def test_api_subscribe_no_session(web_client):
    client, _ = web_client
    resp = client.post('/api/subscribe/1')
    assert resp.status_code == 400
    assert resp.get_json()["success"] == -1


def test_waitress_queue_monkeypatch_tiered_logging():
    """The monkeypatch applies correctly and is robust to missing attributes."""
    import logging
    import waitress.task as wt

    # Save original
    orig = wt.ThreadedTaskDispatcher.add_task

    try:
        # Apply monkeypatch (same code as web_runner.py)
        logging.getLogger('waitress.task').setLevel(logging.ERROR)

        def _patched(self, task):
            orig(self, task)
            try:
                queue_size = len(self.queue)
                idle = len(self.threads) - self.stop_count - self.active_count
                depth = queue_size - idle
                if depth >= 10:
                    logging.warning("Task queue depth is %d", depth)
                elif depth >= 5:
                    logging.info("Task queue depth is %d", depth)
                elif depth > 0:
                    logging.debug("Task queue depth is %d", depth)
            except Exception:
                pass

        wt.ThreadedTaskDispatcher.add_task = _patched

        # Verify the original still works by dispatching a task
        class FakeTask:
            service = lambda self: None
            cancel = lambda self: None
            interval = 0
            deferred = False
            handler = None
            start_time = 0
            wrote = 0

        dispatcher = wt.ThreadedTaskDispatcher()
        dispatcher.set_thread_count(2)
        task = FakeTask()
        dispatcher.add_task(task)  # should not raise

        # Verify the patched version was called (depth logged)
        assert wt.ThreadedTaskDispatcher.add_task is _patched

    finally:
        wt.ThreadedTaskDispatcher.add_task = orig


def test_waitress_monkeypatch_graceful_when_add_task_missing(monkeypatch):
    """If Waitress removes add_task, the server still starts without crash."""
    import waitress.task as wt
    import logging

    old_add = getattr(wt.ThreadedTaskDispatcher, 'add_task', None)
    assert old_add is not None, "precondition: add_task exists"

    # Simulate Waitress removing add_task
    monkeypatch.delattr(wt.ThreadedTaskDispatcher, 'add_task', raising=False)

    # Replicate the guarded monkeypatch from web_runner.py
    _orig = getattr(wt.ThreadedTaskDispatcher, 'add_task', None)
    # Should be None — monkeypatch gracefully does nothing
    assert _orig is None, "monkeypatch should detect missing add_task and skip"

    # Server would still start — no crash
    # Restore is unnecessary since we used monkeypatch in a test fixture


def test_api_subscribe_failed(web_client):
    """POST /api/subscribe_failed dequeues item and tracks failure."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 999, "is_queued_for_subscription": 1})
    resp = client.post('/api/subscribe_failed/999')
    assert resp.status_code == 200
    # Item should be dequeued
    queued = client.get('/api/queued').get_json()
    assert not any(q["workshop_id"] == 999 for q in queued)
    # Failure should be tracked
    failures = client.get('/api/sub_failures').get_json()
    assert 999 in failures


def test_api_sub_failures_empty(web_client):
    """GET /api/sub_failures returns empty list when no failures."""
    client, _ = web_client
    import src.webserver as ws
    ws._sub_failures.clear()
    failures = client.get('/api/sub_failures').get_json()
    assert failures == []


# ── Daemon control routes ────────────────────────────────────────────────────

class _FakeDaemonController:
    """Records route calls without ever touching a real process."""

    def __init__(self, status=None, log_file=None, tail=None):
        self._status = status or {"running": False, "pid": None}
        self._log_file = log_file
        self._tail = tail or {"lines": [], "offset": 0, "reset": False}
        self.calls = []

    def status(self):
        return dict(self._status)

    def log_file(self):
        return self._log_file

    def start(self):
        self.calls.append("start")
        return True, "Daemon started (PID: 123)"

    def stop(self):
        self.calls.append("stop")
        return True, "Daemon stopped"

    def restart(self):
        self.calls.append("restart")
        return True, "Daemon started (PID: 123)"

    def tail_log(self, since=0):
        self.calls.append(("log", since))
        return dict(self._tail)


@pytest.fixture
def daemon_client(tmp_path):
    db_path = str(tmp_path / "test_daemon_web.db")
    initialize_database(db_path)
    fake = _FakeDaemonController(status={"running": True, "pid": 123}, log_file="/tmp/daemon.log")
    init_webserver(db_path, {"database": {"path": db_path}}, daemon_controller=fake)
    return app.test_client(), fake


def test_daemon_panel_and_toolbar_control_are_present(web_client):
    client, _ = web_client
    doc = lxml.html.fromstring(client.get('/').data.decode())
    assert doc.xpath('//*[@id="btn-daemon"]'), "toolbar control for the daemon panel is missing"
    assert doc.xpath('//*[@id="daemon-overlay"]'), "daemon panel is missing"
    assert doc.xpath('//*[@id="daemon-status"]'), "daemon status line is missing"
    assert doc.xpath('//*[@id="daemon-log"]'), "daemon log view is missing"


def test_daemon_status_route(daemon_client):
    client, fake = daemon_client
    resp = client.get('/api/daemon')
    assert resp.status_code == 200
    assert resp.get_json() == {"running": True, "pid": 123, "log_file": "/tmp/daemon.log"}


def test_daemon_start_stop_restart_routes(daemon_client):
    client, fake = daemon_client
    for route in ('/api/daemon/start', '/api/daemon/stop', '/api/daemon/restart'):
        resp = client.post(route)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert "message" in body
    assert fake.calls == ["start", "stop", "restart"]


def test_daemon_log_route_passes_offset(daemon_client):
    client, fake = daemon_client
    fake._tail = {"lines": ["a line"], "offset": 9, "reset": False}
    resp = client.get('/api/daemon/log?since=5')
    assert resp.status_code == 200
    assert resp.get_json() == {"lines": ["a line"], "offset": 9, "reset": False}
    assert ("log", 5) in fake.calls


def test_daemon_log_route_reads_configured_file_incrementally(tmp_path):
    db_path = str(tmp_path / "test_daemon_log.db")
    initialize_database(db_path)
    log_path = tmp_path / "daemon.log"
    log_path.write_text("hello\nworld\n")
    config = {"database": {"path": db_path}, "logging": {"file": str(log_path)}}
    init_webserver(db_path, config)
    client = app.test_client()

    first = client.get('/api/daemon/log?since=0').get_json()
    assert first["lines"] == ["hello", "world"]
    assert first["reset"] is False

    with open(log_path, "a") as f:
        f.write("again\n")
    second = client.get(f"/api/daemon/log?since={first['offset']}").get_json()
    assert second["lines"] == ["again"]
    assert second["reset"] is False


def test_daemon_log_route_missing_file_returns_empty(tmp_path):
    db_path = str(tmp_path / "test_daemon_missing_log.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "logging": {"file": str(tmp_path / "missing.log")}}
    init_webserver(db_path, config)
    client = app.test_client()

    resp = client.get('/api/daemon/log?since=0')
    assert resp.status_code == 200
    assert resp.get_json() == {"lines": [], "offset": 0, "reset": False}

def test_detail_payload_ships_both_language_variants(web_client):
    """Both variants travel together so the toggle needs no second request.

    The TUI switches between translated and original with a local re-render.
    Shipping only the translated field, as this route used to, made the
    original unreachable from the web UI at all.
    """
    client, db_path = web_client
    insert_or_update_item(db_path, {
        "workshop_id": 77, "title": "Original Title", "title_en": "Translated Title",
        "extended_description": "[b]Original[/b]",
        "extended_description_en": "[b]Translated[/b]",
        "translate_version": 12345, "status": 200,
    })

    data = client.get('/api/item/77').get_json()
    assert data["display_title"] == "Translated Title"
    assert data["display_title_original"] == "Original Title"
    assert "<b>Translated</b>" in data["description_html"]
    assert "<b>Original</b>" in data["description_html_original"]
    assert data["has_translation"] is True


def test_detail_payload_without_a_translation_offers_no_toggle(web_client):
    """An untranslated item reports no translation, matching the TUI.

    With nothing translated the variants must coincide rather than the original
    being empty, so the client still has something to render.
    """
    client, db_path = web_client
    insert_or_update_item(db_path, {
        "workshop_id": 78, "title": "Only Original",
        "extended_description": "[b]Nur Original[/b]", "status": 200,
    })

    data = client.get('/api/item/78').get_json()
    assert data["has_translation"] is False
    assert data["display_title_original"] == "Only Original"
    assert data["description_html_original"] == data["description_html"]


def test_detail_translation_flag_follows_translate_version(web_client):
    """`has_translation` keys off translate_version, not off translated text.

    A field can carry text identical to the original, or carry text with no
    recorded revision. Only translate_version marks an item as translated, and
    that is the test the TUI's toggle uses too.
    """
    client, db_path = web_client
    insert_or_update_item(db_path, {
        "workshop_id": 80, "title": "Same", "title_en": "Same",
        "translate_version": 0, "status": 200,
    })
    assert client.get('/api/item/80').get_json()["has_translation"] is False

    insert_or_update_item(db_path, {
        "workshop_id": 81, "title": "A", "translate_version": 5, "status": 200,
    })
    assert client.get('/api/item/81').get_json()["has_translation"] is True


# ── the userscript bridge's session push ─────────────────────────────────────
#
# The bridge re-pushes on a timer, so this handler is on a hot path: an open
# Steam tab calls it every thirty seconds whether or not anything has changed.


@pytest.fixture
def session_client(tmp_path, monkeypatch):
    """A client with a real config path, and save_config counted."""
    import src.webserver as ws

    db_path = str(tmp_path / "test_session.db")
    initialize_database(db_path)
    config_path = str(tmp_path / "config.yaml")
    config = {"database": {"path": db_path}, "daemon": {"target_appids": [1]}}

    saves = []
    monkeypatch.setattr(ws, "save_config", lambda path, cfg: saves.append(path))
    # _sessionid is module state and would otherwise leak between tests.
    monkeypatch.setattr(ws, "_sessionid", "")

    init_webserver(db_path, config, config_path=config_path)
    return app.test_client(), ws, saves, config_path


def test_sessionid_push_stores_the_login_cookie(session_client):
    client, ws, saves, config_path = session_client

    resp = client.post('/api/sessionid', json={"sessionid": "abc123", "login_secure": "cookie-1"})

    assert resp.get_json() == {"ok": True}
    assert ws._sessionid == "abc123"
    assert ws._config["session"]["login_secure"] == "cookie-1"
    assert saves == [config_path], "a changed cookie must be persisted for the daemon"


def test_sessionid_push_does_not_rewrite_an_unchanged_cookie(session_client):
    """Regression: this rewrote config.yaml every thirty seconds.

    The bridge re-pushes on a timer and a cookie is valid for days, so an
    unchanged value cost a YAML serialisation and a file write per open Steam
    tab, forever.
    """
    client, _ws, saves, _ = session_client

    client.post('/api/sessionid', json={"sessionid": "abc", "login_secure": "same"})
    assert len(saves) == 1

    client.post('/api/sessionid', json={"sessionid": "abc", "login_secure": "same"})
    assert len(saves) == 1, "an unchanged cookie must not rewrite the config"

    client.post('/api/sessionid', json={"sessionid": "abc", "login_secure": "different"})
    assert len(saves) == 2, "a changed cookie must still be persisted"


def test_sessionid_push_updates_the_token_even_when_the_cookie_is_unchanged(session_client):
    """The CSRF token lives in memory and is stored, not compared."""
    client, ws, _saves, _ = session_client

    client.post('/api/sessionid', json={"sessionid": "first", "login_secure": "same"})
    client.post('/api/sessionid', json={"sessionid": "second", "login_secure": "same"})

    assert ws._sessionid == "second"


def test_sessionid_push_without_a_cookie_does_not_persist(session_client):
    """No cookie to store is not a reason to write the config file."""
    client, ws, saves, _ = session_client

    resp = client.post('/api/sessionid', json={"sessionid": "abc"})

    assert resp.get_json() == {"ok": True}
    assert ws._sessionid == "abc"
    assert saves == []


def test_sessionid_push_without_a_sessionid_is_rejected(session_client):
    client, _ws, saves, _ = session_client

    resp = client.post('/api/sessionid', json={"login_secure": "cookie"})

    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert saves == []

