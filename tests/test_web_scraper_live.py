import pytest
import os
import time
import logging
from src.web_scraper import discover_items_by_date_html
from src.steam_api import get_workshop_details_api # To verify tags

API_KEY = os.environ.get("STEAM_API_KEY")

@pytest.mark.skipif(not API_KEY, reason="STEAM_API_KEY environment variable not set. Skipping live web scraper tests.")
class TestLiveWebScraper:

    # Use RimWorld (AppID 294100) for live tests
    APPID = 294100

    def test_positive_filter_discovery(self):
        """
        Live test: Discover items with a specific text and required tag, then verify results.
        """
        logging.info("Running positive live web scraper test (RimWorld: Vampire + Translation, 12 months).")
        search_text = "Vampire"
        required_tags = ["Translation"]
        
        # Use a 12-month date range for more consistent results
        end_date = int(time.time())
        start_date = end_date - (365 * 24 * 3600) # Last 12 months

        items = discover_items_by_date_html(
            self.APPID, start_date, end_date, 
            search_text=search_text, 
            required_tags=required_tags
        )
        
        assert len(items) > 0, "Should find some items for 'Vampire' with 'Translation' tag in the last 12 months."
        
        for item_id in items:
            # Verify the tags using the Steam API, as web scraper doesn't parse all tags.
            details = get_workshop_details_api(item_id, API_KEY)
            assert details is not None
            item_tags = [t.get("tag") for t in details.get("tags", []) if isinstance(t, dict)]
            assert required_tags[0] in item_tags

        logging.info(f"Found {len(items)} items matching positive filter.")

    def test_negative_filter_discovery(self):
        """
        Live test: Discover items excluding a specific tag, then verify results.
        Sort by trend to get popular items, then check if the excluded tag is absent.
        """
        logging.info("Running negative live web scraper test (RimWorld: exclude 1.6 from trend, 12 months). ")
        excluded_tag = "1.6"
        
        # Sort by trend with a 12-month period
        # Note: discover_items_by_date_html uses browsesort=mostrecent for date filtering
        # For this test, we need to ensure the exclusion works, so we'll rely on the date range
        # and check the results.

        end_date = int(time.time())
        start_date = end_date - (365 * 24 * 3600) # Last 12 months

        items = discover_items_by_date_html(
            self.APPID, start_date, end_date, 
            excluded_tags=[excluded_tag]
        )

        assert len(items) > 0, "Should find items when excluding a tag."
        
        # Fetch details for some top items to verify exclusion using the API
        # Limiting to top few to avoid excessive API calls
        for item_id in items[:5]: # Check only the first 5 results
            details = get_workshop_details_api(item_id, API_KEY)
            assert details is not None
            item_tags = [t.get("tag") for t in details.get("tags", []) if isinstance(t, dict)]
            
            assert excluded_tag not in item_tags, f"Item {item_id} has excluded tag {excluded_tag}"
        
        logging.info(f"Found {len(items)} items and verified excluded tag {excluded_tag} is absent in top 5 results.")
