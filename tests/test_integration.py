import pytest
from src.steam_api import get_workshop_details_api
from src.web_scraper import scrape_extended_details

@pytest.mark.integration
def test_live_steam_api_contract():
    """
    Hits the LIVE Steam API to verify the contract hasn't changed.
    Uses a known, highly popular public item (Garry's Mod: Extended Spawnmenu).
    """
    # No API key is strictly required for public items on this endpoint
    details = get_workshop_details_api(item_id=104603291, api_key="")
    
    assert details is not None, "Failed to reach Steam API or parse response."
    assert details["title"] == "Extended Spawnmenu"
    assert "description" in details, "Steam API stopped returning 'description'"
    assert "creator" in details, "Steam API stopped returning 'creator'"
    assert isinstance(details["tags"], list), "Tags format changed"

@pytest.mark.integration
def test_live_web_scraper_contract():
    """
    Hits the LIVE Steam Workshop website to verify the HTML DOM structure hasn't changed.
    """
    url = "https://steamcommunity.com/sharedfiles/filedetails/?id=104603291"
    details = scrape_extended_details(url)
    
    assert details is not None, "Failed to reach Steam Website or parse response."
    assert details["description"] is not None, "Failed to find the description element. DOM may have changed."
    assert "Garry's Mod" in details["description"]
    assert len(details["tags"]) > 0, "Failed to find tags in the DOM."
