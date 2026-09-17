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
from textual.widgets import Label, ListView

from src import subscription
from src.tui import DetailsPane, ScraperApp
from tests.conftest import ASYNC_PAUSE


def _markup_of(label: Label) -> str:
    """The raw markup a Label was constructed with (Textual keeps it as-is)."""
    return label._Static__content


def _spans(markup: str):
    content = Content.from_markup(markup)
    return content.plain, content.spans


@pytest.mark.parametrize("columns,state", [
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
