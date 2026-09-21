"""The TUI half of the creator-ignore feature.

The creator-scoped view is the ``btn-jump-author`` filtered result list, and it
knows its creator from ``ScraperApp.author_mode_creator``: the jump sets it from
the highlighted row, Return clears it, and the ignore action reads it rather than
``current_item_creator`` -- because the first press settles every item of the
creator and the re-query leaves the list empty, so there may be no highlighted
row left while the button must still reverse the change.

The action calls ``toggle_creator_ignored`` and labels the button from
``creator_ignore_label``, both in ``src/database``, so it cannot disagree with the
web route about direction or wording. The whole view is re-queried, not patched
in place.
"""

from unittest.mock import patch

import pytest
from textual.widgets import Button, ListView

from src import database
from src.database import (
    get_connection,
    insert_or_update_item,
)
from tests.conftest import ASYNC_PAUSE

CREATOR = 111
OTHER = 222


def _row(db_path, workshop_id, columns="*"):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT {columns} FROM workshop_items WHERE workshop_id = ?",
            (workshop_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _seed_item(db_path, workshop_id, creator, **over):
    record = {
        "workshop_id": workshop_id,
        "title": f"item {workshop_id}",
        "creator_steamid": creator,
        "fetch_status": 200,
        "api_fetched_at": 123,
        "extended_description": "present",
    }
    record.update(over)
    insert_or_update_item(db_path, record)


def _app_config(db_path):
    return {"database": {"path": db_path}, "logging": {"level": "INFO"}}


async def _enter_author_mode(app, pilot, creator):
    """Set the highlighted creator and press Jump to Author, as a person would."""
    list_view = app.query_one(ListView)
    list_view.index = 0
    app.set_focus(list_view)
    await pilot.pause(ASYNC_PAUSE)
    assert app.current_item_creator == creator
    await app.on_button_pressed(
        Button.Pressed(app.query_one("#btn-jump-author", Button)))
    await pilot.pause(ASYNC_PAUSE * 2)


@pytest.mark.asyncio
async def test_the_creator_toggle_is_in_the_search_row_and_hidden_until_author_mode(db_path):
    from src.tui import ScraperApp

    _seed_item(db_path, 1, CREATOR)

    with patch("src.tui.load_config", return_value=_app_config(db_path)), \
         patch("src.tui.get_all_creator_ids", return_value=[str(CREATOR)]):
        app = ScraperApp()
        async with app.run_test(size=(120, 200)) as pilot:
            await pilot.pause(ASYNC_PAUSE)
            button = app.query_one("#btn-ignore-creator", Button)
            assert button.display is False, "the toggle belongs to author mode alone"

            await _enter_author_mode(app, pilot, CREATOR)

            assert button.display is True
            assert str(button.label) == database.creator_ignore_label(False), \
                "the label names the next press and comes from the shared function"
            assert app.author_mode_creator == CREATOR

            row_ids = [child.id for child in button.parent.children]
            assert abs(row_ids.index("btn-ignore-creator")
                       - row_ids.index("btn-return")) > 1, \
                "the toggle must sit near Return but not beside it"


@pytest.mark.asyncio
async def test_the_toggle_settles_the_viewed_creators_items_and_requeries(db_path):
    """One press ignores the creator's items and they leave the re-queried view.

    The second press proves the action reads the stored creator rather than the
    highlighted row: by then the list is empty, so a row-derived creator would
    have nothing to act on.
    """
    from src.tui import ScraperApp

    _seed_item(db_path, 1, CREATOR)
    _seed_item(db_path, 2, OTHER)

    with patch("src.tui.load_config", return_value=_app_config(db_path)), \
         patch("src.tui.get_all_creator_ids", return_value=[str(CREATOR), str(OTHER)]):
        app = ScraperApp()
        async with app.run_test(size=(120, 200)) as pilot:
            await pilot.pause(ASYNC_PAUSE)
            await _enter_author_mode(app, pilot, CREATOR)
            button = app.query_one("#btn-ignore-creator", Button)

            await app.on_button_pressed(Button.Pressed(button))
            await pilot.pause(ASYNC_PAUSE * 3)

            assert _row(db_path, 1)["fetch_status"] == -2, "the viewed creator settles"
            assert _row(db_path, 2)["fetch_status"] == 200, \
                "another creator's item is untouched"
            assert str(button.label) == database.creator_ignore_label(True)
            assert len(app.query_one(ListView).children) == 0, \
                "the settled item left the re-queried view"

            # The list is empty now; the reverse must still work.
            await app.on_button_pressed(Button.Pressed(button))
            await pilot.pause(ASYNC_PAUSE * 3)

            assert _row(db_path, 1)["fetch_status"] == 200
            assert str(button.label) == database.creator_ignore_label(False)
            assert len(app.query_one(ListView).children) == 1, \
                "the restored item came back on the re-query"


@pytest.mark.asyncio
async def test_entering_for_an_already_ignored_creator_labels_the_reverse(db_path):
    from src.tui import ScraperApp

    _seed_item(db_path, 1, CREATOR)
    database.ignore_creator(db_path, CREATOR)  # the item is now settled and hidden

    with patch("src.tui.load_config", return_value=_app_config(db_path)), \
         patch("src.tui.get_all_creator_ids", return_value=[]):
        app = ScraperApp()
        async with app.run_test(size=(120, 200)) as pilot:
            await pilot.pause(ASYNC_PAUSE)
            # Author mode can still be entered for the creator (from a picker or
            # a stale view), so the control must label itself from the stored
            # flag rather than defaulting to "Ignore creator".
            app.current_item_creator = CREATOR
            await app.on_button_pressed(
                Button.Pressed(app.query_one("#btn-jump-author", Button)))
            await pilot.pause(ASYNC_PAUSE * 2)

            button = app.query_one("#btn-ignore-creator", Button)
            assert str(button.label) == database.creator_ignore_label(True)


@pytest.mark.asyncio
async def test_the_toggle_acts_on_the_viewed_creator_not_the_highlight(db_path):
    """It moves the creator the view is pinned to, never the selected item.

    The author filter normally keeps every visible row on the viewed creator, so
    this sets the highlight's creator to another account directly to pin the
    contract: the action reads `author_mode_creator`, not `current_item_creator`.
    """
    from src.tui import ScraperApp

    _seed_item(db_path, 1, CREATOR)
    _seed_item(db_path, 2, OTHER)

    with patch("src.tui.load_config", return_value=_app_config(db_path)), \
         patch("src.tui.get_all_creator_ids", return_value=[str(CREATOR), str(OTHER)]):
        app = ScraperApp()
        async with app.run_test(size=(120, 200)) as pilot:
            await pilot.pause(ASYNC_PAUSE)
            await _enter_author_mode(app, pilot, CREATOR)
            app.current_item_creator = OTHER

            await app.on_button_pressed(
                Button.Pressed(app.query_one("#btn-ignore-creator", Button)))
            await pilot.pause(ASYNC_PAUSE * 3)

            assert _row(db_path, 1)["fetch_status"] == -2, \
                "the creator being viewed is the one the toggle moves"
            assert _row(db_path, 2)["fetch_status"] == 200, \
                "the highlighted item's creator is not"


@pytest.mark.asyncio
async def test_return_hides_the_creator_toggle_and_clears_the_creator(db_path):
    from src.tui import ScraperApp

    _seed_item(db_path, 1, CREATOR)

    with patch("src.tui.load_config", return_value=_app_config(db_path)), \
         patch("src.tui.get_all_creator_ids", return_value=[str(CREATOR)]):
        app = ScraperApp()
        async with app.run_test(size=(120, 200)) as pilot:
            await pilot.pause(ASYNC_PAUSE)
            await _enter_author_mode(app, pilot, CREATOR)

            await app.on_button_pressed(
                Button.Pressed(app.query_one("#btn-return", Button)))
            await pilot.pause(ASYNC_PAUSE * 3)

            assert app.is_author_mode is False
            assert app.author_mode_creator is None
            assert app.query_one("#btn-ignore-creator", Button).display is False
