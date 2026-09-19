"""The subscription marker's web side: the payload, the endpoint, and the click.

Three things are pinned here, all of which the old two-indicator shape got
wrong:

* the read endpoints carry the marker's state and its whole appearance, computed
  from `src/subscription.py`, so the page owns no copy of the glyph/colour table;
* ``POST /api/subscribed`` stamps the subscription as well as clearing the queue
  flag -- that call is the highest-fidelity signal the project gets, and it used
  to throw the subscription fact away;
* the marker's click routes the three actionable states through the existing
  toggle route and sends nothing for ``subscribed``.

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


@pytest.mark.parametrize("columns,state", [
    ({"own_subscribed": 1, "downloaded_at": 1000}, subscription.DOWNLOADED),
    ({"own_subscribed": 1, "own_first_subscribed_at": 1000}, subscription.SUBSCRIBED),
    ({"is_queued_for_subscription": 1}, subscription.QUEUED),
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
    assert by_id[1]["subscription_clickable"] is False
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
                                    "own_subscribed": 1, "downloaded_at": 1000})

    rows = client.post('/api/items', json={"ids": [3]}).get_json()

    assert rows[0]["downloaded_at"] == 1000
    assert rows[0]["subscription_state"] == subscription.DOWNLOADED
    assert rows[0]["subscription_glyph"] == subscription.glyph(subscription.DOWNLOADED)


def test_the_queued_payload_carries_the_whole_marker(web_client):
    """The queue overlay is a marker surface too, so /api/queued derives it."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 4, "title": "D", "fetch_status": 200,
                                    "is_queued_for_subscription": 1,
                                    "own_subscribed": 1, "downloaded_at": 1000})

    rows = client.get('/api/queued').get_json()

    assert rows[0]["subscription_state"] == subscription.DOWNLOADED
    assert rows[0]["subscription_glyph"] == subscription.glyph(subscription.DOWNLOADED)
    assert rows[0]["subscription_colour"] == subscription.colour(subscription.DOWNLOADED)
    assert rows[0]["subscription_tooltip"] == subscription.tooltip(subscription.DOWNLOADED)


# --- opening a downloaded item's folder -------------------------------------


def _enable_open_folder(monkeypatch, db_path, content_dir, launcher):
    """Point the server's global helper at a Windows build with a fake launcher.

    No test may open Explorer: the launcher is always injected, and the folder is
    checked against a temp content directory rather than a real Steam library.
    """
    service = workshop_folders.WorkshopFolders(
        db_path, {"steam": {"workshop_content_dirs": [str(content_dir)]}},
        platform="win32", launcher=launcher)
    monkeypatch.setattr(webserver, "_workshop_folders", service)
    return service


def _green_item(db_path, wid=7, *, content_dir, make_folder=True):
    insert_or_update_item(db_path, {
        "workshop_id": wid, "title": "T", "fetch_status": 200, "consumer_appid": 294100,
        "own_subscribed": 1, "downloaded_at": 1000,
    })
    if make_folder:
        (content_dir / "294100" / str(wid)).mkdir(parents=True)


def test_open_folder_refuses_off_windows(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 7, "title": "T", "fetch_status": 200,
                                    "own_subscribed": 1, "downloaded_at": 1000})

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
    assert _row(db_path, 7)["downloaded_at"] == 1000, "the refusal changes no state"


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
    assert _row(db_path, 7)["downloaded_at"] == 1000


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

    off = client.get('/').data.decode()
    assert '<button id="btn-open-folder"' not in off
    assert "e.key === 'o'" not in off, "off Windows the shortcut is not bound"

    _enable_open_folder(monkeypatch, db_path, tmp_path / "content", lambda path: None)
    on = client.get('/').data.decode()
    assert '<button id="btn-open-folder"' in on
    assert "e.key === 'o'" in on
    assert "open_folder_enabled" not in on


# --- the stamp --------------------------------------------------------------

def test_api_subscribed_stamps_the_subscription_and_clears_the_queue(web_client):
    """The userscript's confirmation is the highest-fidelity signal there is."""
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
    result = subprocess.run([NODE, str(path)], capture_output=True, text=True)
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

// The four states, clicked.
fn(event, el('subscribed', '0', 1, '\\u2605'));
fn(event, el('queued', '1', 2, '\\u2606'));
fn(event, el('previously', '1', 3, '\\u2606'));
fn(event, el('never', '1', 4, '\\u25cb'));
console.log(JSON.stringify({calls: calls, stopped: stopped}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_clicking_the_marker_routes_the_three_actionable_states(web_client, tmp_path):
    """subscribed sends nothing; the other three toggle; none of them bubbles.

    The cell underneath opens the detail pane, so a marker click that did not
    stop propagating would toggle the queue *and* drag the pane to the item.
    """
    client, _ = web_client
    fn = _extract_function(_served_inline_script(client), "onSubMarkerClick")
    result = _run_node(SUB_CLICK_DRIVER.replace("__FN__", fn), tmp_path)

    assert result["calls"] == [2, 3, 4], \
        "only queued, previously and never act; subscribed must send nothing"
    assert result["stopped"] == 4, \
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
  {needs_image: 0, image_extension: 'jpg', translation_priority: 0, needs_web_scrape: 0},
  over);
console.log(JSON.stringify({
  stage: listNeedsPoll([item({needs_web_scrape: 5})]),
  subscription: listNeedsPoll([item({subscription_state: 'queued'})]),
  subscribed: listNeedsPoll([item({subscription_state: 'subscribed'})]),
  never: listNeedsPoll([item({subscription_state: 'never'})]),
}));
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_a_row_queued_only_for_subscription_is_polled(web_client, tmp_path):
    """A row with no stage spinner still has a marker that can move.

    The subscription marker changes the moment a subscribe lands, so the poll
    must be started for a row whose only outstanding state is the queue entry --
    otherwise the cell is never re-read and keeps the pending marker.
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
    assert result["subscribed"] is False, "nothing to re-read once subscribed"
    assert result["never"] is False, "nothing to re-read when settled"


POLL_SCOPE_DRIVER = """
let _listPollTimer = null;
const _stopListPoll = () => {};
const startFn = (__START__);

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
    spinner row and the queued row, and not the settled one. Refreshing the
    cell here rather than at each writer is what stops one write path -- the
    userscript's /api/subscribed, the page's own cancel/clear handlers, the
    direct /api/subscribe route -- being able to bypass the refresh.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = POLL_SCOPE_DRIVER.replace("__START__",
                                       _extract_function(script, "_startListPoll"))
    result = _run_node(driver, tmp_path)

    assert result["fetched"] and sorted(result["fetched"]) == [11, 22], \
        "the poll must re-read the spinner row and the queued row"


TOGGLE_STARTS_POLL_DRIVER = """
const _applySub = () => {};
let started = 0;
var _startListPoll = () => { started += 1; };
let queued = 0;
global._currentDetail = {workshop_id: 77};
global.renderDetail = () => {};
global.alert = () => {};
global.document = {querySelector: () => null};
global.fetch = async (url) => {
  if (url.indexOf('/api/toggle_subscription_queue/') === 0) {
    return {ok: true, status: 200, statusText: 'OK'};
  }
  if (url.indexOf('/api/item/') === 0) {
    queued = queued ? 0 : 1;
    return {ok: true, status: 200, statusText: 'OK',
            json: async () => ({workshop_id: 77,
                                subscription_state: queued ? 'queued' : 'never'})};
  }
  throw new Error('unexpected url ' + url);
};
const fn = (__FN__);
(async () => {
  await fn(77);
  const afterQueue = started;
  await fn(77);
  const afterUnqueue = started;
  console.log(JSON.stringify({afterQueue: afterQueue, afterUnqueue: afterUnqueue}));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_queueing_a_row_starts_the_poll(web_client, tmp_path):
    """A row queued after the search rendered must be watched too.

    _listNeedsPoll only runs when a batch is rendered, so a marker clicked into
    `queued` afterwards would otherwise never be re-read and the subscribe
    landing would leave the stale marker the poll exists to clear.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = TOGGLE_STARTS_POLL_DRIVER.replace(
        "__FN__", _extract_function(script, "toggleDetailQueue"))
    result = _run_node(driver, tmp_path)

    assert result["afterQueue"] == 1, "queueing a marker must start the list poll"
    assert result["afterUnqueue"] == 1, "un-queueing must not start it again"


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


def test_the_old_queue_buttons_are_gone_from_the_pane():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "toggleDetailQueue(${item.workshop_id})" not in html
    assert ">Unqueue</button>" not in html
    # The marker helper is what the pane calls instead.
    assert "showSubscriptionMarker(item)" in html
