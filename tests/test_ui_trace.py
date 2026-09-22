"""The UI trace: what the page did, and what it called.

The page's JavaScript is only observable through tests that drive extracted
functions, and the agent cannot open a browser. This is the instrument that
turns "here is what I infer from the source" into "here is what the page did":
an ordered timeline of actions and the calls they caused, written by the page
to ``<outbox_dir>/web_ui_trace/`` while ``daemon.capture_web_ui_trace`` is on.

Three properties carry the weight here:

* the route is **inert when the switch is off** — no file, no manifest entry —
  and one accepted batch is one atomically written, manifest-registered record;
* the tree is **bounded**: events per batch (oldest dropped first), bytes per
  record, and files per page session, with the truncation recorded rather than
  silently swallowed;
* the trace is **additive**: a trace POST that fails, throws or is refused
  leaves the action it describes behaving exactly as it did before.

The page half runs under node against the served inline script, the same way
``tests/test_webserver.py`` drives the infinite-scroll and author-mode code.
"""

import json
import re

import lxml.html
import pytest

from src import capture
from src.database import initialize_database
from src.webserver import app, init_webserver
from tests.test_webserver import (
    NODE,
    _extract_function,
    _run_node,
    _served_inline_script,
)

# The documented caps, pinned here so a test fails on the value rather than
# borrowing it from the code it checks.
EVENTS_PER_BATCH = 200
BYTES_PER_RECORD = 256 * 1024
FILES_PER_SESSION = 200


@pytest.fixture(autouse=True)
def _reset_capture():
    yield
    capture.configure(None)


def _server(tmp_path, daemon_config, **sections):
    db_path = str(tmp_path / "trace.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "daemon": daemon_config}
    config.update(sections)
    init_webserver(db_path, config)
    return app.test_client(), db_path, config


def _trace_files(outbox):
    directory = outbox / "web_ui_trace"
    return sorted(directory.glob("*.json")) if directory.exists() else []


def _manifest(outbox):
    path = outbox / "manifest.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["artifacts"]


# ── the switch and the sink ───────────────────────────────────────────────────

def test_the_documented_caps_are_the_code_constants():
    assert capture.UI_TRACE_MAX_EVENTS_PER_BATCH == EVENTS_PER_BATCH
    assert capture.UI_TRACE_MAX_RECORD_BYTES == BYTES_PER_RECORD
    assert capture.UI_TRACE_MAX_FILES_PER_SESSION == FILES_PER_SESSION
    assert capture.WEB_UI_TRACE_DIR_NAME == "web_ui_trace"


def test_the_route_is_inert_when_the_switch_is_off(tmp_path):
    """A trace the operator did not ask for must be impossible to write."""
    outbox = tmp_path / "outbox"
    client, _, _ = _server(tmp_path, {"outbox_dir": str(outbox)})

    resp = client.post('/api/ui_trace',
                       json={"session": "s", "events": [{"event": "click"}]})

    assert resp.status_code == 404
    assert "not enabled" in (resp.get_json() or {}).get("error", "")
    assert _trace_files(outbox) == []
    assert _manifest(outbox) == []


def test_the_route_writes_a_manifest_registered_record(tmp_path):
    outbox = tmp_path / "outbox"
    client, _, _ = _server(tmp_path,
                           {"outbox_dir": str(outbox), "capture_web_ui_trace": True})

    resp = client.post('/api/ui_trace', json={
        "session": "page-1", "seq": 0, "events": [
            {"t": 0, "event": "session", "inner_width": 1200, "inner_height": 800},
            {"t": 5, "event": "keydown", "key": "s", "consumed": True},
        ]})

    assert resp.status_code == 200
    files = _trace_files(outbox)
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["kind"] == "ui_trace"
    assert record["session"] == "page-1"
    assert [event["event"] for event in record["events"]] == ["session", "keydown"]
    entry = next(a for a in _manifest(outbox) if a["kind"] == "ui_trace")
    assert entry["path"] == f"web_ui_trace/{files[0].name}"
    assert entry["role"] == "batch"


def test_the_page_is_told_the_switch_and_the_bounds(tmp_path):
    outbox = tmp_path / "outbox"
    client, _, _ = _server(tmp_path,
                           {"outbox_dir": str(outbox), "capture_web_ui_trace": True})

    html = client.get('/').data.decode()

    assert "const UI_TRACE_ENABLED = true;" in html
    match = re.search(r'const UI_TRACE_LIMITS = (\{.*?\});', html)
    assert match, "the page must be told the server's bounds"
    assert json.loads(match.group(1)) == capture.ui_trace_limits()
    assert capture.app_version() in html


# ── bounds ────────────────────────────────────────────────────────────────────

def test_a_batch_over_the_event_cap_drops_the_oldest_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "UI_TRACE_MAX_EVENTS_PER_BATCH", 3)
    outbox = tmp_path / "outbox"
    client, _, _ = _server(tmp_path,
                           {"outbox_dir": str(outbox), "capture_web_ui_trace": True})

    events = [{"t": i, "event": "scroll", "n": i} for i in range(5)]
    resp = client.post('/api/ui_trace', json={"session": "s", "events": events})

    assert resp.status_code == 200
    record = json.loads(_trace_files(outbox)[0].read_text(encoding="utf-8"))
    assert record["events_truncated"] is True
    assert record["events_dropped"] == 2
    assert [event["n"] for event in record["events"]] == [2, 3, 4], \
        "the cap drops the oldest, keeping the newest"


def test_a_record_over_the_byte_cap_is_trimmed_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "UI_TRACE_MAX_RECORD_BYTES", 1500)
    outbox = tmp_path / "outbox"
    client, _, _ = _server(tmp_path,
                           {"outbox_dir": str(outbox), "capture_web_ui_trace": True})

    events = [{"t": i, "event": "click", "label": "x" * 200} for i in range(20)]
    resp = client.post('/api/ui_trace', json={"session": "s", "events": events})

    assert resp.status_code == 200
    path = _trace_files(outbox)[0]
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["bytes_truncated"] is True
    assert len(path.read_bytes()) <= 1500, "the written record honours the byte cap"
    assert len(record["events"]) < 20


def test_the_session_file_cap_stops_the_page_and_the_server(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "UI_TRACE_MAX_FILES_PER_SESSION", 2)
    outbox = tmp_path / "outbox"
    client, _, _ = _server(tmp_path,
                           {"outbox_dir": str(outbox), "capture_web_ui_trace": True})

    for seq in range(2):
        resp = client.post('/api/ui_trace', json={
            "session": "s", "seq": seq, "events": [{"event": "click"}]})
        assert resp.status_code == 200

    # The batch that crosses the cap is written as the truncation marker; the
    # response tells the page to stop buffering.
    resp = client.post('/api/ui_trace', json={
        "session": "s", "seq": 2, "final": True, "reason": "session_file_cap",
        "events": [{"event": "click"}]})
    assert resp.status_code == 429
    assert resp.get_json()["refused"] == "session_cap"

    files = _trace_files(outbox)
    assert len(files) == 3, "two event records and one truncation marker"
    marker = json.loads(files[-1].read_text(encoding="utf-8"))
    assert marker["truncated"] is True
    assert marker["truncation_reason"] == "session_file_cap"

    # And the server keeps refusing: nothing more is written for the session.
    again = client.post('/api/ui_trace', json={
        "session": "s", "seq": 3, "events": [{"event": "click"}]})
    assert again.status_code == 429
    assert len(_trace_files(outbox)) == 3


# ── scrubbing ─────────────────────────────────────────────────────────────────

def test_a_credential_value_in_a_trace_is_scrubbed(tmp_path):
    """The page holds no cookie, but elision must not depend on that."""
    secret = "STEAM-LOGIN-SECRET-0123456789"
    outbox = tmp_path / "outbox"
    client, _, _ = _server(
        tmp_path,
        {"outbox_dir": str(outbox), "capture_web_ui_trace": True},
        session={"login_secure": secret})

    resp = client.post('/api/ui_trace', json={
        "session": "s",
        "events": [{"event": "click", "label": secret}]})

    assert resp.status_code == 200
    text = _trace_files(outbox)[0].read_text(encoding="utf-8")
    assert secret not in text
    assert capture.REDACTED in text


# ── the page, driven under node ───────────────────────────────────────────────
#
# The driver installs the stubs the page's own module scope would provide, then
# runs the served `_startUiTraceIfEnabled` — the one guard — and exercises the
# page's real wrapper code. Nothing test-only is exposed on the page: the batch
# is read back from the POST the unload flush already makes.

UI_TRACE_DRIVER = r"""
// ── the stub environment ────────────────────────────────────────────────────
let _clock = 1000;
const _windowListeners = {};
const _documentListeners = {};
const _elements = {};
function _fakeEl(id) {
  return {
    id: id, clientHeight: 700, scrollTop: 0, scrollHeight: 4000, value: '',
    textContent: '', listeners: {},
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); },
    getAttribute() { return null; },
    closest() { return null; },
  };
}
['results-grid', 'sort-by', 'sort-order', 'subscribed-overlay'].forEach(function(id) {
  _elements[id] = _fakeEl(id);
});
globalThis.window = globalThis;
globalThis.innerWidth = 1200;
globalThis.innerHeight = 800;
globalThis.location = {href: 'http://localhost/'};
globalThis.performance = {now: function() { return _clock; }};
globalThis.addEventListener = function(name, fn) {
  (_windowListeners[name] = _windowListeners[name] || []).push(fn);
};
globalThis.document = {
  getElementById: function(id) { return _elements[id] || null; },
  addEventListener: function(name, fn) {
    (_documentListeners[name] = _documentListeners[name] || []).push(fn);
  },
};

// ── the page globals the install wraps ──────────────────────────────────────
let currentOffset = 0, hasMore = true, loading = false, _observedCell = null;
let _listPollTimer = null;
function getFilters() { return []; }
function _overlayValue() { return 'any'; }
function doSearch(reset) {
  currentOffset += 3;
  return fetch('/api/search', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({filters: [], subscribed: 'any', sort_by: 'title',
                          sort_order: 'ASC', offset: 0, limit: 50}),
  }).then(function() {});
}
function _observeNextBatch(firstCell) {}
function _onScrollIntersection(entries) {}
function _listPollTick() {}
function _itemUpdateTick() {}
function _startListPoll(filters, sortBy, sortOrder) {}
function jumpToAuthor(creator) {}
function showDetail(wid) { return Promise.resolve(); }
function _subscribedItemIds() { return [1, 2]; }
function _loadViewState() { return __SAVED_VIEW__; }
async function _loadUntil(done, maxBatches) {
  let batches = 0;
  while (hasMore && !done() && batches < maxBatches) {
    const before = currentOffset;
    await doSearch(false);
    if (currentOffset === before) break;
    batches += 1;
  }
}
let _restoreChecks = 0;
async function _restoreView(state) {
  _restoreChecks = 0;
  await _loadUntil(function() { _restoreChecks += 1; return _restoreChecks >= 3; }, 5);
}
async function loadState() {
  const local = _loadViewState();
  if (local) await _restoreView(local);
}

// ── the served trace code ───────────────────────────────────────────────────
const UI_TRACE_ENABLED = __ENABLED__;
const UI_TRACE_LIMITS = __LIMITS__;
const BUILD_VERSION = __VERSION__;
const _installUiTrace = (__INSTALL__);
const _startUiTraceIfEnabled = (__START__);

// ── the fetch stub, in place before the trace captures the original ─────────
const _fetchCalls = [];
const _stubFetch = function(url, init) {
  _fetchCalls.push({url: url, init: init});
  __TRACE_FAIL__
  return Promise.resolve({ok: true, status: 200});
};
globalThis.fetch = _stubFetch;

(async function() {
  _startUiTraceIfEnabled();
  const wrapped = globalThis.fetch !== _stubFetch;

  let batch = null;
  let tracePosts = 0;
  if (wrapped) {
    __ACTION__
    await null;
    // The unload flush is the production path; there is no test-only hook.
    _windowListeners.pagehide[0]();
    await null;
    const posts = _fetchCalls.filter(function(c) { return c.url === '/api/ui_trace'; });
    tracePosts = posts.length;
    if (posts.length) batch = JSON.parse(posts[posts.length - 1].init.body);
  }
  console.log(JSON.stringify({
    fetchIsWrapped: wrapped,
    tracePosts: tracePosts,
    batch: batch,
    calls: _fetchCalls.map(function(c) { return c.url; }),
  }));
})();
"""

# A keydown and the call the page's own handler makes for it.
_TRACE_ACTION = """
    _documentListeners.keydown[0]({
      key: 's', defaultPrevented: true,
      target: {textContent: 'cell 42', closest: function() { return null; },
               getAttribute: function() { return '42'; }}});
    await doSearch(true);
"""

# The view restore the page runs on every load, with no user action at all.
_TRACE_RESTORE = """
    await loadState();
"""


def _trace_driver(script, *, enabled, trace_fails=False, action=None, saved_view="null"):
    limits = json.dumps(capture.ui_trace_limits())
    return (UI_TRACE_DRIVER
            .replace("__ENABLED__", "true" if enabled else "false")
            .replace("__LIMITS__", limits)
            .replace("__VERSION__", json.dumps(capture.app_version()))
            .replace("__SAVED_VIEW__", saved_view)
            .replace("__ACTION__", _TRACE_ACTION if action is None else action)
            .replace("__TRACE_FAIL__",
                     "if (url === '/api/ui_trace') return Promise.reject(new Error('refused'));"
                     if trace_fails else "")
            .replace("__INSTALL__", _extract_function(script, "_installUiTrace"))
            .replace("__START__", _extract_function(script, "_startUiTraceIfEnabled")))


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_tracing_off_installs_no_wrapper_and_buffers_nothing(tmp_path):
    client, _, _ = _server(tmp_path, {"outbox_dir": str(tmp_path / "outbox")})
    script = _served_inline_script(client)
    assert "const UI_TRACE_ENABLED = false;" in script

    out = _run_node(_trace_driver(script, enabled=False), tmp_path)

    assert out["fetchIsWrapped"] is False, "no fetch wrapper may be installed"
    assert out["tracePosts"] == 0, "nothing may be buffered or posted"
    assert out["calls"] == [], "the stub fetch was never called at all"


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_keydown_and_the_call_it_causes_appear_in_order(tmp_path):
    outbox = tmp_path / "outbox"
    client, _, _ = _server(tmp_path,
                           {"outbox_dir": str(outbox), "capture_web_ui_trace": True})
    script = _served_inline_script(client)
    assert "const UI_TRACE_ENABLED = true;" in script

    out = _run_node(_trace_driver(script, enabled=True), tmp_path)

    assert out["fetchIsWrapped"] is True
    assert out["tracePosts"] == 1
    events = out["batch"]["events"]
    assert [event["event"] for event in events] == [
        "session", "keydown", "do_search", "fetch", "do_search"], \
        "actions and the calls they caused must be one ordered timeline"

    session = events[0]
    assert session["inner_width"] == 1200
    assert session["inner_height"] == 800
    assert session["grid_client_height"] == 700
    assert session["grid_scroll_top"] == 0
    assert session["app_version"] == capture.app_version()

    keydown = events[1]
    assert keydown["key"] == "s"
    assert keydown["wid"] == "42"
    assert keydown["consumed"] is True
    assert isinstance(keydown["t"], (int, float))
    assert keydown["loads_since_scroll"] == 0

    call = events[3]
    assert call["method"] == "POST"
    assert call["path"] == "/api/search"
    assert call["body"]["kind"] == "search"
    assert call["body"]["filters"] == 0
    assert call["status"] == 200


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_view_restore_is_traced_with_its_done_values_and_batches(tmp_path):
    """The loop that runs on every load, with no user action, is visible.

    `loadState` -> `_restoreView` -> `_loadUntil` is the confirmed suspect for
    the "it is scrolling on its own" report: up to two passes of 40 searches
    before anyone touches the page. A trace has to show the restore ran, whether
    a saved view was found, each pass's `done` value and every batch it asked
    for -- otherwise those searches arrive with no visible cause.
    """
    outbox = tmp_path / "outbox"
    client, _, _ = _server(tmp_path,
                           {"outbox_dir": str(outbox), "capture_web_ui_trace": True})
    script = _served_inline_script(client)

    out = _run_node(_trace_driver(
        script, enabled=True, action=_TRACE_RESTORE,
        saved_view="{scroll: 6000, selected: null}"), tmp_path)

    events = out["batch"]["events"]
    assert events[0]["event"] == "session"

    saved = next(e for e in events if e.get("phase") == "saved_view")
    assert saved["found"] is True

    enter = next(e for e in events if e.get("phase") == "enter")
    assert enter["has_state"] is True
    assert enter["scroll"] == 6000

    checks = [e for e in events if e.get("phase") == "done_check"]
    assert [e["value"] for e in checks] == [False, False, True], \
        "each pass records the value its done predicate returned"

    searches = [e for e in events
                if e["event"] == "do_search" and e.get("phase") == "enter"]
    assert searches, "the restore's batches must appear in the timeline"
    assert all(e["reset"] is False for e in searches)
    assert out["calls"].count("/api/search") == len(searches), \
        "every batch the loop requested is a recorded call"

    done = next(e for e in events if e.get("phase") == "load_until_done")
    assert done["passes"] == 3


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_failed_trace_post_leaves_the_action_unchanged(tmp_path):
    """Capture is additive: the traced call still happens and still returns."""
    outbox = tmp_path / "outbox"
    client, _, _ = _server(tmp_path,
                           {"outbox_dir": str(outbox), "capture_web_ui_trace": True})
    script = _served_inline_script(client)

    out = _run_node(_trace_driver(script, enabled=True, trace_fails=True), tmp_path)

    assert out["tracePosts"] == 1, "the trace POST was attempted and rejected"
    assert "/api/search" in out["calls"], \
        "a failing trace POST must not stop the call it was describing"
    assert [event["event"] for event in out["batch"]["events"]][3] == "fetch", \
        "the traced call is still recorded in the batch that failed to send"

