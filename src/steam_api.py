import requests

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
    
    try:
        response = requests.post(url, data=data, timeout=10)
        response.raise_for_status()
        json_data = response.json()
        
        details = json_data.get("response", {}).get("publishedfiledetails", [])
        if not details:
            return None
            
        item = details[0]
        if item.get("result") != 1:
            return None
            
        return item
        
    except requests.exceptions.RequestException:
        return None
