import pytest
from unittest.mock import patch, MagicMock, ANY
import signal
import json
from src.daemon import Daemon
from src.database import get_app_tracking, initialize_database, update_app_tracking

@pytest.fixture
def mock_config(db_path):
    return {
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"batch_size": 2, "request_delay_seconds": 0.01, "target_appids": [123]}
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
    assert daemon.batch_size == 10
    assert daemon.api_delay == 1.5
    assert daemon.item_staleness_days == 30
    assert daemon.user_staleness_days == 90
    assert daemon.target_appids == [456]

def test_daemon_init_missing_appids():
    """Test that the Daemon raises a ValueError if target_appids is omitted."""
    empty_config = {}
    with pytest.raises(ValueError, match="must be provided as a list"):
        Daemon(empty_config)
        
    invalid_config = {"daemon": {"target_appids": "not_a_list_just_a_string"}}
    with pytest.raises(ValueError, match="must be provided as a list"):
        Daemon(invalid_config)

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.get_user')
@patch('src.daemon.insert_or_update_user')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.flag_for_web_scrape')
@patch('time.sleep')
def test_daemon_process_batch_success(mock_sleep, mock_flag_web, mock_insert, mock_insert_user, mock_get_user, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 123}]
    mock_api.return_value = {"title": "Test Mod", "creator": "111", "status": 200}
    mock_get_user.return_value = {"steamid": 111, "api_fetched_at": 1767225600}

    daemon = Daemon(mock_config)
    daemon.process_batch()

    mock_get_items.assert_called_once_with(mock_config["database"]["path"], limit=2, staleness_days=30)
    mock_api.assert_called_once_with(123, "TEST_KEY")
    mock_flag_web.assert_called_once_with(mock_config["database"]["path"], 123, 3)

    inserted_data = mock_insert.call_args[0][1]
    assert inserted_data["workshop_id"] == 123
    assert inserted_data["title"] == "Test Mod"

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_daemon_process_batch_api_failure(mock_sleep, mock_insert, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 456}]
    mock_api.return_value = {"status": 500, "publishedfileid": 456} # Mock API failure with status

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

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
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

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
def test_daemon_process_batch_exit_early(mock_api, mock_get_items, mock_count, mock_config):
    """Test behavior when shutdown signal is received mid-batch."""
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 1}, {'workshop_id': 2}, {'workshop_id': 3}]
    daemon = Daemon(mock_config)
    daemon.running = False # Simulate shutdown right before processing
    daemon.process_batch()
    mock_api.assert_not_called()

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.get_user')
@patch('src.daemon.insert_or_update_user')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.flag_for_web_scrape')
@patch('time.sleep')
def test_api_delay_decreases_on_success(mock_sleep, mock_flag_web, mock_insert, mock_insert_user, mock_get_user, mock_api, mock_get_items, mock_count, mock_config):
    items = [{'workshop_id': i} for i in range(100)]
    mock_count.return_value = 1000
    mock_get_items.return_value = items
    # A fresh dict per call: _merge_and_clean_api_data pops keys off the response,
    # so a shared mock object loses its "status" after the first item and every
    # later item reads as an unhandled code rather than a success.
    mock_api.side_effect = lambda *a, **k: {"title": "Mod", "creator": "111", "status": 200}
    mock_get_user.return_value = {"steamid": 111, "api_fetched_at": 1767225600}

    daemon = Daemon(mock_config)
    daemon.api_delay = 1.0
    initial = daemon.api_delay
    daemon.process_batch()
    assert daemon.api_delay < initial

@patch('src.daemon.count_unscraped_items')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.insert_or_update_item')
@patch('time.sleep')
def test_api_delay_increases_on_failures(mock_sleep, mock_insert, mock_api, mock_get_items, mock_count, mock_config):
    mock_count.return_value = 1000
    mock_get_items.return_value = [{'workshop_id': 11}, {'workshop_id': 12}]
    mock_api.return_value = {"status": 500, "publishedfileid": 11}

    daemon = Daemon(mock_config)
    daemon.api_delay = 1.0
    daemon.api_successes = 5
    daemon.api_had_streak = True
    initial = daemon.api_delay

    daemon.process_batch()
    assert daemon.api_delay > initial


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


@patch('src.database.initialize_database')
@patch('src.daemon.save_config')
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.count_unscraped_items', return_value=0)
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.flag_for_web_scrape')
@patch('src.daemon.flag_for_image')
@patch('src.daemon.get_connection')
@patch('src.daemon.get_user')
def test_process_batch_404_status_marker(
    mock_user, mock_conn, mock_img, mock_web, mock_insert,
    mock_api, mock_count, mock_items, mock_save, mock_init, mock_config
):
    """Verify 404 item gets status=-1 passed to insert."""
    mock_api.return_value = {"status": 404, "publishedfileid": 1}
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
@patch('src.daemon.get_next_items_to_scrape')
@patch('src.daemon.count_unscraped_items', return_value=0)
@patch('src.daemon.get_workshop_details_api')
@patch('src.daemon.insert_or_update_item')
@patch('src.daemon.flag_for_web_scrape')
@patch('src.daemon.flag_for_image')
@patch('src.daemon.get_connection')
@patch('src.daemon.get_user')
@patch('src.daemon.get_app_tracking', return_value=None)
def test_process_batch_inherits_priority(
    mock_track, mock_user, mock_conn, mock_img, mock_web, mock_insert,
    mock_api, mock_count, mock_items, mock_save, mock_init, mock_config
):
    """flag_for_web_scrape and flag_for_image get max(inherited, default)."""
    mock_api.return_value = {"title": "Test", "creator": "111", "preview_url": "http://x", "status": 200}
    mock_items.return_value = [{"workshop_id": 1, "api_priority": 5, "status": 200}]
    mock_user.return_value = None
    daemon = Daemon(mock_config)
    daemon.process_batch()
    # Web scrape should be flagged at max(3, 5) = 5 (enriched, inherited_prio=5)
    # Image should be flagged at max(3, 5) = 5
    for call in mock_web.call_args_list:
        assert call[0][2] >= 5
    for call in mock_img.call_args_list:
        assert call[0][2] >= 5


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
        "daemon": {"batch_size": 1, "target_appids": [1]},
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
        "daemon": {"batch_size": 1, "target_appids": [1]},
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

    with patch("src.daemon.get_workshop_details_api", return_value={"status": 500}):
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

    with patch("src.daemon.get_workshop_details_api", return_value={"status": 404}):
        daemon._process_item(existing)

    conn = get_connection(db_path)
    row = dict(conn.execute("SELECT * FROM workshop_items WHERE workshop_id=1").fetchone())
    conn.close()
    assert row["status"] == -1
    assert row["api_fetched_at"] == 1000
    assert row["last_fetch_attempted_at"] > 1000


def test_process_item_success_moves_both_clocks(db_path, tmp_path):
    from src.database import insert_or_update_item, get_connection

    insert_or_update_item(db_path, {
        "workshop_id": 1, "status": 200, "api_priority": 5,
        "api_fetched_at": 1000, "last_fetch_attempted_at": 1000,
    })
    daemon = _real_db_daemon(db_path, tmp_path)
    existing = {"workshop_id": 1, "status": 200, "api_priority": 5,
                "api_fetched_at": 1000, "last_fetch_attempted_at": 1000}

    with patch("src.daemon.get_workshop_details_api",
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

