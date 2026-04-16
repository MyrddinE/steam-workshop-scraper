import json
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Input, ListView, ListItem, Static, Label, Select, Button
from textual.containers import Horizontal, Vertical
from src.database import search_items, get_all_authors, initialize_database
from src.config import load_config

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

        details_view = Static("Select an item to see details", id="item-details")
        details_container = Vertical(
            details_view,
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
        if author_select.value and str(author_select.value) not in ["Select.BLANK", "Select.NULL"]:
            author_q = str(author_select.value)

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
        
        detail_pane = self.query_one("#item-details", Static)
        jump_btn = self.query_one("#btn-jump-author", Button)
        
        tags = item_data.get("tags", "[]")
        try:
            tags_list = json.loads(tags)
        except json.JSONDecodeError:
            tags_list = []

        details = [
            f"[b][u]{item_data.get('title', 'N/A')}[/u][/b]",
            f"ID: {item_data.get('workshop_id', 'N/A')}",
            f"Creator: {item_data.get('creator', 'N/A')}",
            f"AppID: {item_data.get('consumer_appid', 'N/A')}",
            f"Tags: {', '.join(tags_list)}",
            "",
            "[b]Description:[/b]",
            item_data.get("extended_description") or item_data.get("short_description") or "N/A"
        ]
        
        detail_pane.update("\n".join(details))
        
        if self.current_item_creator:
            jump_btn.display = True
        else:
            jump_btn.display = False

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses (e.g., Jump to Author)."""
        if event.button.id == "btn-jump-author" and self.current_item_creator:
            # Clear text inputs
            self.query_one("#search-title", Input).value = ""
            self.query_one("#search-desc", Input).value = ""
            
            # Set combo box to the author (this triggers on_select_changed which searches)
            author_select = self.query_one("#search-author", Select)
            author_select.value = self.current_item_creator

def main():
    app = ScraperApp()
    app.run()

if __name__ == "__main__":
    main()
