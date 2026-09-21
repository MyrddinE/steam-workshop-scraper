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
is actually needed: it returns at once while at least `DISCOVERY_FILL_TARGET` (300)
items are fetchable, which is what keeps this thread cheap. The target sits above
the level the fetch loop drains it to between passes, so an unskipped pass leads
the drain rather than racing it.

It holds no state of its own. The cursor, the page-discovery cooldown and
`_cursor_exhausted` live on the daemon, and only this thread writes them — which
is what keeps them safe without a lock. The fetch loop reads none of them. The
finished-walk latch `app_discovery.cursor_walk_finished` is likewise written and
read only here (`seed_database` sets it, and skips a finished AppID's cursor scan
on later passes).

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

**A lock that outlives the busy timeout**: `process_batch()` is called inside a `try` that catches `sqlite3.OperationalError`. A write that could not start because another writer held the lock past the connection's 15 s busy timeout is a warning line, not the end of the daemon: the loop pauses `pacing.DB_LOCK_RETRY_SECONDS` (5 s) with `pacing.wait`, which serves the pause but stays responsive to a stop, and then runs another pass. The PID-file stop check sits outside the `try`, so it runs on every iteration, including one that lost the lock — a graceful stop is never postponed by a persistent lock. The iteration is retried whole — the rows the aborted pass did not reach are still queued because it wrote nothing to them, so there is no per-item recovery and no queue flag is cleared. Only a lock is tolerated; any other exception still propagates.

### Web Scraper Thread (`WebScraperThread`)

Independent daemon thread. Picks up items with highest `web_scrape_priority` priority (10 = detail view, 5 = list view, 3 = new item, 1 = backlog). Downloads the Steam Community page, extracts extended_description and tags. Writes `extended_description`, `web_scrape_priority` and `web_scraped_at` (our completion time). Flags non-ASCII extended_description for translation.

**Shared state**: Reads `workshop_items` (preview_url, extended_description, etc.), writes `extended_description`, `web_scrape_priority` and `web_scraped_at` (via `insert_or_update_item`). Writes `translation_queue` via `queue_field_for_translation`. On failure it raises `api_priority` to 2. `web_scraped_at` is written only on the success branch; a miss, a wall, a throttle and a transport failure all leave it alone.

**A lock that outlives the busy timeout**: the whole iteration — the queue read, the scrape, and every write it makes — sits inside a `try` that catches `sqlite3.OperationalError`. A lock that outlived the connection's 15 s busy timeout is logged at warning (with the item's workshop id when one has been read) and the worker pauses `pacing.DB_LOCK_RETRY_SECONDS` with `pacing.wait` before taking the next item. The row is left exactly as it was: the pass did not reach its write, so it is still queued and the next attempt retries it. Before this guard an `OperationalError` escaped `run()` and ended the thread.

### Image Download Thread (`ImageDownloadThread`)

Independent daemon thread. Picks up items with highest `image_priority` priority. Downloads the preview image, detects MIME/extension, saves to the bucketed `images/` directory. Writes `image_answer`, `image_priority` and `image_fetched_at`.

**Shared state**: Reads `workshop_items` (preview_url, image_answer, image_priority). Writes `image_answer`, `image_priority`, `image_fetched_at`. On failure it decrements `image_priority` and raises `api_priority` to 2. It writes no Steam revision: it used to overwrite `scrape_version` on every download, which made an item whose page had never been scraped claim a scrape at its current revision (issue 7); that write was removed and migration 34→35 then dropped the column. `image_fetched_at` is written only when the bytes are on disk — a 404, an unclassifiable content type and a transport failure all leave it alone.

**A lock that outlives the busy timeout**: the iteration is inside a `try` that catches `sqlite3.OperationalError`, covering the parts the request's own `try` never did — `get_next_image_item`, the flag-clearing write for a row with no `preview_url`, and the writes in the request-failure handler. A lock is logged at warning (with the item id once one has been read) and the worker pauses `pacing.DB_LOCK_RETRY_SECONDS` with `pacing.wait` before its next attempt. The row is left as it was, flag included, so the item is still queued and is retried. Before this guard the exception escaped `run()`, `threading.excepthook` recorded a dump, and image downloads stopped silently while the daemon kept running.

### Translation Thread (`TranslatorThread`)

Independent daemon thread. Fetches a request's worth of fields from `translation_queue` — packed by an estimated cost (`openai.batch_char_cap`, charging each field its source text plus `PER_FIELD_OVERHEAD_CHARS` of boundary scaffolding; there is no item ceiling) — and sends them to an OpenAI-compatible API as boundary blocks: a boundary line carrying a per-request phrase of four words, the queue row's `entity_id` and its field label, then the source text beneath it. Writes translated fields to `_en` columns. Handles both `workshop_items` (title_en, short_description_en, extended_description_en) and `creators` (personaname_en).

**Shared state**: Reads `translation_queue`. Writes `_en` columns on `workshop_items` and `creators`, stamps `translate_version` on items and `translated_at` on creators, deletes from `translation_queue`, resets `translation_priority` to 0 when the queue is empty for an item. In that same last-field statement it stamps the item's `translated_at` with our clock — the completion time of the item as a whole. A per-field write while another field is still queued does not stamp it.

**Pacing and failure**: Every API-calling thread is expected to pace itself and to back off when a
request fails, the way the daemon's dynamic `api_delay` does — see [data-pipeline.md](data-pipeline.md).
Retrying a failed batch at the normal inter-batch interval turns a transient outage into a tight loop
against a third-party API, and buries the real message in the log. A failed batch leaves its
`translation_queue` rows in place, so a backoff costs nothing but time.

A reply covering only part of its request is **partial success**, not a failure: the fields it
resolved are written, the ones it missed keep their rows for a later pass — each with its `priority`
lowered by one, so it stops holding the head of the queue — and the backoff is not grown; the model
answered, so backing off would slow work it did return. A reply with **no usable block at all** is a
failure and does back off, which is what stops an unparseable answer being re-sent in a tight loop.
A whole-batch failure demotes nothing: it is unlikely to be content-related, so every row keeps the
priority it had and the batch is rebuilt and retried as it stands.

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
why it keeps its ceilings while the other three lost theirs. Storage is the one place it no longer
differs: its backoff has always lived in the daemon state file, and the other three's delays now live
there too, one section per worker, rather than in `config.yaml`.

**The four delays all live in the state file.** Each rate-seeking worker reads its starting delay from
its own `.daemon_state.yaml` section and writes it back as it moves — bounded by
`pacing.PERSIST_STEP_SECONDS`, so the file is not rewritten on every request — exactly as the
translator's backoff is recorded below. None of them is read from `config.yaml` any more, and a config
that still carries one of the retired keys gets one warning and is otherwise ignored.

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

**Resetting one.** Every worker's delay is reset the same cheap way: delete its section from
`.daemon_state.yaml` — or the file, which only ever holds daemon-owned state — and restart. The
sections are `translation_backoff` here, and `api_delay`, `web_delay` and `image_delay` for the three
rate-seeking workers; one section per worker, so clearing one leaves the rest alone. The three used to
be reset by editing their key in `config.yaml`, and moving them to the state file keeps that escape
hatch rather than losing it: an operator can still pull a misbehaving delay back down without touching a
Python file. [config-security.md](config-security.md#pacing-delays-are-state-not-config) states the same
path for the operator. The translator is still the trusted one of the four: it has never backed off
when it should not, nor failed to when it should. Its ceilings are not the same device as theirs either:
they bound how long a condition that waiting cannot fix is left alone, so a raised spend limit is picked
up within the hour without a restart. See issue 21 in [code-issues.md](code-issues.md) for the history.

---

## Thread Safety

### SQLite WAL Mode

The database runs in WAL (Write-Ahead Logging) mode, set once during `initialize_database` (which every entry point calls before it reads or writes). A database whose recorded version is newer than the build is refused there before the mode is set, so an older build never reaches a read or write; see [schema-migrations.md](schema-migrations.md#schemaversionerror-database). WAL is a property of the file, not of a connection, so `get_connection(db_path)` only opens the connection and installs `Row`; it issues no `PRAGMA`. Each thread opens its own connection via `get_connection(db_path)`. SQLite serializes write transactions internally.

A `PRAGMA journal_mode` per connection used to be the exception to that concurrency story: a journal-mode statement is not covered by the connection's 15 s busy timeout, so it could raise `database is locked` on a read while the daemon held a lock — and in a Textual timer callback that ended the whole TUI session (issue 43). A lock that still outlives the busy timeout no longer ends a thread or the process either: the daemon's main loop and all four worker threads catch `sqlite3.OperationalError` per iteration, log it at warning, pause briefly and retry, leaving the rows that pass did not reach alone (the main loop and the web and image workers gained this guard with issue 44; discovery and translation already caught a failed pass). On the TUI side the unattended polls now catch `sqlite3.OperationalError`, skip the tick and retry on the next one, logging the first failure at warning and repeats at debug; a user-initiated action still reports its failure. See [tui.md](tui.md).

### Column Ownership

No formal locking protocol exists, but columns have clear ownership:
- **Main loop**: title, short_description, extended_description (via insert_or_update_item), subscriptions, favorited, views, tags, `steam_*`, `first_seen_at`, `api_fetched_at`, `last_fetch_attempted_at`, `wilson_*`, `translation_priority`
- **Discovery thread**: nothing beyond the bare row it creates — `workshop_id` and `api_priority` — so every other column on a discovered item is the main loop's
- **Web scraper**: extended_description, web_scrape_priority, `web_scraped_at` (our completion time, success only)
- **Image thread**: image_answer, image_priority, `image_fetched_at` (our completion time, success only)
- **Translator**: title_en, short_description_en, extended_description_en, personaname_en, translate_version, `translated_at` on both tables (on an item it is written when the last queued field completes; for a creator the per-field write stamps `creators.translated_at`, and completion only clears `creators.translation_priority`, because a creator has no version key)

### Priority Bumping

The `web_scrape_priority`, `image_priority`, and `translation_priority` columns use priority levels (10 = highest, 1 = lowest, 0 = done). Bump functions use `MAX(current, new_priority)` to upgrade without downgrading. This allows the main loop and frontend views to independently bump priority without coordination.

The image thread uses a direct UPDATE to set `image_priority = max(0, current - 1)` on failure, deliberately using a non-MAX path to decrement priority for transient failures. The web scraper does not do the same for a selector miss: it leaves `web_scrape_priority` untouched when the page was not the item's, and clears it when the item page genuinely has no description. It does not raise `api_priority`, because the request itself succeeded. See [failure-capture.md](failure-capture.md).

### The Outbox Manifest

Two producers write the same `manifest.json` when an outbox is configured: the backup thread (database snapshots) and the failure-capture writer. `update_manifest` reads, modifies and rewrites that one file, so its read-modify-write is serialised by a module lock in `backup.py`; `remove_manifest_entries` — which housekeeping uses to drop a pruned debug capture's entry, so the puller never chases a file that is gone — takes the same lock. Two separate **processes** sharing one outbox would still need a real file lock, which is not implemented; that also means a pull tool that edits the manifest while the daemon writes it can lose an entry, which is why the daemon-side prune and the puller both remove a file and its entry in one operation.

### `insert_or_update_item` Concurrency

This function uses `INSERT ... ON CONFLICT(workshop_id) DO UPDATE SET`. It's called by the main loop, the discovery thread, the web scraper, and the image thread. Each call only writes the columns it has data for (dict keys are filtered against `WORKSHOP_ITEM_COLUMNS`). The `DO UPDATE SET` only updates columns that appear in the INSERT, so concurrent writes to different columns don't overwrite each other.

---

## Polling (Web UI)

The web UI's image poll runs in the browser at an adaptive interval via `setTimeout`. Each cycle:
1. Collects workshop_ids from DOM elements with `.grid-img-placeholder`
2. POSTs to `/api/items` (bulk ID lookup, near-instant)
3. Updates DOM for each returned item (title, image, scores)

**Delay**: `max(1, log2(pending_count))` seconds, so polls speed up as images arrive. The poll starts when `doSearch` detects `image_priority > 0` items and stops when no pending placeholders remain.

The detail poll runs at a fixed 3-second interval for the currently selected item, checking `translation_priority > 0` to detect when translation completes.

**Server-side**: The `/api/items` endpoint does a simple `WHERE workshop_id IN (...)` primary-key lookup. No sorting, filtering, or JOIN overhead.

---

## Daemon Startup and Shutdown

### Startup (`daemon_runner.main`)

1. `_fix_windows_encoding()` — sets console to UTF-8 on Windows
2. Loads config
3. **Takes the PID file**: creates `.daemon.pid` with an exclusive create (`O_CREAT|O_EXCL`) — see [cross-platform.md](cross-platform.md#pid-file-protocol). If the file already exists, or the create fails for any other reason, the start is refused *here*: the reason is logged and the process exits non-zero (3), before the logging reconfiguration, before `initialize_database` and before any migration, so a refused start has touched neither the PID file nor the database
4. Optionally invokes `_daemonize()` (double-fork on Unix, no-op on Windows). It runs *after* the file is taken, so a refusal is decided by the process the UI spawned rather than by its detached grandchild, and its non-zero exit code is what the controller can read
5. Writes the daemon's own PID into the already-taken file and registers the `atexit` handler to remove it, still before the migrations (the stop-then-migrate order is unchanged)
6. Configures logging (file handler, stdout handler with `_SafeStreamHandler` on Windows). The file handler is rotation-aware: it reopens the log when the generation marker beside it changes, which is how it follows the operator's **manual** rotation instead of writing into the compressed archive (`src/log_rotation.py`)
7. Initializes database (runs migrations)
8. Creates `Daemon` instance, calls `daemon.run()`

### The PID file refuses a second start

The PID file is the guard as well as the record. A hand-started second daemon used to overwrite the live file unconditionally and then migrate, applying migrations under the running daemon; the first daemon watches the file's *absence*, so it kept running, and the two shared one file — whichever exited first stopped the other. The exclusive create closes that: two simultaneous starts cannot both win, and the loser aborts before it has touched the database or written a PID file of its own.

**Existence blocks the start, not liveness.** A stale file left by a crash therefore refuses the start too, until the operator removes it. That is the owner's chosen rule and the safe direction: a liveness probe would free the start over a *recycled* PID — the number now belongs to an unrelated process — and let the second daemon take the file anyway, which is the hazard this guard exists to prevent. So the refusal message names the file and, when it can be read, the PID inside it, and tells the operator what to do: if that PID is a running daemon, stop it first; if the daemon crashed and left the file behind, remove the file and start again. A create that fails for another reason — a missing directory, a read-only filesystem, permissions — is refused with the same shape and the filesystem's own reason. The refusal is written to stderr because the configured handlers are deliberately not installed yet, and it is the process the UI spawned that exits non-zero, so `DaemonController.start()` reports the refusal instead of a success it did not have.

### A pending migration and a running daemon

The daemon is detached, so closing the TUI leaves it running while a new UI starts. A migration is DDL that rewrites tables — 34→35's `DROP COLUMN` rewrote `workshop_items`, 283 s *measured live* in production — so migrating under that live writer is the defect, and afterwards the daemon would keep running old code against the new schema. The TUI and the standalone web runner therefore do not call `initialize_database` directly; they call `initialize_database_with_daemon_stopped` (`src/daemon_control.py`), which:

1. reads the recorded `PRAGMA user_version` read-only (`database.read_schema_version`) — a WAL reader may do that while the daemon runs — and refuses a database newer than the build with `newer_schema_error` **before** touching the daemon, so a refused start does not take the service down on its way out;
2. leaves the daemon alone when the version already equals `EXPECTED_VERSION`, which is the ordinary relaunch and stays free;
3. when a migration is pending and the daemon is running, logs the migration as the reason and stops it through the same `DaemonController.stop()` the Stop button uses (PID-file removal, an owned-process signal, and the `STOP_TIMEOUT_SECONDS` grace described below);
4. refuses to migrate when that stop did not succeed, raising `DaemonStillRunningError` with a sentence saying the daemon is still running and the migration was not attempted — a gate, not best-effort;
5. migrates, then restarts the daemon and logs it; if the migration raised it does not restart, and `SchemaMigrationFailedError` says the daemon was stopped and has not been restarted.

The daemon's own startup (step 7 above) is unchanged: it performs its pending migrations directly, before it constructs the `Daemon`, and `DaemonController.start()` already refuses a second start for anything the controller launches. A start spawned this way that is refused by the PID-file guard reports the refusal to the UI rather than success; see [The PID file refuses a second start](#the-pid-file-refuses-a-second-start).

### Graceful Shutdown

The daemon has two stop signals and both end in the same sequence. On **Unix**
the controller signals the process it started, which `handle_shutdown` answers
by clearing `self.running` and the worker flags. On **Windows**, and as a
fallback everywhere, the controller deletes `.daemon.pid`, and the daemon
notices the file is gone at its next stop checkpoint. The checkpoints are the
top of `process_batch` (before the housekeeping), `_wait_for_work` (every
second), after `_read_batch`, per item, per details chunk, before the
creator refresh, and per discovery or subscription page.

Whether the file's absence is a stop request at all is a property of how the
daemon was launched. `src.daemon_runner` writes the file *before* it constructs
the `Daemon`, so it passes `expect_pid_file=True` and the daemon reads the
absence as a stop from its very first check. That closes the race the old
"only once I have seen the file exist" guard left open: a stop landing during
config load, the migrations, the constructor or thread startup — all of which
happen after the file is written and before the first check — was previously
ignored for ever. A daemon constructed directly (tests, embedding) has no file
and is not fooled in the other direction: the flag starts unset and is armed
only once the file has actually been seen.

The loop then exits and `_shutdown_workers()` runs. Every worker's stop flag is
set **before the first join**: signalling them together is what lets them unwind
at the same time. That ordering is the fix for the previous sequence, which
signalled one worker, joined it with its own 5-second timeout, and only then
told the next one to stop — five additive joins whose worst case was 25 seconds,
long enough that the controller's grace expired while the workers were still
logging. The joins now share one deadline, `SHUTDOWN_BUDGET_SECONDS` (20 s in
`src/daemon.py`); each join gets only the budget the previous ones left, and once
it is gone the remaining workers are not joined at all. Whatever is still alive
at the deadline is named in a warning and left behind. Each worker that did stop
gets exactly one line, logged by the daemon as it confirms the join rather than
by the worker itself, so the owner's log no longer shows the same sentence
twice.

The closing database snapshot is taken only when every worker did stop. A
survivor means a writer may still be mid-transaction, so the snapshot is skipped
with a line naming the thread that outlived the budget. The daemon then exits,
and `atexit` removes the PID file.

The controller's side is derived so that it covers that worst case rather than
fitting it by luck: `STOP_TIMEOUT_SECONDS` (40 s in `src/daemon_control.py`) is
the longest single blocking call the daemon's main thread can be inside when the
stop arrives (15 s: the SQLite busy timeout in `src/database.py`, and the
subscriptions page fetch in `src/subscription_sync._fetch_page`; the Steam calls
on that path are 10 s and the 15 s workers run on their own threads), plus the
daemon's 20 s join budget (`SHUTDOWN_BUDGET_SECONDS`) plus a 5 s margin for the
up-to-a-second PID-file tick in `_wait_for_work`, the failure-capture flush, the
controller's own half-second poll and process teardown. Only after that grace
does the controller escalate to terminate/kill, and only against a process it
started. The closing snapshot, when a backup outbox is configured, runs after
the joins and is bounded by the size of the database rather than by the budget,
so it is not covered by this figure.

---

## TUI Web Server Thread

The TUI starts a Waitress web server in a daemon thread during `ScraperApp.__init__`. The thread uses `waitress.serve()` which blocks until the process exits. The port is persisted to `config.yaml` on first run and reused on subsequent launches. `action_quit` calls `self.exit()`, which terminates the main thread, and the daemon web thread dies with the process.

The web server and the TUI share the same `_db_path` (global variable in webserver module, set by `init_webserver`). They also share `_config`. No explicit locking exists; Flask handles request concurrency internally. Database reads use their own connections via `get_connection`.
