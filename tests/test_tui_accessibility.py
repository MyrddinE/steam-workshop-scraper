import pytest
from textual.color import Color
from textual.widgets import Label, Button, Select, ListItem, ListView, Markdown
from src.tui import ScraperApp, DetailsPane
from tests.conftest import ASYNC_PAUSE
from unittest.mock import patch

@pytest.fixture
def mock_config():
    return {
        "database": {"path": "test.db"},
        "logging": {"level": "INFO"}
    }

def is_readable(widget):
    """Checks if text is readable by comparing brightness of FG and BG."""
    style = widget.rich_style
    fg = Color.from_rich_color(style.color)
    bg = Color.from_rich_color(style.bgcolor)
    
    # Very simple check: brightness difference should be significant
    diff = abs(fg.brightness - bg.brightness)
    return diff > 0.3 # 0.3 is a loose threshold for basic visibility

@pytest.mark.asyncio
async def test_main_ui_contrast(mock_config):
    """Check contrast of primary static elements."""
    app = ScraperApp()
    async with app.run_test() as pilot:
        btn = app.query_one("#btn-execute-search")
        assert is_readable(btn)

@pytest.mark.asyncio
async def test_details_pane_contrast(mock_config):
    """Check contrast of the redesigned Details pane elements."""
    results = [{
        "workshop_id": 1, "title": "Test Item", "creator": "123", 
        "personaname": "Author Name", "file_size": 1024,
        "time_created": 1000, "views": 10, "subscriptions": 5, "favorited": 2,
        "tags": '["Tag1"]', "dt_updated": None, "dt_found": "2023", "status": 200
    }]
    
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=results), \
         patch('src.tui.get_item_details', return_value=results[0]), \
         patch('src.tui.get_all_authors', return_value=[]):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one(ListView)
            list_view.index = 0
            await pilot.pause(ASYNC_PAUSE)
            
            detail_pane = app.query_one("#item-details", DetailsPane)
            title = detail_pane.query_one("#item-title")
            assert is_readable(title)

@pytest.mark.asyncio
async def test_command_palette_contrast(mock_config):
    """Check contrast of items in the Command Palette."""
    app = ScraperApp()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause(ASYNC_PAUSE)
        
        # Check the top-most screen (should be CommandPalette)
        top_screen = pilot.app.screen_stack[-1]
        from textual.command import CommandPalette
        assert isinstance(top_screen, CommandPalette)
        
        # Type something to get hits
        await pilot.press("c", "l", "e", "a", "r")
        await pilot.pause(ASYNC_PAUSE * 2)
        
        # Verify there are hits in the CommandList
        from textual.command import CommandList
        command_list = top_screen.query_one(CommandList)
        assert command_list.option_count > 0
        
        # Check readability of the first hit
        # OptionList rendering is complex, but we can try to find the internal widgets if any,
        # or just check the style of the list itself.
        assert is_readable(command_list)
        
        cp_input = top_screen.query_one("CommandInput")
        assert is_readable(cp_input)

@pytest.mark.asyncio
async def test_select_dropdown_contrast(mock_config):
    """Check contrast of Select dropdown menus."""
    app = ScraperApp()
    async with app.run_test() as pilot:
        await pilot.click(Select)
        await pilot.pause(ASYNC_PAUSE)
        
        # Select opens an overlay on the current screen
        from textual.widgets._select import SelectOverlay
        overlay = pilot.app.screen.query_one(SelectOverlay)
        assert is_readable(overlay)
