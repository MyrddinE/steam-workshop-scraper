from requests_html import HTMLSession
import requests
import re
import requests.utils
import logging
from src.config import load_config

def scrape_extended_details(item_url: str) -> dict | None:
    """
    Scrapes the extended description and tags from a Steam Workshop page.

    Args:
        item_url: The URL of the workshop item page.

    Returns:
        A dictionary with 'description' and 'tags', or None if scraping fails.
    """
    session = HTMLSession()
    try:
        response = session.get(item_url, timeout=10)
        response.raise_for_status()
        
        description_element = response.html.find('.workshopItemDescription#highlightContent', first=True)
        description = description_element.text if description_element else None
        
        tag_elements = response.html.find('.workshopTags a')
        tags = [tag.text for tag in tag_elements] if tag_elements else []
        
        return {
            "description": description,
            "tags": tags
        }
    except requests.exceptions.RequestException:
        return None

def discover_items_by_date_html(appid: int, start_date: int, end_date: int, page: int = 1, search_text: str = "", required_tags: list[str] = None, excluded_tags: list[str] = None, appids_required_for_use: list[int] = None) -> tuple[list[int], int]:
    """
    Scrapes the Steam Workshop browse page using date filters.
    Returns a tuple of (list_of_ids, total_pages).
    """
    config_path = "config.yaml"
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logging.error(f"Configuration file not found: {config_path}")
        sys.exit(1)

    session_id = config.get("session", {}).get("id", "")

    # workshop_preferences_v2 cookie value is: {"bOptedIn":true}
    cookies = {
        'workshop_preferences_v2': '%7B%22bOptedIn%22%3Atrue%7D',
        'sessionid': session_id
    }
    
    # Use 'mostrecent' to get chronological results, then apply date range
    # Use updated_date_range_filter, as requested by user.
    url_params = [
        f"appid={appid}",
        f"browsesort=mostrecent",
        f"section=readytouseitems",
        f"p={page}",
        f"updated_date_range_filter_start={start_date}",
        f"updated_date_range_filter_end={end_date}"
    ]

    if search_text:
        url_params.append(f"searchtext={requests.utils.quote(search_text)}")

    if required_tags:
        for tag in required_tags:
            url_params.append(f"requiredtags[]={requests.utils.quote(tag)}")

    if excluded_tags:
        for tag in excluded_tags:
            url_params.append(f"excludedtags[]={requests.utils.quote(tag)}")

    if appids_required_for_use:
        for required_appid in appids_required_for_use:
            url_params.append(f"appids_required_for_use[]={required_appid}")

    url = f"https://steamcommunity.com/workshop/browse?" + "&".join(url_params)
    logging.info(url)
    
    session = HTMLSession()
    try:
        response = session.get(url, cookies=cookies, timeout=15)
        response.raise_for_status()
        
        ids = []
        # Extract data from the SSR JSON blob (only publishedfileid for now)
        # Look for a pattern like: "publishedfileid":"123"
        item_id_pattern = re.compile(r'\\\"publishedfileid\\\":\\\"(\d+)\\\"')
        matches = item_id_pattern.findall(response.text)
        
        if not matches:
            # Try a less escaped version
            item_id_pattern = re.compile(r'"publishedfileid":"(\d+)"'
            )
            matches = item_id_pattern.findall(response.text)

        for item_id in matches:
            ids.append(int(item_id))
            
        # Fallback to standard hrefs if JSON extraction fails
        if not ids:
            links = response.html.find('a[href*="sharedfiles/filedetails/?id="]')
            for link in links:
                href = link.attrs.get('href', '')
                match = re.search(r'id=(\d+)', href)
                if match:
                    ids.append(int(match.group(1)))

        total_pages = 1
        page_pattern = re.compile(r'\\\\\\\"total_pages\\\\\\\":(\d+)')
        page_match = page_pattern.search(response.text)
        if page_match:
            total_pages = int(page_match.group(1))

        return list(set(ids)), total_pages
    except (requests.exceptions.RequestException, Exception):
        return [], -1
