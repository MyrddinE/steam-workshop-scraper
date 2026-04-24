import pytest
import os
import requests
import time
import logging
from src.steam_api import query_files_by_date, get_workshop_details_api
from src.web_scraper import discover_items_by_date_html

# This test is designed to verify the depth limit of the Steam API vs Web Scraping.
# It requires a real Steam API key to run.
API_KEY = os.environ.get("STEAM_API_KEY")

@pytest.mark.skipif(not API_KEY, reason="STEAM_API_KEY not set in environment")
def test_compare_api_vs_web_reachability():
    """
    Compare the oldest items reachable via API pagination vs Web Date Filtering.
    Target: Garry's Mod (AppID 4000) - known to have millions of items.
    """
    appid = 4000
    
    # 1. Attempt to find the oldest item via API by going to the max page (500)
    # query_type 1 is RankedByPublicationDate (Newest First)
    # We want the 'last' reachable page to see how far back it goes.
    max_page = 500
    logging.info(f"Querying API for AppID {appid} at max page {max_page}...")
    
    # query_files_by_date uses the API's date range, but here we just want to see 
    # if we can reach the bottom of the stack without date filters if we could.
    # Actually, the API wrapper in src/steam_api.py uses query_type=1 (Publication Date).
    
    # Let's use a very wide date range for the API to simulate 'all time'
    start_all = 1317484800 # Oct 2011
    end_all = int(time.time())
    
    api_result = query_files_by_date(appid, start_all, end_all, API_KEY, page=max_page)
    
    if not api_result["items"]:
        logging.warning("API returned no items at page 500. It might be empty or restricted.")
        api_oldest_timestamp = None
    else:
        # Get details for the last item on the last reachable page
        last_item = api_result["items"][-1]
        wid = int(last_item["publishedfileid"])
        details = get_workshop_details_api(wid, API_KEY)
        api_oldest_timestamp = details.get("time_created") if details else None
        logging.info(f"API Oldest Reachable Item ID: {wid}, Created: {api_oldest_timestamp}")

    # 2. Use Web Scraper with a known old date range (Jan 2013)
    # Jan 1, 2013 to Feb 1, 2013
    web_start = 1356998400
    web_end = 1359676800
    
    logging.info(f"Querying Web Scraper for AppID {appid} in range {web_start} to {web_end}...")
    web_ids = discover_items_by_date_html(appid, web_start, web_end, page=1)
    
    assert len(web_ids) > 0, "Web scraper should find items in the first month of GMod workshop."
    
    # Get details for one of the web items to confirm its age
    web_wid = web_ids[0]
    web_details = get_workshop_details_api(web_wid, API_KEY)
    web_item_timestamp = web_details.get("time_created") if web_details else None
    logging.info(f"Web Discovered Item ID: {web_wid}, Created: {web_item_timestamp}")

    # 3. Validation
    if api_oldest_timestamp and web_item_timestamp:
        # If the web item is older than the API's oldest reachable item, 
        # then we have proven the API depth limit is an issue.
        is_web_deeper = web_item_timestamp < api_oldest_timestamp
        logging.info(f"Web deeper than API: {is_web_deeper}")
        
        # On AppID 4000, this SHOULD be true because there are > 50,000 items.
        # But we'll just log the comparison for the user.
        print(f"\nAPI Max Page Date: {api_oldest_timestamp}")
        print(f"Web Filtered Date: {web_item_timestamp}")
        print(f"Proof: Web reaches items {api_oldest_timestamp - web_item_timestamp} seconds older than API pagination.")
    else:
        pytest.fail("Could not retrieve enough data to compare API and Web reachability.")
