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

### Crash dumps

An unhandled TUI traceback used to go to the terminal and nowhere else. `main()`
now installs `src/crash.py` after logging is configured (`crash.install("tui",
config, config_path=config_path)`, so the ring-buffer handler survives the forced
`basicConfig`), and wraps `app.run()` in a `try/except` that records the
exception and re-raises — the belt to the hooks' braces. The same installer is
called by `src/web_runner.py` and `src/daemon_runner.py`; in those two it adds no
handler beyond the ring buffer and changes no exit code or control flow.

The TUI is the interesting case, because Textual catches an unhandled error in
its own message pump and workers and renders a Rich traceback itself, so neither
`sys.excepthook` nor `threading.excepthook` ever sees it.
`ScraperApp._handle_exception` is therefore overridden to write the dump *before*
delegating to Textual's implementation, which still renders the traceback and
exits unchanged. Textual calls `_handle_exception` once per error and prints only
the first in normal mode, so every call writes its own numbered dump; a second
error is never suppressed.

Each dump is `<outbox_dir>/crashes/<stamp>-tui-error<N>.txt`, registered in the
outbox manifest with `kind: "crash"`. With no outbox configured it is written
beside the configured log file, else into the working directory, and its path is
printed to the console. It holds the context header, the traceback with each
frame's locals, and the last ~200 log records; the locals are redacted by key
name and by every known or runtime-registered credential, and truncated.
`docs/failure-capture.md` has the file shape and the elision rule, including its
residual risk.

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

**Operator categories** mirror the web UI: text operators for Title/Description/Filename/Full Text, numeric operators (including `percentile`) for File Size/Subs/Favs/Views/Subscriber Score/Favorite Score, and id operators for Author ID/Workshop ID/AppID.

**Field type determination** in `compose()` and `on_select_changed()` uses explicit field name checks rather than category lists, ensuring Subscriber Score and Favorite Score are consistently classified as numeric.

### Percentile Clamping

On `Input.Blurred` (when the value input loses focus), `_clamp_percentile()` rounds the value to 0-99. Also clamped in `get_filters()` as a safety net.

---

## List View

### `WorkshopItem` (list item)

Renders a single item in the list. Shows title (preferring `title_en`), creator name (preferring `personaname_en`), and subscription status. Each item stores `item_data` (the full search result dict) for detail rendering and state tracking.

### Steam text is escaped before it is rendered

Titles, tag names and persona names routinely contain square brackets — *measured live*, 129,533 titles in the library hold a bracket pair, and 2 of the 9 items queued for subscription did — and every widget here parses markup from a string (`Label.update`, `Static`, `DataTable` cells all do). A title is therefore markup unless it is escaped: `[najar]偶像大师 樋口円香（有断面+配音版）` raised `MissingStyle` and took the subscription queue down, while in the Textual-parsed widgets the same interpolation silently *ate* the tag instead of raising.

Every value that comes from Steam goes through `escape_markup` in `src/tui.py`, which escapes every `[`. That is deliberately stronger than `rich.markup.escape`, which only escapes brackets that already look like a tag — an unbalanced `[` is left standing, and Textual's parser then swallows everything up to the next `]`, including the closing tag the project wrote itself. Only markup the module writes itself (`[b]`, the spinner, the subscription colours) is left unescaped. `tests/test_tui_markup_injection.py` holds the reported strings to this.

### The subscription marker

The row's second line shows the owner's subscription marker next to the pending spinner, and the detail pane shows the same marker immediately before the title — the convention the web pane uses too. `_subscription_marker` and `DetailsPane.update_content` both read `src/subscription.py`, which owns the five states (`downloaded`, `subscribed`, `pending`, `previously`, `never`), their precedence, and the glyph/colour for each, so the TUI and the web grid cannot disagree about why the same row looks the way it does. The marker replaces the old leading `*` prefix on the title line for `is_queued_for_subscription`; there is only one indicator. `downloaded` is a solid `★` in a deeper green than `pending`'s outline, and it requires both `own_subscribed` and the local `downloaded_at` latch, so a stray timestamp cannot claim it.

**The subscription queue screen draws the same five states** — `SubscriptionQueueScreen._row_text` reads the shared table from the row `get_queued_items` returns, so a completed subscribe moves that row's glyph too.

**The downloaded marker (and opening the folder).** Windows only. On its own
`DOWNLOAD_SCAN_INTERVAL_SECONDS` (60 s) timer the TUI runs
`src/workshop_folders.scan`, which stamps `downloaded_at` for subscribed,
unconfirmed items whose folder Steam has on disk; the marker turns green on the
next render. The timer skips the scan entirely while `self._daemon_controller`
can see a daemon running, because the daemon runs the same scan and two of them
would stat the same folders in parallel. Off Windows the scan is a no-op and the
whole affordance is absent.

The action that opens the folder is available two ways while an item is green:
the plain `o` key, built into `ScraperApp.BINDINGS` only on Windows, acting on
the highlighted list item exactly like `s`; and the `Open Folder` button beside
`Show Original` in the detail pane. The button is **visible but disabled** for
any item that is not `downloaded`, with the reason in its label
(`Open Folder (not downloaded)`) and tooltip, so the affordance is discoverable
rather than invisible; the key shows the same refusal as a notification rather
than doing nothing. Both call `ScraperApp.open_folder_for` →
`src/workshop_folders.open`, which owns every guard — Windows only, the item must
be green, and the folder must still be on disk — and changes no state when it
refuses. If the folder is gone at click time (an unplugged drive, a moved
library, Steam cleaned up) the notification names the places that were looked in
and the marker is left alone; nothing is launched into an error.

The TUI marker is **not clickable** — there is no click affordance in the TUI, and `s` remains the way to change the state. The detail pane's old `btn-queue-sub` / `btn-unqueue-sub` pair is gone for the same reason: the marker *is* that control now.

`previously` can only mean "we have seen this account subscribed"; Steam exposes no per-account subscription history, so the marker carries that limitation in its tooltip. See [data-model.md](data-model.md) for the two columns behind it and [web-ui.md](web-ui.md) for the shared table.

**A rendered row follows the database, not the write that changed it.** A row's marker is built from the item data captured when the row was made, so a subscribe landing behind it would leave the green `pending` outline in place until a search or a scroll rebuilt the list. `ScraperApp._start_subscription_poll` is the fix, and it is the web grid's `_startListPoll` in TUI form: a one-shot `set_timer` re-reads only the rendered rows whose marker is still `pending`, replaces just their subscription columns from the database, and redraws those rows. Its own tick re-arms it only while some rendered row is still pending, so a settled list costs no reads at all; a new search, a row toggled into `pending` from the keyboard, and a pass result each arm it. Because the reader is the shared database and no writer is hooked, all three writers are covered — the TUI's own subscribe pass, the web UI's routes, and the daemon's daily reconcile. The pass's result callback additionally calls `ScraperApp.refresh_subscription_rows` for the item it just reported, so a row on screen moves at once rather than at the poll's next tick; that is the fast path, not the mechanism. The stage spinner keeps its own 0.15 s tick (`_tick_spinners`), which is a cosmetic redraw and does not re-read the database. The read is wrapped in `db_poll.guard_db_poll`, so a transient database lock skips that read rather than raising: the tick then re-arms on the rendered state, which a skipped read did not change (see [Unattended Reads](#unattended-reads)).

### Infinite Scroll

A watcher on `list_view.scroll_y` checks if the user is within 5 pixels of the bottom. If so, triggers `load_more_items()` via `self.run_worker()`. Items are fetched in pages of 50 with `summary_only=True` to minimize data transfer.

### Item Bumping

When items appear in the list, the TUI bumps their priority for web scraping, translation, and image download at list-level priority (5). This ensures viewed items get processed promptly. The bumps only *upgrade* an item that is already queued (`AND needs_web_scrape > 0` and its siblings), so viewing an item cannot create work that the daemon had decided was unnecessary.

### The pending marker

Each row's marker is not a binary "pending": its speed says *which* stage the item is waiting on, and its colour fades as it slows, so a marker that will clear in seconds does not look like one that may take hours.

| stage | rotation | colour | wording (web tooltip) |
|---|---|---|---|
| image | 1x | vivid green | Waiting for the image |
| translation | 4x slower | mid green | Waiting for the translation |
| web scrape | 16x slower | grey | Waiting for the web scrape |

The wording sits with the speed and colour in `src/pending.py` even though only the web marker has a hover to show it. The TUI has no hover and draws no text, but it names the same stage, so a description kept only in the template could describe that state differently from this table.

Three rules, all in `src/pending.py` and mirrored by the web list:

* **Fastest stage wins.** An item owing several stages shows the one about to clear. The slow, faded marker is the one that must not nag, and it will still be there afterwards.
* **Below priority 5 draws nothing.** Lower levels are background work, not something worth animating.
* **The API is not a stage.** A list only shows items the API has already returned, so a pending *refresh* is not content anyone is waiting on. The clause this replaced measured zero across 2.27M live rows.

One shared `_tick` drives the whole list and each item divides it by its stage's multiplier, so different rows run at different speeds from a single timer.

---

## Details Pane

### `DetailsPane` (widget)

Displays detailed metadata for the selected item. Shows: formatted title, creator, Wilson scores with percentile-colored markup, created/updated dates (Steam timestamps formatted via `format_ts`), file size (color-coded via `format_size`), views (formatted via `format_count`), subscription/favorite counts (current/lifetime), tags (parsed via `parse_tags`), description text, and action buttons.

**Translation toggle**: The `translate_version` field determines whether the pane shows translated or original text. A "Show Original"/"Show Translation" button appears only when `translate_version` is set.

**The pane deliberately shows the foreign text for a field that has no translation.** It is not hiding a gap and it is not a stopgap: with the original in front of them the reader can copy it and translate it themselves rather than waiting on the queue. Note that this makes a missing translation invisible on its own — `translate_version` and `translation_priority` are item-level while the `_en` columns are per field, so the translated view can fall back to the original for one field while another is translated. The pending marker is the signal for that, which is why it names the stage.

**Queue indicator**: If `translation_priority > 0` and `translate_version` is not set, a banner shows "Translation requested, currently in queue..."

### Detail Fetching

When the detail pane adopts an item, `DetailsPane.watch_workshop_id` calls the `bump_*_for_detail` functions (priority 10) and then loads the item via `get_item_details`. The bump sits on the pane-load path rather than in the list-highlight handler so it fires once per adopted item instead of on every highlight event. The pane's own two-second refresh re-reads the item without re-queuing it; it is guarded, so a lock on that read skips the tick and leaves `item_data` exactly as it was rather than ending the session (see [Unattended Reads](#unattended-reads)).

---

## Unattended Reads

The poll callbacks are the one place a database error must not be fatal: nobody asked for that particular read, the next tick is already coming, and an exception raised inside a Textual timer callback does not fail the refresh — Textual reports it and tears the session down. That is what happened on 2026-09-18: the detail pane's poll hit a lock held by the daemon and took the whole TUI with it (issue 43).

Every TUI timer callback that reads the database is therefore wrapped in `src.db_poll.guard_db_poll`. It catches `sqlite3.OperationalError` (a lock, and nothing else), leaves the widget's state untouched, and returns, so the next tick retries: `DetailsPane.refresh_data` on its two-second interval, and `ScraperApp.refresh_subscription_rows` — reached by both the one-shot subscription-marker poll and the subscribe pass's fast-path callback. A user-initiated action is deliberately **not** guarded: a search, a button press or a queue toggle still reports its failure, because its failure is the answer the person asked for.

**The log does not grow per tick.** The daemon log is large and unrotated (issue 37), so a lock that persists would otherwise write a warning every two seconds forever. `db_poll.guard_db_poll` logs the first failure of a run at warning and repeats at debug until a read succeeds, at which point the next failure warns again. The stats screen's per-metric failures go through the same reporter (`src/metrics.py`), because its scheduler retries a failed metric on the next tick.

The stats screen's own read runs on a worker (`exit_on_error=False`), so it was never able to kill the session the way the timer callbacks could; its tolerance is the per-metric catch plus that throttled log. A test walks every `set_interval`/`set_timer` callback in `src/tui.py` and requires each one either to carry the guard or to be listed with the reason it does not read the database, so a poll added later cannot reintroduce the crash by forgetting.

---

## Stats Screen

### `StatsScreen` (`src/tui.py:104`)

Opened by Ctrl+R. The screen asks `src.metrics` for named metrics and draws each into its own
labelled section — one widget per metric — so a chunk appears the moment its own query
finishes without touching any other. Nothing is grouped or classified by cost. The twelve
metrics and what each section renders:

| Metric | Rendered as |
|---|---|
| `high_water` | the last successful API fetch as a timestamp, or "never" |
| `totals` | live/dead item counts with the overall total |
| `app_tracking` | the per-AppID tracking table |
| `status_counts` | the status distribution |
| `dead_queued` | the dead-items-still-queued counter, with an all-clear at zero |
| `stuck_work` | the stuck-work callout |
| `queued_nowhere` | the items-in-no-queue counter, with an all-clear at zero |
| `fetch_recency` | fresh / stale / never-attempted counts |
| `coverage` | coverage bars over live items |
| `translation_status` | the translation classification |
| `tag_counts` | the tag table |
| `priority_breakdowns` | per-queue waiting counts by priority |

**Layout.** The screen is two columns. The metrics scroll down the left; `tag_counts` is the
exception and gets the right-hand column to itself, filling the screen height and scrolling within
it. Tags are the one metric that is a long list rather than a handful of numbers, so drawing it in
the same column pushed every section below it off the screen. Where a chunk is drawn has nothing to
do with when it is requested: the ordering below is unaffected.

Coverage (`_format_coverage`, `src/tui.py:396`) is drawn as a labelled progress bar per
stage — API data, description, image, translation, creator — against the number of live
items, with dead items excluded because they can never be covered. `stuck_work`
(`_format_stuck`, `src/tui.py:418`) names any dead items still flagged in a queue and says
the queues will not drain until they are cleared; a zero value shows an all-clear. The two
handoff counters share `_format_handoff_metric`: `dead_queued` names any dead items still
holding a flag, and `queued_nowhere` any live items in no queue the pipeline never completed,
each drawn as a green sentence when it reads the healthy zero. `stuck_work` and `dead_queued`
are not duplicates: the scalar `dead_queued` is the invariant that must read zero, and the
`stuck_work` breakdown says which queue still holds the dead rows, so a non-zero scalar sends
the reader to the breakdown for the diagnosis. The
priority section (`_format_priority`, `src/tui.py:439`) reads as queue state — "Translation
queue: N waiting" followed by the priority mix — rather than a raw column dump. `high_water`
is the one metric whose `None` is a real answer ("never"), not a failure.

Each metric's heading quietly carries the `ms` it measured on its last run, so its cost is
visible without being a label.

### Ordering and per-metric refresh

One thread worker streams the pass (`self.run_worker(..., thread=True, group="stats",
exit_on_error=False)`, `src/tui.py:257`; worker body `_stream_metrics`, `src/tui.py:269`).
It uses `metrics.iter_metrics(...)` — one shared connection — and applies each `(name, entry)`
to its own widget from the UI thread as it arrives (`_apply_metric`, `src/tui.py:306`), so a
chunk is drawn as soon as its own query returns rather than when the slowest one does.
`on_unmount` (`src/tui.py:211`) cancels the group so a late result cannot touch a closed
screen.

The order metrics are *requested* in is the only global decision. The first pass uses
`metrics.all_names()`, the seed order; afterwards `_request_order` (`src/tui.py:224`) sorts by
the duration each metric actually took last time, falling back to its seed hint while
unmeasured, so the order follows the data. If a query gets cheap or expensive the display
reorders itself with no code change, and cheapest-first means a slow query is never started
ahead of a fast one that is already due.

Refresh is likewise per metric. A metric is re-run once its own interval —
`max(2 s, 50 × its own measured duration)` (`_interval_for`, `src/tui.py:220`) — has elapsed;
the UI-thread scheduler asks `_due_metrics` (`src/tui.py:244`) and starts a pass over only the
due metrics. A slow metric's long interval therefore cannot stretch a fast metric's refresh
out, and `_inflight` keeps a metric that is still computing from being started again. The
pre-rework screen applied one `50 ×` rule to the whole payload.

### `compact_tag_ids` Integration

The tag-frequency compaction still runs off the UI thread when the `tag_counts` chunk is
computed — once per tag-metric arrival (`src/tui.py:284`) — rather than on the UI thread on
every screen update.

---

## Daemon Management

### `DaemonController` (`src/daemon_control.py:89`)

Owns the daemon process for both UIs: `start`, `stop`, `restart`, `status`, `read_pid`, `is_running` and `tail_log`. It launches `python -m src.daemon_runner <config> --daemon` detached (`DETACHED_PROCESS` on Windows, `DEVNULL` stdio elsewhere) and keeps the Popen handle (`DaemonController.proc`) for liveness and forced shutdown.

The PID-file protocol is unchanged. On Unix, stop sends SIGTERM then deletes `.daemon.pid`; on Windows it deletes the file. Either way it waits up to 15 s for exit, then escalates (Popen terminate/kill, `TerminateProcess` via ctypes on Windows, SIGKILL on Unix). `start` while running and `stop` while stopped are idempotent no-ops.

### `DaemonManagerScreen` (`src/tui.py:528`)

Allows starting, stopping, and restarting the daemon process from within the TUI. It no longer holds the process logic; every button delegates to the app's single `DaemonController` (`src/tui.py:1353`), which is the same instance handed to the embedded web server, so a start or stop from either UI is visible to the other. The status text and `PID: n` display read through `DaemonController.status()`.

Every transition runs on a worker thread (`_begin_transition`), because each one blocks: `stop()` polls the process every half second for up to `STOP_TIMEOUT_SECONDS` (15 s) and then waits up to another 3 s for a forced kill, and `restart()` is `stop()` followed by `start()`. Called straight from the button handler that held the Textual event loop for the whole shutdown — no keypress, no screen change and no timer, including the log poll below, which fell silent at the moment its output was most wanted. The result comes back through `call_from_thread`, the three transition buttons are disabled while one is in flight so a second press cannot start an overlapping shutdown, and `restart` is passed to the worker as a single call rather than stop-then-start so the halves cannot interleave. A controller fault puts the controls back rather than leaving the screen disabled with no way out.

The screen's log pane (`RichLog`, `src/tui.py:551`) is fed by `_poll_tail` (`src/tui.py:593`) on a two-second timer: it calls `DaemonController.tail_log` with the byte offset of the last line shown, writes the returned lines, and clears the pane when the response sets `reset`. That call is the same bounded preview the web UI's daemon panel reads through `/api/daemon/log` — at most 64 KiB and 500 lines per poll — so the pane never scans the whole log. The screen previously spawned `tail -f` for this and the pane was disabled as too slow; that subprocess is gone, and the timer is stopped in `on_unmount`.

---

## Embedded Web Server

### `_start_webserver`

Starts a Waitress server in a daemon thread serving the Flask app. Waitress binds the listening socket itself (`create_server`), and the TUI reads the bound port back from `server.effective_port`, so the port it records is the socket the server holds — there is no probe-then-close window for another process to take it. Port selection:
1. If `config["web"]["port"]` is set, binds to that port
2. If the configured port is in use, falls back to an ephemeral port
3. If no port is configured, uses an ephemeral port and persists it to config via `save_config`

The chosen port is stored as an `int` (Waitress reports it as a string). If startup fails after the socket was bound, the server is closed so the port is not left occupied.

The server shares the TUI's database connection path (set via `init_webserver`). It also shares the `_sessionid` global for subscribe operations.

### Subscribe Action (Ctrl+B)

Sends a POST request to the local web server's `/api/subscribe/<workshop_id>` endpoint. The server proxies the subscribe request to Steam using the stored session ID. Handles Steam response codes (success=1, expired=2, permission denied=15, limit reached=25). The route builds its request from the shared helpers in `src/subscribe_engine.py`, so the single-item action and the queue screen present the same identity and the same form.

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

The "Jump to Author" button replaces the current filter set with a single `Author ID is <creator>` filter, built as the row's initial filter rather than by assigning the Select widgets after mount (the field's Change handler is what populates the operator list, so assigning `is` first was rejected for the default text field).

Before replacing the rows, the app saves state to disk and snapshots the current filters in memory. `is_single_creator_mode` is then set, which hides "Save Filter for Scraper" and makes `save_state` a no-op so the author filter is not persisted.

The `btn-return` button leaves single-creator mode: it clears the flag, shows the save button again, restores the in-memory filter snapshot (replacing the author row and re-running the search), and lets state saving apply once more. The snapshot is in memory rather than re-read from `.tui_state.yaml` so that a state write between the jump and the Return cannot lose the filters the jump replaced.

### Subscription Queue (s/l keys)

`s` toggles `is_queued_for_subscription` on the selected item. `l` opens the queue screen
(`SubscriptionQueueScreen`), which **subscribes each queued item through `src/subscribe_engine.py`** —
there are no clickable Steam URLs any more and no browser tabs. Each row is a `Static` built with Rich
`Text.append`, so a Steam title never reaches a parser (see
[Steam text is escaped before it is rendered](#steam-text-is-escaped-before-it-is-rendered)).

For each item the engine reads the page's server-rendered `#SubscribeItemBtn`, sends the subscribe POST
only when the button says the item is not subscribed, and confirms from a second page read. `Subscribe`
runs the pass on a worker thread and draws each item's outcome in place as it lands; a verified
subscribe records `mark_own_subscribed`, which clears `is_queued_for_subscription`. Failures and
disagreements stay queued. Both page reads wait the shared adaptive web interval, and the pass takes
`.pauselock` for its duration and releases it in a `finally` — so the daemon's web and image workers
pause for the pass and resume even if the engine raises. The screen also creates the lock on mount and
removes it on unmount, so the queue stays quiet while it is open. No live Steam call happens in tests;
the engine's fetch and POST seams are patched. See
[data-pipeline.md](data-pipeline.md#subscribe-engine-browser-free) for the engine's semantics and
[future-plans.md](future-plans.md#retiring-the-subscribe-confirmation-read) for the planned retirement
of the confirmation read.

**Each row's marker is the item's real state**, read back from the database after the engine reports an
outcome and rendered through `src/subscription.py`, the same table the list row and the detail pane
use. Before that the row changed only its status word and the final tally: every row drew the green
`pending` outline whatever happened, including a row whose subscribe had just been confirmed. A
confirmed subscribe moves that row to the yellow ★; an outcome that leaves the item queued (throttled,
refused, a disagreement) leaves it on the green ☆, which is what `mark_own_subscribed` not being called
means. `get_queued_items` now carries the three subscription columns so the screen can draw that state
rather than assuming it.

**Watching the queue drain.** While the pass runs, the screen ticks four times a second (the web
overlay's cadence) and gives every row still waiting an estimated whole number of seconds until the
engine reaches it, shown as `~24s`; the row the engine is reading now carries `subscribing...` in place
of a countdown, and a reported outcome drops the countdown and keeps its status word. The estimate is
deliberately an estimate, not a promise: the pass can be refused, throttled or cancelled after it is
drawn, and the screen's own status line says so. It is built from the configured web delay — the same
`daemon.web_delay_seconds` the engine reads through `src.web_worker.configured_web_delay` — times
**two**, because each item costs two gated page reads (the pre-read and the confirmation read) while
the subscribe POST is an XHR and pays no interval. The delay is read fresh on every tick, so a throttle
that doubles the engine's `WebInterval` mid-pass moves the estimate with it. This differs from the web
overlay's countdown on purpose; see
[web-ui.md](web-ui.md#subscribe-feature) for why the two cover different flows.
