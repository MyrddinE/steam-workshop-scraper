import pytest
from textual.widgets import Button, ListView, Static
from src.database import insert_or_update_item, toggle_subscription_queue
from src.tui import ScraperApp, SubscriptionQueueScreen
from unittest.mock import patch
from tests.conftest import ASYNC_PAUSE

@pytest.mark.asyncio
async def test_tui_toggle_subscription_queue(mock_config):
    mock_results = [
        {"workshop_id": 1, "title": "Item 1", "creator_steamid": "A", "is_queued_for_subscription": 0},
        {"workshop_id": 2, "title": "Item 2", "creator_steamid": "B", "is_queued_for_subscription": 0},
    ]
    # The rows render from the mocked search, but the action hands the change to
    # the registry by reading the item back, so the rows must exist in the
    # database the read hits.
    for item in mock_results:
        insert_or_update_item(mock_config["database"]["path"],
                              dict(item, fetch_status=200))

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.toggle_subscription_queue',
               wraps=toggle_subscription_queue) as mock_toggle_db:
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)

            list_view = app.query_one("#results-list", ListView)
            list_view.index = 0
            original_item = list_view.highlighted_child

            assert original_item.item_data["is_queued_for_subscription"] == 0

            await pilot.press("s")
            await pilot.pause(ASYNC_PAUSE)

            mock_toggle_db.assert_called_once_with(mock_config["database"]["path"], 1)
            assert original_item.item_data["is_queued_for_subscription"] == 1
            assert list_view.index == 1

@pytest.mark.asyncio
async def test_tui_show_subscription_queue(mock_config, tmp_path):
    lock_file = tmp_path / ".pauselock"

    # `ScraperApp` reads `load_config` itself, so without this patch it took the
    # checkout's configuration and opened `workshop.db` in the repository root --
    # a gitignored artefact, not a fixture. The other tests in this file patch it
    # to a temporary database the same way; `mock_config` was passed in but left
    # unused here.
    with patch('src.tui.load_config', return_value=mock_config):
        app = ScraperApp()
        # Guard: an app built from the checkout's configuration would not carry
        # the fixture's path, so dropping the patch fails here rather than
        # silently reading the repository's database.
        assert app.config["database"] == mock_config["database"]
        app.pause_lock_file = str(lock_file)
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)

            assert not lock_file.exists()

            await pilot.press("l")
            await pilot.pause(ASYNC_PAUSE)

            assert isinstance(app.screen, SubscriptionQueueScreen)
            assert lock_file.exists()

            await pilot.click("#btn-close-subscription-queue")
            await pilot.pause(ASYNC_PAUSE)

            assert not isinstance(app.screen, SubscriptionQueueScreen)
            assert not lock_file.exists()

@pytest.mark.asyncio
async def test_queue_lists_items_without_urls(mock_config, tmp_path):
    """The screen subscribes through the engine; it no longer hands out links."""
    from src.database import initialize_database, toggle_subscription_queue, insert_or_update_item
    db_path = str(tmp_path / "queue_render.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "[b]Bold Title[/b]", "fetch_status": 200})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "Plain Title", "fetch_status": 200})
    toggle_subscription_queue(db_path, 1)
    toggle_subscription_queue(db_path, 2)

    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}
    with patch('src.tui.load_config', return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)

            await pilot.press("l")
            await pilot.pause(ASYNC_PAUSE * 2)

            screen = app.screen
            assert isinstance(screen, SubscriptionQueueScreen)
            drawn = " ".join(str(s.render()) for s in screen.query(Static))
            assert "steamcommunity.com" not in drawn, "no URLs to click any more"
            # The Steam title's brackets survive literally, and there is a
            # control that runs the engine.
            assert "[b]Bold Title[/b]" in drawn
            assert "Plain Title" in drawn
            assert screen.query_one("#btn-subscribe-queue", Button)


@pytest.mark.asyncio
async def test_the_queue_button_runs_the_engine_and_shows_progress(mock_config, tmp_path):
    from src import subscribe_engine
    from src.database import initialize_database, toggle_subscription_queue, insert_or_update_item
    db_path = str(tmp_path / "queue_run.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Item One", "fetch_status": 200})
    toggle_subscription_queue(db_path, 1)
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    def fake_pass(items, **kwargs):
        outcomes = []
        for item in items:
            outcome = subscribe_engine.SubscribeOutcome(
                item["workshop_id"], subscribe_engine.SUBSCRIBED, subscribed=True)
            outcomes.append(outcome)
            kwargs["on_result"](outcome)
        return outcomes

    with patch('src.tui.load_config', return_value=config), \
         patch('src.subscribe_engine.run_subscription_pass', side_effect=fake_pass):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            await pilot.press("l")
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen
            await pilot.click("#btn-subscribe-queue")
            await pilot.pause(ASYNC_PAUSE * 3)
            drawn = " ".join(str(s.render()) for s in screen.query(Static))
            pass_running = screen._pass_running

    assert "subscribed" in drawn.lower(), "the per-item result must be on screen"
    assert pass_running is False, "the pass must have finished before teardown"


@pytest.mark.asyncio
async def test_the_queue_run_button_and_guidance_are_direction_neutral(mock_config, tmp_path):
    """The queue holds additions and removals, so the run control names neither.

    A queue of removals must not be run by a button that says Subscribe, and one
    button cannot name a mixed queue; the rows and the pass tally carry the
    direction instead.
    """
    from src.database import initialize_database, insert_or_update_item
    db_path = str(tmp_path / "queue_neutral.db")
    initialize_database(db_path)
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "T", "fetch_status": 200,
        "own_subscribed": 1, "is_queued_for_subscription": 1,
        "own_first_subscribed_at": 1000})
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch('src.tui.load_config', return_value=config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            await pilot.press("l")
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen
            label = str(screen.query_one("#btn-subscribe-queue", Button).label)
            status = str(screen.query_one("#subscription-queue-status", Static).render())

    assert "Run Queue" in label, label
    assert "Subscribe" not in label, \
        "a removal queue must not be run by a Subscribe button"
    assert "Run Queue" in status, status
