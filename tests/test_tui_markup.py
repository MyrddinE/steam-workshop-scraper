import pytest
from textual.widgets import Static, ListView
from src.tui import ScraperApp
from unittest.mock import patch

@pytest.fixture
def mock_config():
    return {
        "database": {"path": "test.db"},
        "logging": {"level": "INFO"}
    }

@pytest.fixture
def mock_results_with_bbcode():
    return [
        {
            "workshop_id": 12345,
            "title": "[b]Bold Title[/b]",
            "creator": "Author [X]",
            "consumer_appid": 294100,
            "short_description": "Short [i]Italic[/i] description.",
            "extended_description": "[url=https://example.com] [img]https://example.com/img.gif[/img] [/url]",
            "tags": '["[Tag]"]'
        }
    ]

@pytest.mark.asyncio
async def test_tui_no_markup_error_on_bbcode(mock_config, mock_results_with_bbcode):
    """
    Verifies that the DetailsPane does not crash when encountering Steam BBCode 
    or strings that look like Rich markup (e.g. [b], [url]).
    """
    def get_details_mock(db, wid):
        return mock_results_with_bbcode[0]

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results_with_bbcode), \
         patch('src.tui.get_item_details', side_effect=get_details_mock), \
         patch('src.tui.get_all_authors', return_value=[]):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(0.1)

            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            
            # If we reached here without an exception, the fix worked.
            # We specifically want to check the Static widget in DetailsPane
            from src.tui import DetailsPane
            detail_pane = app.query_one("#item-details", DetailsPane)
            detail_content = detail_pane.query_one("#detail-content", Static)
            
            # Verify markup is disabled (internal attribute check)
            assert detail_content._render_markup is False
            
            # Verify content is rendered as plain text
            content = str(detail_content.render())
            assert "[b]Bold Title[/b]" in content
            assert "[url=https://example.com]" in content
