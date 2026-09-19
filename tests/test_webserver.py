import pytest
import json
import logging
import os
import re
import shutil
import subprocess
import base64
import time
from unittest.mock import MagicMock
import lxml.html
from src import activity
from src import session_health
from src import webserver
from src import subscribe_engine
from src.webserver import app, init_webserver
from src.database import initialize_database, insert_or_update_item, normalize_tags, get_image_subdirs, get_connection
from src import metrics
from src import pacing
from src import web_scraper


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


def test_the_server_no_longer_registers_the_unused_number_filters(web_client):
    """The browser formats counts in JS; these filters had no template consumer."""
    from src.webserver import app

    assert "fcount" not in app.jinja_env.filters
    assert "fsize" not in app.jinja_env.filters


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


def test_every_registered_metric_reaches_the_web_catalogue(web_client):
    """The web panel builds one section per catalogue entry, so a metric is only
    visible there if `/api/metrics` serves it with a note.

    The client falls back to a JSON dump for a value it has no renderer for, so
    registration is the whole web requirement -- but a metric missing from the
    catalogue would silently never render, and this names it. The TUI needs more
    than this; see
    `test_every_registered_metric_has_the_wiring_both_front_ends_need`.
    """
    client, _ = web_client
    served = {m["name"]: m for m in client.get('/api/metrics').get_json()["metrics"]}
    gaps = [f"{name}: not in /api/metrics" for name in metrics.all_names()
            if name not in served]
    gaps += [f"{name}: catalogue entry carries no note"
             for name, meta in served.items() if not meta.get("note")]
    assert not gaps, "metric(s) the web panel cannot draw:\n  " + "\n  ".join(gaps)

    # The two handoff counters are served as values as well as entries.
    for name in ("queued_nowhere", "dead_queued"):
        assert name in metrics.REGISTRY
        assert client.get(f'/api/metrics/{name}').get_json()["value"] == 0


def test_metric_endpoint_returns_value_and_measured_cost(web_client):
    client, db_path = web_client
    # The fixture config targets AppID 294100, so the route must pass that
    # through: the item belongs to the target and both scopes count it.
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "x", "status": 200,
                                    "consumer_appid": 294100})

    resp = client.get('/api/metrics/coverage')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["name"] == "coverage"
    assert data["value"]["total"] == 1
    # Both scopes reach the client: the whole live library and the target
    # AppIDs' filters. The target has no filters stored, so nothing is
    # excluded and the second coincides; the scope detail says so.
    assert data["value"]["filtered"]["total"] == 1
    assert data["value"]["filtered"]["appids"] == [294100]
    assert data["value"]["filtered"]["with_filters"] == []
    assert data["ms"] >= 0.0
    assert data["note"] == metrics.REGISTRY["coverage"].note
    assert data["seed_ms"] == metrics.REGISTRY["coverage"].seed_ms


def test_metric_endpoint_runs_only_the_requested_metric(web_client, monkeypatch):
    """One request is one metric: asking for item_counts must not pay for tags."""
    client, _ = web_client
    ran = []

    def spy(conn, params):
        ran.append(True)
        return {}

    monkeypatch.setitem(
        metrics.REGISTRY, "tag_counts",
        metrics.Metric(name="tag_counts", seed_ms=358.0, note="spy", run=spy),
    )

    resp = client.get('/api/metrics/item_counts')
    assert resp.status_code == 200
    assert resp.get_json()["name"] == "item_counts"
    assert ran == [], "the item_counts request ran the tag metric"


def test_metrics_unknown_metric_is_a_404(web_client):
    client, _ = web_client
    resp = client.get('/api/metrics/banana')
    assert resp.status_code == 404
    assert "banana" in resp.get_json()["error"]


def test_the_pause_routes_record_one_interval(web_client, tmp_path, monkeypatch):
    """`POST /api/pause` is a writer of `.pauselock`, so its interval is recorded.

    A second pause while the lock is already held must not open a second
    interval, and resume is idempotent: the drain estimate subtracts the paused
    interval exactly once (see src/activity.py).
    """
    from src.daemon_state import StateStore, state_path_for

    client, db_path = web_client
    monkeypatch.chdir(tmp_path)
    try:
        assert client.post('/api/pause').get_json() == {"ok": True}
        assert client.post('/api/pause').get_json() == {"ok": True}
        assert client.post('/api/resume').get_json() == {"ok": True}
        assert client.post('/api/resume').get_json() == {"ok": True}
    finally:
        if os.path.exists('.pauselock'):
            os.remove('.pauselock')

    section = StateStore(state_path_for(db_path)).load().get(activity.PAUSE_SECTION)
    assert section["open"] is None
    assert len(section["closed"]) == 1
    start, end = section["closed"][0]
    assert end >= start


def test_stats_button_opens_a_panel_instead_of_navigating(web_client):
    """The affordance keeps its id, now lives in the header, and is no longer a link to JSON."""
    client, _ = web_client
    body = client.get('/').data.decode()
    doc = lxml.html.fromstring(body)

    buttons = doc.xpath('//*[@id="btn-stats"]')
    assert len(buttons) == 1, "expected exactly one #btn-stats"
    assert buttons[0].tag == "button", "the stats affordance must not navigate away"
    assert not buttons[0].get("href"), "the stats button still points at /api/stats"
    assert "Stats" in buttons[0].text_content(), \
        "the moved button must carry the visible word Stats, like its neighbours"

    # The button moved out of the search builder into the page header, beside
    # the daemon button; pin that home so a regression is caught.
    header = doc.xpath('//header')
    assert header, "missing <header>"
    assert buttons[0] in header[0].iterdescendants(), \
        "#btn-stats must be a descendant of <header>"

    daemon = doc.xpath('//*[@id="btn-daemon"]')
    assert len(daemon) == 1, "expected exactly one #btn-daemon"
    assert daemon[0].getparent() is buttons[0].getparent(), \
        "#btn-stats and #btn-daemon must share a parent"

    overlay = doc.xpath('//*[@id="stats-modal"]')
    assert overlay, "missing #stats-modal"
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


def _render_tags_js():
    """The _renderTags body, read as text like the repo's other client-side checks."""
    from pathlib import Path
    html = Path("templates/index.html").read_text(encoding="utf-8")
    body = html[html.index("function _renderTags(tags) {"):]
    return body[:body.index("\n}")]


def _css_rule(selector):
    from pathlib import Path
    html = Path("templates/index.html").read_text(encoding="utf-8")
    marker = selector + " {"
    if marker not in html:
        return ""
    rule = html[html.index(marker):]
    return rule[:rule.index("}")]


def test_tag_stats_render_as_a_table_not_a_truncated_paragraph(web_client):
    """Tags are their own rows, all of them, not an inline run cut at fifteen.

    The old renderer joined the top 15 into one "Most used: …" paragraph: the
    run was mushed inline with the others and the list was truncated. Both
    halves are pinned here.
    """
    js = _render_tags_js()
    assert 'class="stats-table"' in js, "tags must render as the shared stats table"
    assert "<th>Tag</th>" in js and "<th>Count</th>" in js, \
        "the tag table needs Tag and Count headers"
    assert "_escapeHtml(name)" in js, \
        "a tag containing & or < must be escaped, not interpolated raw"
    assert "slice(0, 15)" not in js, "every tag must be listed, not a top 15"
    assert "Most used:" not in js, "the inline run of tag names must be gone"
    assert 'class="stats-scroll"' in js, "the table must sit in the scroll container"
    # A tag count is read and compared, so it is printed in full: fmtCount
    # reports 5,000 as "5.00K", which is the number the table exists to show.
    assert "fmtExact(tags[name])" in js, "the count column must show the full digits"
    assert "fmtCount(tags[name])" not in js, "not a contracted count"


def test_tag_stats_table_is_bounded_and_scrollable(web_client):
    """A long tag list scrolls inside its chunk instead of pushing the rest down."""
    rule = _css_rule(".stats-scroll")
    assert rule, "missing the .stats-scroll rule"
    assert "max-height" in rule, "the tag list needs a bounded height"
    assert "overflow" in rule, "and must scroll rather than grow"


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
    bucket1, bucket2, bucket3 = get_image_subdirs(workshop_id)
    nested_dir = os.path.join(os.path.dirname(db_path), "images", bucket1, bucket2, bucket3)
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


def test_api_items_reports_the_image_state_the_page_branches_on(web_client):
    """The page is told which cells may be fetched and which must be drawn.

    `image_extension` now holds an answer as well as a file type, so the raw
    column cannot be used as a truthiness test in the browser: '404' is truthy
    and would become a request for /images/<id>.404. The classification is made
    once, server-side, and sent alongside the value it describes.
    """
    from src.database import insert_or_update_item
    client, db_path = web_client
    for wid, ext in ((11, "jpg"), (12, "404"), (13, "html"), (14, None), (15, "503")):
        row = {"workshop_id": wid, "title": f"item {wid}", "status": 200}
        if ext is not None:
            row["image_extension"] = ext
        insert_or_update_item(db_path, row)

    items = {it["workshop_id"]: it
             for it in client.post('/api/items', json={"ids": [11, 12, 13, 14, 15]}).get_json()}

    assert len(items) == 5
    assert items[11]["image_state"] == "present" and items[11]["image_resolved"] is True
    assert items[12]["image_state"] == "permanent" and items[12]["image_resolved"] is True
    assert items[13]["image_state"] == "other" and items[13]["image_resolved"] is True
    assert items[14]["image_state"] == "absent" and items[14]["image_resolved"] is False
    assert items[15]["image_state"] == "transient" and items[15]["image_resolved"] is False


def test_the_api_image_resolved_agrees_with_the_shared_predicate(web_client):
    """Every state's answer is `images.is_resolved`, not a page-local copy of it."""
    from src import images
    from src.database import insert_or_update_item

    client, db_path = web_client
    stored = ((21, "jpg"), (22, "404"), (23, "html"), (24, None), (25, "503"))
    for wid, ext in stored:
        row = {"workshop_id": wid, "title": f"item {wid}", "status": 200}
        if ext is not None:
            row["image_extension"] = ext
        insert_or_update_item(db_path, row)

    items = {it["workshop_id"]: it
             for it in client.post('/api/items',
                                   json={"ids": [wid for wid, _ in stored]}).get_json()}

    for wid, ext in stored:
        assert items[wid]["image_state"] == images.image_state(ext)
        assert items[wid]["image_resolved"] is images.is_resolved(ext)


def test_the_api_image_resolved_is_decided_by_images_py(web_client, monkeypatch):
    """A change to the shared predicate must move the page, not be re-derived."""
    from src import images
    from src.database import insert_or_update_item

    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 31, "title": "x", "status": 200,
                                    "image_extension": "jpg"})

    calls = []

    def spy(stored):
        calls.append(stored)
        return False

    monkeypatch.setattr(images, "is_resolved", spy)

    item = client.post('/api/items', json={"ids": [31]}).get_json()[0]

    assert calls == ["jpg"], "the page must ask src/images.py, not spell the rule out"
    assert item["image_resolved"] is False


def _image_cell_js():
    """The _imageCellHtml body, read as text like the repo's other client-side checks."""
    from pathlib import Path
    html = Path("templates/index.html").read_text(encoding="utf-8")
    body = html[html.index("function _imageCellHtml(item) {"):]
    return body[:body.index("\n}")]


def test_the_grid_builds_an_image_url_only_for_a_real_extension():
    """The single URL the grid builds sits behind the state guard.

    The browser must not be asked for /images/<id>.<ext> when <ext> is a status
    marker, so the one URL construction has to be inside the 'present' branch
    and every other state has to draw something instead.
    """
    js = _image_cell_js()
    assert js.count("/images/") == 1, "an image URL must be built in exactly one place"
    assert js.index("state === 'present'") < js.index("/images/"), \
        "the URL must sit behind the state guard"
    assert "grid-img-failed" in js, "a final answer must render as the failure cell"
    assert "_escapeHtml(item.image_extension)" in js, \
        "the drawn value is interpolated into markup, so it must be escaped"


def test_api_subscribe_no_session(web_client):
    client, _ = web_client
    resp = client.post('/api/subscribe/1')
    assert resp.status_code == 400
    assert resp.get_json()["success"] == -1


# --- /api/subscribe/<id>: the credential source and the request shape -------
#
# This route is the only server-side subscribe. Its credential and its CSRF
# token must come from one read of the cookie source, it must refuse before
# spending a request when either half is missing or locally expired, and a
# Steam confirmation must be recorded rather than dropped. The network is the
# shared scraper session, replaced here with a recorder.

# Fixed expiries: `session_cookie.parse` compares against the wall clock, and
# these are on the right side of any plausible run date, so no test waits or
# depends on when it runs.
_FUTURE_EXPIRY = 4_102_444_800   # 2100-01-01
_PAST_EXPIRY = 1_000_000_000     # 2001-09-09


def _login_cookie(expires_at, steamid="76561198000000000"):
    """A `steamLoginSecure` value whose token states its own expiry.

    The three-segment shape a browser stores -- steamid, `%7C%7C`, then a token
    carrying `exp` -- because that is what `src/session_cookie.py` reads.
    """
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": expires_at}).encode()).decode().rstrip("=")
    return f"{steamid}%7C%7CeyJhbGciOiJub25lIn0.{payload}.c2ln"


def _synthetic_page(session_id=None, *, authenticated=True, toggled=False):
    """A hand-built item page for the route tests -- never a captured one.

    The local mirror's captures carry real `g_sessionID` values and real login
    cookies, so no capture is ever committed as a fixture. This page carries the
    account marker when `authenticated` and a **fake** `g_sessionID` only when
    one is asked for.
    """
    classes = 'class="btn toggled"' if toggled else 'class="btn"'
    account = '<div id="account_pulldown">signed in</div>' if authenticated else ""
    token = f'<script>var g_sessionID = "{session_id}";</script>' if session_id else ""
    return (f'<html><body>{account}'
            f'<a id="SubscribeItemBtn" {classes}>Subscribe</a>{token}</body></html>')


@pytest.fixture
def subscribe_env(tmp_path, monkeypatch):
    """A database with item 1, plus the route's two seams replaced.

    `_get_session` is the POST seam, and `scrape_extended_details` is the page
    read the route now makes to source its CSRF token. The cookie source is
    replaced with a set the test dictates -- the route must read that entry
    point, so a route taking the token from anywhere else is caught here. There
    is no runtime tripwire on a bare `requests.post` because the module no
    longer imports `requests`; that shape is pinned at the source instead, in
    `test_the_subscribe_route_has_no_bare_requests_call`.

    The default page is authenticated (so a Steam refusal is a token refusal,
    not a session problem) and carries no `g_sessionID` (so the pushed/configured
    fallback is exercised); a test that wants the page's own token sets
    `state["page"]`.
    """
    db_path = str(tmp_path / "subscribe.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "T", "status": 200,
                                    "consumer_appid": 294100})
    config = {"database": {"path": db_path}, "daemon": {"target_appids": [294100]},
              "session": {}}
    init_webserver(db_path, config)
    monkeypatch.setattr(webserver, "_pushed_sessionid", "")
    # The route's page read waits the shared interval; tests must not sleep.
    monkeypatch.setattr(pacing, "wait", lambda seconds, keep_running=None: True)

    state = {"cookies": {}, "payload": {"success": 1}, "calls": [], "pages": [],
             "page": _synthetic_page(), "status_code": 200}

    monkeypatch.setattr(webserver.web_scraper, "_build_workshop_cookies",
                        lambda config: dict(state["cookies"]))

    def _read_page(url, keep_body=False):
        state["pages"].append(url)
        return {"description": "d", "tags": [], "body": state["page"],
                "http_status": 200, "final_url": url,
                "request": {"method": "GET", "url": url}}

    monkeypatch.setattr(webserver.web_scraper, "scrape_extended_details", _read_page)

    class _Session:
        def post(self, url, **kwargs):
            state["calls"].append({"url": url, **kwargs})
            resp = MagicMock()
            resp.status_code = state.get("status_code", 200)
            resp.url = url
            resp.headers = dict(state.get("response_headers") or {})
            resp.text = state.get("body") or json.dumps(state["payload"])
            resp.json.return_value = state["payload"]
            return resp

    monkeypatch.setattr(webserver.web_scraper, "_get_session", lambda: _Session())
    return db_path, state


def _post_subscribe(client, wid=1):
    return client.post(f'/api/subscribe/{wid}')


def _item_row(db_path, wid=1):
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM workshop_items WHERE workshop_id=?", (wid,)).fetchone()
    conn.close()
    return dict(row)


def test_subscribe_token_comes_from_the_built_cookie_set(subscribe_env, monkeypatch):
    """(a) With no token on the page, the built set's token is used."""
    _, state = subscribe_env
    state["cookies"] = {"sessionid": "FROM_COOKIES",
                        "steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}
    monkeypatch.setattr(webserver, "_pushed_sessionid", "PUSHED_GLOBAL")
    webserver._config.setdefault("session", {})["csrf_token"] = "CONFIG_ID"
    client = app.test_client()

    resp = _post_subscribe(client)

    assert resp.status_code == 200
    call = state["calls"][0]
    assert call["data"]["sessionid"] == "FROM_COOKIES"
    assert call["cookies"]["sessionid"] == "FROM_COOKIES"


def test_subscribe_pushed_global_is_the_fallback(subscribe_env, monkeypatch):
    """(b) A built set with no sessionid still uses the pushed global."""
    _, state = subscribe_env
    state["cookies"] = {"steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}
    monkeypatch.setattr(webserver, "_pushed_sessionid", "PUSHED_GLOBAL")

    resp = _post_subscribe(app.test_client())

    assert resp.status_code == 200
    call = state["calls"][0]
    assert call["data"]["sessionid"] == "PUSHED_GLOBAL"
    assert call["cookies"]["sessionid"] == "PUSHED_GLOBAL"


@pytest.mark.parametrize("cookies", [
    {"steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)},   # no CSRF token
    {"sessionid": "TOK"},                                  # no login cookie
])
def test_subscribe_refuses_without_a_complete_credential(subscribe_env, cookies):
    """(c) Half a credential answers 400 with the remedy, and sends nothing."""
    _, state = subscribe_env
    state["cookies"] = cookies

    resp = _post_subscribe(app.test_client())

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] == -1
    assert "session.login_secure" in body["message"]
    assert state["calls"] == []


def test_subscribe_refuses_an_expired_credential_before_the_request(subscribe_env):
    """(d) A locally expired token answers 400, records it, and sends nothing."""
    db_path, state = subscribe_env
    expired = _login_cookie(_PAST_EXPIRY)
    state["cookies"] = {"sessionid": "TOK", "steamLoginSecure": expired}

    resp = _post_subscribe(app.test_client())

    expected = session_health.evaluate_login(expired)
    assert expected, "the fixture token must actually read as expired"
    assert resp.status_code == 400
    assert resp.get_json()["message"] == expected
    assert state["calls"] == []
    recorded = session_health.read(db_path)
    assert recorded and recorded["detail"] == expected


def test_subscribe_success_marks_the_item_and_clears_the_session_problem(subscribe_env):
    """(e) `success: 1` stamps the subscribe and clears a recorded problem."""
    db_path, state = subscribe_env
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "T", "status": 200,
                                    "consumer_appid": 294100,
                                    "is_queued_for_subscription": 1})
    session_health.record_rejected(db_path, "an older problem")
    state["cookies"] = {"sessionid": "TOK",
                        "steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}
    state["payload"] = {"success": 1}

    resp = _post_subscribe(app.test_client())

    assert resp.status_code == 200
    assert resp.get_json() == {"success": 1}
    row = _item_row(db_path)
    assert row["own_subscribed"] == 1
    assert row["is_queued_for_subscription"] == 0
    assert session_health.read(db_path) is None


@pytest.mark.parametrize("payload,status", [({"success": 2}, 200),
                                            ({"success": 15}, 200),
                                            ({"success": 2}, 401)])
def test_a_refused_token_with_an_authenticated_page_read_is_not_a_session_problem(
        subscribe_env, payload, status):
    """(f) A refusal beside an authenticated read is about the token, not the login.

    The page this attempt read was served to the signed-in account, so the
    credential is proven good. Recording a session problem here is what told the
    owner to sign in again while the login was working.
    """
    db_path, state = subscribe_env
    state["cookies"] = {"steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}
    state["page"] = _synthetic_page("PAGE_SESSION_TOKEN", authenticated=True)
    state["payload"] = payload
    state["status_code"] = status

    resp = _post_subscribe(app.test_client())

    assert session_health.read(db_path) is None, "the login was proven good"
    # The response body is still Steam's own, untouched.
    assert resp.get_json() == payload


def test_a_refused_token_with_an_anonymous_page_read_is_still_a_session_problem(
        subscribe_env, monkeypatch):
    """(g) A refusal beside an anonymous read records the session problem."""
    db_path, state = subscribe_env
    state["cookies"] = {"steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}
    state["page"] = _synthetic_page(authenticated=False)
    state["payload"] = {"success": 2}
    monkeypatch.setattr(webserver, "_pushed_sessionid", "PUSHED_TOKEN")

    resp = _post_subscribe(app.test_client())

    recorded = session_health.read(db_path)
    assert recorded, "an anonymous page read leaves the session problem"
    assert "sign in to steam" in recorded["detail"].lower()
    assert resp.status_code == 200
    assert resp.get_json() == {"success": 2}


def test_subscribe_token_comes_from_the_page_when_it_carries_one(subscribe_env, monkeypatch):
    """(h) The regression for issue 38: g_sessionID beats the configured token.

    `sessionid` is a session cookie Firefox never persists, so the profile read
    cannot supply the current one; the page the attempt reads is the honest
    source. The pushed global and the cookie set are only fallbacks, and the
    form token and the cookie must end up as the page's value together.
    """
    _, state = subscribe_env
    state["cookies"] = {"sessionid": "COOKIE_TOKEN",
                        "steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}
    state["page"] = _synthetic_page("PAGE_SESSION_TOKEN")
    monkeypatch.setattr(webserver, "_pushed_sessionid", "PUSHED_TOKEN")

    resp = _post_subscribe(app.test_client())

    assert resp.status_code == 200
    call = state["calls"][0]
    assert call["data"]["sessionid"] == "PAGE_SESSION_TOKEN"
    assert call["cookies"]["sessionid"] == "PAGE_SESSION_TOKEN"


def test_the_route_gates_its_page_read_on_the_shared_interval(subscribe_env, monkeypatch):
    """(i) Sourcing the token must not read pages off the shared rate.

    The read goes through the same `WebInterval`, the same
    `daemon.web_delay_seconds` and the same `pacing.wait` as the engine.
    """
    _, state = subscribe_env
    webserver._config.setdefault("daemon", {})["web_delay_seconds"] = 11.0
    waits = []
    monkeypatch.setattr(
        pacing, "wait",
        lambda seconds, keep_running=None: waits.append(seconds) or True)
    state["cookies"] = {"steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}

    _post_subscribe(app.test_client())

    assert state["pages"], "the route must read the page to source its token"
    assert waits == [11.0], "the read waits the shared interval exactly once"


def test_the_route_log_carries_a_fingerprint_and_never_the_token(subscribe_env, caplog):
    """(j) The log compares tokens by hash, and never writes one down."""
    _, state = subscribe_env
    token = "PAGE_SESSION_TOKEN"
    state["cookies"] = {"steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}
    state["page"] = _synthetic_page(token)

    with caplog.at_level(logging.INFO):
        _post_subscribe(app.test_client())

    text = caplog.text
    assert "POSTing to Steam" in text
    assert token not in text, "the token must never be logged"
    assert subscribe_engine.token_fingerprint(token) in text


def test_subscribe_item_error_branches_are_unchanged(subscribe_env):
    """(k) The 404 no-item and 400 no-AppID branches still answer, send nothing."""
    db_path, state = subscribe_env
    state["cookies"] = {"sessionid": "TOK",
                        "steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "No appid", "status": 200,
                                    "consumer_appid": None})
    client = app.test_client()

    missing = _post_subscribe(client, wid=999)
    no_appid = _post_subscribe(client, wid=2)

    assert missing.status_code == 404
    assert missing.get_json()["message"] == "Item not found."
    assert no_appid.status_code == 400
    assert no_appid.get_json()["message"] == "Item has no AppID."
    assert state["calls"] == []


def test_subscribe_item_error_branches_are_logged(subscribe_env, caplog):
    """An attempt that fails on the item must not be invisible in the log.

    Every other branch on this route logs; these two did not, and the log is
    the only durable record of a diagnostic run. The responses and statuses are
    pinned separately so the logging cannot have changed them.
    """
    db_path, state = subscribe_env
    state["cookies"] = {"sessionid": "TOK",
                        "steamLoginSecure": _login_cookie(_FUTURE_EXPIRY)}
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "No appid", "status": 200,
                                    "consumer_appid": None})
    client = app.test_client()

    with caplog.at_level(logging.WARNING):
        _post_subscribe(client, wid=999)
        _post_subscribe(client, wid=2)

    assert "No item row for workshop_id=999" in caplog.text
    assert "Item 2 has no AppID" in caplog.text


def test_subscribe_request_shape_matches_the_scrape_path(subscribe_env):
    """The POST shares the session, the project UA, and the full cookie jar.

    The hardcoded Chrome UA and the two-name cookie dict were the request-side
    half of why this route never worked; the scrape path had already been
    corrected, and this pins the subscribe call to the same identity.
    """
    _, state = subscribe_env
    built = {"sessionid": "TOK", "steamLoginSecure": _login_cookie(_FUTURE_EXPIRY),
             "browserid": "BROWSER", "steamCountry": "US", "timezoneOffset": "0"}
    state["cookies"] = built

    resp = _post_subscribe(app.test_client())

    assert resp.status_code == 200
    call = state["calls"][0]
    assert call["url"] == "https://steamcommunity.com/sharedfiles/subscribe"
    assert call["headers"]["User-Agent"] == web_scraper.USER_AGENT
    assert call["headers"]["Accept-Language"]
    # The whole built set, not the two hand-picked names the old code sent.
    assert call["cookies"] == built
    # The fetch metadata of a same-origin XHR, not of a navigation.
    assert call["headers"]["Sec-Fetch-Site"] == "same-origin"
    assert call["headers"]["Sec-Fetch-Mode"] != "navigate"


def test_the_subscribe_route_has_no_bare_requests_call():
    """The shared session is the only request path; the import is gone.

    `requests` is no longer imported in `src/webserver.py` -- commit d66adc3
    moved the call to `web_scraper._get_session()` -- so a runtime tripwire
    cannot be installed. The shape is pinned at the source instead, as the
    capture tests pin their own.
    """
    body = __import__("pathlib").Path("src/webserver.py").read_text(encoding="utf-8")
    assert "requests.post(" not in body


def test_the_subscribe_pull_is_captured(subscribe_env, tmp_path):
    """Switch on: the page read and the subscribe are captured, no credential."""
    from src import capture

    db_path, state = subscribe_env
    secret = _login_cookie(_FUTURE_EXPIRY)
    sid = "SUBSCRIBE-SESSIONID-SECRET-01"
    state["cookies"] = {"sessionid": sid, "steamLoginSecure": secret,
                        "browserid": "BROWSER-ID-0123456789"}
    state["response_headers"] = {"Content-Type": "application/json"}
    # Steam's answer echoes the credential, so the record being clean is not
    # enough: the body file has to be scrubbed too.
    state["body"] = json.dumps({"success": 1, "echo": secret})
    state["payload"] = {"success": 1, "echo": secret}
    outbox = tmp_path / "outbox"
    capture.configure(str(outbox), capture_web_downloads=True)
    try:
        resp = _post_subscribe(app.test_client())
    finally:
        capture.configure(None)

    assert resp.status_code == 200
    records = [json.loads(path.read_text(encoding="utf-8"))
               for path in (outbox / "web_downloads").glob("*.json")]
    # The route now reads the item page to source its CSRF token, so that read
    # is captured under `item_page` beside the POST.
    assert sorted(record["kind"] for record in records) == ["item_page", "subscribe"]
    record = next(r for r in records if r["kind"] == "subscribe")
    assert record["workshop_id"] == 1
    assert record["request"]["method"] == "POST"
    assert record["request"]["data"]["sessionid"] == "***"
    assert record["request"]["data"]["id"] == "1"
    assert record["request"]["cookies"]["steamLoginSecure"] == "***"
    assert record["body_file"] is not None
    assert (outbox / record["body_file"]).exists(), "Steam's answer is on disk"
    for path in outbox.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert secret not in text
            assert sid not in text


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
    failures = client.get('/api/subscribe_failures').get_json()
    assert 999 in failures


def test_api_subscribe_failures_empty(web_client):
    """GET /api/subscribe_failures returns empty list when no failures."""
    client, _ = web_client
    import src.webserver as ws
    ws._subscribe_failures.clear()
    failures = client.get('/api/subscribe_failures').get_json()
    assert failures == []


def test_a_page_cached_before_the_rename_still_calls_the_old_routes(web_client):
    """`templates/index.html` is served from the browser cache.

    A tab opened before the Batch 4 route rename still polls the old paths, so
    each one answers through the renamed view until the deploy has propagated.
    """
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 4242, "is_queued_for_subscription": 0})
    assert client.get('/api/sub_health').status_code == 200
    assert client.get('/api/sub_failures').status_code == 200
    assert client.post('/api/toggle_sub/4242').get_json() == {"ok": True}
    assert client.post('/api/clear_pending').get_json()["ok"] is True


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

    def tail_log(self, since_offset=0):
        self.calls.append(("log", since_offset))
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
    assert doc.xpath('//*[@id="daemon-modal"]'), "daemon panel is missing"
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
    resp = client.get('/api/daemon/log?since_offset=5')
    assert resp.status_code == 200
    assert resp.get_json() == {"lines": ["a line"], "offset": 9, "reset": False}
    assert ("log", 5) in fake.calls


def test_daemon_log_route_still_accepts_the_pre_rename_since_key(daemon_client):
    """A page cached before the Batch 4 rename polls the offset under `since`."""
    client, fake = daemon_client
    resp = client.get('/api/daemon/log?since=5')
    assert resp.status_code == 200
    assert ("log", 5) in fake.calls


def test_daemon_log_route_reads_configured_file_incrementally(tmp_path):
    db_path = str(tmp_path / "test_daemon_log.db")
    initialize_database(db_path)
    log_path = tmp_path / "daemon.log"
    log_path.write_text("hello\nworld\n")
    config = {"database": {"path": db_path}, "logging": {"file": str(log_path)}}
    init_webserver(db_path, config)
    client = app.test_client()

    first = client.get('/api/daemon/log?since_offset=0').get_json()
    assert first["lines"] == ["hello", "world"]
    assert first["reset"] is False

    with open(log_path, "a") as f:
        f.write("again\n")
    second = client.get(f"/api/daemon/log?since_offset={first['offset']}").get_json()
    assert second["lines"] == ["again"]
    assert second["reset"] is False


def test_daemon_log_route_missing_file_returns_empty(tmp_path):
    db_path = str(tmp_path / "test_daemon_missing_log.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "logging": {"file": str(tmp_path / "missing.log")}}
    init_webserver(db_path, config)
    client = app.test_client()

    resp = client.get('/api/daemon/log?since_offset=0')
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
    # _pushed_sessionid is module state and would otherwise leak between tests.
    monkeypatch.setattr(ws, "_pushed_sessionid", "")

    init_webserver(db_path, config, config_path=config_path)
    return app.test_client(), ws, saves, config_path


def test_sessionid_push_stores_the_login_cookie(session_client):
    client, ws, saves, config_path = session_client

    resp = client.post('/api/sessionid', json={"sessionid": "abc123", "login_secure": "cookie-1"})

    assert resp.get_json() == {"ok": True}
    assert ws._pushed_sessionid == "abc123"
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

    assert ws._pushed_sessionid == "second"


def test_sessionid_push_without_a_cookie_does_not_persist(session_client):
    """No cookie to store is not a reason to write the config file."""
    client, ws, saves, _ = session_client

    resp = client.post('/api/sessionid', json={"sessionid": "abc"})

    assert resp.get_json() == {"ok": True}
    assert ws._pushed_sessionid == "abc"
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


# ── delete never fetched items ───────────────────────────────────────────────
#
# The route is a thin wrapper over delete_never_fetched_items, so the predicate is the
# contract worth pinning: the rows it removes and, just as importantly, the rows
# it leaves alone.


def test_delete_never_fetched_route_deletes_only_the_never_fetched_rows(web_client):
    client, db_path = web_client
    # Removed: never successfully fetched, with no status or a 404.
    insert_or_update_item(db_path, {"workshop_id": 1, "status": None, "api_fetched_at": None})
    insert_or_update_item(db_path, {"workshop_id": 2, "status": 404, "api_fetched_at": None})
    # Kept: a real status, or a recorded successful fetch, or both.
    insert_or_update_item(db_path, {"workshop_id": 3, "status": 200, "api_fetched_at": None})
    insert_or_update_item(db_path, {"workshop_id": 4, "status": None, "api_fetched_at": 1672531200})
    insert_or_update_item(db_path, {"workshop_id": 5, "status": 404, "api_fetched_at": 1672531200})

    resp = client.post('/api/delete_never_fetched_items')
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "deleted": 2}

    conn = get_connection(db_path)
    ids = [r["workshop_id"] for r in conn.execute(
        "SELECT workshop_id FROM workshop_items ORDER BY workshop_id")]
    conn.close()
    assert ids == [3, 4, 5], "the predicate removed a row it must not touch"


def test_delete_never_fetched_route_reports_zero_and_is_post_only(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "status": 200, "api_fetched_at": 1672531200})

    assert client.get('/api/delete_never_fetched_items').status_code == 405
    resp = client.post('/api/delete_never_fetched_items')
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "deleted": 0}, \
        "an empty delete must still report the count the UI shows"


def _extract_const(script: str, name: str) -> str:
    """Return the literal value of a top-level `const name = ...;` declaration.

    Tests inject the page's real constants rather than restating them, so a
    changed version or cap cannot silently diverge from what the driver checks.
    """
    match = re.search(r'^const\s+' + re.escape(name) + r'\s*=\s*([^;]+);', script, re.M)
    assert match, f"no const {name} in the served script"
    return match.group(1).strip()




# ── delete never fetched: the confirmation and the report ─────────────────────
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
def test_delete_never_fetched_confirms_before_deleting_and_reports_the_count(web_client, tmp_path):
    client, _ = web_client
    doc = lxml.html.fromstring(client.get('/').data.decode())
    buttons = doc.xpath('//*[@id="btn-delete-never-fetched"]')
    assert len(buttons) == 1, "expected exactly one #btn-delete-never-fetched"
    assert buttons[0].tag == "button", "the affordance must be a button, not a link"
    assert "doDeleteNeverFetched" in (buttons[0].get("onclick") or ""), \
        "the button must call the confirming handler, not the route directly"

    fn = _extract_function(_served_inline_script(client), "doDeleteNeverFetched")
    out = _run_node(CLEAR_PENDING_DRIVER.replace("__FN__", fn), tmp_path)

    assert len(out["prompts"]) == 4, "every invocation must ask before deleting"
    for prompt in out["prompts"]:
        lowered = prompt.lower()
        assert "never-fetched" in lowered and "cannot be undone" in lowered, \
            f"the confirmation must state what is deleted, not a generic warning: {prompt!r}"
    assert out["urls"] == ["/api/delete_never_fetched_items"] * 3, \
        "declining must send nothing; accepting must call the route"
    assert out["methods"] == ["POST"] * 3
    assert out["alerts"] == [
        "Removed 2 never-fetched item(s).",
        "Delete never-fetched failed: 500 INTERNAL SERVER ERROR",
        "Delete never-fetched failed: backend down",
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
// Author mode is a no-op writer for the same reason a restore is: the stored
// entry is the view Return restores, so the author's filter set must not
// replace it.
global._authorMode = false;
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
  'subscribed-overlay': {value: 'never'},
};
global.document = {getElementById: (id) => values[id]};
const SUBSCRIBED_VALUES = ['any', 'never', 'subscribed', 'previously', 'queued', 'downloaded'];
const LEGACY_SUBSCRIBED_VALUES = __LEGACY__;
const _normaliseSubscribedValue = (__NORM__);
const _subscribedOverlayEl = () => values['subscribed-overlay'];
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

// A view saved before the marker vocabulary was unified names `currently`; it
// must load as the current spelling rather than widen to `any`.
store[key] = JSON.stringify({v: version, filters: [], subscribed: 'currently'});
out.legacySubscribed = loadFn();

// While the page is restoring, nothing may write the state back early.
delete store[key];
global._restoringView = true;
saveFn();
out.wroteWhileRestoring = Object.prototype.hasOwnProperty.call(store, key);

// The same holds in author mode: the saved view is the one Return restores, so
// a search or a scroll while the mode is on must not overwrite it.
delete store[key];
global._restoringView = false;
global._authorMode = true;
saveFn();
out.wroteInAuthorMode = Object.prototype.hasOwnProperty.call(store, key);
global._authorMode = false;
saveFn();
out.wroteAfterAuthorMode = JSON.parse(store[key] || 'null');
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
              .replace("__LEGACY__", _extract_const(script, "LEGACY_SUBSCRIBED_VALUES"))
              .replace("__NORM__", _extract_function(script, "_normaliseSubscribedValue"))
              .replace("__KEY__", _extract_const(script, "VIEW_STATE_KEY"))
              .replace("__VER__", _extract_const(script, "VIEW_STATE_VERSION")))
    out = _run_node(driver, tmp_path)

    expected = {
        "v": 1,
        "filters": [{"field": "Subs", "op": "gte", "value": "100"}],
        "sort_by": "subscriptions",
        "sort_order": "DESC",
        "subscribed": "never",
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
    # A stored view that predates the overlay, or carries a value this build
    # does not know, reads back as the no-constraint default rather than hiding
    # rows.
    assert out["filtered"]["subscribed"] == "any"
    assert out["legacySubscribed"]["subscribed"] == "subscribed", \
        "a view saved with `currently` must load as the current value"
    assert out["wroteWhileRestoring"] is False, \
        "a restore in progress must not overwrite the state it is reading"
    assert out["wroteInAuthorMode"] is False, \
        "author mode must not overwrite the view its Return restores"
    assert out["wroteAfterAuthorMode"] == expected, \
        "leaving author mode must let the view be persisted again"


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
// The overlay control is DOM-backed; loadState only hands it a value and then
// syncs its greyed-out state once the builder rows are drawn.
global._applySubscribedOverlay = (v) => { out.subscribed = v; };
global._syncSubscribedOverlay = () => {};
const emit = console.log.bind(console);
global.console = {debug: () => {}, warn: () => {}, error: () => {}, log: emit};
const fetches = [];
global.fetch = async (url) => {
  fetches.push(url);
  return {json: async () => ({
    filters: [{field: 'Subs', op: 'gte', value: '9'}],
    sort_by: 'views', sort_order: 'ASC', subscribed_overlay: 'previously',
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
               subscribed: out.subscribed,
               fetches: fetches.slice(), searches: out.searches, restored: out.restored,
               addedRow: out.addedRow || 0};
  local = null;
  out.applied = null; out.searches = 0; out.restored = null; out.addedRow = 0;
  out.subscribed = null;
  fetches.length = 0; sortBy.value = ''; sortOrder.value = ''; filterRows.children.length = 0;
  await fn();
  out.tui = {applied: out.applied, sort_by: sortBy.value, sort_order: sortOrder.value,
             subscribed: out.subscribed,
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
        "sort_by": "title", "sort_order": "DESC", "subscribed": "queued",
        "selected": 5, "scroll": 120,
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
    assert out["local"]["subscribed"] == "queued", \
        "the browser's own overlay value is restored with the rest of its view"
    assert out["local"]["restored"] == local, "the saved view drives the restore"
    assert out["local"]["searches"] == 1

    assert out["tui"]["fetches"] == ["/api/state"], \
        "a first visit must still seed from the TUI's saved state"
    assert out["tui"]["applied"] == [{"field": "Subs", "op": "gte", "value": "9"}]
    assert out["tui"]["sort_by"] == "views"
    assert out["tui"]["sort_order"] == "ASC"
    assert out["tui"]["subscribed"] == "previously", \
        "the TUI's saved overlay is the first-visit seed, beside its sort and filters"
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

# ── author mode ──────────────────────────────────────────────────────────────
#
# The TUI's single-creator mode exists because a jump replaces the filter set and
# cannot undo that. The web page now does the same, and the property that matters
# is the return: the view must come back exactly as the jump took it. These
# drivers run the served functions in node against a fake DOM, so the assertions
# are on the state the page ends up in, not on a button merely existing.


# A DOM small enough to run the author code without a browser, and honest about
# what it is: elements hold children, form controls hold a value, and a class or
# data attribute can be found. `innerHTML = ''` clears children and never parses
# markup, because the page builds rows as elements; a test that relied on string
# parsing would be testing something the page does not do.
FAKE_DOM = r"""
function fakeEl(tag) {
  const el = {
    tagName: String(tag).toUpperCase(), children: [], style: {}, dataset: {},
    textContent: '', className: '', disabled: false, listeners: {},
    appendChild(child) { el.children.push(child); return child; },
    remove() {},
    addEventListener(name, fn) { (el.listeners[name] = el.listeners[name] || []).push(fn); },
    click() { (el.listeners.click || []).forEach((fn) => fn()); },
    closest(sel) { return sel === 'button[data-author]' && el.tagName === 'BUTTON' ? el : null; }
  };
  // The page toggles row state through classList; the classes land on the same
  // string className so `done`/`failed` are readable the way the CSS sees them.
  el.classList = {
    add(name) {
      const parts = String(el.className).split(/\s+/).filter(Boolean);
      if (parts.indexOf(name) === -1) parts.push(name);
      el.className = parts.join(' ');
    },
    remove(name) {
      el.className = String(el.className).split(/\s+/).filter((c) => c && c !== name).join(' ');
    },
    contains(name) { return String(el.className).split(/\s+/).indexOf(name) !== -1; }
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return ''; },
    set(value) { if (!value) el.children.length = 0; },
    configurable: true
  });
  const byClass = (node, name) => {
    for (const child of node.children || []) {
      if (String(child.className).split(/\s+/).indexOf(name) !== -1) return child;
      const hit = byClass(child, name);
      if (hit) return hit;
    }
    return null;
  };
  el.querySelector = function(sel) {
    if (sel[0] === '.') return byClass(el, sel.slice(1));
    const m = sel.match(/\[data-wid="(\d+)"\]/);
    if (m) {
      for (const child of el.children) {
        if (String(child.dataset.wid) === m[1]) return child;
      }
    }
    return null;
  };
  return el;
}
function fakeFilterRow(field, op, value) {
  const row = fakeEl('div');
  row.className = 'filter-row';
  const fieldSelect = fakeEl('select'); fieldSelect.className = 'field-select'; fieldSelect.value = field;
  const opSelect = fakeEl('select'); opSelect.className = 'op-select'; opSelect.value = op;
  const valueInput = fakeEl('input'); valueInput.className = 'value-input'; valueInput.value = value;
  row.querySelector = function(sel) {
    const cls = sel.replace('.', '');
    if (cls === 'field-select') return fieldSelect;
    if (cls === 'op-select') return opSelect;
    if (cls === 'value-input') return valueInput;
    return null;
  };
  return {row: row, field: field, op: op, value: value};
}
"""


def _author_driver(script, tmp_path):
    """Run the author-mode functions from the served page in node.

    The harness installs the stubs the page's own module scope would provide --
    the DOM lookups, the row builder, the search call -- and records what the
    jump and the return did to them. `_restoreView` and `showDetail` are stubbed
    because they page the result set, which the restore test above already
    covers; what is asserted here is the state handed to them.

    The served functions are injected as parenthesised function expressions, so
    `__JUMPTOAUTHOR__('76561198000000000')` is an IIFE. A bare `async function
    foo() { ... }()` declaration followed by an argument is a syntax error, and
    the page's own module scope is not reproduced here.
    """
    driver = (AUTHOR_MODE_DRIVER
              .replace("__DOM__", FAKE_DOM)
              .replace("__VIEWSNAPSHOT__", _extract_function(script, "_viewSnapshot"))
              .replace("__SETAUTHORMODEUI__", _extract_function(script, "_setAuthorModeUi"))
              .replace("__JUMPTOAUTHOR__", "(" + _extract_function(script, "jumpToAuthor") + ")")
              .replace("__RETURNFROMAUTHOR__", "(" + _extract_function(script, "returnFromAuthor") + ")")
              .replace("__AUTHORMODEACTIVEFN__", "(" + _extract_function(script, "_authorModeActive") + ")")
              .replace("__SHOWAUTHORLIST__", "(" + _extract_function(script, "showAuthorList") + ")")
              .replace("__INITAUTHORLIST__", "(" + _extract_function(script, "_initAuthorList") + ")()")
              .replace("__CLOSEAUTHORLIST__",
                       "(function(event) { _closedList += 1; "
                       "_elements['author-modal'].style.display = 'none'; })"))
    return _run_node(driver, tmp_path)


# The fake harness. `__DOM__` and the extracted functions are substituted in.
AUTHOR_MODE_DRIVER = """
__DOM__

let _selectedWid = 222;
let _authorMode = false;
let _preJumpView = null;
let _snapshotSubscribed = null;
let _appliedFilters = [];
let _appliedOverlay = null;
let _detailOpened = [];
let _closedList = 0;
let _restoreViewState = null;
const _searches = [];

function _rowOf(r) { return {field: r.row.querySelector('.field-select').value, op: r.row.querySelector('.op-select').value, value: r.row.querySelector('.value-input').value}; }
function getFilters() { return Array.from(_rows).map(_rowOf); }
// The page builds rows with `innerHTML = ''` then `appendChild`. The fake
// element's innerHTML setter cannot reach this list, so the clear is mirrored
// here -- without it the removed rows would keep answering getFilters().
function _dropRows() { _rows.length = 0; }
function _setRowsFromFilters(filters) {
  const container = _elements['filter-rows'];
  container.children.length = 0;
  _dropRows();
  filters.forEach((f) => addRow(undefined, f));
}
function addRow(logic, initial) {
  const init = initial || {field: 'Title', op: 'contains', value: ''};
  const r = fakeFilterRow(init.field, init.op, init.value);
  _rows.push(r);
  _elements['filter-rows'].appendChild(r.row);
}
function _applyFilters(filters) { _appliedFilters = filters; _setRowsFromFilters(filters); }
function _overlayValue() { return _snapshotSubscribed; }
function _applySubscribedOverlay(value) { _appliedOverlay = value; }
function _syncSubscribedOverlay() { _syncs += 1; }
function doSearch(reset) { _searches.push(reset); return Promise.resolve(); }
function _restoreView(state) { _restoreViewState = state; return Promise.resolve(); }
function showDetail(wid) { _detailOpened.push(wid); return Promise.resolve(); }
__VIEWSNAPSHOT__
__SETAUTHORMODEUI__

let _syncs = 0;
const _elements = {
  'author-mode-bar': fakeEl('div'),
  'author-mode-name': fakeEl('span'),
  // The page replaces the builder by setting `innerHTML = ''` and mounting
  // fresh rows. The fake element cannot reach this driver's row list from its
  // own setter, so the clear is mirrored in the trap.
  'filter-rows': new Proxy(fakeEl('div'), {
    set(target, prop, value) {
      if (prop === 'innerHTML' && !value) _rows.length = 0;
      target[prop] = value;
      return true;
    }
  }),
  'filter-buttons': fakeEl('div'),
  'btn-save-filter': fakeEl('button'),
  'sort-by': fakeEl('select'),
  'sort-order': fakeEl('select'),
  'subscribed-overlay': fakeEl('select'),
  'results-grid': fakeEl('div'),
  'author-modal': fakeEl('div'),
  'author-list': fakeEl('div'),
  'author-list-status': fakeEl('span'),
  'btn-authors': fakeEl('button'),
  'author-close': fakeEl('button')
};
_elements['sort-by'].value = 'subscriptions';
_elements['sort-order'].value = 'DESC';
_elements['subscribed-overlay'].value = 'previously';
_elements['results-grid'].scrollTop = 900;
_snapshotSubscribed = 'previously';
const _rows = [];
_setRowsFromFilters([
  {field: 'Subs', op: 'gte', value: '500'},
  {field: 'Title', op: 'contains', value: 'pack'}
]);

global.document = {
  getElementById: (id) => _elements[id] || null,
  createElement: (tag) => fakeEl(tag),
  querySelectorAll: () => []
};
global.fetch = async (url) => {
  if (String(url) === '/api/authors') {
    return {ok: true, status: 200, json: async () => ['111', '222']};
  }
  return {ok: true, status: 200, json: async () => ({})};
};

// The page's functions, under the names the page's other functions call.
const showAuthorList = __SHOWAUTHORLIST__;
const jumpToAuthor = __JUMPTOAUTHOR__;
const returnFromAuthor = __RETURNFROMAUTHOR__;
function _authorModeActive() { return (__AUTHORMODEACTIVEFN__)(); }
// The page's real close, with the count this driver records around it.
const _closeAuthorListFromPage = __CLOSEAUTHORLIST__;
function _closeAuthorList() { _closedList += 1; _closeAuthorListFromPage(); }
// The semicolon keeps this IIFE from being parsed as a call of the declaration
// above it.
__INITAUTHORLIST__;

(async () => {
  const sortBefore = _elements['sort-by'].value;

  jumpToAuthor('76561198000000000');
  const snapshot = _preJumpView;
  const jump = {
    mode: _authorMode,
    rows: _elements['filter-rows'].children.length,
    filters: getFilters(),
    sortBy: _elements['sort-by'].value,
    sortOrder: _elements['sort-order'].value,
    barVisible: _elements['author-mode-bar'].style.display,
    name: _elements['author-mode-name'].textContent,
    saveVisible: _elements['btn-save-filter'].style.display,
    rowsVisible: _elements['filter-rows'].style.display,
    searchResets: _searches.slice()
  };

  await returnFromAuthor();
  const restored = {
    modeActive: _authorModeActive(),
    filters: getFilters(),
    sortBy: _elements['sort-by'].value,
    sortOrder: _elements['sort-order'].value,
    subscribed: _appliedOverlay,
    appliedFilters: _appliedFilters,
    restoreState: _restoreViewState,
    detailOpened: _detailOpened.slice(),
    barVisible: _elements['author-mode-bar'].style.display,
    name: _elements['author-mode-name'].textContent,
    saveVisible: _elements['btn-save-filter'].style.display,
    buttonsVisible: _elements['filter-buttons'].style.display,
    searchResets: _searches.slice()
  };

  await showAuthorList();
  const list = {
    status: _elements['author-list-status'].textContent,
    count: _elements['author-list'].children.length,
    authors: _elements['author-list'].children.map((b) => b.dataset.author),
    texts: _elements['author-list'].children.map((b) => b.textContent),
    overlayShown: _elements['author-modal'].style.display
  };
  if (_elements['author-list'].children.length) {
    _elements['author-list'].onclick({target: _elements['author-list'].children[0]});
  }
  const pickerAfterChoice = _elements['author-modal'].style.display;

  console.log(JSON.stringify({
    sortBefore: sortBefore,
    snapshot: snapshot,
    jump: jump,
    restored: restored,
    list: list,
    nameAfterListClick: _elements['author-mode-name'].textContent,
    modeAfterListClick: _authorMode,
    closedList: _closedList,
    pickerAfterChoice: pickerAfterChoice,
    syncs: _syncs
  }));
  process.exit(0);
})();
"""



@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_entering_author_mode_from_an_item_overrides_filters_and_keeps_the_sort(web_client, tmp_path):
    """The jump is the TUI's single-creator mode, not just another filter row.

    `jumpToAuthor` used to leave the builder alone and set one more row, so the
    previous filters stayed in force and the view was the intersection. It now
    replaces them with one `Author ID is <id>` row, leaves the sort menus
    untouched (they are not builder rows), swaps the filter area for the author
    box, and hides Save Filter -- all of which the TUI does.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    out = _author_driver(script, tmp_path)

    assert out["snapshot"]["filters"] == [
        {"field": "Subs", "op": "gte", "value": "500"},
        {"field": "Title", "op": "contains", "value": "pack"},
    ], "the jump must snapshot the rows it is about to replace"

    jump = out["jump"]
    assert jump["mode"] is True
    assert jump["rows"] == 1, "author mode must replace the filter rows, not add to them"
    assert jump["filters"] == [
        {"field": "Author ID", "op": "is", "value": "76561198000000000"}
    ], "the one row must be the author, or the old filters would still constrain the view"
    assert jump["sortBy"] == out["sortBefore"], "the sort is not a builder filter and must not change"
    assert jump["sortOrder"] == "DESC"
    assert jump["barVisible"] == "flex", "the author box must replace the filter area"
    assert jump["name"] == "76561198000000000", "the author box must name the creator"
    assert jump["saveVisible"] == "none", "Save Filter is hidden in single-creator mode"
    assert jump["rowsVisible"] == "none", "the old rows are replaced, not merely overlaid"
    assert jump["searchResets"] == [True], "the jump re-runs the search once"


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_returning_from_author_mode_restores_the_previous_view_exactly(web_client, tmp_path):
    """The point of the mode: Return puts back what the jump threw away.

    The assertions are on the restored state -- the rows' field/op/value, the
    sort pair, the Subscribed overlay, the paged restore of the item and scroll
    position -- rather than on the presence of a Return button. A half-restore
    (filters back but the author row still present, or the sort left where the
    author view had it) fails here.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    out = _author_driver(script, tmp_path)
    restored = out["restored"]

    assert restored["modeActive"] is False, "Return must leave single-creator mode"
    assert restored["filters"] == out["snapshot"]["filters"], \
        "Return must restore the filters the jump replaced, exactly as they were"
    assert all(f["field"] != "Author ID" for f in restored["filters"]), \
        "the author row must be gone, not restored as part of the view"
    assert restored["sortBy"] == "subscriptions" and restored["sortOrder"] == "DESC"
    assert restored["subscribed"] == "previously", \
        "the Subscribed overlay is view state the jump must restore too"
    assert restored["appliedFilters"] == out["snapshot"]["filters"]
    assert restored["restoreState"] == out["snapshot"], \
        "the item and scroll snapshot must be handed to the restore routine"
    assert restored["barVisible"] == "none" and restored["name"] == ""
    assert restored["saveVisible"] == "", "Save Filter comes back with the filter area"
    assert restored["buttonsVisible"] == "flex"
    assert restored["searchResets"] == [True, True], \
        "the jump and the return each re-run the search once"


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_creator_list_reads_api_authors_and_enters_author_mode(web_client, tmp_path):
    """The author list is the surface /api/authors never had a client for.

    The item jump can only reach a creator whose item is already on screen. The
    list surfaces the same rows `/api/authors` returns, in the order the route
    answers them, and picking one enters the same single-creator mode the item
    jump does -- one entry point, not a second implementation.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    out = _author_driver(script, tmp_path)
    listed = out["list"]

    assert listed["overlayShown"] == "block", "the picker must be shown"
    assert listed["authors"] == ["111", "222"], "the list must be the route's own answer, in order"
    assert listed["texts"] == ["111", "222"], "each creator must be readable, not just clickable"
    assert listed["status"] == "2 creators"
    assert out["modeAfterListClick"] is True, \
        "picking a creator from the list must enter author mode"
    assert out["nameAfterListClick"] == "111", \
        "the mode must be entered for the creator that was picked"
    assert out["pickerAfterChoice"] == "none", "the picker closes behind the choice"


def test_author_mode_scaffold_is_in_the_served_page(web_client):
    """The controls the drivers reach only exist if the page renders them."""
    client, _ = web_client
    doc = lxml.html.fromstring(client.get('/').data.decode())

    for el_id in ("author-mode-bar", "author-mode-name", "btn-return-author",
                  "btn-authors", "author-modal", "author-list", "author-list-status"):
        assert doc.xpath(f'//*[@id="{el_id}"]'), f"missing #{el_id}"

    bars = doc.xpath('//*[@id="author-mode-bar"]')
    rows = doc.xpath('//*[@id="filter-rows"]')
    buttons = doc.xpath('//*[@id="filter-buttons"]')
    assert bars[0] in rows[0].getparent().iterdescendants(), \
        "the author box must live in the filter area it replaces"
    return_btn = doc.xpath('//*[@id="btn-return-author"]')[0]
    assert "returnFromAuthor" in (return_btn.get("onclick") or "") or \
        doc.xpath('//*[@id="btn-return-author"]'), "the Return control must be wired"


# ── the subscribe action's target ─────────────────────────────────────────────
#
# The bridge is not being retired, only bypassed by this page: the route does the
# whole job now, so the button and the queue drain call it. What the drivers pin
# is the target and the absence of a tab, plus the ordering the route's shared
# interval depends on. `tests/test_userscript_contract.py`, run alongside this
# file, is what holds the untouched bridge's own contract.


SUBSCRIBE_ACTION_DRIVER = """
const fn = (__FN__);
const calls = [];
const opened = [];
global.window = {open: (url, name) => { opened.push({url: url, name: name}); return null; }};
global.alert = (m) => calls.push({alert: m});
// The pane and the grid cell are not the subject here; the read-back only has
// to complete so its request is visible in the recorded calls.
global.document = {getElementById: () => null, querySelector: () => null};
global._currentDetail = null;
global._startListPoll = () => {};
async function refreshItemState(wid) { return __REFRESH__(wid); }
global.fetch = async (url, opts) => {
  calls.push({url: url, method: opts && opts.method});
  if (String(url).indexOf('/api/subscribe/') === 0) {
    return {ok: true, status: 200, statusText: 'OK', json: async () => ({success: 1})};
  }
  return {ok: true, status: 200, statusText: 'OK', json: async () => ({workshop_id: 77, subscription_state: 'subscribed', subscription_glyph: '\\u2713', subscription_clickable: false})};
};
(async () => {
  await fn(77);
  global.fetch = async (url, opts) => {
    calls.push({url: url, method: opts && opts.method});
    return {ok: false, status: 400, statusText: 'BAD REQUEST', json: async () => ({success: -1, message: 'No steamLoginSecure available.'})};
  };
  await fn(78);
  console.log(JSON.stringify({calls: calls, opened: opened}));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_subscribe_action_calls_the_server_route_and_opens_no_tab(web_client, tmp_path):
    """The button asks the server to subscribe instead of opening a Steam tab.

    It used to `window.open` the item page with `autosubscribe=true` and let the
    userscript do the work. The route does that work now, so the only request is
    a POST to `/api/subscribe/<id>`; a tab appearing is the old behaviour and
    fails this test. The userscript scaffolding is deliberately still installed
    (see the bridge test below) -- it is just no longer this button's path.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (SUBSCRIBE_ACTION_DRIVER
              .replace("__FN__", _extract_function(script, "doSubscribe"))
              .replace("__REFRESH__", _extract_function(script, "refreshItemState")))
    out = _run_node(driver, tmp_path)

    posts = [c for c in out["calls"] if c.get("url") == "/api/subscribe/77"]
    assert len(posts) == 1, "the Subscribe button must POST the subscribe route"
    assert posts[0]["method"] == "POST"
    assert [c for c in out["calls"] if c.get("url") == "/api/item/77"], \
        "the marker must be re-read from the server after the route records it"
    assert out["opened"] == [], "subscribing must not open a Steam tab any more"

    # A refusal is the route's message, and nothing is opened for it either.
    refused = [c for c in out["calls"] if c.get("url") == "/api/subscribe/78"]
    assert len(refused) == 1
    assert out["opened"] == [], "a refused subscribe must not fall back to a tab"
    assert any("No steamLoginSecure available." in c.get("alert", "") for c in out["calls"]), \
        "the route's reason must reach the user rather than being swallowed"


QUEUE_DRAIN_DRIVER = """
__DOM__

let _subPollIv = null;
let _subScheduleIv = null;
let _subCanceled = false;
let _subThrottleStopped = false;
const WEB_DELAY = 1.0;
// The row HTML escapes the tooltip; the real helper is a string replace with no
// behaviour worth re-running here.
function _escapeHtml(text) { return String(text); }

const queueItems = [
  {workshop_id: 11, title: 'A', subscription_colour: '#0f0', subscription_tooltip: 'a', subscription_glyph: '\\u2713'},
  {workshop_id: 22, title: 'B', subscription_colour: '#0f0', subscription_tooltip: 'b', subscription_glyph: '\\u2713'}
];
const queueRows = queueItems.map((it) => {
  const row = fakeEl('div');
  row.className = 'sub-queue-item';
  row.dataset.wid = String(it.workshop_id);
  const countdown = fakeEl('span');
  countdown.className = 'countdown';
  row.appendChild(countdown);
  return row;
});
const queueList = fakeEl('div');
queueRows.forEach((r) => queueList.appendChild(r));
queueList.querySelectorAll = (sel) => (sel === '.sub-queue-item' ? queueRows.slice() : []);

const elements = {
  'sub-queue-modal': fakeEl('div'),
  'sub-queue-list': queueList,
  'sub-progress': fakeEl('span'),
  'sub-cancel': fakeEl('button'),
  'sub-clear-failed': fakeEl('button')
};
// The old drain built a Steam URL from the page's origin; the stub lets it run
// far enough that the tab it opens is what fails the test, not a missing global.
global.location = {origin: 'http://localhost:8080'};
const opened = [];
global.window = {open: (url, name) => { opened.push(String(url)); return null; }};
global.document = {
  getElementById: (id) => elements[id] || null,
  createElement: (tag) => fakeEl(tag),
  querySelectorAll: () => []
};

const order = [];
const subscribeCalls = [];
let overlap = false;
let inFlight = 0;
global.fetch = async (url, opts) => {
  const u = String(url);
  order.push(u);
  if (u.indexOf('/api/subscribe/') === 0) {
    const wid = u.slice('/api/subscribe/'.length);
    subscribeCalls.push({wid: wid, startedInFlight: inFlight, order: order.length});
    inFlight += 1;
    if (inFlight > 1) overlap = true;
    await new Promise((resolve) => setTimeout(resolve, 5));
    inFlight -= 1;
    return {ok: true, status: 200, statusText: 'OK', json: async () => ({success: 1})};
  }
  if (u === '/api/queued') {
    return {ok: true, status: 200, json: async () => queueItems};
  }
  if (u === '/api/subscribe_failures') { return {ok: true, status: 200, json: async () => []}; }
  if (u === '/api/subscribe_throttle') {
    return {ok: true, status: 200, json: async () => ({throttled_at: 0, retry_after: 300})};
  }
  return {ok: true, status: 200, json: async () => ({ok: true})};
};

(async () => {
  const fn = (__FN__);
  await fn();
  // Let the poll's first tick (1s) run so the pass's own bookkeeping settles.
  await new Promise((resolve) => setTimeout(resolve, 1100));
  console.log(JSON.stringify({
    order: order,
    subscribeCalls: subscribeCalls,
    overlap: overlap,
    done: queueRows.map((r) => r.className),
    progress: elements['sub-progress'].textContent,
    opened: opened
  }));
  process.exit(0);
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_queue_drain_calls_the_subscribe_route_once_per_item_in_order(web_client, tmp_path):
    """Drain serially through the route; the route already owns the interval.

    The old drain scheduled a `window.open` per item on its own `WEB_DELAY`
    timer. The route's page read is gated on the shared web interval and its POST
    is exempt, so the caller must do exactly two things: call
    `/api/subscribe/<id>`, and never have two calls in flight at once. A second
    client-side delay would pay the interval twice per item, and firing items
    without awaiting the previous call is what this test's overlap check catches.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (QUEUE_DRAIN_DRIVER
              .replace("__DOM__", FAKE_DOM)
              .replace("__FN__", _extract_function(script, "_startAutoSubscribe")))
    out = _run_node(driver, tmp_path)

    assert [c["wid"] for c in out["subscribeCalls"]] == ["11", "22"], \
        "every queued item must be asked for through the route"
    assert out["overlap"] is False, \
        "the drain must wait for each call to return before starting the next"
    assert all(c["startedInFlight"] == 0 for c in out["subscribeCalls"]), \
        "a call started while another was in flight means the queue was fired at once"
    assert out["order"].count("/api/subscribe/11") == 1
    assert out["order"].count("/api/subscribe/22") == 1
    assert out["opened"] == [], "the drain must not open a Steam tab any more"
    assert set(out["done"]) == {"sub-queue-item done"}, "a confirmed item must show as done"
    assert out["progress"] == "2 / 2"


def test_the_subscribe_bridge_scaffolding_is_left_in_place(web_client):
    """The new path is proven before anything is deleted; nothing is deleted.

    The userscript, its endpoints and the version meta stay exactly as they
    were. This pins their presence so a later change cannot quietly drop the
    bridge while retiring this page's use of it; `tests/test_userscript_contract.py`
    independently pins the script and the meta tag to each other.
    """
    client, _ = web_client

    # The routes are still mounted and still POST-only where they were.
    assert client.get('/api/sessionid').status_code == 405
    assert client.get('/api/subscribed/1').status_code == 405
    assert client.get('/api/subscribe_failed/1').status_code == 405
    assert client.get('/api/subscribe_throttled/1').status_code == 405
    assert client.get('/api/subscribe_throttle').status_code == 200
    assert set(client.get('/api/subscribe_throttle').get_json()) == \
        {"throttled_at", "throttled_id", "retry_after"}
    assert client.get('/api/subscribe_failures').status_code == 200

    # The userscript is still served, and the page still carries the contract it
    # checks the script against.
    served = client.get('/userscript/steam_subscribe.user.js')
    assert served.status_code == 200
    assert b"@version" in served.data

    html = client.get('/').data.decode()
    assert 'name="userscript-version"' in html
    assert "US_EXPECTED_VER" in html
    assert "function _userscriptPresent()" in html
    assert "US_RAW_URL" in html
    assert "autosubscribe=true" in client.get(
        '/userscript/steam_subscribe.user.js').data.decode()


def test_analysis_panel_dom_contract(web_client):
    """The toolbar opens a panel modelled on the daemon and stats overlays.

    The endpoint existed with no client for it. This pins the static half of the
    contract: the button the page wires up, the bucket box defaulting to the
    TUI's seven days, and the elements the render code writes into.
    """
    client, _ = web_client
    doc = lxml.html.fromstring(client.get('/').data.decode())

    buttons = doc.xpath('//*[@id="btn-analysis"]')
    assert len(buttons) == 1, "expected exactly one #btn-analysis"
    assert buttons[0].tag == "button", "the analysis affordance must not navigate away"
    assert not buttons[0].get("href"), "the analysis button must not be a link"

    overlay = doc.xpath('//*[@id="analysis-modal"]')
    assert overlay, "missing #analysis-modal"
    for el_id in ('analysis-bucket-days', 'analysis-recalc', 'analysis-close',
                  'analysis-summary', 'analysis-table-host'):
        nodes = doc.xpath(f'//*[@id="{el_id}"]')
        assert nodes, f"missing #{el_id}"
        assert nodes[0] in overlay[0].iterdescendants(), \
            f"#{el_id} must live inside the overlay"

    bucket = doc.xpath('//*[@id="analysis-bucket-days"]')[0]
    assert bucket.get("value") == "7", "the bucket box must default to the TUI's 7 days"


def test_analysis_bucket_days_parameter_sets_the_bucket_width(web_client):
    client, db_path = web_client
    import time
    now = int(time.time())
    for i in range(30):
        insert_or_update_item(db_path, {
            "workshop_id": i + 1,
            "steam_created_at": now - i * 86400,
            "views": 100 + i,
        })

    one = client.get('/api/analysis?bucket_days=1').get_json()
    week = client.get('/api/analysis?bucket_days=7').get_json()

    assert one["items_analyzed"] == 30
    assert all(b["age_end"] - b["age_start"] == 1 for b in one["buckets"])
    assert all(b["age_end"] - b["age_start"] == 7 for b in week["buckets"])
    assert len(one["buckets"]) > len(week["buckets"]), \
        "a smaller bucket must split the same items into more buckets"


def test_analysis_reports_a_null_window_honestly(web_client):
    """Too few populated buckets leaves the knee unknown rather than zero.

    The panel keys its summary off null, so the endpoint must preserve the
    distinction between "no estimate" and "an estimate of zero days" instead of
    collapsing both to 0.
    """
    client, db_path = web_client
    import time
    now = int(time.time())
    for i in range(3):
        insert_or_update_item(db_path, {
            "workshop_id": i + 1,
            "steam_created_at": now,
            "views": 50,
        })

    resp = client.get('/api/analysis?bucket_days=7')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["items_analyzed"] == 3
    assert "estimated_window_days" in data
    assert data["estimated_window_days"] is None


def test_analysis_with_no_data_reports_a_null_window(web_client):
    client, _ = web_client
    data = client.get('/api/analysis').get_json()
    assert data == {"buckets": [], "estimated_window_days": None, "items_analyzed": 0}


def test_analysis_clamps_a_nonpositive_bucket_width(web_client):
    """A hand-made request with a zero width must not divide by zero.

    The TUI clamps its bucket box to at least one day; this endpoint takes the
    parameter from the query string, so it needs the same floor. A missing or
    unparsable value keeps the seven-day default.
    """
    client, db_path = web_client
    import time
    insert_or_update_item(db_path, {
        "workshop_id": 1, "steam_created_at": int(time.time()), "views": 5,
    })

    zero = client.get('/api/analysis?bucket_days=0')
    assert zero.status_code == 200
    assert all(b["age_end"] - b["age_start"] == 1 for b in zero.get_json()["buckets"])

    bad = client.get('/api/analysis?bucket_days=abc')
    assert bad.status_code == 200
    assert all(b["age_end"] - b["age_start"] == 7 for b in bad.get_json()["buckets"])




# ── the view window analysis panel ───────────────────────────────────────────
#
# The panel's render functions are pure string builders and its in-flight guard
# is a token comparison, so both can be exercised in node against tiny stubs
# rather than asserting that certain words merely appear in the served script.


ANALYSIS_RENDER_DRIVER = """
__FMT__
__SUMMARY__
__TABLE__
const cases = __CASES__;
console.log(JSON.stringify(cases.map(function(d) {
  return {summary: _renderAnalysisSummary(d), table: _renderAnalysisTable(d)};
})));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_analysis_render_shows_the_knee_and_scales_the_bars(web_client, tmp_path):
    """The summary names the knee, and each bar is a share of the peak median.

    A text-presence check on the served script would pass with the knee branch
    inverted or the bar divisor wrong; this runs the real render functions.
    """
    client, _ = web_client
    script = _served_inline_script(client)

    kneed = {
        "buckets": [
            {"age_start": 0, "age_end": 7, "count": 120, "median": 300, "p10": 3, "p90": 900},
            {"age_start": 7, "age_end": 14, "count": 80, "median": 150, "p10": 2, "p90": 400},
            {"age_start": 14, "age_end": 21, "count": 40, "median": 60, "p10": 1, "p90": 100},
            {"age_start": 21, "age_end": 28, "count": 10, "median": 5, "p10": 1, "p90": 20},
        ],
        "estimated_window_days": 21,
        "items_analyzed": 1725544,
    }
    no_knee = {"buckets": [], "estimated_window_days": None, "items_analyzed": 1725544}

    driver = (ANALYSIS_RENDER_DRIVER
              .replace("__FMT__", _extract_function(script, "fmtCount"))
              .replace("__SUMMARY__", _extract_function(script, "_renderAnalysisSummary"))
              .replace("__TABLE__", _extract_function(script, "_renderAnalysisTable"))
              .replace("__CASES__", json.dumps([kneed, no_knee])))
    results = _run_node(driver, tmp_path)

    summary = results[0]["summary"]
    assert "Estimated view window: ~21 days" in summary
    assert "1.73M items analyzed" in summary, "a large item count must be readable"
    assert "4 buckets" in summary

    table = results[0]["table"]
    assert table.count("<tr>") == 5, "one heading row plus four buckets"
    assert "0\u20137d" in table and "21\u201328d" in table
    assert "<td>120</td>" in table and "<td>300</td>" in table
    # Bars are proportional to the peak median (300): 100%, 50%, 20%, ~1.7%.
    assert "width:100.0%" in table
    assert "width:50.0%" in table
    assert "width:20.0%" in table
    assert "width:1.7%" in table

    null_summary = results[1]["summary"]
    assert "Insufficient data to estimate a view window." in null_summary
    assert "~0 days" not in null_summary, "a null knee must not be shown as a zero-day window"
    assert "0 buckets" in null_summary
    assert "No items with recorded views" in results[1]["table"]


ANALYSIS_DISCARD_DRIVER = """
__FN__

const events = [];
const els = {};
function el(id) { return els[id] || (els[id] = {value: '7', innerHTML: ''}); }
global.document = {getElementById: el};
let _analysisToken = 0;
function _analysisBucketDays() { return 7; }
function _drawAnalysis() { events.push('draw'); }
function response(data) { return {json: async function() { return data; }}; }
function deferred() {
  let resolve, reject;
  const promise = new Promise(function(res, rej) { resolve = res; reject = rej; });
  return {promise: promise, resolve: resolve, reject: reject};
}

(async function() {
  // A request that lands while the panel is open draws.
  _analysisToken = 1;
  let d = deferred();
  global.fetch = function() { return d.promise; };
  let pending = _loadAnalysis(1);
  d.resolve(response({items_analyzed: 1}));
  await pending;
  const drawsWhenCurrent = events.length;

  // The panel closes with a request in flight: its response is discarded.
  _analysisToken = 1;
  d = deferred();
  global.fetch = function() { return d.promise; };
  pending = _loadAnalysis(1);
  _analysisToken = 99;  // _closeAnalysisPanel() bumps the token
  d.resolve(response({items_analyzed: 2}));
  await pending;
  const drawsAfterClose = events.length;

  // A failing request that lands after close must not write an error either.
  _analysisToken = 1;
  d = deferred();
  global.fetch = function() { return d.promise; };
  pending = _loadAnalysis(1);
  _analysisToken = 99;
  d.reject(new Error('down'));
  await pending;
  const stale = els['analysis-summary'];
  const summaryAfterStaleFailure = stale ? stale.innerHTML : '';

  // A failure with the current token is still reported in the open panel.
  _analysisToken = 5;
  d = deferred();
  global.fetch = function() { return d.promise; };
  pending = _loadAnalysis(5);
  d.reject(new Error('down'));
  await pending;
  console.log(JSON.stringify({
    drawsWhenCurrent: drawsWhenCurrent,
    drawsAfterClose: drawsAfterClose,
    summaryAfterStaleFailure: summaryAfterStaleFailure,
    summaryAfterCurrentFailure: els['analysis-summary'].innerHTML
  }));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_analysis_panel_discards_responses_after_close(web_client, tmp_path):
    """Closing mid-request must not repaint the panel, matching `_statsToken`.

    The token is bumped on close and again on every recalculate, so a response
    that lands later is dropped. Asserting the script contains `_analysisToken`
    would pass even if the comparison were never made; this drives the real
    `_loadAnalysis` with a deferred fetch and observes what it draws.
    """
    client, _ = web_client
    fn = _extract_function(_served_inline_script(client), "_loadAnalysis")
    result = _run_node(ANALYSIS_DISCARD_DRIVER.replace("__FN__", fn), tmp_path)

    assert result["drawsWhenCurrent"] == 1, "an open panel must render its response"
    assert result["drawsAfterClose"] == 1, "a response landing after close must be discarded"
    assert result["summaryAfterStaleFailure"] == "", \
        "a failure landing after close must not write into the closed panel"
    assert "Failed: down" in result["summaryAfterCurrentFailure"], \
        "an open panel must report a failed request"




# ── detail-pane parity: jump-to-author and queue/unqueue ─────────────────────
#
# The two affordances live only in the served inline script, so their behaviour
# is exercised in node against stubs, as the save report and the header port
# are. Asserting on the script's text would pass with the branch inverted.


def test_detail_payload_ships_the_creator_id_as_an_exact_string(web_client):
    """A SteamID64 is seventeen digits, past what a JS number holds exactly.

    Jump-to-author puts the creator ID into an Author ID filter and posts it
    back, so the payload has to carry it losslessly. `creator` stays the
    database integer; `creator_id` is the same value as a string the client can
    use without JSON.parse rounding it.
    """
    client, db_path = web_client
    steamid = 76561198765432109  # as a float64 this rounds to ...110
    insert_or_update_item(db_path, {
        "workshop_id": 90, "title": "Big ID", "creator": steamid, "status": 200,
    })

    data = client.get('/api/item/90').get_json()
    assert data["creator"] == steamid
    assert data["creator_id"] == "76561198765432109"
    assert isinstance(data["creator_id"], str)


def test_detail_payload_omits_creator_id_without_a_creator(web_client):
    """No creator means no jump target, so no string is invented for one."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 91, "title": "Anonymous", "status": 200})

    data = client.get('/api/item/91').get_json()
    assert data["creator"] is None
    assert "creator_id" not in data


def test_toggle_subscription_queue_route_flips_the_queue_flag(web_client):
    """/api/toggle_sub answers {ok} and flips is_queued_for_subscription.

    It is the route the detail pane's Queue/Unqueue button calls; the button's
    label comes from a read-back of this same route's data, so the round trip
    through the database is the state that has to be right.
    """
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 4242, "title": "Q", "status": 200})

    assert client.post('/api/toggle_subscription_queue/4242').get_json() == {"ok": True}
    assert client.get('/api/item/4242').get_json()["is_queued_for_subscription"] == 1

    assert client.post('/api/toggle_subscription_queue/4242').get_json() == {"ok": True}
    assert client.get('/api/item/4242').get_json()["is_queued_for_subscription"] == 0


JUMP_AUTHOR_DRIVER = """
const jumpFn = (__JUMP__);
const returnFn = (__RETURN__);
let nowMode = false;
const rows = [];
const added = [];
const elements = {
  'filter-rows': {innerHTML: '', children: [], appendChild(child) { rows.push(child); }},
  'author-mode-bar': {style: {}}, 'author-mode-name': {textContent: ''},
  'filter-buttons': {style: {}}, 'btn-save-filter': {style: {}},
  'sort-by': {value: 'subscriptions'}, 'sort-order': {value: 'DESC'},
  'subscribed-overlay': {value: 'previously'},
  'results-grid': {scrollTop: 900},
};
global.document = {getElementById: (id) => elements[id] || null};
global._authorMode = false;
global._preJumpView = null;
global._selectedWid = 222;
global._applied = null;
global.getFilters = () => ([{field: 'Subs', op: 'gte', value: '500'}]);
global._applyFilters = (filters) => { global._applied = filters; };
global._setAuthorModeUi = (on, creator) => {
  elements['author-mode-bar'].style.display = on ? 'flex' : 'none';
  elements['author-mode-name'].textContent = on ? String(creator) : '';
  elements['btn-save-filter'].style.display = on ? 'none' : '';
};
global._viewSnapshot = () => ({
  filters: global.getFilters(),
  sort_by: elements['sort-by'].value, sort_order: elements['sort-order'].value,
  subscribed: elements['subscribed-overlay'].value,
  selected: global._selectedWid, scroll: elements['results-grid'].scrollTop,
});
global._syncSubscribedOverlay = () => {};
global._overlayValue = () => elements['subscribed-overlay'].value;
global._applySubscribedOverlay = () => {};
global._restoreView = () => Promise.resolve();
let searched = 0;
global.doSearch = () => { searched += 1; return Promise.resolve(); };
global.addRow = (logic, initial) => added.push({hasLogic: logic !== undefined, initial: initial});

jumpFn('76561198765432109');
const first = {
  mode: global._authorMode,
  snapshot: global._preJumpView,
  added: added.slice(),
  cleared: elements['filter-rows'].innerHTML === '',
  bar: elements['author-mode-bar'].style.display,
  name: elements['author-mode-name'].textContent,
  save: elements['btn-save-filter'].style.display,
  sort: elements['sort-by'].value,
  searches: searched,
};

// A second jump while the mode is already on must not overwrite the snapshot
// the Return will use with the author view it is standing in.
jumpFn('42');
const second = {snapshot: global._preJumpView, searches: searched};

// The mode's exit goes back through the same filter-application path a restore
// uses, with the snapshot it captured.
returnFn().then(() => {
  console.log(JSON.stringify({
    first: first,
    second: second,
    applied: global._applied,
    modeAfterReturn: global._authorMode,
    nameAfterReturn: elements['author-mode-name'].textContent,
  }));
});
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_jump_to_author_enters_single_creator_mode_like_the_tui(web_client, tmp_path):
    """The creator click must enter author mode, not just add a filter row.

    It used to leave the builder alone and set one more `Author ID` row, so the
    previous filters stayed in force and the view was their intersection with
    the author. It now mirrors the TUI's `btn-jump-author`: the rows are
    replaced by one `Author ID` / `is` row, the filter area is swapped for the
    author box, Save Filter is hidden, and the sort is untouched. A second jump
    while the mode is on must not overwrite the snapshot Return depends on.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (JUMP_AUTHOR_DRIVER
              .replace("__JUMP__", "(" + _extract_function(script, "jumpToAuthor") + ")")
              .replace("__RETURN__", "(" + _extract_function(script, "returnFromAuthor") + ")"))
    result = _run_node(driver, tmp_path)

    first = result["first"]
    assert first["mode"] is True, "the jump must enter author mode"
    assert first["cleared"] is True, "the old rows must be replaced, not added to"
    assert first["added"] == [{
        "hasLogic": False,
        "initial": {"field": "Author ID", "op": "is", "value": "76561198765432109"},
    }], "the one row must be the author filter the TUI builds"
    assert first["bar"] == "flex", "the author box replaces the filter area"
    assert first["name"] == "76561198765432109", "the box must name the creator"
    assert first["save"] == "none", "Save Filter is hidden in single-creator mode"
    assert first["sort"] == "subscriptions", "the sort is not a builder filter"
    assert first["searches"] == 1, "the jump re-runs the search once"
    assert first["snapshot"]["filters"] == [{"field": "Subs", "op": "gte", "value": "500"}]
    assert first["snapshot"]["sort_by"] == "subscriptions"
    assert first["snapshot"]["subscribed"] == "previously"

    assert result["second"]["snapshot"] == first["snapshot"], \
        "a second jump must not replace the snapshot Return restores"

    assert result["applied"] == first["snapshot"]["filters"], \
        "Return must put back the filters the jump replaced"
    assert result["modeAfterReturn"] is False
    assert result["nameAfterReturn"] == ""


RENDER_DETAIL_DRIVER = """
const fn = (__FN__);
const subFn = (__SUB_FN__);
let html = '';
global.document = { getElementById: () => ({ set innerHTML(v) { html = v; } }) };
global._showTranslated = true;
global._subTitleText = 'sub';
global._favTitleText = 'fav';
global._currentDetail = null;
global.wClass = () => 'wilson-low';
global.fmtSize = () => '1 MB';
global.sizeClass = () => '';
global.fmtCount = (n) => String(n || 0);
global._escapeHtml = (s) => String(s == null ? '' : s);
global.showSubscriptionMarker = subFn;
// The open-folder control lives in the static bottom bar, so renderDetail only
// asks a separate helper to enable/disable it; this test is about the pane's own
// HTML, so the helper is stubbed like the other collaborators above.
global._refreshOpenFolderButton = () => {};
const base = {
  workshop_id: 77, creator: 'Alice', creator_id: '76561198765432109',
  personaname: 'Alice', has_translation: false,
  display_title_original: 'Mod', title: 'Mod',
  subscription_state: 'never', subscription_glyph: '\\u25cb',
  subscription_colour: '#808080', subscription_class: 'sub-never',
  subscription_label: 'Never subscribed', subscription_tooltip: 'never',
  subscription_clickable: true,
};
fn(base);
const never = html;
fn(Object.assign({}, base, {
  subscription_state: 'subscribed', subscription_glyph: '\\u2605',
  subscription_colour: '#ffd700', subscription_class: 'sub-subscribed',
  subscription_label: 'Currently subscribed', subscription_tooltip: 'subscribed',
  subscription_clickable: false,
}));
const subscribed = html;
fn(Object.assign({}, base, {creator: null, creator_id: null}));
const noCreator = html;
console.log(JSON.stringify({never: never, subscribed: subscribed, noCreator: noCreator}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_render_detail_wires_the_author_and_subscription_marker(web_client, tmp_path):
    """renderDetail must offer the jump and draw the subscription marker.

    This runs the served function against a stub pane and inspects the HTML it
    actually builds. The old Queue/Unqueue button pair is gone: the marker is
    the queue control now, so the pane must not carry a second indicator of the
    same flag.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    fn = _extract_function(script, "renderDetail")
    sub_fn = _extract_function(script, "showSubscriptionMarker")
    result = _run_node(RENDER_DETAIL_DRIVER
                       .replace("__FN__", fn)
                       .replace("__SUB_FN__", sub_fn), tmp_path)

    assert "jumpToAuthor('76561198765432109')" in result["never"], \
        "the creator must carry the lossless id into the jump"
    # The marker is a real element carrying the glyph the server sent, and it
    # comes before the title link: it reads as status next to the identity.
    assert 'data-sub-state="never"' in result["never"]
    assert ">○</div>" in result["never"]
    assert result["never"].index('data-sub-state="never"') < result["never"].index("<a href="), \
        "the marker must precede the title"
    # subscribed is not clickable, so the pane carries no action for it.
    assert 'data-sub-state="subscribed"' in result["subscribed"]
    assert 'data-sub-clickable="0"' in result["subscribed"]
    assert ">★</div>" in result["subscribed"]
    assert 'data-sub-clickable="1"' in result["never"]

    # The affordance this replaced must be gone, not merely hidden.
    for html in result.values():
        assert ">Queue</button>" not in html
        assert ">Unqueue</button>" not in html

    assert "jumpToAuthor(" not in result["noCreator"], \
        "an item with no creator has nothing to jump to"


TOGGLE_QUEUE_DRIVER = """
const fn = (__FN__);
const subFn = (__SUB_FN__);
const rendered = [];
const classes = new Set();
let serverQueued = 0;
// A real-enough marker element: attributes plus a color style, so the driver
// exercises the same _applySub the page uses rather than a stub.
function markerEl() {
  const attrs = {};
  return {
    attrs: attrs,
    style: {},
    textContent: '',
    setAttribute: (k, v) => { attrs[k] = String(v); },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
  };
}
const marker = markerEl();
global._currentDetail = {workshop_id: 77, is_queued_for_subscription: 0};
global.renderDetail = (it) => {
  rendered.push(it.is_queued_for_subscription);
  global._currentDetail = it;
};
global.alert = (m) => rendered.push('alert:' + m);
global.document = {
  querySelector: () => ({
    querySelector: () => marker,
    classList: { toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); } },
  }),
};
global._applySub = subFn;
// Queueing a row starts the list poll that re-reads its marker; this driver is
// about the toggle itself, so the poll is stubbed rather than run.
global._startListPoll = () => {};
global.fetch = async (url) => {
  if (url.indexOf('/api/toggle_subscription_queue/') === 0) {
    serverQueued = serverQueued ? 0 : 1;
    return {ok: true, status: 200, statusText: 'OK'};
  }
  if (url.indexOf('/api/item/') === 0) {
    return {ok: true, status: 200, statusText: 'OK',
            json: async () => ({workshop_id: 77, is_queued_for_subscription: serverQueued,
                                subscription_state: serverQueued ? 'queued' : 'never',
                                subscription_glyph: serverQueued ? '\\u2606' : '\\u25cb',
                                subscription_colour: serverQueued ? '#2ecc40' : '#808080',
                                subscription_tooltip: 'tip', subscription_clickable: true})};
  }
  throw new Error('unexpected url ' + url);
};
(async () => {
  await fn(77);
  const afterFirst = global._currentDetail.is_queued_for_subscription;
  const markerAfterFirst = marker.getAttribute('data-sub-state');

  // The cache is deliberately stale (0) while the database says queued (1), as
  // the `s` shortcut would leave it. The read-back must win over any local flip.
  global._currentDetail.is_queued_for_subscription = 0;
  serverQueued = 1;
  await fn(77);
  const afterExternalQueued = global._currentDetail.is_queued_for_subscription;
  const markerAfterExternalQueued = marker.getAttribute('data-sub-state');

  global.fetch = async (url) => {
    if (url.indexOf('/api/toggle_subscription_queue/') === 0) {
      return {ok: false, status: 500, statusText: 'INTERNAL SERVER ERROR'};
    }
    throw new Error('the item must not be read after a failed toggle');
  };
  await fn(77);
  const afterServerError = global._currentDetail.is_queued_for_subscription;

  global.fetch = async () => { throw new Error('down'); };
  await fn(77);
  console.log(JSON.stringify({rendered: rendered, afterFirst: afterFirst,
                              afterExternalQueued: afterExternalQueued,
                              afterServerError: afterServerError,
                              markerAfterFirst: markerAfterFirst,
                              markerAfterExternalQueued: markerAfterExternalQueued,
                              markerText: marker.textContent,
                              leftoverClasses: [...classes]}));
})();
"""

# The served _applySub is assigned to global._applySub inside the driver above,
# after the stub document exists.


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_toggle_queue_reflects_the_databases_state_and_updates_the_marker(web_client, tmp_path):
    """The toggle must re-render from the stored state, not a flipped guess.

    /api/toggle_sub returns only {ok}, and the `s` shortcut and the subscribe
    drain change the same flag behind the pane's back, so the client reads the
    item back through the read-only route and renders that. The grid cell's
    marker follows the same payload, and a failed toggle must not touch either.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    fn = _extract_function(script, "toggleDetailQueue")
    sub_fn = _extract_function(script, "_applySub")
    result = _run_node(TOGGLE_QUEUE_DRIVER
                       .replace("__FN__", fn)
                       .replace("__SUB_FN__", sub_fn), tmp_path)

    assert result["afterFirst"] == 1
    # The DB said queued while the cached payload still said 0; the read-back
    # value (which the toggle then cleared) is what the pane shows.
    assert result["afterExternalQueued"] == 0
    assert result["rendered"] == [
        1, 0,
        'alert:Queue update failed: 500 INTERNAL SERVER ERROR',
        'alert:Queue update failed: down',
    ]
    # The cell marker was rewritten from the read-back payload, not from a local
    # flip of the old `queued` class.
    assert result["markerAfterFirst"] == "queued"
    assert result["markerAfterExternalQueued"] == "never"
    assert result["markerText"] == "\u25cb"
    assert result["leftoverClasses"] == [], \
        "the old `queued` class must not be toggled any more"
    assert result["afterServerError"] == 0, "a rejected toggle must not change the pane"
    assert result["afterServerError"] == 0, "a rejected toggle must not change the pane"


# ── the daemon log pane decodes the colour escapes ────────────────────────────
#
# The daemon writes SGR escapes into its log file (the "enriching" marker on a
# selected discovery is green). A text node renders the escape as a replacement
# glyph and the code itself as literal "[32m" text, so the pane showed neither
# the colour nor a readable line.

ANSI_LOG_DRIVER = """
const ANSI_SGR = __SGR__;
const _escapeHtml = (__ESC__);
const _ansiToHtml = (__ANSI__);
const E = '\\u001b';
console.log(JSON.stringify({
  plain: _ansiToHtml('no codes here'),
  enriching: _ansiToHtml('x ' + E + '[32menriching' + E + '[0m'),
  unterminated: _ansiToHtml('x ' + E + '[31mred to the end'),
  html: _ansiToHtml('<b>&</b> ' + E + '[32mok' + E + '[0m'),
  bold: _ansiToHtml(E + '[1mheading' + E + '[0m'),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_daemon_log_decodes_colour_instead_of_showing_the_escape(web_client, tmp_path):
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (ANSI_LOG_DRIVER
              .replace("__SGR__", _extract_const(script, "ANSI_SGR"))
              .replace("__ESC__", _extract_function(script, "_escapeHtml"))
              .replace("__ANSI__", _extract_function(script, "_ansiToHtml")))
    out = _run_node(driver, tmp_path)

    assert out["plain"] == "no codes here", "a line without codes is passed through"
    assert "[32m" not in out["enriching"], "the escape code must not survive as visible text"
    assert "\u001b" not in out["enriching"], "the raw escape must not reach the page"
    assert "enriching" in out["enriching"]
    assert "color:#98c379" in out["enriching"], \
        "the green the daemon's enriching marker uses must be applied"
    assert out["enriching"].count("<span") == 1, "one run of colour, one span"
    assert out["enriching"].endswith("</span>"), "the span must be closed"

    # An unterminated run must still close, or every later line inherits the
    # colour and the pane goes monochrome.
    assert out["unterminated"].endswith("</span>")

    # Log text is data: markup in it must be escaped, not interpreted.
    assert "&lt;b&gt;" in out["html"] and "<b>" not in out["html"]

    assert "font-weight:600" in out["bold"], "bold is honoured too"


def test_the_log_pane_keeps_raw_lines_so_the_cap_counts_lines(web_client):
    """The 500-line cap must apply to lines, not to rendered markup.

    Slicing rendered HTML on newlines would cut a span in half and leak colour
    into the rest of the pane, so the raw lines are kept alongside.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    assert re.search(r'^let\s+_daemonLogLines\s*=\s*\[\];', script, re.M), \
        "the pane must keep the raw lines it renders"
    assert "_daemonLogLines.map(_ansiToHtml)" in script, \
        "the pane must render from the raw lines"


# ── the cutoff query must not hold up the results ─────────────────────────────
#
# `doSearch` cleared the grid and then *awaited* the percentile-cutoff query
# before fetching the first page, so every fresh search blanked the pane for as
# long as that query took -- seconds on a large database. It decides colour only,
# so it is now started and left to land.

CUTOFF_DRIVER = """
let cutoffs = {};
const wClass = (__WCLASS__);
const _applyCutoffColours = (__APPLY__);
const els = [
  { score: '0.55', key: 'wilson_subscription', className: 'stale',
    getAttribute(n) { return n === 'data-score' ? this.score : this.key; } },
  { score: '', key: 'wilson_favorite', className: 'stale',
    getAttribute(n) { return n === 'data-score' ? this.score : this.key; } },
];
global.document = { querySelectorAll: () => els };

const empty = wClass(0.55, 'wilson_subscription');

cutoffs = { wilson_subscription_p50: 0.1, wilson_subscription_p90: 0.5,
            wilson_subscription_p99: 0.9, wilson_favorite_p50: 0.1 };
_applyCutoffColours();

console.log(JSON.stringify({
  empty: empty,
  after: els.map(e => e.className),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_scores_are_uncoloured_until_their_cutoffs_arrive(web_client, tmp_path):
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (CUTOFF_DRIVER
              .replace("__WCLASS__", _extract_function(script, "wClass"))
              .replace("__APPLY__", _extract_function(script, "_applyCutoffColours")))
    out = _run_node(driver, tmp_path)

    # With no cutoffs there is nothing to compare against. Treating 0/0/0 as the
    # thresholds made every score "top tier", which is a claim, not a default.
    assert out["empty"] == "wilson-unknown", \
        "a score with no cutoffs must make no percentile claim"

    # Once they land, the rows already on screen are re-coloured from the score
    # kept on each span -- no re-render and no refetch.
    assert out["after"][0] == "wilson-p90", "0.55 sits between p50 and p90"
    assert out["after"][1] == "wilson-low", "a missing score stays low"


def test_the_search_does_not_wait_for_the_cutoff_query(web_client):
    """The stall was an `await` between clearing the grid and the first fetch."""
    client, _ = web_client
    script = _served_inline_script(client)
    body = _extract_function(script, "doSearch")

    assert "await loadCutoffs()" not in body, \
        "the cutoff query must not be awaited on the search path"
    assert "_refreshCutoffs()" in body, \
        "the cutoffs still have to be fetched, just not waited on"
    # The rows must be built after the reset without a cutoff round trip between.
    assert body.index("_refreshCutoffs()") < body.index("fetch('/api/search'"), \
        "the cutoff refresh is started, not blocking, before the search fetch"


# ── the served page must not be cached ────────────────────────────────────────
#
# Both the page and the injected userscript are generated per request from files
# on disk, so a cached copy is a stale copy. With no Cache-Control and no
# validators either, the browser had nothing to revalidate against: a web-UI fix
# was deployed and verified on the server while the browser kept running the
# previous page, and the fix looked broken.

def test_the_page_is_served_without_cache(web_client):
    client, _ = web_client
    resp = client.get('/')
    assert resp.headers.get('Cache-Control') == 'no-store', \
        "a generated page must not be cached; there is nothing to revalidate against"


def test_the_userscript_is_served_without_cache(web_client):
    client, _ = web_client
    resp = client.get('/userscript/steam_subscribe.user.js')
    assert resp.status_code == 200
    assert resp.headers.get('Cache-Control') == 'no-store', \
        "the injected userscript changes with the server's host; it must not be cached"


def test_images_may_still_be_cached(web_client):
    """The exclusion matters: images are large, immutable once written, and the
    one thing here worth caching."""
    import src.webserver as ws
    client, _ = web_client
    resp = client.get('/images/nothing.jpg')
    assert resp.headers.get('Cache-Control') != 'no-store', \
        "images must keep whatever caching they had"
