import pytest
from textual.widgets import ListView
from src.tui import ScraperApp, SubscriptionQueueScreen
from unittest.mock import patch
from tests.conftest import ASYNC_PAUSE

@pytest.mark.asyncio
async def test_tui_toggle_subscription_queue(mock_config):
    mock_results = [
        {"workshop_id": 1, "title": "Item 1", "creator": "A", "is_queued_for_subscription": 0},
        {"workshop_id": 2, "title": "Item 2", "creator": "B", "is_queued_for_subscription": 0},
    ]

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.toggle_subscription_queue_status') as mock_toggle_db:
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)

            list_view = app.query_one("#results-list", ListView)
            list_view.index = 0
            original_item = list_view.highlighted_child

            assert original_item.item_data["is_queued_for_subscription"] == 0

            await pilot.press("s")
            await pilot.pause(ASYNC_PAUSE)

            mock_toggle_db.assert_called_once_with(mock_config["database"]["path"], 1)
            assert original_item.item_data["is_queued_for_subscription"] == 1
            assert list_view.index == 1

@pytest.mark.asyncio
async def test_tui_show_subscription_queue(mock_config, tmp_path):
    lock_file = tmp_path / ".pauselock"

    app = ScraperApp()
    app.pause_lock_file = str(lock_file)
    async with app.run_test() as pilot:
        await pilot.pause(ASYNC_PAUSE)

        assert not lock_file.exists()

        await pilot.press("l")
        await pilot.pause(ASYNC_PAUSE)

        assert isinstance(app.screen, SubscriptionQueueScreen)
        assert lock_file.exists()

        await pilot.click("#btn-close-sub-queue")
        await pilot.pause(ASYNC_PAUSE)

        assert not isinstance(app.screen, SubscriptionQueueScreen)
        assert not lock_file.exists()
