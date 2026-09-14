# Threading & Concurrency Model

The daemon runs three worker threads — web scraper, image downloader, and translator — plus the main loop, and one more (the backup thread) when database backups are configured. The TUI and web server run in the main process with their own threading. This document covers thread responsibilities, shared state, locking, and coordination.

---

## Daemon Thread Architecture

### Main Loop (`run` / `process_batch`)

Runs on the main thread. Spawns the worker threads, then enters a `while self.running` loop calling `process_batch` repeatedly. Each iteration:
1. Checks for PID file existence (graceful shutdown signal from TUI)
2. Fetches items due for processing
3. If none, triggers discovery (page-based or cursor-based)
4. For each item: calls Steam API, merges data, writes to DB, flags for web/image/translation
5. Dynamic API delay sleeps between items

The main loop is the only thread that writes metadata fields (title, description, subscriptions, etc.) and the only thread that creates new items. It reads `api_fetched_at` to determine staleness.

### Web Scraper Thread (`WebScraperThread`)

Independent daemon thread. Picks up items with highest `needs_web_scrape` priority (10 = detail view, 5 = list view, 3 = new item, 1 = backlog). Downloads the Steam Community page, extracts extended_description and tags. Writes `extended_description`, `needs_web_scrape`, `scrape_version`. Flags non-ASCII extended_description for translation.

**Shared state**: Reads `workshop_items` (preview_url, extended_description, etc.), writes `extended_description`, `needs_web_scrape`, `scrape_version` (via `insert_or_update_item`). Writes `translation_queue` via `flag_field_for_translation`. On failure it raises `api_priority` to 2.

### Image Download Thread (`ImageScraperThread`)

Independent daemon thread. Picks up items with highest `needs_image` priority. Downloads the preview image, detects MIME/extension, saves to the bucketed `images/` directory. Writes `image_extension`, `needs_image`, `scrape_version`.

**Shared state**: Reads `workshop_items` (preview_url, image_extension, needs_image). Writes `image_extension`, `needs_image`, `scrape_version`. On failure it decrements `needs_image` and raises `api_priority` to 2.

### Translation Thread (`TranslatorThread`)

Independent daemon thread. Batch-fetches fields from `translation_queue` (up to 20), sends to OpenAI API, writes translated fields to `_en` columns. Handles both `workshop_items` (title_en, short_description_en, extended_description_en) and `users` (personaname_en).

**Shared state**: Reads `translation_queue`. Writes `_en` columns on `workshop_items` and `users`, stamps `translate_version` on items and `translated_at` on users, deletes from `translation_queue`, resets `translation_priority` to 0 when the queue is empty for an item.

**Pacing and failure**: Every API-calling thread is expected to pace itself and to back off when a
request fails, the way the daemon's dynamic `api_delay` does — see [data-pipeline.md](data-pipeline.md).
Retrying a failed batch at the normal inter-batch interval turns a transient outage into a tight loop
against a third-party API, and buries the real message in the log. A failed batch leaves its
`translation_queue` rows in place, so a backoff costs nothing but time.

The translator's delay doubles from a base and caps, in one of two shapes, chosen because the failures
differ in kind. A **service** failure — a transport error, a rate limit, a 5xx — starts at 2 s and caps
at 5 minutes, because it can clear on its own. An **account-level** rejection — 401, 402 or 403, meaning
credentials, billing or permissions — starts at 60 s and caps at an hour, because something has to
change before retrying can work; the thread resumes on its own once it does, and one log line per
attempt replaces the storm. The streak resets on the first batch that gets through. The wait is served
in one-second steps so a long backoff cannot hold the daemon's shutdown.

**The backoff outlives the process.** The streak and the moment its next attempt falls due are written
to the daemon state file beside the database, `.daemon_state.yaml`, so restarting the daemon resumes
the backoff instead of beginning again at the base — otherwise a service condition that had already
reached its five minutes is retried two seconds later merely because the process restarted, and an
account-level rejection that had reached its hour is retried a minute after every restart. The recorded
streak is what reconstructs the delay; the timestamp is what lets a restart wait only the remainder
rather than serving the whole delay again. Wait and streak are clamped on the way in, since the file is
input as far as the thread is concerned: a corrupt streak would otherwise build a gigantic integer
before any cap applied, and a timestamp far beyond the cap would park the thread for an unexplained
age. The first batch that gets through removes the section, so a healthy translator leaves nothing
behind. The store is *injected*, so a thread constructed without one — in a test, or embedded — behaves
exactly as it did before. The file itself is owned by `src/daemon_state.py`, which is best-effort: an
unreadable or unwritable state file costs one extra attempt, never a crash.

---

## Thread Safety

### SQLite WAL Mode

The database runs in WAL (Write-Ahead Logging) mode, set during `initialize_database`. WAL allows concurrent readers and a single writer without blocking. Each thread opens its own connection via `get_connection(db_path)`. SQLite serializes write transactions internally.

### Column Ownership

No formal locking protocol exists, but columns have clear ownership:
- **Main loop**: title, short_description, extended_description (via insert_or_update_item), subscriptions, favorited, views, tags, `steam_*`, `first_seen_at`, `api_fetched_at`, `last_fetch_attempted_at`, `wilson_*`, `translation_priority`
- **Web scraper**: extended_description, needs_web_scrape
- **Image thread**: image_extension, needs_image
- **Translator**: title_en, short_description_en, extended_description_en, personaname_en, translate_version (translated_at on users)
- **Web scraper and image thread**: scrape_version (stamped by whichever last processed the item)

### Priority Bumping

The `needs_web_scrape`, `needs_image`, and `translation_priority` columns use priority levels (10 = highest, 1 = lowest, 0 = done). Bump functions use `MAX(current, new_priority)` to upgrade without downgrading. This allows the main loop and frontend views to independently bump priority without coordination.

The image thread uses a direct UPDATE to set `needs_image = max(0, current - 1)` on failure, deliberately using a non-MAX path to decrement priority for transient failures. The web scraper does not do the same for a selector miss: it leaves `needs_web_scrape` untouched when the page was not the item's, and clears it when the item page genuinely has no description. It does not raise `api_priority`, because the request itself succeeded. See [failure-capture.md](failure-capture.md).

### The Outbox Manifest

Two producers write the same `manifest.json` when an outbox is configured: the backup thread (database snapshots) and the failure-capture writer. `update_manifest` reads, modifies and rewrites that one file, so its read-modify-write is serialised by a module lock in `backup.py`. Two separate **processes** sharing one outbox would still need a real file lock, which is not implemented.

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
