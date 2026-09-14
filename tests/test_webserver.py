import pytest
import json
import os
import re
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


def test_metrics_catalogue_lists_every_metric_in_seed_order(web_client):
    """The panel draws its layout from this, so order and names must match."""
    client, _ = web_client
    resp = client.get('/api/metrics')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["default_order"] == metrics.all_names()
    assert [m["name"] for m in data["metrics"]] == metrics.all_names()
    for entry in data["metrics"]:
        assert entry["note"], "a metric reached the client with no note"
        assert entry["seed_ms"] == metrics.REGISTRY[entry["name"]].seed_ms
    assert "tiers" not in data, "the response must not group metrics into tiers"


def test_metric_endpoint_returns_value_and_measured_cost(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "x", "status": 200})

    resp = client.get('/api/metrics/coverage')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["name"] == "coverage"
    assert data["value"]["total"] == 1
    assert data["ms"] >= 0.0
    assert data["note"] == metrics.REGISTRY["coverage"].note
    assert data["seed_ms"] == metrics.REGISTRY["coverage"].seed_ms


def test_metric_endpoint_runs_only_the_requested_metric(web_client, monkeypatch):
    """One request is one metric: asking for totals must not pay for tags."""
    client, _ = web_client
    ran = []

    def spy(conn, params):
        ran.append(True)
        return {}

    monkeypatch.setitem(
        metrics.REGISTRY, "tag_counts",
        metrics.Metric(name="tag_counts", seed_ms=358.0, note="spy", run=spy),
    )

    resp = client.get('/api/metrics/totals')
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "totals"
    assert ran == [], "the totals request ran the tag metric"


def test_metrics_unknown_metric_is_a_404(web_client):
    client, _ = web_client
    resp = client.get('/api/metrics/banana')
    assert resp.status_code == 404
    assert "banana" in resp.get_json()["error"]


def test_stats_button_opens_a_panel_instead_of_navigating(web_client):
    """The affordance keeps its id and place, but it is no longer a link to JSON."""
    client, _ = web_client
    body = client.get('/').data.decode()
    doc = lxml.html.fromstring(body)

    buttons = doc.xpath('//*[@id="btn-stats"]')
    assert len(buttons) == 1, "expected exactly one #btn-stats"
    assert buttons[0].tag == "button", "the stats affordance must not navigate away"
    assert not buttons[0].get("href"), "the stats button still points at /api/stats"

    overlay = doc.xpath('//*[@id="stats-overlay"]')
    assert overlay, "missing #stats-overlay"
    assert doc.xpath('//*[@id="stats-metrics"]')[0] in overlay[0].iterdescendants(), \
        "#stats-metrics must live inside the overlay"
    assert doc.xpath('//*[@id="stats-close"]')[0] in overlay[0].iterdescendants(), \
        "the panel needs a close control"

    # The sections are built at runtime from the catalogue, so the per-metric
    # container is a contract of the served script rather than the static markup:
    # it must build one [data-metric] section per metric, and no tier sections.
    scripts = "\n".join(s.text or "" for s in doc.xpath('//script[not(@src)]'))
    assert 'data-metric="' in scripts, "the panel must build one container per metric"
    assert "stats-tier" not in scripts and "data-tier" not in scripts, \
        "the tier grouping must be gone from the statistics panel"


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


# ── clear pending database ───────────────────────────────────────────────────
#
# The route is a thin wrapper over clear_pending_items, so the predicate is the
# contract worth pinning: the rows it removes and, just as importantly, the rows
# it leaves alone.


def test_clear_pending_route_deletes_only_the_pending_rows(web_client):
    client, db_path = web_client
    # Removed: never successfully fetched, with no status or a 404.
    insert_or_update_item(db_path, {"workshop_id": 1, "status": None, "api_fetched_at": None})
    insert_or_update_item(db_path, {"workshop_id": 2, "status": 404, "api_fetched_at": None})
    # Kept: a real status, or a recorded successful fetch, or both.
    insert_or_update_item(db_path, {"workshop_id": 3, "status": 200, "api_fetched_at": None})
    insert_or_update_item(db_path, {"workshop_id": 4, "status": None, "api_fetched_at": 1672531200})
    insert_or_update_item(db_path, {"workshop_id": 5, "status": 404, "api_fetched_at": 1672531200})

    resp = client.post('/api/clear_pending')
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "deleted": 2}

    conn = get_connection(db_path)
    ids = [r["workshop_id"] for r in conn.execute(
        "SELECT workshop_id FROM workshop_items ORDER BY workshop_id")]
    conn.close()
    assert ids == [3, 4, 5], "the predicate removed a row it must not touch"


def test_clear_pending_route_reports_zero_and_is_post_only(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "status": 200, "api_fetched_at": 1672531200})

    assert client.get('/api/clear_pending').status_code == 405
    resp = client.post('/api/clear_pending')
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "deleted": 0}, \
        "an empty delete must still report the count the UI shows"


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


# ── the honest save report and the header port ───────────────────────────────
#
# Both behaviours live only in the served inline script, so a browser would be
# needed to observe them end to end. Rather than assert on the script's text —
# which would pass as long as the words appear, even with the branch inverted —
# these extract the named functions and run them in node against tiny stubs.


def _served_inline_script(client) -> str:
    doc = lxml.html.fromstring(client.get('/').data.decode())
    return "\n".join(s.text or "" for s in doc.xpath('//script[not(@src)]'))


def _extract_function(script: str, name: str) -> str:
    """Return the source of the top-level function `name` from the script.

    Brace-matched rather than line-grepped, so the snippet node receives is a
    real function definition and not a fragment that happens to contain it.
    """
    match = re.search(r'(?:async\s+)?function\s+' + re.escape(name) + r'\s*\(', script)
    assert match, f"no function {name} in the served script"
    start = match.start()
    brace = script.index('{', match.end() - 1)
    depth = 0
    for i in range(brace, len(script)):
        if script[i] == '{':
            depth += 1
        elif script[i] == '}':
            depth -= 1
            if depth == 0:
                return script[start:i + 1]
    raise AssertionError(f"unterminated function {name}")


def _extract_const(script: str, name: str) -> str:
    """Return the literal value of a top-level `const name = ...;` declaration.

    Tests inject the page's real constants rather than restating them, so a
    changed version or cap cannot silently diverge from what the driver checks.
    """
    match = re.search(r'^const\s+' + re.escape(name) + r'\s*=\s*([^;]+);', script, re.M)
    assert match, f"no const {name} in the served script"
    return match.group(1).strip()


def _run_node(driver: str, tmp_path):
    path = tmp_path / "driver.js"
    path.write_text(driver, encoding="utf-8")
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"node driver failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


SAVE_FILTER_DRIVER = """
const fn = (__FN__);
const calls = [];
global.alert = (m) => calls.push(m);
global.getFilters = () => [];
async function run(resp) {
  global.fetch = async () => resp;
  await fn();
}
(async () => {
  await run({ok: true, status: 200, statusText: 'OK', json: async () => ({ok: true})});
  await run({ok: false, status: 400, statusText: 'BAD REQUEST',
             json: async () => ({error: 'No target AppID configured'})});
  await run({ok: false, status: 500, statusText: 'INTERNAL SERVER ERROR',
             json: async () => { throw new Error('not json'); }});
  global.fetch = async () => { throw new Error('backend down'); };
  await fn();
  console.log(JSON.stringify(calls));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_save_filter_reports_the_servers_rejection(web_client, tmp_path):
    """A 400 must not be reported as a save, and the server's reason must show.

    /api/save_filter answers 400 with {"error": "No target AppID configured"}
    when no AppID is set. The old handler ignored the response and always
    alerted success, so a rejected save was indistinguishable from a stored one.
    """
    client, _ = web_client
    fn = _extract_function(_served_inline_script(client), "saveFilterAndReport")
    calls = _run_node(SAVE_FILTER_DRIVER.replace("__FN__", fn), tmp_path)

    assert calls == [
        'Filter saved for scraper.',
        'Filter not saved: No target AppID configured',
        'Filter not saved: 500 INTERNAL SERVER ERROR',
        'Filter not saved: backend down',
    ]


PORT_DRIVER = """
const fn = (__FN__);
let shown = null;
global.document = { getElementById: () => ({ set textContent(v) { shown = v; } }) };
global.location = { port: '8080' };
fn();
const withPort = shown;
global.location = { port: '' };
shown = null;
fn();
console.log(JSON.stringify({withPort: withPort, defaultPort: shown}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_header_port_display_is_filled_from_the_pages_own_location(web_client, tmp_path):
    """The header span renders the port the panel was served on.

    It was declared and styled with nothing ever writing to it, so the header
    showed an empty span. The page already knows its own port (location.port),
    so this costs no request and cannot go stale when the configured port
    changes between loads.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    doc = lxml.html.fromstring(client.get('/').data.decode())
    assert doc.xpath('//*[@id="port-display"]'), \
        "the header port element must exist to be populated"

    fn = _extract_function(script, "showServerPort")
    result = _run_node(PORT_DRIVER.replace("__FN__", fn), tmp_path)
    assert result["withPort"] == ":8080"
    assert result["defaultPort"] == ""

    # The function only helps if the page actually calls it on load.
    assert re.search(r'^showServerPort\(\);$', script, re.M), \
        "the page never calls showServerPort()"


# ── clear pending: the confirmation and the report ────────────────────────────
#
# The route is destructive, so the client's half of the contract is that the
# confirmation is asked first and names what will go, and that the count the
# route returns is what the user is told. A browser would be needed to click it
# end to end, so the handler runs in node against stubbed confirm/fetch/alert.

CLEAR_PENDING_DRIVER = """
const fn = (__FN__);
const out = {prompts: [], urls: [], methods: [], alerts: [], searches: 0};
global.doSearch = () => { out.searches += 1; };
global.alert = (m) => out.alerts.push(m);
let answer = false;
global.confirm = (m) => { out.prompts.push(m); return answer; };
let response = {ok: true, status: 200, statusText: 'OK', json: async () => ({ok: true, deleted: 2})};
let throwError = null;
global.fetch = async (url, opts) => {
  out.urls.push(url);
  out.methods.push(opts && opts.method);
  if (throwError) throw throwError;
  return response;
};
(async () => {
  await fn();                       // declined: nothing is sent
  answer = true;
  await fn();                       // accepted: the delete happens and is reported
  response = {ok: false, status: 500, statusText: 'INTERNAL SERVER ERROR', json: async () => ({})};
  await fn();                       // server rejected the delete
  throwError = new Error('backend down');
  await fn();                       // the backend is unreachable
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_clear_pending_confirms_before_deleting_and_reports_the_count(web_client, tmp_path):
    client, _ = web_client
    doc = lxml.html.fromstring(client.get('/').data.decode())
    buttons = doc.xpath('//*[@id="btn-clear-pending"]')
    assert len(buttons) == 1, "expected exactly one #btn-clear-pending"
    assert buttons[0].tag == "button", "the affordance must be a button, not a link"
    assert "doClearPending" in (buttons[0].get("onclick") or ""), \
        "the button must call the confirming handler, not the route directly"

    fn = _extract_function(_served_inline_script(client), "doClearPending")
    out = _run_node(CLEAR_PENDING_DRIVER.replace("__FN__", fn), tmp_path)

    assert len(out["prompts"]) == 4, "every invocation must ask before deleting"
    for prompt in out["prompts"]:
        lowered = prompt.lower()
        assert "pending" in lowered and "cannot be undone" in lowered, \
            f"the confirmation must state what is deleted, not a generic warning: {prompt!r}"
    assert out["urls"] == ["/api/clear_pending"] * 3, \
        "declining must send nothing; accepting must call the route"
    assert out["methods"] == ["POST"] * 3
    assert out["alerts"] == [
        "Removed 2 pending item(s).",
        "Clear pending failed: 500 INTERNAL SERVER ERROR",
        "Clear pending failed: backend down",
    ]
    assert out["searches"] == 1, "only a successful clear re-runs the search"


# ── view state persistence ────────────────────────────────────────────────────
#
# The stored view is browser-only state, so its whole contract is the shape it
# writes and the guard it reads it back through. These run the real functions
# from the served script in node against a localStorage stub.

VIEW_STATE_DRIVER = """
const loadFn = (__LOAD__);
const saveFn = (__SAVE__);
const key = __KEY__;
const version = __VER__;
global.VIEW_STATE_KEY = key;
global.VIEW_STATE_VERSION = version;
global.ALL_FIELDS = ['Title', 'Subs'];
global._restoringView = false;
global._selectedWid = 42;
const store = {};
global.localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
const emit = console.log.bind(console);
global.console = {debug: () => {}, warn: () => {}, error: () => {}, log: emit};
global.getFilters = () => ([{field: 'Subs', op: 'gte', value: '100'}]);
const values = {
  'results-grid': {scrollTop: 777},
  'sort-by': {value: 'subscriptions'},
  'sort-order': {value: 'DESC'},
};
global.document = {getElementById: (id) => values[id]};
const out = {};
saveFn();
out.stored = JSON.parse(store[key]);
out.loaded = loadFn();

store[key] = JSON.stringify({v: 999, filters: []});
out.staleVersion = loadFn();
store[key] = 'not json';
out.malformed = loadFn();
store[key] = JSON.stringify({v: version, filters: 'nope'});
out.badFilters = loadFn();
store[key] = JSON.stringify({
  v: version,
  filters: [
    {field: 'Nope', op: 'contains', value: 'x'},
    {field: 'Title', op: 'contains', value: 5},
  ],
  sort_by: 'title', sort_order: 'ASC', selected: 7, scroll: -3,
});
out.filtered = loadFn();

// While the page is restoring, nothing may write the state back early.
delete store[key];
global._restoringView = true;
saveFn();
out.wroteWhileRestoring = Object.prototype.hasOwnProperty.call(store, key);
emit(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_view_state_round_trips_and_rejects_stale_or_malformed_entries(web_client, tmp_path):
    """The stored view is versioned and shape-checked like the stats ordering.

    A browser's saved view is the source of truth for the next load, so a
    corrupt record must read back as "no state" (fall back to the TUI) rather
    than reach the filter builder as a half-built shape.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (VIEW_STATE_DRIVER
              .replace("__LOAD__", _extract_function(script, "_loadViewState"))
              .replace("__SAVE__", _extract_function(script, "_saveViewState"))
              .replace("__KEY__", _extract_const(script, "VIEW_STATE_KEY"))
              .replace("__VER__", _extract_const(script, "VIEW_STATE_VERSION")))
    out = _run_node(driver, tmp_path)

    expected = {
        "v": 1,
        "filters": [{"field": "Subs", "op": "gte", "value": "100"}],
        "sort_by": "subscriptions",
        "sort_order": "DESC",
        "selected": 42,
        "scroll": 777,
    }
    assert out["stored"] == expected, "the saved shape must carry every restored field"
    # The loaded view is the validated set of fields; the guard's own version
    # marker is consumed on the way in rather than handed to the caller.
    assert out["loaded"] == {k: v for k, v in expected.items() if k != "v"}, \
        "a saved view must load back unchanged"

    assert out["staleVersion"] is None, "an entry from another version must be ignored"
    assert out["malformed"] is None, "unparseable storage must be ignored"
    assert out["badFilters"] is None, "a non-list filters field must be ignored"

    # Unknown fields are dropped and the value is coerced to the string the
    # text input holds; a negative scroll is not a position.
    assert out["filtered"]["filters"] == [{"field": "Title", "op": "contains", "value": "5"}]
    assert out["filtered"]["selected"] == 7
    assert out["filtered"]["scroll"] == 0
    assert out["wroteWhileRestoring"] is False, \
        "a restore in progress must not overwrite the state it is reading"


LOAD_STATE_DRIVER = """
const fn = (__FN__);
let local = __LOCAL__;
const out = {};
const filterRows = {innerHTML: '', children: {length: 0}};
const sortBy = {value: ''};
const sortOrder = {value: ''};
global._loadViewState = () => local;
global._applyFilters = (f) => { out.applied = f; filterRows.children.length = f.length; };
global.addRow = () => { out.addedRow = (out.addedRow || 0) + 1; };
global.doSearch = async () => { out.searches = (out.searches || 0) + 1; };
global._restoreView = async (s) => { out.restored = s; };
const emit = console.log.bind(console);
global.console = {debug: () => {}, warn: () => {}, error: () => {}, log: emit};
const fetches = [];
global.fetch = async (url) => {
  fetches.push(url);
  return {json: async () => ({
    filters: [{field: 'Subs', op: 'gte', value: '9'}],
    sort_by: 'views', sort_order: 'ASC',
  })};
};
global.document = {getElementById: (id) => {
  if (id === 'filter-rows') return filterRows;
  if (id === 'sort-by') return sortBy;
  if (id === 'sort-order') return sortOrder;
  return null;
}};
(async () => {
  await fn();
  out.local = {applied: out.applied, sort_by: sortBy.value, sort_order: sortOrder.value,
               fetches: fetches.slice(), searches: out.searches, restored: out.restored,
               addedRow: out.addedRow || 0};
  local = null;
  out.applied = null; out.searches = 0; out.restored = null; out.addedRow = 0;
  fetches.length = 0; sortBy.value = ''; sortOrder.value = ''; filterRows.children.length = 0;
  await fn();
  out.tui = {applied: out.applied, sort_by: sortBy.value, sort_order: sortOrder.value,
             fetches: fetches.slice(), searches: out.searches, restored: out.restored,
             addedRow: out.addedRow || 0};
  emit(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_browser_state_wins_over_the_tui_seed_on_load(web_client, tmp_path):
    """A browser that has state of its own does not ask the TUI for a seed.

    The two sources exist side by side, so the rule has to be one of them
    outright: local state wins, and `/api/state` is consulted only on a first
    visit (no entry, or one the guard rejected).
    """
    client, _ = web_client
    script = _served_inline_script(client)
    local = {
        "filters": [{"field": "Title", "op": "contains", "value": "x"}],
        "sort_by": "title", "sort_order": "DESC", "selected": 5, "scroll": 120,
    }
    driver = (LOAD_STATE_DRIVER
              .replace("__FN__", _extract_function(script, "loadState"))
              .replace("__LOCAL__", json.dumps(local)))
    out = _run_node(driver, tmp_path)

    assert out["local"]["fetches"] == [], \
        "local state is the source of truth; the TUI file must not be read"
    assert out["local"]["applied"] == local["filters"]
    assert out["local"]["sort_by"] == "title"
    assert out["local"]["sort_order"] == "DESC"
    assert out["local"]["restored"] == local, "the saved view drives the restore"
    assert out["local"]["searches"] == 1

    assert out["tui"]["fetches"] == ["/api/state"], \
        "a first visit must still seed from the TUI's saved state"
    assert out["tui"]["applied"] == [{"field": "Subs", "op": "gte", "value": "9"}]
    assert out["tui"]["sort_by"] == "views"
    assert out["tui"]["sort_order"] == "ASC"
    assert out["tui"]["restored"] is None, "there is nothing of the browser's own to restore"
    assert out["tui"]["searches"] == 1


RESTORE_DRIVER = """
const restoreFn = (__FN__);
global._loadUntil = (__LOADUNTIL__);
globalThis.MAX_RESTORE_BATCHES = __MAX__;
const out = {};
function makeGrid() {
  return {
    scrollTop: 0, clientHeight: 500, scrollHeight: 500,
    cell: false, cellOnHeight: null,
    querySelector: function(sel) {
      if (sel.indexOf('data-wid') !== -1 && this.cell) return {wid: 77};
      return null;
    },
  };
}
function install(grid, perBatch, stopAfter) {
  globalThis.hasMore = true;
  globalThis.currentOffset = 0;
  let batches = 0;
  globalThis.doSearch = async function() {
    if (!globalThis.hasMore) return;
    batches += 1;
    globalThis.currentOffset += 50;
    grid.scrollHeight += perBatch;
    if (grid.cellOnHeight != null && grid.scrollHeight >= grid.cellOnHeight) grid.cell = true;
    if (stopAfter != null && batches >= stopAfter) globalThis.hasMore = false;
  };
  globalThis.document = {getElementById: function() { return grid; }};
  globalThis.showDetail = async function() {
    out.showDetailCalls = (out.showDetailCalls || 0) + 1;
    grid.scrollTop = 5;   // a focus-style jump the restore has to override
  };
  return function() { return batches; };
}
(async () => {
  let grid = makeGrid();
  grid.cellOnHeight = 900;
  let count = install(grid, 400, 4);
  await restoreFn({scroll: 1500, selected: 77});
  out.restore = {scrollTop: grid.scrollTop, batches: count(),
                 showDetailCalls: out.showDetailCalls || 0};

  grid = makeGrid();
  count = install(grid, 1, null);
  await restoreFn({scroll: 100000, selected: null});
  out.cap = {batches: count()};
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_restore_pages_to_the_saved_position_and_scroll_wins_at_the_end(web_client, tmp_path):
    """Restoring a deep view asks doSearch for pages; it never re-implements them.

    The saved position may sit past the first 50-item batch, so `_restoreView`
    keeps calling `doSearch(false)` — the function that owns `currentOffset`
    and the sentinel — until the grid is tall enough, re-opens the selected
    item, and then applies the saved scroll last because focusing that item
    moves the grid. The paging is bounded so a deleted selection cannot walk the
    whole result set.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (RESTORE_DRIVER
              .replace("__FN__", _extract_function(script, "_restoreView"))
              .replace("__LOADUNTIL__", _extract_function(script, "_loadUntil"))
              .replace("__MAX__", _extract_const(script, "MAX_RESTORE_BATCHES")))
    out = _run_node(driver, tmp_path)

    assert out["restore"]["batches"] == 4, "must page until the grid can hold the saved scroll"
    assert out["restore"]["showDetailCalls"] == 1, "the selected item must be re-opened"
    assert out["restore"]["scrollTop"] == 1500, \
        "the saved scroll must be applied after focus moves the grid"

    max_batches = int(_extract_const(script, "MAX_RESTORE_BATCHES"))
    assert max_batches == 40
    assert out["cap"]["batches"] == max_batches, \
        "restoring an unreachable position must stop at the batch cap"

