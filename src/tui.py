import json
import logging
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Input, ListView, ListItem, Static, Label, Select, Button
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from src.database import search_items, get_all_authors, initialize_database, flag_for_translation
from src.config import load_config

class DetailsPane(VerticalScroll):
    """A scrollable pane for viewing workshop item details."""
    item_data = reactive(None)
    show_translated = reactive(True)

    def compose(self) -> ComposeResult:
        with Horizontal(id="details-header"):
            yield Label("[b]Item Details[/b]", id="details-header-label")
            yield Button("Show Original", id="btn-toggle-translation", classes="details-btn")
            yield Button("Translate", id="btn-request-translation", classes="details-btn")
        yield Static(id="detail-content")

    def watch_item_data(self, item_data: dict) -> None:
        self.update_content()

    def watch_show_translated(self, show_translated: bool) -> None:
        self.update_content()

    def update_content(self) -> None:
        if not self.item_data:
            self.query_one("#detail-content", Static).update("Select an item to see details.")
            self.query_one("#btn-toggle-translation").display = False
            self.query_one("#btn-request-translation").display = False
            return

        item = self.item_data
        
        # Determine which fields to show based on toggle and existence
        display_translated = self.show_translated and item.get("dt_translated")
        
        title = item.get("title_en") if display_translated and item.get("title_en") else item.get("title", "N/A")
        
        # Descriptions fallback chain
        if display_translated:
            desc = item.get("extended_description_en") or item.get("short_description_en")
            if not desc:
                desc = item.get("extended_description") or item.get("short_description") or "N/A"
        else:
            desc = item.get("extended_description") or item.get("short_description") or "N/A"

        # Toggle Button visibility and label
        toggle_btn = self.query_one("#btn-toggle-translation")
        if item.get("dt_translated"):
            toggle_btn.display = True
            toggle_btn.label = "Show Original" if self.show_translated else "Show Translation"
        else:
            toggle_btn.display = False

        # Request Translation Button visibility
        req_btn = self.query_one("#btn-request-translation")
        req_btn.display = True

        tags = item.get("tags", "[]")
        tags_list = []
        try:
            parsed = json.loads(tags) if isinstance(tags, str) else tags
            tags_list = [str(t.get("tag") if isinstance(t, dict) else t) for t in (parsed if isinstance(parsed, list) else [])]
        except: pass

        content = [
            f"[b][u]{title}[/u][/b]",
            f"ID: {item.get('workshop_id', 'N/A')}",
            f"Creator: {item.get('creator', 'N/A')}",
            f"AppID: {item.get('consumer_appid', 'N/A')}",
            f"Language ID: {item.get('language', 'N/A')}",
            f"Tags: {', '.join(tags_list)}",
            "",
            "[b]Description:[/b]",
            desc
        ]
        self.query_one("#detail-content", Static).update("\n".join(content))

class WorkshopItem(ListItem):
    """A list item representing a workshop item."""
    def __init__(self, item_data: dict):
        super().__init__()
        self.item_data = item_data

    def compose(self) -> ComposeResult:
        title = self.item_data.get("title", "Unknown Title")
        wid = self.item_data.get("workshop_id", "Unknown ID")
        creator = self.item_data.get("creator", "Unknown Creator")
        appid = self.item_data.get("consumer_appid", "Unknown AppID")
        yield Label(f"[b]{title}[/b] ({wid})")
        yield Label(f"By: {creator} | AppID: {appid}")

class ScraperApp(App):
    """A Terminal GUI for searching the Steam Workshop database."""
    CSS = """
    Screen {
        layout: vertical;
    }
    #search-container {
        height: auto;
        margin: 1;
        padding: 1;
        border: solid $accent;
    }
    .search-row {
        height: 3;
        margin-bottom: 1;
    }
    .search-input-w {
        width: 1fr;
        border: solid $primary;
    }
    .search-input-w:focus {
        border: solid $secondary;
    }
    Select {
        width: 1fr;
        border: solid $primary;
    }
    Select:focus {
        border: solid $secondary;
    }
    #main-container {
        layout: horizontal;
    }
    #results-list {
        width: 40%;
        border: solid green;
    }
    #details-container {
        width: 60%;
        border: solid blue;
        padding: 1;
        layout: vertical;
    }
    #item-details {
        height: 1fr;
    }
    #btn-jump-author {
        margin-top: 1;
        display: none;
    }
    #details-header {
        height: 3;
        margin-bottom: 1;
        border-bottom: solid $primary;
    }
    #details-header-label {
        width: 1fr;
        content-align: left middle;
    }
    .details-btn {
        margin-left: 1;
        min-width: 16;
    }
    """

    def __init__(self, config_path: str = "config.yaml"):
        super().__init__()
        # Force a light theme to guarantee high contrast on terminal emulators like PuTTY
        self.theme = "textual-light"
        
        try:
            self.config = load_config(config_path)
        except FileNotFoundError:
            self.config = {"database": {"path": "workshop.db"}}
        self.db_path = self.config["database"]["path"]
        initialize_database(self.db_path)
        self.current_item_creator = None

    def on_mount(self) -> None:
        """Run an empty search on startup to populate the list."""
        self.call_after_refresh(self.execute_search)

    def compose(self) -> ComposeResult:
        yield Header()
        
        # Build Author choices from DB
        try:
            authors = get_all_authors(self.db_path)
            author_options = [(a, a) for a in authors if a]
        except Exception:
            author_options = []
        author_options.insert(0, ("Any Author", ""))

        # Create widgets with border titles
        title_in = Input(placeholder="Search Title (supports -exclusion)...", id="search-title", classes="search-input-w")
        title_in.border_title = "Title"
        
        desc_in = Input(placeholder="Search Description...", id="search-desc", classes="search-input-w")
        desc_in.border_title = "Description"
        
        file_in = Input(placeholder="Search Filename...", id="search-filename", classes="search-input-w")
        file_in.border_title = "Filename"
        
        tags_in = Input(placeholder="Search Tags...", id="search-tags", classes="search-input-w")
        tags_in.border_title = "Tags"
        
        author_sel = Select(author_options, prompt="Select Author", id="search-author")
        author_sel.border_title = "Author ID"

        size_in = Input(placeholder="e.g. < 1000", id="search-file-size", classes="search-input-w")
        size_in.border_title = "File Size"
        
        subs_in = Input(placeholder="e.g. >= 50", id="search-subscriptions", classes="search-input-w")
        subs_in.border_title = "Subs"
        
        fav_in = Input(placeholder="Favorited...", id="search-favorited", classes="search-input-w")
        fav_in.border_title = "Favs"
        
        view_in = Input(placeholder="Views...", id="search-views", classes="search-input-w")
        view_in.border_title = "Views"

        search_container = Vertical(
            Horizontal(title_in, desc_in, file_in, classes="search-row"),
            Horizontal(tags_in, author_sel, classes="search-row"),
            Horizontal(size_in, subs_in, fav_in, view_in, classes="search-row"),
            id="search-container"
        )
        search_container.border_title = "Filters"

        results_list = ListView(id="results-list")
        results_list.border_title = "Items"

        details_container = Vertical(
            DetailsPane(id="item-details"),
            Button("Jump to Author", id="btn-jump-author", variant="primary"),
            id="details-container"
        )
        details_container.border_title = "Details"

        yield search_container
        yield Horizontal(
            results_list,
            details_container,
            id="main-container"
        )
        yield Footer()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self.execute_search()

    async def on_select_changed(self, event: Select.Changed) -> None:
        await self.execute_search()

    async def execute_search(self) -> None:
        """Executes a search using all active filters."""
        title_q = self.query_one("#search-title", Input).value
        desc_q = self.query_one("#search-desc", Input).value
        file_q = self.query_one("#search-filename", Input).value
        tags_q = self.query_one("#search-tags", Input).value
        
        author_select = self.query_one("#search-author", Select)
        author_q = ""
        # Robustly handle Textual's Select.BLANK/NULL internal objects
        if isinstance(author_select.value, str):
            author_q = author_select.value

        numeric_filters = {
            "file_size": self.query_one("#search-file-size", Input).value,
            "subscriptions": self.query_one("#search-subscriptions", Input).value,
            "favorited": self.query_one("#search-favorited", Input).value,
            "views": self.query_one("#search-views", Input).value
        }

        results = search_items(
            self.db_path, 
            title_query=title_q, 
            desc_query=desc_q, 
            filename_query=file_q,
            tags_query=tags_q,
            creator=author_q,
            numeric_filters=numeric_filters
        )
        
        list_view = self.query_one("#results-list", ListView)
        await list_view.clear()
        
        import sys
        print(f"SEARCH PARAMS: title={title_q}, author={author_q}, results={len(results)}", file=sys.stderr)
        
        for item in results:
            await list_view.append(WorkshopItem(item))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle selection of an item in the list."""
        if not event.item:
            return
            
        item_data = event.item.item_data
        self.current_item_creator = item_data.get('creator')
        
        detail_pane = self.query_one("#item-details", DetailsPane)
        detail_pane.item_data = item_data
        
        jump_btn = self.query_one("#btn-jump-author", Button)
        if self.current_item_creator:
            jump_btn.display = True
        else:
            jump_btn.display = False

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses (e.g., Jump to Author, Translation)."""
        if event.button.id == "btn-jump-author" and self.current_item_creator:
            # Clear text inputs
            self.query_one("#search-title", Input).value = ""
            self.query_one("#search-desc", Input).value = ""
            
            # Set combo box to the author (this triggers on_select_changed which searches)
            author_select = self.query_one("#search-author", Select)
            author_select.value = self.current_item_creator
        
        elif event.button.id == "btn-toggle-translation":
            detail_pane = self.query_one("#item-details", DetailsPane)
            detail_pane.show_translated = not detail_pane.show_translated
            
        elif event.button.id == "btn-request-translation":
            detail_pane = self.query_one("#item-details", DetailsPane)
            if detail_pane.item_data:
                wid = detail_pane.item_data.get("workshop_id")
                flag_for_translation(self.db_path, wid, priority=10)
                self.notify(f"Item {wid} flagged for high-priority translation.")

def main():
    app = ScraperApp()
    app.run()

if __name__ == "__main__":
    main()
