"""The bridge's retry policy is bounded, backed off, and reset by success.

`pushSessionToBackend` schedules its own retry, so the only honest test drives
it. This runs the real userscript in node against a stubbed Tampermonkey
environment: `setTimeout`/`setInterval` are captured rather than executed, so a
test can fail a push, read the delay it schedules, fire it, and repeat. A text
check for a delay literal would pass even if the chain never stopped.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
USERSCRIPT = ROOT / "userscripts" / "steam_subscribe.user.js"
NODE = shutil.which("node")

DRIVER = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8');

const requests = [];
const timeouts = [];
const intervals = [];
let nextId = 1;
let sessionid = 'sid-1';
let loginSecure = 'login-1';
let fakeNow = 1000000;

const sandbox = {
  location: { hostname: '127.0.0.1', origin: 'http://127.0.0.1:8080', search: '' },
  document: {
    querySelector: () => ({ content: '10' }),
    body: { dataset: {} },
    getElementById: () => null,
    querySelectorAll: () => [],
    createElement: () => ({ style: {}, classList: { add() {} }, remove() {}, appendChild() {} }),
    addEventListener: () => {},
  },
  GM_info: { script: { version: '10' } },
  GM_getValue: (key, fallback) => {
    if (key === 'steam_sessionid') return sessionid;
    if (key === 'steam_login_secure') return loginSecure;
    return fallback;
  },
  GM_setValue: () => {},
  GM_xmlhttpRequest: (opts) => { requests.push(opts); },
  console: { log() {}, warn() {}, error() {}, debug() {} },
  alert: () => {},
  setTimeout: (fn, delay) => { const id = nextId++; timeouts.push({ id, fn, delay }); return id; },
  clearTimeout: (id) => {
    for (let i = 0; i < timeouts.length; i++) {
      if (timeouts[i].id === id) { timeouts.splice(i, 1); return; }
    }
  },
  setInterval: (fn, delay) => { const id = nextId++; intervals.push({ id, fn, delay }); return id; },
  Date: { now: () => fakeNow },
};

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: 'steam_subscribe.user.js' });

const last = () => requests[requests.length - 1];
const fail = () => last().onerror();
const succeed = () => last().onload();

const out = {};
out.initial_requests = requests.length;
out.interval_count = intervals.length;
out.interval_ms = intervals.length ? intervals[0].delay : null;

// Consecutive failures back off, then stop.
const delays = [];
for (let i = 0; i < 12; i++) {
  fail();
  if (timeouts.length === 0) break;
  const timer = timeouts.shift();
  delays.push(timer.delay);
  timer.fn();  // the scheduled retry fires and issues the next request
}
out.retry_delays = delays;
out.requests_after_failures = requests.length;
out.timeouts_after_giving_up = timeouts.length;

// The periodic interval survives the give-up and keeps probing, slowly.
let before = requests.length;
intervals[0].fn();
out.periodic_pushed_after_giveup = requests.length - before;
fail();
out.timeouts_after_periodic_failure = timeouts.length;

// A success resets the backoff.
succeed();
out.timeouts_after_success = timeouts.length;
sessionid = 'sid-2';
before = requests.length;
intervals[0].fn();
out.pushed_on_changed_value = requests.length - before;
fail();
const reset = timeouts.shift();
out.retry_delay_after_success = reset ? reset.delay : null;

// Change detection still bites: an unchanged push after a success is silent.
if (reset) reset.fn();
succeed();
before = requests.length;
intervals[0].fn();
out.unchanged_push_is_silent = requests.length === before;

// The slow re-push still fires once the payload is stale.
fakeNow += 11 * 60 * 1000;
intervals[0].fn();
out.slow_repush_after_stale = requests.length - before;

console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the userscript")
def test_bridge_retry_is_bounded_and_reset_by_success(tmp_path):
    driver_path = tmp_path / "driver.js"
    driver_path.write_text(DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver_path), str(USERSCRIPT)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"node driver failed:\n{result.stdout}\n{result.stderr}"
    out = json.loads(result.stdout)

    assert out["initial_requests"] == 1, "the page load pushes once"
    assert out["interval_count"] == 1 and out["interval_ms"] == 30000, \
        "the periodic push must still be armed every thirty seconds"

    # Start small, double, hit the one-minute cap, then stop.
    assert out["retry_delays"] == [5000, 10000, 20000, 40000, 60000, 60000], \
        f"unexpected retry schedule: {out['retry_delays']}"
    assert out["requests_after_failures"] == 7, \
        "the initial push plus six retries is the whole budget"
    assert out["timeouts_after_giving_up"] == 0, "the retry chain must stop"

    # The thirty-second interval is the slow safety net and keeps working.
    assert out["periodic_pushed_after_giveup"] == 1, \
        "the periodic interval must still try after the fast retries give up"
    assert out["timeouts_after_periodic_failure"] == 0, \
        "a spent retry budget must not restart a fast chain on its own"

    # A success resets the backoff so a later transient failure is prompt.
    assert out["timeouts_after_success"] == 0
    assert out["pushed_on_changed_value"] == 1
    assert out["retry_delay_after_success"] == 5000, \
        "after a success the next failure must retry promptly"

    # The change-detection and the slow re-push safety net are preserved.
    assert out["unchanged_push_is_silent"] is True
    assert out["slow_repush_after_stale"] == 1
