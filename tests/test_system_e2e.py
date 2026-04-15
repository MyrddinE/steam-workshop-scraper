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
    insert_or_update_item(db_path, {"workshop_id": 8888})
    
    return {
        "database": {"path": db_path},
        "api": {"key": "E2E_KEY"},
        "daemon": {"batch_size": 1, "request_delay_seconds": 0.05}
    }

@pytest.mark.asyncio
@responses.activate
async def test_end_to_end_system_flow(system_config):
    """
    Simulates the entire system:
    1. The Daemon fetching from 'Steam'.
    2. The Database storing it.
    3. The TUI searching and displaying it.
    """
    
    # 1. Mock the Steam API and Website
    api_url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    api_mock_data = {
        "response": {
            "result": 1,
            "resultcount": 1,
            "publishedfiledetails": [
                {
                    "publishedfileid": "8888",
                    "result": 1,
                    "title": "E2E Final Boss Mod",
                    "creator": "System Tester",
                    "subscriptions": 9999
                }
            ]
        }
    }
    responses.add(responses.POST, api_url, json=api_mock_data, status=200)

    web_url = "https://steamcommunity.com/sharedfiles/filedetails/?id=8888"
    web_mock_html = '''
    <html><body>
        <div class="workshopItemDescription" id="highlightContent">The ultimate test.</div>
        <div class="workshopTags"><a href="#">E2E</a></div>
    </body></html>
    '''
    responses.add(responses.GET, web_url, body=web_mock_html, status=200, content_type="text/html")

    # 2. Run the Daemon (synchronously for exactly one batch)
    daemon = Daemon(system_config)
    daemon.process_batch()
    
    # Verify DB state directly first
    db_results = search_items(system_config["database"]["path"], title_query="E2E")
    assert len(db_results) == 1
    assert db_results[0]["status"] == 200

    # 3. Spin up the TUI and search for what the Daemon just downloaded
    # We have to patch load_config so the TUI uses our temporary system_config
    from unittest.mock import patch
    with patch('src.tui.load_config', return_value=system_config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            
            # Type "E2E" into the title search
            title_input = app.query_one("#search-title", Input)
            title_input.value = "E2E"
            
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
            
            detail_pane = app.query_one("#item-details", Static)
            content = str(detail_pane.render())
            assert "The ultimate test" in content
            assert "E2E" in content # From tags
