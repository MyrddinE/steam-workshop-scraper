import pytest
import responses
import requests
from src.steam_api import get_workshop_details_api, query_workshop_items, query_workshop_files, get_player_summaries

STEAM_API_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
QUERY_API_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"

@responses.activate
def test_query_workshop_items_success():
    """Test successful 200 OK response from Steam Query API using real-world schema."""
    mock_json = {
        "response": {
            "publishedfiledetails": [
                {"publishedfileid": "1001"},
                {"publishedfileid": "1002"}
            ]
        }
    }
    responses.add(
        responses.GET,
        QUERY_API_URL,
        json=mock_json,
        status=200
    )

    ids = query_workshop_items(appid=294100, api_key="TEST_KEY")
    assert ids == [1001, 1002]

@responses.activate
def test_get_workshop_details_api_success():
    """Test successful 200 OK response from Steam API using a real-world snapshot."""
    mock_json = {
        "response": {
            "result": 1,
            "resultcount": 1,
            "publishedfiledetails": [
                {
                    "publishedfileid": "104603291",
                    "result": 1,
                    "creator": "76561197996891752",
                    "creator_app_id": 4000,
                    "consumer_app_id": 4000,
                    "filename": "",
                    "file_size": "41780",
                    "title": "Extended Spawnmenu",
                    "description": "A small script that extends abilities of your spawnmenu...",
                    "time_created": 1351179889,
                    "time_updated": 1706570020,
                    "visibility": 0,
                    "banned": 0,
                    "views": 825998,
                    "subscriptions": 1183622,
                    "favorited": 56931,
                    "tags": [{"tag": "Addon"}, {"tag": "tool"}]
                }
            ]
        }
    }
    responses.add(
        responses.POST,
        STEAM_API_URL,
        json=mock_json,
        status=200
    )

    details = get_workshop_details_api(item_id=104603291, api_key="TEST_KEY")
    assert details is not None
    assert details["title"] == "Extended Spawnmenu"
    assert details["creator"] == "76561197996891752"
    assert details["description"].startswith("A small script")

@responses.activate
@pytest.mark.parametrize("setup_fn,expected_status", [
    pytest.param(lambda url: responses.add(responses.POST, url, status=404), 500, id="http_404"),
    pytest.param(lambda url: responses.add(responses.POST, url, status=429), 500, id="http_429"),
    pytest.param(lambda url: responses.add(responses.POST, url, body=requests.exceptions.Timeout()), 500, id="timeout"),
    pytest.param(lambda url: responses.add(responses.POST, url, json={"response": {"result": 1, "resultcount": 0, "publishedfiledetails": []}}, status=200), 404, id="empty_details"),
    pytest.param(lambda url: responses.add(responses.POST, url, json={"response": {"result": 1, "resultcount": 1, "publishedfiledetails": [{"publishedfileid": "123", "result": 9}]}}, status=200), 404, id="invalid_item"),
])
def test_get_workshop_details_api_errors(setup_fn, expected_status):
    setup_fn(STEAM_API_URL)
    details = get_workshop_details_api(item_id=123, api_key="TEST_KEY")
    assert details["status"] == expected_status
@responses.activate
def test_get_player_summaries_success():
    url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    mock_data = {
        "response": {
            "players": [
                {"steamid": "123", "personaname": "Player One"},
                {"steamid": "456", "personaname": "Player Two"}
            ]
        }
    }
    responses.add(responses.GET, url, json=mock_data, status=200)
    
    from src.steam_api import get_player_summaries
    summaries = get_player_summaries([123, 456], "FAKE_KEY")
    assert len(summaries) == 2
    assert summaries[123]["personaname"] == "Player One"
    assert summaries[456]["personaname"] == "Player Two"

def test_get_player_summaries_empty():
    from src.steam_api import get_player_summaries
    assert get_player_summaries([], "test_key") == {}

@responses.activate
def test_get_player_summaries_exception():
    from src.steam_api import get_player_summaries
    url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    responses.add(responses.GET, url, body=requests.exceptions.ConnectionError("Connection timeout"))
    result = get_player_summaries([123], "test_key")
    assert result == {}

@responses.activate
def test_query_workshop_files_success():
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    mock_data = {
        "response": {
            "total": 2,
            "publishedfiledetails": [
                {"publishedfileid": "111", "time_updated": 1000},
                {"publishedfileid": "222", "time_updated": 2000}
            ],
            "next_cursor": "abc"
        }
    }
    responses.add(responses.GET, url, json=mock_data, status=200)
    result = query_workshop_files(4000, cursor="*", api_key="TEST_KEY")
    assert result["total"] == 2
    assert len(result["items"]) == 2
    assert result["items"][0]["publishedfileid"] == "111"
    assert result["next_cursor"] == "abc"

@responses.activate
def test_query_workshop_files_empty():
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    responses.add(responses.GET, url, json={"response": {}}, status=200)
    result = query_workshop_files(4000, cursor="*", api_key="TEST_KEY")
    assert result["total"] == 0
    assert len(result["items"]) == 0
    assert result["next_cursor"] == ""

@responses.activate
def test_query_workshop_files_error():
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    responses.add(responses.GET, url, status=500)
    result = query_workshop_files(4000, cursor="*", api_key="TEST_KEY")
    assert result["total"] == 0
    assert len(result["items"]) == 0

@responses.activate
def test_query_workshop_files_partial_response():
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    responses.add(responses.GET, url, json={}, status=200)
    result = query_workshop_files(4000, cursor="*", api_key="TEST_KEY")
    assert result["total"] == 0
    assert len(result["items"]) == 0

@responses.activate
def test_query_workshop_items_empty():
    responses.add(responses.GET, QUERY_API_URL, json={"response": {"publishedfiledetails": []}}, status=200)
    ids = query_workshop_items(appid=294100, api_key="TEST_KEY")
    assert ids == []

@responses.activate
def test_query_workshop_items_error():
    responses.add(responses.GET, QUERY_API_URL, status=500)
    ids = query_workshop_items(appid=294100, api_key="TEST_KEY")
    assert ids == []


