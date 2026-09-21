"""The one item-update path: a change to an item reaches every display of it.

The invariant this file pins: any data a front end obtains about an item -- from
a callback, a poll, or an action -- is reflected in every component that displays
that item. The list row and the detail pane must never disagree about the same
``workshop_id``.

The four properties, one section each below:

* a change written to the database behind the front end's back appears in the
  list row *and* the detail pane (the TUI test and the web test);
* a marker change reaches both front ends;
* a non-subscription change -- a download -- moves the marker in both;
* a panel that has stopped displaying an item receives nothing.

The mechanism is the per-item registry and its single dispatch point: a
component subscribes to the id while it is on screen, and every update goes
through ``dispatch_item_update`` (TUI) / ``dispatchItemUpdate`` (web). These
tests drive those public paths rather than a particular writer, so they do not
name a call site that a later change could move.
"""

from unittest.mock import patch

import pytest
from textual.widgets import Label, ListView
from textual.content import Content

from src import subscription
from src.database import (
    get_connection,
    initialize_database,
    insert_or_update_item,
)
from src.tui import DetailsPane, ScraperApp
from tests.conftest import ASYNC_PAUSE
from tests.test_subscription_web import (  # noqa: F401  (web_client is a fixture)
    NODE,
    _extract_function,
    _run_node,
    _served_inline_script,
    web_client,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _seed(db_path, wid=5, **columns):
    insert_or_update_item(
        db_path,
        dict({"workshop_id": wid, "title": f"Item {wid}", "fetch_status": 200,
              "consumer_appid": 294100}, **columns),
    )


def _write_behind_the_back(db_path, sql, params=()):
    """A writer outside the front end: the daemon's folder scan, another UI."""
    conn = get_connection(db_path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def _markup_of(label: Label) -> str:
    return label._Static__content


def _spans(markup: str):
    content = Content.from_markup(markup)
    return content.plain, content.spans


def _list_row_markup(app, index=0) -> str:
    list_view = app.query_one("#results-list", ListView)
    return _markup_of(list_view.children[index].query(Label)[1])


def _pane_markup(app) -> str:
    pane = app.query_one("#detail-pane", DetailsPane)
    return _markup_of(pane.query_one("#item-sub-marker", Label))


def _assert_marker(markup: str, state: str):
    plain, spans = _spans(markup)
    assert subscription.glyph(state) in plain, markup
    assert subscription.colour(state) in [span.style for span in spans], markup


def _config(db_path, **extra):
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}
    config.update(extra)
    return config


class _Recorder:
    """A registry subscriber that just keeps what it was handed."""

    def __init__(self):
        self.items = []

    def apply_item_update(self, item):
        self.items.append(item)


# ── 1. a change behind the front end's back reaches the row and the pane ─────

@pytest.mark.asyncio
async def test_a_download_written_behind_the_tui_reaches_the_row_and_the_pane(tmp_path):
    """The daemon's folder scan stamps the latch; both displays must move.

    Nothing in this process is running for the item -- it is not queued and no
    stage is pending -- so the only thing that can carry the change is the
    general item-update poll over the registry.
    """
    db_path = str(tmp_path / "behind.db")
    initialize_database(db_path)
    _seed(db_path, own_subscribed=1, own_first_subscribed_at=1000)
    config = _config(db_path)

    with patch("src.tui.load_config", return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one("#results-list", ListView)
            list_view.index = 0
            await pilot.pause(ASYNC_PAUSE)

            _assert_marker(_list_row_markup(app), subscription.SUBSCRIBED)
            _assert_marker(_pane_markup(app), subscription.SUBSCRIBED)

            # The daemon's folder scan, behind this process's back.
            _write_behind_the_back(
                db_path,
                "UPDATE workshop_items SET steam_download_seen_at = 123 WHERE workshop_id = 5",
            )

            app._poll_item_updates()
            await pilot.pause(ASYNC_PAUSE)

            after_row = _list_row_markup(app)
            after_pane = _pane_markup(app)

    _assert_marker(after_row, subscription.DOWNLOADED)
    _assert_marker(after_pane, subscription.DOWNLOADED)


@pytest.mark.asyncio
async def test_the_tui_marker_change_reaches_the_row_and_the_pane(tmp_path):
    """A subscribe landing behind the front end's back moves both displays."""
    db_path = str(tmp_path / "marker.db")
    initialize_database(db_path)
    _seed(db_path)
    config = _config(db_path)

    with patch("src.tui.load_config", return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app.query_one("#results-list", ListView).index = 0
            await pilot.pause(ASYNC_PAUSE)

            _assert_marker(_list_row_markup(app), subscription.NEVER)
            _assert_marker(_pane_markup(app), subscription.NEVER)

            # The web UI in another process, or the daemon's reconcile.
            _write_behind_the_back(
                db_path,
                "UPDATE workshop_items SET own_subscribed = 1, own_first_subscribed_at = 2000 "
                "WHERE workshop_id = 5",
            )

            app._poll_item_updates()
            await pilot.pause(ASYNC_PAUSE)

            row = _list_row_markup(app)
            pane = _pane_markup(app)

    _assert_marker(row, subscription.SUBSCRIBED)
    _assert_marker(pane, subscription.SUBSCRIBED)


# ── 2/3. the download moves the marker on both front ends ────────────────────

def test_a_download_behind_the_web_carries_the_same_marker_both_front_ends(web_client):
    """One stored row, one payload shape: the TUI block and the web payload agree.

    The TUI's registry enriches a dispatched block and the web server enriches
    every payload it returns with the same derived marker, so a subscriber on
    either side reads the same field names for the same columns. This is the
    download case: ``steam_download_seen_at`` written behind the front ends'
    backs, and ``downloaded`` is what both hand to a display.
    """
    from src import item_updates as item_updates_module
    from src.database import get_items_by_ids

    client, db_path = web_client
    _seed(db_path, own_subscribed=1, own_first_subscribed_at=1000)
    _write_behind_the_back(
        db_path, "UPDATE workshop_items SET steam_download_seen_at = 123 WHERE workshop_id = 5")

    web_item = client.get("/api/item/5").get_json()
    assert web_item["subscription_state"] == subscription.DOWNLOADED

    registry = item_updates_module.ItemUpdateRegistry()
    recorder = _Recorder()
    registry.subscribe(5, recorder)
    deliveries = registry.dispatch(get_items_by_ids(db_path, [5])[0])

    assert deliveries == 1, "the one subscribed display must receive the block"
    block = recorder.items[0]
    marker_keys = [
        "subscription_state", "subscription_glyph", "subscription_colour",
        "subscription_class", "subscription_label", "subscription_tooltip",
        "subscription_clickable",
    ]
    assert {key: block[key] for key in marker_keys} == \
        {key: web_item[key] for key in marker_keys}
    assert block["subscription_state"] == subscription.DOWNLOADED
    assert block["steam_download_seen_at"] == 123


# ── 4. a display that stopped showing an item receives nothing ──────────────

@pytest.mark.asyncio
async def test_a_pane_that_stopped_displaying_an_item_receives_nothing(tmp_path):
    """The registry describes what is on screen, not what exists in the database."""
    db_path = str(tmp_path / "unsub.db")
    initialize_database(db_path)
    _seed(db_path, wid=5, own_subscribed=1, own_first_subscribed_at=1000)
    _seed(db_path, wid=6, title="Other")
    config = _config(db_path)

    with patch("src.tui.load_config", return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one("#results-list", ListView)
            pane = app.query_one("#detail-pane", DetailsPane)

            # Adopt item 5, then move to item 6: the pane stops displaying 5.
            list_view.index = 0
            await pilot.pause(ASYNC_PAUSE)
            assert pane.workshop_id == 5
            list_view.index = 1
            await pilot.pause(ASYNC_PAUSE)
            assert pane.workshop_id == 6
            assert pane not in app.item_updates.subscribers(5), \
                "the pane must have left item 5's subscriber list"
            assert pane in app.item_updates.subscribers(6)

            held = dict(pane.item_data)
            app.dispatch_item_update({
                "workshop_id": 5, "own_subscribed": 1, "steam_download_seen_at": 123})
            await pilot.pause(ASYNC_PAUSE)

            assert pane.item_data == held, \
                "a pane showing item 6 must not be updated with item 5"

            # The rows leave the registry when the result set does.
            await list_view.clear()
            await pilot.pause(ASYNC_PAUSE)
            assert app.item_updates.subscribers(5) == ()
            assert app.dispatch_item_update({"workshop_id": 5, "own_subscribed": 1}) == 0


# ── the web registry, under node ─────────────────────────────────────────────

WEB_UPDATE_DRIVER = """
const _itemSubscribers = new Map();
let _itemUpdatePollTimer = null;
const _ITEM_UPDATE_POLL_MS = 3000;
const _subscribeItem = (__SUBSCRIBE__);
const _unsubscribeItem = (__UNSUBSCRIBE__);
const _subscribedItemIds = (__IDS__);
const dispatchItemUpdate = (__DISPATCH__);
const dispatchItemUpdates = (__DISPATCH_MANY__);
const _itemUpdateTick = (__TICK__);

let scheduled = 0;
global.setTimeout = () => { scheduled += 1; return scheduled; };

const requests = [];
const seen = {cell: [], pane: [], gone: 0};
let nextItems = [];
global.fetch = async (url, opts) => {
  requests.push(JSON.parse(opts.body).ids);
  return {ok: true, status: 200, json: async () => nextItems};
};

const cellSub = {applyItemUpdate: (it) => seen.cell.push(it.subscription_state)};
const paneSub = {applyItemUpdate: (it) => seen.pane.push(it.subscription_state)};
const goneSub = {isConnected: false, applyItemUpdate: () => { seen.gone += 1; }};

(async () => {
  _subscribeItem(5, cellSub);
  _subscribeItem(5, paneSub);
  nextItems = [{workshop_id: 5, subscription_state: 'downloaded'}];
  await _itemUpdateTick();
  // Nothing is pending here ('downloaded' is settled), so an armed timer is the
  // proof that the general poll does not stop merely because nothing is pending.
  const armedAfterSettled = _itemUpdatePollTimer !== null;

  // The pane stops displaying the item; the cell keeps it.
  _unsubscribeItem(5, paneSub);
  nextItems = [{workshop_id: 5, subscription_state: 'subscribed'}];
  await _itemUpdateTick();

  // A detached display is dropped rather than handed an update.
  _subscribeItem(7, goneSub);
  dispatchItemUpdate({workshop_id: 7, subscription_state: 'never'});
  const goneStillSubscribed = _subscribedItemIds().indexOf(7) !== -1;

  console.log(JSON.stringify({
    requests: requests, seen: seen, armedAfterSettled: armedAfterSettled,
    goneStillSubscribed: goneStillSubscribed,
    ids: _subscribedItemIds(),
  }));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_the_web_general_poll_dispatches_to_every_display_of_the_item(web_client, tmp_path):
    """The web mirror of the TUI test: one poll, one dispatch, both displays.

    The poll reads exactly the registry's ids and hands every returned block to
    the one dispatch point, so the cell and the pane cannot be current in one
    and stale in the other. A display that unsubscribed is left alone, and the
    poll stays armed even when the item is settled.
    """
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (WEB_UPDATE_DRIVER
              .replace("__SUBSCRIBE__", _extract_function(script, "_subscribeItem"))
              .replace("__UNSUBSCRIBE__", _extract_function(script, "_unsubscribeItem"))
              .replace("__IDS__", _extract_function(script, "_subscribedItemIds"))
              .replace("__DISPATCH__", _extract_function(script, "dispatchItemUpdate"))
              .replace("__DISPATCH_MANY__", _extract_function(script, "dispatchItemUpdates"))
              .replace("__TICK__", _extract_function(script, "_itemUpdateTick")))
    result = _run_node(driver, tmp_path)

    assert result["requests"] == [[5], [5]], \
        "the poll must read the registered ids, once per tick"
    assert result["seen"]["cell"] == ["downloaded", "subscribed"], \
        "the cell follows the database on both ticks"
    assert result["seen"]["pane"] == ["downloaded"], \
        "a pane that stopped displaying the item receives nothing"
    assert result["armedAfterSettled"] is True, \
        "the poll must not stop merely because nothing is pending"
    assert result["seen"]["gone"] == 0
    assert result["goneStillSubscribed"] is False, \
        "a detached display is dropped from the registry"
    assert result["ids"] == [5], "only the cell is left subscribed"
