import pytest
from textual.widgets import Input, ListItem, Static, ListView, Select, Button, DataTable, Label
from textual.containers import VerticalScroll
from tests.conftest import ASYNC_PAUSE
from src.tui import ScraperApp, StatsScreen
from src import metrics
from unittest.mock import patch, MagicMock
import threading
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
         patch('src.tui.get_all_creator_ids', return_value=["Author A", "Author B"]):
        
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
            detail_pane = app.query_one("#detail-pane", DetailsPane)
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
         patch('src.tui.get_all_creator_ids', return_value=["Author A"]):
        
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
            detail_pane = app.query_one("#detail-pane", DetailsPane)
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
         patch('src.tui.get_all_creator_ids', return_value=["Author A"]):
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
         patch('src.tui.get_all_creator_ids', return_value=["Author A"]), \
         patch('src.tui.raise_api_priority_for_detail') as mock_api_bump, \
         patch('src.tui.raise_web_scrape_priority_for_detail'), \
         patch('src.tui.raise_image_priority_for_detail'), \
         patch('src.tui.raise_translation_priority_for_detail'):

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
            pane = app.query_one("#detail-pane", DetailsPane)
            await pane.refresh_data()
            await pilot.pause(ASYNC_PAUSE)

            assert mock_api_bump.call_count == 1, "the refresh poll must not re-queue the item"


# --------------------------------------------------------------------------
# stats screen: one independent chunk per metric
# --------------------------------------------------------------------------

def _coverage_bar(key, label, subsidiary, done, maximum, total, detail=None, empty=None):
    """One coverage bar shaped exactly as `src.metrics` returns it."""
    return {"key": key, "label": label, "subsidiary": subsidiary, "done": done,
            "maximum": maximum, "total": total,
            "pct": None if maximum <= 0 or total <= 0 else round(done / total * 100, 1),
            "detail": detail, "empty": empty}


def _fake_coverage(total=2, filtered_total=1):
    def bars(scope_total, api_done, described, imaged, attributed):
        return [
            _coverage_bar("api_fetched", "API Data", False, api_done, scope_total, scope_total),
            _coverage_bar("translations", "Translations", True, 0, 1, scope_total,
                          "reachable 1 of 2 (50.0%): non-ASCII title/short-description "
                          "fields of filter-selected items; 1 filter-selected items need none",
                          metrics.NOTHING_TO_TRANSLATE),
            _coverage_bar("described", "Extended Web", False, described, scope_total, scope_total,
                          "reachable 2 of 2 (100.0%): 0 scraped pages answered with no description"),
            _coverage_bar("web_translated", "Extended Web Translation", True, 0, 1, scope_total,
                          "reachable 1 of 2 (50.0%): non-ASCII descriptions of scraped items",
                          metrics.NOTHING_TO_TRANSLATE),
            _coverage_bar("imaged", "Images", False, imaged, scope_total, scope_total),
            _coverage_bar("attributed", "Creator", False, attributed, scope_total, scope_total),
            _coverage_bar("creator_translated", "Creator Translation", True, 0, 1, scope_total,
                          "reachable 1 of 2 (50.0%): items whose creator's name is non-ASCII",
                          metrics.NOTHING_TO_TRANSLATE),
        ]
    return {
        "total": total,
        "bars": bars(total, 2, 1, 1, 1),
        "filtered": {
            "total": filtered_total,
            "bars": bars(filtered_total, 1, 0, 1, 1),
            "appids": [294100], "with_filters": [294100], "restricting": [294100],
            "unreadable": [],
        },
    }


#: One fake value per metric, shaped exactly as the metric returns it, so the
#: screen's real per-metric renderers run.
_FAKE_METRIC_VALUES = {
    "high_water": 1_700_000_000,
    "totals": {"total": 3, "alive": 2, "dead": 1},
    "app_tracking": [{"appid": 294100, "last_cursor": "abc"}],
    "status_counts": [{"status": 200, "count": 2}, {"status": -1, "count": 1}],
    "stuck_work": {"web": 1, "image": 0, "translation": 0, "api": 0},
    "dead_queued": 1,
    "queued_nowhere": 2,
    "fetch_recency": {"fresh": 1, "stale": 0, "blank": 1},
    "coverage": _fake_coverage(),
    "translation_status": {"Translated": 1, "Queued": 1},
    "tag_counts": {"Alpha": 2, "Beta": 1},
    "priority_breakdowns": {
        "translation_priority": [{"prio": 5, "cnt": 2}],
        "needs_image": [],
        "needs_web_scrape": [{"prio": 10, "cnt": 1}],
    },
    "web_throughput": {"hour": 2, "day": 5, "last_success": 1_700_000_000},
    "image_throughput": {"hour": 1, "day": 3, "last_success": 1_700_000_000},
    # The no-history shape: a queue whose completion column holds no stamp.
    "translation_throughput": {"hour": None, "day": None, "last_success": None},
    "queue_eta": {
        "window_seconds": 86400, "paused_seconds": 3600, "sweep_inflow": 4,
        "queues": {
            "api": {"outstanding": 40, "completed": 0, "active_seconds": 86400,
                    "per_hour": None, "per_day": None, "eta_seconds": None,
                    "uncertainty_pct": None, "gross_completed": 0,
                    "inflow_subtracted": 4, "basis": "net", "honours_pause": False},
            "web": {"outstanding": 100, "completed": 4, "active_seconds": 82800,
                    "per_hour": 0.17, "per_day": 4.2, "eta_seconds": 86400.0,
                    "uncertainty_pct": 50.0, "gross_completed": 4,
                    "inflow_subtracted": 0, "basis": "gross", "honours_pause": True},
            "image": {"outstanding": 0, "completed": 3, "active_seconds": 82800,
                      "per_hour": 0.0, "per_day": 0.0, "eta_seconds": 0.0,
                      "uncertainty_pct": None, "gross_completed": 3,
                      "inflow_subtracted": 0, "basis": "gross", "honours_pause": True},
            "translation": {"outstanding": 5, "completed": 1, "active_seconds": 86400,
                            "per_hour": 0.04, "per_day": 1.0, "eta_seconds": 432000.0,
                            "uncertainty_pct": 100.0, "gross_completed": 1,
                            "inflow_subtracted": 0, "basis": "gross",
                            "honours_pause": False},
        },
    },
}


def _fake_iter_metrics(record=None):
    """A stand-in for `metrics.iter_metrics` that yields every requested metric."""
    def run(db_path, names=None, params=None):
        order = list(names) if names is not None else metrics.all_names()
        if record is not None:
            record.append(order)
        for name in order:
            yield name, {
                "value": _FAKE_METRIC_VALUES[name],
                "ms": 1.0,
                "note": metrics.REGISTRY[name].note,
                "seed_ms": metrics.REGISTRY[name].seed_ms,
            }
    return run


#: Metrics the TUI deliberately draws through a widget other than a
#: `METRIC_CONTENT_IDS` Static. An exemption needs a reason here, so a new metric
#: cannot be left unwired by silence -- the guard test below walks the catalogue
#: and names any other metric that is missing its label or content id.
TUI_RENDER_EXEMPTIONS = {
    "app_tracking": "special-cased onto a DataTable in `_compose_metric_section`",
    "tag_counts": "owns the right-hand column, not a scrolling chunk",
}


def test_every_registered_metric_has_the_wiring_both_front_ends_need():
    """A metric registered in `src/metrics.py` must be drawable by both panels.

    The web panel discovers metrics from the catalogue and falls back to a JSON
    dump for a value it has no renderer for, so its requirement is the catalogue
    entry itself (name and note). The TUI has no such fallback: `_compose_metric_section`
    indexes `METRIC_CONTENT_IDS`, so a registered metric without an entry raises
    `KeyError` inside `compose` and takes the whole statistics screen down. A
    missing renderer branch is quieter -- the chunk simply stays at its
    "Computing…" placeholder -- and is caught by the render test below.

    Every gap is reported with the metric's name and the piece that is absent,
    so the reader does not have to reproduce the crash to find it.
    """
    catalogue = {m["name"]: m for m in metrics.catalogue()}
    gaps = []
    for name in metrics.all_names():
        entry = catalogue.get(name)
        if entry is None or not entry.get("note"):
            gaps.append(f"{name}: no web catalogue entry with a note")
        if name in TUI_RENDER_EXEMPTIONS:
            continue
        if name not in StatsScreen.METRIC_LABELS:
            gaps.append(f"{name}: no StatsScreen.METRIC_LABELS heading")
        if name not in StatsScreen.METRIC_CONTENT_IDS:
            gaps.append(
                f"{name}: no StatsScreen.METRIC_CONTENT_IDS entry "
                "(stats compose would raise KeyError)")
    assert not gaps, (
        "registered metric(s) a front end cannot draw -- add the missing wiring "
        "to src/tui.py, or add an exemption with its reason to "
        "TUI_RENDER_EXEMPTIONS:\n  " + "\n  ".join(gaps))


@pytest.mark.asyncio
async def test_every_registered_metric_renders_in_its_tui_chunk(mock_config):
    """A content id alone is not enough: the metric needs a renderer branch.

    This drives the real screen and the real metrics against the test database,
    so a metric with an id but no `_render_metric` branch is caught by its chunk
    still reading "Computing…" after every metric has landed.
    """
    with patch('src.tui.load_config', return_value=mock_config):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app.push_screen(StatsScreen(app.db_path))
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen
            for _ in range(300):
                await pilot.pause(0.02)
                if len(screen._measured_ms) >= len(metrics.all_names()):
                    break

            gaps = []
            for name in metrics.all_names():
                if name in TUI_RENDER_EXEMPTIONS:
                    continue
                widget_id = StatsScreen.METRIC_CONTENT_IDS.get(name)
                if widget_id is None:
                    continue  # the static guard above names this gap
                text = str(screen.query_one(f"#{widget_id}", Static).render())
                if text in ("Computing…", "[dim]unavailable[/dim]", ""):
                    gaps.append(
                        f"{name}: `_render_metric` left the chunk at {text!r}")
            assert not gaps, (
                "registered metric(s) with no TUI renderer:\n  " + "\n  ".join(gaps))


@pytest.mark.asyncio
async def test_stats_screen_puts_every_metric_in_its_own_chunk(mock_config):
    """Each metric lands in its own widget, in seed order on the first pass."""
    requested = []

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.metrics.iter_metrics', side_effect=_fake_iter_metrics(requested)):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app.push_screen(StatsScreen(app.db_path))
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen
            for _ in range(200):
                await pilot.pause(0.02)
                if len(screen._measured_ms) >= len(metrics.all_names()):
                    break

            assert requested and requested[0] == metrics.all_names(), \
                "the first pass must request metrics in seed order"

            totals = str(screen.query_one("#totals-content", Static).render())
            assert "Live items" in totals and "2" in totals
            coverage = str(screen.query_one("#coverage-content", Static).render())
            assert "API Data" in coverage and "100.0%" in coverage
            assert "Extended Web" in coverage
            stuck = str(screen.query_one("#stuck-content", Static).render())
            assert "dead item(s) are still flagged" in stuck
            assert "Web scrape" in stuck
            translation = str(screen.query_one("#translation-stats-content", Static).render())
            assert "Translated" in translation
            priority = str(screen.query_one("#priority-stats-content", Static).render())
            assert "Translation queue" in priority and "waiting" in priority
            assert screen.query_one("#app-stats-table", DataTable).row_count == 1
            assert screen.query_one("#tag-stats-table", DataTable).row_count == 2

            # One section per metric that is a handful of numbers, no tier
            # grouping, and each shows its cost. Tags are the exception: they are
            # a long table and keep a column of their own to the right, so they
            # own a widget without owning a chunk in the scrolling list.
            assert len(screen.query(".stats-section")) == len(metrics.all_names()) - 1
            assert len(screen.query("#tier-costs")) == 0
            label = str(screen.query_one("#stats-label-totals", Label).render())
            assert "1.0 ms" in label


@pytest.mark.asyncio
async def test_stats_screen_keeps_tags_in_their_own_right_hand_column(mock_config):
    """Tags are a long list, so they get a column rather than a section.

    Regression: the per-metric rework folded every chunk into one scrolling
    column, which pushed the tag table in among the counts and left the sections
    below it off the screen. Tags belonged on the right, filling the height.
    """
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.metrics.iter_metrics', side_effect=_fake_iter_metrics([])):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app.push_screen(StatsScreen(app.db_path))
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen

            assert screen.query("#stats-right-col #tag-stats-table"), \
                "the tag table must live in the right-hand column"
            assert not screen.query("#stats-scroll #tag-stats-table"), \
                "the tag table must not be one of the scrolling metric chunks"

            # Every other metric is still a chunk in the list.
            for name in metrics.all_names():
                if name == StatsScreen.TAG_METRIC:
                    continue
                assert screen.query(f"#chunk-{name}"), f"{name} lost its section"


@pytest.mark.asyncio
async def test_stats_screen_renders_the_handoff_counters(mock_config):
    """The handoff detectors are registered metrics, so the TUI must draw them.

    This is the seam that catches a metric added to ``src/metrics.py`` without a
    matching ``METRIC_CONTENT_IDS``/render branch: composing the screen would raise.
    """
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.metrics.iter_metrics', side_effect=_fake_iter_metrics([])):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app.push_screen(StatsScreen(app.db_path))
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen
            # The worker applies metrics as they finish; wait for both counters.
            for _ in range(200):
                await pilot.pause(0.02)
                if {"queued_nowhere", "dead_queued"} <= set(screen._measured_ms):
                    break

            assert screen.query("#chunk-queued_nowhere"), "queued_nowhere has no section"
            assert screen.query("#chunk-dead_queued"), "dead_queued has no section"
            queued = str(screen.query_one("#queued-nowhere-content", Static).render())
            assert "2" in queued and "not complete" in queued
            dead = str(screen.query_one("#dead-queued-content", Static).render())
            assert "1" in dead and "dead item" in dead


def test_handoff_counters_show_an_all_clear_at_zero():
    """A metric meant to read zero should say so, not print a bare 0."""
    zero = StatsScreen._format_handoff_metric(0, "nothing stranded", "item(s) stranded")
    assert "green" in zero and "0" not in zero
    bad = StatsScreen._format_handoff_metric(3, "nothing stranded", "item(s) stranded")
    assert "red" in bad and "3" in bad and "item(s) stranded" in bad


def test_stats_request_order_learns_from_the_measured_costs():
    """Seed order until something is measured, then the durations actually seen."""
    screen = StatsScreen("unused.db")
    assert screen._request_order() == metrics.all_names()

    screen._measured_ms["status_counts"] = 0.5
    screen._measured_ms["priority_breakdowns"] = 900.0
    order = screen._request_order()
    assert order[0] == "status_counts"
    assert order[-1] == "priority_breakdowns"

    # With every metric measured, the order is purely by measured cost.
    names = metrics.all_names()
    screen._measured_ms = {name: float(len(names) - i) for i, name in enumerate(names)}
    assert screen._request_order() == list(reversed(names))


def test_stats_refresh_is_per_metric_not_global():
    """A slow metric's long interval must not hold back a fast, overdue metric."""
    screen = StatsScreen("unused.db")
    names = metrics.all_names()
    now = time.monotonic()
    screen._intervals = {name: 2.0 for name in names}
    screen._intervals["tag_counts"] = 600.0
    screen._ran_at = {name: now for name in names}
    screen._ran_at["high_water"] = now - 3.0   # due on its own 2 s interval
    screen._ran_at["tag_counts"] = now - 3.0   # overdue by wall clock, but its own interval is 10 min

    assert screen._due_metrics(now) == ["high_water"]


def test_stats_does_not_restart_a_metric_that_is_still_computing():
    screen = StatsScreen("unused.db")
    now = time.monotonic()
    screen._inflight = {"totals"}
    assert screen._is_due("totals", now) is False
    assert screen._is_due("coverage", now) is True


def test_metric_interval_scales_with_its_own_measured_cost():
    screen = StatsScreen("unused.db")
    assert screen._interval_for(10) == screen.MIN_REFRESH_SECONDS
    assert screen._interval_for(100) == pytest.approx(5.0)     # 100 ms x 50
    assert screen._interval_for(2000) == pytest.approx(100.0)  # 2 s x 50


@pytest.mark.asyncio
async def test_tag_compaction_runs_off_the_ui_thread(mock_config):
    """The tag-frequency write must not run on the render path."""
    threads = []

    def fake_compact(db_path, tag_counts):
        threads.append(threading.current_thread())

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.metrics.iter_metrics', side_effect=_fake_iter_metrics()), \
         patch('src.database.compact_tag_ids', side_effect=fake_compact):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app.push_screen(StatsScreen(app.db_path))
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen
            for _ in range(200):
                await pilot.pause(0.02)
                if "tag_counts" in screen._measured_ms:
                    break

    assert threads, "compact_tag_ids must run when the tag metric arrives"
    assert all(t is not threading.main_thread() for t in threads), \
        "compact_tag_ids must stay off the UI thread"
