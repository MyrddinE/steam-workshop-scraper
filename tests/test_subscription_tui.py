"""What the TUI actually draws for the subscription marker, in both places.

The web grid and pane render the shared table from `src/subscription.py` into a
positioned element; the TUI renders the same table into Textual markup. These
tests read the markup the widgets are actually given -- parsed back through
Textual's own markup parser -- so a change to a glyph or a colour that did not go
through the shared module fails here rather than quietly shipping.

The list row's marker replaces the old leading ``*`` prefix for
``is_queued_for_subscription``; there must be exactly one indicator, and it must
be the shared one.
"""

from unittest.mock import patch

import pytest
from textual.content import Content
from textual.widgets import Button, Label, ListView, Static

from src import subscription, subscribe_engine
from src.database import (
    get_connection,
    initialize_database,
    insert_or_update_item,
    mark_own_subscribed,
    toggle_subscription_queue_status,
)
from src.tui import DetailsPane, ScraperApp, SubscriptionQueueScreen, app_bindings
from tests.conftest import ASYNC_PAUSE


def _markup_of(label: Label) -> str:
    """The raw markup a Label was constructed with (Textual keeps it as-is)."""
    return label._Static__content


def _spans(markup: str):
    content = Content.from_markup(markup)
    return content.plain, content.spans


@pytest.mark.parametrize("columns,state", [
    ({"own_subscribed": 1, "downloaded_at": 1000}, subscription.DOWNLOADED),
    ({"own_subscribed": 1, "own_first_subscribed_at": 1000}, subscription.SUBSCRIBED),
    ({"is_queued_for_subscription": 1}, subscription.PENDING),
    ({"own_first_subscribed_at": 1000}, subscription.PREVIOUSLY),
    ({}, subscription.NEVER),
])
@pytest.mark.asyncio
async def test_the_tui_list_row_draws_each_state(tmp_path, columns, state):
    db_path = str(tmp_path / "tui.db")
    from src.database import initialize_database, insert_or_update_item
    initialize_database(db_path)
    insert_or_update_item(db_path, dict({"workshop_id": 5, "title": "Item", "status": 200},
                                        **columns))
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch('src.tui.load_config', return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one("#results-list", ListView)
            row = list_view.children[0]
            by_line = [_markup_of(w) for w in row.query(Label)]

    creator_line = by_line[1]
    plain, spans = _spans(creator_line)
    assert subscription.glyph(state) in plain, creator_line
    assert subscription.colour(state) in [span.style for span in spans], creator_line
    # The old `*` queue prefix is gone: the marker is the only indicator.
    assert "*" not in by_line[0]


@pytest.mark.parametrize("columns,state", [
    ({"own_subscribed": 1, "downloaded_at": 1000}, subscription.DOWNLOADED),
    ({"own_subscribed": 1, "own_first_subscribed_at": 1000}, subscription.SUBSCRIBED),
    ({"is_queued_for_subscription": 1}, subscription.PENDING),
    ({"own_first_subscribed_at": 1000}, subscription.PREVIOUSLY),
    ({}, subscription.NEVER),
])
@pytest.mark.asyncio
async def test_the_tui_detail_pane_draws_each_state_before_the_title(tmp_path, columns, state):
    db_path = str(tmp_path / "detail.db")
    from src.database import initialize_database, insert_or_update_item
    initialize_database(db_path)
    insert_or_update_item(db_path, dict({"workshop_id": 5, "title": "Item", "status": 200},
                                        **columns))
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch('src.tui.load_config', return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            # Move the highlight onto the row, which is what adopts the item into
            # the detail pane (the same move the existing detail-pane tests make).
            list_view = app.query_one("#results-list", ListView)
            list_view.index = 0
            await pilot.pause(ASYNC_PAUSE)
            pane = app.query_one("#item-details", DetailsPane)
            title_row = pane.query_one("#title-creator-row")
            children = list(title_row.children)
            marker_label = pane.query_one("#item-sub-marker", Label)

    # The marker is the first child of the title row, so it renders before the
    # title -- the same convention the web pane uses.
    assert children[0] is marker_label
    assert children[1].id == "item-title"
    plain, spans = _spans(_markup_of(marker_label))
    assert subscription.glyph(state) in plain
    assert subscription.colour(state) in [span.style for span in spans]
    assert marker_label.tooltip == subscription.tooltip(state)


@pytest.mark.asyncio
async def test_the_tui_detail_pane_has_no_queue_buttons(tmp_path):
    """The marker is the control; a second one is what this removed."""
    db_path = str(tmp_path / "nobuttons.db")
    from src.database import initialize_database
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch('src.tui.load_config', return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            pane = app.query_one("#item-details", DetailsPane)
            ids = {widget.id for widget in pane.query("*")}

    assert "btn-queue-sub" not in ids
    assert "btn-unqueue-sub" not in ids


@pytest.mark.asyncio
async def test_the_keyboard_toggle_updates_the_marker(tmp_path):
    """The keyboard stays the TUI's way to change the state."""
    db_path = str(tmp_path / "toggle.db")
    from src.database import initialize_database, insert_or_update_item
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 5, "title": "Item", "status": 200})
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch('src.tui.load_config', return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one("#results-list", ListView)
            list_view.index = 0
            await pilot.pause(ASYNC_PAUSE)
            row = list_view.highlighted_child
            before = _markup_of(row.query(Label)[1])

            await pilot.press("s")
            await pilot.pause(ASYNC_PAUSE)

            after = _markup_of(list_view.highlighted_child.query(Label)[1])
            pane_marker = _markup_of(
                app.query_one("#item-details", DetailsPane)
                .query_one("#item-sub-marker", Label))

    assert subscription.glyph(subscription.NEVER) in before
    # The `s` binding queues the item, so both sites must now draw pending.
    assert subscription.glyph(subscription.PENDING) in after, \
        "the list row must follow the keyboard toggle"
    _plain, after_spans = _spans(after)
    assert subscription.colour(subscription.PENDING) in [s.style for s in after_spans]
    assert subscription.glyph(subscription.PENDING) in pane_marker


# --- the list marker follows the database, whoever wrote it -----------------


def _seed_queued(db_path: str, workshop_id: int = 5, title: str = "Item") -> None:
    initialize_database(db_path)
    insert_or_update_item(
        db_path, {"workshop_id": workshop_id, "title": title, "status": 200})
    toggle_subscription_queue_status(db_path, workshop_id)


def _list_row_markup(app: ScraperApp, index: int = 0) -> str:
    list_view = app.query_one("#results-list", ListView)
    return _markup_of(list_view.children[index].query(Label)[1])


@pytest.mark.asyncio
async def test_the_list_marker_follows_a_subscribe_that_lands_behind_it(tmp_path):
    """The writer need not be this UI: the shared table is re-read.

    This is issue 33's fix in the web grid, repeated here. The row's marker is
    built from the item data captured when the row was made and nothing on the
    two-second detail-pane timer re-reads the list, so a subscribe landed by the
    daemon's reconcile -- or by the web UI in another process -- used to leave
    the green pending outline in place until a search or a scroll rebuilt it.
    """
    db_path = str(tmp_path / "behind.db")
    _seed_queued(db_path)
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch('src.tui.load_config', return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            before = _list_row_markup(app)
            assert subscription.glyph(subscription.PENDING) in before

            # No callback in this process runs: the daemon's daily reconcile (or
            # the web UI) stamps the subscription straight into the shared table.
            mark_own_subscribed(db_path, 5)

            app._start_subscription_poll()
            await pilot.pause(ASYNC_PAUSE * 2)
            after = _list_row_markup(app)
            armed = app._sub_poll_timer

    assert subscription.glyph(subscription.SUBSCRIBED) in after, after
    _plain, spans = _spans(after)
    assert subscription.colour(subscription.SUBSCRIBED) in [s.style for s in spans]
    assert armed is None, "the poll must stop once no rendered row is pending"


@pytest.mark.asyncio
async def test_the_poll_stops_when_no_rendered_row_is_pending(tmp_path):
    """A settled list costs no database reads: the poll must not stay armed."""
    db_path = str(tmp_path / "settled.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 5, "title": "Item", "status": 200})
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch('src.tui.load_config', return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            never_armed = app._sub_poll_timer
            # Even when armed by hand, one tick with nothing pending stops it.
            app._start_subscription_poll()
            await pilot.pause(ASYNC_PAUSE * 2)
            after_tick = app._sub_poll_timer

    assert never_armed is None
    assert after_tick is None


@pytest.mark.asyncio
async def test_the_poll_re_arms_itself_while_a_row_is_still_pending(tmp_path):
    """Stopping is the exception, not the rule: a pending row keeps it armed."""
    db_path = str(tmp_path / "still_pending.db")
    _seed_queued(db_path)
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch('src.tui.load_config', return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app._stop_subscription_poll()
            app._start_subscription_poll()
            await pilot.pause(ASYNC_PAUSE * 2)
            armed = app._sub_poll_timer
            markup = _list_row_markup(app)

    assert armed is not None, "a still-pending row must keep the poll running"
    _plain, spans = _spans(markup)
    assert subscription.colour(subscription.PENDING) in [s.style for s in spans]


@pytest.mark.asyncio
async def test_a_pass_result_moves_an_on_screen_row_without_the_next_tick(tmp_path):
    """The queue's own outcome redraws the list row immediately.

    The poll is what catches the other writers; the pass's callback is only the
    fast path for a row already on screen, so this test stops the poll first and
    shows the row still moves.
    """
    db_path = str(tmp_path / "immediate.db")
    _seed_queued(db_path)
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    def fake_pass(items, **kwargs):
        outcomes = []
        for item in items:
            # The real engine records the subscription before returning, so the
            # read-back in `_apply_result` sees the new state.
            mark_own_subscribed(db_path, item["workshop_id"])
            outcome = subscribe_engine.SubscribeOutcome(
                item["workshop_id"], subscribe_engine.SUBSCRIBED, subscribed=True)
            outcomes.append(outcome)
            kwargs["on_result"](outcome)
        return outcomes

    with patch('src.tui.load_config', return_value=config), \
         patch('src.subscribe_engine.run_subscription_pass', side_effect=fake_pass):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app._stop_subscription_poll()
            await pilot.press("l")
            await pilot.pause(ASYNC_PAUSE)
            await pilot.click("#btn-subscribe-queue")
            await pilot.pause(ASYNC_PAUSE * 3)
            after = _list_row_markup(app)

    assert subscription.glyph(subscription.SUBSCRIBED) in after, after


# --- the queue screen's own rows carry the real marker ----------------------


def test_the_queue_row_builder_draws_the_items_real_state():
    """The row used to hardcode `pending` whatever the item's state was."""
    subscribed = {
        "workshop_id": 5, "title": "Item",
        "own_subscribed": 1, "is_queued_for_subscription": 0,
        "own_first_subscribed_at": 1000,
    }
    line = SubscriptionQueueScreen._row_text(subscribed)
    plain, spans = str(line), [span.style for span in line.spans]
    assert subscription.glyph(subscription.SUBSCRIBED) in plain
    assert subscription.colour(subscription.SUBSCRIBED) in spans


def test_the_queue_row_builder_draws_the_downloaded_state():
    """The queue screen is one of the marker's surfaces, so it draws green too."""
    downloaded = {
        "workshop_id": 5, "title": "Item",
        "own_subscribed": 1, "is_queued_for_subscription": 0,
        "own_first_subscribed_at": 1000, "downloaded_at": 2000,
    }
    line = SubscriptionQueueScreen._row_text(downloaded)
    plain, spans = str(line), [span.style for span in line.spans]
    assert subscription.glyph(subscription.DOWNLOADED) in plain
    assert subscription.colour(subscription.DOWNLOADED) in spans


# --- opening a downloaded item's folder (Windows only) ----------------------


def test_the_open_folder_binding_is_declared_only_on_windows():
    """Off Windows the key must be absent, not present-and-inert."""
    assert ("o", "open_folder", "Open Folder") in app_bindings("win32")
    assert ("o", "open_folder", "Open Folder") not in app_bindings("linux")
    assert ("o", "open_folder", "Open Folder") not in app_bindings("darwin")


@pytest.mark.asyncio
async def test_the_open_folder_button_is_absent_off_windows(tmp_path):
    db_path = str(tmp_path / "off_windows.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch("src.workshop_folders.is_windows", return_value=False), \
         patch("src.tui.load_config", return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            pane = app.query_one("#item-details", DetailsPane)
            ids = {widget.id for widget in pane.query("*")}

    assert "btn-open-folder" not in ids


@pytest.mark.asyncio
async def test_the_open_button_and_key_act_only_on_a_downloaded_item(tmp_path):
    """The control is disabled with the reason until the star is green.

    The key path shows the same refusal as a notification instead of doing
    nothing, and when the latch lands the button enables and the shared helper
    opens the folder through the injected launcher -- no Explorer in the test.
    """
    db_path = str(tmp_path / "open.db")
    initialize_database(db_path)
    content = tmp_path / "content"
    folder = content / "294100" / "5"
    folder.mkdir(parents=True)
    insert_or_update_item(db_path, {
        "workshop_id": 5, "title": "Item", "status": 200,
        "consumer_appid": 294100, "own_subscribed": 1,
    })
    config = {"database": {"path": db_path},
              "steam": {"workshop_content_dirs": [str(content)]},
              "logging": {"level": "INFO"}}
    launched = []

    with patch("src.workshop_folders.is_windows", return_value=True), \
         patch("src.tui.load_config", return_value=config):
        app = ScraperApp()
        app.workshop_folders._launcher = launched.append
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one("#results-list", ListView)
            list_view.index = 0
            await pilot.pause(ASYNC_PAUSE)
            pane = app.query_one("#item-details", DetailsPane)

            button = pane.query_one("#btn-open-folder", Button)
            assert button.disabled is True, "subscribed but not confirmed on disk"
            assert "not downloaded" in str(button.label)

            with patch.object(app, "notify") as notify:
                await app.action_open_folder()
            assert launched == [], "a non-green item must not open anything"
            assert notify.called, "the key must say why, not do nothing"

            # The scan stamps the latch; the pane's own refresh re-reads it.
            conn = get_connection(db_path)
            conn.execute("UPDATE workshop_items SET downloaded_at = 123 WHERE workshop_id = 5")
            conn.commit()
            conn.close()
            await pane.refresh_data()
            await pilot.pause(ASYNC_PAUSE)

            button = pane.query_one("#btn-open-folder", Button)
            assert button.disabled is False
            await app.action_open_folder()
            await pilot.pause(ASYNC_PAUSE)

    assert launched == [str(folder)], "the key opens the folder the helper resolves"


@pytest.mark.asyncio
async def test_the_queue_row_marker_follows_the_pass_outcome(tmp_path):
    """A completed subscribe must move the queue row's own glyph, not just its
    status word and the final tally."""
    db_path = str(tmp_path / "queue_outcome.db")
    _seed_queued(db_path)
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    def fake_pass(items, **kwargs):
        outcomes = []
        for item in items:
            mark_own_subscribed(db_path, item["workshop_id"])
            outcome = subscribe_engine.SubscribeOutcome(
                item["workshop_id"], subscribe_engine.SUBSCRIBED, subscribed=True)
            outcomes.append(outcome)
            kwargs["on_result"](outcome)
        return outcomes

    with patch('src.tui.load_config', return_value=config), \
         patch('src.subscribe_engine.run_subscription_pass', side_effect=fake_pass):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            await pilot.press("l")
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen
            before = str(screen.query_one("#sub-item-5", Static).render())
            await pilot.click("#btn-subscribe-queue")
            await pilot.pause(ASYNC_PAUSE * 3)
            after = str(screen.query_one("#sub-item-5", Static).render())

    assert subscription.glyph(subscription.PENDING) in before
    assert subscription.glyph(subscription.SUBSCRIBED) in after, after
    assert subscription.glyph(subscription.PENDING) not in after, after
