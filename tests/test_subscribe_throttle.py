"""The drain's throttle check, and the pause it must release.

Steam answers an over-budget request with HTTP 200 and its ordinary page shell,
so the subscribe button is simply absent. The userscript bridge used to report
that to the server through `POST /api/subscribe_throttled/<id>`; the report and
the userscript went with the bridge. What remains is the browser-free drain's own
behaviour: it reads `GET /api/subscribe_throttle` before each item, stops the
pass when a throttle is recorded, and must release the daemon pause it took for
the pass on that early stop.

The throttle stop also sets `_subThrottleStopped`, which the Close handler reads
to decide whether to dequeue the rows the pass never verified. That is **per-pass**
state: it is cleared at the start of every pass beside `_subCanceled`, so a pass
that stopped for throttling an hour ago cannot change what a later pass's Cancel
does. The last two tests drive the real `_startAutoSubscribe` and the real
`sub-cancel` handler under node -- the overlay's flow is fetch calls, so `fetch`
is the only thing stubbed -- to pin both the reset and the throttled pass's own
keep-the-rows behaviour.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.database import initialize_database
from src.webserver import app, init_webserver

TEMPLATE = Path("templates/index.html")
NODE = shutil.which("node")


@pytest.fixture
def web_client(tmp_path):
    db_path = str(tmp_path / "test_web.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "daemon": {"target_appids": [294100]}}
    init_webserver(db_path, config)
    return app.test_client(), db_path


def _throttle_body():
    """The source of `_checkSubThrottle`, brace-matched.

    It is nested inside `_startAutoSubscribe`, so the end cannot be found by
    indentation: an early return inside the function is also a line at the
    function body's own indent level, and a slice to the first such line stops
    before the release this test exists to pin.
    """
    html = Path("templates/index.html").read_text(encoding="utf-8")
    start = html.index("async function _checkSubThrottle()")
    brace = html.index("{", start)
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    raise AssertionError("unterminated _checkSubThrottle")


def test_a_throttle_stop_releases_the_daemon_pause():
    """Otherwise the daemon stays paused for good.

    The resume otherwise happens only when every item was verified, and stopping
    the pass early means the remaining ones never are.
    """
    body = _throttle_body()
    assert "fetch('/api/subscribe_throttle')" in body, \
        "the kept read is what the drain checks"
    assert "_subThrottleStopped = true" in body
    assert "await fetch('/api/resume'" in body, "the early stop must release the pause"
    assert "clearInterval(_subPollIv)" in body, "and stop the poll that would have resumed it"


def test_a_throttle_stop_leaves_the_queue_intact():
    """The retry depends on the items surviving. The Close path dequeues them."""
    html = Path("templates/index.html").read_text(encoding="utf-8")
    close = html[html.index("sub-cancel').onclick"):]
    close = close[:close.index("sub-clear-failed")]
    assert "if (!_subThrottleStopped)" in close, "Close must not dequeue a throttled pass"
    assert "fetch('/api/resume'" in close


def test_the_drain_pauses_the_daemon_for_its_duration():
    """The control this all depends on: a subscribe pass owns the daemon."""
    html = Path("templates/index.html").read_text(encoding="utf-8")
    assert "await fetch('/api/pause'" in html
    src = Path("src/web_worker.py").read_text(encoding="utf-8")
    assert "os.path.exists(self.pause_lock_file)" in src
    img = Path("src/image_worker.py").read_text(encoding="utf-8")
    assert "os.path.exists(self.pause_lock_file)" in img


# --- the throttle flag is per-pass ------------------------------------------
#
# `_subThrottleStopped` is read by the Close handler to decide whether to
# dequeue the rows the pass never verified. The tests below run the served
# `_startAutoSubscribe` and the served `sub-cancel` handler under node, with only
# `fetch` (and the DOM and the timers) hand-stubbed: the drain is a sequence of
# fetch calls, so that is enough to drive both passes for real. Pass one stops
# for throttling; pass two is a normal pass the user cancels mid-flight, and its
# start must have cleared the flag so Close dequeues.

def _served_inline_script(client) -> str:
    import lxml.html
    doc = lxml.html.fromstring(client.get('/').data.decode())
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


def _extract_cancel_onclick(script: str) -> str:
    """The served `sub-cancel` click handler, as its assignment statement."""
    start = script.index("document.getElementById('sub-cancel').onclick")
    brace = script.index('{', start)
    depth = 0
    for i in range(brace, len(script)):
        if script[i] == '{':
            depth += 1
        elif script[i] == '}':
            depth -= 1
            if depth == 0:
                return script[start:i + 1] + ';'
    raise AssertionError("unterminated sub-cancel onclick")


def _run_node(driver: str, tmp_path):
    path = tmp_path / "driver.js"
    path.write_text(driver, encoding="utf-8")
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"node driver failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


# The overlay's state is declared module scope by the page; the driver declares
# the same five names so the extracted functions close over them. The timers are
# stubbed to handles that never fire, because this driver drives the pass itself
# and a 250 ms tick or a 1 s poll running behind it would only add noise.
OVERLAY_CANCEL_DEQUEUE_DRIVER = r"""
__ESTIMATOR__
// A plain string escape, not the behaviour under test.
function _escapeHtml(text) { return String(text); }

let _subPollIv = null;
let _subScheduleIv = null;
let _subCanceled = false;
let _subThrottleStopped = false;
let _subEstimate = null;

let _timerId = 0;
global.setInterval = function() { return ++_timerId; };
global.clearInterval = function() {};
global.setTimeout = function() { return ++_timerId; };

// One fake row per queued item, with the class list the Close dequeue reads.
function makeRow(wid) {
  const classes = ['sub-queue-item'];
  const countdown = {textContent: ''};
  const verb = {textContent: ''};
  return {
    dataset: {wid: String(wid)}, style: {}, textContent: '',
    classList: {
      add: function(name) { if (classes.indexOf(name) === -1) classes.push(name); },
      contains: function(name) { return classes.indexOf(name) !== -1; }
    },
    getAttribute: function(k) { return k === 'data-wid' ? String(wid) : null; },
    querySelector: function(sel) {
      if (sel === '.countdown') return countdown;
      if (sel === '.sub-queue-verb') return verb;
      return null;
    }
  };
}

const queueItems = [
  {workshop_id: 11, title: 'A', subscription_colour: '#0f0',
   subscription_tooltip: 'a', subscription_glyph: 'x'},
  {workshop_id: 22, title: 'B', subscription_colour: '#0f0',
   subscription_tooltip: 'b', subscription_glyph: 'x'}
];
const rows = queueItems.map(function(it) { return makeRow(it.workshop_id); });

const list = {
  innerHTML: '',
  querySelector: function(sel) {
    const m = sel.match(/\[data-wid="(\d+)"\]/);
    if (!m) return null;
    for (let i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute('data-wid') === m[1]) return rows[i];
    }
    return null;
  },
  querySelectorAll: function(sel) {
    if (sel === '.sub-queue-item') return rows.slice();
    if (sel === '.sub-queue-item .countdown') {
      return rows.map(function(r) { return r.querySelector('.countdown'); });
    }
    return [];
  }
};
const elements = {
  'sub-queue-modal': {style: {}},
  'sub-queue-list': list,
  'sub-progress': {textContent: ''},
  'sub-cancel': {textContent: '', onclick: null},
  'sub-clear-failed': {textContent: '', style: {}}
};
global.document = {
  getElementById: function(id) { return elements[id] || null; },
  querySelectorAll: function(sel) {
    if (sel === '.sub-queue-item:not(.done)') {
      return rows.filter(function(r) { return !r.classList.contains('done'); });
    }
    return [];
  }
};

// The served click handler, wired onto the stub exactly as the page wires it.
__CANCEL__

const dequeues = [];
let phase = 'throttled';
let releaseSubscribe = null;
let releaseStarted = null;
const subscribeStarted = new Promise(function(resolve) { releaseStarted = resolve; });

function okJson(body) {
  return {ok: true, status: 200, statusText: 'OK', json: async function() { return body; }};
}
global.fetch = async function(url, opts) {
  const u = String(url);
  if (u === '/api/queued') return okJson(queueItems);
  if (u === '/api/subscribe_pace') return okJson({web_delay_seconds: 1, seed_seconds: 1});
  if (u === '/api/subscribe_throttle') {
    if (phase === 'throttled') {
      return okJson({throttled_at: Date.now() / 1000, retry_after: 300});
    }
    return okJson({throttled_at: 0, retry_after: 300});
  }
  if (u.indexOf('/api/subscribe/') === 0) {
    if (phase === 'normal') {
      // Hold the pass mid-call so the first Cancel click lands on a live pass.
      releaseStarted();
      await new Promise(function(resolve) { releaseSubscribe = resolve; });
    }
    return {ok: true, status: 200, statusText: 'OK',
            json: async function() { return {success: 1}; }};
  }
  if (u.indexOf('/api/dequeue/') === 0) {
    dequeues.push(u.slice('/api/dequeue/'.length));
  }
  return okJson({ok: true});
};

const runPass = (__START__);
const cancelButton = elements['sub-cancel'];

(async function() {
  // Pass one: the throttle stop fires after the first item.
  await runPass();
  const afterThrottled = _subThrottleStopped;
  // Close the throttled pass. The rows it never verified must stay queued.
  cancelButton.onclick();
  const afterThrottledClose = dequeues.slice();

  // Pass two: a normal pass the user cancels mid-flight. Its start must clear
  // the previous pass's throttle flag, so Close dequeues what it never verified.
  phase = 'normal';
  const pass = runPass();
  await subscribeStarted;
  cancelButton.onclick();
  releaseSubscribe();
  await pass;
  cancelButton.onclick();

  console.log(JSON.stringify({
    afterThrottled: afterThrottled,
    afterThrottledClose: afterThrottledClose,
    afterSecondPassClose: dequeues
  }));
})();
"""


def _overlay_cancel_scenario(client, tmp_path):
    script = _served_inline_script(client)
    helpers = "\n".join(
        _extract_function(script, name)
        for name in ("_subItemSeconds", "_subBatchRemainingSeconds", "_subFormatDuration",
                     "_subElapsedCurrent", "_subSeedSeconds", "_subRenderProgress",
                     "_clearSubEstimates"))
    driver = (OVERLAY_CANCEL_DEQUEUE_DRIVER
              .replace("__ESTIMATOR__", helpers)
              .replace("__START__", _extract_function(script, "_startAutoSubscribe"))
              .replace("__CANCEL__", _extract_cancel_onclick(script)))
    return _run_node(driver, tmp_path)


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_new_pass_resets_the_throttle_stop_so_cancel_dequeues_again(web_client, tmp_path):
    """The regression: a throttled pass must not disable every later Cancel.

    Pass one stops for throttling and leaves the rows queued (correct). A second,
    normal pass starts and the user cancels it mid-flight; its Close must dequeue
    the row that pass never verified. Against the pre-change page the flag is
    still `true` from pass one, so the dequeue is skipped and only `22` -- the
    unverified row -- is missing from the calls.
    """
    client, _ = web_client
    out = _overlay_cancel_scenario(client, tmp_path)

    assert out["afterThrottled"] is True, "pass one must record its throttle stop"
    assert out["afterSecondPassClose"] == ["22"], (
        "a pass started after a throttled one must clear the flag, so its Cancel "
        f"dequeues the row it never verified; got {out['afterSecondPassClose']}")


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_throttled_pass_still_keeps_its_rows_on_cancel(web_client, tmp_path):
    """The converse: the fix must not dequeue the throttled pass's own tail.

    The throttle stop leaves the remaining rows queued for a later drain, so the
    Close reached from that pass must send no `/api/dequeue/<id>` at all.
    """
    client, _ = web_client
    out = _overlay_cancel_scenario(client, tmp_path)

    assert out["afterThrottled"] is True
    assert out["afterThrottledClose"] == [], (
        "a throttled pass's own Close must leave every row queued; got "
        f"{out['afterThrottledClose']}")
