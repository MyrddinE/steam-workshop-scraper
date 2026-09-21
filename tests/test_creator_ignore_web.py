"""The web half of the creator-ignore feature.

The control lives in the author-mode bar, near Return but not beside it, and it
acts on the creator the view is pinned to rather than on the selected item. The
route reads and toggles the shared flag in ``src/database`` and answers the
label, so the page holds no copy of the wording or the toggle direction. After a
toggle the view is re-queried, because every item of the creator moved at once.

The route tests are ordinary Flask tests; the page's jump and toggle are run in
node against a fake DOM, the way the author-mode tests in ``test_webserver.py``
are, so the assertions are on the state the page reaches rather than on a button
existing.
"""

import json
import re
import shutil
import subprocess

import pytest
from lxml import html as lxml_html

from src import database
from src.database import (
    get_connection,
    initialize_database,
    insert_or_update_item,
)
from src.webserver import app, init_webserver

NODE = shutil.which("node")


@pytest.fixture
def web_client(tmp_path):
    db_path = str(tmp_path / "test_creator_ignore_web.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "daemon": {"target_appids": [294100]}}
    init_webserver(db_path, config)
    return app.test_client(), db_path


def _row(db_path, workshop_id, columns="*"):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT {columns} FROM workshop_items WHERE workshop_id = ?",
            (workshop_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _served_inline_script(client) -> str:
    doc = lxml_html.fromstring(client.get('/').data.decode())
    return "\n".join(s.text or "" for s in doc.xpath('//script[not(@src)]'))


def _extract_function(script: str, name: str) -> str:
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
    path = tmp_path / "creator_ignore_driver.js"
    path.write_text(driver, encoding="utf-8")
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"node driver failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


# ── the route ────────────────────────────────────────────────────────────────


def test_the_creator_ignore_route_reads_and_toggles_the_shared_flag(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "x", "creator_steamid": 111,
        "fetch_status": 200, "api_fetched_at": 123, "extended_description": "p",
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "title": "y", "creator_steamid": 222, "fetch_status": 200,
    })

    read = client.get('/api/creator/111/ignore')
    assert read.status_code == 200
    assert read.get_json() == {"ignored": False, "label": "Ignore creator"}

    first = client.post('/api/creator/111/ignore').get_json()
    assert first == {"ok": True, "ignored": True, "label": "Un-ignore creator"}
    assert _row(db_path, 1)["fetch_status"] == -2, "the creator's item settles"
    assert _row(db_path, 2)["fetch_status"] == 200, "another creator's does not"

    assert client.get('/api/creator/111/ignore').get_json() == {
        "ignored": True, "label": "Un-ignore creator"}

    second = client.post('/api/creator/111/ignore').get_json()
    assert second == {"ok": True, "ignored": False, "label": "Ignore creator"}
    assert _row(db_path, 1)["fetch_status"] == 200, "the reverse restores it"


def test_the_route_uses_the_shared_wording(web_client):
    """The label is built by `creator_ignore_label`, not retyped in the route."""
    client, _ = web_client

    assert database.creator_ignore_label(False) != database.creator_ignore_label(True)
    assert client.get('/api/creator/7/ignore').get_json()["label"] == \
        database.creator_ignore_label(False)
    assert client.post('/api/creator/7/ignore').get_json()["label"] == \
        database.creator_ignore_label(True)


def test_the_page_carries_the_control_and_the_shared_default_wording(web_client):
    client, _ = web_client
    page = client.get('/').data.decode()

    assert 'id="btn-ignore-creator"' in page, "the author bar must carry the control"
    assert database.creator_ignore_label(False) in page, \
        "the default wording must come from src/database, not a template literal"


# ── the page's toggle, run in node ───────────────────────────────────────────

# A DOM small enough to run the toggle: `getElementById` returns a stub per id,
# and `dataset` records the ignored state the page drew.
CREATOR_TOGGLE_DOM = r"""
function fakeEl(tag) {
  return {
    tagName: String(tag).toUpperCase(), style: {}, dataset: {},
    textContent: '', className: '', disabled: false, listeners: {},
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); },
    click() { (this.listeners.click || []).forEach((fn) => fn()); }
  };
}
const _elements = {
  'author-mode-bar': fakeEl('div'),
  'author-mode-name': fakeEl('span'),
  'filter-rows': fakeEl('div'),
  'filter-buttons': fakeEl('div'),
  'btn-save-filter': fakeEl('button'),
  'btn-ignore-creator': fakeEl('button')
};
global.document = {getElementById: (id) => _elements[id] || null};
"""

CREATOR_TOGGLE_DRIVER = """
__DOM__

let _authorCreator = null;
const _searches = [];
const _requests = [];
function doSearch(reset) { _searches.push(reset); return Promise.resolve(); }
function _viewSnapshot() { return {}; }
function addRow() {}
function _syncSubscribedOverlay() {}
__SETAUTHORMODEUI__
__DRAWCONTROL__
__REFRESHCONTROL__
__TOGGLE__

// The route's answer per method: GET reports not ignored, POST flips on the
// first press and back on the second.
let _postCount = 0;
global.fetch = async (url, options) => {
  const method = (options && options.method) || 'GET';
  _requests.push(method + ' ' + url);
  const body = method === 'POST'
    ? (++_postCount === 1
        ? {ok: true, ignored: true, label: 'Un-ignore creator'}
        : {ok: true, ignored: false, label: 'Ignore creator'})
    : {ignored: false, label: 'Ignore creator'};
  return {ok: true, status: 200, statusText: 'OK', json: async () => body};
};
global.alert = (message) => { throw new Error('unexpected alert: ' + message); };

(async () => {
  // Entering the mode is what names the creator the control acts on.
  _setAuthorModeUi(true, '76561198000000000');
  const creatorAfterEnter = _authorCreator;
  await _refreshCreatorIgnoreControl();
  const read = {
    label: _elements['btn-ignore-creator'].textContent,
    ignored: _elements['btn-ignore-creator'].dataset.ignored
  };

  await toggleCreatorIgnored();
  const first = {
    label: _elements['btn-ignore-creator'].textContent,
    ignored: _elements['btn-ignore-creator'].dataset.ignored,
    searches: _searches.slice()
  };

  await toggleCreatorIgnored();
  const second = {
    label: _elements['btn-ignore-creator'].textContent,
    ignored: _elements['btn-ignore-creator'].dataset.ignored,
    searches: _searches.slice()
  };

  // Leaving the mode clears the creator the toggle acts on.
  _setAuthorModeUi(false, '');
  const creatorAfterReturn = _authorCreator;

  console.log(JSON.stringify({
    creatorAfterEnter: creatorAfterEnter,
    creatorAfterReturn: creatorAfterReturn,
    read: read,
    first: first,
    second: second,
    requests: _requests
  }));
  process.exit(0);
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_page_toggle_acts_on_the_viewed_creator_and_requeries(web_client, tmp_path):
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (CREATOR_TOGGLE_DRIVER
              .replace("__DOM__", CREATOR_TOGGLE_DOM)
              .replace("__SETAUTHORMODEUI__", _extract_function(script, "_setAuthorModeUi"))
              .replace("__DRAWCONTROL__", _extract_function(script, "_drawCreatorIgnoreControl"))
              .replace("__REFRESHCONTROL__", _extract_function(script, "_refreshCreatorIgnoreControl"))
              .replace("__TOGGLE__", _extract_function(script, "toggleCreatorIgnored")))
    out = _run_node(driver, tmp_path)

    assert out["creatorAfterEnter"] == "76561198000000000", \
        "entering the mode must name the creator the control acts on"
    assert out["creatorAfterReturn"] is None, "leaving the mode must clear it"

    assert out["read"] == {"label": "Ignore creator", "ignored": "0"}, \
        "the label and the state come from the read route"

    assert out["first"] == {
        "label": "Un-ignore creator", "ignored": "1", "searches": [True],
    }, "the toggle draws the new direction and re-queries the view once"
    assert out["second"] == {
        "label": "Ignore creator", "ignored": "0", "searches": [True, True],
    }, "the reverse re-queries again"

    assert out["requests"] == [
        "GET /api/creator/76561198000000000/ignore",
        "POST /api/creator/76561198000000000/ignore",
        "POST /api/creator/76561198000000000/ignore",
    ], "the route is the creator's, not an item's"
