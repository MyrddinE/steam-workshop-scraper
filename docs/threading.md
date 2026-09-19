# Threading & Concurrency Model

The daemon runs four worker threads — web scraper, image downloader, translator, and discovery — plus the main loop, and one more (the backup thread) when database backups are configured. The TUI and web server run in the main process with their own threading. This document covers thread responsibilities, shared state, locking, and coordination.

---

## Daemon Thread Architecture

### Discovery Thread (`DiscoveryThread`)

Keeps the API fetch queue topped up. It used to be the main loop's job, and only
once that loop had drained the queue to nothing: the daemon starved, blocked on
paging until enough new items appeared, and then resumed fetching. Batching the
details calls made fetching fast enough that the stall became a visible share of
the daemon's time, so the refill moved off that path.

It buys no extra API budget. `steam_api._rate_limit` is one schedule shared by
every caller, so this thread waits its turn exactly as the fetch loop does; what
it buys is that the queue is refilled *while* the loop is still working, so the
loop never has to stop.

It calls `_run_page_discovery` when page mode is worth checking and `seed_database`
otherwise, then sets `_work_available` so an idling fetch loop wakes now rather
than at the end of its poll. `seed_database`'s own guard decides whether anything
is actually needed: it returns at once while at least `DISCOVERY_FILL_TARGET` (200)
items are fetchable, which is what keeps this thread cheap. The target sits above
the level the fetch loop drains it to between passes, so an unskipped pass leads
the drain rather than racing it.

It holds no state of its own. The cursor, the page-discovery cooldown and
`_cursor_exhausted` live on the daemon, and only this thread writes them — which
is what keeps them safe without a lock. The fetch loop reads none of them.

Its requests feed the adaptive backoff exactly as the details calls do. While
discovery was serialised behind the fetch loop it was tolerable that a refused
page only logged an error; on its own thread it would have been a second request
stream the controller could not see, so a refusal now doubles the delay and a
healthy page decays it. A page abandoned for shutdown moves nothing, because a
shutdown is not evidence about the request rate.

### Main Loop (`run` / `process_batch`)

Runs on the main thread. Spawns the worker threads, then enters a `while self.running` loop calling `process_batch` repeatedly. Each iteration:
1. Checks for PID file existence (graceful shutdown signal from TUI)
2. Fetches items due for processing
3. If none, waits for the discovery thread to refill the queue
4. For each item: calls Steam API, merges data, writes to DB, flags for web/image/translation
5. Dynamic API delay sleeps between items

The main loop is the only thread that writes metadata fields (title, description, subscriptions, etc.). It reads `api_fetched_at` to determine staleness. It is *not* the only thread that creates items: cursor discovery does that too, on the discovery thread, and did so from the main thread before the refill moved. Both go through `insert_or_update_item`, whose column filtering is what keeps that safe.

### Web Scraper Thread (`WebScraperThread`)

Independent daemon thread. Picks up items with highest `needs_web_scrape` priority (10 = detail view, 5 = list view, 3 = new item, 1 = backlog). Downloads the Steam Community page, extracts extended_description and tags. Writes `extended_description`, `needs_web_scrape`, `scrape_version` (the Steam revision) and `web_scraped_at` (our completion time). Flags non-ASCII extended_description for translation.

**Shared state**: Reads `workshop_items` (preview_url, extended_description, etc.), writes `extended_description`, `needs_web_scrape`, `scrape_version` and `web_scraped_at` (via `insert_or_update_item`). Writes `translation_queue` via `queue_field_for_translation`. On failure it raises `api_priority` to 2. `web_scraped_at` is written only on the success branch; a miss, a wall, a throttle and a transport failure all leave it alone.

### Image Download Thread (`ImageScraperThread`)

Independent daemon thread. Picks up items with highest `needs_image` priority. Downloads the preview image, detects MIME/extension, saves to the bucketed `images/` directory. Writes `image_extension`, `needs_image` and `image_fetched_at`.

**Shared state**: Reads `workshop_items` (preview_url, image_extension, needs_image). Writes `image_extension`, `needs_image`, `image_fetched_at`. On failure it decrements `needs_image` and raises `api_priority` to 2. It deliberately does **not** write `scrape_version`: that column records the revision the page was scraped at, and the image worker used to overwrite it on every download (issue 7; pinned by `test_a_downloaded_image_does_not_rewrite_the_scrape_version`). `image_fetched_at` is written only when the bytes are on disk — a 404, an unclassifiable content type and a transport failure all leave it alone.

### Translation Thread (`TranslatorThread`)

Independent daemon thread. Fetches a request's worth of fields from `translation_queue` — packed by source length (`openai.batch_char_cap`) and capped at `openai.batch` fields — and sends them to an OpenAI-compatible API as boundary blocks: a boundary line carrying a per-request phrase of four words, the queue row's `item_id` and its field label, then the source text beneath it. Writes translated fields to `_en` columns. Handles both `workshop_items` (title_en, short_description_en, extended_description_en) and `users` (personaname_en).

**Shared state**: Reads `translation_queue`. Writes `_en` columns on `workshop_items` and `users`, stamps `translate_version` on items and `translated_at` on users, deletes from `translation_queue`, resets `translation_priority` to 0 when the queue is empty for an item. In that same last-field statement it stamps the item's `translated_at` with our clock — the completion time of the item as a whole. A per-field write while another field is still queued does not stamp it.

**Pacing and failure**: Every API-calling thread is expected to pace itself and to back off when a
request fails, the way the daemon's dynamic `api_delay` does — see [data-pipeline.md](data-pipeline.md).
Retrying a failed batch at the normal inter-batch interval turns a transient outage into a tight loop
against a third-party API, and buries the real message in the log. A failed batch leaves its
`translation_queue` rows in place, so a backoff costs nothing but time.

A reply covering only part of its request is **partial success**, not a failure: the fields it
resolved are written, the ones it missed keep their rows for a later pass, and the backoff is not
grown — the model answered, so backing off would slow work it did return. A reply with **no usable
block at all** is a failure and does back off, which is what stops an unparseable answer being
re-sent in a tight loop.

The translator's delay doubles from a base and caps, in one of two shapes, chosen because the failures
differ in kind. A **service** failure — a transport error, a rate limit, a 5xx — starts at 2 s and caps
at 5 minutes, because it can clear on its own. An **account-level** rejection — 401, 402 or 403, meaning
credentials, billing or permissions — starts at 60 s and caps at an hour, because something has to
change before retrying can work; the thread resumes on its own once it does, and one log line per
attempt replaces the storm. The streak resets on the first batch that gets through. The wait is served
in one-second steps so a long backoff cannot hold the daemon's shutdown.

**Why this one does not share the pacing rule.** The other three move a delay up and down to find a
rate Steam will sustain, and they now share `src/pacing.py`. The translator is waiting out a daily
allowance that resets every 24 hours, not a rate: asking more slowly buys nothing, and asking less
slowly costs nothing except on the day the allowance runs out. The horizon is the reset, so backing off
to an hour and re-probing about twenty-four times a day is the right shape, and an uncapped or
rate-seeking delay would be worse than useless — it would answer a question nobody asked. This is also
why it keeps its ceilings while the other three lost theirs, and why it is the one worker that was
never given a config key: it has never needed an escape hatch.

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

**Resetting it.** The three rate-seeking delays are reset by editing their key in `config.yaml` and
restarting, because they are not trusted yet and an operator needs to pull one back down when it
over-reacts. The translator's has no config key, so the equivalent is to delete the `translation_backoff`
section from `.daemon_state.yaml` — or the file, which only ever holds daemon-owned state — and restart.
It is the trusted one of the four: it has never backed off when it should not, nor failed to when it
should, which is exactly why it was never given a config key, and why its differing from the other three
is not a gap to close. Its ceilings are not the same device as theirs either: they bound how long a
condition that waiting cannot fix is left alone, so a raised spend limit is picked up within the hour
without a restart. See issue 21 in [code-issues.md](code-issues.md), where the config storage and the
ceilings on the other three are recorded as one change, to be made together and not before.

---

## Thread Safety

### SQLite WAL Mode

The database runs in WAL (Write-Ahead Logging) mode, set once during `initialize_database` (which every entry point calls before it reads or writes). WAL is a property of the file, not of a connection, so `get_connection(db_path)` only opens the connection and installs `Row`; it issues no `PRAGMA`. Each thread opens its own connection via `get_connection(db_path)`. SQLite serializes write transactions internally.

A `PRAGMA journal_mode` per connection used to be the exception to that concurrency story: a journal-mode statement is not covered by the connection's 15 s busy timeout, so it could raise `database is locked` on a read while the daemon held a lock — and in a Textual timer callback that ended the whole TUI session (issue 43). The daemon's own worker threads already survive a failed pass (discovery and translation catch it; the web and image loops rely on the busy timeout for ordinary write contention). On the TUI side the unattended polls now catch `sqlite3.OperationalError`, skip the tick and retry on the next one, logging the first failure at warning and repeats at debug; a user-initiated action still reports its failure. See [tui.md](tui.md).

### Column Ownership

No formal locking protocol exists, but columns have clear ownership:
- **Main loop**: title, short_description, extended_description (via insert_or_update_item), subscriptions, favorited, views, tags, `steam_*`, `first_seen_at`, `api_fetched_at`, `last_fetch_attempted_at`, `wilson_*`, `translation_priority`
- **Discovery thread**: nothing beyond the bare row it creates — `workshop_id` and `api_priority` — so every other column on a discovered item is the main loop's
- **Web scraper**: extended_description, needs_web_scrape, `web_scraped_at` (our completion time, success only)
- **Image thread**: image_extension, needs_image, `image_fetched_at` (our completion time, success only)
- **Translator**: title_en, short_description_en, extended_description_en, personaname_en, translate_version, `translated_at` on both tables (on an item it is written when the last queued field completes; for a creator the per-field write stamps `users.translated_at`, and completion only clears `users.translation_priority`, because a creator has no version key)
- **Web scraper**: scrape_version, the revision the *page* was scraped at. The image thread used to write it too, which made an unscraped item claim a scrape; it no longer touches the column

### Priority Bumping

The `needs_web_scrape`, `needs_image`, and `translation_priority` columns use priority levels (10 = highest, 1 = lowest, 0 = done). Bump functions use `MAX(current, new_priority)` to upgrade without downgrading. This allows the main loop and frontend views to independently bump priority without coordination.

The image thread uses a direct UPDATE to set `needs_image = max(0, current - 1)` on failure, deliberately using a non-MAX path to decrement priority for transient failures. The web scraper does not do the same for a selector miss: it leaves `needs_web_scrape` untouched when the page was not the item's, and clears it when the item page genuinely has no description. It does not raise `api_priority`, because the request itself succeeded. See [failure-capture.md](failure-capture.md).

### The Outbox Manifest

Two producers write the same `manifest.json` when an outbox is configured: the backup thread (database snapshots) and the failure-capture writer. `update_manifest` reads, modifies and rewrites that one file, so its read-modify-write is serialised by a module lock in `backup.py`. Two separate **processes** sharing one outbox would still need a real file lock, which is not implemented.

### `insert_or_update_item` Concurrency

This function uses `INSERT ... ON CONFLICT(workshop_id) DO UPDATE SET`. It's called by the main loop, the discovery thread, the web scraper, and the image thread. Each call only writes the columns it has data for (dict keys are filtered against `WORKSHOP_ITEM_COLUMNS`). The `DO UPDATE SET` only updates columns that appear in the INSERT, so concurrent writes to different columns don't overwrite each other.

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

The web server and the TUI share the same `_db_path` (global variable in webserver module, set by `init_webserver`). They also share `_config` and `_pushed_sessionid` globals. No explicit locking exists; Flask handles request concurrency internally. Database reads use their own connections via `get_connection`.
