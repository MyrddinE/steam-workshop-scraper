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
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                            encoding="utf-8")
    assert result.returncode == 0, f"node driver failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


# The overlay's state is declared module scope by the page; the driver declares
# the same names so the extracted functions close over them. The timers are
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
let _subPassToken = 0;
let _subPassLive = false;
let _subPageId = 'test-page';

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
let _subPassToken = 0;
let _subPassLive = false;
let _subPageId = 'test-page';

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


# --- a still-draining pass must not clear a newer pass's handles -------------
#
# Issue 86. Two passes overlap: the second is started from the `l` shortcut
# while the first is still draining, so the first reaches its per-item throttle
# check and its `finally` *after* the second has armed its own poll and
# schedule. Those two exit paths cleared the module-scope `_subPollIv` and
# `_subScheduleIv` and dropped `_subEstimate` -- by then the second pass's --
# so a stale pass stopped the new pass's verification poll and its estimate.
#
# The driver holds the first pass inside its first `/api/subscribe` call, starts
# the second, then lets the first finish. The first pass is throttled on its
# one throttle check, so both of its stale clears run. What is asked afterwards
# is whether the second pass's poll, schedule and estimate survived, whether the
# schedule tick still draws, and whether the stale pass released a daemon pause
# the second pass owns.

OVERLAY_STALE_PASS_DRIVER = r"""
__ESTIMATOR__
// A plain string escape, not the behaviour under test.
function _escapeHtml(text) { return String(text); }

let _subPollIv = null;
let _subScheduleIv = null;
let _subCanceled = false;
let _subThrottleStopped = false;
let _subEstimate = null;
let _subPassToken = 0;
let _subPassLive = false;
let _subPageId = 'test-page';

// Timers are registered but never fire on their own; the test fires the
// schedule by hand, which is what makes "the estimate still ticks" observable.
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
function liveIntervals(ms) {
  const out = [];
  _timers.forEach(function(t, id) {
    if (t.kind === 'interval' && t.ms === ms) out.push(id);
  });
  return out;
}

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
  querySelectorAll: function(sel) {
    if (sel === '.sub-queue-item:not(.done)') {
      return rows.filter(function(r) { return !r.classList.contains('done'); });
    }
    return [];
  }
};
global.alert = function() {};

// The served click handler, wired onto the stub exactly as the page wires it.
__CANCEL__

const queuedNow = [
  {workshop_id: 11, title: 'A', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'a', subscription_glyph: 'x'},
  {workshop_id: 22, title: 'B', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'b', subscription_glyph: 'x'}
];

function okJson(body) {
  return {ok: true, status: 200, statusText: 'OK', json: async function() { return body; }};
}

let phase = 'pass1';
let pass1Calls = 0, pass2Calls = 0;
let pass1StartedResolve = null;
const pass1Started = new Promise(function(resolve) { pass1StartedResolve = resolve; });
let releasePass1 = null;
let pass2StartedResolve = null;
const pass2Started = new Promise(function(resolve) { pass2StartedResolve = resolve; });
let releasePass2 = null;
let throttleCalls = 0;
const subscribeCalls = [];
const resumes = [];

global.fetch = async function(url, opts) {
  const u = String(url);
  if (u === '/api/queued') return okJson(queuedNow.slice());
  if (u === '/api/subscribe_pace') return okJson({seed_seconds: 1});
  if (u === '/api/subscribe_failures') return okJson([]);
  if (u === '/api/pause') return okJson({ok: true});
  if (u === '/api/resume') {
    let owner = 'unnamed';
    try { owner = JSON.parse(opts.body).owner; } catch (e) {}
    resumes.push(owner);
    return okJson({ok: true});
  }
  if (u === '/api/subscribe_throttle') {
    throttleCalls += 1;
    // The first check is pass one's; it is throttled, so both of that stale
    // pass's clears run before it stops.
    if (throttleCalls === 1) {
      return okJson({throttled_at: Date.now() / 1000, retry_after: 300});
    }
    return okJson({throttled_at: 0, retry_after: 300});
  }
  if (u.indexOf('/api/subscribe/') === 0) {
    subscribeCalls.push(phase + ':' + u);
    if (phase === 'pass1') {
      pass1Calls += 1;
      if (pass1Calls === 1) {
        pass1StartedResolve();
        await new Promise(function(resolve) { releasePass1 = resolve; });
      }
    } else {
      pass2Calls += 1;
      if (pass2Calls === 1) {
        pass2StartedResolve();
        await new Promise(function(resolve) { releasePass2 = resolve; });
      }
    }
    return {ok: true, status: 200, statusText: 'OK',
            json: async function() { return {success: 1}; }};
  }
  return okJson({ok: true});
};

const runPass = (__START__);

(async function() {
  // Pass one: hold it inside its first subscribe call.
  const pass1 = runPass();
  await pass1Started;

  // Pass two starts mid-drain and arms its own poll, schedule and estimate.
  phase = 'pass2';
  const pass2 = runPass();
  await pass2Started;
  const pollB = _subPollIv;
  const schedB = _subScheduleIv;
  const estimateB = _subEstimate;

  // Let pass one finish. Its throttle check and its finally both ran against
  // handles and an estimate that already belonged to pass two.
  releasePass1();
  await pass1;
  const pollAfterPass1 = _subPollIv;
  const schedAfterPass1 = _subScheduleIv;
  const estimateSurvived = _subEstimate === estimateB;
  const resumesAfterPass1 = resumes.length;
  const resumeOwnersAfterPass1 = resumes.slice();
  const liveSchedulesAfterPass1 = liveIntervals(250);

  // The 250 ms schedule tick is what redraws the countdowns; a stale pass that
  // dropped the estimate leaves the tick with nothing to draw.
  const tick = fireInterval(schedB);
  if (tick.promise && typeof tick.promise.then === 'function') await tick.promise;
  const countdownsAfterTick = rows.map(function(r) {
    return r.querySelector('.countdown').textContent;
  });

  // Let pass two finish so nothing is left in flight.
  releasePass2();
  await pass2;

  console.log(JSON.stringify({
    poll_b: pollB,
    sched_b: schedB,
    poll_after_pass1: pollAfterPass1,
    sched_after_pass1: schedAfterPass1,
    estimate_survived: estimateSurvived,
    resumes_after_pass1: resumesAfterPass1,
    resume_owners_after_pass1: resumeOwnersAfterPass1,
    live_schedules_after_pass1: liveSchedulesAfterPass1,
    schedule_tick_ran: tick.ran,
    countdowns_after_tick: countdownsAfterTick,
    subscribe_calls: subscribeCalls
  }));
})();
"""


def _overlay_stale_pass_scenario(client, tmp_path):
    script = _served_inline_script(client)
    helpers = "\n".join(
        _extract_function(script, name)
        for name in ("_subItemSeconds", "_subRowRemainingSeconds",
                     "_subBatchRemainingSeconds", "_subRowFigure", "_subFormatDuration",
                     "_subElapsedCurrent", "_subSeedSeconds", "_subRenderProgress",
                     "_clearSubEstimates", "_subTickEstimates"))
    driver = (OVERLAY_STALE_PASS_DRIVER
              .replace("__ESTIMATOR__", helpers)
              .replace("__START__", _extract_function(script, "_startAutoSubscribe"))
              .replace("__CANCEL__", _extract_cancel_onclick(script)))
    return _run_node(driver, tmp_path)


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_stale_pass_leaves_a_newer_passs_poll_schedule_and_estimate_alone(web_client, tmp_path):
    """The defect: the older pass's exit paths clear the newer pass's handles.

    Against the pre-change page the first pass's throttle stop clears
    `_subPollIv` (already the second pass's) and drops `_subEstimate`, and its
    `finally` clears `_subScheduleIv`, so the second pass is left with no poll,
    no schedule tick and no estimate.

    The pause release is the one clear that is **not** guarded any more: with
    owner-scoped release a superseded pass releasing its own pause is always
    correct, so the stale pass now frees its own lock and only its own. Against
    the pre-change page this test's resume-owner assertion sees no call at all
    (`resumes_after_pass1: 0`); after the change the stale pass's `finally`
    releases `test-page:1` while leaving pass two's lock alone.
    """
    client, _ = web_client
    out = _overlay_stale_pass_scenario(client, tmp_path)

    assert out["poll_after_pass1"] == out["poll_b"], (
        "the older pass's throttle stop must not clear the newer pass's poll; "
        f"got {out['poll_after_pass1']!r} for new {out['poll_b']!r}")
    assert out["sched_after_pass1"] == out["sched_b"], (
        "the older pass's finally must not clear the newer pass's schedule; "
        f"got {out['sched_after_pass1']!r} for new {out['sched_b']!r}")
    assert out["estimate_survived"] is True, (
        "the older pass must not drop the newer pass's estimate")
    assert out["schedule_tick_ran"] is True, (
        "the newer pass's schedule tick must still be live")
    assert all(out["countdowns_after_tick"]), (
        "the newer pass's estimate must still draw figures; got "
        f"{out['countdowns_after_tick']}")
    assert out["resume_owners_after_pass1"] == ["test-page:1"], (
        "a superseded pass releases its own owned pause -- and only its own -- "
        "from its own finally; got "
        f"{out['resume_owners_after_pass1']} resume call(s)")
    assert out["resumes_after_pass1"] == 1, (
        "the stale pass must release exactly its own pause; got "
        f"{out['resumes_after_pass1']} resume call(s)")


# --- a Cancel click landing inside the `/api/pause` await --------------------
#
# Issue 87. `_startAutoSubscribe` draws the fresh overlay and then awaits
# `/api/pause`; the schedule is armed only after that await. A Cancel click in
# that window saw `_subScheduleIv` still null and fell through to the Close
# branch -- hiding the overlay and resuming the daemon -- while the pass it was
# pressed against carried on arming its schedule and draining rows with nothing
# on screen. The driver holds the pass inside the await, clicks the served
# Cancel handler, and asks what the pass did after the await returned.

OVERLAY_CANCEL_DURING_PAUSE_DRIVER = r"""
__ESTIMATOR__
// A plain string escape, not the behaviour under test.
function _escapeHtml(text) { return String(text); }

let _subPollIv = null;
let _subScheduleIv = null;
let _subCanceled = false;
let _subThrottleStopped = false;
let _subEstimate = null;
let _subPassToken = 0;
let _subPassLive = false;
let _subPageId = 'test-page';

let _timerId = 0;
const _timers = new Map();
let scheduleArms = 0;
global.setInterval = function(fn, ms) {
  const id = ++_timerId;
  if (ms === 250) scheduleArms += 1;
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
function liveSchedules() {
  const out = [];
  _timers.forEach(function(t, id) {
    if (t.kind === 'interval' && t.ms === 250) out.push(id);
  });
  return out;
}

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
  querySelectorAll: function(sel) {
    if (sel === '.sub-queue-item:not(.done)') {
      return rows.filter(function(r) { return !r.classList.contains('done'); });
    }
    return [];
  }
};
global.alert = function() {};

// The served click handler, wired onto the stub exactly as the page wires it.
__CANCEL__

const queuedNow = [
  {workshop_id: 11, title: 'A', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'a', subscription_glyph: 'x'},
  {workshop_id: 22, title: 'B', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'b', subscription_glyph: 'x'}
];

function okJson(body) {
  return {ok: true, status: 200, statusText: 'OK', json: async function() { return body; }};
}

const subscribeCalls = [];
const dequeues = [];
const resumes = [];
let pauseStartedResolve = null;
const pauseStarted = new Promise(function(resolve) { pauseStartedResolve = resolve; });
let releasePause = null;

global.fetch = async function(url, opts) {
  const u = String(url);
  if (u === '/api/queued') return okJson(queuedNow.slice());
  if (u === '/api/subscribe_pace') return okJson({seed_seconds: 1});
  if (u === '/api/subscribe_failures') return okJson([]);
  if (u === '/api/pause') {
    // Hold the pass exactly where the overlay is drawn and no schedule exists.
    pauseStartedResolve();
    await new Promise(function(resolve) { releasePause = resolve; });
    return okJson({ok: true});
  }
  if (u === '/api/resume') { resumes.push(1); return okJson({ok: true}); }
  if (u === '/api/subscribe_throttle') return okJson({throttled_at: 0, retry_after: 300});
  if (u.indexOf('/api/subscribe/') === 0) {
    subscribeCalls.push(u);
    return {ok: true, status: 200, statusText: 'OK',
            json: async function() { return {success: 1}; }};
  }
  if (u.indexOf('/api/dequeue/') === 0) {
    dequeues.push(u.slice('/api/dequeue/'.length));
    return okJson({ok: true});
  }
  return okJson({ok: true});
};

const runPass = (__START__);
const cancelButton = elements['sub-cancel'];

(async function() {
  const pass = runPass();
  await pauseStarted;
  // The click lands inside the await, before any schedule is armed.
  cancelButton.onclick();
  const buttonAfterClick = cancelButton.textContent;
  releasePause();
  await pass;

  console.log(JSON.stringify({
    button_after_click: buttonAfterClick,
    subscribe_calls: subscribeCalls,
    dequeues: dequeues,
    resumes: resumes.length,
    schedule_arms: scheduleArms,
    live_schedules: liveSchedules(),
    overlay_display: elements['sub-queue-modal'].style.display
  }));
})();
"""


def _overlay_cancel_during_pause_scenario(client, tmp_path):
    script = _served_inline_script(client)
    helpers = "\n".join(
        _extract_function(script, name)
        for name in ("_subItemSeconds", "_subBatchRemainingSeconds", "_subFormatDuration",
                     "_subElapsedCurrent", "_subSeedSeconds", "_subRenderProgress",
                     "_clearSubEstimates", "_subTickEstimates"))
    driver = (OVERLAY_CANCEL_DURING_PAUSE_DRIVER
              .replace("__ESTIMATOR__", helpers)
              .replace("__START__", _extract_function(script, "_startAutoSubscribe"))
              .replace("__CANCEL__", _extract_cancel_onclick(script)))
    return _run_node(driver, tmp_path)


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_cancel_inside_the_pause_await_stops_the_pass_before_it_drains(web_client, tmp_path):
    """The defect: the click takes the Close branch and the pass drains on.

    Against the pre-change page the click sees `_subScheduleIv` null, hides the
    overlay, dequeues both rows and resumes the daemon; the pass then arms its
    schedule, drains both items, and resumes the daemon a second time. The
    assertions below therefore fail on the subscribe calls, the schedule arming
    and the resume count, not only on the overlay's visibility.
    """
    client, _ = web_client
    out = _overlay_cancel_during_pause_scenario(client, tmp_path)

    assert out["button_after_click"] == "Close", \
        "the click must be read as Cancel, not as a Close of nothing"
    assert out["subscribe_calls"] == [], (
        "a pass cancelled inside the pause await must drain no row; got "
        f"{out['subscribe_calls']}")
    assert out["schedule_arms"] == 0, (
        "a cancelled pass must not arm its schedule; armed "
        f"{out['schedule_arms']} time(s)")
    assert out["live_schedules"] == [], (
        f"no schedule may be left armed; got {out['live_schedules']}")
    assert out["resumes"] == 1, (
        "the cancelled pass must release the daemon pause exactly once; got "
        f"{out['resumes']} resume call(s)")
    assert out["dequeues"] == [], (
        "Cancel must not dequeue -- that is Close's job; got "
        f"{out['dequeues']}")
    assert out["overlay_display"] == "block", \
        "the overlay must stay up for the user's Close"


# --- a pass that aborts on an empty queue must not take ownership -------------
#
# The ownership claim has to be taken only once a pass has committed to
# running. Taking the token above the `/api/queued` read let a pass that then
# abandoned -- the empty-queue return, or a rejected read -- invalidate the
# pass that was actually running: the running pass's `finally` saw a stale
# token, so it skipped the handle clears, `_clearSubEstimates()` and `finish()`,
# and nothing else releases the pause. Reachable by starting a pass with a row
# or two, letting the drain empty the queue, and pressing `l` again while the
# verification poll is still running. The driver holds pass one inside its
# first `/api/subscribe` call, runs a second pass that reads an empty queue, and
# asks whether pass one's own exit still cleared its handles and released the
# pause.
#
# This is a regression pin, not a pre-issue-86 reproduction: on the page before
# the ownership token the aborting pass mutated nothing either, so this test
# passes there.

OVERLAY_EMPTY_QUEUE_DRIVER = r"""
__ESTIMATOR__
// A plain string escape, not the behaviour under test.
function _escapeHtml(text) { return String(text); }

let _subPollIv = null;
let _subScheduleIv = null;
let _subCanceled = false;
let _subThrottleStopped = false;
let _subEstimate = null;
let _subPassToken = 0;
let _subPassLive = false;
let _subPageId = 'test-page';

// Timers are registered but never fire on their own; this scenario only asks
// which handles are still registered.
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
function liveIntervals(ms) {
  const out = [];
  _timers.forEach(function(t, id) {
    if (t.kind === 'interval' && t.ms === ms) out.push(id);
  });
  return out;
}

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
  querySelectorAll: function(sel) {
    if (sel === '.sub-queue-item:not(.done)') {
      return rows.filter(function(r) { return !r.classList.contains('done'); });
    }
    return [];
  }
};
const alerts = [];
global.alert = function(message) { alerts.push(String(message)); };

// The served click handler, wired onto the stub exactly as the page wires it.
__CANCEL__

let queuedNow = [
  {workshop_id: 11, title: 'A', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'a', subscription_glyph: 'x'},
  {workshop_id: 22, title: 'B', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'b', subscription_glyph: 'x'}
];

function okJson(body) {
  return {ok: true, status: 200, statusText: 'OK', json: async function() { return body; }};
}

const resumes = [];
const subscribeCalls = [];
let pass1Calls = 0;
let pass1StartedResolve = null;
const pass1Started = new Promise(function(resolve) { pass1StartedResolve = resolve; });
let releasePass1 = null;

global.fetch = async function(url, opts) {
  const u = String(url);
  if (u === '/api/queued') return okJson(queuedNow.slice());
  if (u === '/api/subscribe_pace') return okJson({seed_seconds: 1});
  if (u === '/api/subscribe_failures') return okJson([]);
  if (u === '/api/pause') return okJson({ok: true});
  if (u === '/api/resume') { resumes.push(1); return okJson({ok: true}); }
  if (u === '/api/subscribe_throttle') return okJson({throttled_at: 0, retry_after: 300});
  if (u.indexOf('/api/subscribe/') === 0) {
    subscribeCalls.push(u);
    pass1Calls += 1;
    if (pass1Calls === 1) {
      pass1StartedResolve();
      await new Promise(function(resolve) { releasePass1 = resolve; });
    }
    return {ok: true, status: 200, statusText: 'OK',
            json: async function() { return {success: 1}; }};
  }
  return okJson({ok: true});
};

const runPass = (__START__);

(async function() {
  // Pass one: hold it inside its first subscribe call, with its poll and
  // schedule armed and the daemon paused.
  const pass1 = runPass();
  await pass1Started;
  const liveBefore = _subPassLive;
  const scheduleBefore = _subScheduleIv;
  const estimateBefore = _subEstimate !== null;

  // The drain's own subscribe calls have emptied the queue. A second pass finds
  // nothing and aborts; it must not become the owner or touch pass one.
  queuedNow = [];
  const pass2 = runPass();
  await pass2;
  const resumesAfterPass2 = resumes.length;
  const scheduleAfterPass2 = _subScheduleIv;
  const liveAfterPass2 = _subPassLive;

  // Let pass one finish. Its finally is now the only holder of the pause.
  releasePass1();
  await pass1;
  const resumesAfterPass1 = resumes.length;
  const scheduleAfterPass1 = _subScheduleIv;
  const estimateAfterPass1 = _subEstimate;
  const liveAfterPass1 = _subPassLive;

  console.log(JSON.stringify({
    live_before: liveBefore,
    schedule_before: scheduleBefore,
    estimate_before: estimateBefore,
    resumes_after_pass2: resumesAfterPass2,
    schedule_after_pass2: scheduleAfterPass2,
    live_after_pass2: liveAfterPass2,
    resumes_after_pass1: resumesAfterPass1,
    schedule_after_pass1: scheduleAfterPass1,
    estimate_after_pass1: estimateAfterPass1,
    live_after_pass1: liveAfterPass1,
    live_schedules_after_pass1: liveIntervals(250),
    subscribe_calls: subscribeCalls,
    alerts: alerts
  }));
})();
"""


def _overlay_empty_queue_scenario(client, tmp_path):
    script = _served_inline_script(client)
    helpers = "\n".join(
        _extract_function(script, name)
        for name in ("_subItemSeconds", "_subRowRemainingSeconds",
                     "_subBatchRemainingSeconds", "_subRowFigure", "_subFormatDuration",
                     "_subElapsedCurrent", "_subSeedSeconds", "_subRenderProgress",
                     "_clearSubEstimates", "_subTickEstimates"))
    driver = (OVERLAY_EMPTY_QUEUE_DRIVER
              .replace("__ESTIMATOR__", helpers)
              .replace("__START__", _extract_function(script, "_startAutoSubscribe"))
              .replace("__CANCEL__", _extract_cancel_onclick(script)))
    return _run_node(driver, tmp_path)


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_an_empty_queue_pass_leaves_a_running_pass_its_ownership(web_client, tmp_path):
    """A pass that aborts on an empty queue must not invalidate the live one.

    The ownership claim has to sit below the queue read. Against a page that
    takes the token first, the aborting pass's bump leaves the running pass
    stale, so its `finally` skips the clears and the pause release and the
    daemon stays paused with the countdown tick still registered.
    """
    client, _ = web_client
    out = _overlay_empty_queue_scenario(client, tmp_path)

    assert out["live_before"] is True, "pass one must be the live pass"
    assert out["schedule_before"] is not None, "pass one must have armed its schedule"
    assert out["estimate_before"] is True, "pass one must have drawn an estimate"
    assert out["alerts"] == ["No items queued for subscription."], \
        "the second pass must be the one that aborts"

    assert out["resumes_after_pass1"] == 1, (
        "the running pass must still release the daemon pause after a newer "
        "pass aborts on an empty queue; got "
        f"{out['resumes_after_pass1']} resume call(s)")
    assert out["schedule_after_pass1"] is None, (
        "the running pass's own finally must still clear its schedule; got "
        f"{out['schedule_after_pass1']}")
    assert out["live_schedules_after_pass1"] == [], (
        "no countdown tick may outlive the pass that armed it; got "
        f"{out['live_schedules_after_pass1']}")
    assert out["estimate_after_pass1"] is None, (
        "the running pass must still drop its estimate; got "
        f"{out['estimate_after_pass1']}")
    assert out["live_after_pass1"] is False, (
        "the running pass must still end as the live pass; got "
        f"{out['live_after_pass1']}")


# --- a rejected pause must hand ownership back -------------------------------
#
# The other early exit after the ownership claim. A pass that takes the token
# and then fails its `/api/pause` never runs; if it kept the token, the pass
# that was already draining would be stale, its `finally` would skip its clears
# and its pause release, and the daemon would stay paused with the countdown
# tick registered. The driver holds pass one mid-drain, runs a second pass whose
# pause rejects, and asks whether pass one kept its ownership, handles and
# estimate and still released the pause when it finished.

OVERLAY_PAUSE_REJECT_DRIVER = r"""
__ESTIMATOR__
// A plain string escape, not the behaviour under test.
function _escapeHtml(text) { return String(text); }

let _subPollIv = null;
let _subScheduleIv = null;
let _subCanceled = false;
let _subThrottleStopped = false;
let _subEstimate = null;
let _subPassToken = 0;
let _subPassLive = false;
let _subPageId = 'test-page';

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
function liveIntervals(ms) {
  const out = [];
  _timers.forEach(function(t, id) {
    if (t.kind === 'interval' && t.ms === ms) out.push(id);
  });
  return out;
}

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
  querySelectorAll: function(sel) {
    if (sel === '.sub-queue-item:not(.done)') {
      return rows.filter(function(r) { return !r.classList.contains('done'); });
    }
    return [];
  }
};
global.alert = function() {};

// The served click handler, wired onto the stub exactly as the page wires it.
__CANCEL__

const queuedNow = [
  {workshop_id: 11, title: 'A', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'a', subscription_glyph: 'x'},
  {workshop_id: 22, title: 'B', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'b', subscription_glyph: 'x'}
];

function okJson(body) {
  return {ok: true, status: 200, statusText: 'OK', json: async function() { return body; }};
}

const resumes = [];
const subscribeCalls = [];
let pass1Calls = 0;
let pass1StartedResolve = null;
const pass1Started = new Promise(function(resolve) { pass1StartedResolve = resolve; });
let releasePass1 = null;
let pauseCalls = 0;
let failNextPause = false;

global.fetch = async function(url, opts) {
  const u = String(url);
  if (u === '/api/queued') return okJson(queuedNow.slice());
  if (u === '/api/subscribe_pace') return okJson({seed_seconds: 1});
  if (u === '/api/subscribe_failures') return okJson([]);
  if (u === '/api/pause') {
    pauseCalls += 1;
    if (failNextPause) throw new Error('pause unavailable');
    return okJson({ok: true});
  }
  if (u === '/api/resume') { resumes.push(1); return okJson({ok: true}); }
  if (u === '/api/subscribe_throttle') return okJson({throttled_at: 0, retry_after: 300});
  if (u.indexOf('/api/subscribe/') === 0) {
    subscribeCalls.push(u);
    pass1Calls += 1;
    if (pass1Calls === 1) {
      pass1StartedResolve();
      await new Promise(function(resolve) { releasePass1 = resolve; });
    }
    return {ok: true, status: 200, statusText: 'OK',
            json: async function() { return {success: 1}; }};
  }
  return okJson({ok: true});
};

const runPass = (__START__);

(async function() {
  // Pass one: hold it inside its first subscribe call, running.
  const pass1 = runPass();
  await pass1Started;
  const pollA = _subPollIv;
  const schedA = _subScheduleIv;
  const estimateA = _subEstimate;

  // Pass two claims, then its pause rejects. It never runs, so it must hand
  // ownership back rather than leave pass one stale.
  failNextPause = true;
  let pass2Error = null;
  try {
    await runPass();
  } catch (e) {
    pass2Error = e.message;
  }
  const pollAfterReject = _subPollIv;
  const schedAfterReject = _subScheduleIv;
  const liveAfterReject = _subPassLive;

  // Let pass one finish; it must still be the owner.
  releasePass1();
  await pass1;
  const resumesAfterPass1 = resumes.length;
  const scheduleAfterPass1 = _subScheduleIv;
  const estimateAfterPass1 = _subEstimate;
  const liveAfterPass1 = _subPassLive;

  console.log(JSON.stringify({
    poll_a: pollA,
    sched_a: schedA,
    estimate_a: estimateA !== null,
    pass2_error: pass2Error,
    poll_after_reject: pollAfterReject,
    sched_after_reject: schedAfterReject,
    live_after_reject: liveAfterReject,
    resumes_after_pass1: resumesAfterPass1,
    schedule_after_pass1: scheduleAfterPass1,
    estimate_after_pass1: estimateAfterPass1,
    live_after_pass1: liveAfterPass1,
    live_schedules_after_pass1: liveIntervals(250),
    subscribe_calls: subscribeCalls
  }));
})();
"""


def _overlay_pause_reject_scenario(client, tmp_path):
    script = _served_inline_script(client)
    helpers = "\n".join(
        _extract_function(script, name)
        for name in ("_subItemSeconds", "_subRowRemainingSeconds",
                     "_subBatchRemainingSeconds", "_subRowFigure", "_subFormatDuration",
                     "_subElapsedCurrent", "_subSeedSeconds", "_subRenderProgress",
                     "_clearSubEstimates", "_subTickEstimates"))
    driver = (OVERLAY_PAUSE_REJECT_DRIVER
              .replace("__ESTIMATOR__", helpers)
              .replace("__START__", _extract_function(script, "_startAutoSubscribe"))
              .replace("__CANCEL__", _extract_cancel_onclick(script)))
    return _run_node(driver, tmp_path)


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_rejected_pause_hands_ownership_back_to_the_running_pass(web_client, tmp_path):
    """A pass that never ran must not leave the running pass stale.

    Against a page that claims the token and clears the predecessor's handles
    before the pause, the rejection leaves the running pass stale: its handles
    are already gone, its `finally` skips the estimate clear and the pause
    release, and the daemon stays paused.
    """
    client, _ = web_client
    out = _overlay_pause_reject_scenario(client, tmp_path)

    assert out["pass2_error"] == "pause unavailable", \
        "the second pass must fail on the rejected pause"
    assert out["live_after_reject"] is True, (
        "the running pass must still be the live pass after the attempt fails; "
        f"got {out['live_after_reject']}")
    assert out["poll_after_reject"] == out["poll_a"], (
        "the running pass's poll must survive the rejected pause; got "
        f"{out['poll_after_reject']!r} for {out['poll_a']!r}")
    assert out["sched_after_reject"] == out["sched_a"], (
        "the running pass's schedule must survive the rejected pause; got "
        f"{out['sched_after_reject']!r} for {out['sched_a']!r}")
    assert out["resumes_after_pass1"] == 1, (
        "the running pass must still release the daemon pause; got "
        f"{out['resumes_after_pass1']} resume call(s)")
    assert out["schedule_after_pass1"] is None, (
        f"the running pass's finally must clear its schedule; got "
        f"{out['schedule_after_pass1']}")
    assert out["live_schedules_after_pass1"] == [], (
        f"no countdown tick may outlive the pass; got "
        f"{out['live_schedules_after_pass1']}")
    assert out["estimate_after_pass1"] is None, (
        f"the running pass must drop its estimate; got "
        f"{out['estimate_after_pass1']}")
    assert out["live_after_pass1"] is False, (
        f"the running pass must end as the live pass; got "
        f"{out['live_after_pass1']}")


# --- issue 88: a predecessor finishing inside a rejected pause's await --------
#
# With owner-scoped release, a pass releasing **its own** pause is always
# correct, even when it has been superseded -- so the resume in
# `_startAutoSubscribe`'s `finally` is no longer guarded by the pass token and
# the page's `finish()` calls `/api/resume` unconditionally. That is what closes
# issue 88's stranded-claim window: pass one is still draining when pass two
# claims the token and awaits `/api/pause`; pass two's pause rejects, and pass
# one finishes inside that same await, by which time pass one is stale. Its
# `finally` used to skip the resume because of the token guard, leaving the
# `.pauselock` with no holder. Now pass one frees its own lock from its own
# `finally`, and pass two -- which never acquired -- frees nothing.
#
# The stub models the server's ownership rules: `begin_pause` never steals a
# held lock, and `end_pause` removes the lock only for the owner it is given.
# A legacy, unnamed owner would match anyone; the page always sends one.

OVERLAY_PREDECESSOR_FINISHES_IN_REJECTED_PAUSE_DRIVER = r"""
__ESTIMATOR__
// A plain string escape, not the behaviour under test.
function _escapeHtml(text) { return String(text); }

let _subPollIv = null;
let _subScheduleIv = null;
let _subCanceled = false;
let _subThrottleStopped = false;
let _subEstimate = null;
let _subPassToken = 0;
let _subPassLive = false;
let _subPageId = 'test-page';

let _timerId = 0;
global.setInterval = function() { return ++_timerId; };
global.clearInterval = function() {};
global.setTimeout = function() { return ++_timerId; };
global.clearTimeout = function() {};

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
  querySelectorAll: function(sel) {
    if (sel === '.sub-queue-item:not(.done)') {
      return rows.filter(function(r) { return !r.classList.contains('done'); });
    }
    return [];
  }
};
global.alert = function() {};

// The served click handler, wired onto the stub exactly as the page wires it.
__CANCEL__

const queuedNow = [
  {workshop_id: 11, title: 'A', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'a', subscription_glyph: 'x'},
  {workshop_id: 22, title: 'B', subscription_state: 'queued',
   subscription_colour: '#0f0', subscription_tooltip: 'b', subscription_glyph: 'x'}
];

function okJson(body) {
  return {ok: true, status: 200, statusText: 'OK', json: async function() { return body; }};
}

// The simulated `.pauselock`: whoever `begin_pause` first recorded holds it
// (the server never steals a held lock), and `end_pause` removes it only for
// that owner. `resumes` records every owner the page asked to release.
let pauseHolders = [];
const resumes = [];
const pauseOwners = [];
let pass1StartedResolve = null;
const pass1Started = new Promise(function(resolve) { pass1StartedResolve = resolve; });
let releasePass1 = null;
let pass2PauseRejects = false;
let pauseStartedResolve = null;
const pauseStarted = new Promise(function(resolve) { pauseStartedResolve = resolve; });
let releasePause = null;

function _ownerOf(opts) {
  // A page before this change sends no body at all: that is an unnamed caller,
  // which the server would treat as a legacy (releasable-by-anyone) lock.
  if (!opts || !opts.body) return 'unnamed';
  try { return JSON.parse(opts.body).owner || 'unnamed'; } catch (e) { return 'unnamed'; }
}

global.fetch = async function(url, opts) {
  const u = String(url);
  if (u === '/api/queued') return okJson(queuedNow.slice());
  if (u === '/api/subscribe_pace') return okJson({seed_seconds: 1});
  if (u === '/api/subscribe_failures') return okJson([]);
  if (u === '/api/pause') {
    pauseOwners.push(_ownerOf(opts));
    if (pass2PauseRejects) {
      // Pass two is held exactly inside its pause await, then rejected.
      pauseStartedResolve();
      await new Promise(function(resolve) { releasePause = resolve; });
      throw new Error('pause unavailable');
    }
    // begin_pause never steals a held lock: the first owner keeps it.
    if (pauseHolders.length === 0) pauseHolders.push(_ownerOf(opts));
    return okJson({ok: true});
  }
  if (u === '/api/resume') {
    const owner = _ownerOf(opts);
    resumes.push(owner);
    pauseHolders = pauseHolders.filter(function(held) { return held !== owner; });
    return okJson({ok: true});
  }
  if (u === '/api/subscribe_throttle') return okJson({throttled_at: 0, retry_after: 300});
  if (u.indexOf('/api/subscribe/') === 0) {
    if (pass1StartedResolve !== null && _subPassToken === 1) {
      pass1StartedResolve();
      await new Promise(function(resolve) { releasePass1 = resolve; });
      pass1StartedResolve = null;
    }
    return {ok: true, status: 200, statusText: 'OK',
            json: async function() { return {success: 1}; }};
  }
  return okJson({ok: true});
};

const runPass = (__START__);

(async function() {
  // Pass one starts and holds its first subscribe call, owning the pause.
  const pass1 = runPass();
  await pass1Started;
  const holdersAfterPass1Pause = pauseHolders.slice();

  // Pass two claims the token and awaits a pause that will reject.
  pass2PauseRejects = true;
  const pass2 = runPass();
  await pauseStarted;
  const holdersAfterPass2Pause = pauseHolders.slice();
  const pass2PauseOwner = pauseOwners[pauseOwners.length - 1];

  // Pass one finishes inside pass two's pause await -- it is stale by then.
  releasePass1();
  await pass1;
  const resumesAfterPass1 = resumes.slice();
  const holdersAfterPass1 = pauseHolders.slice();

  // The rejected pause then resolves as a throw; pass two hands ownership back.
  releasePause();
  let pass2Error = null;
  try {
    await pass2;
  } catch (e) {
    pass2Error = e.message;
  }

  console.log(JSON.stringify({
    holders_after_pass1_pause: holdersAfterPass1Pause,
    holders_after_pass2_pause: holdersAfterPass2Pause,
    pass2_pause_owner: pass2PauseOwner,
    resume_owners_after_pass1: resumesAfterPass1,
    holders_after_pass1: holdersAfterPass1,
    pass2_error: pass2Error,
    holders_after_rejection: pauseHolders.slice()
  }));
})();
"""


def _overlay_predecessor_in_rejected_pause_scenario(client, tmp_path):
    script = _served_inline_script(client)
    helpers = "\n".join(
        _extract_function(script, name)
        for name in ("_subItemSeconds", "_subBatchRemainingSeconds", "_subFormatDuration",
                     "_subElapsedCurrent", "_subSeedSeconds", "_subRenderProgress",
                     "_clearSubEstimates"))
    driver = (OVERLAY_PREDECESSOR_FINISHES_IN_REJECTED_PAUSE_DRIVER
              .replace("__ESTIMATOR__", helpers)
              .replace("__START__", _extract_function(script, "_startAutoSubscribe"))
              .replace("__CANCEL__", _extract_cancel_onclick(script)))
    return _run_node(driver, tmp_path)


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_predecessor_finishing_inside_a_rejected_pause_await_releases_its_own_lock(
        web_client, tmp_path):
    """Issue 88's stranded window, driven through the served page.

    Pass one owns the pause; pass two claims the token and awaits `/api/pause`;
    pass one finishes inside that await and is superseded; pass two's pause then
    rejects and hands the token back. Against the pre-change page pass one's
    `finally` skips the resume because of the token guard, so the simulated
    `.pauselock` still has a holder (`['test-page:1']`) and no resume carries
    pass one's owner. After the change pass one releases its own lock and the
    holder set is empty -- the daemon is resumed -- while pass two, which never
    acquired, frees nothing.
    """
    client, _ = web_client
    out = _overlay_predecessor_in_rejected_pause_scenario(client, tmp_path)

    assert out["resume_owners_after_pass1"] == ["test-page:1"], (
        "the superseded predecessor must release its own lock from its own "
        f"finally; got {out['resume_owners_after_pass1']}")
    assert out["holders_after_pass1"] == [], (
        "the abandoned claim must not strand `.pauselock`; got "
        f"{out['holders_after_pass1']}")
    assert out["holders_after_pass1_pause"] == ["test-page:1"], (
        "pass one's pause must own the lock; got "
        f"{out['holders_after_pass1_pause']}")
    assert out["holders_after_pass2_pause"] == ["test-page:1"], (
        "begin_pause must not steal a held lock; got "
        f"{out['holders_after_pass2_pause']}")
    assert out["pass2_pause_owner"] == "test-page:2", (
        "the second pass must send its own owner; got "
        f"{out['pass2_pause_owner']!r}")
    assert out["pass2_error"] == "pause unavailable", \
        "the second pass must still fail on the rejected pause"
    assert out["holders_after_rejection"] == [], (
        "the daemon must end resumed; got "
        f"{out['holders_after_rejection']}")
