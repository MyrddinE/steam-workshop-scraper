import pytest
import responses
import requests
from src.steam_api import get_workshop_details_api, query_workshop_items

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
def test_get_workshop_details_api_404():
    """Test handling of 404 Not Found."""
    responses.add(
        responses.POST,
        STEAM_API_URL,
        status=404
    )
    details = get_workshop_details_api(item_id=123, api_key="TEST_KEY")
    assert details is None

@responses.activate
def test_get_workshop_details_api_429():
    """Test handling of 429 Too Many Requests."""
    responses.add(
        responses.POST,
        STEAM_API_URL,
        status=429
    )
    details = get_workshop_details_api(item_id=123, api_key="TEST_KEY")
    assert details is None

@responses.activate
def test_get_workshop_details_api_timeout():
    """Test handling of request timeouts."""
    responses.add(
        responses.POST,
        STEAM_API_URL,
        body=requests.exceptions.Timeout()
    )
    details = get_workshop_details_api(item_id=123, api_key="TEST_KEY")
    assert details is None

@responses.activate
def test_get_workshop_details_api_empty_details():
    """Test when Steam API returns an empty publishedfiledetails array."""
    mock_json = {
        "response": {
            "result": 1,
            "resultcount": 0,
            "publishedfiledetails": []
        }
    }
    responses.add(
        responses.POST,
        STEAM_API_URL,
        json=mock_json,
        status=200
    )
    details = get_workshop_details_api(item_id=123, api_key="TEST_KEY")
    assert details is None

@responses.activate
def test_get_workshop_details_api_invalid_item():
    """Test when Steam API returns 200 but item doesn't exist (result != 1)."""
    mock_json = {
        "response": {
            "result": 1,
            "resultcount": 1,
            "publishedfiledetails": [
                {"publishedfileid": "123", "result": 9} # 9 usually means file not found
            ]
        }
    }
    responses.add(
        responses.POST,
        STEAM_API_URL,
        json=mock_json,
        status=200
    )
    details = get_workshop_details_api(item_id=123, api_key="TEST_KEY")
    assert details is None

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
