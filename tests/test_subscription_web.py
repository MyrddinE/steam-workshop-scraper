"""The subscription marker's web side: the payload, the endpoints, and the click.

Four things are pinned here, all of which the old two-indicator shape got
wrong:

* the read endpoints carry the marker's state and its whole appearance, computed
  from `src/subscription.py`, so the page owns no copy of the glyph/colour table;
* ``POST /api/subscribed`` and ``POST /api/unsubscribed`` are the outcome stamps:
  each writes its own state and clears the queue flag, and the removal stamp
  leaves the sticky first-seen time so the marker reads ``previously``;
* ``POST /api/dequeue`` is the direction-agnostic dequeue Cancel and Clear Failed
  post -- it clears the flag and records no outcome, where the old
  ``/api/subscribed`` call claimed a subscription for a row never attempted;
* the marker's click routes every actionable state through the existing toggle
  route and sends nothing for ``downloaded`` (the owner reversed ``subscribed``'s
  old inertness, because its click only queues a removal).

The click and the template are exercised by running the served JavaScript under
node, so the assertions are about what the page actually builds rather than what
its source happens to contain.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.webserver import app, init_webserver
from src.database import get_connection, initialize_database, insert_or_update_item
from src import subscription
from src import workshop_folders
import src.webserver as webserver

TEMPLATE = Path("templates/index.html")
NODE = shutil.which("node")


@pytest.fixture
def web_client(tmp_path):
    db_path = str(tmp_path / "test_web.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "daemon": {"target_appids": [294100]}}
    init_webserver(db_path, config)
    return app.test_client(), db_path


def _row(db_path, wid):
    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM workshop_items WHERE workshop_id = ?", (wid,)).fetchone()
    conn.close()
    return dict(row)


# --- the read payload -------------------------------------------------------

def test_the_item_payload_carries_the_whole_marker(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "T", "fetch_status": 200})

    item = client.get('/api/item/1').get_json()

    assert item["subscription_state"] == subscription.NEVER
    assert item["subscription_glyph"] == subscription.glyph(subscription.NEVER)
    assert item["subscription_colour"] == subscription.colour(subscription.NEVER)
    assert item["subscription_class"] == subscription.marker_spec(subscription.NEVER)[2]
    assert item["subscription_label"] == subscription.marker_spec(subscription.NEVER)[3]
    assert item["subscription_tooltip"] == subscription.tooltip(subscription.NEVER)
    assert item["subscription_clickable"] is True
    assert item["subscription_action"] == "subscribe"
    assert item["subscription_action_label"] == "Subscribe"


def test_the_payload_words_a_queued_removal_as_an_unsubscribe(web_client):
    """The pane's control must not say Subscribe when its press removes."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 8, "title": "T", "fetch_status": 200,
                                    "own_subscribed": 1, "is_queued_for_subscription": 1})

    item = client.get('/api/item/8').get_json()

    assert item["subscription_state"] == subscription.QUEUED_REMOVE
    assert item["subscription_action"] == "cancel_remove"
    assert item["subscription_action_label"] == "Cancel Unsubscribe"


def test_the_payload_words_a_plain_subscription_as_an_unsubscribe(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 8, "title": "T", "fetch_status": 200,
                                    "own_subscribed": 1, "own_first_subscribed_at": 1000})

    item = client.get('/api/item/8').get_json()

    assert item["subscription_state"] == subscription.SUBSCRIBED
    assert item["subscription_action"] == "queue_remove"
    assert item["subscription_action_label"] == "Unsubscribe"


@pytest.mark.parametrize("columns,state", [
    ({"own_subscribed": 1, "steam_download_seen_at": 1000}, subscription.DOWNLOADED),
    ({"own_subscribed": 1, "own_first_subscribed_at": 1000}, subscription.SUBSCRIBED),
    ({"is_queued_for_subscription": 1}, subscription.QUEUED),
    ({"own_subscribed": 1, "is_queued_for_subscription": 1}, subscription.QUEUED_REMOVE),
    ({"own_first_subscribed_at": 1000}, subscription.PREVIOUSLY),
    ({}, subscription.NEVER),
])
def test_the_item_payload_derives_every_state(web_client, columns, state):
    client, db_path = web_client
    insert_or_update_item(db_path, dict({"workshop_id": 7, "title": "T", "fetch_status": 200},
                                        **columns))

    item = client.get('/api/item/7').get_json()

    assert item["subscription_state"] == state
    assert item["subscription_glyph"] == subscription.glyph(state)
    assert item["subscription_colour"] == subscription.colour(state)
    assert item["subscription_clickable"] is subscription.is_clickable(state)


def test_the_search_payload_carries_the_marker_for_every_cell(web_client):
    """The grid draws its marker from the search rows, so they must carry it."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "A", "fetch_status": 200,
                                    "own_subscribed": 1, "own_first_subscribed_at": 5})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "B", "fetch_status": 200,
                                    "is_queued_for_subscription": 1})

    rows = client.post('/api/search', json={"limit": 10}).get_json()

    by_id = {r["workshop_id"]: r for r in rows}
    assert by_id[1]["subscription_state"] == subscription.SUBSCRIBED
    # The owner reversed the old inertness: a subscribed marker now queues a
    # removal, so it is clickable.
    assert by_id[1]["subscription_clickable"] is True
    assert by_id[2]["subscription_state"] == subscription.QUEUED
    # own_first_subscribed_at travels with the row, or a cell toggled away from
    # `queued` could not tell `never` from `previously`.
    assert by_id[1]["own_first_subscribed_at"] == 5


def test_the_items_payload_carries_the_marker(web_client):
    """The list poll updates markers in place from this route."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 3, "title": "C", "fetch_status": 200,
                                    "own_first_subscribed_at": 42})

    rows = client.post('/api/items', json={"ids": [3]}).get_json()

    assert rows[0]["subscription_state"] == subscription.PREVIOUSLY
    assert rows[0]["subscription_glyph"] == subscription.glyph(subscription.PREVIOUSLY)


def test_the_items_payload_carries_the_downloaded_latch(web_client):
    """Without the latch on the payload a subscribed cell could only ever be yellow."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 3, "title": "C", "fetch_status": 200,
                                    "own_subscribed": 1, "steam_download_seen_at": 1000})

    rows = client.post('/api/items', json={"ids": [3]}).get_json()

    assert rows[0]["steam_download_seen_at"] == 1000
    assert rows[0]["subscription_state"] == subscription.DOWNLOADED
    assert rows[0]["subscription_glyph"] == subscription.glyph(subscription.DOWNLOADED)


def test_the_queued_payload_carries_the_whole_marker(web_client):
    """The queue overlay is a marker surface too, so /api/queued derives it.

    A queued + subscribed row is the derived removal direction, and it outranks
    the download latch: the pending removal must stay visible over the green
    star, so the overlay draws the empty red outline rather than `downloaded`.
    """
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 4, "title": "D", "fetch_status": 200,
                                    "is_queued_for_subscription": 1,
                                    "own_subscribed": 1, "steam_download_seen_at": 1000})

    rows = client.get('/api/queued').get_json()

    assert rows[0]["subscription_state"] == subscription.QUEUED_REMOVE
    assert rows[0]["subscription_glyph"] == subscription.glyph(subscription.QUEUED_REMOVE)
    assert rows[0]["subscription_colour"] == subscription.colour(subscription.QUEUED_REMOVE)
    assert rows[0]["subscription_tooltip"] == subscription.tooltip(subscription.QUEUED_REMOVE)


# --- opening a downloaded item's folder -------------------------------------


def _enable_open_folder(monkeypatch, db_path, content_dir, launcher, *,
                        platform="win32"):
    """Point the server's global helper at a platform build with a fake launcher.

    ``platform`` defaults to the Windows host the feature is for. Passing a
    non-Windows value is how the off-Windows tests build the "off" half from the
    same seam, so they assert the non-Windows behaviour on every host rather than
    only on a non-Windows one. No test may open Explorer: the launcher is always
    injected, and the folder is checked against a temp content directory rather
    than a real Steam library.
    """
    service = workshop_folders.WorkshopFolders(
        db_path, {"steam": {"workshop_content_dirs": [str(content_dir)]}},
        platform=platform, launcher=launcher)
    monkeypatch.setattr(webserver, "_workshop_folders", service)
    return service


def _green_item(db_path, wid=7, *, content_dir, make_folder=True):
    insert_or_update_item(db_path, {
        "workshop_id": wid, "title": "T", "fetch_status": 200, "consumer_appid": 294100,
        "own_subscribed": 1, "steam_download_seen_at": 1000,
    })
    if make_folder:
        (content_dir / "294100" / str(wid)).mkdir(parents=True)


def test_open_folder_refuses_off_windows(web_client, monkeypatch, tmp_path):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 7, "title": "T", "fetch_status": 200,
                                    "own_subscribed": 1, "steam_download_seen_at": 1000})
    _enable_open_folder(monkeypatch, db_path, tmp_path / "content", lambda path: None,
                        platform="linux")

    resp = client.post('/api/open_folder/7')

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "Windows" in body["message"]


def test_open_folder_refuses_an_item_that_is_not_downloaded(web_client, monkeypatch,
                                                            tmp_path):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 7, "title": "T", "fetch_status": 200,
                                    "consumer_appid": 294100, "own_subscribed": 1})
    launched = []
    _enable_open_folder(monkeypatch, db_path, tmp_path / "content", launched.append)

    resp = client.post('/api/open_folder/7')

    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert launched == []


def test_open_folder_warns_and_changes_nothing_when_the_folder_is_gone(
        web_client, monkeypatch, tmp_path):
    client, db_path = web_client
    content = tmp_path / "content"
    _green_item(db_path, content_dir=content, make_folder=False)
    launched = []
    _enable_open_folder(monkeypatch, db_path, content, launched.append)

    resp = client.post('/api/open_folder/7')

    body = resp.get_json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert str(content) in body["message"], "the warning names where it looked"
    assert launched == [], "nothing may be launched into an error"
    assert _row(db_path, 7)["steam_download_seen_at"] == 1000, "the refusal changes no state"


def test_open_folder_launches_the_folder_on_the_server_host(web_client, monkeypatch,
                                                            tmp_path):
    client, db_path = web_client
    content = tmp_path / "content"
    _green_item(db_path, content_dir=content)
    launched = []
    _enable_open_folder(monkeypatch, db_path, content, launched.append)

    resp = client.post('/api/open_folder/7')

    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert launched == [str(content / "294100" / "7")]
    assert _row(db_path, 7)["steam_download_seen_at"] == 1000


def test_open_folder_reports_an_unknown_item(web_client, monkeypatch, tmp_path):
    client, db_path = web_client
    launched = []
    _enable_open_folder(monkeypatch, db_path, tmp_path / "content", launched.append)

    resp = client.post('/api/open_folder/999')

    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert launched == []


def test_the_open_folder_control_is_rendered_only_on_windows(web_client, monkeypatch,
                                                             tmp_path):
    client, db_path = web_client

    # The "off" half is the non-Windows host, injected through the same seam as
    # the "on" half -- otherwise the assertion only holds when the suite itself
    # runs off Windows.
    _enable_open_folder(monkeypatch, db_path, tmp_path / "content", lambda path: None,
                        platform="linux")
    off = client.get('/').data.decode()
    assert '<button id="btn-open-folder"' not in off
    assert "e.key === 'o'" not in off, "off Windows the shortcut is not bound"

    _enable_open_folder(monkeypatch, db_path, tmp_path / "content", lambda path: None)
    on = client.get('/').data.decode()
    assert '<button id="btn-open-folder"' in on
    assert "e.key === 'o'" in on
    assert "open_folder_enabled" not in on
    assert "><u>O</u>pen Folder (not downloaded)</button>" in on, \
        "the initial render carries the `o` hint, disabled variant included"


# --- the stamp --------------------------------------------------------------

def test_api_subscribed_stamps_the_subscription_and_clears_the_queue(web_client):
    """The outcome stamp for a confirmed add: mark it and clear the queue flag."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "fetch_status": 200,
                                    "is_queued_for_subscription": 1})

    assert client.post('/api/subscribed/9').get_json() == {"ok": True}

    row = _row(db_path, 9)
    assert row["own_subscribed"] == 1
    assert row["own_first_subscribed_at"] is not None, \
        "the first-seen stamp is what makes `previously` possible later"
    assert row["is_queued_for_subscription"] == 0


def test_api_subscribed_does_not_move_an_existing_stamp(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "fetch_status": 200,
                                    "own_first_subscribed_at": 1000})

    client.post('/api/subscribed/9')

    assert _row(db_path, 9)["own_first_subscribed_at"] == 1000


def test_api_subscribed_makes_the_marker_subscribed(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "fetch_status": 200})

    client.post('/api/subscribed/9')

    assert client.get('/api/item/9').get_json()["subscription_state"] == subscription.SUBSCRIBED


def test_api_unsubscribed_is_the_removal_outcome_stamp(web_client):
    """The counterpart of /api/subscribed: clear the subscription, keep history."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "fetch_status": 200,
                                    "own_subscribed": 1, "is_queued_for_subscription": 1,
                                    "own_first_subscribed_at": 1000})

    assert client.post('/api/unsubscribed/9').get_json() == {"ok": True}

    row = _row(db_path, 9)
    assert row["own_subscribed"] == 0
    assert row["is_queued_for_subscription"] == 0
    assert row["own_first_subscribed_at"] == 1000, "the sticky stamp is the history"
    assert client.get('/api/item/9').get_json()["subscription_state"] \
        == subscription.PREVIOUSLY


def test_api_unsubscribed_makes_the_marker_previously(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "fetch_status": 200,
                                    "own_subscribed": 1, "own_first_subscribed_at": 1000})

    client.post('/api/unsubscribed/9')

    assert client.get('/api/item/9').get_json()["subscription_state"] \
        == subscription.PREVIOUSLY


def test_api_dequeue_clears_only_the_queue_flag(web_client):
    """Cancel and Clear Failed must not stamp a subscription or a removal."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "fetch_status": 200,
                                    "is_queued_for_subscription": 1})

    assert client.post('/api/dequeue/9').get_json() == {"ok": True}

    row = _row(db_path, 9)
    assert row["is_queued_for_subscription"] == 0
    assert row["own_subscribed"] == 0, "a cancelled add is not a subscription"
    assert row["own_first_subscribed_at"] is None, \
        "the sticky stamp must not be written for a row the drain never attempted"


def test_api_dequeue_does_not_stamp_a_cancelled_removal_either(web_client):
    """The removal direction is dequeued without claiming an unsubscribe."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "fetch_status": 200,
                                    "own_subscribed": 1, "is_queued_for_subscription": 1,
                                    "own_first_subscribed_at": 1000})

    client.post('/api/dequeue/9')

    row = _row(db_path, 9)
    assert row["is_queued_for_subscription"] == 0
    assert row["own_subscribed"] == 1, "a cancelled removal is still subscribed"
    assert row["own_first_subscribed_at"] == 1000


def test_toggle_subscription_queue_still_only_flips_the_queue_flag(web_client):
    """/api/toggle_subscription_queue is unchanged: it is the route both directions use."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "fetch_status": 200})

    client.post('/api/toggle_subscription_queue/9')
    assert _row(db_path, 9)["is_queued_for_subscription"] == 1
    assert _row(db_path, 9)["own_subscribed"] == 0

    client.post('/api/toggle_subscription_queue/9')
    assert _row(db_path, 9)["is_queued_for_subscription"] == 0


# --- the served click routing -----------------------------------------------

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


def _run_node(driver: str, tmp_path):
    path = tmp_path / "driver.js"
    path.write_text(driver, encoding="utf-8")
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                            encoding="utf-8")
    assert result.returncode == 0, f"node driver failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


SUB_CLICK_DRIVER = """
const fn = (__FN__);
const calls = [];
let stopped = 0;
global.toggleDetailQueue = (wid) => calls.push(wid);
function el(state, clickable, wid, text) {
  const attrs = {state: state, clickable: clickable, wid: String(wid)};
  return {textContent: text, getAttribute: (k) =>
    (k === 'data-sub-state' ? attrs.state
      : k === 'data-sub-clickable' ? attrs.clickable
      : k === 'data-wid' ? attrs.wid : null)};
}
const event = {stopPropagation: () => { stopped += 1; }};

// Every state, clicked. `downloaded` is the one inert state.
fn(event, el('downloaded', '0', 0, '\\u2605'));
fn(event, el('subscribed', '1', 1, '\\u2605'));
fn(event, el('queued_remove', '1', 2, '\\u2606'));
fn(event, el('queued', '1', 3, '\\u2606'));
fn(event, el('previously', '1', 4, '\\u2606'));
fn(event, el('never', '1', 5, '\\u25cb'));
console.log(JSON.stringify({calls: calls, stopped: stopped}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_clicking_the_marker_routes_every_actionable_state(web_client, tmp_path):
    """downloaded sends nothing; every other state toggles the queue; none bubbles.

    The owner reversed the old rule that `subscribed` was inert: a click there
    only queues a removal, and the queue cancels, so it is recoverable. The new
    `queued_remove` state toggles too, which is how a queued removal is
    cancelled. The cell underneath opens the detail pane, so a marker click that
    did not stop propagating would toggle the queue *and* drag the pane to the
    item.
    """
    client, _ = web_client
    fn = _extract_function(_served_inline_script(client), "onSubMarkerClick")
    result = _run_node(SUB_CLICK_DRIVER.replace("__FN__", fn), tmp_path)

    assert result["calls"] == [1, 2, 3, 4, 5], \
        "every state but downloaded must toggle; downloaded must send nothing"
    assert result["stopped"] == 6, \
        "every marker click must stop propagation, including the inert one"


# --- the grid cell follows a subscribe that lands ---------------------------
#
# /api/subscribed/<id> stamps `own_subscribed` and clears the queue flag
# immediately, but the cell is only re-read by _startListPoll. A row queued only
# for subscription has no stage spinner, so the poll's id set used to miss it
# and the cell kept the green `queued` marker after the subscribe had landed.
# These drive the served functions under node: what the poll actually asks
# /api/items for, and whether the poll is started for such a row at all.

POLL_NEEDS_DRIVER = """
const _pendingStage = (__STAGE__);
const listNeedsPoll = (__LIST__);
const item = (over) => Object.assign(
  {image_priority: 0, image_answer: 'jpg', translation_priority: 0, web_scrape_priority: 0},
  over);
console.log(JSON.stringify({
  stage: listNeedsPoll([item({web_scrape_priority: 5})]),
  subscription: listNeedsPoll([item({subscription_state: 'queued'})]),
  removal: listNeedsPoll([item({subscription_state: 'queued_remove'})]),
  subscribed: listNeedsPoll([item({subscription_state: 'subscribed'})]),
  never: listNeedsPoll([item({subscription_state: 'never'})]),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_row_queued_only_for_subscription_is_polled(web_client, tmp_path):
    """A row with no stage spinner still has a marker that can move.

    The subscription marker changes the moment a subscribe -- or a removal --
    lands, so the poll must be started for a row whose only outstanding state is
    the queue entry, in either direction. Otherwise the cell is never re-read and
    keeps the pending marker.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (POLL_NEEDS_DRIVER
              .replace("__STAGE__", _extract_function(script, "_pendingStage"))
              .replace("__LIST__", _extract_function(script, "_listNeedsPoll")))
    result = _run_node(driver, tmp_path)

    assert result["stage"] is True, "a stage spinner still starts the poll"
    assert result["subscription"] is True, \
        "a row queued only for subscription must start the poll too"
    assert result["removal"] is True, \
        "a row queued for removal must be watched the same way"
    assert result["subscribed"] is False, "nothing to re-read once subscribed"
    assert result["never"] is False, "nothing to re-read when settled"


POLL_SCOPE_DRIVER = """
let _listPollTimer = null;
const _stopListPoll = () => {};
// The tick hands every block to the one dispatch point; this driver is about
// the ids it asks for, so the dispatch is stubbed rather than run. The tick
// itself is a top-level function beside `_startListPoll` -- named so the UI
// trace can wrap it -- so both are served into the driver.
const dispatchItemUpdates = () => {};
__FNS__
const startFn = _startListPoll;

function marker(state) {
  return {getAttribute: (k) => (k === 'data-sub-state' ? state : null)};
}
function cell(wid, classes, subState) {
  const set = new Set(classes);
  return {
    wid: wid,
    classList: {contains: (c) => set.has(c)},
    querySelector: (sel) => (sel === '.grid-sub' && subState ? marker(subState) : null),
    getAttribute: (k) => (k === 'data-wid' ? String(wid) : null),
  };
}
const cells = [
  cell(11, ['grid-cell', 'has-spinner'], null),   // a stage marker
  cell(22, ['grid-cell'], 'queued'),             // queued only for subscription
  cell(33, ['grid-cell'], 'subscribed'),          // settled
  cell(44, ['grid-cell'], 'queued_remove'),       // queued for removal
];
// The selectors the poll uses: '.grid-cell[data-wid]' and the old
// '.grid-cell.has-spinner[data-wid]'. Emulated so the driver fails on the id
// the page asks for, not on the fake's selector support.
function matches(c, sel) {
  if (sel.indexOf('.grid-cell') !== -1 && !c.classList.contains('grid-cell')) return false;
  if (sel.indexOf('.has-spinner') !== -1 && !c.classList.contains('has-spinner')) return false;
  if (sel.indexOf('[data-wid]') !== -1 && c.getAttribute('data-wid') == null) return false;
  return true;
}
const grid = {querySelectorAll: (sel) => cells.filter((c) => matches(c, sel))};
global.document = {getElementById: () => grid};
let scheduled = null;
global.setTimeout = (fn) => { scheduled = fn; return 1; };
let fetched = null;
global.fetch = async (url, opts) => {
  fetched = JSON.parse(opts.body).ids;
  return {json: async () => []};
};
(async () => {
  startFn([], '', '');
  await scheduled();
  console.log(JSON.stringify({fetched: fetched}));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_poll_re_reads_a_row_queued_only_for_subscription(web_client, tmp_path):
    """The poll's id set must not be limited to rows with a stage spinner.

    Driving one tick shows the ids the page actually posts to /api/items: the
    spinner row and both queued directions, and not the settled one. Refreshing
    the cell here rather than at each writer is what stops one write path -- the
    page's own cancel/clear handlers, the direct /api/subscribe route -- being
    able to bypass the refresh.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    fns = "\n".join(_extract_function(script, name)
                    for name in ("_listPollTick", "_startListPoll"))
    driver = POLL_SCOPE_DRIVER.replace("__FNS__", fns)
    result = _run_node(driver, tmp_path)

    assert result["fetched"] and sorted(result["fetched"]) == [11, 22, 44], \
        "the poll must re-read the spinner row and both queued directions"


TOGGLE_STARTS_POLL_DRIVER = """
const _applySub = () => {};
// The toggle's read-back goes through the one dispatch point; this driver is
// about the poll it starts, so the dispatch is stubbed.
global.dispatchItemUpdate = () => {};
let started = 0;
var _startListPoll = () => { started += 1; };
const states = ['queued', 'queued_remove', 'never'];
let step = 0;
global._currentDetail = {workshop_id: 77};
global.renderDetail = () => {};
global.alert = () => {};
global.document = {querySelector: () => null};
global.fetch = async (url) => {
  if (url.indexOf('/api/toggle_subscription_queue/') === 0) {
    return {ok: true, status: 200, statusText: 'OK'};
  }
  if (url.indexOf('/api/item/') === 0) {
    const state = states[Math.min(step, states.length - 1)];
    step += 1;
    return {ok: true, status: 200, statusText: 'OK',
            json: async () => ({workshop_id: 77, subscription_state: state})};
  }
  throw new Error('unexpected url ' + url);
};
const fn = (__FN__);
(async () => {
  await fn(77);
  const afterQueue = started;
  await fn(77);
  const afterRemoval = started;
  await fn(77);
  const afterSettled = started;
  console.log(JSON.stringify({afterQueue: afterQueue, afterRemoval: afterRemoval,
                              afterSettled: afterSettled}));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_queueing_a_row_starts_the_poll(web_client, tmp_path):
    """A row queued after the search rendered must be watched too, both ways.

    _listNeedsPoll only runs when a batch is rendered, so a marker clicked into
    `queued` (or into the removal direction) afterwards would otherwise never be
    re-read and the outcome landing would leave the stale marker the poll exists
    to clear. A settled read must not start it again.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = TOGGLE_STARTS_POLL_DRIVER.replace(
        "__FN__", _extract_function(script, "toggleDetailQueue"))
    result = _run_node(driver, tmp_path)

    assert result["afterQueue"] == 1, "queueing a marker must start the list poll"
    assert result["afterRemoval"] == 2, \
        "queueing a removal must start the poll as well"
    assert result["afterSettled"] == 2, "a settled read must not start it again"


# --- the template's own source ----------------------------------------------

def test_the_template_has_one_subscription_indicator():
    """The old `queued` class and its star must be gone, not merely unused."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert ".queued .grid-title::before" not in html
    assert "classList.add('queued')" not in html
    assert "classList.toggle('queued'" not in html
    assert "grid-title::before" not in html


def test_the_grid_marker_is_top_right_and_declares_its_placement():
    html = TEMPLATE.read_text(encoding="utf-8")
    rule_start = html.index(".grid-cell .grid-sub {")
    rule = html[rule_start:html.index("}", rule_start)]
    assert "position: absolute" in rule
    assert "top:" in rule and "right:" in rule


def test_the_marker_is_inside_the_cell_that_opens_the_pane():
    """The placement is what makes the stopPropagation necessary."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert '<div class="grid-sub"></div>' in html
    assert "onSubMarkerClick(event, this)" in html
    assert "div.onclick = () => showDetail" in html


def test_the_pane_has_one_direction_aware_control_and_no_queue_button_pair():
    """The old Queue/Unqueue pair is gone; the marker is still the indicator.

    The pane's one control is rendered by `subscriptionControl`, which takes its
    word and its handler from the item's derived direction on the payload, so it
    can say Unsubscribe for a subscribed item without a Queue/Unqueue pair
    reappearing.
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    assert ">Queue</button>" not in html
    assert ">Unqueue</button>" not in html
    assert "subscriptionControl(item)" in html
    assert "subscription_action_label" in html
    # The marker helper is what the pane calls for the indicator.
    assert "showSubscriptionMarker(item)" in html


PANE_CONTROL_DRIVER = """
const _escapeHtml = (__ESC__);
const _hintKey = (__HINT__);
const fn = (__FN__);
const never = fn({workshop_id: 1, subscription_action: 'subscribe',
                  subscription_action_label: 'Subscribe'});
const subscribed = fn({workshop_id: 2, subscription_action: 'queue_remove',
                       subscription_action_label: 'Unsubscribe'});
const removal = fn({workshop_id: 3, subscription_action: 'cancel_remove',
                    subscription_action_label: 'Cancel Unsubscribe'});
// A payload without the new fields (an older block) must fall back safely.
const legacy = fn({workshop_id: 4});
console.log(JSON.stringify(
  {never: never, subscribed: subscribed, removal: removal, legacy: legacy}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_pane_control_words_and_wires_the_derived_direction(web_client, tmp_path):
    """Each state's control says what its press is, and calls the right route.

    An unsubscribed item's press subscribes directly; a subscribed item's press
    queues its removal; a queued removal's press cancels it. The label comes
    from the shared table on the payload, never from a literal in the page.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    out = _run_node(PANE_CONTROL_DRIVER
                    .replace("__ESC__", _extract_function(script, "_escapeHtml"))
                    .replace("__HINT__", _extract_function(script, "_hintKey"))
                    .replace("__FN__", _extract_function(script, "subscriptionControl")),
                    tmp_path)

    assert "doSubscribe(1)" in out["never"]
    assert "><u>S</u>ubscribe</button>" in out["never"], \
        "the `s` hint marks the label's own key letter"
    assert "toggleDetailQueue(2)" in out["subscribed"], \
        "a subscribed item's control queues the removal, never acting on Steam"
    assert ">Un<u>s</u>ubscribe</button>" in out["subscribed"]
    assert "toggleDetailQueue(3)" in out["removal"]
    assert ">Cancel Un<u>s</u>ubscribe</button>" in out["removal"]
    assert "doSubscribe(4)" in out["legacy"], "an older payload falls back safely"


def test_the_overlay_rows_name_the_direction_and_the_official_dequeue():
    """The drain's rows read the direction; Cancel records no outcome.

    The row carries the derived direction and a verb that reads
    ``unsubscribing…`` / ``unsubscribed`` for a removal, and Cancel and Clear
    Failed post the direction-agnostic ``/api/dequeue`` rather than the outcome
    stamp that used to claim a subscription.
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "data-remove=" in html
    assert "sub-queue-verb" in html
    assert "unsubscribing…" in html
    assert "'unsubscribed' : 'subscribed'" in html
    assert "fetch('/api/dequeue/' + wid" in html
    assert "fetch('/api/subscribed/' + wid" not in html


# --- the web-only key hints for `o`, `s` and `i` ----------------------------
#
# The TUI lists every binding in its footer, so the hints are web-only, and only
# for the three keys whose labels the web page owns. One helper builds all of
# them, so the marking rule cannot differ per surface; the subscription label is
# server-provided, so the helper escapes the whole label before it attaches any
# markup of its own.

HINT_KEY_DRIVER = """
const _escapeHtml = (__ESC__);
const fn = (__FN__);
console.log(JSON.stringify({
  open: fn('Open Folder', 'o'),
  unsubscribe: fn('Unsubscribe', 's'),
  unignore: fn('Unignore', 'i'),
  escaped: fn('<b>Open</b>', 'o'),
  amp: fn('Tom & Jerry', 'o'),
  absent: fn('Remove', 's'),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_key_hint_marks_the_first_key_letter_and_escapes(web_client, tmp_path):
    """One helper marks the first occurrence of the key in an escaped label.

    `Unsubscribe` must mark its own `s`, not the leading `U` a naive
    "first letter" rule would pick. A label with no such letter gets the key in
    brackets, so the hint is never silently absent. And because the subscription
    label is built on the server, a `<` in it must come out escaped rather than
    as live markup.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    out = _run_node(HINT_KEY_DRIVER
                    .replace("__ESC__", _extract_function(script, "_escapeHtml"))
                    .replace("__FN__", _extract_function(script, "_hintKey")),
                    tmp_path)

    assert out["open"] == "<u>O</u>pen Folder"
    assert out["unsubscribe"] == "Un<u>s</u>ubscribe", \
        "the mark goes on the key letter, not the label's first letter"
    assert out["unignore"] == "Un<u>i</u>gnore"
    assert out["escaped"] == "&lt;b&gt;<u>O</u>pen&lt;/b&gt;", \
        "a server-provided `<` must not become live markup"
    assert out["amp"] == "T<u>o</u>m &amp; Jerry"
    assert out["absent"] == "Remove (S)", \
        "a label without the key letter carries the key in brackets"


OPEN_FOLDER_LABEL_DRIVER = """
const _escapeHtml = (__ESC__);
const _hintKey = (__HINT__);
const fn = (__FN__);
const btn = {disabled: null, innerHTML: '', textContent: '', title: ''};
global.document = {getElementById: (id) => (id === 'btn-open-folder' ? btn : null)};
fn({workshop_id: 7, subscription_state: 'downloaded'});
const green = {label: btn.innerHTML, disabled: btn.disabled, title: btn.title};
fn({workshop_id: 7, subscription_state: 'subscribed'});
const grey = {label: btn.innerHTML, disabled: btn.disabled, title: btn.title};
console.log(JSON.stringify({green: green, grey: grey}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_open_folder_button_keeps_its_hint_in_both_states(web_client, tmp_path):
    """The update path writes the hint markup, disabled variant included.

    The button is visible-but-disabled for anything not downloaded, and the
    owner's point is discoverability *with* the reason, so the disabled label
    keeps its hint too. The update path therefore writes `innerHTML` through the
    one helper rather than the old plain `textContent`.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    out = _run_node(OPEN_FOLDER_LABEL_DRIVER
                    .replace("__ESC__", _extract_function(script, "_escapeHtml"))
                    .replace("__HINT__", _extract_function(script, "_hintKey"))
                    .replace("__FN__",
                             _extract_function(script, "_refreshOpenFolderButton")),
                    tmp_path)

    assert out["green"]["label"] == "<u>O</u>pen Folder"
    assert out["grey"]["label"] == "<u>O</u>pen Folder (not downloaded)", \
        "the disabled variant keeps its hint"
    assert out["green"]["disabled"] is False and out["grey"]["disabled"] is True
    assert out["green"]["title"] != out["grey"]["title"]


OPEN_FOLDER_KEY_DRIVER = """
const _openFolderShortcut = (__HELPER__);
const fn = (__FN__);
const calls = {opened: [], fromDetail: 0, prevented: 0};
const cell = {
  classList: {contains: (c) => c === 'grid-cell'},
  getAttribute: (n) => (n === 'data-wid' ? '11' : null),
};
const paneChild = {};
const filterInput = {};
const pane = {contains: (el) => el === paneChild};
const bar = {contains: () => false};
const grid = {contains: (el) => el === cell, querySelectorAll: () => [cell]};
let active = cell;
global.document = {
  getElementById: (id) => (id === 'results-grid' ? grid
    : id === 'detail-pane' ? pane : bar),
  get activeElement() { return active; },
};
global.openFolder = (wid) => calls.opened.push(wid);
global.openFolderFromDetail = () => { calls.fromDetail += 1; };
global.toggleDetailQueue = () => {};
global.toggleIgnoredItem = () => {};
global._focusGridCell = () => {};
global._startAutoSubscribe = () => {};
function press() { fn({key: 'o', preventDefault: () => { calls.prevented += 1; }}); }

press();
const onCell = {opened: calls.opened.slice(), fromDetail: calls.fromDetail};
active = paneChild;
press();
const inPane = {opened: calls.opened.slice(), fromDetail: calls.fromDetail};
active = filterInput;
press();
console.log(JSON.stringify({onCell: onCell, inPane: inPane, prevented: calls.prevented,
                            opened: calls.opened, fromDetail: calls.fromDetail}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_o_key_opens_the_focused_cell_or_the_panes_item_once(web_client, monkeypatch,
                                                                 tmp_path):
    """`o` is app-level: the focused cell's item, or the pane's, exactly once.

    The pane carries the subscription button and `#detail-buttons` carries Open
    Folder, so focus is routinely inside the detail surface. The key must route
    there without also pressing a grid cell -- one keypress, one action. A filter
    input is deliberately not a detail surface: `o` typed into one is that
    input's character, not a request to open a folder.
    """
    client, db_path = web_client
    _enable_open_folder(monkeypatch, db_path, tmp_path / "content", lambda path: None)
    script = _served_inline_script(client)
    out = _run_node(OPEN_FOLDER_KEY_DRIVER
                    .replace("__HELPER__", _extract_function(script, "_openFolderShortcut"))
                    .replace("__FN__", _extract_function(script, "_onGridKeydown")),
                    tmp_path)

    assert out["onCell"] == {"opened": [11], "fromDetail": 0}, \
        "a focused cell opens its own item"
    assert out["inPane"] == {"opened": [11], "fromDetail": 1}, \
        "the pane opens the item it shows, and the cell is not acted on as well"
    assert out["prevented"] == 2, \
        "the shortcut acts twice, and leaves an unrelated input's key alone"
    assert out["opened"] == [11] and out["fromDetail"] == 1, \
        "each press opened exactly one folder"


IGNORE_BUTTON_ROUTE_DRIVER = """
global.toggleIgnoredItem = (__TOGGLE__);
const wrap = (__WRAP__);
const keyFn = (__KEY__);
const calls = {posts: []};
global.dispatchItemUpdate = () => {};
global.alert = () => {};
global._focusGridCell = () => {};
global.toggleDetailQueue = () => {};
global._startAutoSubscribe = () => {};
global._currentDetail = {workshop_id: 7};
global.fetch = async (url) => {
  calls.posts.push(url);
  if (url.indexOf('/api/item/') === 0) {
    return {ok: true, status: 200, statusText: 'OK',
            json: async () => ({workshop_id: 7, fetch_status: -2})};
  }
  return {ok: true, status: 200, statusText: 'OK', json: async () => ({ok: true})};
};
const cell = {
  classList: {contains: (c) => c === 'grid-cell'},
  getAttribute: (n) => (n === 'data-wid' ? '7' : null),
};
global.document = {
  getElementById: () => ({contains: () => true, querySelectorAll: () => [cell]}),
  get activeElement() { return cell; },
};
(async () => {
  keyFn({key: 'i', preventDefault: () => {}});
  await new Promise((r) => setTimeout(r, 0));
  const afterKey = calls.posts.slice();
  calls.posts.length = 0;
  wrap();
  await new Promise((r) => setTimeout(r, 0));
  console.log(JSON.stringify({afterKey: afterKey, afterButton: calls.posts.slice()}));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_item_ignore_button_posts_the_same_route_as_the_i_key(web_client, tmp_path):
    """The pane's ignore control and the `i` key run the one toggle function.

    Both paths are driven through the real read-back, so the routes they post are
    the assertion: the key's focused cell and the pane's item both hit
    ``/api/ignore/<id>`` and then read the item back, and they cannot diverge
    because the button calls the same `toggleIgnoredItem` the key does.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    out = _run_node(IGNORE_BUTTON_ROUTE_DRIVER
                    .replace("__TOGGLE__", _extract_function(script, "toggleIgnoredItem"))
                    .replace("__WRAP__",
                             _extract_function(script, "toggleIgnoredItemFromDetail"))
                    .replace("__KEY__", _extract_function(script, "_onGridKeydown")),
                    tmp_path)

    assert out["afterKey"] == ['/api/ignore/7', '/api/item/7']
    assert out["afterButton"] == out["afterKey"], \
        "the detail button must post exactly the route the `i` key posts"


IGNORE_BUTTON_LABEL_DRIVER = """
const IGNORED_FETCH_STATUS = -2;
const _escapeHtml = (__ESC__);
const _hintKey = (__HINT__);
const fn = (__FN__);
const btn = {disabled: null, innerHTML: '', title: ''};
global.document = {getElementById: (id) => (id === 'btn-ignore-item' ? btn : null)};
fn({workshop_id: 7, fetch_status: -2});
const ignored = {label: btn.innerHTML, title: btn.title, disabled: btn.disabled};
fn({workshop_id: 7, fetch_status: 200});
const normal = {label: btn.innerHTML, title: btn.title, disabled: btn.disabled};
console.log(JSON.stringify({ignored: ignored, normal: normal}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_pane_ignore_button_names_and_marks_both_directions(web_client, tmp_path):
    """The pane's ignore control is the pane's own ignored-state readout.

    Today only the grid shows the ignored state, as a strike on the title; the
    button is the pane's first indication of it, so its label and tooltip must
    follow the same payload field the grid's class follows.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    out = _run_node(IGNORE_BUTTON_LABEL_DRIVER
                    .replace("__ESC__", _extract_function(script, "_escapeHtml"))
                    .replace("__HINT__", _extract_function(script, "_hintKey"))
                    .replace("__FN__", _extract_function(script, "_refreshIgnoreButton")),
                    tmp_path)

    assert out["normal"]["label"] == "<u>I</u>gnore"
    assert out["ignored"]["label"] == "Un<u>i</u>gnore"
    assert out["normal"]["disabled"] is False and out["ignored"]["disabled"] is False
    assert out["ignored"]["title"] != out["normal"]["title"], \
        "the tooltip must say which direction the press is"


def test_the_item_ignore_button_is_a_detail_buttons_control(web_client, monkeypatch,
                                                            tmp_path):
    """The pane's toggle is a real button beside Open Folder, not grid-only."""
    client, db_path = web_client
    _enable_open_folder(monkeypatch, db_path, tmp_path / "content", lambda path: None)
    html = client.get('/').data.decode()
    start = html.index('<div id="detail-buttons">')
    bar = html[start:html.index('</div>', start)]
    assert 'id="btn-ignore-item"' in bar, \
        "the toggle belongs to the pane's button bar, where Open Folder lives"
    assert 'id="btn-open-folder"' in bar
    assert "toggleIgnoredItemFromDetail()" in bar
    assert "<u>I</u>gnore" in bar, "the button carries the `i` hint"
