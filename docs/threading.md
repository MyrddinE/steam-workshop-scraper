# Threading & Concurrency Model

The daemon runs four background threads plus the main loop. The TUI and web server run in the main process with their own threading. This document covers thread responsibilities, shared state, locking, and coordination.

---

## Daemon Thread Architecture

### Main Loop (`run` / `process_batch`)

Runs on the main thread. Spawns three worker threads, then enters a `while self.running` loop calling `process_batch` repeatedly. Each iteration:
1. Checks for PID file existence (graceful shutdown signal from TUI)
2. Fetches items due for processing
3. If none, triggers discovery (page-based or cursor-based)
4. For each item: calls Steam API, merges data, writes to DB, flags for web/image/translation
5. Dynamic API delay sleeps between items

The main loop is the only thread that writes metadata fields (title, description, subscriptions, etc.) and the only thread that creates new items. It reads `dt_attempted` to determine staleness.

### Web Scraper Thread (`WebScraperThread`)

Independent daemon thread. Picks up items with highest `needs_web_scrape` priority (10 = detail view, 5 = list view, 3 = new item, 1 = backlog). Downloads the Steam Community page, extracts extended_description and full-size preview_url. Writes `extended_description`, `preview_url`, `needs_web_scrape`, `dt_attempted`. Flags non-ASCII extended_description for translation.

**Shared state**: Reads `workshop_items` (preview_url, extended_description, etc.), writes `extended_description`, `preview_url`, `needs_web_scrape`, `dt_attempted`, `image_extension` (via insert_or_update_item). Writes `translation_queue` via `flag_field_for_translation`.

### Image Download Thread (`ImageScraperThread`)

Independent daemon thread. Picks up items with highest `needs_image` priority. Downloads the preview image, detects MIME/extension, saves to `images/` directory. Writes `image_extension`, `needs_image`, `dt_attempted`.

**Shared state**: Reads `workshop_items` (preview_url, image_extension, needs_image). Writes `image_extension`, `needs_image`, `dt_attempted`.

### Translation Thread (`TranslatorThread`)

Independent daemon thread. Batch-fetches fields from `translation_queue` (up to 20), sends to OpenAI API, writes translated fields to `_en` columns. Handles both `workshop_items` (title_en, short_description_en, extended_description_en) and `users` (personaname_en).

**Shared state**: Reads `translation_queue`. Writes `_en` columns on `workshop_items` and `users`, stamps `dt_translated`, deletes from `translation_queue`, resets `translation_priority` to 0 when queue is empty for an item.

---

## Thread Safety

### SQLite WAL Mode

The database runs in WAL (Write-Ahead Logging) mode, set during `initialize_database`. WAL allows concurrent readers and a single writer without blocking. Each thread opens its own connection via `get_connection(db_path)`. SQLite serializes write transactions internally.

### Column Ownership

No formal locking protocol exists, but columns have clear ownership:
- **Main loop**: title, short_description, extended_description (via insert_or_update_item), subscriptions, favorited, views, tags, time_*, dt_found, wilson_*, translation_priority
- **Web scraper**: extended_description, preview_url, needs_web_scrape
- **Image thread**: image_extension, needs_image
- **Translator**: title_en, short_description_en, extended_description_en, personaname_en, dt_translated
- **All threads**: dt_attempted (stamped by whoever last processed the item)

### Priority Bumping

The `needs_web_scrape`, `needs_image`, and `translation_priority` columns use priority levels (10 = highest, 1 = lowest, 0 = done). Bump functions use `MAX(current, new_priority)` to upgrade without downgrading. This allows the main loop and frontend views to independently bump priority without coordination.

The image thread uses a direct UPDATE to set `needs_image = max(0, current - 1)` on failure, deliberately using a non-MAX path to decrement priority for transient failures.

### `insert_or_update_item` Concurrency

This function uses `INSERT ... ON CONFLICT(workshop_id) DO UPDATE SET`. It's called by the main loop, web scraper, and image thread. Each call only writes the columns it has data for (dict keys are filtered against `WORKSHOP_ITEM_COLUMNS`). The `DO UPDATE SET` only updates columns that appear in the INSERT, so concurrent writes to different columns don't overwrite each other.

---

## Polling (Web UI)

The web UI's image poll runs in the browser at an adaptive interval via `setTimeout`. Each cycle:
1. Collects workshop_ids from DOM elements with `.grid-img-placeholder`
2. POSTs to `/api/items` (bulk ID lookup, near-instant)
3. Updates DOM for each returned item (title, image, scores)

**Delay**: `max(1, log2(pending_count))` seconds, so polls speed up as images arrive. The poll starts when `doSearch` detects `needs_image > 0` items and stops when no pending placeholders remain.

The detail poll runs at a fixed 3-second interval for the currently selected item, checking `translation_priority > 0` to detect when translation completes.

**Server-side**: The `/api/items` endpoint does a simple `WHERE workshop_id IN (...)` primary-key lookup. No sorting, filtering, or JOIN overhead.

---

## Daemon Startup and Shutdown

### Startup (`daemon_runner.main`)

1. `_fix_windows_encoding()` — sets console to UTF-8 on Windows
2. Loads config, optionally invokes `_daemonize()` (double-fork on Unix, no-op on Windows)
3. Writes PID to `.daemon.pid`, registers `atexit` handler to remove it
4. Configures logging (file handler, stdout handler with `_SafeStreamHandler` on Windows)
5. Initializes database (runs migrations)
6. Creates `Daemon` instance, calls `daemon.run()`

### Graceful Shutdown

**Unix**: The TUI sends SIGTERM via `Popen.send_signal()`. The daemon's `handle_shutdown` sets `self.running = False` and stops all threads. The main loop exits, joins threads, and the process terminates. `atexit` removes the PID file.

**Windows**: The TUI deletes `.daemon.pid`. The daemon's main loop checks for PID file existence after each `process_batch`. If missing, sets `self.running = False` and performs the same graceful shutdown sequence. If the daemon doesn't respond within 5 seconds, the TUI calls `Popen.terminate()` as fallback.

### Thread Join Order

On shutdown, threads are stopped in order: web_worker first, image_worker second, translator third. Each thread is signaled (`running = False`), then joined with a 5-second timeout. After all threads stop, the daemon process exits.

---

## TUI Web Server Thread

The TUI starts a Waitress web server in a daemon thread during `ScraperApp.__init__`. The thread uses `waitress.serve()` which blocks until the process exits. The port is persisted to `config.yaml` on first run and reused on subsequent launches. `action_quit` calls `self.exit()`, which terminates the main thread, and the daemon web thread dies with the process.

The web server and the TUI share the same `_db_path` (global variable in webserver module, set by `init_webserver`). They also share `_config` and `_sessionid` globals. No explicit locking exists; Flask handles request concurrency internally. Database reads use their own connections via `get_connection`.
