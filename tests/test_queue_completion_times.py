"""Migration 26->27: the per-queue completion clocks, and the metrics that read them.

Throughput, burn-down and ETA need to know when *we* finished a stage. The web
and translation stages recorded Steam's revision instead of a time, and the image
stage recorded nothing, so migration 26->27 adds one wall-clock completion column
per stage. What is pinned here is what the schema decision rests on:

* each stage stamps our clock at its success point, and stamps nothing on a
  failure or a partial stage;
* a stamp is taken when the stage completes, not when it started -- checked by
  moving the clock while the work is in flight;
* historical rows stay NULL, are not backfilled, and are not counted by the
  metrics;
* a queue with no stamps reports "no history yet" rather than a zero, and a
  stamped-but-stale queue reports a measured zero -- the two are different
  answers;
* the migration is additive and idempotent.

The three columns did not exist before this change, so the migration cases
cannot be shown failing against the old behaviour: the old schema simply had no
such column. They lean on the positive case (the columns and their indexes exist
after the chain runs) and the negative one (a row that never completed a stage
keeps NULL).

The SQL the throughput metric runs is captured from the real function and pinned
with `EXPLAIN QUERY PLAN`, as `tests/test_queue_indexes.py` does for the worker
polls: a query rewritten so it no longer matches its partial index fails here
rather than silently scanning 2.6M rows.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from src import database, metrics
from src.daemon import MERGE_EXCLUDED_KEYS, MERGE_ITEM_KEYS
from src.database import (
    EXPECTED_VERSION,
    WORKSHOP_ITEM_COLUMNS,
    queue_field_for_translation,
    get_connection,
    initialize_database,
    insert_or_update_item,
)

COMPLETION_COLUMNS = ("web_scraped_at", "image_fetched_at", "translated_at")
COMPLETION_INDEXES = tuple(f"idx_{column}" for column in COMPLETION_COLUMNS)
QUEUE_METRIC = {
    "web_scraped_at": "web_throughput",
    "image_fetched_at": "image_throughput",
    "translated_at": "translation_throughput",
}

# The outcome fixtures the web worker classifies, copied from the cases in
# tests/test_workers.py so this file reads independently.
FOUND = {"description": "scraped text", "tags": [], "body": None,
         "http_status": 200, "final_url": "https://example.invalid/?id=1"}
MISS = {"description": None, "tags": [], "body": "<html>no selector</html>",
        "http_status": 200, "final_url": "https://example.invalid/?id=1"}
ITEM_PAGE_WITHOUT_DESCRIPTION = dict(
    MISS, body='<html><div class="workshopItem">x</div></html>')
MISSING_ITEM_PAGE = dict(
    MISS, body='<title>Steam Community :: Error</title>'
               '<h3>That item does not exist.  It may have been removed by the author.</h3>')


# --------------------------------------------------------------------------
# schema and storage helpers
# --------------------------------------------------------------------------


def _stored(db_path, workshop_id, column):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT {column} FROM workshop_items WHERE workshop_id = ?",
            (workshop_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _columns_and_version(db_path):
    conn = get_connection(db_path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workshop_items)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    return columns, version


def _index_sql(db_path) -> dict[str, str]:
    conn = get_connection(db_path)
    try:
        return {
            row["name"]: row["sql"]
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND tbl_name='workshop_items'"
            )
        }
    finally:
        conn.close()


def _plan(db_path, sql: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    finally:
        conn.close()
    return " | ".join(row[3] for row in rows)


def _regress_to_v26(db_path):
    """Undo migration 26->27: drop the indexes and the columns, lower the version.

    This is the base the migration is required to upgrade from. The indexes go
    first because SQLite refuses to drop a column a partial index names.
    """
    conn = get_connection(db_path)
    for name in COMPLETION_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    for column in COMPLETION_COLUMNS:
        conn.execute(f"ALTER TABLE workshop_items DROP COLUMN {column}")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------
# the migration
# --------------------------------------------------------------------------


def test_a_fresh_database_carries_the_three_completion_clocks(db_path):
    columns, version = _columns_and_version(db_path)
    assert EXPECTED_VERSION >= 27, "the clocks are migration 26->27"
    assert version == EXPECTED_VERSION
    assert set(COMPLETION_COLUMNS) <= columns
    assert set(COMPLETION_INDEXES) <= set(_index_sql(db_path))


def test_upgrading_a_v26_database_adds_the_clocks_additively(db_path):
    _regress_to_v26(db_path)
    columns, version = _columns_and_version(db_path)
    assert version == 26
    assert not (set(COMPLETION_COLUMNS) & columns), "the base must not have them"

    initialize_database(db_path)

    columns, version = _columns_and_version(db_path)
    assert version == EXPECTED_VERSION
    assert set(COMPLETION_COLUMNS) <= columns
    assert set(COMPLETION_INDEXES) <= set(_index_sql(db_path))


def test_the_migration_backfills_nothing_on_a_pre_existing_row(db_path):
    """Historical rows keep NULL: the time was never recorded and cannot be invented."""
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "old", "status": 200,
                                    "steam_updated_at": 1})

    _regress_to_v26(db_path)
    initialize_database(db_path)

    for column in COMPLETION_COLUMNS:
        assert _stored(db_path, 1, column) is None, (
            f"{column} must stay NULL for a row that completed before the column"
        )


def test_the_migration_is_idempotent_on_a_database_that_already_has_the_columns(db_path):
    _regress_to_v26(db_path)
    initialize_database(db_path)
    initialize_database(db_path)

    columns, version = _columns_and_version(db_path)
    indexes = _index_sql(db_path)
    assert version == EXPECTED_VERSION
    for column in COMPLETION_COLUMNS:
        assert column in columns
        assert f"idx_{column}" in indexes


@pytest.mark.parametrize("column", COMPLETION_COLUMNS)
def test_each_index_is_partial_on_its_own_stamp(db_path, column):
    """The `IS NOT NULL` predicate keeps the index empty until completions exist."""
    sql = _index_sql(db_path)[f"idx_{column}"]
    assert f"({column})" in sql
    assert f"WHERE {column} IS NOT NULL" in sql


def test_the_clocks_are_real_columns_but_never_api_merge_keys(db_path):
    """They are our clock, so a Steam payload must not be able to carry one."""
    for column in COMPLETION_COLUMNS:
        assert column in WORKSHOP_ITEM_COLUMNS
        assert column in MERGE_EXCLUDED_KEYS
        assert column not in MERGE_ITEM_KEYS


# --------------------------------------------------------------------------
# the metric: no history vs a measured zero
# --------------------------------------------------------------------------


@pytest.mark.parametrize("column", COMPLETION_COLUMNS)
def test_a_queue_with_no_stamps_reports_no_history(db_path, column):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "x", "status": 200})

    value = metrics.values(
        metrics.compute(db_path, [QUEUE_METRIC[column]]))[QUEUE_METRIC[column]]

    assert value == {"hour": None, "day": None, "last_success": None}


def test_a_stamped_but_stale_queue_reports_a_measured_zero(db_path):
    """Once one stamp exists a 0 is an answer, not missing data."""
    now = int(time.time())
    insert_or_update_item(db_path, {"workshop_id": 1, "status": 200,
                                    "web_scraped_at": now - 3 * 86400})

    value = metrics.values(
        metrics.compute(db_path, ["web_throughput"]))["web_throughput"]

    assert value["hour"] == 0
    assert value["day"] == 0
    assert value["last_success"] == now - 3 * 86400


@pytest.mark.parametrize("column", COMPLETION_COLUMNS)
def test_throughput_counts_only_rows_inside_the_window_and_skips_nulls(db_path, column):
    now = int(time.time())
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "in-hour",
                                    "status": 200, column: now - 60})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "in-day",
                                    "status": 200, column: now - 2 * 3600})
    insert_or_update_item(db_path, {"workshop_id": 3, "title": "older-than-a-day",
                                    "status": 200, column: now - 3 * 86400})
    # The historical row: the stage ran before the column existed, so it holds
    # NULL and must not be counted in either window.
    insert_or_update_item(db_path, {"workshop_id": 4, "title": "historical",
                                    "status": 200})

    value = metrics.values(
        metrics.compute(db_path, [QUEUE_METRIC[column]]))[QUEUE_METRIC[column]]

    assert value["hour"] == 1
    assert value["day"] == 2
    assert value["last_success"] == now - 60


@pytest.mark.parametrize("column", COMPLETION_COLUMNS)
def test_the_throughput_metric_reads_its_partial_index(db_path, column):
    """The plan the metric runs must not scan the item table."""
    conn = get_connection(db_path)
    conn.executemany(
        "INSERT INTO workshop_items (workshop_id, status) VALUES (?, 200)",
        [(i,) for i in range(1, 401)],
    )
    conn.commit()
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        metrics._completion_window(conn, column)
    finally:
        conn.close()

    sql = next(s for s in seen if "AS last_success" in s)
    plan = _plan(db_path, sql)
    assert f"USING COVERING INDEX idx_{column}" in plan, plan
    assert "SCAN workshop_items" not in plan, plan


# --------------------------------------------------------------------------
# the web worker
# --------------------------------------------------------------------------


def _run_web_stage(db_path, item, response, on_request=None):
    """Run the real worker over exactly one item, then stop it.

    ``on_request`` runs at the moment the page would be fetched, so a test can
    move a clock while the work is in flight.
    """
    from src.web_worker import WebScraperThread

    worker = WebScraperThread(db_path, ".pauselock")
    served = 0

    def next_item(*args, **kwargs):
        nonlocal served
        served += 1
        if served > 1:
            worker.running = False
            return None
        return item

    def scrape(*args, **kwargs):
        if on_request is not None:
            on_request()
        return response

    with patch("src.web_worker.get_next_web_scrape_item", side_effect=next_item), \
         patch("src.web_worker.scrape_extended_details", side_effect=scrape), \
         patch("time.sleep"), patch("src.pacing.wait"):
        worker.start()
        worker.join(timeout=5)


def test_a_web_scrape_success_stamps_our_clock_not_the_steam_version(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5,
                                    "steam_updated_at": 123456})

    before = int(time.time())
    _run_web_stage(db_path, {"workshop_id": 1, "steam_updated_at": 123456}, FOUND)
    after = int(time.time())

    stamp = _stored(db_path, 1, "web_scraped_at")
    assert stamp is not None
    assert before <= stamp <= after
    assert stamp != 123456, "the completion clock is ours, not scrape_version"
    assert _stored(db_path, 1, "scrape_version") == 123456


def test_the_web_stamp_is_taken_when_the_scrape_completes_not_when_it_started(db_path, monkeypatch):
    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5,
                                    "steam_updated_at": 1})
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])

    _run_web_stage(db_path, {"workshop_id": 1, "steam_updated_at": 1}, FOUND,
                   on_request=lambda: clock.update(now=2000.0))

    assert _stored(db_path, 1, "web_scraped_at") == 2000


@pytest.mark.parametrize("response", [MISS, ITEM_PAGE_WITHOUT_DESCRIPTION,
                                      MISSING_ITEM_PAGE, None])
def test_a_web_scrape_that_is_not_a_success_leaves_the_stamp_alone(db_path, response):
    """A miss, a wall and a transport failure all keep the previous completion."""
    insert_or_update_item(db_path, {"workshop_id": 1, "needs_web_scrape": 5,
                                    "steam_updated_at": 1, "web_scraped_at": 777})

    _run_web_stage(db_path, {"workshop_id": 1, "steam_updated_at": 1}, response)

    assert _stored(db_path, 1, "web_scraped_at") == 777


# --------------------------------------------------------------------------
# the image worker
# --------------------------------------------------------------------------


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


def _run_image_stage(db_path, response=None, error=None, images_root=None,
                     on_request=None):
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

    def request(*args, **kwargs):
        if on_request is not None:
            on_request()
        if error is not None:
            raise error
        return response

    with ExitStack() as stack:
        stack.enter_context(patch("src.image_worker.get_next_image_item",
                                  side_effect=next_item))
        stack.enter_context(patch("src.image_worker.requests.get",
                                  side_effect=request))
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


def test_an_image_success_stamps_our_clock(db_path, tmp_path):
    response = _FakeImageResponse(headers={"Content-Type": "image/jpeg"},
                                  body=b"\xff\xd8\xff\xe0" + b"jpeg" * 8)

    before = int(time.time())
    _run_image_stage(db_path, response=response,
                     images_root=str(tmp_path / "images"))
    after = int(time.time())

    stamp = _stored(db_path, 5, "image_fetched_at")
    assert stamp is not None
    assert before <= stamp <= after


def test_the_image_stamp_is_taken_after_the_bytes_are_fetched(db_path, tmp_path, monkeypatch):
    response = _FakeImageResponse(headers={"Content-Type": "image/jpeg"},
                                  body=b"\xff\xd8\xff\xe0" + b"jpeg" * 8)
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])

    _run_image_stage(db_path, response=response,
                     images_root=str(tmp_path / "images"),
                     on_request=lambda: clock.update(now=2000.0))

    assert _stored(db_path, 5, "image_fetched_at") == 2000


def test_a_served_answer_that_is_not_a_picture_does_not_stamp(db_path, tmp_path):
    """An unclassifiable content type clears the queue but is not a completion."""
    insert_or_update_item(db_path, {"workshop_id": 5, "needs_image": 1,
                                    "preview_url": "http://example.com/img.jpg",
                                    "image_fetched_at": 777})
    response = _FakeImageResponse(headers={"Content-Type": "text/html"},
                                  body=b"<html>not an image</html>")

    _run_image_stage(db_path, response=response,
                     images_root=str(tmp_path / "images"))

    assert _stored(db_path, 5, "needs_image") == 0, "the item still leaves the queue"
    assert _stored(db_path, 5, "image_fetched_at") == 777


def test_a_permanent_image_failure_does_not_stamp(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 5, "needs_image": 1,
                                    "preview_url": "http://example.com/img.jpg",
                                    "image_fetched_at": 777})
    response = _FakeImageResponse(status_code=404, body=b"")

    _run_image_stage(db_path, response=response,
                     images_root=str(tmp_path / "images"))

    assert _stored(db_path, 5, "image_fetched_at") == 777


def test_a_transport_failure_does_not_stamp(db_path, tmp_path):
    insert_or_update_item(db_path, {"workshop_id": 5, "needs_image": 1,
                                    "preview_url": "http://example.com/img.jpg",
                                    "image_fetched_at": 777})

    _run_image_stage(db_path, error=Exception("Connection refused"),
                     images_root=str(tmp_path / "images"))

    assert _stored(db_path, 5, "image_fetched_at") == 777


# --------------------------------------------------------------------------
# the translator
# --------------------------------------------------------------------------


def _translate(db_path, item_id, fields, returned, on_call=None):
    """Drive the real `_translate_batch` with a fake OpenAI client.

    ``fields`` is the queue's field list and ``returned`` the subset the model
    answers, so a partial batch can be simulated. The boundary phrase is drawn per
    request, so it is held still here and the reply is written in the shape the
    request asks for.
    """
    from src.translator import TranslatorThread, field_label

    config = {
        "database": {"path": db_path},
        "openai": {"api_key": "SK-TEST", "endpoint": "https://test/v1",
                   "model": "gpt-test"},
    }
    thread = TranslatorThread(config)
    thread.db_path = db_path

    phrase = "goat smelt bob and"
    payload = "\n".join(
        f"{phrase} {item_id} {field_label(field)}\nEN {field}" for field in returned
    )
    client = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = payload
    if on_call is None:
        client.chat.completions.create.return_value = response
    else:
        def create(**_kwargs):
            on_call()
            return response
        client.chat.completions.create.side_effect = create

    batch = [
        {"id": index + 1, "item_type": "item", "item_id": item_id,
         "field": field, "original_text": "テキスト", "priority": 10}
        for index, field in enumerate(fields)
    ]
    with patch("src.translator.choose_phrase", return_value=phrase):
        thread._translate_batch(batch, client, "gpt-test")


def test_the_translator_stamps_our_clock_when_the_items_last_field_completes(db_path, monkeypatch):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テキスト",
                                    "status": 200, "steam_updated_at": 1710000000})
    queue_field_for_translation(db_path, "item", 1, "title_en", "テキスト", 10)

    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    _translate(db_path, 1, ["title_en"], ["title_en"],
               on_call=lambda: clock.update(now=2000.0))

    assert _stored(db_path, 1, "translation_priority") == 0
    assert _stored(db_path, 1, "translated_at") == 2000, \
        "our clock, taken when the last field completed"
    assert _stored(db_path, 1, "translate_version") == 1710000000, \
        "translate_version still records Steam's revision"


def test_a_partial_translation_does_not_stamp(db_path):
    """One field written while another is still queued is not a completion."""
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テキスト",
                                    "short_description": "テキスト", "status": 200,
                                    "steam_updated_at": 1710000000})
    queue_field_for_translation(db_path, "item", 1, "title_en", "テキスト", 10)
    queue_field_for_translation(db_path, "item", 1, "short_description_en", "テキスト", 10)

    _translate(db_path, 1, ["title_en", "short_description_en"], ["title_en"])

    assert _stored(db_path, 1, "title_en") == "EN title_en"
    assert _stored(db_path, 1, "translation_priority") > 0, "a field is still queued"
    assert _stored(db_path, 1, "translated_at") is None


def test_a_reply_with_no_text_is_a_failure_and_does_not_stamp(db_path):
    """A reply that translates nothing is a failed request, not an empty success.

    It has to raise: treating it as success would have the loop re-send the same
    batch immediately, which is the tight loop the backoff exists to stop. The
    field itself is untouched -- still queued, nothing stamped.
    """
    from src.translator import TranslationResponseError

    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テキスト",
                                    "status": 200, "steam_updated_at": 1710000000})
    queue_field_for_translation(db_path, "item", 1, "title_en", "テキスト", 10)

    with pytest.raises(TranslationResponseError):
        _translate(db_path, 1, ["title_en"], [])

    assert _stored(db_path, 1, "translated_at") is None
    assert _stored(db_path, 1, "translation_priority") == 10, \
        "the queue row stays, so the item is still queued"
