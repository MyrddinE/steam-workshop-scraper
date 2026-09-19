"""Tests for web scraper, image download, and translator worker threads."""
import pytest
from unittest.mock import patch

from src.web_worker import WEB_DELAY_DEFAULT


# ── Web worker ───────────────────────────────────────────────────────────────

def test_web_worker_thread_lifecycle(tmp_path):
    """Web worker starts, runs, and stops cleanly."""
    from src.web_worker import WebScraperThread

    db_path = str(tmp_path / "test.db")
    worker = WebScraperThread(db_path, '.pauselock')
    assert worker.running is True
    worker.running = False
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_an_idle_worker_wakes_for_a_stop_instead_of_sleeping_it_out(db_path):
    """The idle nap polls the stop flag; it used to be a blind ten-second sleep.

    That blind sleep is why a shutdown with an empty queue waited out the
    daemon's join timeout: the flag was set and the worker did not look at it
    until the whole nap had run.
    """
    import time as _time
    from src.web_worker import WebScraperThread

    worker = WebScraperThread(db_path, ".pauselock")
    with patch("src.web_worker.get_next_web_scrape_item", return_value=None):
        worker.start()
        _time.sleep(0.2)  # let it enter the idle wait
        worker.running = False
        worker.join(timeout=2)
        assert not worker.is_alive(), "the idle wait must observe the stop flag"


def test_web_worker_failure_sets_api_priority(db_path):
    """Web scrape failure updates api_priority to 2 via the else branch."""
    from src.web_worker import WebScraperThread
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5, "api_priority": 0})

    worker = WebScraperThread(db_path, '.pauselock')

    with patch('src.web_worker.get_next_web_scrape_item') as mock_next, \
         patch('src.web_worker.scrape_extended_details', return_value=None), \
         patch('time.sleep'):  # don't actually sleep
        mock_next.return_value = {"workshop_id": 1, "steam_updated_at": 123456}
        worker.start()
        worker.running = False  # stop after current iteration
        worker.join(timeout=2)

    conn = get_connection(db_path)
    prio = conn.execute(
        "SELECT api_priority FROM workshop_items WHERE workshop_id=1"
    ).fetchone()[0]
    conn.close()
    assert prio == 2


# ── Web worker: the delay floor ──────────────────────────────────────────────
# The floor was raised from 1.0 s to 6.0 s because the same Steam budget is
# shared with the owner's own browsing: when scrapes fail, the worker has to
# back off far enough that the Workshop is still usable by hand.

def test_the_default_web_delay_is_not_below_the_floor():
    """A default under the floor is a delay the decay rule considers too fast."""
    from src.web_worker import WEB_DELAY_FLOOR, WebScraperThread

    worker = WebScraperThread("test.db", ".pauselock")
    assert worker.web_delay == WEB_DELAY_DEFAULT
    assert worker.web_delay >= WEB_DELAY_FLOOR

    # An explicit persisted value above the floor is still honoured: the floor
    # only binds while decaying, it must not reset a slow installation.
    configured = WebScraperThread("test.db", ".pauselock", {"web_delay_seconds": 20})
    assert configured.web_delay == 20.0


def test_the_web_delay_floor_is_honoured_during_decay(db_path):
    """The decay is measured in time, and it stops at the floor.

    The clock is driven rather than waited on: one tick per attempt, each worth
    well over the half-life, so a handful of successes is enough to drive the
    delay into the floor. Successes alone would do nothing at all now -- the
    decay is paid for in elapsed time, not in requests.
    """
    from src import pacing
    from src.database import insert_or_update_item
    from src.web_worker import WEB_DELAY_FLOOR, WebScraperThread

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    # Just above the floor: one step lands under it unless it is clamped.
    worker = WebScraperThread(db_path, ".pauselock")
    worker.web_delay = WEB_DELAY_FLOOR + 0.2

    ticks = iter(range(0, 10_000, 120))
    with patch("src.pacing.now", side_effect=lambda: next(ticks)):
        worker._clock = pacing.Clock()
        worker = _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                                 [FOUND] * 10, worker=worker)

    assert worker.web_delay == WEB_DELAY_FLOOR, \
        "the decay must stop at the floor, not pass through it"


# ── Image worker ─────────────────────────────────────────────────────────────


def test_image_worker_thread_lifecycle(tmp_path):
    """Image worker starts, runs, and stops cleanly."""
    from src.image_worker import ImageDownloadThread

    db_path = str(tmp_path / "test.db")
    worker = ImageDownloadThread(db_path, '.pauselock')
    assert worker.running is True
    worker.running = False
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_image_worker_failure_sets_api_priority(db_path):
    """Image download failure bumps api_priority to 2."""
    from src.image_worker import ImageDownloadThread
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {
        "workshop_id": 1, "needs_image": 5, "api_priority": 0,
        "preview_url": "http://example.com/img.jpg"
    })

    worker = ImageDownloadThread(db_path, '.pauselock')

    call_count = [0]
    def fail_on_first(*args, **kwargs):
        call_count[0] += 1
        raise Exception("Connection refused")

    with patch('src.image_worker.get_next_image_item') as mock_next, \
         patch('src.image_worker.requests.get', side_effect=fail_on_first), \
         patch('src.image_worker.time.sleep'):  # don't actually sleep
        mock_next.return_value = {
            "workshop_id": 1, "preview_url": "http://example.com/img.jpg",
            "needs_image": 5, "steam_updated_at": 123456,
        }
        worker.start()
        import time as _time
        deadline = _time.time() + 3
        while call_count[0] < 1 and _time.time() < deadline:
            _time.sleep(0.05)
        worker.running = False
        worker.join(timeout=2)

    conn = get_connection(db_path)
    prio = conn.execute(
        "SELECT api_priority FROM workshop_items WHERE workshop_id=1"
    ).fetchone()[0]
    conn.close()
    assert prio == 2


# ── Image worker: capture ────────────────────────────────────────────────────
# The image worker made no capture calls at all, so a failure left no evidence
# beyond a one-line warning. Failures are now captured whenever an outbox is
# configured; successes only under the image debug switch. The image bytes are
# never copied into the outbox — the file the download wrote is the artefact.

class _FakeImageResponse:
    """Just enough of a requests response to drive the download path."""

    def __init__(self, status_code=200, headers=None, body=b"",
                 url="http://example.com/img.jpg"):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self._body = body

    def iter_content(self, chunk_size):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]


def _run_image_worker(db_path, response=None, error=None, images_root=None):
    """Run exactly one image download, then stop the worker.

    ``response`` is a fake response; ``error`` makes ``requests.get`` raise
    instead. ``images_root`` redirects the saved file out of the repository's
    own images directory, which a success test must not pollute.
    """
    import os
    from contextlib import ExitStack
    from src.image_worker import ImageDownloadThread

    worker = ImageDownloadThread(db_path, ".pauselock")
    item = {"workshop_id": 5, "preview_url": "http://example.com/img.jpg",
            "needs_image": 1, "steam_updated_at": 1}
    served = [0]

    def next_item(*args, **kwargs):
        served[0] += 1
        if served[0] > 1:
            worker.running = False
            return None
        return item

    with ExitStack() as stack:
        stack.enter_context(patch("src.image_worker.get_next_image_item",
                                  side_effect=next_item))
        if error is not None:
            stack.enter_context(patch("src.image_worker.requests.get",
                                      side_effect=error))
        else:
            stack.enter_context(patch("src.image_worker.requests.get",
                                      return_value=response))
        stack.enter_context(patch("src.image_worker.time.sleep"))
        # `pacing.wait` measures its deadline against the real monotonic clock,
        # so stubbing sleep alone leaves it spinning for the delay in real time.
        stack.enter_context(patch("src.pacing.wait"))
        if images_root is not None:
            stack.enter_context(patch(
                "src.image_worker.get_image_path",
                side_effect=lambda base, wid, ext: os.path.join(
                    images_root, f"{wid}.{ext}")))
        worker.start()
        worker.join(timeout=5)
    return worker


def _image_failure_records(outbox):
    import json

    from src import capture

    group = capture.group_id("image_download_failed", None, "image_download")
    group_dir = outbox / "failures" / group
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(group_dir.glob("*.json"))
            if path.name != "_group.json"]


def test_an_image_failure_is_captured_with_status_and_headers(db_path, tmp_path):
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 5, "needs_image": 1,
                                        "preview_url": "http://example.com/img.jpg"})
        _run_image_worker(db_path, response=_FakeImageResponse(
            status_code=404,
            headers={"Content-Type": "text/html", "X-Cache": "MISS"},
            body=b"<html>not found</html>"))
    finally:
        capture.configure(None)

    records = _image_failure_records(outbox)
    assert len(records) == 1
    record = records[0]
    assert record["workshop_id"] == 5
    assert record["http_status"] == 404
    assert record["headers"]["X-Cache"] == "MISS"
    assert record["content_type"] == "text/html"
    assert record["body_file"] is None
    assert record["error"]


def test_an_image_transport_failure_is_captured_with_the_exception(db_path, tmp_path):
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 5, "needs_image": 1,
                                        "preview_url": "http://example.com/img.jpg"})
        _run_image_worker(db_path, error=Exception("Connection refused"))
    finally:
        capture.configure(None)

    records = _image_failure_records(outbox)
    assert len(records) == 1
    assert records[0]["http_status"] is None
    assert records[0]["headers"] == {}
    assert "Connection refused" in records[0]["error"]


def test_an_image_success_is_captured_only_when_the_switch_is_on(db_path, tmp_path):
    import os

    from src import capture
    from src.database import insert_or_update_item

    image_bytes = b"\xff\xd8\xff\xe0" + b"jpeg-payload" * 8
    images_root = tmp_path / "images"
    outbox = tmp_path / "outbox"
    insert_or_update_item(db_path, {"workshop_id": 5, "needs_image": 1,
                                    "preview_url": "http://example.com/img.jpg"})
    response = _FakeImageResponse(headers={"Content-Type": "image/jpeg"},
                                  body=image_bytes)

    # Switch off: the image is downloaded but no capture directory appears.
    capture.configure(str(outbox))
    try:
        _run_image_worker(db_path, response=response, images_root=str(images_root))
    finally:
        capture.configure(None)
    assert not (outbox / "image_downloads").exists()

    # Switch on: metadata is recorded and points at the file on disk.
    capture.configure(str(outbox), capture_image_downloads=True)
    try:
        _run_image_worker(db_path, response=response, images_root=str(images_root))
    finally:
        capture.configure(None)

    import json
    records = [json.loads(path.read_text(encoding="utf-8"))
               for path in (outbox / "image_downloads").glob("*.json")]
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "image_download"
    assert record["http_status"] == 200
    assert record["content_type"] == "image/jpeg"
    assert record["bytes_written"] == len(image_bytes)
    assert record["body_file"] is None
    with open(record["saved_path"], "rb") as handle:
        assert handle.read() == image_bytes, "the captured path must be the artefact"


def test_no_image_capture_writes_the_bytes_into_the_outbox(db_path, tmp_path):
    """Assert on the files written, not on intent: the outbox holds metadata."""
    from src import capture
    from src.database import insert_or_update_item

    image_bytes = b"\x89PNG\r\n\x1a\n" + b"UNIQUE-IMAGE-PAYLOAD" * 64
    outbox = tmp_path / "outbox"
    insert_or_update_item(db_path, {"workshop_id": 5, "needs_image": 1,
                                    "preview_url": "http://example.com/img.jpg"})

    capture.configure(str(outbox), capture_image_downloads=True)
    try:
        _run_image_worker(db_path, response=_FakeImageResponse(
            headers={"Content-Type": "image/png"}, body=image_bytes),
            images_root=str(tmp_path / "images"))
    finally:
        capture.configure(None)

    written = [path for path in outbox.rglob("*") if path.is_file()]
    assert written, "the debug capture was on, so the metadata record must exist"
    for path in written:
        assert path.suffix == ".json", f"unexpected non-metadata artefact: {path}"
        assert image_bytes not in path.read_bytes(), f"image bytes copied into {path}"
    assert not list(outbox.rglob("*.body"))


def test_puremagic_detection_is_logged_at_debug_not_info(db_path, tmp_path, caplog):
    """The successful magic-byte guess is diagnostic, not operational.

    It fires for every response whose Content-Type is absent from ``MIME_MAP``
    but whose body puremagic recognises, so at INFO it narrates a routine
    fallback at the level reserved for things the owner may need to act on. The
    level is the whole behaviour pinned here: DEBUG, and nothing louder.
    """
    import logging

    from src.database import insert_or_update_item

    image_bytes = b"\x89PNG\r\n\x1a\n" + b"PNG-PAYLOAD" * 16
    insert_or_update_item(db_path, {"workshop_id": 5, "needs_image": 1,
                                    "preview_url": "http://example.com/img.jpg"})

    caplog.set_level(logging.DEBUG)
    _run_image_worker(
        db_path,
        response=_FakeImageResponse(
            headers={"Content-Type": "application/octet-stream"},
            body=image_bytes),
        images_root=str(tmp_path / "images"))

    detected = [record for record in caplog.records
                if "Puremagic detected" in record.getMessage()]
    assert detected, "the detection line must be emitted"
    assert [record.levelno for record in detected] == [logging.DEBUG]
    assert "→ .png" in detected[0].getMessage()


# ── Web worker: selector miss ────────────────────────────────────────────────
# A selector miss returns {"description": None, "tags": []}, which is truthy. It
# used to be taken as success, writing extended_description = NULL and
# needs_web_scrape = 0, so the item was recorded as permanently scraped with
# nothing to show for it and was never retried. A miss now distinguishes a page
# that was never the item's (left queued) from the item page with no description
# (a permanent absence, so the item leaves the queue).

def _run_web_worker(db_path, item, scrape_data, iterations=None, worker=None):
    """Run the worker through exactly ``iterations`` items, then stop it.

    A thread cannot be asked for a fixed number of iterations, so the item
    source is exhausted instead: after the requested count it clears
    ``running`` and returns None, and the loop's own check ends the run. That
    makes the pacing counters deterministic, which a start-then-stop race is
    not. Pass a list of responses to give each iteration its own page; the
    count defaults to the list's length. ``worker`` allows pacing state to be
    seeded before the single run; it is never restarted.
    """
    from src.web_worker import WebScraperThread

    if worker is None:
        worker = WebScraperThread(db_path, ".pauselock")
    if isinstance(scrape_data, list):
        responses = list(scrape_data)
        if iterations is None:
            iterations = len(responses)
        scrape_patch = patch("src.web_worker.scrape_extended_details",
                             side_effect=responses)
    else:
        if iterations is None:
            iterations = 1
        scrape_patch = patch("src.web_worker.scrape_extended_details",
                             return_value=scrape_data)

    served = 0

    def next_item(*args, **kwargs):
        nonlocal served
        served += 1
        if served > iterations:
            worker.running = False
            return None
        return item

    # `time.sleep` alone is not enough to keep this fast: pacing.wait measures
    # its deadline against the real monotonic clock, so with sleep stubbed it
    # would spin for the delay in real time and the join below would give up
    # mid-run. The wait is not what these tests are about, so it is patched out.
    with patch("src.web_worker.get_next_web_scrape_item", side_effect=next_item), \
         scrape_patch, patch("time.sleep"), patch("src.pacing.wait"):
        worker.start()
        worker.join(timeout=5)
    return worker


def _web_scrape_priority(db_path, workshop_id=1):
    from src.database import get_connection
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT needs_web_scrape FROM workshop_items WHERE workshop_id=?",
            (workshop_id,)).fetchone()[0]
    finally:
        conn.close()


def _run_one_attempt_with_mock(item, response, worker):
    """Run one attempt, handing back the scrape mock so its calls can be counted.

    ``_run_web_worker`` owns its own patch, so it cannot answer the question
    these cases ask: how many requests one attempt made.
    """
    served = 0

    def next_item(*args, **kwargs):
        nonlocal served
        served += 1
        if served > 1:
            worker.running = False
            return None
        return item

    with patch("src.web_worker.get_next_web_scrape_item", side_effect=next_item), \
         patch("src.web_worker.scrape_extended_details", return_value=response) as scrape, \
         patch("time.sleep"), patch("src.pacing.wait"):
        worker.start()
        worker.join(timeout=5)
    return scrape


MISS = {"description": None, "tags": [], "body": "<html>no selector</html>",
        "http_status": 200, "final_url": "https://example.invalid/?id=1"}

# The description selector matched and the item was stored: the one outcome that
# is unambiguously a success.
FOUND = {"description": "scraped text", "tags": [], "body": None,
         "http_status": 200, "final_url": "https://example.invalid/?id=1"}

# The item template is present but the description element is not: the page really
# is the item's, and it simply has no extended description.
ITEM_PAGE_WITHOUT_DESCRIPTION = dict(
    MISS, body='<html><div class="workshopItem">x</div></html>')

# The Workshop's "no such item" page. A live probe showed an absent but
# well-formed id is served with **HTTP 200** from Steam's ordinary error shell,
# so the wording, not the status, is the evidence; the id-0 wording is included
# because it is the other phrasing the same shell carries.
MISSING_ITEM_PAGE = dict(
    MISS, body='<title>Steam Community :: Error</title>'
               '<h3>That item does not exist.  It may have been removed by the author.</h3>')


def test_selector_miss_without_the_item_page_leaves_the_item_queued(db_path):
    """An error, wall or throttle page is not the item's fault, so the item keeps
    its place in the queue rather than decaying out of it."""
    from src.database import insert_or_update_item

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1}, MISS)

    assert _web_scrape_priority(db_path) == 5, \
        "the item must not be blamed for a page it never got"


def test_selector_miss_on_the_item_page_clears_the_queue(db_path):
    """The item page loaded and has no description: retrying cannot change that,
    so the item leaves the queue, which is what lets the queue drain."""
    from src.database import insert_or_update_item

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                    ITEM_PAGE_WITHOUT_DESCRIPTION)

    assert _web_scrape_priority(db_path) == 0, \
        "a genuine, permanent absence must not stay queued"


def test_selector_miss_leaves_existing_description_untouched(db_path):
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {
        "workshop_id": 1, "needs_web_scrape": 3,
        "extended_description": "previously scraped text"})

    _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                    ITEM_PAGE_WITHOUT_DESCRIPTION)

    conn = get_connection(db_path)
    try:
        stored = conn.execute(
            "SELECT extended_description FROM workshop_items WHERE workshop_id=1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == "previously scraped text", "a miss must not blank the description"


def test_selector_miss_does_not_touch_api_priority(db_path):
    """The request succeeded, so this is not a network failure."""
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 2, "api_priority": 0})

    _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1}, MISS)

    conn = get_connection(db_path)
    try:
        prio = conn.execute(
            "SELECT api_priority FROM workshop_items WHERE workshop_id=1").fetchone()[0]
    finally:
        conn.close()
    assert prio == 0


def test_repeated_walls_grow_the_web_delay(db_path):
    """A page that is not the item's is the server declining to serve, so it
    must slow the scraper down exactly like a request failure."""
    from src.database import insert_or_update_item
    from src.web_worker import WEB_DELAY_DEFAULT

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    # A healthy run first: the delay rule compounds off a streak, and the walls
    # are what should end it.
    worker = _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                             [FOUND] * 5 + [MISS] * 2)

    assert worker.web_had_success_streak is False, "the walls ended the success streak"
    assert worker.web_failures >= 2
    assert worker.web_delay > WEB_DELAY_DEFAULT, "repeated walls must slow the scraper down"


def test_a_descriptionless_item_is_neutral_for_pacing(db_path):
    """The page was served and the item is finished with, so there is nothing to
    back off from — but it yielded nothing, so it must not count as a success
    and reset a failure streak that is still growing."""
    from src.database import insert_or_update_item
    from src.web_worker import WEB_DELAY_DEFAULT, WebScraperThread

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    # A streak is in progress: one wall has just landed and the next wall would
    # grow the delay. The description-less page must leave all of that alone.
    worker = WebScraperThread(db_path, ".pauselock")
    worker.web_had_success_streak = True
    worker.web_failures = 1
    worker.web_successes = 0

    worker = _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                             ITEM_PAGE_WITHOUT_DESCRIPTION, worker=worker)

    assert worker.web_failures == 1, "a served page is not a failure"
    assert worker.web_successes == 0, "and it is not a success either"
    assert worker.web_delay == WEB_DELAY_DEFAULT, "it does not grow the delay"


def test_a_found_description_resets_the_failure_streak(db_path):
    from src.database import insert_or_update_item

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    worker = _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                             [MISS] * 7 + [FOUND])

    assert worker.web_failures == 0, "a found description clears the streak"
    assert worker.web_successes >= 1


# ── Web worker: the outcome taxonomy ─────────────────────────────────────────
# Which outcome buys which response is the policy. Only an unattributable
# outcome grows the delay now: a rate limit pauses, a missing item and a
# description-less item page are answers the item gave, and a gate is a session
# problem a slower pace cannot fix. The classification is one table so the next
# person can read the policy off it.

def test_classify_scrape_names_every_outcome():
    from src.web_worker import ScrapeOutcome, classify_scrape

    assert classify_scrape(FOUND) is ScrapeOutcome.SUCCESS
    assert classify_scrape(dict(MISS, http_status=404)) is ScrapeOutcome.ITEM_MISSING
    assert classify_scrape(dict(MISS, http_status=410)) is ScrapeOutcome.ITEM_MISSING
    assert classify_scrape(MISSING_ITEM_PAGE) is ScrapeOutcome.ITEM_MISSING
    assert classify_scrape(
        dict(MISS, body="<h1>You have made too many requests</h1>")
    ) is ScrapeOutcome.RATE_LIMITED
    assert classify_scrape(
        ITEM_PAGE_WITHOUT_DESCRIPTION) is ScrapeOutcome.ITEM_PAGE_WITHOUT_DESCRIPTION
    assert classify_scrape(
        dict(MISS, body='<title>Steam Community :: Error</title><div id="AgeCheck">x</div>')
    ) is ScrapeOutcome.GATED
    assert classify_scrape(MISS) is ScrapeOutcome.UNKNOWN
    assert classify_scrape(None) is ScrapeOutcome.UNKNOWN
    # A 5xx is a server fault with no attributable cause, whatever the body says.
    assert classify_scrape(
        dict(MISS, http_status=503,
             body='<title>Steam Community :: Error</title><div id="AgeCheck">x</div>')
    ) is ScrapeOutcome.UNKNOWN
    assert classify_scrape(
        dict(MISS, http_status=503, body="<h3>That item does not exist.</h3>")
    ) is ScrapeOutcome.UNKNOWN


def test_a_404_does_not_grow_the_delay_and_clears_the_queue(db_path):
    """A missing item is the item's own doing, not a pacing problem."""
    from src.database import insert_or_update_item
    from src.web_worker import WebScraperThread

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    worker = WebScraperThread(db_path, ".pauselock")
    worker.web_had_success_streak = True
    worker.web_failures = 1

    worker = _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                             dict(MISS, http_status=404, body=""), worker=worker)

    assert _web_scrape_priority(db_path) == 0, "a gone item must leave the queue"
    assert worker.web_failures == 1, "a 404 is not a failure for the delay rule"
    assert worker.web_delay == WEB_DELAY_DEFAULT


def test_steams_missing_item_page_clears_the_queue_despite_http_200(db_path):
    """Steam serves the item-error page with HTTP 200, so wording is the signal."""
    from src.database import insert_or_update_item
    from src.web_worker import WebScraperThread

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    worker = WebScraperThread(db_path, ".pauselock")
    worker.web_had_success_streak = True
    worker.web_failures = 1

    worker = _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                             MISSING_ITEM_PAGE, worker=worker)

    assert _web_scrape_priority(db_path) == 0, "the page says the item is gone"
    assert worker.web_failures == 1
    assert worker.web_delay == WEB_DELAY_DEFAULT


def test_a_transport_failure_still_grows_the_delay(db_path):
    """The one unattributable outcome keeps the back-off it always had."""
    from src.database import insert_or_update_item

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    worker = _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                             [FOUND] * 5 + [None] * 2)

    assert worker.web_had_success_streak is False, "the transport failures ended the streak"
    assert worker.web_failures >= 2
    assert worker.web_delay > WEB_DELAY_DEFAULT, "a transport failure still slows the scraper"


def test_a_rate_limit_halves_the_request_rate(db_path):
    """A throttle is an unambiguous refusal, so it slows the scraper at once.

    It used to sleep a fixed 300 s and leave the delay untouched, which was a
    second pacing rule that could not converge -- the same pause however often
    the throttle recurred, and no slower a rate afterwards. Now the doubling is
    served by the wait at the end of the iteration, so a sustained throttle
    backs off geometrically.
    """
    from src.database import insert_or_update_item
    from src.web_worker import WebScraperThread

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    worker = WebScraperThread(db_path, ".pauselock")
    before = worker.web_delay

    throttled = dict(MISS, body="<h1>You've made too many requests recently.</h1>")
    worker = _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                             throttled, worker=worker)

    assert worker.web_delay == before * 2, \
        "a throttle is a refusal and halves the request rate"
    assert worker.web_failures == 1, "a throttle is counted, unlike a bad item"
    assert _web_scrape_priority(db_path) == 5, "the item is not at fault"


def test_a_gate_does_not_grow_the_delay(db_path):
    """A sign-in wall or age check is a session problem, not a pacing one."""
    from src.database import insert_or_update_item
    from src.web_worker import WebScraperThread

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    worker = WebScraperThread(db_path, ".pauselock")
    worker.web_had_success_streak = True
    worker.web_failures = 1

    gated = dict(MISS, body='<title>Steam Community :: Error</title>'
                            '<div id="AgeCheck">age check</div>')
    worker = _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                             gated, worker=worker)

    assert worker.web_failures == 1, "a wall is not a failure for the delay rule"
    assert worker.web_delay == WEB_DELAY_DEFAULT
    assert _web_scrape_priority(db_path) == 5, "the item keeps its queue place"


def test_a_gated_attempt_makes_exactly_one_request_and_keeps_the_item_queued(db_path):
    """The retry is the queue's job now, under the worker's own adaptive delay.

    A gated page used to earn a second request the moment the cookie refresh
    changed anything -- the only request in the worker that paid no delay at
    all, and one spaced by nothing but the scraper's own fixed 5 s gate. The
    miss now takes the ordinary `GATED` path: one request, the cookie still
    refreshed, the item left queued, and the queue retries it later.
    """
    from src.database import insert_or_update_item
    from src.web_worker import WebScraperThread

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    refreshed = []
    worker = WebScraperThread(db_path, ".pauselock", {}, None,
                              lambda: refreshed.append(1) or True)
    gated = dict(MISS, body='<title>Steam Community :: Error</title>'
                            '<div id="AgeCheck">age check</div>')

    scrape = _run_one_attempt_with_mock(
        {"workshop_id": 1, "steam_updated_at": 1}, gated, worker)

    assert scrape.call_count == 1, "one request per attempt, never an immediate re-scrape"
    assert refreshed == [1], "the cookie refresh still happens on a gated page"
    assert _web_scrape_priority(db_path) == 5, "normal gate handling leaves the item queued"


def test_the_missing_item_log_quotes_the_status_and_the_wording(db_path, caplog):
    """The owner reads scraper.log alone: a 404 and a timeout must read apart."""
    import logging
    from src.database import insert_or_update_item

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    with caplog.at_level(logging.WARNING):
        _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1},
                        dict(MISS, http_status=404, body=""))

    text = caplog.text.lower()
    assert "http 404" in text
    assert "not available" in text
    assert "transport failure" not in text


def test_the_transport_failure_log_is_distinguishable_from_a_missing_item(db_path, caplog):
    import logging
    from src.database import insert_or_update_item

    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})

    with caplog.at_level(logging.WARNING):
        _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1}, None)

    text = caplog.text.lower()
    assert "transport failure" in text
    assert "404" not in text


def test_a_missing_item_is_captured_as_its_own_kind(db_path, tmp_path):
    """There is no capture of this page yet, so it must be noisily on record."""
    import json
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 9, "needs_web_scrape": 5})
        _run_web_worker(db_path, {"workshop_id": 9, "steam_updated_at": 1},
                        MISSING_ITEM_PAGE)
    finally:
        capture.configure(None)

    group = capture.group_id("web_item_missing", None, "web_scrape")
    group_dir = outbox / "failures" / group
    records = [p for p in group_dir.glob("*.json") if p.name != "_group.json"]
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["workshop_id"] == 9
    assert record["http_status"] == 200
    assert record["shape"]["title_tag"] == "Steam Community :: Error"



def _only_capture_record(outbox, kind):
    """The one sample record a run filed under ``kind``, read back as JSON."""
    import json
    from src import capture

    group = capture.group_id(kind, None, "web_scrape")
    group_dir = outbox / "failures" / group
    records = [p for p in group_dir.glob("*.json") if p.name != "_group.json"]
    assert len(records) == 1, f"expected one {kind} capture, found {len(records)}"
    return json.loads(records[0].read_text())


def test_a_descriptionless_item_page_is_captured_under_its_own_kind(db_path, tmp_path):
    """The page is the item's and simply has no description: not a selector miss."""
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 21, "needs_web_scrape": 5})
        _run_web_worker(db_path, {"workshop_id": 21, "steam_updated_at": 1},
                        ITEM_PAGE_WITHOUT_DESCRIPTION)
    finally:
        capture.configure(None)

    record = _only_capture_record(outbox, "web_description_absent")
    assert record["kind"] == "web_description_absent"
    assert record["selector"] is None, "this page is the item's; no selector failed"


def test_a_gated_page_is_captured_under_its_own_kind(db_path, tmp_path):
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 22, "needs_web_scrape": 5})
        gated = dict(MISS, body='<title>Steam Community :: Error</title>'
                                '<div id="AgeCheck">age check</div>')
        _run_web_worker(db_path, {"workshop_id": 22, "steam_updated_at": 1}, gated)
    finally:
        capture.configure(None)

    record = _only_capture_record(outbox, "web_gated")
    assert record["kind"] == "web_gated"
    assert record["selector"] is None


def test_an_unattributable_page_is_captured_as_unknown(db_path, tmp_path):
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 23, "needs_web_scrape": 5})
        _run_web_worker(db_path, {"workshop_id": 23, "steam_updated_at": 1}, MISS)
    finally:
        capture.configure(None)

    record = _only_capture_record(outbox, "web_unknown")
    assert record["kind"] == "web_unknown"
    assert record["selector"] is None


def test_no_miss_is_still_filed_under_the_retired_selector_kind(db_path, tmp_path):
    """``web_selector_miss`` was the default and described none of these paths."""
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 24, "needs_web_scrape": 5})
        _run_web_worker(db_path, {"workshop_id": 24, "steam_updated_at": 1}, MISS)
    finally:
        capture.configure(None)

    from src.web_scraper import DESCRIPTION_SELECTOR
    retired = outbox / "failures" / capture.group_id(
        "web_selector_miss", DESCRIPTION_SELECTOR, "web_scrape")
    assert not retired.exists(), "the default kind must not be used for an unknown page"


def test_an_unattributable_page_records_the_page_shape(db_path, tmp_path):
    """The capture must describe the page well enough to recognise the break."""
    from src import capture
    from src.database import insert_or_update_item

    outbox = tmp_path / "outbox"
    capture.configure(str(outbox))
    try:
        insert_or_update_item(db_path, {"workshop_id": 7, "needs_web_scrape": 5})
        _run_web_worker(db_path, {"workshop_id": 7, "steam_updated_at": 1},
                        dict(MISS, body='<html><head><title>Workshop Error</title></head>'
                                        '<body><div class="errorPageBlock">x</div></body></html>'))
    finally:
        capture.configure(None)

    record = _only_capture_record(outbox, "web_unknown")
    assert record["shape"]["title_tag"] == "Workshop Error"
    assert record["shape"]["class_count"] == 1


# ── Translator worker ────────────────────────────────────────────────────────

def test_translator_thread_lifecycle(tmp_path):
    """Translator starts and stops cleanly."""
    from src.translator import TranslatorThread
    from src.database import initialize_database

    db_path = str(tmp_path / "test.db")
    initialize_database(db_path)
    config = {
        "database": {"path": db_path},
        "openai": {"api_key": "sk-test", "endpoint": "https://test/v1", "model": "gpt-test"},
    }
    thread = TranslatorThread(config)
    assert thread.running is True
    thread.running = False
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_a_downloaded_image_does_not_rewrite_the_scrape_version(db_path, tmp_path):
    """`scrape_version` is the revision the *page* was scraped at.

    The image worker wrote the item's `steam_updated_at` into it on every
    download, so an item whose page had never been scraped still claimed a
    scrape at its current revision. Nothing reads the column, which is why it
    went unnoticed -- but a column that cannot be trusted is worse than one that
    is absent, and the web worker is the writer that gives it its meaning.
    """
    from src.database import get_connection, insert_or_update_item

    # Already scraped, at a revision older than the one the worker will see.
    insert_or_update_item(db_path, {
        "workshop_id": 5, "scrape_version": 999, "needs_image": 1,
        "preview_url": "http://example.com/img.jpg", "steam_updated_at": 1,
    })

    _run_image_worker(db_path, response=_FakeImageResponse(
        status_code=200,
        headers={"Content-Type": "image/jpeg"},
        body=b"\xff\xd8\xff" + b"x" * 64,
    ), images_root=str(tmp_path / "images"))

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT scrape_version, image_extension FROM workshop_items WHERE workshop_id = 5"
        ).fetchone()
    finally:
        conn.close()

    assert row["image_extension"], "the download itself still succeeded"
    assert row["scrape_version"] == 999, \
        "an image download must leave the page's scrape revision alone"


# ── Web worker: the debug capture ---------------------------------------------
# The one web-download switch covers every Steam community pull. The item page is
# the caller in this module; the subscriptions page and the subscribe route have
# their own tests beside their callers.

def test_the_item_page_pull_is_captured_as_a_full_exchange(db_path, tmp_path):
    """The switch produces one item_page record, body on disk, no credential."""
    import json

    from src import capture
    from src.database import insert_or_update_item

    secret = "ITEM-PAGE-LOGIN-SECRET-0123456789"
    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5})
    page = {
        "description": "scraped text",
        "tags": [],
        "body": f'<html><div class="account_pulldown">{secret}</div></html>',
        "http_status": 200,
        "final_url": "https://steamcommunity.com/sharedfiles/filedetails/?id=1",
        "request": {
            "method": "GET",
            "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=1",
            "headers": {"User-Agent": "UA"},
            "cookies": {"steamLoginSecure": secret,
                        "browserid": "BROWSER-ID-0123456789"},
            "data": None,
        },
    }
    outbox = tmp_path / "outbox"
    capture.configure(str(outbox), capture_web_downloads=True)
    try:
        _run_web_worker(db_path, {"workshop_id": 1, "steam_updated_at": 1}, page)
    finally:
        capture.configure(None)

    records = [json.loads(path.read_text(encoding="utf-8"))
               for path in (outbox / "web_downloads").glob("*.json")]
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "item_page"
    assert record["workshop_id"] == 1
    assert record["request"]["cookies"] == {
        "steamLoginSecure": "***", "browserid": "***"}
    assert record["body_file"] is not None
    assert (outbox / record["body_file"]).exists(), "the response body is on disk"
    for path in outbox.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8", errors="replace")


