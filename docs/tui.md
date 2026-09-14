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

**Translation toggle**: The `translate_version` field determines whether the pane shows translated or original text. A "Show Original"/"Show Translation" button appears only when `translate_version` is set.

**Queue indicator**: If `translation_priority > 0` and `translate_version` is not set, a banner shows "Translation requested, currently in queue..."

### Detail Fetching

When the detail pane adopts an item, `DetailsPane.watch_workshop_id` calls the `bump_*_for_detail` functions (priority 10) and then loads the item via `get_item_details`. The bump sits on the pane-load path rather than in the list-highlight handler so it fires once per adopted item instead of on every highlight event. The pane's own two-second refresh re-reads the item without re-queuing it.

---

## Stats Screen

### `StatsScreen` (`src/tui.py:104`)

Opened by Ctrl+R. The screen asks `src.metrics` for one tier at a time and draws each tier
into its own widgets, so the cheap numbers are on screen while the expensive queries are
still running. General layout and what each tier fills in:

| Tier | Metrics | Rendered as |
|---|---|---|
| instant | `totals`, `high_water`, `app_tracking` | live/dead item counts, the last successful API fetch, and the per-AppID tracking table |
| fast | `coverage`, `stuck_work`, `status_counts`, `fetch_recency` | coverage bars over live items, the stuck-work callout, the status distribution, and fetch recency |
| slow | `translation_status`, `priority_breakdowns`, `tag_counts` | the translation classification, per-queue waiting counts by priority, and the tag table |

Coverage (`_format_coverage`, `src/tui.py:353`) is drawn as a labelled progress bar per
stage — API data, description, image, translation, creator — against the number of live
items, with dead items excluded because they can never be covered. `stuck_work`
(`_format_stuck`, `src/tui.py:375`) names any dead items still flagged in a queue and says
the queues will not drain until they are cleared; a zero value shows an all-clear. The
priority section reads as queue state — "Translation queue: N waiting" followed by the
priority mix — rather than a raw column dump.

`#tier-costs` (`_render_tier_costs`, `src/tui.py:395`) lists each tier's total and each
metric's own `ms`, so the slow tier's cost is visible directly on the screen.

### Tiered refresh

Each tier runs in its own thread worker (`self.run_worker(..., thread=True)`,
`src/tui.py:186`; the worker body is `_compute_tier`, `src/tui.py:198`) and its result is
applied back on the UI thread from `on_worker_state_changed` (`src/tui.py:237`). The first
pass runs strictly instant → fast → slow, so the cheap numbers cannot be beaten to the
screen by the expensive ones.

Refresh is throttled per tier, not globally. A tier's interval is
`max(2 s, 50 × its own measured duration)` (`_interval_for`, `src/tui.py:182`), and a
one-second timer starts any tier whose own interval has elapsed
(`_refresh_due_tiers`, `src/tui.py:213`). A slow tier therefore no longer stretches the
instant tier's refresh out to minutes; the pre-rework screen used one `50 ×` rule for the
whole payload.

### `compact_tag_ids` Integration

The tag-frequency compaction still runs, but in the slow tier's worker thread and once per
slow-tier arrival, rather than on the UI thread on every screen update
(`src/tui.py:209`).

---

## Daemon Management

### `DaemonController` (`src/daemon_control.py:21`)

Owns the daemon process for both UIs: `start`, `stop`, `restart`, `status`, `read_pid`, `is_running` and `tail_log`. It launches `python -m src.daemon_runner <config> --daemon` detached (`DETACHED_PROCESS` on Windows, `DEVNULL` stdio elsewhere) and keeps the Popen handle (`DaemonController.proc`) for liveness and forced shutdown.

The PID-file protocol is unchanged. On Unix, stop sends SIGTERM then deletes `.daemon.pid`; on Windows it deletes the file. Either way it waits up to 15 s for exit, then escalates (Popen terminate/kill, `TerminateProcess` via ctypes on Windows, SIGKILL on Unix). `start` while running and `stop` while stopped are idempotent no-ops.

### `DaemonManagerScreen` (`src/tui.py:479`)

Allows starting, stopping, and restarting the daemon process from within the TUI. It no longer holds the process logic; every button delegates to the app's single `DaemonController` (`src/tui.py:1353`), which is the same instance handed to the embedded web server, so a start or stop from either UI is visible to the other. The status text and `PID: n` display read through `DaemonController.status()`.

The screen previously had a live log tail (via `tail -f` on Unix), but it's disabled due to performance issues with large log files. The web UI's daemon panel reads the same log incrementally through `/api/daemon/log`.

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
