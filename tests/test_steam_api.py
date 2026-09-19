import os

import pytest
import responses
import requests
from src.steam_api import (
    get_workshop_details,
    get_workshop_details_batch,
    query_workshop_items,
    query_workshop_newest_page,
    get_player_summaries,
    STEAM_API_MAX_IDS_PER_REQUEST,
)

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

    details = get_workshop_details(item_id=104603291, api_key="TEST_KEY")
    assert details is not None
    assert details["title"] == "Extended Spawnmenu"
    assert details["creator"] == "76561197996891752"
    assert details["description"].startswith("A small script")

@responses.activate
@pytest.mark.parametrize("setup_fn,expected_status", [
    pytest.param(lambda url: responses.add(responses.POST, url, status=404), 500, id="http_404"),
    pytest.param(lambda url: responses.add(responses.POST, url, status=429), 500, id="http_429"),
    pytest.param(lambda url: responses.add(responses.POST, url, body=requests.exceptions.Timeout()), 500, id="timeout"),
    # An empty response is not a deletion: the endpoint answers every requested
    # id (bad ones with result != 1), so no entry at all means the response did
    # not arrive whole. 500 requeues the item; 404 would kill it for good.
    pytest.param(lambda url: responses.add(responses.POST, url, json={"response": {"result": 1, "resultcount": 0, "publishedfiledetails": []}}, status=200), 500, id="empty_details"),
    pytest.param(lambda url: responses.add(responses.POST, url, json={"response": {"result": 1, "resultcount": 1, "publishedfiledetails": [{"publishedfileid": "123", "result": 9}]}}, status=200), 404, id="invalid_item"),
])
def test_get_workshop_details_api_errors(setup_fn, expected_status):
    setup_fn(STEAM_API_URL)
    details = get_workshop_details(item_id=123, api_key="TEST_KEY")
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
    result = query_workshop_newest_page(4000, cursor="*", api_key="TEST_KEY")
    assert result["total"] == 2
    assert len(result["items"]) == 2
    assert result["items"][0]["publishedfileid"] == "111"
    assert result["next_cursor"] == "abc"

@responses.activate
def test_query_workshop_files_empty():
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    responses.add(responses.GET, url, json={"response": {}}, status=200)
    result = query_workshop_newest_page(4000, cursor="*", api_key="TEST_KEY")
    assert result["total"] == 0
    assert len(result["items"]) == 0
    assert result["next_cursor"] == ""

@responses.activate
def test_query_workshop_files_error():
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    responses.add(responses.GET, url, status=500)
    result = query_workshop_newest_page(4000, cursor="*", api_key="TEST_KEY")
    assert result["total"] == 0
    assert len(result["items"]) == 0

@responses.activate
def test_query_workshop_files_partial_response():
    url = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
    responses.add(responses.GET, url, json={}, status=200)
    result = query_workshop_newest_page(4000, cursor="*", api_key="TEST_KEY")
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




# ── Unparsed API bodies ──────────────────────────────────────────────────────
# A non-JSON body (an HTML error page, a proxy notice) carries nothing usable,
# but requests.exceptions.JSONDecodeError subclasses RequestException, so the
# existing handler already collapsed it to a generic 500 and threw the body
# away. The body is now captured first; the returned value is unchanged.

@responses.activate
def test_non_json_body_is_captured_and_still_reported_as_500(tmp_path):
    import json
    from src import capture

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        responses.add(responses.POST, STEAM_API_URL,
                      body="<html><body>proxy error</body></html>",
                      status=200, content_type="text/html")
        result = get_workshop_details(4242, "TEST_KEY")
    finally:
        capture.configure(None)

    assert result == {"status": 500, "publishedfileid": 4242}

    group = capture.group_id("api_unparsed_body", None, "api_fetch")
    group_dir = outbox / "failures" / group
    records = [p for p in group_dir.glob("*.json") if p.name != "_group.json"]
    assert len(records) == 1, "the unparsed body was not captured"

    record = json.loads(records[0].read_text())
    assert record["workshop_id"] == 4242
    assert record["stage"] == "api_fetch"
    assert record["http_status"] == 200
    assert record["content_type"] == "text/html"
    body_file = group_dir / os.path.basename(record["body_file"])
    assert body_file.read_bytes().startswith(b"<html>")


@responses.activate
def test_non_json_body_with_capture_off_writes_nothing(tmp_path):
    from src import capture

    capture.configure(None)
    responses.add(responses.POST, STEAM_API_URL,
                  body="<html>proxy</html>", status=200, content_type="text/html")
    result = get_workshop_details(1, "TEST_KEY")

    assert result == {"status": 500, "publishedfileid": 1}
    assert not (tmp_path / "outbox").exists()


# ── Batched details fetch ────────────────────────────────────────────────────
# One POST carries many ids. The response is matched to the request by each
# entry's publishedfileid, never by position, so reordering, duplicates or extra
# ids cannot mis-assign a result.

def _detail(item_id, **overrides):
    detail = {"publishedfileid": str(item_id), "result": 1, "title": f"Mod {item_id}"}
    detail.update(overrides)
    return detail


@responses.activate
def test_batch_call_builds_one_request_body_for_n_ids():
    ids = [101, 202, 303]
    responses.add(responses.POST, STEAM_API_URL, json={
        "response": {"result": 1, "resultcount": 3,
                     "publishedfiledetails": [_detail(i) for i in ids]}
    }, status=200)

    result = get_workshop_details_batch(ids, "TEST_KEY")

    assert len(responses.calls) == 1, "the batch must be a single request"
    body = responses.calls[0].request.body
    if isinstance(body, bytes):
        body = body.decode()
    from urllib.parse import parse_qs
    fields = parse_qs(body)
    assert fields["itemcount"] == ["3"]
    assert fields["publishedfileids[0]"] == ["101"]
    assert fields["publishedfileids[1]"] == ["202"]
    assert fields["publishedfileids[2]"] == ["303"]
    assert set(result) == set(ids)


@responses.activate
def test_batch_results_map_back_to_the_right_items_regardless_of_order():
    # The response is deliberately shuffled and one id is absent.
    responses.add(responses.POST, STEAM_API_URL, json={
        "response": {"result": 1, "resultcount": 2,
                     "publishedfiledetails": [_detail(303), _detail(101)]}
    }, status=200)

    result = get_workshop_details_batch([101, 202, 303], "TEST_KEY")

    assert result[101]["title"] == "Mod 101"
    assert result[303]["title"] == "Mod 303"
    # 202 was omitted from the response. That is reported as a TEMPORARY failure,
    # not a permanent one: the endpoint answers every requested id (bad ones with
    # result != 1), so an omission means the response did not arrive whole, and
    # treating it as not-found would kill the item irreversibly on the strength of
    # one truncated response. Requeued-and-sunk is the safe direction.
    assert result[202] == {"status": 500, "publishedfileid": 202}


@responses.activate
def test_batch_ignores_extra_and_duplicate_ids():
    responses.add(responses.POST, STEAM_API_URL, json={
        "response": {"result": 1, "resultcount": 4, "publishedfiledetails": [
            _detail(1, title="first"),
            _detail(1, title="duplicate-should-not-win"),
            _detail(999, title="unrequested"),
            {"publishedfileid": "not-a-number", "result": 1, "title": "malformed"},
        ]}
    }, status=200)

    result = get_workshop_details_batch([1, 2], "TEST_KEY")

    assert set(result) == {1, 2}
    assert result[1]["title"] == "first", "a duplicate must not overwrite the first entry"
    # 2 was omitted entirely: a temporary failure (see the batch docstring), not
    # the permanent not-found that result != 1 reports.
    assert result[2] == {"status": 500, "publishedfileid": 2}


@responses.activate
def test_batch_result_code_9_is_a_permanent_not_found():
    responses.add(responses.POST, STEAM_API_URL, json={
        "response": {"result": 1, "resultcount": 2, "publishedfiledetails": [
            _detail(1),
            {"publishedfileid": "2", "result": 9},
        ]}
    }, status=200)

    result = get_workshop_details_batch([1, 2], "TEST_KEY")

    assert result[1]["title"] == "Mod 1"
    assert result[2] == {"status": 404, "publishedfileid": 2}


@responses.activate
@pytest.mark.parametrize("setup", [
    pytest.param(lambda: responses.add(responses.POST, STEAM_API_URL, status=500), id="http_500"),
    pytest.param(lambda: responses.add(responses.POST, STEAM_API_URL, status=429), id="http_429"),
    pytest.param(lambda: responses.add(responses.POST, STEAM_API_URL,
                                       body=requests.exceptions.Timeout()), id="timeout"),
    pytest.param(lambda: responses.add(responses.POST, STEAM_API_URL,
                                       body="<html>proxy</html>", status=200,
                                       content_type="text/html"), id="unparseable"),
])
def test_batch_request_failure_returns_none_not_an_empty_mapping(setup):
    setup()
    assert get_workshop_details_batch([1, 2, 3], "TEST_KEY") is None


@responses.activate
def test_batch_of_zero_ids_makes_no_request():
    assert get_workshop_details_batch([], "TEST_KEY") == {}
    assert len(responses.calls) == 0


def test_batch_ceiling_is_a_named_constant():
    """The per-request id ceiling is a documented constant, not a literal."""
    assert STEAM_API_MAX_IDS_PER_REQUEST == 100
