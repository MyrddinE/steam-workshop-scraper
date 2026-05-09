from requests_html import HTMLSession
import requests
import re
import sys
import time
import requests.utils
import logging
from src.config import load_config

_last_web_call = 0.0
_WEB_DELAY = 5.0


def set_web_delay(seconds: float):
    global _WEB_DELAY
    _WEB_DELAY = seconds


def _rate_limit():
    global _last_web_call
    elapsed = time.time() - _last_web_call
    if 0 < elapsed < _WEB_DELAY:
        time.sleep(_WEB_DELAY - elapsed)
    _last_web_call = time.time()


def _build_workshop_cookies(config: dict) -> dict:
    """Builds cookies dict for Steam Workshop requests."""
    session_id = config.get("session", {}).get("id", "")
    return {
        'workshop_preferences_v2': '%7B%22bOptedIn%22%3Atrue%7D',
        'sessionid': session_id
    }

def _build_browse_url_params(appid: int, start_date: int, end_date: int, page: int,
                              search_text: str = "", required_tags: list[str] = None,
                              excluded_tags: list[str] = None,
                              appids_required_for_use: list[int] = None) -> list[str]:
    """Builds query parameters for the Steam Workshop browse page."""
    params = [
        f"appid={appid}", "browsesort=mostrecent", "section=readytouseitems",
        f"p={page}",
        f"updated_date_range_filter_start={start_date}",
        f"updated_date_range_filter_end={end_date}"
    ]
    if search_text:
        params.append(f"searchtext={requests.utils.quote(search_text)}")
    if required_tags:
        for tag in required_tags:
            params.append(f"requiredtags[]={requests.utils.quote(tag)}")
    if excluded_tags:
        for tag in excluded_tags:
            params.append(f"excludedtags[]={requests.utils.quote(tag)}")
    if appids_required_for_use:
        for rid in appids_required_for_use:
            params.append(f"appids_required_for_use[]={rid}")
    return params

def _extract_item_ids_from_page(response) -> list[int]:
    """Extracts workshop item IDs from a Steam Workshop browse page response.
    Tries SSR JSON blob first, falls back to HTML hrefs."""
    ids = []
    item_id_pattern = re.compile(r'\\\"publishedfileid\\\":\\\"(\d+)\\\"')
    matches = item_id_pattern.findall(response.text)
    if not matches:
        item_id_pattern = re.compile(r'"publishedfileid":"(\d+)"')
        matches = item_id_pattern.findall(response.text)
    for item_id in matches:
        ids.append(int(item_id))
    if not ids:
        links = response.html.find('a[href*="sharedfiles/filedetails/?id="]')
        for link in links:
            href = link.attrs.get('href', '')
            match = re.search(r'id=(\d+)', href)
            if match:
                ids.append(int(match.group(1)))
    return list(set(ids))

def _extract_total_pages(response) -> int:
    """Extracts total_pages from a Steam Workshop browse page SSR JSON."""
    page_pattern = re.compile(r'\\\\\\\"total_pages\\\\\\\":(\d+)')
    page_match = page_pattern.search(response.text)
    return int(page_match.group(1)) if page_match else 1

def scrape_extended_details(item_url: str) -> dict | None:
    """
    Scrapes the extended description and tags from a Steam Workshop page.
    """
    session = HTMLSession()
    _rate_limit()
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
    try:
        config = load_config("config.yaml")
    except FileNotFoundError:
        logging.error("Configuration file not found: config.yaml")
        sys.exit(1)

    cookies = _build_workshop_cookies(config)
    url_params = _build_browse_url_params(appid, start_date, end_date, page,
        search_text=search_text, required_tags=required_tags,
        excluded_tags=excluded_tags, appids_required_for_use=appids_required_for_use)
    url = "https://steamcommunity.com/workshop/browse?" + "&".join(url_params)
    logging.info(url)

    session = HTMLSession()
    try:
        response = session.get(url, cookies=cookies, timeout=15)
        response.raise_for_status()
        ids = _extract_item_ids_from_page(response)
        total_pages = _extract_total_pages(response)
        return ids, total_pages
    except (requests.exceptions.RequestException, Exception):
        return [], -1
