"""A throttled subscription is not a failed one.

Steam answers an over-budget request with HTTP 200 and its ordinary page shell,
so the subscribe button is simply absent. The plugin used to read that as a
failure, which both recorded a failure that never happened and cleared the item
from the queue — throwing away the work the drain had queued up.
"""

import pytest

from src.database import get_queued_items, insert_or_update_item, initialize_database
from src.webserver import app, init_webserver


@pytest.fixture
def client(tmp_path):
    # The throttle timestamp is module state, so a test that reports one would
    # otherwise leak into the next.
    from src import webserver
    webserver._sub_throttled_at = 0.0
    webserver._sub_throttled_id = None

    db_path = str(tmp_path / "test_web.db")
    initialize_database(db_path)
    init_webserver(db_path, {"database": {"path": db_path}}, config_path=str(tmp_path / "c.yaml"))
    insert_or_update_item(db_path, {"workshop_id": 4242, "title": "T"})
    with app.test_client() as c:
        yield c, db_path


def _queue(db_path):
    return [row["workshop_id"] for row in get_queued_items(db_path)]


def test_a_throttled_subscription_stays_queued(client):
    """The retry path: nothing was attempted, so nothing should be discarded."""
    c, db_path = client
    from src.database import toggle_subscription_queue_status
    toggle_subscription_queue_status(db_path, 4242)
    assert _queue(db_path) == [4242]

    resp = c.post("/api/subscribe_throttled/4242")
    assert resp.status_code == 200
    assert resp.get_json()["retry_after"] > 0
    assert _queue(db_path) == [4242], "still queued, so the next drain retries it"


def test_a_failed_subscription_is_cleared_for_contrast(client):
    """The old path, kept deliberately different: a real failure is cleared."""
    c, db_path = client
    from src.database import toggle_subscription_queue_status
    toggle_subscription_queue_status(db_path, 4242)

    c.post("/api/subscribe_failed/4242")
    assert _queue(db_path) == []


def test_sub_health_reports_the_throttle(client):
    c, _ = client
    assert c.get("/api/sub_health").get_json()["throttled_at"] == 0.0

    c.post("/api/subscribe_throttled/4242")
    health = c.get("/api/sub_health").get_json()
    assert health["throttled_id"] == 4242
    assert health["throttled_at"] > 0
    assert health["retry_after"] > 0


def test_the_plugin_reports_throttling_rather_than_failure():
    """The plugin has to tell the two apart, or the server cannot."""
    from pathlib import Path
    js = Path("userscripts/steam_subscribe.user.js").read_text(encoding="utf-8")
    assert "too many requests" in js.lower()
    assert "/api/subscribe_throttled/" in js


# --- the pause must be released --------------------------------------------

def _throttle_body():
    """The source of `_checkSubThrottle`, brace-matched.

    It is nested inside `_startAutoSubscribe`, so the end cannot be found by
    indentation: an early return inside the function is also a line at the
    function body's own indent level, and a slice to the first such line stops
    before the release this test exists to pin.
    """
    from pathlib import Path
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
    the tabs early means the remaining ones never are.
    """
    body = _throttle_body()
    assert "_subThrottleStopped = true" in body
    assert "await fetch('/api/resume'" in body, "the early stop must release the pause"
    assert "clearInterval(_subPollIv)" in body, "and stop the poll that would have resumed it"


def test_a_throttle_stop_leaves_the_queue_intact():
    """The retry depends on the items surviving. The Close path dequeues them."""
    from pathlib import Path
    html = Path("templates/index.html").read_text(encoding="utf-8")
    close = html[html.index("sub-cancel').onclick"):]
    close = close[:close.index("sub-clear-failed")]
    assert "if (!_subThrottleStopped)" in close, "Close must not dequeue a throttled pass"
    assert "fetch('/api/resume'" in close


def test_the_drain_pauses_the_daemon_for_its_duration():
    """The control this all depends on: a subscribe pass owns the daemon."""
    from pathlib import Path
    html = Path("templates/index.html").read_text(encoding="utf-8")
    assert "await fetch('/api/pause'" in html
    src = Path("src/web_worker.py").read_text(encoding="utf-8")
    assert "os.path.exists(self.pause_lock_file)" in src
    img = Path("src/image_worker.py").read_text(encoding="utf-8")
    assert "os.path.exists(self.pause_lock_file)" in img
