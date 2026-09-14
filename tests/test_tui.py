import pytest
from textual.widgets import Input, ListItem, Static, ListView, Select, Button, DataTable
from textual.containers import VerticalScroll
from tests.conftest import ASYNC_PAUSE
from src.tui import ScraperApp, StatsScreen
from src import metrics
from unittest.mock import patch, MagicMock
import time

@pytest.fixture
def mock_results():
    return [
        {
            "workshop_id": 1,
            "title": "Amazing Mod",
            "creator": "Author A",
            "consumer_appid": 294100,
            "extended_description": "This mod is truly amazing.",
            "tags": '["Graphic", "Utility"]'
        }
    ]

@pytest.mark.asyncio
async def test_tui_advanced_search_flow(mock_config, mock_results):
    def get_details_mock(db, wid):
        for r in mock_results:
            if r["workshop_id"] == wid: return r
        return None

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_item_details', side_effect=get_details_mock), \
         patch('src.tui.get_all_authors', return_value=["Author A", "Author B"]):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            # Wait for on_mount auto-search to complete
            await pilot.pause(ASYNC_PAUSE)

            # Verify results auto-populated
            list_view = app.query_one(ListView)
            assert len(list_view.children) == 1

            # Verify new inputs exist
            search_builder = app.query_one("#search-builder")
            assert search_builder is not None
            
            # Select the item
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            
            from src.tui import DetailsPane
            from textual.widgets import Markdown, Label
            detail_pane = app.query_one("#item-details", DetailsPane)
            title_label = detail_pane.query_one("#item-title", Label)
            detail_content = detail_pane.query_one("#detail-content", Markdown)

            assert "Amazing Mod" in str(title_label.render())
            content = str(detail_content._markdown)
            assert "amazing" in content.lower()

@pytest.mark.asyncio
@pytest.mark.skip(reason="Timing-sensitive Textual widget test — tested by search/filter unit tests")
async def test_tui_jump_to_author(mock_config, mock_results):
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_all_authors', return_value=["Author A"]):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            # Wait for on_mount
            await pilot.pause(ASYNC_PAUSE)
            
            # Select item
            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            
            # Wait for ListView.Selected event to process and layout to un-hide the button
            await pilot.pause(ASYNC_PAUSE)
            
            # Click 'Jump to Author' button
            await pilot.click("#btn-jump-author")
            
            # Wait for button press event and call_after_refresh to process
            await pilot.pause(ASYNC_PAUSE * 2)
            
            # Verify Author Select is updated and title is cleared
            builder = app.query_one("#search-builder")
            rows = builder.query("SearchRow")
            assert len(rows) == 1
            first_row = list(rows)[0]
            assert str(first_row.query_one("#field-select").value) == "Author ID"
            assert str(first_row.query_one("#value-input").value) == "Author A"

@pytest.mark.asyncio
async def test_tui_translation_flow(mock_config, mock_results):
    """Tests TUI behavior with tags provided as string, list, or invalid types."""
    mock_results = [
        {
            "workshop_id": 3,
            "title": "List Mod",
            "tags": ["Valid", "List"]
        },
        {
            "workshop_id": 4,
            "title": "Dict Mod",
            "tags": {"invalid": "dict"}
        },
        {
            "workshop_id": 5,
            "title": "API Tags Mod",
            "tags": '[{"tag": "Mod"}, {"tag": "1.0"}]'
        }
    ]

    from unittest.mock import patch
    def get_details_mock(db, wid):
        for r in mock_results:
            if r["workshop_id"] == wid: return r
        return None

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_item_details', side_effect=get_details_mock):
        
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE) # Wait for mount

            # Verify List Mod
            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.pause(ASYNC_PAUSE)

            from src.tui import DetailsPane
            from textual.widgets import Markdown, Label
            detail_pane = app.query_one("#item-details", DetailsPane)
            title_label = detail_pane.query_one("#item-title", Label)
            tags_label = detail_pane.query_one("#stat-tags", Label)

            assert "List Mod" in str(title_label.render())
            assert "Valid, List" in str(tags_label.render())

            # Verify Dict Mod (should not crash, should just be empty tags)
            list_view.index = 1
            await pilot.pause(ASYNC_PAUSE)

            assert "Dict Mod" in str(title_label.render())
            assert "None" in str(tags_label.render())

            # Verify API Tags Mod
            list_view.index = 2
            await pilot.pause(ASYNC_PAUSE)

            assert "API Tags Mod" in str(title_label.render())
            assert "Mod, 1.0" in str(tags_label.render())


@pytest.mark.asyncio
async def test_tui_operator_selection_by_field_type(mock_config, mock_results):
    from unittest.mock import patch
    from src.tui import ScraperApp
    from textual.widgets import Select

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            
            builder = app.query_one("#search-builder")
            first_row = list(builder.query("SearchRow"))[0]
            field_select = first_row.query_one("#field-select", Select)
            op_select = first_row.query_one("#op-select", Select)
            
            # Switch to numeric field with percentile — should include gt, percentile
            field_select.value = "Subs"
            await pilot.pause(ASYNC_PAUSE)
            assert "gt" in [val for label, val in op_select._options]
            assert "percentile" in [val for label, val in op_select._options]
            assert "does_not_contain" not in [val for label, val in op_select._options]
            
            # Field without percentile (File Size)
            field_select.value = "File Size"
            await pilot.pause(ASYNC_PAUSE)
            assert "gt" in [val for label, val in op_select._options]
            assert "percentile" not in [val for label, val in op_select._options]
            
            # Switch to ID field
            field_select.value = "Author ID"
            await pilot.pause(ASYNC_PAUSE)
            assert "is" in [val for label, val in op_select._options]
            assert "gt" not in [val for label, val in op_select._options]
            
            # Switch to Text field
            field_select.value = "Title"
            await pilot.pause(ASYNC_PAUSE)
            assert "does_not_contain" in [val for label, val in op_select._options]

@pytest.mark.asyncio
@pytest.mark.skip(reason="Timing-sensitive Textual widget test — tested by search/filter unit tests")
async def test_tui_jump_to_author_clears_multiple_rows(mock_config, mock_results):
    from unittest.mock import patch
    from src.tui import ScraperApp
    from textual.widgets import ListView, Button

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_all_authors', return_value=["Author A"]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            
            # Add some extra rows
            await pilot.click("#btn-and")
            await pilot.click("#btn-or")
            await pilot.pause(ASYNC_PAUSE)
            
            builder = app.query_one("#search-builder")
            assert len(builder.query("SearchRow")) == 3
            
            # Select an item to show the jump button
            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            await pilot.pause(ASYNC_PAUSE)
            
            # Click jump button
            jump_btn = app.query_one("#btn-jump-author", Button)
            jump_btn.press()
            await pilot.pause(ASYNC_PAUSE * 2)
            
            # Verify rows cleared and set to Author ID
            rows = list(builder.query("SearchRow"))
            assert len(rows) == 1
            assert rows[0].query_one("#field-select").value == "Author ID"
            assert rows[0].query_one("#value-input").value == "Author A"

    @pytest.mark.asyncio
    async def test_tui_infinite_scroll(mock_config):
        from unittest.mock import patch, call, MagicMock
        from src.tui import ScraperApp
        from textual.widgets import ListView
        from textual.scroll_view import ScrollView

        mock_results = [{"workshop_id": i, "title": f"Item {i}", "creator": "A"} for i in range(100)]
        mock_total_count = 100

        # Mock search_items to paginate
        def mock_search_items(db_path, *args, **kwargs):
            if kwargs.get("count_only"):
                return mock_total_count
            offset = kwargs.get("offset", 0)
            limit = kwargs.get("limit", 50)
            return mock_results[offset:offset+limit]

        with patch('src.tui.load_config', return_value=mock_config), \
             patch('src.tui.search_items', side_effect=mock_search_items) as mock_db_search:
            app = ScraperApp()
            async with app.run_test() as pilot:
                # Wait for on_mount and initial execute_search (which calls load_more_items) to complete
                await pilot.pause()

                # Verify initial load state
                assert app.items_loaded == 50
                assert app.current_offset == 50
                list_view = app.query_one(ListView)
                assert len(list_view.children) == 50 # Now this should be reliable if items are appended

                # Simulate scrolling to trigger load_more_items
                list_view.scroll_y = list_view.max_scroll_y # Scroll to bottom
                await pilot.pause() # Wait for load_more_items to be called and processed

                # Verify load_more_items call and state update
                # The search_items call should reflect the next offset
                expected_query_params = {
                    "title_query": "", "tags": [], "excluded_tags": [], "creator_id": None,
                    "min_size": None, "max_size": None, "min_subs": None, "max_subs": None,
                    "min_favs": None, "max_favs": None, "min_views": None, "max_views": None,
                    "workshop_id": None, "appid": None, "language_id": None,
                    "sort_by": "title", "sort_order": "ASC"
                }
                mock_db_search.assert_called_with(ANY, limit=50, offset=50, **expected_query_params)
                assert app.items_loaded == 100
                assert app.current_offset == 100
                assert len(list_view.children) == 100 # All items should now be rendered

@pytest.mark.asyncio
async def test_tui_details_pane_translation_fallback_and_priority(mock_config):
    from unittest.mock import patch
    from src.tui import ScraperApp
    from textual.widgets import ListView

    mock_results = [{
        "workshop_id": 1, 
        "title": "Title", 
        "translate_version": "2023-01-01", 
        "translation_priority": 0,
        "tags": "{invalid_json",
        "extended_description": "Original Desc"
        # No translated desc, so it will fall back
    }]
    
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_item_details', return_value=mock_results[0]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            await pilot.pause(ASYNC_PAUSE)
            
            # Since priority is 0, it shouldn't say queued
            detail_content = app.query_one("#detail-content")
            assert "Queued for translation..." not in str(detail_content._markdown)
            
            # Toggle translation to trigger the fallback logic and button label logic
            toggle_btn = app.query_one("#btn-toggle-translation")
            assert toggle_btn.display is True
            toggle_btn.press()
            await pilot.pause(ASYNC_PAUSE)
            
            assert "Original Desc" in str(detail_content._markdown) # Fallback worked

@pytest.mark.asyncio
async def test_tui_remove_search_row(mock_config):
    from unittest.mock import patch
    from src.tui import ScraperApp
    from textual.widgets import Button

    with patch('src.tui.load_config', return_value=mock_config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            
            # Call add_row correctly
            builder = app.query_one("#search-builder")
            builder.add_row("AND")
            await pilot.pause(ASYNC_PAUSE * 2)
            
            rows = list(builder.query("SearchRow"))
            assert len(rows) == 2
            
            # Click remove on the second row
            remove_btn = rows[1].query_one("#btn-remove", Button)
            remove_btn.press()
            await pilot.pause(ASYNC_PAUSE)
            
            rows = list(builder.query("SearchRow"))
            assert len(rows) == 1

def test_tui_check_scroll_bottom_exception(mock_config):
    from src.tui import ScraperApp
    from unittest.mock import patch
    with patch('src.tui.load_config', return_value=mock_config):
        app = ScraperApp()
        app._check_scroll_bottom(100)

@pytest.mark.asyncio
async def test_tui_execute_search_not_mounted(mock_config):
    from src.tui import ScraperApp
    from unittest.mock import patch
    with patch('src.tui.load_config', return_value=mock_config):
        app = ScraperApp()
        await app.execute_search()
        
@pytest.mark.asyncio
async def test_tui_execute_search_no_list_view(mock_config):
    from src.tui import ScraperApp
    from unittest.mock import patch
    with patch('src.tui.load_config', return_value=mock_config):
        app = ScraperApp()
        app.is_mounted = True
        await app.execute_search()

@pytest.mark.asyncio
async def test_tui_on_input_submitted(mock_config):
    from src.tui import ScraperApp
    from textual.widgets import Input
    from unittest.mock import patch, AsyncMock
    with patch('src.tui.load_config', return_value=mock_config):
        app = ScraperApp()
        app.execute_search = AsyncMock()
        await app.on_input_submitted(Input.Submitted(Input(), "test"))
        app.execute_search.assert_not_called()  # search only on explicit button click

@pytest.mark.asyncio
async def test_tui_clear_pending_command(tmp_path):
    from src.tui import ScraperApp
    from src.database import initialize_database, insert_or_update_item, get_connection
    from unittest.mock import patch
    
    db_path = str(tmp_path / "tui_clear.db")
    initialize_database(db_path)
    
    # 1. Pending (should be removed)
    insert_or_update_item(db_path, {"workshop_id": 1, "status": None, "api_fetched_at": None})
    # 2. Not Pending (should remain)
    insert_or_update_item(db_path, {"workshop_id": 2, "status": 200, "api_fetched_at": 1672531200})
    
    mock_config = {
        "database": {"path": db_path},
        "logging": {"level": "INFO"}
    }
    
    with patch('src.tui.load_config', return_value=mock_config):
        app = ScraperApp()
        
        async with app.run_test() as pilot:
            # Trigger the action
            app.action_clear_pending()
            await pilot.pause()
            
            # Verify DB state
            conn = get_connection(db_path)
            ids = [row["workshop_id"] for row in conn.execute("SELECT workshop_id FROM workshop_items")]
            conn.close()
            
            assert ids == [2]
            assert 1 not in ids


@pytest.mark.asyncio
async def test_tui_detail_priority_applied_once_per_pane_load(mock_config, mock_results):
    """Pane load applies detail priority once; the 2-second refresh does not.

    Regression: the bumps lived in the list-highlight handler, which fires on
    every highlight move, so arrowing through the list re-queued every item it
    passed at detail priority.
    """
    def get_details_mock(db, wid):
        for r in mock_results:
            if r["workshop_id"] == wid:
                return r
        return None

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.search_items', return_value=mock_results), \
         patch('src.tui.get_item_details', side_effect=get_details_mock), \
         patch('src.tui.get_all_authors', return_value=["Author A"]), \
         patch('src.tui.bump_api_priority_for_detail') as mock_api_bump, \
         patch('src.tui.bump_web_priority_for_detail'), \
         patch('src.tui.bump_image_priority_for_detail'), \
         patch('src.tui.bump_translation_for_detail'):

        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)

            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.pause(ASYNC_PAUSE)

            assert mock_api_bump.call_count == 1, "pane load applies detail priority exactly once"

            # Re-highlighting the item the pane already holds must not re-bump.
            # This is the case the old highlight-handler placement got wrong: it
            # bumped on every Highlighted event, not on every pane change.
            from textual.widgets import ListView as _LV
            app.post_message(_LV.Highlighted(list_view, list_view.children[0]))
            await pilot.pause(ASYNC_PAUSE)
            assert mock_api_bump.call_count == 1, "a repeat highlight must not re-queue the item"

            from src.tui import DetailsPane
            pane = app.query_one("#item-details", DetailsPane)
            await pane.refresh_data()
            await pilot.pause(ASYNC_PAUSE)

            assert mock_api_bump.call_count == 1, "the refresh poll must not re-queue the item"


# --------------------------------------------------------------------------
# stats screen: tiered streaming and per-tier throttle
# --------------------------------------------------------------------------

#: One fake payload per tier, shaped exactly as `metrics.compute_tier` returns,
#: so the screen's renderers run for real.
_FAKE_TIER_VALUES = {
    metrics.INSTANT: {
        "totals": {"total": 3, "alive": 2, "dead": 1},
        "high_water": 1_700_000_000,
        "app_tracking": [{"appid": 294100, "last_page_scanned": 7, "last_cursor": "abc"}],
    },
    metrics.FAST: {
        "status_counts": [{"status": 200, "count": 2}, {"status": -1, "count": 1}],
        "coverage": {
            "total": 2, "api_fetched": 2, "described": 1,
            "imaged": 1, "translated": 0, "attributed": 1,
        },
        "stuck_work": {"web": 1, "image": 0, "translation": 0, "api": 0},
        "fetch_recency": {"fresh": 1, "stale": 0, "blank": 1},
    },
    metrics.SLOW: {
        "translation_status": {"Translated": 1, "Queued": 1},
        "priority_breakdowns": {
            "translation_priority": [{"prio": 5, "cnt": 2}],
            "needs_image": [],
            "needs_web_scrape": [{"prio": 10, "cnt": 1}],
        },
        "tag_counts": {"Alpha": 2, "Beta": 1},
    },
}


@pytest.mark.asyncio
async def test_stats_screen_streams_tiers_cheapest_first(mock_config):
    """Each tier lands in its own widgets, cheapest first, without wiping the rest."""
    calls = []

    def fake_compute_tier(db_path, tier, params=None):
        calls.append(tier)
        return {
            name: {"value": _FAKE_TIER_VALUES[tier][name], "ms": 1.0, "tier": tier, "note": "fake"}
            for name in metrics.names_in(tier)
        }

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.metrics.compute_tier', side_effect=fake_compute_tier):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app.push_screen(StatsScreen(app.db_path))
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen

            for _ in range(100):
                await pilot.pause(0.02)
                if metrics.SLOW in screen._tier_cost:
                    break

            assert calls[:3] == [metrics.INSTANT, metrics.FAST, metrics.SLOW], \
                "tiers must be computed cheapest first"

            # The instant and fast sections survive the slow tier's arrival.
            totals = str(screen.query_one("#totals-content", Static).render())
            assert "Live items" in totals and "2" in totals
            coverage = str(screen.query_one("#coverage-content", Static).render())
            assert "API data" in coverage and "100.0%" in coverage
            stuck = str(screen.query_one("#stuck-content", Static).render())
            assert "dead item(s) are still flagged" in stuck
            assert "Web scrape" in stuck
            translation = str(screen.query_one("#translation-stats-content", Static).render())
            assert "Translated" in translation
            priority = str(screen.query_one("#priority-stats-content", Static).render())
            assert "Translation queue" in priority and "waiting" in priority

            costs = str(screen.query_one("#tier-costs", Static).render())
            assert metrics.SLOW in costs and "ms" in costs, \
                "the screen must report what the slow tier cost"

            assert screen.query_one("#app-stats-table", DataTable).row_count == 1
            assert screen.query_one("#tag-stats-table", DataTable).row_count == 2


def test_stats_refresh_is_per_tier_not_global(monkeypatch):
    """A slow tier's long interval must not hold back a cheap, overdue tier."""
    screen = StatsScreen("unused.db")
    started = []
    monkeypatch.setattr(screen, "_start_tier", lambda tier: started.append(tier))
    now = time.monotonic()
    screen._tier_interval = {metrics.INSTANT: 2.0, metrics.FAST: 2.0, metrics.SLOW: 600.0}
    screen._tier_last = {
        metrics.INSTANT: now - 3.0,  # due on its own 2 s interval
        metrics.FAST: now,           # not due
        metrics.SLOW: now - 3.0,     # overdue by wall clock, but its own interval is 10 min
    }

    screen._refresh_due_tiers()

    assert started == [metrics.INSTANT]


def test_tier_interval_scales_with_that_tiers_own_cost():
    screen = StatsScreen("unused.db")
    assert screen._interval_for(10) == screen.MIN_REFRESH_SECONDS
    assert screen._interval_for(100) == pytest.approx(5.0)     # 100 ms x 50
    assert screen._interval_for(2000) == pytest.approx(100.0)  # 2 s x 50


