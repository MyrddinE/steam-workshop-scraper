import pytest
from textual.widgets import Input, ListItem, Static, ListView, Select, Button
from src.tui import ScraperApp
from unittest.mock import patch, MagicMock

@pytest.fixture
def mock_config():
    return {
        "database": {"path": "test.db"},
        "logging": {"level": "INFO"}
    }

@pytest.fixture
def mock_results():
    return [
        {
            "workshop_id": 1,
            "title": "Amazing Mod",
            "creator": "Author A",
            "consumer_appid": 294100,
            "extended_description": "This mod is truly amazing.",
            "tags": '["Graphic", "Utility"]'
        }
    ]

@pytest.mark.asyncio
async def test_tui_advanced_search_flow(mock_config, mock_results):
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_all_authors', return_value=["Author A", "Author B"]):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            # Verify separate inputs exist
            title_input = app.query_one("#search-title", Input)
            desc_input = app.query_one("#search-desc", Input)
            author_select = app.query_one("#search-author", Select)

            assert title_input.value == ""
            
            # Verify new inputs exist
            app.query_one("#search-filename", Input)
            app.query_one("#search-tags", Input)
            app.query_one("#search-subscriptions", Input)
            app.query_one("#search-views", Input)
            
            # Type in title and hit enter
            await pilot.click("#search-title")
            await pilot.press(*"Amazing")
            await pilot.press("enter")
            
            # Verify results appear
            list_view = app.query_one(ListView)
            assert len(list_view.children) == 1
            
            # Select the item
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            
            detail_pane = app.query_one("#item-details", Static)
            content = str(detail_pane.render())
            assert "Amazing Mod" in content

@pytest.mark.asyncio
async def test_tui_jump_to_author(mock_config, mock_results):
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_all_authors', return_value=["Author A"]):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            # Populate list
            await pilot.click("#search-title")
            await pilot.press("enter")
            
            # Select item
            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            
            # Wait for ListView.Selected event to process and layout to un-hide the button
            await pilot.pause(0.1)
            
            # Click 'Jump to Author' button using official widget method
            jump_btn = app.query_one("#btn-jump-author", Button)
            jump_btn.press()
            
            # Wait for button press event to process and execute search
            await pilot.pause(0.1)
            
            # Verify Author Select is updated and title is cleared
            author_select = app.query_one("#search-author", Select)
            title_input = app.query_one("#search-title", Input)
            
            assert title_input.value == ""
            assert author_select.value == "Author A"
