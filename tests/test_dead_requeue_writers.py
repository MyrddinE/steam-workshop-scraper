"""Issue 74: no writer may put a dead item back in the API queue.

A dead item is deliberately final -- ``fetch_status = -1`` is the API's
permanent answer, ``_promote_stale_items`` promotes only ``fetch_status = 200``,
and the handoff invariant is that a dead item is in **no** queue. Four writers
that raise a queue priority on an item that already exists did not honour that:
the image worker's and the web worker's "the item changed" bump, and the two
discovery call sites. Because the fetch poll excludes dead rows, the priority
they wrote could never be handed out -- but it stranded the row in the
``dead_queued`` / ``dead_items_by_queue`` readings and broke the invariant.

Each writer is pinned twice: a dead row is not bumped, and a live row still is.
"""

import time
from unittest.mock import patch

from src.daemon import Daemon
from src.database import get_connection, insert_or_update_item
from tests.conftest import seed_pacing_delay

# ── helpers ──────────────────────────────────────────────────────────────────


def _row(db_path, workshop_id, columns="fetch_status, api_priority, image_priority"):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT {columns} FROM workshop_items WHERE workshop_id = ?",
            (workshop_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _image_bump_once(db_path, item):
    """Run the image worker until its "item changed" UPDATE has committed.

    One iteration exactly: the fake queue hands the item over once, and
    ``pacing.wait`` -- which runs *after* the commit -- stops the worker. That
    the worker stopped is what proves the write landed.
    """
    from src.image_worker import ImageDownloadThread

    worker = ImageDownloadThread(db_path, ".pauselock")
    served = [0]

    def next_item(*args, **kwargs):
        served[0] += 1
        return item if served[0] == 1 else None

    def stop_after_write(*args, **kwargs):
        worker.running = False

    with patch("src.image_worker.get_next_image_item", side_effect=next_item), \
         patch("src.image_worker.requests.get",
               side_effect=Exception("Connection refused")), \
         patch("src.image_worker.pacing.wait", side_effect=stop_after_write):
        worker.start()
        worker.join(timeout=5)

    assert not worker.is_alive(), "the worker did not stop after its write"


def _web_bump_once(db_path, workshop_id):
    """One transport failure through the web worker's own handler."""
    from src.web_worker import WebScraperThread

    worker = WebScraperThread(db_path, ".pauselock")
    worker._handle_unknown({"workshop_id": workshop_id}, "http://example.com", None)


def _config(db_path):
    seed_pacing_delay(db_path, "api", 0.01)
    return {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "api_batch_size": 10},
    }


# ── the image worker's changed-item bump ─────────────────────────────────────


def test_the_image_worker_does_not_bump_a_dead_rows_api_priority(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": -1, "api_priority": 0, "image_priority": 5,
        "preview_url": "http://example.com/img.jpg",
    })

    _image_bump_once(db_path, {
        "workshop_id": 1, "preview_url": "http://example.com/img.jpg",
        "image_priority": 5, "steam_updated_at": 1,
    })

    row = _row(db_path, 1)
    assert row["fetch_status"] == -1
    assert row["api_priority"] == 0, "a dead item is in no queue"
    assert row["image_priority"] == 4, (
        "only the api_priority term is guarded: the image decrement still runs, "
        "because it moves the row out of the image queue rather than reviving it"
    )


def test_the_image_worker_still_bumps_a_live_rows_api_priority(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": 200, "api_priority": 0, "image_priority": 5,
        "preview_url": "http://example.com/img.jpg",
    })

    _image_bump_once(db_path, {
        "workshop_id": 1, "preview_url": "http://example.com/img.jpg",
        "image_priority": 5, "steam_updated_at": 1,
    })

    row = _row(db_path, 1)
    assert row["api_priority"] == 2, "a live item still asks for the metadata refresh"
    assert row["image_priority"] == 4


# ── the web worker's transport-failure bump ──────────────────────────────────


def test_the_web_worker_does_not_bump_a_dead_rows_api_priority(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": -1, "api_priority": 0,
    })

    _web_bump_once(db_path, 1)

    assert _row(db_path, 1)["api_priority"] == 0, "a dead item is in no queue"
    assert _row(db_path, 1)["fetch_status"] == -1


def test_the_web_worker_still_bumps_a_live_rows_api_priority(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "fetch_status": 200, "api_priority": 0,
    })

    _web_bump_once(db_path, 1)

    assert _row(db_path, 1)["api_priority"] == 2


# ── cursor discovery ─────────────────────────────────────────────────────────


def _discover_walk(db_path, workshop_ids):
    with patch("src.daemon.query_workshop_newest_page") as mock_query, \
            patch("src.daemon.time.sleep"):
        mock_query.return_value = {
            "total": len(workshop_ids),
            "items": [{"publishedfileid": str(wid)} for wid in workshop_ids],
            "next_cursor": "",
        }
        Daemon(_config(db_path)).seed_database(fill_target=100)


def test_cursor_discovery_does_not_requeue_a_dead_item(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 4242, "fetch_status": -1, "api_priority": 0,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 4243, "fetch_status": 200, "api_priority": 0,
    })

    _discover_walk(db_path, [4242, 4243])

    dead = _row(db_path, 4242)
    assert dead["fetch_status"] == -1, "a dead item is final"
    assert dead["api_priority"] == 0, "re-seeing a dead item does not re-queue it"
    assert _row(db_path, 4243)["api_priority"] == 3, \
        "a live item rediscovered on the cursor walk is still queued at new priority"


# ── page discovery ───────────────────────────────────────────────────────────


def test_page_discovery_does_not_requeue_a_dead_item(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 4242, "fetch_status": -1, "api_priority": 0,
        "steam_updated_at": 100,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 4243, "fetch_status": 200, "api_priority": 0,
        "steam_updated_at": 100,
    })
    daemon = Daemon(_config(db_path))

    with patch("src.daemon.query_workshop_updated_page") as mock_query:
        mock_query.return_value = {
            "total": 2,
            "items": [
                {"publishedfileid": "4242", "time_updated": 200},
                {"publishedfileid": "4243", "time_updated": 200},
            ],
            "next_cursor": "",
        }
        daemon._run_page_discovery()

    dead = _row(db_path, 4242)
    assert dead["fetch_status"] == -1, "a dead item is final"
    assert dead["api_priority"] == 0, "the updated page hands back dead items too"
    assert _row(db_path, 4243)["api_priority"] == 5, \
        "a live item the updated page saw again is still queued at page priority"
