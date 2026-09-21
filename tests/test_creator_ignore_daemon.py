"""The daemon's half of the creator-ignore feature: attribution and the sweep.

A new item can only be attributed to a creator at the API merge, because
discovery's page response carries the ``publishedfileid`` alone
(``_process_item`` -> ``_merge_and_clean_api_data``). An item whose merged
``creator_steamid`` the owner flagged is written settled and no web scrape, image
or translation stage is queued for it. Separately, the creator staleness sweep
skips a flagged creator, so their profile stops being refreshed.

The new names are reached through the ``database`` module so this file collects
against the pre-change source and each test can be shown failing on its own.
"""

from unittest import mock

from src import database
from src.daemon import Daemon
from src.database import (
    get_connection,
    get_creator,
    get_next_items_to_fetch,
    insert_or_update_creator,
    insert_or_update_item,
)

IGNORED_CREATOR = 111
LIVE_CREATOR = 222


# ── helpers ──────────────────────────────────────────────────────────────────


def _daemon(db_path, tmp_path) -> Daemon:
    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST"},
        "daemon": {"api_batch_size": 1, "target_appids": [1]},
    }
    return Daemon(config, config_path=str(tmp_path / "config.yaml"))


def _row(db_path, workshop_id, columns="*"):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT {columns} FROM workshop_items WHERE workshop_id = ?",
            (workshop_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _queue_rows(db_path):
    conn = get_connection(db_path)
    try:
        return [
            (row["entity_type"], row["entity_id"], row["field"])
            for row in conn.execute(
                "SELECT entity_type, entity_id, field FROM translation_queue "
                "ORDER BY entity_type, entity_id, field")
        ]
    finally:
        conn.close()


def _payload(creator):
    """A successful detail response with work that would normally all queue."""
    return {
        "status": 200,
        "title": "非 ASCII タイトル",
        "description": "A short description",
        "preview_url": "https://example.invalid/preview.jpg",
        "creator": creator,
        "time_updated": 1000,
    }


def _process(db_path, tmp_path, workshop_id, creator):
    row = {
        "workshop_id": workshop_id,
        "title": "seed",
        "fetch_status": None,
        "api_priority": 3,
        "creator_steamid": creator,
    }
    insert_or_update_item(db_path, row)
    daemon = _daemon(db_path, tmp_path)
    with mock.patch.object(daemon, "_should_enrich", return_value=True):
        daemon._process_item(row, api_data=_payload(creator))
    return daemon


# ── the API merge is the only place a creator is known ───────────────────────


def test_a_new_item_from_an_ignored_creator_lands_settled_with_no_queues(db_path, tmp_path):
    database.ignore_creator(db_path, IGNORED_CREATOR)

    _process(db_path, tmp_path, 1, IGNORED_CREATOR)

    row = _row(db_path, 1)
    assert row["fetch_status"] == -2, "the item is written ignored, not 200"
    for column in ("api_priority", "web_scrape_priority",
                   "image_priority", "translation_priority"):
        assert row[column] == 0, f"no downstream stage is queued ({column})"
    assert _queue_rows(db_path) == [], "and no translation field is queued either"
    assert row["api_fetched_at"] is not None, \
        "the detail request succeeded, so its clock is recorded"
    assert get_next_items_to_fetch(db_path) == [], \
        "an ignored item is in no fetch queue"


def test_a_new_item_from_a_live_creator_still_queues_its_stages(db_path, tmp_path):
    """The control: the same payload without the flag queues everything.

    Without this the ignored assertion would pass on a payload that never queued
    anything in the first place.
    """
    insert_or_update_creator(db_path, {"steamid": LIVE_CREATOR, "personaname": "Live"})

    _process(db_path, tmp_path, 1, LIVE_CREATOR)

    row = _row(db_path, 1)
    assert row["fetch_status"] == 200
    assert row["web_scrape_priority"] > 0, "a description-less item is scraped"
    assert row["image_priority"] > 0, "a preview is downloaded"
    assert ("item", 1, "title_en") in _queue_rows(db_path), \
        "a non-ASCII title is queued for translation"


def test_an_item_with_no_creator_is_not_settled(db_path, tmp_path):
    """Only an attributed item can be judged; a missing creator is not ignored."""
    _process(db_path, tmp_path, 1, None)

    assert _row(db_path, 1)["fetch_status"] == 200


# ── the staleness sweep skips an ignored creator ─────────────────────────────


def test_the_sweep_skips_an_ignored_creator_and_refreshes_the_rest(db_path, tmp_path):
    daemon = _daemon(db_path, tmp_path)
    insert_or_update_creator(db_path, {
        "steamid": IGNORED_CREATOR, "personaname": "Old", "api_fetched_at": 1,
    })
    database.ignore_creator(db_path, IGNORED_CREATOR)
    insert_or_update_creator(db_path, {
        "steamid": LIVE_CREATOR, "personaname": "Old2", "api_fetched_at": 1,
    })

    with mock.patch(
        "src.daemon.get_player_summaries",
        return_value={IGNORED_CREATOR: {"personaname": "作者"},
                      LIVE_CREATOR: {"personaname": "作者二"}},
    ) as summaries:
        daemon._refresh_creators([IGNORED_CREATOR, LIVE_CREATOR])

    assert summaries.call_args[0][0] == [LIVE_CREATOR], \
        "the ignored creator must not be in the request at all"
    assert get_creator(db_path, IGNORED_CREATOR)["personaname"] == "Old", \
        "the ignored creator's profile is left as it was"
    assert get_creator(db_path, LIVE_CREATOR)["personaname"] == "作者二"
