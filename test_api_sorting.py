import requests
import json
import os

API_KEY = os.environ.get("STEAM_API_KEY", "")
APPID = 4000 # RimWorld

if not API_KEY:
    print("Warning: STEAM_API_KEY environment variable is missing.")

def test_query(query_type, name):
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    params = {
        "key": API_KEY,
        "input_json": json.dumps({
            "appid": APPID,
            "query_type": query_type,
            "return_details": True,
            "numperpage": 3
        })
    }
    print(f"--- Testing Query Type: {name} ({query_type}) ---")
    resp = requests.get(url, params=params)
    if resp.status_code == 200:
        data = resp.json().get("response", {})
        print(f"Total results: {data.get('total')}")
        for item in data.get("publishedfiledetails", []):
            print(f"ID: {item.get('publishedfileid')} | Created: {item.get('time_created')} | Updated: {item.get('time_updated')} | Title: {item.get('title')}")
    else:
        print(f"Error {resp.status_code}: {resp.text}")

print("Testing pagination limit...")

def test_pagination(page):
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    params = {
        "key": API_KEY,
        "input_json": json.dumps({
            "appid": APPID,
            "query_type": 1,
            "return_details": True,
            "numperpage": 100,
            "page": page
        })
    }
    resp = requests.get(url, params=params)
    if resp.status_code == 200:
        data = resp.json().get("response", {})
        items = data.get("publishedfiledetails", [])
        print(f"Page {page}: Returned {len(items)} items.")
        return len(items) > 0
    else:
        print(f"Page {page} Error {resp.status_code}: {resp.text}")
        return False

# Binary search for max page
low = 1
high = 100000
max_page = 0
while low <= high:
    mid = (low + high) // 2
    if test_pagination(mid):
        max_page = mid
        low = mid + 1
    else:
        high = mid - 1
print(f"Max page found: {max_page}")

