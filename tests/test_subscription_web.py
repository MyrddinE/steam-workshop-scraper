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
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "T", "status": 200})

    item = client.get('/api/item/1').get_json()

    assert item["subscription_state"] == subscription.NEVER
    assert item["subscription_glyph"] == subscription.glyph(subscription.NEVER)
    assert item["subscription_colour"] == subscription.colour(subscription.NEVER)
    assert item["subscription_class"] == subscription.css_class(subscription.NEVER)
    assert item["subscription_label"] == subscription.label(subscription.NEVER)
    assert item["subscription_tooltip"] == subscription.tooltip(subscription.NEVER)
    assert item["subscription_clickable"] is True


@pytest.mark.parametrize("columns,state", [
    ({"own_subscribed": 1, "own_first_subscribed_at": 1000}, subscription.SUBSCRIBED),
    ({"is_queued_for_subscription": 1}, subscription.PENDING),
    ({"own_first_subscribed_at": 1000}, subscription.PREVIOUSLY),
    ({}, subscription.NEVER),
])
def test_the_item_payload_derives_every_state(web_client, columns, state):
    client, db_path = web_client
    insert_or_update_item(db_path, dict({"workshop_id": 7, "title": "T", "status": 200},
                                        **columns))

    item = client.get('/api/item/7').get_json()

    assert item["subscription_state"] == state
    assert item["subscription_glyph"] == subscription.glyph(state)
    assert item["subscription_colour"] == subscription.colour(state)
    assert item["subscription_clickable"] is subscription.is_clickable(state)


def test_the_search_payload_carries_the_marker_for_every_cell(web_client):
    """The grid draws its marker from the search rows, so they must carry it."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "A", "status": 200,
                                    "own_subscribed": 1, "own_first_subscribed_at": 5})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "B", "status": 200,
                                    "is_queued_for_subscription": 1})

    rows = client.post('/api/search', json={"limit": 10}).get_json()

    by_id = {r["workshop_id"]: r for r in rows}
    assert by_id[1]["subscription_state"] == subscription.SUBSCRIBED
    assert by_id[1]["subscription_clickable"] is False
    assert by_id[2]["subscription_state"] == subscription.PENDING
    # own_first_subscribed_at travels with the row, or a cell toggled away from
    # `pending` could not tell `never` from `previously`.
    assert by_id[1]["own_first_subscribed_at"] == 5


def test_the_items_payload_carries_the_marker(web_client):
    """The list poll updates markers in place from this route."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 3, "title": "C", "status": 200,
                                    "own_first_subscribed_at": 42})

    rows = client.post('/api/items', json={"ids": [3]}).get_json()

    assert rows[0]["subscription_state"] == subscription.PREVIOUSLY
    assert rows[0]["subscription_glyph"] == subscription.glyph(subscription.PREVIOUSLY)


# --- the stamp --------------------------------------------------------------

def test_api_subscribed_stamps_the_subscription_and_clears_the_queue(web_client):
    """The userscript's confirmation is the highest-fidelity signal there is."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "status": 200,
                                    "is_queued_for_subscription": 1})

    assert client.post('/api/subscribed/9').get_json() == {"ok": True}

    row = _row(db_path, 9)
    assert row["own_subscribed"] == 1
    assert row["own_first_subscribed_at"] is not None, \
        "the first-seen stamp is what makes `previously` possible later"
    assert row["is_queued_for_subscription"] == 0


def test_api_subscribed_does_not_move_an_existing_stamp(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "status": 200,
                                    "own_first_subscribed_at": 1000})

    client.post('/api/subscribed/9')

    assert _row(db_path, 9)["own_first_subscribed_at"] == 1000


def test_api_subscribed_makes_the_marker_subscribed(web_client):
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "status": 200})

    client.post('/api/subscribed/9')

    assert client.get('/api/item/9').get_json()["subscription_state"] == subscription.SUBSCRIBED


def test_toggle_sub_still_only_flips_the_queue_flag(web_client):
    """/api/toggle_sub is unchanged: it is the route both directions use."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 9, "title": "T", "status": 200})

    client.post('/api/toggle_sub/9')
    assert _row(db_path, 9)["is_queued_for_subscription"] == 1
    assert _row(db_path, 9)["own_subscribed"] == 0

    client.post('/api/toggle_sub/9')
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
fn(event, el('pending', '1', 2, '\\u2606'));
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
        "only pending, previously and never act; subscribed must send nothing"
    assert result["stopped"] == 4, \
        "every marker click must stop propagation, including the inert one"


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
