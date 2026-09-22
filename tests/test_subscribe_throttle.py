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

The final three use the same node-driven shape to pin the pass's **timers**
(issue 83): a new pass clears the cancelled pass's still-live poll before
arming its own, so the old interval cannot fire again and its completion branch
cannot clear the new pass's handle.
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


# --- a new pass must not inherit the previous pass's poll --------------------
#
# Cancel deliberately leaves the cancelled pass's 1 s poll running while the
# overlay stays open, because that poll is what keeps its rows in step with the
# database. Starting a new pass from the grid cell that still holds focus (`l`)
# used to arm the new pass's poll over that live handle: the old interval kept
# running, and its completion branch cleared `_subPollIv` -- by then the *new*
# pass's handle -- so a stale poll could stop the new pass's polling while
# re-reading the queue on behalf of its own stale `items` list.
#
# The driver below drives a cancelled pass and then a new one, with the timers
# hand-stubbed so an interval fires only when the test says so. That is what
# makes the three properties observable: how many polls are live after the
# re-entry, whether the old handle still fires, and whose handle the stale
# completion branch clears.

OVERLAY_REENTRY_DRIVER = r"""
__ESTIMATOR__
// A plain string escape, not the behaviour under test.
function _escapeHtml(text) { return String(text); }
// The schedule's tick only redraws countdowns; this scenario never fires it.
function _subTickEstimates() {}

let _subPollIv = null;
let _subScheduleIv = null;
let _subCanceled = false;
let _subThrottleStopped = false;
let _subEstimate = null;

// Timers are registered but never fire on their own. `fireInterval` invokes a
// still-registered interval's callback and says whether it ran, which is how
// the test asks whether the page cleared the old poll when the new pass began.
let _timerId = 0;
const _timers = new Map();
global.setInterval = function(fn, ms) {
  const id = ++_timerId;
  _timers.set(id, {fn: fn, ms: ms, kind: 'interval'});
  return id;
};
global.setTimeout = function(fn, ms) {
  const id = ++_timerId;
  _timers.set(id, {fn: fn, ms: ms, kind: 'timeout'});
  return id;
};
global.clearInterval = function(id) { _timers.delete(id); };
global.clearTimeout = function(id) { _timers.delete(id); };
function fireInterval(id) {
  const t = _timers.get(id);
  if (!t || t.kind !== 'interval') return {ran: false, promise: null};
  return {ran: true, promise: t.fn()};
}
function liveIntervals(kind, ms) {
  const out = [];
  _timers.forEach(function(t, id) {
    if (t.kind === kind && t.ms === ms) out.push(id);
  });
  return out;
}

// One fake row per queued item, with the countdown and verb spans the drain and
// the poll read.
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
const rows = [11, 22].map(makeRow);
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
  querySelectorAll: function() { return []; }
};
global.alert = function() {};

// The served click handler, wired onto the stub exactly as the page wires it.
__CANCEL__

// The queue the fake database answers. The stale poll's own tick needs it to
// drain empty -- that is the completion whose branch clears the wrong handle.
let queuedNow = [
  {workshop_id: 11, title: 'A', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'a', subscription_glyph: 'x'},
  {workshop_id: 22, title: 'B', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'b', subscription_glyph: 'x'}
];
let phase = 'pass1';
let releasePass1Call = null;
let releasePass1Started = null;
const pass1CallStarted = new Promise(function(resolve) { releasePass1Started = resolve; });
let releasePass2Started = null;
const pass2CallStarted = new Promise(function(resolve) { releasePass2Started = resolve; });

function okJson(body) {
  return {ok: true, status: 200, statusText: 'OK', json: async function() { return body; }};
}
global.fetch = async function(url, opts) {
  const u = String(url);
  if (u === '/api/queued') return okJson(queuedNow.slice());
  if (u === '/api/subscribe_pace') return okJson({seed_seconds: 1});
  if (u === '/api/subscribe_failures') return okJson([]);
  if (u === '/api/subscribe_throttle') return okJson({throttled_at: 0, retry_after: 300});
  if (u === '/api/pause' || u === '/api/resume') return okJson({ok: true});
  if (u.indexOf('/api/subscribe/') === 0) {
    if (phase === 'pass1') {
      // Hold pass one mid-call so Cancel lands on a live schedule and leaves its
      // poll alive behind it.
      releasePass1Started();
      await new Promise(function(resolve) { releasePass1Call = resolve; });
    } else {
      releasePass2Started();
    }
    return {ok: true, status: 200, statusText: 'OK',
            json: async function() { return {success: 1}; }};
  }
  return okJson({ok: true});
};

const runPass = (__START__);
const cancelButton = elements['sub-cancel'];

(async function() {
  // Pass one: start it, wait until the first subscribe call is in flight, then
  // Cancel. That clears the schedule and deliberately leaves the poll running.
  const pass1 = runPass();
  await pass1CallStarted;
  const pollA = _subPollIv;
  cancelButton.onclick();
  releasePass1Call();
  await pass1;
  const livePollsAfterCancel = liveIntervals('interval', 1000);

  // Re-enter the overlay while that cancelled poll is still live.
  phase = 'pass2';
  const pass2 = runPass();
  await pass2CallStarted;
  const pollB = _subPollIv;
  const livePollsAfterReentry = liveIntervals('interval', 1000);

  // Now let the *stale* poll tick, as the browser would have had the new pass
  // not cleared its handle. Its own items are gone from the queue, so its tick
  // reaches the completion branch that clears the module-scope handle.
  queuedNow = [];
  const stale = fireInterval(pollA);
  if (stale.promise && typeof stale.promise.then === 'function') await stale.promise;
  const pollAfterStaleFire = _subPollIv;
  const livePollsAfterStaleFire = liveIntervals('interval', 1000);

  await pass2;

  console.log(JSON.stringify({
    poll_a: pollA,
    live_polls_after_cancel: livePollsAfterCancel,
    new_poll: pollB,
    live_polls_after_reentry: livePollsAfterReentry,
    old_poll_fired: stale.ran,
    poll_after_stale_fire: pollAfterStaleFire,
    live_polls_after_stale_fire: livePollsAfterStaleFire
  }));
})();
"""


def _overlay_reentry_scenario(client, tmp_path):
    script = _served_inline_script(client)
    helpers = "\n".join(
        _extract_function(script, name)
        for name in ("_subItemSeconds", "_subBatchRemainingSeconds", "_subFormatDuration",
                     "_subElapsedCurrent", "_subSeedSeconds", "_subRenderProgress",
                     "_clearSubEstimates"))
    driver = (OVERLAY_REENTRY_DRIVER
              .replace("__ESTIMATOR__", helpers)
              .replace("__START__", _extract_function(script, "_startAutoSubscribe"))
              .replace("__CANCEL__", _extract_cancel_onclick(script)))
    return _run_node(driver, tmp_path)


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_re_entered_pass_leaves_exactly_one_live_poll(web_client, tmp_path):
    """Cancel keeps its pass's poll; a new pass must clear it, not inherit it.

    Against the pre-change page the cancelled poll's handle is still registered
    when the new pass arms its own, so two 1 s polls are live at once.
    """
    client, _ = web_client
    out = _overlay_reentry_scenario(client, tmp_path)

    assert out["live_polls_after_cancel"] == [out["poll_a"]], (
        "Cancel's deliberate behaviour: its poll stays live while the overlay is "
        f"open; got {out['live_polls_after_cancel']}")
    assert out["live_polls_after_reentry"] == [out["new_poll"]], (
        "a new pass must clear the cancelled pass's live poll before arming its "
        f"own; got {out['live_polls_after_reentry']} "
        f"(old {out['poll_a']}, new {out['new_poll']})")


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_cleared_poll_does_not_fire_again(web_client, tmp_path):
    """The handle the new pass cleared must be gone, not merely overwritten.

    The stale poll re-reads the queue for its own old `items` list, so a browser
    still delivering its ticks would redraw the new pass's rows from the old
    pass's bookkeeping.
    """
    client, _ = web_client
    out = _overlay_reentry_scenario(client, tmp_path)

    assert out["old_poll_fired"] is False, (
        "the cancelled pass's interval must be cleared at the new pass's start, "
        "not left to fire behind it")


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_new_polls_handle_survives_the_stale_completion(web_client, tmp_path):
    """The defect: the stale closure's `clearInterval(_subPollIv)` hits the new handle.

    The poll's completion branch clears the module-scope handle rather than its
    own, so a predecessor the browser still delivers stops the *new* pass's
    polling. Clearing both handles at pass start is what makes that unreachable.
    """
    client, _ = web_client
    out = _overlay_reentry_scenario(client, tmp_path)

    assert out["poll_after_stale_fire"] == out["new_poll"], (
        "the new pass's poll handle must survive the predecessor's completion; "
        f"got {out['poll_after_stale_fire']!r} for new {out['new_poll']!r}")
    assert out["live_polls_after_stale_fire"] == [out["new_poll"]], (
        "the new pass's poll must still be the one live interval after the "
        f"stale completion; got {out['live_polls_after_stale_fire']}")
