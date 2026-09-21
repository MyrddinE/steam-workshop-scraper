"""The drain's throttle check, and the pause it must release.

Steam answers an over-budget request with HTTP 200 and its ordinary page shell,
so the subscribe button is simply absent. The userscript bridge used to report
that to the server through `POST /api/subscribe_throttled/<id>`; the report and
the userscript went with the bridge. What remains is the browser-free drain's own
behaviour: it reads `GET /api/subscribe_throttle` before each item, stops the
pass when a throttle is recorded, and must release the daemon pause it took for
the pass on that early stop.
"""

from pathlib import Path


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
