import json
import logging
from textual.app import App, ComposeResult, SystemCommand
from textual import on, events
from textual.command import Provider, Hit, DiscoveryHit
from textual.system_commands import SystemCommandsProvider
from typing import Iterable
from textual.screen import Screen, ModalScreen
from textual.widgets import Header, Footer, Input, ListView, ListItem, Static, Label, Select, Button, Markdown, DataTable
from textual.containers import Horizontal, Vertical, VerticalScroll, Center, Grid
from textual.reactive import reactive
from src.database import search_items, get_all_authors, initialize_database, flag_for_translation, get_item_details, save_app_filter, clear_pending_items, toggle_subscription_queue_status, get_queued_items, get_db_stats, compute_wilson_cutoffs
from src.analysis import view_window_analysis
from src.config import load_config
import os
import yaml
import datetime

def format_ts(ts):
    """Converts a Unix timestamp to YYYY-MM-DD string."""
    if not ts: return "N/A"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
    except:
        return "N/A"

def format_size(size_bytes):
    """Converts bytes to human-readable KB/MB/GB with Rich markup."""
    if not size_bytes: return "N/A"
    try:
        kb = float(size_bytes) / 1024
        if kb < 1024: return f"[gray]{kb:.1f} KB[/gray]"
        mb = kb / 1024
        if mb < 1024: return f"[white]{mb:.1f} MB[/white]"
        gb = mb / 1024
        return f"[yellow]{gb:.1f} GB[/yellow]"
    except: return "N/A"

def format_count(n):
    """Humanizes a number to 3 significant digits with K/M suffix and color markup."""
    if not n or n == 0:
        return "[gray]N/A[/gray]"
    try:
        n = int(n)
    except (ValueError, TypeError):
        return "[gray]N/A[/gray]"
    if n < 1000:
        return f"[gray]{n}[/gray]"
    if n < 1_000_000:
        if n < 10_000:
            return f"[white]{n/1000:.2f}K[/white]"
        elif n < 100_000:
            return f"[white]{n/1000:.1f}K[/white]"
        else:
            return f"[white]{n/1000:.0f}K[/white]"
    # >= 1M
    v = n / 1_000_000
    if n < 10_000_000:
        return f"[yellow]{v:.2f}M[/yellow]"
    elif n < 100_000_000:
        return f"[yellow]{v:.1f}M[/yellow]"
    else:
        return f"[yellow]{v:.0f}M[/yellow]"

def parse_tags(tags) -> list[str]:
    """Parses the tags JSON field (string or list) into a list of tag-name strings."""
    tags_list = []
    try:
        parsed = json.loads(tags) if isinstance(tags, str) else tags
        tags_list = [str(t.get("tag") if isinstance(t, dict) else t) for t in (parsed if isinstance(parsed, list) else [])]
    except: pass
    return tags_list

class StatsScreen(Screen):
    """A screen that displays database statistics."""

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="stats-left-col"):
                yield Label("[b]General Statistics[/b]", classes="stats-header")
                yield Static(id="general-stats-content")
                yield Label("\n[b]Translation Status[/b]", classes="stats-header")
                yield Static(id="translation-stats-content")
                yield Label("\n[b]App Tracking[/b]", classes="stats-header")
                yield DataTable(id="app-stats-table")
            with Vertical(id="stats-right-col"):
                yield Label("[b]Tag Statistics[/b]", classes="stats-header")
                with VerticalScroll(id="tag-stats-scroll"):
                    yield DataTable(id="tag-stats-table")
        yield Footer()

        yield Button("Close", id="btn-close-sub-queue")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-sub-queue":
            self.app.pop_screen()

    def on_mount(self) -> None:
        self.update_stats()
        self.set_interval(10.0, self.update_stats)

    def update_stats(self) -> None:
        stats = get_db_stats(self.db_path)
        
        # General Stats
        highest_dt = stats.get("highest_dt_updated") or "N/A"
        status_text = ""
        for row in stats["status_counts"]:
            status_text += f"  Status {row['status']}: {row['count']}\n"
        
        dt_text = ""
        for category, count in stats["dt_attempted_counts"].items():
            dt_text += f"  {category}: {count}\n"

        general_content = (
            f"Highest dt_updated: {highest_dt}\n\n"
            f"Record count by status:\n{status_text}\n"
            f"Record count by dt_attempted:\n{dt_text}"
        )
        self.query_one("#general-stats-content", Static).update(general_content)

        # Translation Stats
        trans_text = ""
        for status, count in stats["translation_status"].items():
            trans_text += f"  {status}: {count}\n"
        self.query_one("#translation-stats-content", Static).update(trans_text)

        # App Stats Table
        app_table = self.query_one("#app-stats-table", DataTable)
        app_table.clear(columns=True)
        app_table.add_columns("AppID", "Last Page", "Last Cursor")
        for app in stats["app_stats"]:
            cursor_str = str(app.get("last_cursor", "") or "")
            app_table.add_row(
                str(app["appid"]),
                str(app.get("last_page_scanned", 0) or 0),
                cursor_str[:30] + "..." if len(cursor_str) > 30 else cursor_str,
            )

        # Tag Stats Table
        tag_table = self.query_one("#tag-stats-table", DataTable)
        tag_table.clear(columns=True)
        tag_table.add_columns("Tag", "Count")
        sorted_tags = sorted(stats["tag_counts"].items(), key=lambda x: x[1], reverse=True)
        for tag, count in sorted_tags:
            tag_table.add_row(tag, str(count))

class AnalysisScreen(Screen):
    """Screen that analyzes the view window for Steam Workshop items."""

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path
        self.bucket_days = 7

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("[b]View Window Analysis[/b]", id="analysis-title")
        with Horizontal(id="analysis-controls"):
            yield Label("Bucket size (days): ", classes="control-label")
            yield Input(value="7", id="analysis-bucket-size", type="integer")
            yield Button("Recalculate", id="btn-analysis-recalc")
        yield Static(id="analysis-summary")
        with VerticalScroll(id="analysis-table-scroll"):
            yield DataTable(id="analysis-table")
        yield Footer()

    def on_mount(self) -> None:
        self.run_analysis()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-analysis-recalc":
            self.run_analysis()
        elif event.button.id == "btn-close-sub-queue":
            self.app.pop_screen()

    def run_analysis(self) -> None:
        try:
            bucket_val = self.query_one("#analysis-bucket-size", Input).value
            self.bucket_days = max(1, int(bucket_val or "7"))
        except ValueError:
            self.bucket_days = 7

        result = view_window_analysis(self.db_path, bucket_days=self.bucket_days)
        table = self.query_one("#analysis-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Age Range", "Items", "Median Views", "P10", "P90", "Relative")

        max_median = max((b["median"] for b in result["buckets"]), default=1)

        for b in result["buckets"]:
            bar_len = int(b["median"] / max(max_median, 1) * 40)
            bar = "█" * bar_len
            table.add_row(
                f"{b['age_start']}-{b['age_end']}d",
                str(b["count"]),
                str(b["median"]),
                str(b["p10"]),
                str(b["p90"]),
                f"[white]{bar}[/white]",
            )

        if result["estimated_window_days"]:
            summary = (f"[b]Estimated view window: ~{result['estimated_window_days']} days[/b]  "
                       f"(analyzed {result['items_analyzed']:,} items, "
                       f"{len(result['buckets'])} buckets)")
        else:
            summary = (f"Insufficient data to estimate view window. "
                       f"({result['items_analyzed']:,} items analyzed, "
                       f"{len(result['buckets'])} buckets)")
        self.query_one("#analysis-summary", Static).update(summary)

class SubscriptionQueueScreen(ModalScreen):
    """A modal screen that displays the subscription queue with clickable links."""

    def __init__(self, db_path: str, pause_lock_file: str):
        super().__init__()
        self.db_path = db_path
        self.pause_lock_file = pause_lock_file

    def on_mount(self) -> None:
        """Create the pause lock file when the screen is mounted."""
        try:
            with open(self.pause_lock_file, "w") as f:
                pass # Create the file
        except Exception as e:
            logging.error(f"Failed to create pause lock file: {e}")

    def on_unmount(self) -> None:
        """Remove the pause lock file when the screen is unmounted."""
        try:
            if os.path.exists(self.pause_lock_file):
                os.remove(self.pause_lock_file)
        except Exception as e:
            logging.error(f"Failed to remove pause lock file: {e}")

    def compose(self) -> ComposeResult:
        with Vertical(id="sub-queue-container"):
            yield Label("Subscription Queue", id="sub-queue-title")
            
            items = get_queued_items(self.db_path)
            if not items:
                yield Label("Queue is empty. Press 's' on an item to add it.")
            else:
                for item in items:
                    wid = item['workshop_id']
                    title = item['title']
                    url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"
                    # Use Textual's @click handler for hyperlinks
                    yield Label(f"{url} : {title}")
            
            yield Button("Close", id="btn-close-sub-queue")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-sub-queue":
            self.app.pop_screen()

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
        with Horizontal(id="details-buttons-row"):
            with Horizontal(id="top-left-buttons"):
                yield Button("Queue", id="btn-queue-sub", classes="details-button")
                yield Button("Unqueue", id="btn-unqueue-sub", classes="details-button")
                yield Button("Translate", id="btn-request-translation", classes="details-button")
                yield Button("Show Original", id="btn-toggle-translation", classes="details-button")
            yield Button("jump", id="btn-jump-author", variant="primary")

        with Horizontal(id="title-creator-row"):
            yield Label("", id="item-title")
            yield Label("", id="item-creator")
        
        yield Label("", id="wilson-scores")
        yield Static(classes="blank-line")
            
        with Horizontal(id="stats-row"):
            with Vertical(classes="stats-col"):
                yield Label("ID: N/A", id="stat-id")
                yield Label("Created: N/A", id="stat-created")
                yield Label("Updated: N/A", id="stat-updated")
                yield Label("Tags: N/A", id="stat-tags")
            with Vertical(classes="stats-col"):
                yield Label("Size: N/A", id="stat-size")
                yield Label("Views: N/A", id="stat-views")
                yield Label("Subs: N/A", id="stat-subs")
                yield Label("Favs: N/A", id="stat-favs")

        desc_container = Vertical(
            Markdown(id="detail-content"),
            id="desc-container"
        )
        desc_container.border_title = "Description"
        yield desc_container

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
            self.query_one("#item-title", Label).update("")
            self.query_one("#item-creator", Label).update("")
            self.query_one("#btn-toggle-translation").display = False
            self.query_one("#btn-request-translation").display = False
            self.query_one("#btn-jump-author").display = False
            self.query_one("#btn-queue-sub").display = False
            self.query_one("#btn-unqueue-sub").display = False
            
            for stat in ["id", "created", "updated", "tags", "size", "views", "subs", "favs"]:
                self.query_one(f"#stat-{stat}", Label).display = False
            self.query_one("#wilson-scores", Label).update("")
            return

        item = self.item_data
        
        is_queued = bool(item.get("is_queued_for_subscription", 0))
        self.query_one("#btn-queue-sub").display = not is_queued
        self.query_one("#btn-unqueue-sub").display = is_queued
        
        display_translated = self.show_translated and item.get("dt_translated")
        title = item.get("title_en") if display_translated and item.get("title_en") else item.get("title", "N/A")
        
        creator_name = item.get("personaname_en") if display_translated and item.get("personaname_en") else item.get("personaname")
        if not creator_name:
            creator_name = str(item.get("creator", "N/A"))
            
        self.query_one("#item-title", Label).update(f"[b]{title}[/b]")
        self.query_one("#item-creator", Label).update(creator_name)
        
        jump_btn = self.query_one("#btn-jump-author", Button)
        if item.get("creator"):
            jump_btn.display = True
        else:
            jump_btn.display = False

        if display_translated:
            desc = item.get("extended_description_en") or item.get("short_description_en")
            if not desc:
                desc = item.get("extended_description") or item.get("short_description") or "N/A"
        else:
            desc = item.get("extended_description") or item.get("short_description") or "N/A"

        toggle_btn = self.query_one("#btn-toggle-translation")
        if item.get("dt_translated"):
            toggle_btn.display = True
            toggle_btn.label = "Show Original" if self.show_translated else "Show Translation"
        else:
            toggle_btn.display = False

        req_btn = self.query_one("#btn-request-translation")
        req_btn.display = True

        tags_list = parse_tags(item.get("tags", "[]"))

        for stat in ["id", "created", "updated", "tags", "size", "views", "subs", "favs"]:
            self.query_one(f"#stat-{stat}", Label).display = True

        self.query_one("#stat-id", Label).update(f"[b]ID:[/b] {item.get('workshop_id', 'N/A')}")
        self.query_one("#stat-created", Label).update(f"[b]Created:[/b] {format_ts(item.get('time_created'))}")
        
        updated_ts = item.get('time_updated')
        updated_str = format_ts(updated_ts) if updated_ts and updated_ts != item.get('time_created') else "N/A"
        
        updated_label = self.query_one("#stat-updated", Label)
        if updated_str == "N/A":
            updated_label.display = False
        else:
            updated_label.display = True
            updated_label.update(f"[b]Updated:[/b] {updated_str}")
        
        self.query_one("#stat-tags", Label).update(f"[b]Tags:[/b] {', '.join(tags_list) if tags_list else 'None'}")
        self.query_one("#stat-size", Label).update(f"[b]Size:[/b] {format_size(item.get('file_size'))}")
        self.query_one("#stat-views", Label).update(f"[b]Views:[/b] {format_count(item.get('views', 0))}")
        
        subs_current = format_count(item.get('subscriptions', 0))
        subs_lifetime = format_count(item.get('lifetime_subscriptions', 0))
        self.query_one("#stat-subs", Label).update(f"[b]Subscribers:[/b] {subs_current} / {subs_lifetime}")
        
        favs_current = format_count(item.get('favorited', 0))
        favs_lifetime = format_count(item.get('lifetime_favorited', 0))
        self.query_one("#stat-favs", Label).update(f"[b]Favorites:[/b] {favs_current} / {favs_lifetime}")

        wilson_label = self.query_one("#wilson-scores", Label)
        app = self.app
        cutoffs = getattr(app, '_wilson_cutoffs', {}) if app else {}
        wilson_label.update(self._format_wilson_scores(item, cutoffs))
        wilson_label.display = bool(item.get("wilson_favorite_score") is not None)

        md_content = bbcode_to_markdown(desc)
        if item.get("translation_priority", 0) > 0 and not item.get("dt_translated"):
             md_content = f"> *[yellow]Translation requested, currently in queue...[/yellow]*\n\n{md_content}"
             
        self.query_one("#detail-content", Markdown).update(md_content)

    def _format_wilson_scores(self, item: dict, cutoffs: dict) -> str:
        """Formats Wilson scores with percentile-based coloring."""
        def colorize(label, score_key):
            score = item.get(score_key)
            if score is None:
                return f"[gray]{label}: N/A[/gray]"
            pct = score * 100
            p99 = cutoffs.get(score_key.replace("score", "p99"), 0) or 0
            p90 = cutoffs.get(score_key.replace("score", "p90"), 0) or 0
            p50 = cutoffs.get(score_key.replace("score", "p50"), 0) or 0
            if score >= p99:
                return f"[yellow]{label}: ! {pct:.1f}% ![/yellow]"
            if score >= p90:
                return f"[yellow]{label}: {pct:.1f}%[/yellow]"
            if score >= p50:
                return f"[white]{label}: {pct:.1f}%[/white]"
            return f"[gray]{label}: {pct:.1f}%[/gray]"
        fav = colorize("Favorite Score", "wilson_favorite_score")
        sub = colorize("Subscriber Score", "wilson_subscription_score")
        return f"{sub}   {fav}"

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
        
        is_queued = self.item_data.get("is_queued_for_subscription", 0)
        prefix = "[green]*[/green] " if is_queued else "  "

        yield Label(f"{prefix}[b]{title}[/b] ({wid})")
        yield Label(f"  By: {creator} | AppID: {appid}")

    async def refresh_item(self) -> None:
        """Re-compose the item to reflect any changes in item_data."""
        await self.recompose()


class SearchRow(Horizontal):
    """A single row in the search builder."""
    def __init__(self, fields: list[str], operators: dict[str, list[str]], is_first: bool = False, initial_filter: dict = None):
        super().__init__(classes="search-row")
        self.fields = fields
        self.operators_map = operators
        self.is_first = is_first
        self.initial_filter = initial_filter or {}

    def compose(self) -> ComposeResult:
        field = self.initial_filter.get("field", self.fields[0])
        field_options = [(f, f) for f in self.fields]
        yield Select(field_options, prompt="Field", id="field-select", classes="row-field", value=field)

        # Determine ops based on field
        if field in ["Author ID", "Workshop ID", "AppID"]:
            op_type = "id"
        elif field in ["File Size", "Subs", "Favs", "Views", "Language ID"]:
            op_type = "numeric"
        else:
            op_type = "text"
            
        ops = self.operators_map[op_type]
        op_options = [(o.replace("_", " "), o) for o in ops]
        
        op = self.initial_filter.get("op", ops[0])
        if op not in ops:
            op = ops[0]
            
        yield Select(op_options, prompt="Op", id="op-select", classes="row-op", value=op)
        
        val = self.initial_filter.get("value", "")
        yield Input(placeholder="Value", id="value-input", classes="row-input", value=val)
        
        yield Button("AND", id="btn-and", variant="default", classes="row-btn")
        yield Button("OR", id="btn-or", variant="default", classes="row-btn")
        if not self.is_first:
            yield Button("X", id="btn-remove", variant="error", classes="row-btn-remove")
        else:
            # Placeholder to keep alignment
            yield Static("", classes="row-btn-remove")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "field-select":
            field = str(event.value)
            # Determine field type to show relevant operators
            if field == "Author ID" or field == "Workshop ID" or field == "AppID":
                op_type = "id"
            elif field in ["File Size", "Subs", "Favs", "Views", "Language ID", "Subscriber Score", "Favorite Score"]:
                op_type = "numeric"
            else:
                op_type = "text"
                
            ops = self.operators_map[op_type]
            try:
                op_select = self.query_one("#op-select", Select)
                current_val = op_select.value
                
                # If we have an initial filter and op_select is uninitialized or blank, use it
                if current_val == Select.BLANK and self.initial_filter:
                    current_val = self.initial_filter.get("op", ops[0])
                    
                op_select.set_options([(o.replace("_", " "), o) for o in ops])
                
                if current_val in ops:
                    op_select.value = current_val
                else:
                    op_select.value = ops[0]
            except Exception:
                pass # Overlay might not be ready during initial mount

class SearchBuilder(VerticalScroll):
    """A container for multiple SearchRows."""
    def compose(self) -> ComposeResult:
        self.fields = [
            "Title", "Description", "Filename", "Tags", "Author ID",
            "File Size", "Subs", "Favs", "Views", "Workshop ID", "AppID", "Language ID",
            "Subscriber Score", "Favorite Score",
        ]
        self.operators = {
            "text": ["contains", "does_not_contain", "is", "is_not", "is_empty", "is_not_empty"],
            "numeric": ["is", "is_not", "gt", "lt", "gte", "lte", "is_empty", "is_not_empty"],
            "id": ["is", "is_not"]
        }
        yield SearchRow(self.fields, self.operators, is_first=True)

    def add_row(self, logic: str) -> None:
        new_row = SearchRow(self.fields, self.operators)
        self.mount(new_row)
        new_row.logic = logic

    def set_filters(self, filters: list[dict]) -> None:
        """Populates the builder with a given list of filters."""
        for row in list(self.query(SearchRow)):
            row.remove()
            
        if not filters:
            self.mount(SearchRow(self.fields, self.operators, is_first=True))
            return

        for i, f in enumerate(filters):
            is_first = (i == 0)
            row = SearchRow(self.fields, self.operators, is_first=is_first, initial_filter=f)
            if not is_first:
                row.logic = f.get("logic", "AND")
            self.mount(row)

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

class DatabaseCommands(Provider):
    """A command provider for database operations."""
    
    async def discover(self) -> Iterable[DiscoveryHit]:
        """Yield commands that should be discoverable when the palette opens."""
        yield DiscoveryHit(
            "Clear Pending Database",
            self.app.action_clear_pending,
            help="Remove all unscraped/pending items from the database",
        )
        yield DiscoveryHit(
            "Show Subscription Queue",
            self.app.action_show_sub_queue,
            help="Show items queued for subscription as clickable links",
        )

    async def search(self, query: str) -> Iterable[Hit]:
        """Search for database commands matching the query."""
        matcher = self.matcher(query)
        
        commands = {
            "Clear Pending Database": self.app.action_clear_pending,
            "Show Subscription Queue": self.app.action_show_sub_queue,
        }
        
        for label, action in commands.items():
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    action,
                    help=f"Action: {label}",
                )

class ScraperApp(App):
    """A Terminal GUI for searching the Steam Workshop database."""

    COMMANDS = {SystemCommandsProvider, DatabaseCommands}

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+d", "show_stats", "Stats"),
        ("s", "toggle_queue", "Queue for Sub"),
        ("l", "show_sub_queue", "List Queued Items"),
        ("ctrl+s", "save_filter_for_scraper", "Save Filter"),
        ("ctrl+t", "request_translation", "Translate"),
        ("ctrl+w", "toggle_translation", "Toggle Translation"),
        ("ctrl+a", "add_and_row", "AND"),
        ("ctrl+o", "add_or_row", "OR"),
        ("ctrl+x", "delete_bottom_row", "Delete Row"),
        ("ctrl+question_mark", "show_analysis", "Analysis"),
    ]

    CSS = """
    #_default {
        layout: vertical;
    }
    #stats-left-col {
        width: 40%;
        padding: 1;
        border-right: tall $primary;
    }
    #stats-right-col {
        width: 60%;
        padding: 1;
    }
    .stats-header {
        color: $accent;
        margin-bottom: 1;
    }
    #tag-stats-scroll {
        height: 1fr;
    }
    #tag-stats-table, #app-stats-table {
        height: auto;
        border: none;
    }
    #search-container {
        height: auto;
        margin: 0;
        padding: 0 1;
        border: solid $accent;
    }
    #search-builder {
        height: auto;
        max-height: 12;
    }
    .search-row {
        height: 2;
        margin-bottom: 0;
        align: left middle;
    }
    .search-row Select, .search-row Input, .search-row Button {
        height: 1;
        border: none;
        background: $boost;
    }
    .search-row Select > SelectCurrent {
        border: none;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    .search-buttons {
        height: auto;
        padding-top: 1;
        margin-bottom: 1;
    }
    .search-buttons Button {
        height: 1;
        border: none;
        background: $boost;
        width: auto;
        margin-left: 1;
        color: $accent;
    }
    .search-row Button {
        height: 1;
        border: none;
        background: $boost;
        color: $accent;
    }
    #top-left-overlay Button, .top-right-btn {
        color: $accent;
    }
    Button.-primary, Button.-error, Button.-success, Button.-warning {
        color: auto 100%;
        background: $primary;
    }
    .search-row Button.-error {
        background: $error;
    }
    .search-row .row-field { width: 20%; }
    .search-row .row-op { width: 20%; }
    .search-row .row-input { width: 35%; }
    .search-row .row-btn { width: 8%; min-width: 0; margin-left: 1; }
    .search-row .row-btn-remove { width: 5%; min-width: 0; margin-left: 1; }

    #main-container {
        layout: horizontal;
    }
    #results-column {
        width: 40%;
        layout: vertical;
    }
    #results-list {
        height: 1fr;
        border: solid $success;
        margin: 0;
    }
    #sort-container {
        height: auto;
        layout: horizontal;
        border: solid $primary;
        margin-bottom: 0;
        padding: 0 1;
    }
    #sort-container Select {
        height: 1;
        border: none;
        background: $boost;
        margin-top: 1;
        margin-bottom: 1;
    }
    #sort-container Select > SelectCurrent {
        border: none;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    .sort-select { width: 60%; }
    .sort-order { width: 40%; }

    #details-container {
        width: 60%;
        border: solid $secondary;
        padding: 0 1;
        margin: 0;
        layout: vertical;
    }
    #item-details {
        height: 1fr;
    }
    #details-buttons-row {
        height: 1;
        margin-bottom: 0;
    }
    #top-left-buttons {
        width: 1fr;
    }
    .details-button {
        height: 1;
        border: none;
        padding: 0 1;
        min-width: 0;
        margin-right: 1;
        background: $boost;
        color: $accent;
    }
    #btn-jump-author {
        height: 1;
        border: none;
        padding: 0 1;
        min-width: 0;
        background: $primary;
        color: auto;
        display: none;
    }
    #title-creator-row {
        height: auto;
        margin-top: 0;
    }
    #item-title {
        width: 1fr;
        content-align: left middle;
    }
    #item-creator {
        width: auto;
        max-width: 50%;
        content-align: right middle;
    }
    .blank-line {
        height: 1;
    }
    #stats-row {
        height: auto;
    }
    .stats-col {
        width: 50%;
        height: auto;
    }
    #desc-container {
        border-top: solid $primary;
        border-right: none;
        border-bottom: none;
        border-left: none;
        margin: 0;
        padding: 0;
        height: auto;
    }
    #sub-queue-container {
        width: 80%;
        height: 80%;
        background: $surface;
        border: thick $primary;
        padding: 1;
    }
    #sub-queue-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        padding-bottom: 1;
    }
    """

    def get_system_commands(self, screen):
        """Filter out Screenshot and Theme commands from the system commands."""
        for command in super().get_system_commands(screen):
            if "screenshot" in command.title.lower() or "theme" in command.title.lower():
                continue
            yield command

    def __init__(self, config_path: str = "config.yaml"):
        super().__init__()
        self.theme = "textual-dark"
        
        try:
            self.config = load_config(config_path)
        except FileNotFoundError:
            self.config = {"database": {"path": "workshop.db"}}
        self.db_path = self.config["database"]["path"]
        initialize_database(self.db_path)
        self._wilson_cutoffs = {}
        self.current_item_creator = None
        self.pause_lock_file = ".pauselock"
        
        # Pagination state
        self.current_offset = 0
        self.has_more_results = True
        self.is_loading = False
        self.is_single_creator_mode = False
        
        # UI State recovery
        # We use a hidden file to avoid cluttering the working directory
        self.state_file = ".tui_state.yaml"
        self._initial_state = load_tui_state(self.state_file)
        self._restored_scroll_y = self._initial_state.get("scroll_y", 0)
        self._restored_selected_id = self._initial_state.get("selected_workshop_id", None)
        self._has_restored_state = False

    def save_state(self) -> None:
        """Saves current UI state to disk."""
        if not self.is_mounted or not self._has_restored_state or self.is_single_creator_mode:
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
        self.query_one("#btn-return", Button).display = False
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
                Button("Return", id="btn-return", variant="warning"),
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
            ("Subscriber Score", "wilson_subscription_score"),
            ("Favorite Score", "wilson_favorite_score"),
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
        self._compute_percentiles()
        await self.load_more_items()

    def _compute_percentiles(self) -> None:
        """Computes Wilson score percentile cutoffs for the current filter set."""
        try:
            builder = self.query_one("#search-builder", SearchBuilder)
            filters = builder.get_filters()
        except Exception:
            return
        self._wilson_cutoffs = compute_wilson_cutoffs(self.db_path, filters)

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
        
        items = [WorkshopItem(item) for item in results]
        await list_view.mount(*items)
            
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
        """Load more items when scrolling near the bottom of the list, and update details pane."""
        list_view = event.list_view
        if list_view.id == "results-list":
            # If we are within 10 items of the end, fetch more
            if list_view.index is not None and list_view.index >= len(list_view) - 10:
                await self.load_more_items()
                
            if event.item and hasattr(event.item, 'item_data'):
                item_data = event.item.item_data
                self.current_item_creator = item_data.get('creator')
                
                detail_pane = self.query_one("#item-details", DetailsPane)
                detail_pane.workshop_id = item_data.get("workshop_id")
                
                jump_btn = self.query_one("#btn-jump-author", Button)
                if self.current_item_creator:
                    jump_btn.display = True
                else:
                    jump_btn.display = False
                    
                self.save_state()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle selection of an item in the list."""
        # Selection logic moved to highlighted event for immediate viewing
        self.save_state()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses (e.g., Jump to Author, Translation, Search Builder buttons)."""
        if event.button.id in ("btn-queue-sub", "btn-unqueue-sub"):
            await self.action_toggle_queue()
        elif event.button.id == "btn-execute-search":
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
            # Save state before switching to single creator mode
            if not self.is_single_creator_mode:
                self.save_state()

            self.is_single_creator_mode = True
            self.query_one("#btn-save-filter", Button).display = False
            self.query_one("#btn-return", Button).display = True

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

        elif event.button.id == "btn-return":
            self.is_single_creator_mode = False
            self.query_one("#btn-save-filter", Button).display = True
            self.query_one("#btn-return", Button).display = False

            # Reload saved state
            state = load_tui_state(self.state_file)
            if state and "filters" in state:
                builder = self.query_one("#search-builder", SearchBuilder)
                builder.set_filters(state["filters"])

            self.call_after_refresh(self.execute_search)

        elif event.button.id == "btn-toggle-translation":
            self.action_toggle_translation()

        elif event.button.id == "btn-request-translation":
            self.action_request_translation()

    async def action_save_filter_for_scraper(self) -> None:
        builder = self.query_one("#search-builder", SearchBuilder)
        filters = builder.get_filters()
        current_appid = self.config.get("daemon", {}).get("target_appids", [None])[0]

        if current_appid is None:
            self.notify("No target AppID configured for saving filter.", severity="error")
            return

        save_app_filter(self.db_path, current_appid, enrichment_filters=json.dumps(filters))
        self.notify(f"Filter saved for AppID {current_appid}. Scraper will use this for enrichment.")

    def action_toggle_translation(self) -> None:
        detail_pane = self.query_one("#item-details", DetailsPane)
        detail_pane.show_translated = not detail_pane.show_translated
        
    async def action_toggle_queue(self) -> None:
        """Toggles the subscription queue status of the highlighted item."""
        list_view = self.query_one("#results-list", ListView)
        if list_view.index is None:
            return

        item = list_view.highlighted_child
        if not item or not hasattr(item, "item_data"):
            return

        workshop_id = item.item_data.get("workshop_id")
        if not workshop_id:
            return

        # Toggle in DB
        toggle_subscription_queue_status(self.db_path, workshop_id)
        
        # Update UI state in place
        item.item_data["is_queued_for_subscription"] = not item.item_data.get("is_queued_for_subscription", 0)

        # Refresh the ListItem to show the change
        await item.refresh_item()

        # Update details pane if it's showing the same item
        detail_pane = self.query_one("#item-details", DetailsPane)
        if detail_pane.workshop_id == workshop_id and detail_pane.item_data is not None:
            detail_pane.item_data["is_queued_for_subscription"] = item.item_data["is_queued_for_subscription"]
            detail_pane.update_content()
        # Move to next item
        if list_view.index < len(list_view) - 1:
            list_view.index += 1
        
        # Scroll to keep highlight visible if needed
        # list_view.scroll_to_widget(item)

    def action_request_translation(self) -> None:
        detail_pane = self.query_one("#item-details", DetailsPane)
        if detail_pane.item_data:
            item = detail_pane.item_data
            wid = item.get("workshop_id")
            
            if item.get("translation_priority", 0) > 0:
                self.notify(f"Item {wid} is already in the translation queue.", severity="warning")
                return

            flag_for_translation(self.db_path, wid, priority=10)
            item["translation_priority"] = 10
            detail_pane.update_content()
            self.notify(f"Item {wid} flagged for high-priority translation.")

    async def action_add_and_row(self) -> None:
        self.query_one("#search-builder", SearchBuilder).add_row("AND")
        await self.execute_search()

    async def action_add_or_row(self) -> None:
        self.query_one("#search-builder", SearchBuilder).add_row("OR")
        await self.execute_search()

    async def action_delete_bottom_row(self) -> None:
        builder = self.query_one("#search-builder", SearchBuilder)
        rows = list(builder.query(SearchRow))
        if len(rows) > 1:
            row_to_delete = rows[-1]
            row_to_delete.remove()
            self.call_after_refresh(self.execute_search)

    def action_clear_pending(self) -> None:
        """Removes all unscraped/pending items from the database."""
        count = clear_pending_items(self.db_path)
        self.notify(f"Database cleared: {count} pending items removed.")
        self.run_worker(self.execute_search())

    def action_show_stats(self) -> None:
        """Shows the database statistics screen."""
        self.push_screen(StatsScreen(self.db_path))
        
    def action_show_sub_queue(self) -> None:
        """Shows the subscription queue modal screen."""
        self.push_screen(SubscriptionQueueScreen(self.db_path, self.pause_lock_file))

    def action_show_analysis(self) -> None:
        """Shows the view window analysis screen."""
        self.push_screen(AnalysisScreen(self.db_path))

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
