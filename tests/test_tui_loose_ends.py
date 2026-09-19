"""Regression tests for the TUI loose ends in docs/code-issues.md (2, 11, 12, 13, 15)."""

import socket
import time
from unittest.mock import MagicMock, patch

import pytest
import requests
from textual.widgets import Button, Input, ListView, Select

from src.database import get_connection, insert_or_update_item
from src.tui import ScraperApp, SearchBuilder, SearchRow
from tests.conftest import ASYNC_PAUSE


@pytest.fixture
def mock_results():
    return [
        {
            "workshop_id": 1,
            "title": "Amazing Mod",
            "creator": "Author A",
            "consumer_appid": 294100,
            "extended_description": "This mod is truly amazing.",
            "tags": '["Graphic", "Utility"]',
        }
    ]


@pytest.mark.asyncio
async def test_removed_request_translation_button_is_harmless(mock_config):
    """Issue 11: the handler for the removed button must not survive."""
    with patch("src.tui.load_config", return_value=mock_config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)

            assert len(app.query("#btn-request-translation")) == 0

            ghost = MagicMock()
            ghost.id = "btn-request-translation"
            # Before the fix this branch called action_request_translation(),
            # which does not exist, so this raised AttributeError.
            await app.on_button_pressed(Button.Pressed(ghost))

            assert not hasattr(app, "action_request_translation")


@pytest.mark.asyncio
async def test_return_button_leaves_single_creator_mode_and_restores_filters(
    mock_config, mock_results
):
    """Issue 12: Return must clear the flag, restore the save button and filters."""
    with patch("src.tui.load_config", return_value=mock_config), \
         patch("src.tui.search_items", return_value=mock_results), \
         patch("src.tui.get_all_creator_ids", return_value=["Author A"]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            builder = app.query_one("#search-builder", SearchBuilder)

            # A filter the jump is going to replace.
            first_row = list(builder.query(SearchRow))[0]
            first_row.query_one("#field-select", Select).value = "Title"
            first_row.query_one("#op-select", Select).value = "contains"
            first_row.query_one("#value-input", Input).value = "Amazing"
            await pilot.pause(ASYNC_PAUSE)

            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            await pilot.pause(ASYNC_PAUSE)
            assert app.current_item_creator == "Author A"

            jump = app.query_one("#btn-jump-author", Button)
            await app.on_button_pressed(Button.Pressed(jump))
            await pilot.pause(ASYNC_PAUSE * 2)

            assert app.is_author_mode is True
            assert app.query_one("#btn-save-filter", Button).display is False
            assert app.query_one("#btn-return", Button).display is True
            assert builder.get_filters()[0]["field"] == "Author ID"

            # Before the fix btn-return had no handler at all, so all of the
            # assertions below failed with the app stuck in single-creator mode.
            back = app.query_one("#btn-return", Button)
            await app.on_button_pressed(Button.Pressed(back))
            await pilot.pause(ASYNC_PAUSE * 3)

            assert app.is_author_mode is False
            assert app.query_one("#btn-save-filter", Button).display is True
            assert app.query_one("#btn-return", Button).display is False

            restored = builder.get_filters()
            assert restored and restored[0]["field"] == "Title"
            assert restored[0]["value"] == "Amazing"


@pytest.mark.asyncio
async def test_update_visible_does_not_requeue_dead_items(db_path):
    """Issue 13: the TUI bulk update must carry the web route's status guard."""
    insert_or_update_item(
        db_path, {"workshop_id": 1, "title": "Dead Item", "status": -1, "api_priority": 0}
    )
    insert_or_update_item(
        db_path, {"workshop_id": 2, "title": "Live Item", "status": 200, "api_priority": 0}
    )
    insert_or_update_item(
        db_path, {"workshop_id": 3, "title": "Unfetched Item", "status": None, "api_priority": 0}
    )

    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}
    with patch("src.tui.load_config", return_value=config):
        app = ScraperApp()
        async with app.run_test(size=(120, 200)) as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one(ListView)
            assert len(list_view.children) == 3

            await app.action_update_visible()
            await pilot.pause(ASYNC_PAUSE)

    conn = get_connection(db_path)
    priorities = {
        row["workshop_id"]: row["api_priority"]
        for row in conn.execute("SELECT workshop_id, api_priority FROM workshop_items")
    }
    conn.close()

    # Without the guard the dead item was set back to 10 and re-queued.
    assert priorities[1] == 0, "a dead item must not be put back in the fetch queue"
    assert priorities[2] == 10
    assert priorities[3] == 10


def test_webserver_serves_on_the_port_it_reports(db_path):
    """Issue 2: the reported port is the socket the server actually holds.

    The old code bound a probe socket, closed it, and let Waitress bind again
    later. The ``save_config`` hook below lands exactly in that gap and takes the
    chosen port, which is what a second process could do; with the probe gone the
    server already owns the socket and the hook's bind fails.
    """
    busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    busy.bind(("0.0.0.0", 0))
    busy.listen(1)
    busy_port = busy.getsockname()[1]

    config = {
        "database": {"path": db_path},
        "logging": {"level": "INFO"},
        "web": {"port": busy_port},
    }
    stolen: list[socket.socket] = []

    def racing_save_config(path, cfg):
        racer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            racer.bind(("0.0.0.0", int(cfg["web"]["port"])))
        except OSError:
            racer.close()
            return
        racer.listen(1)
        stolen.append(racer)

    try:
        with patch("src.tui.load_config", return_value=config), \
             patch("src.tui.save_config", side_effect=racing_save_config):
            app = ScraperApp()
            port = app._web_port

        assert port and port != busy_port, "a busy configured port must fall back"
        assert config["web"]["port"] == port, "the chosen port must still be persisted"
        assert stolen == [], "the chosen port was free for another process to take"

        deadline = time.monotonic() + 5.0
        response = None
        last_error = None
        while time.monotonic() < deadline:
            try:
                response = requests.get(f"http://127.0.0.1:{port}/api/queued", timeout=1)
                break
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(0.05)

        assert response is not None, f"no server answered on its reported port {port}: {last_error}"
        assert response.status_code == 200
    finally:
        for racer in stolen:
            racer.close()
        busy.close()


def test_no_two_widgets_share_an_id():
    """Two screens answering to one id is how a duplicated handler appeared.

    `StatsScreen` and `SubscriptionQueueScreen` both used `btn-close-sub-queue`.
    Textual scopes queries per screen, so both worked, which is exactly why it
    went unnoticed -- the id named the wrong screen and nothing failed.
    """
    import collections
    import re
    from pathlib import Path

    source = Path("src/tui.py").read_text(encoding="utf-8")
    ids = re.findall(r'id="([A-Za-z0-9_-]+)"', source)
    duplicates = sorted(name for name, count in collections.Counter(ids).items() if count > 1)
    assert not duplicates, f"widget ids must be unique across the app: {duplicates}"
