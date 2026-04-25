import json
import logging
from textual.app import App, ComposeResult
from textual import on, events
from textual.widgets import Header, Footer, Input, ListView, ListItem, Static, Label, Select, Button, Markdown
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from src.database import search_items, get_all_authors, initialize_database, flag_for_translation, get_item_details, save_app_filter
from src.config import load_config
import os
import yaml

def load_tui_state(path: str) -> dict:
    """Loads the TUI state from a YAML file."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def save_tui_state(path: str, state: dict) -> None:
    """Saves the TUI state to a YAML file."""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(state, f, default_flow_style=False)
    except Exception:
        pass

import re

def bbcode_to_markdown(text: str) -> str:
    """Converts common Steam BBCode tags to Markdown."""
    if not text:
        return ""
    
    # 1. Headers
    text = re.sub(r'\[h1\](.*?)\[/h1\]', r'# \1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[h2\](.*?)\[/h2\]', r'## \1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[h3\](.*?)\[/h3\]', r'### \1', text, flags=re.IGNORECASE | re.DOTALL)

    # 2. Basic Formatting
    text = re.sub(r'\[b\](.*?)\[/b\]', r'**\1**', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[i\](.*?)\[/i\]', r'*\1*', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[u\](.*?)\[/u\]', r'\1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[s\](.*?)\[/s\]', r'~~\1~~', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[strike\](.*?)\[/strike\]', r'~~\1~~', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[spoiler\](.*?)\[/spoiler\]', r'[SPOILER: \1]', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[noparse\](.*?)\[/noparse\]', r'`\1`', text, flags=re.IGNORECASE | re.DOTALL)

    # 3. Links, Images, Videos
    text = re.sub(r'\[url=(.*?)\](.*?)\[/url\]', r'[\2](\1)', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[url\](.*?)\[/url\]', r'[\1](\1)', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[img\](.*?)\[/img\]', r'![image](\1)', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[video\](.*?)\[/video\]', r'[Video](\1)', text, flags=re.IGNORECASE | re.DOTALL)

    # 4. Lists
    # Bulleted lists
    text = re.sub(r'\[list\]', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/list\]', '\n', text, flags=re.IGNORECASE)
    # Numbered lists (simplifying olist to numbered list)
    text = re.sub(r'\[olist\]', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/olist\]', '\n', text, flags=re.IGNORECASE)
    # List items: handle [*] item -> * item
    text = re.sub(r'\[\*\]\s*', '* ', text)

    # 5. Tables (Simple conversion - Markdown tables are limited)
    text = re.sub(r'\[table\]', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/table\]', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[tr\]', '| ', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/tr\]', ' |\n', text, flags=re.IGNORECASE)
    text = re.sub(r'\[th\]', ' **', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/th\]', '** |', text, flags=re.IGNORECASE)
    text = re.sub(r'\[td\]', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/td\]', ' |', text, flags=re.IGNORECASE)

    # 6. Quotes and Code
    text = re.sub(r'\[quote=(.*?)\](.*?)\[/quote\]', r'> **\1 said:**\n> \2', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[quote\](.*?)\[/quote\]', r'> \1', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'\[code\](.*?)\[/code\]', r'```\n\1\n```', text, flags=re.IGNORECASE | re.DOTALL)

    # 7. HR
    text = re.sub(r'\[hr\]', '\n---\n', text, flags=re.IGNORECASE)
    
    # Cleanup: remove extra spaces resulting from some conversions
    text = re.sub(r'\|  \|', '|', text)
    
    return text

class DetailsPane(VerticalScroll):
    """A scrollable pane for viewing workshop item details."""
    workshop_id = reactive(None)
    item_data = reactive(None) # Detailed data fetched from DB
    show_translated = reactive(True)

    def compose(self) -> ComposeResult:
        with Horizontal(id="details-header"):
            yield Label("[b]Item Details[/b]", id="details-header-label")
            yield Button("Show Original", id="btn-toggle-translation", classes="details-btn")
            yield Button("Translate", id="btn-request-translation", classes="details-btn")
            
        with Horizontal(id="creator-box"):
            yield Label("[b]Creator:[/b] N/A", id="creator-label")
            yield Button("jump", id="btn-jump-author", variant="primary")
            
        yield Markdown(id="detail-content")

    def on_mount(self) -> None:
        """Setup background refresh to catch translation updates."""
        self.set_interval(2.0, self.refresh_data)

    async def refresh_data(self) -> None:
        """Fetches fresh data from DB for the current workshop_id."""
        if self.workshop_id:
            # We access db_path via self.app (ScraperApp instance)
            fresh_data = get_item_details(self.app.db_path, self.workshop_id)
            if fresh_data:
                self.item_data = fresh_data

    async def watch_workshop_id(self, workshop_id: int) -> None:
        """When ID changes, clear old data and fetch new."""
        self.item_data = None
        if workshop_id:
            await self.refresh_data()

    def watch_item_data(self, item_data: dict) -> None:
        self.update_content()

    def watch_show_translated(self, show_translated: bool) -> None:
        self.update_content()

    def update_content(self) -> None:
        if not self.item_data:
            self.query_one("#detail-content", Markdown).update("Select an item to see details.")
            self.query_one("#creator-label", Label).update("[b]Creator:[/b] N/A")
            self.query_one("#btn-toggle-translation").display = False
            self.query_one("#btn-request-translation").display = False
            self.query_one("#btn-jump-author").display = False
            return

        item = self.item_data
        
        # Determine which fields to show based on toggle and existence
        display_translated = self.show_translated and item.get("dt_translated")
        
        title = item.get("title_en") if display_translated and item.get("title_en") else item.get("title", "N/A")
        
        # User name prioritization
        creator_name = item.get("personaname_en") if display_translated and item.get("personaname_en") else item.get("personaname")
        if not creator_name:
            creator_name = str(item.get("creator", "N/A"))
            
        self.query_one("#creator-label", Label).update(f"[b]Creator:[/b] {creator_name}")
        
        jump_btn = self.query_one("#btn-jump-author", Button)
        if item.get("creator"):
            jump_btn.display = True
        else:
            jump_btn.display = False

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
        
        def format_ts(ts):
            if not ts: return "N/A"
            try:
                import datetime
                return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
            except:
                return "N/A"

        created_str = format_ts(item.get('time_created'))
        updated_str = format_ts(item.get('time_updated'))
        
        date_str = f"**Created:** {created_str}"
        if updated_str != "N/A" and updated_str != created_str:
            date_str += f" | **Updated:** {updated_str}"

        # Convert to Markdown
        md_content = [
            f"# {bbcode_to_markdown(title)}",
            f"**ID:** {item.get('workshop_id', 'N/A')}  ",
            f"**AppID:** {item.get('consumer_appid', 'N/A')}  ",
            f"**Language ID:** {item.get('language', 'N/A')}  ",
            date_str + "  ",
            f"**Views:** {item.get('views', 0):,} | **Subscribers:** {item.get('subscriptions', 0):,} | **Favorites:** {item.get('favorited', 0):,}  ",
            f"**Tags:** {', '.join(tags_list)}  ",
        ]
        
        if item.get("translation_priority", 0) > 0:
            md_content.append("\n*Queued for translation...*")
        
        md_content.extend([
            "\n---",
            "### Description",
            bbcode_to_markdown(desc)
        ])
        self.query_one("#detail-content", Markdown).update("\n".join(md_content))

class WorkshopItem(ListItem):
    """A list item representing a workshop item."""
    def __init__(self, item_data: dict):
        super().__init__()
        self.item_data = item_data

    def compose(self) -> ComposeResult:
        wid = self.item_data.get("workshop_id", "N/A")

        # Prefer translated title for the list view
        title = self.item_data.get("title_en") or self.item_data.get("title", "Unknown Title")

        # Prefer translated persona name
        creator = self.item_data.get("personaname_en") or self.item_data.get("personaname") or self.item_data.get("creator", "Unknown Creator")

        appid = self.item_data.get("consumer_appid", "Unknown AppID")
        yield Label(f"[b]{title}[/b] ({wid})")
        yield Label(f"By: {creator} | AppID: {appid}")


class SearchRow(Horizontal):
    """A single row in the search builder."""
    def __init__(self, fields: list[str], operators: dict[str, list[str]], is_first: bool = False):
        super().__init__(classes="search-row")
        self.fields = fields
        self.operators_map = operators
        self.is_first = is_first

    def compose(self) -> ComposeResult:
        field_options = [(f, f) for f in self.fields]
        yield Select(field_options, prompt="Field", id="field-select", classes="row-field")
        yield Select([], prompt="Op", id="op-select", classes="row-op")
        yield Input(placeholder="Value", id="value-input", classes="row-input")
        yield Button("AND", id="btn-and", variant="default", classes="row-btn")
        yield Button("OR", id="btn-or", variant="default", classes="row-btn")
        if not self.is_first:
            yield Button("X", id="btn-remove", variant="error", classes="row-btn-remove")
        else:
            # Placeholder to keep alignment
            yield Static("", classes="row-btn-remove")

    def on_mount(self) -> None:
        self.query_one("#field-select", Select).value = self.fields[0]

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "field-select":
            field = str(event.value)
            # Determine field type to show relevant operators
            if field == "Author ID" or field == "Workshop ID" or field == "AppID":
                op_type = "id"
            elif field in ["File Size", "Subs", "Favs", "Views", "Language ID"]:
                op_type = "numeric"
            else:
                op_type = "text"
                
            ops = self.operators_map[op_type]
            op_select = self.query_one("#op-select", Select)
            op_select.set_options([(o.replace("_", " "), o) for o in ops])
            op_select.value = ops[0]

class SearchBuilder(VerticalScroll):
    """A container for multiple SearchRows."""
    def compose(self) -> ComposeResult:
        self.fields = [
            "Title", "Description", "Filename", "Tags", "Author ID",
            "File Size", "Subs", "Favs", "Views", "Workshop ID", "AppID", "Language ID"
        ]
        self.operators = {
            "text": ["contains", "does_not_contain", "is", "is_not", "is_empty", "is_not_empty"],
            "numeric": ["is", "is_not", "gt", "lt", "gte", "lte", "is_empty", "is_not_empty"],
            "id": ["is", "is_not"]
        }
        yield SearchRow(self.fields, self.operators, is_first=True)

    def add_row(self, logic: str) -> None:
        # Get the last row to set its logical operator display if needed? 
        # Actually the buttons in the row that was clicked should probably be used.
        new_row = SearchRow(self.fields, self.operators)
        self.mount(new_row)
        new_row.logic = logic # Custom attribute to store logic from previous row

    def set_filters(self, filters: list[dict]) -> None:
        """Populates the builder with a given list of filters."""
        for row in list(self.query(SearchRow)):
            row.remove()
            
        if not filters:
            self.mount(SearchRow(self.fields, self.operators, is_first=True))
            return

        for i, f in enumerate(filters):
            is_first = (i == 0)
            row = SearchRow(self.fields, self.operators, is_first=is_first)
            if not is_first:
                row.logic = f.get("logic", "AND")
            self.mount(row)

            # Use closure to capture loop variables correctly
            def apply_values(r=row, field=f.get("field"), op=f.get("op"), val=f.get("value")):
                try:
                    field_select = r.query_one("#field-select", Select)
                    field_select.value = field
                    
                    # Manually update options so they are synchronously available
                    if field == "Author ID" or field == "Workshop ID" or field == "AppID":
                        op_type = "id"
                    elif field in ["File Size", "Subs", "Favs", "Views", "Language ID"]:
                        op_type = "numeric"
                    else:
                        op_type = "text"
                    ops = self.operators[op_type]
                    op_select = r.query_one("#op-select", Select)
                    op_select.set_options([(o.replace("_", " "), o) for o in ops])
                    
                    op_select.value = op
                    
                    value_input = r.query_one("#value-input", Input)
                    value_input.value = val
                except Exception as e:
                    import logging
                    logging.error(f"apply_values failed: {e}", exc_info=True)
                    
            self.app.call_after_refresh(apply_values)

    def get_filters(self) -> list[dict]:
        filters = []
        rows = self.query(SearchRow)
        for i, row in enumerate(rows):
            f = {
                "field": row.query_one("#field-select", Select).value,
                "op": row.query_one("#op-select", Select).value,
                "value": row.query_one("#value-input", Input).value,
            }
            if i > 0:
                f["logic"] = getattr(row, "logic", "AND")
            filters.append(f)
        return filters

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
    #search-builder {
        height: auto;
        max-height: 12; /* Roughly 4 rows */
    }
    .search-row {
        height: 3;
        margin-bottom: 0;
    }
    .search-buttons {
        height: 3;
        padding-top: 1;
    }
    .search-buttons Button {
        width: auto; # Let buttons size naturally
        margin-left: 1;
    }
    .row-field { width: 20%; }
    .row-op { width: 20%; }
    .row-input { width: 35%; }
    .row-btn { width: 8%; min-width: 0; margin-left: 1; }
    .row-btn-remove { width: 5%; min-width: 0; margin-left: 1; }

    #main-container {
        layout: horizontal;
    }
    #results-column {
        width: 40%;
        layout: vertical;
    }
    #results-list {
        height: 1fr;
        border: solid green;
    }
    #sort-container {
        height: 3;
        layout: horizontal;
        border: solid $primary;
        margin-bottom: 1;
    }
    .sort-select { width: 60%; }
    .sort-order { width: 40%; }

    #details-container {
        width: 60%;
        border: solid blue;
        padding: 1;
        layout: vertical;
    }
    #item-details {
        height: 1fr;
    }
    #creator-box {
        height: auto;
        layout: horizontal;
        align: left middle;
        margin-bottom: 1;
    }
    #creator-label {
        content-align: left middle;
        margin-right: 1;
    }
    #btn-jump-author {
        display: none;
        height: 1;
        min-width: 8;
        padding: 0 1;
        border: none;
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
        
        # Pagination state
        self.current_offset = 0
        self.has_more_results = True
        self.is_loading = False
        
        # UI State recovery
        # We use a hidden file to avoid cluttering the working directory
        self.state_file = ".tui_state.yaml"
        self._initial_state = load_tui_state(self.state_file)
        self._restored_scroll_y = self._initial_state.get("scroll_y", 0)
        self._restored_selected_id = self._initial_state.get("selected_workshop_id", None)
        self._has_restored_state = False

    def save_state(self) -> None:
        """Saves current UI state to disk."""
        if not self.is_mounted or not self._has_restored_state:
            return
            
        try:
            builder = self.query_one("#search-builder", SearchBuilder)
            filters = builder.get_filters()
            sort_by = self.query_one("#sort-by", Select).value
            sort_order = self.query_one("#sort-order", Select).value
            list_view = self.query_one("#results-list", ListView)
            
            selected_id = None
            if list_view.index is not None and list_view.index < len(list_view.children):
                item = list_view.children[list_view.index]
                if hasattr(item, 'item_data'):
                    selected_id = item.item_data.get("workshop_id")
            
            state = {
                "filters": filters,
                "sort_by": sort_by,
                "sort_order": sort_order,
                "scroll_y": list_view.scroll_y,
                "selected_workshop_id": selected_id
            }
            save_tui_state(self.state_file, state)
        except Exception:
            pass

    def on_mount(self) -> None:
        """Initialize the UI and recover state."""
        # Recover sorting and filters
        if self._initial_state:
            try:
                if "sort_by" in self._initial_state:
                    self.query_one("#sort-by", Select).value = self._initial_state["sort_by"]
                if "sort_order" in self._initial_state:
                    self.query_one("#sort-order", Select).value = self._initial_state["sort_order"]
                if "filters" in self._initial_state:
                    builder = self.query_one("#search-builder", SearchBuilder)
                    builder.set_filters(self._initial_state["filters"])
            except Exception:
                pass
                
        self.call_after_refresh(self.execute_search)
        
        # Watch the scroll_y property to trigger infinite loading
        list_view = self.query_one("#results-list", ListView)
        self.watch(list_view, "scroll_y", self._check_scroll_bottom)

    def _check_scroll_bottom(self, scroll_y: float) -> None:
        self.save_state()
        try:
            list_view = self.query_one("#results-list", ListView)
            if list_view.max_scroll_y == 0:
                return
            if scroll_y >= list_view.max_scroll_y - 5:
                self.run_worker(self.load_more_items())
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        yield Header()
        
        search_builder = SearchBuilder(id="search-builder")
        search_container = Vertical(
            search_builder,
            Horizontal(
                Button("Execute Search", id="btn-execute-search", variant="primary"),
                Button("Save Filter for Scraper", id="btn-save-filter", variant="default"),
                classes="search-buttons"
            ),
            id="search-container"
        )
        search_container.border_title = "Filters"

        sort_options = [
            ("Title", "title"),
            ("File Size", "file_size"),
            ("Subscriptions", "subscriptions"),
            ("Favorited", "favorited"),
            ("Views", "views"),
            ("Workshop ID", "workshop_id"),
            ("Created Time", "time_created"),
            ("Updated Time", "time_updated"),
        ]
        
        sort_container = Horizontal(
            Select(sort_options, value="title", id="sort-by", classes="sort-select"),
            Select([("ASC", "ASC"), ("DESC", "DESC")], value="ASC", id="sort-order", classes="sort-order"),
            id="sort-container"
        )
        sort_container.border_title = "Sort"

        results_list = ListView(id="results-list")
        results_list.border_title = "Items"

        details_container = Vertical(
            DetailsPane(id="item-details"),
            id="details-container"
        )
        details_container.border_title = "Details"

        yield search_container
        yield Horizontal(
            Vertical(
                sort_container,
                results_list,
                id="results-column"
            ),
            details_container,
            id="main-container"
        )
        yield Footer()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self.execute_search()

    async def on_select_changed(self, event: Select.Changed) -> None:
        # Avoid triggering search while initializing Selects
        if event.value is not None:
            if self._has_restored_state:
                self.save_state()
            await self.execute_search()

    async def execute_search(self) -> None:
        """Executes a new search, resetting pagination."""
        if not self.is_mounted:
            return
            
        self.current_offset = 0
        self.has_more_results = True
        
        try:
            list_view = self.query_one("#results-list", ListView)
        except Exception:
            return
        await list_view.clear()
        
        await self.load_more_items()

    async def load_more_items(self) -> None:
        """Fetches the next chunk of items from the database."""
        if self.is_loading or not self.has_more_results:
            return
            
        self.is_loading = True
        
        search_builder = self.query_one("#search-builder", SearchBuilder)
        filters = search_builder.get_filters()
        
        sort_by = self.query_one("#sort-by", Select).value
        sort_order = self.query_one("#sort-order", Select).value

        # Handle potential Select.BLANK
        if not isinstance(sort_by, str): sort_by = "title"
        if not isinstance(sort_order, str): sort_order = "ASC"

        results = search_items(
            self.db_path, 
            filters=filters,
            sort_by=sort_by,
            sort_order=sort_order,
            summary_only=True,
            limit=50,
            offset=self.current_offset
        )
        
        list_view = self.query_one("#results-list", ListView)
        
        for item in results:
            await list_view.append(WorkshopItem(item))
            
        self.current_offset += len(results)
        
        if len(results) < 50:
            self.has_more_results = False
            
        self.is_loading = False

        if not self._has_restored_state:
            self._has_restored_state = True
            
            def restore_state():
                try:
                    if self._restored_selected_id:
                        for i, item in enumerate(list_view.children):
                            if getattr(item, 'item_data', {}).get("workshop_id") == self._restored_selected_id:
                                list_view.index = i
                                break
                                
                    if self._restored_scroll_y > 0:
                        list_view.scroll_y = self._restored_scroll_y
                except Exception:
                    pass

            self.call_after_refresh(restore_state)
        else:
            self.save_state()

    async def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Load more items when scrolling near the bottom of the list."""
        list_view = event.list_view
        if list_view.id == "results-list":
            # If we are within 10 items of the end, fetch more
            if list_view.index is not None and list_view.index >= len(list_view) - 10:
                await self.load_more_items()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle selection of an item in the list."""
        self.save_state()
        if not event.item:
            return
            
        item_data = event.item.item_data
        self.current_item_creator = item_data.get('creator')
        
        detail_pane = self.query_one("#item-details", DetailsPane)
        detail_pane.workshop_id = item_data.get("workshop_id")
        
        jump_btn = self.query_one("#btn-jump-author", Button)
        if self.current_item_creator:
            jump_btn.display = True
        else:
            jump_btn.display = False

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses (e.g., Jump to Author, Translation, Search Builder buttons)."""
        if event.button.id == "btn-execute-search":
            await self.execute_search()
        elif event.button.id == "btn-save-filter":
            await self.action_save_filter_for_scraper()
        elif event.button.id in ("btn-and", "btn-or"):
            logic = "AND" if event.button.id == "btn-and" else "OR"
            self.query_one("#search-builder", SearchBuilder).add_row(logic)
            await self.execute_search()
        
        elif event.button.id == "btn-remove":
            row = event.button.parent
            if isinstance(row, SearchRow):
                row.remove()
                self.call_after_refresh(self.execute_search)

        elif event.button.id == "btn-jump-author" and self.current_item_creator:
            builder = self.query_one("#search-builder", SearchBuilder)
            
            # Clear all current rows
            await builder.query(SearchRow).remove()
            
            # Add a fresh first row
            new_row = SearchRow(builder.fields, builder.operators, is_first=True)
            await builder.mount(new_row)
            
            # Use call_after_refresh to ensure selects are populated
            def setup_author_filter():
                new_row.query_one("#field-select", Select).value = "Author ID"
                new_row.query_one("#op-select", Select).value = "is"
                new_row.query_one("#value-input", Input).value = str(self.current_item_creator)
                self.run_worker(self.execute_search())
            
            self.call_after_refresh(setup_author_filter)
        
        elif event.button.id == "btn-toggle-translation":
            detail_pane = self.query_one("#item-details", DetailsPane)
            detail_pane.show_translated = not detail_pane.show_translated
            
        elif event.button.id == "btn-request-translation":
            detail_pane = self.query_one("#item-details", DetailsPane)
            if detail_pane.item_data:
                item = detail_pane.item_data
                wid = item.get("workshop_id")
                
                # Check if it already has a priority set
                if item.get("translation_priority", 0) > 0:
                    self.notify(f"Item {wid} is already in the translation queue.", severity="warning")
                    return

                flag_for_translation(self.db_path, wid, priority=10)
                
                # Update local state immediately so UI can show 'queued'
                item["translation_priority"] = 10
                detail_pane.update_content()
                
                self.notify(f"Item {wid} flagged for high-priority translation.")

    async def action_save_filter_for_scraper(self) -> None:
        """
        Extracts current filters from the TUI and saves them to the database
        for use by the background scraper.
        """
        builder = self.query_one("#search-builder", SearchBuilder)
        filters = builder.get_filters()
        
        filter_text = ""
        required_tags = []
        excluded_tags = []

        for f in filters:
            field = f.get("field")
            op = f.get("op")
            value = f.get("value", "")
            
            if field == "Title" and op == "contains":
                filter_text = value
            elif field == "Tags" and op == "contains":
                # Split tags by comma or space for simplicity, then add
                for tag in re.split(r'[\s,]+', value):
                    if tag:
                        required_tags.append(tag)
            elif field == "Tags" and op == "does_not_contain":
                for tag in re.split(r'[\s,]+', value):
                    if tag:
                        excluded_tags.append(tag)
            # Numeric filters are ignored as per requirements

        # Get currently active appid from config or default
        current_appid = self.config.get("daemon", {}).get("target_appids", [None])[0] # Get first appid for simplicity

        if current_appid is None:
            self.notify("No target AppID configured for saving filter.", severity="error")
            return
            
        save_app_filter(self.db_path, current_appid, filter_text, required_tags, excluded_tags)
        self.notify(f"Filter saved for AppID {current_appid}. Scraper will use this filter.")

def main():
    config_path = "config.yaml"
    import sys
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
        
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        config = {}
        
    log_config = config.get("logging", {})
    level_str = log_config.get("level", "INFO").upper()
    log_level = getattr(logging, level_str, logging.INFO)
    log_file = log_config.get("file")
    
    # For TUI, prefer file logging only so it doesn't mess up the screen
    handlers = []
    if log_file:
        handlers.append(logging.FileHandler(log_file))
        
    if handlers:
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=handlers,
            force=True
        )
    else:
        # Disable logging if no file, as stdout corrupts TUI
        logging.getLogger().addHandler(logging.NullHandler())

    app = ScraperApp(config_path)
    app.run()

if __name__ == "__main__":
    main()
