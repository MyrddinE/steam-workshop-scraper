import pytest
import random
from src.tui import ScraperApp, SearchBuilder
from tests.conftest import ASYNC_PAUSE
from textual.widgets import Select, Input

@pytest.mark.asyncio
async def test_random_filters_save_load(mock_config):
    from unittest.mock import patch
    
    # Generate 5 random 5-part filters
    fields = [
        "Title", "Description", "Filename", "Tags", "Author ID",
        "File Size", "Subs", "Favs", "Views", "Workshop ID", "AppID", "Language ID"
    ]
    operators_map = {
        "text": ["contains", "does_not_contain", "is", "is_not", "is_empty", "is_not_empty"],
        "numeric": ["is", "is_not", "gt", "lt", "gte", "lte", "is_empty", "is_not_empty"],
        "id": ["is", "is_not"]
    }
    
    random_filters = []
    for _ in range(5):
        filter_set = []
        for i in range(5):
            field = random.choice(fields)
            if field in ["Author ID", "Workshop ID", "AppID"]:
                op_type = "id"
            elif field in ["File Size", "Subs", "Favs", "Views", "Language ID"]:
                op_type = "numeric"
            else:
                op_type = "text"
            op = random.choice(operators_map[op_type])
            val = f"test_val_{random.randint(1, 100)}"
            
            f = {"field": field, "op": op, "value": val}
            if i > 0:
                f["logic"] = random.choice(["AND", "OR"])
            filter_set.append(f)
        random_filters.append(filter_set)

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.load_tui_state', return_value={}), \
         patch('src.tui.save_tui_state'):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            
            builder = app.query_one("#search-builder", SearchBuilder)
            
            for i, filters in enumerate(random_filters):
                # Apply the filters
                builder.set_filters(filters)
                
                # Wait for call_after_refresh to run
                await pilot.pause(ASYNC_PAUSE * 2)
                
                # Retrieve the filters
                loaded_filters = builder.get_filters()
                
                # Compare
                assert loaded_filters == filters, f"Filter set {i} failed to load back as-is.\nExpected: {filters}\nGot: {loaded_filters}"
