import requests
import time
import threading
import logging

from src import capture

# The shared request schedule. ``_next_slot`` is the earliest monotonic moment at
# which the next caller may start a request, so every caller — the batched
# details fetch, the creator summaries, discovery — draws on one budget rather
# than each keeping its own. That is what makes it safe to run discovery on its
# own thread: concurrency redistributes the budget, it does not enlarge it.
_lock = threading.Lock()
_next_slot = 0.0
_api_delay = 1.5

# Ceiling on ids carried by one bulk request. The Steamworks reference documents
# ISteamUser/GetPlayerSummaries/v2's `steamids` as a "Comma-delimited list of
# SteamIDs (max: 100)"; GetPublishedFileDetails/v1 lists `itemcount` and
# `publishedfileids[0..]` with no published cap, so the documented
# GetPlayerSummaries figure is used as the conservative ceiling for both rather
# than a literal buried at each call site. The live probe that motivated this
# batching sent 50 ids in one GetPublishedFileDetails call and got all 50 back
# in 0.441 s.
#   https://partner.steamgames.com/doc/webapi/ISteamUser#GetPlayerSummaries
#   https://partner.steamgames.com/doc/webapi/ISteamRemoteStorage#GetPublishedFileDetails
STEAM_API_MAX_IDS_PER_REQUEST = 100


def set_api_delay(seconds: float):
    global _api_delay
    with _lock:
        _api_delay = seconds


def _rate_limit(keep_running=None) -> bool:
    """Wait for this caller's own slot in the shared request schedule.

    The slot is reserved under the lock and slept for outside it. Holding the
    lock across the wait would serialise the *requests* as well as the gaps, and
    the gap is the only thing being rationed; reserving and releasing lets
    several callers be asleep at once, each waking for its own turn.

    ``time.monotonic`` rather than the wall clock, because a clock correction
    must not be able to break the schedule.

    ``keep_running`` makes the wait abandonable: the wait is served in one-second
    steps and the call returns ``False`` if the predicate goes false first. A
    discovery thread needs that, because a backoff can leave a long wait ahead of
    it and a shutdown must not be held for it. Without a predicate the whole wait
    is served, which is what every existing caller wants.

    Returns whether the caller may proceed. Callers that pass no predicate can
    ignore it: ``False`` is then impossible.
    """
    global _next_slot
    with _lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + _api_delay
        delay = slot - now

    if delay <= 0:
        return True
    if keep_running is None:
        time.sleep(delay)
        return True

    deadline = time.monotonic() + delay
    while keep_running():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(1.0, remaining))
    return False


def get_workshop_details_batch(item_ids: list[int], api_key: str) -> dict[int, dict] | None:
    """Fetch metadata for many workshop items in a single POST.

    Returns a mapping keyed by the requested id. Every requested id is present in
    the result: one the API omitted is reported as a synthetic ``500``, a
    temporary failure, so the row is requeued rather than killed. An omission is
    not evidence of deletion -- the endpoint answers every requested id, with
    ``result != 1`` marking the ones that are gone -- so it is read as a response
    that did not arrive whole. See the note at the fill-in below for why the
    direction matters.

    Results are keyed by the ``publishedfileid`` each response entry carries, not
    by its position, so a response that reorders, duplicates or adds ids can never
    mis-assign a result to the wrong item. Duplicate and unrequested ids are
    logged and ignored.

    Returns ``None`` -- deliberately not an empty mapping -- when the *request*
    failed: a transport error, a timeout, an HTTP error status, or a body that is
    not JSON. That is the signal the daemon backs off on. A request that returns
    and parses is a success even if every item in it is not-found.
    """
    requested = list(dict.fromkeys(int(i) for i in item_ids))
    if not requested:
        return {}

    url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    data = {"itemcount": len(requested), "key": api_key}
    for index, item_id in enumerate(requested):
        data[f"publishedfileids[{index}]"] = item_id

    _rate_limit()
    try:
        response = requests.post(url, data=data, timeout=10)
        response.raise_for_status()
        try:
            json_data = response.json()
        except ValueError:
            # Not JSON: an HTML error page, a proxy notice, a truncated body.
            # Capture the evidence once for the whole request, then report a
            # request-level failure so the caller backs off and retries the batch.
            capture.record_failure(
                kind="api_unparsed_body",
                stage="api_fetch",
                workshop_id=requested[0] if len(requested) == 1 else None,
                http_status=response.status_code,
                final_url=url,
                body=response.text,
                content_type=response.headers.get("Content-Type"),
                context={"item_ids": requested, "itemcount": len(requested)},
            )
            return None

        details = (json_data.get("response", {}).get("publishedfiledetails", [])
                   if isinstance(json_data, dict) else [])

        requested_set = set(requested)
        by_id: dict[int, dict] = {}
        for detail in details:
            if not isinstance(detail, dict):
                continue
            try:
                detail_id = int(detail.get("publishedfileid"))
            except (TypeError, ValueError):
                # No usable id, so it cannot be matched to an item and must not
                # be guessed at by position.
                logging.warning("Steam API batch entry carried no usable publishedfileid; ignoring it.")
                continue
            if detail_id not in requested_set:
                logging.warning("Steam API batch returned unrequested publishedfileid %s; ignoring it.", detail_id)
                continue
            if detail_id in by_id:
                logging.warning("Steam API batch returned duplicate publishedfileid %s; keeping the first entry.", detail_id)
                continue
            if detail.get("result") != 1:
                # Deleted, private or otherwise unavailable; same permanent
                # not-found the single-item path reports for result != 1.
                by_id[detail_id] = {"status": 404, "publishedfileid": detail_id}
                continue
            # Ensure the item always has a status, default to 200 if not provided by API
            if "status" not in detail:
                detail["status"] = 200
            by_id[detail_id] = detail

        for item_id in requested:
            # An omitted id is NOT the same as "not found". The bulk endpoint
            # answers one entry per requested id -- a live probe of 50 ids (10 of
            # them nonexistent) returned 50 entries, the bad ones carrying
            # result=9 -- so an omission means the response did not arrive whole,
            # not that Steam has deleted the item. Reporting it as 404 would mark
            # every omitted item dead, permanently and irreversibly: a truncated
            # response is one event, and `status = -1` is never revived by
            # anything. It is reported as a temporary failure instead, which
            # requeues the item one priority lower -- it survives, and sinks.
            by_id.setdefault(item_id, {"status": 500, "publishedfileid": item_id})
        return by_id

    except requests.exceptions.RequestException:
        # Transport failure, timeout, or an HTTP error status (429/5xx included):
        # the request itself failed.
        return None


def get_workshop_details(item_id: int, api_key: str) -> dict | None:
    """
    Fetches metadata for a single Steam Workshop item using the Steam Web API.

    This is the one-id spelling of :func:`get_workshop_details_batch`, kept for
    its existing callers and tests. Both paths share request shaping, failure
    capture and id matching.

    Args:
        item_id: The ID of the workshop item.
        api_key: Your Steam Web API key.

    Returns:
        A dictionary containing the item details, or None if the request fails.
    """
    results = get_workshop_details_batch([item_id], api_key)
    if results is None:
        # Preserve the single-item contract: a request failure reads as 500.
        return {"status": 500, "publishedfileid": item_id}
    return results.get(int(item_id), {"status": 404, "publishedfileid": item_id})

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
        for player in players:
            sid = int(player["steamid"])
            result[sid] = player
        return result
        
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return {}

def query_workshop_newest_page(appid: int, cursor: str, api_key: str,
                               keep_running=None) -> dict:
    """
    Queries the Steam Workshop using IPublishedFileService/QueryFiles,
    sorted by publication date (newest first). Uses cursor-based pagination
    for unlimited depth (pass '*' for the first page).
    Returns a dict with 'total', 'items', and 'next_cursor'.

    ``keep_running`` is forwarded to the shared rate limiter so a caller on its
    own thread can abandon a long wait when the daemon is stopping. When it goes
    false the request is not made and ``{"abandoned": True}`` comes back, which
    the caller must not mistake for a page of results.
    """
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    params = {
        "key": api_key,
        "query_type": 1,
        "cursor": cursor,
        "numperpage": 100,
        "appid": appid,
        "return_short_description": True,
        "return_tags": True,
        "return_previews": False,
        "return_children": False,
        "return_for_sale_data": False,
        "return_metadata": False,
    }
    
    if not _rate_limit(keep_running):
        return {"abandoned": True, "total": 0, "items": [], "next_cursor": ""}
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json().get("response", {})
        
        return {
            "total": data.get("total", 0),
            "items": data.get("publishedfiledetails", []),
            "next_cursor": data.get("next_cursor", ""),
        }
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            logging.error(f"Steam API returned 403 Forbidden for QueryFiles. "
                          "API key may be missing or invalid.")
        return {"total": 0, "items": [], "next_cursor": "", "failed": True}
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return {"total": 0, "items": [], "next_cursor": "", "failed": True}


def query_workshop_updated_page(appid: int, cursor: str, api_key: str, numperpage: int = 100,
                                keep_running=None) -> dict:
    """
    Queries Steam Workshop via IPublishedFileService/QueryFiles with
    query_type=21 (rank by last updated), cursor-based pagination.
    Pass '*' for the first page.
    Returns a dict with 'total', 'items' (list of publishedfileid dicts),
    and 'next_cursor'.
    """
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    params = {
        "key": api_key,
        "query_type": 21,  # rank by last updated
        "cursor": cursor,
        "numperpage": numperpage,
        "appid": appid,
        "return_short_description": True,
        "return_tags": True,
        "return_previews": False,
        "return_children": False,
        "return_for_sale_data": False,
        "return_metadata": False,
    }

    if not _rate_limit(keep_running):
        return {"abandoned": True, "total": 0, "items": [], "next_cursor": "", "failed": False}
    try:
        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json().get("response", {})
        items = data.get("publishedfiledetails", [])
        return {
            "total": data.get("total", 0),
            "items": items,
            "next_cursor": data.get("next_cursor", ""),  # page mode may still return a cursor
            "failed": False,
        }
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            logging.error("Steam API returned 403 Forbidden for page-mode QueryFiles.")
        return {"total": 0, "items": [], "next_cursor": "", "failed": True}
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return {"total": 0, "items": [], "next_cursor": "", "failed": True}
