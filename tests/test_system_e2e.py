import pytest
import responses
import threading
import time
import os
import json
from src.daemon import Daemon
from src.database import initialize_database, insert_or_update_item, search_items
from src.tui import ScraperApp
from textual.widgets import Input, ListView, Static

@pytest.fixture
def system_config(tmp_path):
    db_path = str(tmp_path / "system_e2e.db")
    initialize_database(db_path)
    
    # We must seed an initial item for the daemon to pick up
    # Using a real RimWorld item ID for live verification
    real_rimworld_id = 2838181007
    insert_or_update_item(db_path, {"workshop_id": real_rimworld_id})
    
    # Add dummy items to suppress automatic seeding/discovery expansion during test
    for i in range(101):
        insert_or_update_item(db_path, {"workshop_id": 99000 + i, "dt_attempted": "2026-01-01", "status": 200})
    
    return {
        "database": {"path": db_path},
        "api": {"key": os.environ.get("STEAM_API_KEY")},
        "daemon": {
            "batch_size": 1, 
            "request_delay_seconds": 0.5,
            "target_appids": [294100]
        }
    }

@pytest.mark.asyncio
async def test_end_to_end_system_flow(system_config):
    """
    Simulates the entire system using the LIVE Steam site.
    """
    if not system_config["api"]["key"]:
        pytest.skip("STEAM_API_KEY not set")

    # 2. Run the Daemon (synchronously for exactly one batch)
    daemon = Daemon(system_config)
    daemon.process_batch()
    
    # Verify DB state directly first
    # Search for an item we know exists or was just scraped
    from src.database import get_item_details
    item = get_item_details(system_config["database"]["path"], 2838181007)
    assert item is not None
    assert item["status"] == 200
    assert item["title"] is not None

    # 3. Spin up the TUI and search for what the Daemon just downloaded
    # We have to patch load_config so the TUI uses our temporary system_config
    from unittest.mock import patch
    with patch('src.tui.load_config', return_value=system_config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            
            # Setup the first SearchRow
            builder = app.query_one("#search-builder")
            rows = builder.query("SearchRow")
            first_row = list(rows)[0]
            
            first_row.query_one("#field-select").value = "Title"
            first_row.query_one("#op-select").value = "contains"
            
            # Type "E2E" into the value search
            value_input = first_row.query_one("#value-input")
            value_input.value = "E2E"
            
            # Explicitly execute search
            await app.execute_search()
            await pilot.pause(0.1) # Wait for the UI to update
            
            # Verify the ListView populated with the downloaded item
            list_view = app.query_one(ListView)
            assert len(list_view.children) == 1
            assert list_view.children[0].item_data["title"] == "E2E Final Boss Mod"
            
            # Select it and verify the extended description from the web scraper
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")

            from src.tui import DetailsPane
            from textual.widgets import Markdown
            detail_pane = app.query_one("#item-details", DetailsPane)
            detail_content = detail_pane.query_one("#detail-content", Markdown)

            content = str(detail_content._markdown)
            assert "The ultimate test" in content
            assert "E2E" in content # From tags

