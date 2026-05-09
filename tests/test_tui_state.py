import os
import pytest
from src.tui import load_tui_state, save_tui_state
from tests.conftest import ASYNC_PAUSE

def test_save_and_load_tui_state(tmp_path):
    state_file = tmp_path / ".tui_state.yaml"
    state = {
        "sort_by": "file_size",
        "sort_order": "DESC",
        "scroll_y": 15,
        "selected_workshop_id": 999,
        "filters": [{"field": "Title", "op": "contains", "value": "test", "logic": "AND"}]
    }
    
    save_tui_state(str(state_file), state)
    assert os.path.exists(state_file)
    
    loaded = load_tui_state(str(state_file))
    assert loaded == state

def test_load_tui_state_missing(tmp_path):
    state_file = tmp_path / "nonexistent.yaml"
    loaded = load_tui_state(str(state_file))
    assert loaded == {}

@pytest.mark.asyncio
async def test_tui_app_loads_state(mock_config):
    from src.tui import ScraperApp, SearchBuilder
    from textual.widgets import Select, ListView
    from unittest.mock import patch

    state = {
        "sort_by": "file_size",
        "sort_order": "DESC",
        "filters": [
            {"field": "Title", "op": "contains", "value": "test_title_state"}
        ],
        "scroll_y": 12,
        "selected_workshop_id": 123
    }
    
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.load_tui_state', return_value=state), \
         patch('src.tui.save_tui_state') as mock_save:
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            
            # Verify sort was applied
            assert app.query_one("#sort-by", Select).value == "file_size"
            assert app.query_one("#sort-order", Select).value == "DESC"
            
            # Verify filters applied
            builder = app.query_one("#search-builder", SearchBuilder)
            filters = builder.get_filters()
            assert len(filters) == 1
            assert filters[0]["field"] == "Title"
            assert filters[0]["value"] == "test_title_state"
            
            # Verify scroll loaded
            assert app._restored_scroll_y == 12
            
            # Verify selected item
            assert app._restored_selected_id == 123

@pytest.mark.asyncio
async def test_tui_app_saves_state(mock_config):
    from src.tui import ScraperApp
    from textual.widgets import Select
    from unittest.mock import patch
    
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.load_tui_state', return_value={}), \
         patch('src.tui.save_tui_state') as mock_save:
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            
            # Change sort order
            sort_order = app.query_one("#sort-order", Select)
            sort_order.value = "DESC"
            await pilot.pause(ASYNC_PAUSE)
            
            # It should have called save_tui_state
            assert mock_save.called
            
            # Check what was saved
            saved_state = mock_save.call_args[0][1]
            assert saved_state["sort_order"] == "DESC"
