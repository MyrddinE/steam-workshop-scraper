import pytest
from textual.color import Color
from textual.widgets import Label, Button, Select, ListItem, ListView, Markdown
from src.database import initialize_database
from src.tui import ScraperApp, DetailsPane
from tests.conftest import ASYNC_PAUSE
from unittest.mock import patch

@pytest.fixture
def mock_config(tmp_path):
    """A config pointing at an initialized throwaway database.

    The app queries a database as it mounts -- the search and the Wilson
    percentile computation -- so these tests need a real schema to run against,
    just not the checkout's. ``tmp_path`` gives them one that no other process
    shares and that is removed with the test.
    """
    db_path = str(tmp_path / "test_workshop.db")
    initialize_database(db_path)
    return {
        "database": {"path": db_path},
        "logging": {"level": "INFO"}
    }

@pytest.fixture(autouse=True)
def hermetic_tui_environment(mock_config):
    """Keep every test in this file off the checkout's configuration and database.

    ``ScraperApp.__init__`` reads ``load_config(config_path)`` itself and then
    calls ``initialize_database_with_daemon_stopped`` on the path that yields.
    Unpatched, that is the repository's ``config.yaml`` -- or ``workshop.db`` in
    the repository root when the config is absent -- a gitignored artefact, not a
    fixture. A stale one sent ``test_main_ui_contrast`` down the pending-migration
    path on 2026-09-21 and failed it with ``SystemExit: 2`` for a reason unrelated
    to contrast; the failed run had itself migrated the artefact, so the re-run
    passed. Both reads are patched away here, so the app is built from the
    fixture's ``tmp_path`` database and never runs the migration/daemon
    machinery. Each test then asserts its app came from the mock, and that
    assertion (not this fixture) is what fails if the patch is dropped.
    """
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.initialize_database_with_daemon_stopped'):
        yield

def is_readable(widget):
    """Checks if text is readable by comparing brightness of FG and BG."""
    style = widget.rich_style
    fg = Color.from_rich_color(style.color)
    bg = Color.from_rich_color(style.bgcolor)
    
    # Very simple check: brightness difference should be significant
    diff = abs(fg.brightness - bg.brightness)
    return diff > 0.3 # 0.3 is a loose threshold for basic visibility

@pytest.mark.asyncio
async def test_main_ui_contrast(mock_config):
    """Check contrast of primary static elements."""
    app = ScraperApp()
    # Hermeticity guard: a `ScraperApp` built from the checkout's configuration
    # would not carry the fixture's database path, so this fails if the patches
    # above are ever dropped.
    assert app.config["database"] == mock_config["database"]
    async with app.run_test() as pilot:
        btn = app.query_one("#btn-search")
        assert is_readable(btn)

@pytest.mark.asyncio
async def test_details_pane_contrast(mock_config):
    """Check contrast of the redesigned Details pane elements."""
    results = [{
        "workshop_id": 1, "title": "Test Item", "creator_steamid": "123", 
        "personaname": "Author Name", "file_size": 1024,
        "steam_created_at": 1000, "views": 10, "subscriptions": 5, "favorited": 2,
        "tags": '["Tag1"]', "api_fetched_at": None, "first_seen_at": "2023", "fetch_status": 200
    }]
    
    with patch('src.tui.search_items', return_value=results), \
         patch('src.tui.get_item_details', return_value=results[0]), \
         patch('src.tui.get_all_creator_ids', return_value=[]):
        
        app = ScraperApp()
        # Hermeticity guard, as above.
        assert app.config["database"] == mock_config["database"]
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one(ListView)
            list_view.index = 0
            await pilot.pause(ASYNC_PAUSE)
            
            detail_pane = app.query_one("#detail-pane", DetailsPane)
            title = detail_pane.query_one("#item-title")
            assert is_readable(title)

@pytest.mark.asyncio
async def test_command_palette_contrast(mock_config):
    """Check contrast of items in the Command Palette."""
    app = ScraperApp()
    # Hermeticity guard, as above.
    assert app.config["database"] == mock_config["database"]
    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause(ASYNC_PAUSE)
        
        # Check the top-most screen (should be CommandPalette)
        top_screen = pilot.app.screen_stack[-1]
        from textual.command import CommandPalette
        assert isinstance(top_screen, CommandPalette)
        
        # The palette lists its commands with an empty query, so nothing is
        # typed here: the readability check needs hits, not any particular
        # command, and typing a query would tie this test to one command's
        # label. Batch 4 renamed `Clear Pending Database` and a `clear` query
        # silently stopped matching, which is exactly that coupling.
        await pilot.pause(ASYNC_PAUSE * 2)
        
        # Verify there are hits in the CommandList
        from textual.command import CommandList
        command_list = top_screen.query_one(CommandList)
        assert command_list.option_count > 0
        
        # Check readability of the first hit
        # OptionList rendering is complex, but we can try to find the internal widgets if any,
        # or just check the style of the list itself.
        assert is_readable(command_list)
        
        cp_input = top_screen.query_one("CommandInput")
        assert is_readable(cp_input)

@pytest.mark.asyncio
async def test_select_dropdown_contrast(mock_config):
    """Check contrast of Select dropdown menus."""
    app = ScraperApp()
    # Hermeticity guard, as above.
    assert app.config["database"] == mock_config["database"]
    async with app.run_test() as pilot:
        await pilot.click(Select)
        await pilot.pause(ASYNC_PAUSE)
        
        # Select opens an overlay on the current screen
        from textual.widgets._select import SelectOverlay
        overlay = pilot.app.screen.query_one(SelectOverlay)
        assert is_readable(overlay)
