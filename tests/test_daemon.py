import pytest
from unittest.mock import patch, MagicMock, ANY
import signal
import json
import time
from src import pacing
from src.daemon import Daemon, STALE_SWEEP_INTERVAL_SECONDS, API_DELAY_FLOOR
from src.database import get_app_tracking, initialize_database, update_app_tracking

@pytest.fixture
def mock_config(db_path):
    return {
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"api_batch_size": 2, "request_delay_seconds": 0.01, "target_appids": [123]}
    }

def test_daemon_init_defaults(tmp_path, monkeypatch):
    """Test that the Daemon correctly applies fallback defaults for missing config keys.

    The default database path is relative, so run this in a temporary directory
    and initialise that path: the assertion is about the default's *name*, and
    the constructor reads app_tracking on the way past.
    """
    monkeypatch.chdir(tmp_path)
    initialize_database("workshop.db")
    # Provide minimal valid config (only target_appids is strictly required now)
    minimal_config = {"daemon": {"target_appids": [456]}}
    daemon = Daemon(minimal_config)
    
    assert daemon.db_path == "workshop.db"
    assert daemon.api_key == ""
    assert daemon.api_batch_size == 10
    assert daemon.api_delay == 1.5
    assert daemon.item_staleness_days == 30
    assert daemon.creator_staleness_days == 90
    assert daemon.target_appids == [456]


def test_daemon_carries_no_dead_filter_state(tmp_path, monkeypatch):
    """``last_filters`` held only a cursor the live code reads from the row."""
    monkeypatch.chdir(tmp_path)
    initialize_database("workshop.db")
    daemon = Daemon({"daemon": {"target_appids": [456]}})
    assert not hasattr(daemon, "last_filters")

def test_daemon_init_missing_appids():
    """Test that the Daemon raises a ValueError if target_appids is omitted."""
    empty_config = {}
    with pytest.raises(ValueError, match="must be provided as a list"):
        Daemon(empty_config)
        
    invalid_config = {"daemon": {"target_appids": "not_a_list_just_a_string"}}
    with pytest.raises(ValueError, match="must be provided as a list"):
        Daemon(invalid_config)

@patch('src.daemon.count_never_fetched_items')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.get_workshop_details_batch')
@patch('src.daemon.get_creator')
@patch('src.daemon.insert_or_update_creator')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.raise_web_scrape_priority')
@patch('time.sleep')
def test_daemon_process_batch_success(mock_sleep, mock_flag_web, mock_insert, mock_insert_creator, mock_get_creator, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 123}]
    mock_api.return_value = {123: {"title": "Test Mod", "creator": "111", "status": 200}}
    mock_get_creator.return_value = {"steamid": 111, "api_fetched_at": 1767225600}

    daemon = Daemon(mock_config)
    daemon.process_batch()

    mock_get_items.assert_called_once_with(mock_config["database"]["path"], limit=2)
    # One request carrying the whole batch, not one call per item.
    mock_api.assert_called_once_with([123], "TEST_KEY")
    mock_flag_web.assert_called_once_with(mock_config["database"]["path"], 123, 3)

    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["workshop_id"] == 123
    assert inserted_data["title"] == "Test Mod"

@patch('src.daemon.count_never_fetched_items')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.get_workshop_details_batch')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_daemon_process_batch_api_failure(mock_sleep, mock_insert, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 456}]
    # None is a request-level failure; every id it carried settles as a 500.
    mock_api.return_value = None

    daemon = Daemon(mock_config)

    daemon.process_batch()
    
    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["status"] == 500

def test_daemon_graceful_shutdown(mock_config):
    daemon = Daemon(mock_config)
    daemon.handle_shutdown(signal.SIGINT, None)
    assert daemon.running is False

@patch('src.daemon.Daemon.process_batch')
def test_daemon_run_loop(mock_process, mock_config):
    daemon = Daemon(mock_config)
    def fake_process_batch():
        daemon.running = False
    mock_process.side_effect = fake_process_batch
    daemon.run()
    mock_process.assert_called_once()

@patch('src.daemon.count_never_fetched_items')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.Daemon.seed_database')
@patch('time.sleep')
def test_daemon_process_batch_empty(mock_sleep, mock_seed, mock_get_items, mock_count, mock_config):
    """Test behavior when no items are returned from the queue (triggers seeding)."""
    # First call returns empty, second call (after seed) also returns empty
    mock_count.return_value = 1000
    mock_get_items.return_value = []
    
    daemon = Daemon(mock_config)
    daemon.process_batch()
    
    # Should sleep in 1-second checks for pollable shutdown (600 checks)
    mock_sleep.assert_called_with(1)
    assert mock_sleep.call_count == 600

@patch('src.daemon.count_never_fetched_items')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.get_workshop_details_batch')
def test_daemon_process_batch_exit_early(mock_api, mock_get_items, mock_count, mock_config):
    """Test behavior when shutdown signal is received mid-batch."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 1}, {'workshop_id': 2}, {'workshop_id': 3}]
    daemon = Daemon(mock_config)
    daemon.running = False # Simulate shutdown right before processing
    daemon.process_batch()
    mock_api.assert_not_called()

@patch('src.daemon.count_never_fetched_items')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.get_workshop_details_batch')
@patch('src.daemon.get_creator')
@patch('src.daemon.insert_or_update_creator')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.raise_web_scrape_priority')
@patch('time.sleep')
def test_api_delay_decays_on_every_healthy_request(mock_sleep, mock_flag_web, mock_insert, mock_insert_creator, mock_get_creator, mock_api, mock_get_items, mock_count, mock_config):
    """Every healthy request shaves one step off the delay.

    One request carries the whole batch, so a batch of five successful items is
    one decay step, not five.
    """
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': i} for i in range(5)]
    # A fresh dict per call: _merge_and_clean_api_data pops keys off the
    # response, so a shared mock object loses its "status" after the first item.
    mock_api.side_effect = lambda ids, key: {
        i: {"title": "Mod", "creator": "111", "status": 200} for i in ids}
    mock_get_creator.return_value = {"steamid": 111, "api_fetched_at": 1767225600}

    daemon = Daemon(mock_config)
    daemon.api_batch_size = 5
    daemon.api_delay = 1.0
    # The batch is one request and therefore one interval; pretend the interval
    # lasted a half-life.
    daemon._api_clock._at = pacing.now() - pacing.HALF_LIFE_SECONDS
    daemon.process_batch()
    assert daemon.api_delay == pytest.approx(0.5, rel=1e-4), \
        "a half-life of healthy operation halves the delay, whatever the call count"

@patch('src.daemon.count_never_fetched_items')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.get_workshop_details_batch')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_api_delay_multiplies_on_every_refused_request(mock_sleep, mock_insert, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 11}, {'workshop_id': 12}]
    mock_api.return_value = None  # the request itself failed

    daemon = Daemon(mock_config)
    daemon.api_delay = 0.25

    daemon.process_batch()
    # Two items failed, but that is one refused request: one doubling.
    assert daemon.api_delay == 0.5
    assert daemon.api_failures == 1

    daemon.process_batch()
    assert daemon.api_delay == 1.0, "a sustained outage keeps multiplying"


def test_the_api_delay_doubles_past_the_old_ceiling_and_still_floors(db_path, tmp_path):
    """A refusal doubles the delay, and nothing clips it from above any more.

    The 2 s ceiling was a defence against a delay moved by the wrong signal, but
    it also stopped the client ever reaching a sustainable rate above it. An
    uncapped delay cannot run away: it only doubles when an attempt fails, and
    the next attempt is a whole delay away, so it tracks the outage rather than
    outrunning it.
    """
    daemon = _real_db_daemon(db_path, tmp_path)

    daemon.api_delay = 2.0
    daemon._back_off_api_delay()
    assert daemon.api_delay == 4.0, "the delay doubles past the old 2 s ceiling"

    daemon.api_delay = 0.01
    ticks = iter(range(0, 100_000, 600))
    with patch("src.pacing.now", side_effect=lambda: next(ticks)):
        daemon._api_clock = pacing.Clock()
        daemon._decay_api_delay()
    assert daemon.api_delay == 0.01, "a zero delay is not a rate limit"


def test_the_delay_settles_around_a_refusal_threshold(db_path, tmp_path):
    """Started below the limit, the loop refuses its way up and then hovers.

    The clock is driven forward by one delay per call, because the delay *is*
    the interval -- that is what the wall clock does. The probe back down is
    deliberately slow: taking a doubling back costs `HALF_LIFE_SECONDS` of
    healthy operation, so this runs a long simulated period rather than a few
    hundred calls. That slowness is the trade the API makes for being the most
    reliable of the queues.
    """
    daemon = _real_db_daemon(db_path, tmp_path)
    daemon.api_delay = 0.01
    limit = 0.32
    simulated = {"t": 0.0}
    refusals = 0

    with patch("src.pacing.now", side_effect=lambda: simulated["t"]):
        daemon._api_clock = pacing.Clock()
        for _ in range(6000):
            simulated["t"] += daemon.api_delay
            if daemon.api_delay < limit:
                daemon._back_off_api_delay()
                refusals += 1
            else:
                daemon._decay_api_delay()

    assert refusals > 0, "it must have probed into the limit at least once"
    assert refusals < 100, "and must back off rather than keep knocking"
    assert limit / 4 <= daemon.api_delay <= limit * 2, (
        f"delay settled at {daemon.api_delay}, nowhere near the {limit}s threshold")


def test_mixed_outcome_batch_is_a_healthy_request_not_a_refusal(db_path, tmp_path):
    """A returned-and-parsed request is a success even with not-found items.

    The API did its job; the individual 404s are item state, not pacing. Start
    at the floor so the single decay step is a no-op and the assertion isolates
    "no back-off" from the decay.
    """
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 5, "status": 200})
    insert_or_update_item(db_path, {"workshop_id": 2, "api_priority": 5, "status": 200})
    daemon = _real_db_daemon(db_path, tmp_path)
    daemon.api_batch_size = 2
    daemon.api_delay = API_DELAY_FLOOR
    initial = daemon.api_delay

    with patch("src.daemon.get_next_items_to_fetch", return_value=[
            {"workshop_id": 1, "api_priority": 5, "status": 200},
            {"workshop_id": 2, "api_priority": 5, "status": 200}]), \
         patch("src.daemon.get_workshop_details_batch", return_value={
             1: {"title": "still here", "status": 200},
             2: {"status": 404, "publishedfileid": 2},
         }):
        daemon.process_batch()

    assert daemon.api_delay == initial, "mixed per-item results must not back the delay off"
    assert daemon.api_failures == 0
    assert daemon.api_successes == 1

    conn = get_connection(db_path)
    rows = {r["workshop_id"]: dict(r)
            for r in conn.execute("SELECT * FROM workshop_items")}
    conn.close()
    assert rows[1]["status"] == 200
    assert rows[2]["status"] == -1, "the not-found item is still marked dead"
    assert rows[2]["api_priority"] == 0


def test_request_failure_multiplies_delay_and_success_decays_it(db_path, tmp_path):
    daemon = _real_db_daemon(db_path, tmp_path)
    daemon.api_delay = 0.25

    daemon._back_off_api_delay()
    assert daemon.api_delay == 0.5
    assert daemon.api_failures == 1

    # A half-life of healthy operation is exactly what takes back one doubling.
    daemon._api_clock._at = pacing.now() - pacing.HALF_LIFE_SECONDS
    daemon._decay_api_delay()
    assert daemon.api_delay == pytest.approx(0.25, rel=1e-4), \
        "a half-life of healthy operation undoes a doubling"
    assert daemon.api_failures == 0, "a healthy request resets the failure streak"


def test_page_discovery_not_eligible_initially(mock_config):
    from src.daemon import Daemon
    with patch('src.database.initialize_database'), \
         patch('src.daemon.save_config'):
        daemon = Daemon(mock_config)
        daemon._cursor_exhausted = False
        assert daemon._page_discovery_eligible() is False


def test_page_discovery_eligible_by_cursor(mock_config):
    from src.daemon import Daemon
    with patch('src.database.initialize_database'), \
         patch('src.daemon.save_config'):
        daemon = Daemon(mock_config)
        daemon._cursor_exhausted = True
        assert daemon._page_discovery_eligible() is True


def test_wilson_lower_edge_cases():
    from src.daemon import wilson_lower
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(0, 10) == 0.0
    assert 0.0 <= wilson_lower(5, 10) <= 1.0
    assert 0.0 <= wilson_lower(100, 100) <= 1.0


def test_merge_and_clean_sets_api_priority_zero(mock_config):
    """_merge_and_clean_api_data sets api_priority=0 and api_fetched_at."""
    with patch('src.database.initialize_database'), \
         patch('src.daemon.save_config'):
        daemon = Daemon(mock_config)
        api_data = {"title": "Test"}
        existing = {"workshop_id": 1}
        now = 1000000
        result = daemon._merge_and_clean_api_data(api_data, existing, 1, now)
        assert result["api_priority"] == 0
        assert result["api_fetched_at"] == now


def test_the_api_merge_never_carries_the_downloaded_latch(mock_config):
    """`downloaded_at` is local state, so the merge neither keeps nor sets it.

    It is absent from the cleaned record whether it arrived on the API payload
    (where it is not a Steam field) or on the stored row. `insert_or_update_item`
    only writes the columns it is handed, so leaving it out of the merge is what
    keeps the folder scan the column's only writer.
    """
    with patch('src.database.initialize_database'), \
         patch('src.daemon.save_config'):
        daemon = Daemon(mock_config)
        result = daemon._merge_and_clean_api_data(
            {"title": "Test", "downloaded_at": 1},
            {"workshop_id": 1, "downloaded_at": 99},
            1, 1000000)
        assert "downloaded_at" not in result


@patch('src.database.initialize_database')
@patch('src.daemon.save_config')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.count_never_fetched_items', return_value=0)
@patch('src.daemon.get_workshop_details_batch')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.raise_web_scrape_priority')
@patch('src.daemon.raise_image_priority')
@patch('src.daemon.get_connection')
@patch('src.daemon.get_creator')
def test_process_batch_404_permanent_status_marker(
    mock_user, mock_conn, mock_img, mock_web, mock_insert,
    mock_api, mock_count, mock_items, mock_save, mock_init, mock_config
):
    """Verify 404 item gets status=-1 passed to insert."""
    mock_api.return_value = {1: {"status": 404, "publishedfileid": 1}}
    mock_items.return_value = [{"workshop_id": 1}]
    mock_user.return_value = None
    daemon = Daemon(mock_config)
    daemon.process_batch()
    # Check that insert was called with status=-1 somewhere in the call args
    for call in mock_insert.call_args_list:
        data = call[0][1] if len(call[0]) > 1 else {}
        if data.get("workshop_id") == 1:
            assert data.get("status") == -1
            assert data.get("api_priority") == 0


@patch('src.database.initialize_database')
@patch('src.daemon.save_config')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.count_never_fetched_items', return_value=0)
@patch('src.daemon.get_workshop_details_batch')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.raise_web_scrape_priority')
@patch('src.daemon.raise_image_priority')
@patch('src.daemon.get_connection')
@patch('src.daemon.get_creator')
@patch('src.daemon.get_player_summaries', return_value={})
@patch('src.daemon.get_app_tracking', return_value=None)
def test_process_batch_inherits_priority(
    mock_track, mock_summaries, mock_user, mock_conn, mock_img, mock_web, mock_insert,
    mock_api, mock_count, mock_items, mock_save, mock_init, mock_config
):
    """raise_web_scrape_priority and raise_image_priority keep a priority the user asked for.

    api_priority 5 is "visible in a list" -- a person asked for this item -- so
    the dependent stages inherit it rather than falling back to their own
    default. The priorities the *daemon* sets (1 backlog, 2 retry, 3 discovery)
    do not survive; see test_process_batch_does_not_inherit_the_discovery_priority.
    """
    mock_api.return_value = {1: {"title": "Test", "creator": "111", "preview_url": "http://x", "status": 200}}
    mock_items.return_value = [{"workshop_id": 1, "api_priority": 5, "status": 200}]
    mock_user.return_value = None
    daemon = Daemon(mock_config)
    daemon.process_batch()
    # Web scrape should be flagged at max(3, 5) = 5 (enriched, inherited 5)
    # Image should be flagged at max(3, 5) = 5
    for call in mock_web.call_args_list:
        assert call[0][2] >= 5
    for call in mock_img.call_args_list:
        assert call[0][2] >= 5


@patch('src.database.initialize_database')
@patch('src.daemon.save_config')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.count_never_fetched_items', return_value=0)
@patch('src.daemon.get_workshop_details_batch')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.raise_web_scrape_priority')
@patch('src.daemon.raise_image_priority')
@patch('src.daemon.get_connection')
@patch('src.daemon.get_creator')
@patch('src.daemon.get_player_summaries', return_value={})
@patch('src.daemon.get_app_tracking')
def test_process_batch_does_not_inherit_the_discovery_priority(
    mock_track, mock_summaries, mock_user, mock_conn, mock_img, mock_web, mock_insert,
    mock_api, mock_count, mock_items, mock_save, mock_init, mock_config
):
    """A newly discovered item the filters exclude is queued at backlog, not at 3.

    Discovery is where an item starts, not something anyone asked for, and the
    two queues share one number -- so inheriting all of it put a filtered-out item
    in the same band as one the filters selected. Measured live before this: of
    the items above backlog priority, 760,782 web entries belonged to excluded
    items while 107,365 selected ones waited behind them.
    """
    mock_api.return_value = {1: {
        "title": "Test", "creator": "111", "status": 200, "consumer_appid": 123,
        "preview_url": "http://x", "steam_updated_at": 1000,
    }}
    mock_items.return_value = [{"workshop_id": 1, "api_priority": 3, "status": 200}]
    mock_track.return_value = {"enrichment_filters": json.dumps(
        [{"field": "Tags", "op": "contains", "value": "Mature"}])}
    mock_user.return_value = None
    daemon = Daemon(mock_config)
    daemon.process_batch()

    assert mock_web.call_args_list, "it is still scraped, just not prioritised"
    for call in mock_web.call_args_list:
        assert call[0][2] == 1, "the discovery priority must not reach the scrape queue"
    for call in mock_img.call_args_list:
        assert call[0][2] == 1


def test_page_discovery_eligible_trigger_file(mock_config):
    """_page_discovery_eligible returns True when .fetch_new exists."""
    from src.daemon import Daemon
    import os
    with patch('src.database.initialize_database'), \
         patch('src.daemon.save_config'):
        daemon = Daemon(mock_config)
        daemon._cursor_exhausted = False
        # Without trigger file
        assert daemon._page_discovery_eligible() is False
        # With trigger file
        with open('.fetch_new', 'w') as f:
            f.write('1')
        try:
            assert daemon._page_discovery_eligible() is True
        finally:
            os.remove('.fetch_new')


def test_promote_stale_items_only_promotes_stale_live_unqueued(db_path, tmp_path):
    """Behaviour: the periodic sweep promotes stale, live, not-yet-queued items.

    This replaces a source-text assertion on the sweep SQL. A source check passed
    even if the query was reassembled in a way that changed its meaning, and broke
    whenever the code merely moved; this exercises the real statement against a
    temporary database.

    The sweep must:
      * promote  api_priority = 0 + status = 200 + api_fetched_at stale  -> 1
      * leave    fresh status = 200 rows alone
      * leave    dead rows (status = -1) alone
      * leave    rows already queued (api_priority != 0) alone
    """
    import time
    from src.database import insert_or_update_item, get_connection

    now = int(time.time())
    stale = now - 40 * 86400  # default item_staleness_days is 30
    fresh = now

    insert_or_update_item(db_path, {"workshop_id": 1, "status": 200, "api_priority": 0, "api_fetched_at": stale})
    insert_or_update_item(db_path, {"workshop_id": 2, "status": 200, "api_priority": 0, "api_fetched_at": fresh})
    insert_or_update_item(db_path, {"workshop_id": 3, "status": -1, "api_priority": 0, "api_fetched_at": stale})
    insert_or_update_item(db_path, {"workshop_id": 4, "status": 200, "api_priority": 5, "api_fetched_at": stale})

    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST"},
        "daemon": {"api_batch_size": 1, "target_appids": [1]},
    }
    # Daemon.__init__ calls save_config(self.config_path, ...), and save_config
    # writes to the file when it exists. Point config_path at a path that does not
    # exist so the write is a no-op and the developer's real config.yaml is never
    # touched.
    daemon = Daemon(config, config_path=str(tmp_path / "config.yaml"))
    daemon._promote_stale_items()

    conn = get_connection(db_path)
    priorities = {
        row["workshop_id"]: row["api_priority"]
        for row in conn.execute("SELECT workshop_id, api_priority FROM workshop_items")
    }
    conn.close()

    assert priorities == {1: 1, 2: 0, 3: 0, 4: 5}


# ── api_fetched_at vs last_fetch_attempted_at (bug fixes #1 and #2) ───────────

def _real_db_daemon(db_path, tmp_path):
    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST"},
        "daemon": {"api_batch_size": 1, "target_appids": [1]},
    }
    return Daemon(config, config_path=str(tmp_path / "config.yaml"))


def test_process_item_500_records_attempt_but_not_fetch(db_path, tmp_path):
    """A 500 is an attempt: last_fetch_attempted_at moves, api_fetched_at does not."""
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {
        "workshop_id": 1, "status": 200, "api_priority": 5,
        "api_fetched_at": 1000, "last_fetch_attempted_at": 1000,
    })
    daemon = _real_db_daemon(db_path, tmp_path)
    existing = {"workshop_id": 1, "status": 200, "api_priority": 5,
                "api_fetched_at": 1000, "last_fetch_attempted_at": 1000}

    with patch("src.daemon.get_workshop_details", return_value={"status": 500}):
        daemon._process_item(existing)

    conn = get_connection(db_path)
    row = dict(conn.execute("SELECT * FROM workshop_items WHERE workshop_id=1").fetchone())
    conn.close()
    assert row["api_fetched_at"] == 1000          # success clock untouched
    assert row["last_fetch_attempted_at"] > 1000  # attempt clock moved


def test_process_item_404_records_attempt_but_not_fetch(db_path, tmp_path):
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {
        "workshop_id": 1, "status": 200, "api_priority": 5,
        "api_fetched_at": 1000, "last_fetch_attempted_at": 1000,
    })
    daemon = _real_db_daemon(db_path, tmp_path)
    existing = {"workshop_id": 1, "status": 200, "api_priority": 5,
                "api_fetched_at": 1000, "last_fetch_attempted_at": 1000}

    with patch("src.daemon.get_workshop_details", return_value={"status": 404}):
        daemon._process_item(existing)

    conn = get_connection(db_path)
    row = dict(conn.execute("SELECT * FROM workshop_items WHERE workshop_id=1").fetchone())
    conn.close()
    assert row["status"] == -1
    assert row["api_fetched_at"] == 1000
    assert row["last_fetch_attempted_at"] > 1000


def test_process_item_404_clears_every_queue_flag(db_path, tmp_path):
    """Marking an item dead must remove it from every queue.

    The web, image and translation polls select on their own flag alone with no
    dead-item guard, so a flag left set here keeps the item in a queue that can
    never drain and spends requests on a page that no longer exists.
    """
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {
        "workshop_id": 1, "status": 200, "api_priority": 5,
        "needs_web_scrape": 5, "needs_image": 10, "translation_priority": 3,
    })
    daemon = _real_db_daemon(db_path, tmp_path)
    existing = {"workshop_id": 1, "status": 200, "api_priority": 5,
                "needs_web_scrape": 5, "needs_image": 10,
                "translation_priority": 3}

    with patch("src.daemon.get_workshop_details", return_value={"status": 404}):
        daemon._process_item(existing)

    conn = get_connection(db_path)
    row = dict(conn.execute("SELECT * FROM workshop_items WHERE workshop_id=1").fetchone())
    conn.close()
    assert row["status"] == -1
    assert row["api_priority"] == 0
    assert row["needs_web_scrape"] == 0
    assert row["needs_image"] == 0
    assert row["translation_priority"] == 0


def test_process_item_success_moves_both_clocks(db_path, tmp_path):
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {
        "workshop_id": 1, "status": 200, "api_priority": 5,
        "api_fetched_at": 1000, "last_fetch_attempted_at": 1000,
    })
    daemon = _real_db_daemon(db_path, tmp_path)
    existing = {"workshop_id": 1, "status": 200, "api_priority": 5,
                "api_fetched_at": 1000, "last_fetch_attempted_at": 1000}

    with patch("src.daemon.get_workshop_details",
               return_value={"title": "T", "status": 200}):
        daemon._process_item(existing)

    conn = get_connection(db_path)
    row = dict(conn.execute("SELECT * FROM workshop_items WHERE workshop_id=1").fetchone())
    conn.close()
    assert row["api_fetched_at"] > 1000
    assert row["last_fetch_attempted_at"] > 1000


def test_merge_remaps_steam_api_time_fields(db_path):
    """The Steam API still returns time_created/time_updated; the merge must map
    them onto steam_created_at/steam_updated_at rather than discard them."""
    from src.daemon import Daemon

    config = {"database": {"path": db_path}, "api": {"key": "K"},
              "daemon": {"target_appids": [1]}}
    with patch("src.daemon.save_config"):
        daemon = Daemon(config)
        result = daemon._merge_and_clean_api_data(
            {"time_created": 111, "time_updated": 222, "title": "T"},
            {"workshop_id": 1}, 1, 999)
    assert result["steam_created_at"] == 111
    assert result["steam_updated_at"] == 222
    assert "time_created" not in result
    assert "time_updated" not in result
    assert result["api_fetched_at"] == 999


# ── Batch-level creator refresh and timed staleness sweep ────────────────────

@patch('src.daemon.get_workshop_details_batch')
@patch('src.daemon.get_next_items_to_fetch')
@patch('src.daemon.insert_or_update_creator')
@patch('src.daemon.get_creator')
@patch('src.daemon.get_player_summaries')
def test_creator_refresh_makes_one_call_for_several_creators(
    mock_summaries, mock_get_creator, mock_insert_creator, mock_items, mock_batch,
    db_path, tmp_path
):
    """Distinct stale creators share a single GetPlayerSummaries request.

    The "only enriched items" and staleness rules are unchanged; only the
    per-item request is gone.
    """
    daemon = _real_db_daemon(db_path, tmp_path)
    daemon.api_batch_size = 4
    mock_items.return_value = [
        {"workshop_id": i, "api_priority": 5, "status": 200} for i in (1, 2, 3, 4)
    ]
    # Creator 111 appears twice and is fresh; 222 and 333 are stale.
    mock_batch.return_value = {
        1: {"title": "a", "creator": "111", "status": 200},
        2: {"title": "b", "creator": "222", "status": 200},
        3: {"title": "c", "creator": "333", "status": 200},
        4: {"title": "d", "creator": "111", "status": 200},
    }
    fresh = int(time.time())
    mock_get_creator.side_effect = lambda path, cid: (
        {"steamid": cid, "api_fetched_at": fresh} if cid == 111 else None)
    mock_summaries.return_value = {
        111: {"personaname": "One"},
        222: {"personaname": "Two"},
        333: {"personaname": "Three"},
    }

    daemon.process_batch()

    mock_summaries.assert_called_once()
    assert sorted(mock_summaries.call_args[0][0]) == [222, 333]
    assert mock_insert_creator.call_count == 2


@patch('src.daemon.get_workshop_details_batch')
def test_only_enriched_items_propose_a_creator(mock_batch, db_path, tmp_path):
    """A non-enriched item proposes no creator, so its persona is not fetched."""
    from src.daemon import Daemon
    from src.database import insert_or_update_item

    insert_or_update_item(db_path, {"workshop_id": 1, "api_priority": 5, "status": 200})
    daemon = _real_db_daemon(db_path, tmp_path)
    daemon.api_batch_size = 1
    mock_batch.return_value = {1: {"title": "x", "creator": "111", "status": 200}}
    with patch.object(daemon, "_should_enrich", return_value=False), \
         patch("src.daemon.get_next_items_to_fetch",
               return_value=[{"workshop_id": 1, "api_priority": 5, "status": 200}]), \
         patch("src.daemon.get_player_summaries") as mock_summaries, \
         patch("src.daemon.get_creator") as mock_get_creator:
        daemon.process_batch()
    mock_summaries.assert_not_called()
    mock_get_creator.assert_not_called()


@patch.object(Daemon, '_promote_stale_items')
def test_stale_sweep_runs_at_most_once_per_interval(mock_promote, mock_config):
    daemon = Daemon(mock_config)
    daemon._last_stale_sweep = None

    daemon._maybe_promote_stale_items()
    daemon._maybe_promote_stale_items()
    assert mock_promote.call_count == 1, "a second batch inside the interval must not sweep"

    daemon._last_stale_sweep = time.monotonic() - STALE_SWEEP_INTERVAL_SECONDS - 1
    daemon._maybe_promote_stale_items()
    assert mock_promote.call_count == 2, "the sweep runs again once the interval has elapsed"


@patch.object(Daemon, '_promote_stale_items')
@patch('src.daemon.get_next_items_to_fetch', return_value=None)
def test_stale_sweep_runs_on_the_first_batch_after_startup(mock_items, mock_promote, mock_config):
    daemon = Daemon(mock_config)
    daemon.process_batch()
    mock_promote.assert_called_once()

