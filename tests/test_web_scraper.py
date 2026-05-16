import pytest
import responses
import requests
from src.web_scraper import scrape_extended_details, discover_items_by_date_html
from unittest.mock import patch, MagicMock

STEAM_WORKSHOP_URL = "https://steamcommunity.com/sharedfiles/filedetails/"

@responses.activate
def test_discover_items_by_date_html_url_construction():
    """Test URL construction for discover_items_by_date_html with various filters."""
    appid = 294100
    start_date = 1609459200 # Jan 1, 2021
    end_date = 1612137600   # Feb 1, 2021
    page = 1
    search_text = "Test Mod"
    required_tags = ["Core", "UI"]
    excluded_tags = ["Broken", "Old"]

    expected_url_parts = [
        f"appid={appid}",
        "browsesort=mostrecent",
        "section=readytouseitems",
        f"p={page}",
        f"updated_date_range_filter_start={start_date}",
        f"updated_date_range_filter_end={end_date}",
        f"searchtext={requests.utils.quote(search_text)}",
        f"requiredtags[]={requests.utils.quote(required_tags[0])}",
        f"requiredtags[]={requests.utils.quote(required_tags[1])}",
        f"excludedtags[]={requests.utils.quote(excluded_tags[0])}",
        f"excludedtags[]={requests.utils.quote(excluded_tags[1])}",
    ]
    expected_url = "https://steamcommunity.com/workshop/browse?" + "&".join(sorted(expected_url_parts))

    mock_html_content = '''
    <html><body>
        <script>window.SSR = {renderContext: {rsc: "{\\\"publishedfileid\\\":\\\"123\\\",\\\"title\\\":\\\"Test Mod Title\\\"}"}}</script>
    </body></html>
    '''
    # Mock the actual request to verify the URL
    with patch('src.web_scraper.HTMLSession') as mock_session_class, \
         patch('src.web_scraper.load_config') as mock_load_config:
        mock_load_config.return_value = {"session": {"id": "test_session"}}
        mock_session = mock_session_class.return_value
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.text = mock_html_content
        mock_session.get.return_value = mock_response

        _ = discover_items_by_date_html(appid, start_date, end_date, page, search_text, required_tags, excluded_tags)
        
        # Check if the URL passed to get() matches our expectation
        call_args, _ = mock_session.get.call_args
        actual_url = call_args[0]
        
        # Sort parts for reliable comparison
        actual_url_parts = actual_url.split('?')[1].split('&')
        assert sorted(actual_url_parts) == sorted(expected_url_parts)
        assert mock_session.get.call_count == 1

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
@pytest.mark.parametrize("setup_fn", [
    pytest.param(lambda url: responses.add(responses.GET, url, status=404), id="http_error"),
    pytest.param(lambda url: responses.add(responses.GET, url, body=requests.exceptions.Timeout()), id="timeout"),
])
def test_scrape_extended_details_returns_none_on_failure(setup_fn):
    setup_fn(STEAM_WORKSHOP_URL)
    details = scrape_extended_details("https://steamcommunity.com/sharedfiles/filedetails/?id=123")
    assert details is None

@responses.activate
def test_discover_items_by_date_html_success():
    """
    Test successfully discovering IDs from Workshop browse HTML with paging (SSR).
    Titles are no longer extracted directly by this function.
    """
    appid = 294100
    start_date = 1609459200 # Jan 1, 2021
    end_date = 1612137600   # Feb 1, 2021
    page = 2

    mock_html_content = '''
    <html><body>
        <script>window.SSR = {renderContext: {rsc: "{\\\"publishedfileid\\\":\\\"5001\\\"},{\\\"publishedfileid\\\":\\\"5002\\\"}"}}</script>
    </body></html>
    '''
    # Mock the actual request to return our SSR content
    responses.add(
        responses.GET,
        "https://steamcommunity.com/workshop/browse?appid=294100&browsesort=mostrecent&section=readytouseitems&p=2&updated_date_range_filter_start=1609459200&updated_date_range_filter_end=1612137600",
        body=mock_html_content,
        status=200,
        content_type="text/html"
    )

    with patch('src.web_scraper.load_config') as mock_load_config:
        mock_load_config.return_value = {"session": {"id": "test_session"}}
        ids, total_pages = discover_items_by_date_html(appid, start_date, end_date, page)
    assert len(ids) == 2
    assert 5001 in ids
    assert 5002 in ids


@responses.activate
def test_discover_items_by_date_html_exception():
    """Test handling of exceptions during discover_items_by_date_html."""
    with patch('src.web_scraper.HTMLSession') as mock_session, \
         patch('src.web_scraper.load_config') as mock_load_config:
        mock_load_config.return_value = {"session": {"id": "test_session"}}
        mock_instance = mock_session.return_value
        mock_instance.get.side_effect = requests.exceptions.RequestException("Timeout")
        
        result, pages = discover_items_by_date_html(4000, 0, 0)
        assert result == []


def test_scrape_missing_dom_returns_none():
    from src.web_scraper import scrape_extended_details
    from unittest.mock import patch
    mock_html = '<html><body></body></html>'
    with patch('requests.get') as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = mock_html
        mock_get.return_value.html.find.return_value = None
        assert scrape_extended_details(123) is None


def test_image_worker_runs_without_crash(tmp_path):
    """Smoke test: ImageScraperThread initializes and exits immediately."""
    import os
    from src.database import initialize_database
    from src.image_worker import ImageScraperThread

    db_path = str(tmp_path / "img.db")
    initialize_database(db_path)
    os.makedirs("images", exist_ok=True)

    worker = ImageScraperThread(db_path, ".pauselock")
    worker.running = False
    worker.run()


def test_get_db_stats_empty_defaults(db_path):
    from src.database import get_db_stats
    stats = get_db_stats(db_path)
    assert stats["status_counts"] == []
    for key, val in stats["translation_status"].items():
        assert val == 0


def test_build_filter_clause_unknown_operator():
    from src.database import _build_filter_clause
    clause, params = _build_filter_clause("title", "bogus_op", "val")
    assert clause == ""
    assert params == []


def test_evaluate_single_filter_unknown_operator():
    from src.database import _evaluate_single_filter
    assert _evaluate_single_filter({"t": "x"}, "t", "bogus", "x") is True


def test_wilson_lower_edge_cases():
    from src.daemon import wilson_lower
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(0, 10) == 0.0
    assert 0.0 <= wilson_lower(5, 10) <= 1.0
    assert 0.0 <= wilson_lower(100, 100) <= 1.0


