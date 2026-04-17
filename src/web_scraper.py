from requests_html import HTMLSession
import requests
import re

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

def discover_ids_html(appid: int, page: int = 1) -> list[int]:
    """
    Scrapes the Steam Workshop browse page to find item IDs.
    Acts as a fallback when the Steam API is unavailable or no key is provided.
    """
    url = f"https://steamcommunity.com/workshop/browse/?appid={appid}&browsemethod=mostrecent&section=readytouseitems&p={page}"
    session = HTMLSession()
    try:
        response = session.get(url, timeout=10)
        response.raise_for_status()
        
        # Steam IDs are in 'id' parameters of links (e.g., sharedfiles/filedetails/?id=...)
        # We look for all sharedfiles links
        links = response.html.find('a[href*="sharedfiles/filedetails/?id="]')
        ids = []
        for link in links:
            href = link.attrs.get('href', '')
            # Extract digits after 'id='
            match = re.search(r'id=(\d+)', href)
            if match:
                ids.append(int(match.group(1)))
        
        # Return unique IDs only
        return list(set(ids))
    except (requests.exceptions.RequestException, Exception):
        return []
