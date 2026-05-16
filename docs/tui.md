# TUI Architecture

The TUI is built with Textual (async terminal UI framework). It provides search, detail viewing, stats, daemon management, and an embedded web server. The entry point is `src.tui:main()`, which creates a `ScraperApp` instance.

---

## Application Lifecycle

### `ScraperApp.__init__`

1. Loads config (or defaults to `{"database": {"path": "workshop.db"}}` if config file not found)
2. Initializes database (runs migrations)
3. Starts embedded web server in a daemon thread (`_start_webserver`)
4. Loads TUI state from `.tui_state.yaml` (filters, sort, scroll position, selected workshop ID)
5. Sets up initial state flags: `_has_restored_state = False`, `_wilson_cutoffs = {}`

### `on_mount`

1. Restores saved sort and filter values from `_initial_state` into the corresponding Select widgets
2. Calls `call_after_refresh(self.execute_search)` — the deferred search ensures widgets are fully mounted before the first query
3. Registers `_check_scroll_bottom` as a watcher on `scroll_y` for infinite scroll

### `execute_search`

1. Clears the list view
2. Calls `_compute_percentiles()` to update Wilson score cutoffs for the current filter set
3. Calls `load_more_items()` to fetch and display the first page of 50 items

### State Restoration

After the first `load_more_items` completes, `_has_restored_state` is set to True and a deferred callback restores the scroll position and highlights the previously-selected item. Subsequent `on_select_changed` events (triggered by user interaction with sort/filter selects) save state and re-execute the search.

### `on_select_changed` Guard

During initial mount, setting default values on Select widgets fires `on_select_changed` which would trigger redundant searches. The guard `if self._has_restored_state:` prevents searches during restoration — only the `call_after_refresh(self.execute_search)` call runs the initial query. After state is restored, user-triggered changes fire searches and save state.

---

## Search Builder

### `SearchBuilder` (container)

Holds multiple `SearchRow` widgets. Provides:
- `add_row(logic)` — adds a new filter row with AND/OR logic
- `set_filters(filters)` — replaces all rows with a saved filter list
- `get_filters()` — collects current filter state, clamping percentile values (0-99) as a safety net

### `SearchRow` (individual filter line)

Each row contains:
- A `Select` for the field (from `SearchBuilder.fields`)
- A `Select` for the operator (dynamically populated based on field type: text, numeric, or id)
- An `Input` for the value
- AND/OR buttons, and a Remove button (only on non-first rows)

**Operator categories** mirror the web UI: text operators for Title/Description/Filename/Full Text, numeric operators (including `percentile`) for File Size/Subs/Favs/Views/Language ID/Subscriber Score/Favorite Score, and id operators for Author ID/Workshop ID/AppID.

**Field type determination** in `compose()` and `on_select_changed()` uses explicit field name checks rather than category lists, ensuring Subscriber Score and Favorite Score are consistently classified as numeric.

### Percentile Clamping

On `Input.Blurred` (when the value input loses focus), `_clamp_percentile()` rounds the value to 0-99. Also clamped in `get_filters()` as a safety net.

---

## List View

### `WorkshopItem` (list item)

Renders a single item in the list. Shows title (preferring `title_en`), creator name (preferring `personaname_en`), and subscription queue status. Each item stores `item_data` (the full search result dict) for detail rendering and state tracking.

### Infinite Scroll

A watcher on `list_view.scroll_y` checks if the user is within 5 pixels of the bottom. If so, triggers `load_more_items()` via `self.run_worker()`. Items are fetched in pages of 50 with `summary_only=True` to minimize data transfer.

### Item Bumping

When items appear in the list, the TUI bumps their priority for web scraping, translation, and image download at list-level priority (5). This ensures viewed items get processed promptly.

---

## Details Pane

### `DetailsPane` (widget)

Displays detailed metadata for the selected item. Shows: formatted title, creator, Wilson scores with percentile-colored markup, created/updated dates (Steam timestamps formatted via `format_ts`), file size (color-coded via `format_size`), views (formatted via `format_count`), subscription/favorite counts (current/lifetime), tags (parsed via `parse_tags`), description text, and action buttons.

**Translation toggle**: The `dt_translated` field determines whether the pane shows translated or original text. A "Show Original"/"Show Translation" button appears only when `dt_translated` is set.

**Queue indicator**: If `translation_priority > 0` and `dt_translated` is not set, a banner shows "Translation requested, currently in queue..."

### Detail Fetching

When a list item is highlighted, the TUI calls `bump_*_for_detail` functions (priority 10) and sets `detail_pane.workshop_id`. The `DetailsPane` watches this property and loads item data via `get_item_details` when it changes.

---

## Stats Screen

### `StatsScreen`

Opened by Ctrl+R. Displays:
- **General Statistics**: status code distribution, dt_attempted recency (fresh/stale/blank), highest dt_updated
- **Translation Status**: classification by translation state (Translated, ASCII, Queued, Needs Translation, No data)
- **Priority Breakdowns**: counts by priority level for `translation_priority`, `needs_image`, and `needs_web_scrape`
- **App Tracking**: per-AppID tracking data
- **Tag Statistics**: tag frequency table from `_compute_tag_frequencies`

### Adaptive Refresh

Stats refresh every 2 seconds via `set_interval`, but `update_stats` checks `now - _last_update < 50 * _last_duration` and returns early if the moratorium hasn't elapsed. So if the last update took 200ms, updates are throttled to once per 10 seconds. On first mount, it runs immediately.

### `compact_tag_ids` Integration

After populating tag stats, `compact_tag_ids` is called with the frequency data to reorder tag IDs for space efficiency (top 127 most frequent tags in 1-byte varint range).

---

## Daemon Management

### `DaemonManagerScreen`

Allows starting, stopping, and restarting the daemon process from within the TUI. Uses `subprocess.Popen` with platform-specific flags (DETACHED_PROCESS on Windows, DEVNULL redirects on Linux). Stores the Popen reference for status checking via `poll()` and graceful shutdown.

The screen previously had a live log tail (via `tail -f` on Unix), but it's disabled due to performance issues with large log files.

---

## Embedded Web Server

### `_start_webserver`

Starts a Waitress server in a daemon thread serving the Flask app. Port selection:
1. If `config["web"]["port"]` is set, tries to bind to that port
2. If the configured port is in use, falls back to a random port
3. If no port is configured, picks a random port and persists it to config via `save_config`

The server shares the TUI's database connection path (set via `init_webserver`). It also shares the `_sessionid` global for subscribe operations.

### Subscribe Action (Ctrl+B)

Sends a POST request to the local web server's `/api/subscribe/<workshop_id>` endpoint. The server proxies the subscribe request to Steam using the stored session ID. Handles Steam response codes (success=1, expired=2, permission denied=15, limit reached=25).

---

## Formatting Functions

### `format_ts(ts)` — Unix timestamp to YYYY-MM-DD string, or "N/A"

### `format_size(bytes)` — Bytes to human-readable with Rich markup and color thresholds:
- < 100KB: gray
- 100KB–10MB: gray with decimal precision
- 100MB–1GB: white
- 1GB–10GB: yellow
- ≥ 10GB: red
Uses 3-significant-digit precision matching `format_count`.

### `format_count(n)` — Number to human-readable with K/M suffix and color thresholds:
- < 1000: gray
- 1000–999999: white with K suffix
- ≥ 1M: yellow with M suffix
Uses 3 significant digits with decimal places that reduce as numbers grow.

### `parse_tags(tags)` — Parses comma-separated tag string (from junction table) or legacy JSON into a list of tag names. Used by the details pane for tag display.

---

## Additional Features

### Analysis Screen (`AnalysisScreen`)

Opened by Ctrl+?. Shows a view-window analysis table grouping items by time buckets and counting views, subscribers, etc. Bucket size is configurable via an input field.

### Jump to Author

The "Jump to Author" button replaces the current filter set with a single filter on the creator's ID. The `btn-return` button restores the previous filter state (saved before the jump).

### Subscription Queue (s/l keys)

`s` toggles `is_queued_for_subscription` on the selected item. `l` opens a screen listing all queued items with clickable links to their Steam pages.
