from requests_html import HTMLSession
import requests

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
