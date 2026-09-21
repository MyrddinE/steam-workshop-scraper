"""Issue 68: the cursor walk learns when it has run out of new items.

`seed_database`'s cursor loop had exactly two stops: `fill_target` new items, or
an empty `next_cursor`. Once the pages it walked were all already known neither
was reachable, so a pass paged an exhausted catalogue at ~2.3 pages a second
until the API refused, and the next pass resumed from the saved cursor and
repeated. The live log shows 13,487 / 1,724 / 5,624 / 6,964 pages each adding
zero items, at a page every `api_delay`, and `Cursor exhausted` was never
logged.

The rule now is the one the page-based mode already had, with a wider margin:
stop after five consecutive pages that add nothing, and record in
`app_discovery.cursor_walk_finished` that this AppID's walk is finished, so a
restart cannot re-enable the deep march. A page that adds anything resets the
count; the `fill_target` and API-error exits conclude nothing and must not mark
the walk finished; and the cursor itself is kept, because it records how far the
walk reached. The migration and the column's defaults are pinned separately in
`tests/test_cursor_walk_finished_migration.py`.
"""

import logging
import os
from unittest.mock import patch

from src import database
from src.daemon import Daemon
from src.database import (
    get_app_tracking,
    insert_or_update_item,
    update_app_tracking_cursor,
)
from tests.conftest import seed_pacing_delay

APPID = 431960
KNOWN_IDS = list(range(1, 101))       # every one already in the database
FILL_TARGET = 300
# The owner's chosen stop: five consecutive pages that add nothing. Pinned as a
# literal, not read back from the module, so the test states the rule.
STALL_PAGES = 5


def _config(db_path):
    # The low API delay is daemon state now, not a config key: seed it so the
    # walk's per-page pacing wait does not busy-wait the test.
    seed_pacing_delay(db_path, "api", 0.01)
    return {
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [APPID], "api_batch_size": 10},
    }


def _daemon(db_path):
    # `save_config` only writes config defaults; a test database does not need
    # the file written beside the checkout.
    with patch("src.daemon.save_config"):
        return Daemon(_config(db_path))


def _known(db_path, ids=KNOWN_IDS):
    """Seed items that the pager can hand back without them being new."""
    for wid in ids:
        insert_or_update_item(db_path, {"workshop_id": wid, "api_priority": 0})


def _pager(script):
    """A fake `query_workshop_newest_page` over a scripted list of pages.

    Each entry is ``"empty"`` (a page of already-known ids), an int N (a page
    carrying N fresh ids), or ``"failed"``. A script that runs out is followed
    by a refusal, so an unbounded loop terminates in a visible API error instead
    of hanging the test.
    """
    calls = {"n": 0}

    def query(appid, cursor=None, api_key=None, keep_running=None):
        index = calls["n"]
        calls["n"] += 1
        entry = script[index] if index < len(script) else "failed"
        if entry == "failed":
            return {"failed": "rate limited"}
        if entry == "empty":
            ids = KNOWN_IDS
        else:
            ids = [900_000 + index * 100 + offset for offset in range(entry)]
        return {
            "total": 1_000_000,
            "items": [{"publishedfileid": str(wid)} for wid in ids],
            "next_cursor": f"cursor-{index + 1}",
        }

    return query, calls


def _finished(db_path):
    """The persisted latch as the rest of the system reads it."""
    return get_app_tracking(db_path, APPID)["cursor_walk_finished"]


# --- the stop rule ----------------------------------------------------------

@patch("src.daemon.time.sleep")
def test_a_walk_of_empty_pages_stops_after_five_and_records_it(
        mock_sleep, db_path, caplog):
    """The defect itself: all-known pages must not be paged indefinitely."""
    _known(db_path)
    update_app_tracking_cursor(db_path, APPID, "saved")
    query, calls = _pager(["empty"] * 8)

    daemon = _daemon(db_path)
    with caplog.at_level(logging.WARNING), \
         patch("src.daemon.query_workshop_newest_page", side_effect=query):
        daemon.seed_database(fill_target=FILL_TARGET)

    assert calls["n"] == STALL_PAGES == 5, (
        "five consecutive empty pages must end the walk, not the API")
    assert _finished(db_path) == 1, "the finished state must be recorded"
    # The cursor is the record of where the walk reached; it is kept.
    assert get_app_tracking(db_path, APPID)["last_cursor"] == "cursor-5"
    assert str(APPID) in caplog.text and "stalled" in caplog.text.lower(), \
        "the stall is logged and names the AppID"


@patch("src.daemon.time.sleep")
def test_a_page_that_adds_items_resets_the_stall_counter(mock_sleep, db_path):
    """A stall is five *in a row*: four empty pages plus a page with one new
    item, then five more empty pages, stops on the tenth request.

    A counter that never reset would have stopped on the sixth.
    """
    _known(db_path)
    query, calls = _pager(
        ["empty", "empty", "empty", "empty", 1, "empty", "empty", "empty",
         "empty", "empty"])

    daemon = _daemon(db_path)
    with patch("src.daemon.query_workshop_newest_page", side_effect=query):
        daemon.seed_database(fill_target=FILL_TARGET)

    assert calls["n"] == 10, "the page with a new item reset the consecutive count"
    assert _finished(db_path) == 1


@patch("src.daemon.time.sleep")
def test_the_fill_target_exit_does_not_mark_the_walk_finished(mock_sleep, db_path):
    """Reaching `fill_target` is the healthy path: more items remain, we simply
    have enough, so it must not conclude the catalogue is exhausted."""
    query, calls = _pager([5])

    daemon = _daemon(db_path)
    with patch("src.daemon.query_workshop_newest_page", side_effect=query):
        daemon.seed_database(fill_target=3)

    assert calls["n"] == 1
    assert _finished(db_path) == 0, "the healthy exit must not mark the walk finished"
    assert daemon._cursor_exhausted is False


@patch("src.daemon.time.sleep")
def test_an_api_error_does_not_mark_the_walk_finished(mock_sleep, db_path):
    """A refusal ends the pass without concluding anything about the catalogue,
    so it must not record a finished walk even after four empty pages."""
    _known(db_path)
    query, calls = _pager(["empty", "empty", "empty", "empty", "failed"])

    daemon = _daemon(db_path)
    with patch("src.daemon.query_workshop_newest_page", side_effect=query):
        daemon.seed_database(fill_target=FILL_TARGET)

    assert calls["n"] == 5
    assert _finished(db_path) == 0
    assert daemon._cursor_exhausted is False


# --- the persisted state governs the next run -------------------------------

@patch("src.daemon.time.sleep")
def test_a_finished_walk_is_not_resumed_after_a_restart(mock_sleep, db_path):
    """A fresh `Daemon` must not re-request a cursor page for a finished AppID."""
    _known(db_path)
    update_app_tracking_cursor(db_path, APPID, "saved")
    database.mark_cursor_walk_finished(db_path, APPID)
    query, calls = _pager(["empty"])

    restarted = _daemon(db_path)
    with patch("src.daemon.query_workshop_newest_page", side_effect=query):
        restarted.seed_database(fill_target=FILL_TARGET)

    assert calls["n"] == 0, "no cursor request for a finished walk"
    assert get_app_tracking(db_path, APPID)["last_cursor"] == "saved", \
        "the cursor is the record of where the walk reached; it is not reset"


def test_page_discovery_is_eligible_once_a_walk_is_finished_and_survives_a_restart(db_path):
    """Eligibility is derived from the persisted state, not only from the
    in-memory `_cursor_exhausted`, which a restart clears."""
    # No items are scraped, so the `scraped >= 500` clause cannot be the reason,
    # and no `.fetch_new` trigger file exists.
    assert not os.path.exists(".fetch_new")
    daemon = _daemon(db_path)
    assert daemon._cursor_exhausted is False
    assert daemon._page_discovery_eligible() is False

    database.mark_cursor_walk_finished(db_path, APPID)
    assert daemon._page_discovery_eligible() is True, \
        "the same pass sees the persisted state"

    restarted = _daemon(db_path)
    assert restarted._cursor_exhausted is False, "the in-memory flag starts clear"
    assert restarted._page_discovery_eligible() is True, \
        "a restart cannot re-enable the deep march"


# --- the skip is a one-time fact, not per-pass news (issue 72) ---------------

def _skip_records(caplog):
    """The finished-walk skip lines, at whatever level, for this AppID."""
    return [r for r in caplog.records
            if str(APPID) in r.getMessage()
            and "recorded as finished" in r.getMessage()]


def test_a_finished_walk_is_announced_once_per_process_then_debug(db_path, caplog):
    """Issue 72: the latch is permanent, so the skip is INFO once and DEBUG
    afterwards, and a freshly constructed `Daemon` announces it once again.

    The discovery thread runs `seed_database` every 30 s, so an INFO line per
    pass reported a static fact as news forever, in a log that already grows
    about 115 MB a day.
    """
    database.mark_cursor_walk_finished(db_path, APPID)
    daemon = _daemon(db_path)

    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        daemon.seed_database(fill_target=FILL_TARGET)
        first = _skip_records(caplog)

        caplog.clear()
        daemon.seed_database(fill_target=FILL_TARGET)
        second = _skip_records(caplog)

    assert [r.levelno for r in first] == [logging.INFO], (
        "the first pass announces the finished walk exactly once, at INFO")
    assert [r.levelno for r in second] == [logging.DEBUG], (
        "the second pass repeats the permanent fact at DEBUG, not INFO")
    info = first[0].getMessage()
    assert "page-based updated-order scan" in info and "24 hours" in info, (
        "the single INFO line stands alone: it names where new items now come "
        "from and at most how often, without over-claiming")

    # Logging state is per process, not persisted: a fresh `Daemon` reports it
    # once more rather than staying silent.
    restarted = _daemon(db_path)
    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        restarted.seed_database(fill_target=FILL_TARGET)
        after_restart = _skip_records(caplog)

    assert [r.levelno for r in after_restart] == [logging.INFO], (
        "a freshly constructed Daemon reports the fact again")
    assert daemon._cursor_walk_finished_reported == {APPID}
    assert restarted._cursor_walk_finished_reported == {APPID}
