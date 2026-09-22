"""The web overlay's subscription countdown: the arithmetic, the seed, the tick.

The overlay used to show seconds **since the pass began** on the row it was
asking about -- a monotonic count-up -- and the rows still waiting no figure at
all. It now counts **down**, per row and for the whole batch, from the same
estimator the TUI's queue screen uses (``SubscriptionQueueScreen``), so the two
front ends cannot disagree.

Two kinds of test live here. The estimator is pure JavaScript in
``templates/index.html`` and is driven under node, once directly and once
through the served 250 ms tick, with a hand-controlled clock -- the tick test is
the regression the owner reported: it draws the current row's figure at two
clock readings and requires the second to be smaller. The seed comes from
``GET /api/subscribe_pace``, which must read the persisted delay fresh rather
than snapshot it, because a throttle can double it mid-pass.

There is no jsdom and no ``node_modules`` here; the drivers hand-stub the few
globals the page builds against, the pattern ``tests/test_subscription_web.py``
uses.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src import pacing, subscribe_engine
from src.daemon_state import StateStore, state_path_for
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


def _set_delay(db_path: str, seconds: float) -> None:
    """Change the persisted delay mid-test, as a throttle's doubling would."""
    StateStore(state_path_for(db_path)).save({pacing.WEB_DELAY_SECTION: seconds})


# --- the seed endpoint ------------------------------------------------------


def test_subscribe_pace_prices_the_seed_from_the_persisted_delay(web_client):
    """One gated page read on the default path, so the seed is the delay."""
    client, db_path = web_client
    _set_delay(db_path, 12.0)

    body = client.get('/api/subscribe_pace').get_json()

    assert body == {"web_delay_seconds": 12.0, "seed_seconds": 12.0}


def test_subscribe_pace_reads_the_delay_fresh_on_every_call(web_client):
    """The delay is adaptive state; a mid-pass doubling must move the seed."""
    client, db_path = web_client
    _set_delay(db_path, 12.0)
    assert client.get('/api/subscribe_pace').get_json()["seed_seconds"] == 12.0

    # What the engine's throttle writes back into the shared state section.
    _set_delay(db_path, 24.0)

    body = client.get('/api/subscribe_pace').get_json()
    assert body["web_delay_seconds"] == 24.0
    assert body["seed_seconds"] == 24.0


def test_subscribe_pace_doubles_the_seed_when_the_confirmation_read_is_on(
        web_client, monkeypatch):
    """The retired confirmation read prices a second gated read while on."""
    client, db_path = web_client
    _set_delay(db_path, 12.0)
    monkeypatch.setattr(subscribe_engine, "VERIFY_AFTER_SUBSCRIBE", True)

    body = client.get('/api/subscribe_pace').get_json()

    assert body["web_delay_seconds"] == 12.0
    assert body["seed_seconds"] == 24.0


def test_queued_payload_stays_a_bare_array(web_client):
    """The overlay iterates it directly; the seed must not have joined it."""
    client, db_path = web_client

    body = client.get('/api/queued').get_json()

    assert body == []


# --- running the served JavaScript under node -------------------------------


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


def _extract_setinterval_callback(script: str, name: str) -> str:
    """The callback text of `name = setInterval(<callback>, ...)`.

    Taken textually rather than by function name on purpose: the callback is an
    inline arrow in both the old and the new page, so the same driver can run
    either. That is what lets the regression test reach the old elapsed-up
    expression and fail on its *value*, not on a missing function.
    """
    match = re.search(re.escape(name) + r'\s*=\s*setInterval\s*\(', script)
    assert match, f"no setInterval assignment to {name}"
    brace = script.index('{', match.end())
    depth = 0
    for i in range(brace, len(script)):
        if script[i] == '{':
            depth += 1
        elif script[i] == '}':
            depth -= 1
            if depth == 0:
                return script[match.end():i + 1].strip()
    raise AssertionError(f"unterminated setInterval callback for {name}")


def _estimator_helpers(script: str) -> str:
    """Every estimator function the page defines, if it defines any.

    Absent ones are skipped so the same driver runs against a page that predates
    the estimator (where only the inline tick exists).
    """
    names = ("_subItemSeconds", "_subRowRemainingSeconds",
             "_subBatchRemainingSeconds", "_subRowFigure", "_subFormatDuration",
             "_subElapsedCurrent", "_subRenderProgress", "_subTickEstimates",
             "_clearSubEstimates")
    found = []
    for name in names:
        try:
            found.append(_extract_function(script, name))
        except AssertionError:
            continue
    return "\n".join(found)


def _run_node(driver: str, tmp_path):
    path = tmp_path / "driver.js"
    path.write_text(driver, encoding="utf-8")
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"node driver failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def _figure(text):
    match = re.search(r'-?\d+', text or "")
    return int(match.group(0)) if match else None


# The pure arithmetic, with the seed read as one virtual observation.
SHRINK_DRIVER = """
__HELPERS__
const itemSeconds = _subItemSeconds(12, []);
console.log(JSON.stringify({
  start: _subRowFigure(itemSeconds, 0, 0, 0),
  mid: _subRowFigure(itemSeconds, 0, 0, 5),
  late: _subRowFigure(itemSeconds, 0, 0, 10),
  overrun: _subRowFigure(itemSeconds, 0, 0, 60),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_current_rows_figure_shrinks_as_its_call_runs(web_client, tmp_path):
    """The regression the owner reported: this figure must fall, not rise.

    The figure is the current item's remaining cost -- its own cost minus the
    time already spent on its call -- clamped at one second so a slow item never
    draws zero while it is still running.
    """
    client, _ = web_client
    helpers = _estimator_helpers(_served_inline_script(client))
    out = _run_node(SHRINK_DRIVER.replace("__HELPERS__", helpers), tmp_path)

    start, mid, late, overrun = (_figure(out[k]) for k in ("start", "mid", "late", "overrun"))
    assert start == 12
    assert mid == 7
    assert late == 2
    assert start > mid > late, "the current row's figure must count down"
    assert overrun == 1, "an overrunning item clamps at one second, never zero"


BATCH_DRIVER = """
__HELPERS__
const itemSeconds = _subItemSeconds(12, []);
const done = 1;
console.log(JSON.stringify({
  last: _subRowFigure(itemSeconds, 3, done, 4),
  batch: _subBatchRemainingSeconds(itemSeconds, 4, done, 4),
  doneBatch: _subBatchRemainingSeconds(itemSeconds, 4, 4, 0),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_batch_figure_equals_the_last_rows_figure(web_client, tmp_path):
    """Both price the same remaining work: the current item plus the rest."""
    client, _ = web_client
    helpers = _estimator_helpers(_served_inline_script(client))
    out = _run_node(BATCH_DRIVER.replace("__HELPERS__", helpers), tmp_path)

    assert _figure(out["last"]) == 32   # (3 - 1 + 1) * 12 - 4
    assert out["batch"] == 32           # (4 - 1) * 12 - 4
    assert out["batch"] == _figure(out["last"])
    assert out["doneBatch"] == 0, "a finished batch is zero, not one"


ORDER_DRIVER = """
__HELPERS__
const itemSeconds = _subItemSeconds(12, []);
console.log(JSON.stringify({
  first: _subRowFigure(itemSeconds, 0, 0, 0),
  third: _subRowFigure(itemSeconds, 2, 0, 0),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_later_rows_figure_exceeds_an_earlier_rows(web_client, tmp_path):
    """Each row carries its own cost plus every item ahead of it."""
    client, _ = web_client
    helpers = _estimator_helpers(_served_inline_script(client))
    out = _run_node(ORDER_DRIVER.replace("__HELPERS__", helpers), tmp_path)

    assert _figure(out["first"]) == 12
    assert _figure(out["third"]) == 36
    assert _figure(out["third"]) > _figure(out["first"])


MEAN_DRIVER = """
__HELPERS__
console.log(JSON.stringify({
  seeded: _subItemSeconds(12, []),
  oneSlow: _subItemSeconds(12, [30]),
  oneFast: _subItemSeconds(12, [2]),
  twoSlow: _subItemSeconds(12, [30, 30]),
  batchFromSeed: _subBatchRemainingSeconds(_subItemSeconds(12, []), 5, 0, 0),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_seed_prices_the_queue_and_an_observation_moves_the_mean(web_client, tmp_path):
    """The seed is one virtual observation, so the first item moves the mean a lot."""
    client, _ = web_client
    helpers = _estimator_helpers(_served_inline_script(client))
    out = _run_node(MEAN_DRIVER.replace("__HELPERS__", helpers), tmp_path)

    assert out["seeded"] == 12, "before any observation the mean is the seed"
    assert out["oneSlow"] == 21, "(12 + 30) / 2 -- moved toward the measured cost"
    assert out["oneFast"] == 7, "(12 + 2) / 2 -- moved down just as directly"
    assert out["twoSlow"] == 24, "(12 + 30 + 30) / 3 -- the seed decays"
    assert out["batchFromSeed"] == 60, "5 items at the bare seed"


DONE_DRIVER = """
__HELPERS__
const itemSeconds = _subItemSeconds(12, []);
console.log(JSON.stringify({
  before: _subRowFigure(itemSeconds, 0, 1, 0),
  at: _subRowFigure(itemSeconds, 1, 1, 0),
  after: _subRowFigure(itemSeconds, 2, 1, 0),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_no_figure_is_produced_for_a_row_at_or_below_done(web_client, tmp_path):
    """A settled row carries no countdown; the row being asked about still does."""
    client, _ = web_client
    helpers = _estimator_helpers(_served_inline_script(client))
    out = _run_node(DONE_DRIVER.replace("__HELPERS__", helpers), tmp_path)

    assert out["before"] is None, "a finished row must have no countdown"
    assert out["at"] is not None and _figure(out["at"]) == 12, \
        "the current row's figure is its remaining cost"
    assert out["after"] is not None and _figure(out["after"]) == 24


FORMAT_DRIVER = """
__HELPERS__
console.log(JSON.stringify({
  seconds: _subFormatDuration(45),
  justAMinute: _subFormatDuration(60),
  minute: _subFormatDuration(80),
  over: _subFormatDuration(121),
  zero: _subFormatDuration(0),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_duration_reads_minutes_and_seconds_above_a_minute(web_client, tmp_path):
    """A long batch must not read as a bare five-hundred-second number."""
    client, _ = web_client
    helpers = _estimator_helpers(_served_inline_script(client))
    out = _run_node(FORMAT_DRIVER.replace("__HELPERS__", helpers), tmp_path)

    assert out["seconds"] == "~45s"
    assert out["justAMinute"] == "~60s"
    assert out["minute"] == "~1m 20s"
    assert out["over"] == "~2m 1s"
    assert out["zero"] == "~0s"


# The regression: the served 250 ms tick itself, at two clock readings.
TICK_DRIVER = """
__HELPERS__
let CLOCK = 1000000;
Date.now = () => CLOCK;
const startTime = CLOCK;
const progress = {textContent: ''};
const spans = [];
const rows = [];
for (let i = 0; i < 3; i++) {
  const span = {textContent: ''};
  spans.push(span);
  rows.push({querySelector: (sel) => (sel === '.countdown' ? span : null)});
}
const list = {querySelectorAll: (sel) => (sel === '.sub-queue-item' ? rows : [])};
const verified = {size: 0};
const items = [1, 2, 3];
const document = {getElementById: (id) => (id === 'sub-progress' ? progress : null)};
// The new page keeps the pass's inputs here; the old page has no such object
// and its tick reads only `startTime`.
var _subEstimate = {seed: 12, observed: [], currentStartedAt: CLOCK, count: 3, done: 0};
const tick = (__CALLBACK__);
tick();
const first = spans[0].textContent;
CLOCK += 5000;
tick();
const second = spans[0].textContent;
console.log(JSON.stringify({first: first, second: second}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_drawn_countdown_decreases_across_ticks(web_client, tmp_path):
    """The served tick must draw a figure that falls as the clock advances.

    Against the pre-change page this is the elapsed-up expression
    ``Math.floor((Date.now() - startTime) / 1000)``: the first tick draws `0s`
    and the second `5s`, so the decrease assertion fails on the value the old
    code actually built.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    helpers = _estimator_helpers(script)
    callback = _extract_setinterval_callback(script, "_subScheduleIv")
    driver = (TICK_DRIVER
              .replace("__HELPERS__", helpers)
              .replace("__CALLBACK__", callback))
    out = _run_node(driver, tmp_path)

    first, second = _figure(out["first"]), _figure(out["second"])
    assert first is not None and second is not None, out
    assert second < first, (
        f"the row's figure must count down; it went {out['first']} -> {out['second']}")


# --- the figures leave no stale copy behind ---------------------------------

CLEAR_DRIVER = """
__HELPERS__
const progress = {textContent: '3 / 12 \\u00b7 ~1m 20s left'};
const spans = [{textContent: '~5s'}, {textContent: '~9s'}];
const list = {querySelectorAll: (sel) =>
  (sel === '.sub-queue-item .countdown' ? spans : [])};
global.document = {getElementById: (id) =>
  (id === 'sub-progress' ? progress : (id === 'sub-queue-list' ? list : null))};
var _subEstimate = {seed: 12, observed: [], currentStartedAt: null,
                    count: 12, done: 3};
_clearSubEstimates();
console.log(JSON.stringify({spans: spans.map((s) => s.textContent),
                            progress: progress.textContent,
                            estimate: _subEstimate}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_clearing_the_estimates_leaves_no_stale_figure(web_client, tmp_path):
    """The pass's end wipes every row's countdown and the batch figure."""
    client, _ = web_client
    helpers = _estimator_helpers(_served_inline_script(client))
    out = _run_node(CLEAR_DRIVER.replace("__HELPERS__", helpers), tmp_path)

    assert out["spans"] == ["", ""], "every row's countdown must be cleared"
    assert out["progress"] == "3 / 12", "the batch figure goes, the tally stays"
    assert out["estimate"] is None, "no estimate outlives the pass"


def test_every_pass_exit_path_clears_the_figures():
    """Cancel, the throttle stop, Clear Failed and the completion all clear.

    The clear itself is exercised above; this pins that each of the four ways a
    pass can end actually calls it, so a new exit path cannot quietly leave a
    countdown frozen on screen.
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.count("_clearSubEstimates();") >= 5, (
        "the definition's callers must cover Cancel, the throttle stop, "
        "Clear Failed and the loop's finally")
    cancel = html[html.index("document.getElementById('sub-cancel').onclick"):
                  html.index("document.getElementById('sub-clear-failed').onclick")]
    clear_failed = html[html.index("document.getElementById('sub-clear-failed').onclick"):]
    assert "_clearSubEstimates();" in cancel
    assert "_clearSubEstimates();" in clear_failed


def test_the_template_no_longer_draws_the_elapsed_up_expression():
    """The old count-up is gone, not merely bypassed."""
    html = TEMPLATE.read_text(encoding="utf-8")

    assert "(Date.now() - startTime)" not in html
    assert "const startTime = Date.now();" not in html
