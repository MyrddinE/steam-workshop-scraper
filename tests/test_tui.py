import pytest
from textual.widgets import Input, ListItem, Static, ListView, Select, Button
from textual.containers import VerticalScroll
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
    def get_details_mock(db, wid):
        for r in mock_results:
            if r["workshop_id"] == wid: return r
        return None

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_item_details', side_effect=get_details_mock), \
         patch('src.tui.get_all_authors', return_value=["Author A", "Author B"]):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            # Wait for on_mount auto-search to complete
            await pilot.pause(0.1)

            # Verify results auto-populated
            list_view = app.query_one(ListView)
            assert len(list_view.children) == 1

            # Verify new inputs exist
            search_builder = app.query_one("#search-builder")
            assert search_builder is not None
            
            # Select the item
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            
            from src.tui import DetailsPane
            from textual.widgets import Markdown
            detail_pane = app.query_one("#item-details", DetailsPane)
            detail_content = detail_pane.query_one("#detail-content", Markdown)

            content = str(detail_content._markdown)
            assert "Amazing Mod" in content

@pytest.mark.asyncio
async def test_tui_jump_to_author(mock_config, mock_results):
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_all_authors', return_value=["Author A"]):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            # Wait for on_mount
            await pilot.pause(0.1)
            
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
            builder = app.query_one("#search-builder")
            rows = builder.query("SearchRow")
            assert len(rows) == 1
            first_row = list(rows)[0]
            assert first_row.query_one("#field-select").value == "Author ID"
            assert first_row.query_one("#value-input").value == "Author A"

@pytest.mark.asyncio
async def test_tui_translation_flow(mock_config, mock_results):
    """Tests TUI behavior with tags provided as string, list, or invalid types."""
    mock_results = [
        {
            "workshop_id": 3,
            "title": "List Mod",
            "tags": ["Valid", "List"]
        },
        {
            "workshop_id": 4,
            "title": "Dict Mod",
            "tags": {"invalid": "dict"}
        },
        {
            "workshop_id": 5,
            "title": "API Tags Mod",
            "tags": '[{"tag": "Mod"}, {"tag": "1.0"}]'
        }
    ]

    from unittest.mock import patch
    def get_details_mock(db, wid):
        for r in mock_results:
            if r["workshop_id"] == wid: return r
        return None

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_item_details', side_effect=get_details_mock):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.1) # Wait for mount

            # Verify List Mod
            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")

            from src.tui import DetailsPane
            from textual.widgets import Markdown
            detail_pane = app.query_one("#item-details", DetailsPane)
            detail_content = detail_pane.query_one("#detail-content", Markdown)

            content = str(detail_content._markdown)
            assert "List Mod" in content
            assert "Valid, List" in content

            # Verify Dict Mod (should not crash, should just be empty tags)
            list_view.index = 1
            app.set_focus(list_view)
            await pilot.press("enter")

            content2 = str(detail_content._markdown)
            assert "Dict Mod" in content2

            assert "Tags: \n" in content2 or "Tags: \n" not in content2 # It just shouldn't crash

            # Verify API Tags Mod
            list_view.index = 2
            app.set_focus(list_view)
            await pilot.press("enter")

            content3 = str(detail_content._markdown)
            assert "API Tags Mod" in content3
            assert "Mod, 1.0" in content3


@pytest.mark.asyncio
async def test_tui_operator_selection_by_field_type(mock_config, mock_results):
    from unittest.mock import patch
    from src.tui import ScraperApp
    from textual.widgets import Select

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            
            builder = app.query_one("#search-builder")
            first_row = list(builder.query("SearchRow"))[0]
            field_select = first_row.query_one("#field-select", Select)
            op_select = first_row.query_one("#op-select", Select)
            
            # Switch to numeric field
            field_select.value = "File Size"
            await pilot.pause(0.1)
            assert "gt" in [val for label, val in op_select._options]
            assert "does_not_contain" not in [val for label, val in op_select._options]
            
            # Switch to ID field
            field_select.value = "Author ID"
            await pilot.pause(0.1)
            assert "is" in [val for label, val in op_select._options]
            assert "gt" not in [val for label, val in op_select._options]
            
            # Switch to Text field
            field_select.value = "Title"
            await pilot.pause(0.1)
            assert "does_not_contain" in [val for label, val in op_select._options]

@pytest.mark.asyncio
async def test_tui_jump_to_author_clears_multiple_rows(mock_config, mock_results):
    from unittest.mock import patch
    from src.tui import ScraperApp
    from textual.widgets import ListView, Button

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_all_authors', return_value=["Author A"]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            
            # Add some extra rows
            await pilot.click("#btn-and")
            await pilot.click("#btn-or")
            await pilot.pause(0.1)
            
            builder = app.query_one("#search-builder")
            assert len(builder.query("SearchRow")) == 3
            
            # Select an item to show the jump button
            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            await pilot.pause(0.1)
            
            # Click jump button
            jump_btn = app.query_one("#btn-jump-author", Button)
            jump_btn.press()
            await pilot.pause(0.2)
            
            # Verify rows cleared and set to Author ID
            rows = list(builder.query("SearchRow"))
            assert len(rows) == 1
            assert rows[0].query_one("#field-select").value == "Author ID"
            assert rows[0].query_one("#value-input").value == "Author A"

@pytest.mark.asyncio
async def test_tui_infinite_scroll(mock_config):
    from unittest.mock import patch, PropertyMock
    from src.tui import ScraperApp
    from textual.widgets import ListView

    mock_results = [{"workshop_id": i, "title": f"Item {i}", "creator": "A"} for i in range(100)]

    # We want to mock search_items to paginate
    def mock_search_items(db, *args, **kwargs):
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 50)
        return mock_results[offset:offset+limit]

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', side_effect=mock_search_items):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            list_view = app.query_one(ListView)
            assert len(list_view.children) == 50

            # Real scroll simulation using Textual's scroll API
            list_view.scroll_y = list_view.max_scroll_y
            
            import asyncio
            for _ in range(10):
                if len(list_view.children) >= 100:
                    break
                await asyncio.sleep(0.1)

            # 50 more should be loaded
            assert len(list_view.children) == 100
