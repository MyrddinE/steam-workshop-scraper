import requests
import time
import logging

_last_api_call = 0.0
_API_DELAY = 1.5


def set_api_delay(seconds: float):
    global _API_DELAY
    _API_DELAY = seconds


def _rate_limit():
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if 0 < elapsed < _API_DELAY:
        time.sleep(_API_DELAY - elapsed)
    _last_api_call = time.time()


def get_workshop_details_api(item_id: int, api_key: str) -> dict | None:
    """
    Fetches metadata for a Steam Workshop item using the Steam Web API.

    Args:
        item_id: The ID of the workshop item.
        api_key: Your Steam Web API key.

    Returns:
        A dictionary containing the item details, or None if the request fails.
    """
    url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    data = {
        "itemcount": 1,
        "publishedfileids[0]": item_id,
        "key": api_key
    }
    
    _rate_limit()
    try:
        response = requests.post(url, data=data, timeout=10)
        response.raise_for_status()
        json_data = response.json()
        
        details = json_data.get("response", {}).get("publishedfiledetails", [])
        if not details:
            # If no details, it means the item was not found or is invalid.
            return {"status": 404, "publishedfileid": item_id}
            
        item = details[0]
        if item.get("result") != 1:
            return {"status": 404, "publishedfileid": item_id}
            
        # Ensure the item always has a status, default to 200 if not provided by API
        if "status" not in item:
            item["status"] = 200

        return item
        
    except requests.exceptions.RequestException:
        # Return a 500 status on API request failure
        return {"status": 500, "publishedfileid": item_id}

def query_workshop_items(appid: int, api_key: str, count: int = 50, page: int = 1) -> list[int]:
    """
    Queries the Steam API for a list of workshop items for a specific app.
    Useful for seeding the database with IDs.
    """
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    params = {
        "key": api_key,
        "query_type": 0, # RankByVote (popular)
        "page": page,
        "numperpage": count,
        "creator_appid": appid,
        "appid": appid,
        "return_vote_data": 1
    }
    
    _rate_limit()
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        json_data = response.json()
        
        details = json_data.get("response", {}).get("publishedfiledetails", [])
        return [int(item["publishedfileid"]) for item in details if "publishedfileid" in item]
        
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return []

def get_player_summaries(steamids: list[int], api_key: str) -> dict[int, dict]:
    """
    Fetches persona names for a list of SteamIDs.
    Returns a mapping of SteamID -> {personaname: str, ...}
    """
    if not steamids:
        return {}
        
    url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
    ids_str = ",".join(str(sid) for sid in steamids)
    params = {
        "key": api_key,
        "steamids": ids_str
    }
    
    _rate_limit()
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        json_data = response.json()
        
        players = json_data.get("response", {}).get("players", [])
        result = {}
        for p in players:
            sid = int(p["steamid"])
            result[sid] = p
        return result
        
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return {}

def query_workshop_files(appid: int, page: int, api_key: str, numperpage: int = 100) -> dict:
    """
    Queries the Steam Workshop using IPublishedFileService/QueryFiles,
    sorted by publication date (newest first).
    Returns a dict with 'total' and 'items'.
    """
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    params = {
        "key": api_key,
        "query_type": 1,
        "page": page,
        "numperpage": numperpage,
        "appid": appid,
        "return_short_description": True,
        "return_tags": True,
        "return_previews": False,
        "return_children": False,
        "return_for_sale_data": False,
        "return_metadata": False,
    }
    
    _rate_limit()
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json().get("response", {})
        
        return {
            "total": data.get("total", 0),
            "items": data.get("publishedfiledetails", [])
        }
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return {"total": 0, "items": []}
