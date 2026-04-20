import pytest
import responses
import requests
from src.web_scraper import scrape_extended_details, discover_ids_html

STEAM_WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/"

@responses.activate
def test_scrape_extended_details_success():
    """Test successfully scraping the extended description from HTML."""
    html_content = '''
    <html>
        <body>
            <div class="workshopItemDescription" id="highlightContent">
                [b]This is an extended description.[/b]<br>It has multiple lines.
            </div>
            <div class="rightDetailsBlock">
                 <div class="workshopTags">
                     <a href="?searchtext=&childpublishedfileid=0&section=readytouseitems&requiredtags%5B%5D=1.4">1.4</a>
                 </div>
            </div>
        </body>
    </html>
    '''
    # We mock the GET request
    responses.add(
        responses.GET,
        STEAM_WORKSHOP_URL,
        body=html_content,
        status=200,
        content_type="text/html"
    )

    details = scrape_extended_details("https://steamcommunity.com/sharedfiles/filedetails/?id=2872938263")
    assert details is not None
    assert "This is an extended description" in details["description"]
    assert "1.4" in details["tags"]

@responses.activate
def test_scrape_extended_details_not_found():
    """Test scraping when the description div is missing."""
    html_content = '<html><body>No description here!</body></html>'
    responses.add(
        responses.GET,
        STEAM_WORKSHOP_URL,
        body=html_content,
        status=200,
        content_type="text/html"
    )

    details = scrape_extended_details("https://steamcommunity.com/sharedfiles/filedetails/?id=2872938263")
    assert details is not None
    assert details["description"] is None

@responses.activate
def test_scrape_extended_details_http_error():
    """Test scraping handles HTTP 404 appropriately."""
    responses.add(
        responses.GET,
        STEAM_WORKSHOP_URL,
        status=404
    )

    details = scrape_extended_details("https://steamcommunity.com/sharedfiles/filedetails/?id=123")
    assert details is None

@responses.activate
def test_scrape_extended_details_timeout():
    """Test handling of request timeouts."""
    responses.add(
        responses.GET,
        STEAM_WORKSHOP_URL,
        body=requests.exceptions.Timeout()
    )
    details = scrape_extended_details("https://steamcommunity.com/sharedfiles/filedetails/?id=123")
    assert details is None

@responses.activate
def test_discover_ids_html_success():
    """Test successfully discovering IDs from Workshop browse HTML with paging."""
    html_content = '''
    <html>
        <body>
            <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=5001&searchtext=">Mod 1</a>
            <a href="https://steamcommunity.com/sharedfiles/filedetails/?id=5002">Mod 2</a>
        </body>
    </html>
    '''
    # Verify the URL includes browsemethod=mostrecent and p=2
    responses.add(
        responses.GET,
        "https://steamcommunity.com/workshop/browse/?appid=294100&browsemethod=mostrecent&section=readytouseitems&p=2",
        body=html_content,
        status=200,
        content_type="text/html"
    )

    ids = discover_ids_html(appid=294100, page=2)
    assert len(ids) == 2
    assert 5001 in ids
    assert 5002 in ids

@responses.activate
def test_discover_ids_html_exception():
    """Test handling of exceptions during discovery."""
    from unittest.mock import patch
    import requests
    with patch('src.web_scraper.HTMLSession') as mock_session:
        mock_instance = mock_session.return_value
        mock_instance.get.side_effect = requests.exceptions.RequestException("Timeout")
        
        result = discover_ids_html(4000)
        assert result == []
