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
            "extended_description": """
[h1]Welcome[/h1]
[list]
[*] Item 1
[*] Item 2
[/list]
[table]
[tr][th]Col 1[/th][th]Col 2[/th][/tr]
[tr][td]Val 1[/td][td]Val 2[/td][/tr]
[/table]
[quote=Someone]Hello world[/quote]
[code]print('hi')[/code]
[url=https://example.com] [img]https://example.com/img.gif[/img] [/url]
""",
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
            
            # Specifically check the title label and markdown content separately
            from src.tui import DetailsPane
            from textual.widgets import Markdown, Label
            detail_pane = app.query_one("#item-details", DetailsPane)
            title_label = detail_pane.query_one("#item-title", Label)
            detail_content = detail_pane.query_one("#detail-content", Markdown)
            
            # Wait for any async updates
            await pilot.pause(0.1)
            
            # Verify title is converted to bold in Label (using render() to get the text with styles)
            assert "Bold Title" in str(title_label.render())
            
            # Verify description content is converted to Markdown formatting
            content = str(detail_content._markdown)
            assert "# Welcome" in content
            assert "* Item 1" in content
            assert "Col 1" in content
            assert "> **Someone said:**" in content
            assert "```" in content
            assert "https://example.com" in content

def test_bbcode_to_markdown_empty():
    from src.tui import bbcode_to_markdown
    assert bbcode_to_markdown(None) == ""
    assert bbcode_to_markdown("") == ""
