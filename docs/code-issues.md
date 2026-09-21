# Code Issues

Known defects in the current source. Each entry was re-checked against the code rather than carried
forward from an earlier list, and resolved entries are deleted rather than marked as fixed.

* **Checked against source**: `d6344f4`.
* **Snapshot, not a tracker**: re-read the code before acting on an entry.
* **Priorities** are judgement calls about impact, not measurements.
* **Status**: `Open` (a defect), `Unverified` (a claim not yet tested), `Informational` (true and
  worth knowing, but not a defect to fix).

Each entry describes only what is wrong and points at the documentation that describes how the
system is meant to behave; the fix is not specified here. Figures marked *measured live* come from
the production database on 2026-09-12.

## Open

### Issue 8

**Migration chain still references the dropped `tags` column** — *Informational*, Info

`CREATE TABLE` keeps the historical `tags` name because migrations 1→2 and 5→6 read it (`src/database.py:889`), and a later migration drops it defensively while logging the skip. Fresh databases must therefore replay the old shape. [schema-migrations.md](schema-migrations.md).

### Issue 9

**`status = 206` is never written** — *Informational*, Info

The migration that flags items needing a web scrape treats `206` as a partial-data status that still needs one (`src/database.py:1123`), but no code writes it and the live database contains zero such rows. [data-model.md](data-model.md#known-gaps).

### Issue 21

**The pacing delays are still hand-editable config rather than state** — *Open*, Low

**The ceilings are gone** — `api_delay`, `web_delay` and `image_delay` are no longer clamped from above, and the entry that used to stand here argued they should be removed together with their config storage. The bounding went first and on its own, because a throttle has to be able to move the rate (a fixed pause could not converge), and because the reason for bounding turned out not to hold: the delay doubles only when an attempt fails and the next attempt is a whole delay away, so after k refusals it is `d0 * 2**k` while the elapsed time to reach it is only `d0 * (2**k - 1)`. It tracks the length of the outage instead of outrunning it, and a sustained failure needs no cap. **What is left is the other half.** The three delays are still kept in `config.yaml` and written back as they move — `api_delay_seconds`, `web_delay_seconds` and `image_delay_seconds` — which is what lets an operator reset one by hand to a low value when it misbehaves. That is deliberate for now: the escape hatch stays while the new pacing beds in. The destination already exists — `src/daemon_state.py`, and the `.daemon_state.yaml` beside the database that holds the translator's backoff, with sections that merge so each worker can own one. **The migration must keep a cheap reset path**, since that is the property the config storage provides and trading one storage location for another while losing it would be a downgrade. The write rate is now bounded by `pacing.PERSIST_STEP_SECONDS`, which replaces the `API_DELAY_PERSIST_STEP` this entry used to name. **The translator is not part of this, and its not matching the other three is not a gap to close.** Its backoff has never failed to fire when it should, nor fired when it should not, so it was never distrusted: it was never given a config key, and it has nothing to migrate. The state file it now keeps is a way to survive a restart, not the first step of a migration. Its ceilings are a different device with a different purpose as well — they bound how long a condition that waiting cannot fix, a rejection needing a human, is left alone, so a raised spend limit is picked up without a restart (`src/translator.py:18`) — rather than containing false positives. It is the trusted one of the four until it proves otherwise.

### Issue 24

**The outbox is never pruned** — *Informational*, Info

The original entry here said the web-download capture claimed a budget it did not have. That part is fixed: `record_web_download` now states that it is unbounded on purpose, with the reason and the measured cost, rather than promising a budget that never existed. The capture being unbounded is a **deliberate choice** recorded where it was made — `tests/test_scrape_capture.py` explains it: it is a switch that is on for a session or two, and thinning a sample before anyone has looked at it just means collecting the evidence twice. So there is no defect left in the capture itself. What remains is a property of the whole outbox rather than of any one writer: nothing prunes it — not the web-download capture, not the failure tree, not the snapshots — so its growth is bounded only by what each switch is configured to do, and *measured live* it held 3.1 GB of database snapshots and 94 MB of web-download captures. Whether it should ever prune is the owner's decision and is not recorded here as a defect; this entry exists so the decision is not lost.

### Issue 25

**An artifact from a previous outbox layout is never removed** — *Open*, Low

`<outbox>/db` holds `workshop-backup.db.gz` — 672.9 MB, written 2026-09-12 — beside the current `workshop-backup.db` of 1,915.4 MB. `DB_SNAPSHOT_REL_PATH` is `db/workshop-backup.db` (`src/backup.py`) and the only `gzip` reference left anywhere in `src/` is a request header (`src/web_scraper.py`), so no code path writes, reads or deletes the compressed file: it is a leftover from a layout the module has since changed. **It is now reported** — `_warn_about_stale_artefacts` names any file in that directory the build does not manage, with the total size, once per process, because silence about a file that size is how it goes unnoticed. It deliberately does not delete anything: a backup artefact is exactly the kind of file that may have been kept on purpose, and deleting from someone's outbox is not this module's call. So the 672.9 MB still needs removing by hand. Separately, a snapshot is still written as a complete second copy before `os.replace` publishes it — the temp file lives in the destination directory on purpose (`src/backup.py`) — so each run needs roughly twice the database's size free, and `_check_free_space` only logs a warning when that is not available.

### Issue 36

**The discovery guard's message names a population it does not measure** — *Open*, Low

The line reports `count_never_fetched_items` as "items that have never been fetched but are not queued" (`src/daemon.py:1300`), but that function (`src/database.py:2759`) counts every row with `api_fetched_at IS NULL` and applies no queue predicate at all; the comment above the guard goes further and calls the two populations "disjoint". *Measured live* on 2026-09-18 14:35 UTC: 336 such items — 133 dead (`status = -1`), 87 queued at `api_priority = 3`, and 116 neither queued nor dead, every one of which carries `status = 404`, the API's answer that the item does not exist. So 220 of the 336 do not fit the description, and 87 sit in both populations the comment calls disjoint. The figure is therefore not a measure of stranded work: it has a floor of roughly 250 items that will never be fetched whatever the daemon does, and the part that moves is the queued remainder, which tracks discovery. Over the 24 hours to 2026-09-18 14:00 UTC, containing six restarts, it oscillated between 240 and 476 with no step at any restart — two seconds after the 09:14:20 start it read the same 463 the database already held, the climb having happened earlier while the daemon was running — and the genuinely unqueued population measured 116 at both 02:16 and 14:35 UTC. [data-pipeline.md](data-pipeline.md#discovery-phase)

### Issue 65

**A hand-started second daemon migrates under the first** — *Open*, Low

`daemon_runner.main()` writes `.daemon.pid` unconditionally and then calls `initialize_database`, with no check for a daemon already running, so starting `python -m src.daemon_runner` by hand while another runs overwrites the live PID file and applies pending migrations under it — the hazard issue 63 just closed for the UI path. The first daemon watches for the file's *absence* rather than its contents, so it keeps running; the two then share one PID file, and whichever exits first removes it and stops the other. `DaemonController.start()` already checks `is_running()` before spawning, so the UI path is guarded and this is the operator-error path only. Closing it needs a read-before-overwrite probe of the existing PID file plus a liveness check, which conflicts with the deliberate “write the PID file before migrating” order and revives the stale/recycled-PID hazard the controller was fixed to avoid — so it is recorded rather than patched. [threading.md](threading.md), [cross-platform.md](cross-platform.md)

## Recently closed

Removed from the list above rather than marked resolved. Each is now documented as current
behaviour, or covered by a test:

### The daemon log is never rotated (issue 37)

The log now has a bound, but **not an automatic one**. The owner keeps a persistent `tail` open in another window, and a rotation the daemon chose on a timer or a size threshold would have disrupted that view without warning, so rotation is **wholly manual**: the daemon page of each front end carries a small size readout and a **Rotate Log** button, and nothing rotates on a timer, on a size threshold, or at startup. Pressing it renames the live log to `<log folder>/logs/<stem>-<UTC stamp>.log.gz`, immediately leaves a fresh empty file at the configured path, and compresses the archive on a background thread — the readout reads `Rotating… (<size>)` while a production-sized gzip runs, then `Rotated: logs/<name> (<size>)`. The readout line and the button's label come from one place (`src/log_rotation.py`), so the TUI and the web show the same number and the same words.

The part that needed the care was that **two processes hold the log open** — `src/daemon_runner.py`'s handler and the TUI's — so a rename alone would leave one writing into the renamed inode, and those records would end up inside the compressed archive silently. Both handlers now come from `src/log_rotation.log_file_handler`; the rotator publishes the archive's name to `<log>.generation` and each handler caches that marker and reopens when it changes. That mechanism was chosen over `logging.handlers.WatchedFileHandler` because the latter does not reopen on Windows, which is where production runs. A test holds a handler across a rotation and fails against the old plain `FileHandler`, asserting the next record lands in the new file and not in the sealed archive. **No retention policy was invented** — archives accumulate, and when to delete the owner's log history is the owner's decision, the outbox question of issue 24 in another form. [config-security.md](config-security.md#manual-rotation-and-why-it-is-manual), [tui.md](tui.md#daemonmanagerscreen), [web-ui.md](web-ui.md#daemon-panel)

### Dead items kept their queue priority

They held `api_priority > 0` long after they were known to be gone, because the rows predate the permanent-failure path clearing it. Migration 19→20 zeroes it for `status = -1`, so a count of queued fetches is honest again and the `dead_items_by_queue` statistic — which reports dead items still holding a queue flag — reads zero unless something has genuinely regressed

### Single-creator mode could not be exited

`btn-return` now has a handler: `action_return_from_author_mode` clears `is_author_mode`, shows "Save Filter for Scraper" again, and restores the in-memory filter snapshot the jump replaced before re-running the search. The jump also builds its author row from an initial filter, because assigning the Selects after mount raised `InvalidSelectValueError` for the `is` operator — [tui.md](tui.md#jump-to-author)

### `action_update_visible` could re-queue dead items

The TUI's bulk update carries the same `AND (status IS NULL OR status != -1)` guard as `/api/update_visible` (`src/webserver.py`), so an item known to be gone is not put back in the API fetch queue — [data-pipeline.md](data-pipeline.md)

### `btn-request-translation` was a dead handler for a button that no longer exists

The event branch and its call to the non-existent `action_request_translation` were removed; neither the id nor the method is referenced anywhere in `src/tui.py`

### Port selection raced between probe and bind

`_start_webserver` lets Waitress bind the listening socket (`create_server`) and reads the port back from `effective_port`, so the recorded port is the one served on and there is no probe-then-close gap. A busy configured port still falls back to an ephemeral one, and the chosen port is still persisted to config — [tui.md](tui.md#_start_webserver)

### The web UI reported a filter save that failed

`/api/save_filter` still answers **400** with "No target AppID configured" when no target AppID is set (`src/webserver.py:408`), but the client now reads the response: a non-2xx shows the server's message, a network error shows its own, and only a 2xx claims success (`templates/index.html:719`) — [web-ui.md](web-ui.md)

### `#port-display` was never populated

`showServerPort()` fills the header span from `location.port` on load (`templates/index.html:191`), so the port the embedded server answered on is readable without a request and follows a reconfigured port rather than a baked-in literal — [web-ui.md](web-ui.md)

### The bridge retried a dead backend every five seconds, forever

A failed session push now backs off from five seconds, doubling to a one-minute cap, for at most six attempts before the chain stops; a success resets the schedule, one chain runs at a time, and the thirty-second interval, change-detection and slow re-push are unchanged (`userscripts/steam_subscribe.user.js:279`) — [web-ui.md](web-ui.md)

### Dead items stayed in the scrape and image queues forever

Marking an item dead now clears `web_scrape_priority`, `image_priority` and `translation_priority` along with `api_priority` (`src/daemon.py`), so a dead item is in no queue; migration 16→17 cleared the rows already stranded that way. `dead_items_by_queue` in the statistics reports any that reappear — [data-pipeline.md](data-pipeline.md)

### Items found by cursor discovery were never queued for an API fetch

Cursor discovery now passes `api_priority = 3`, the documented new-item priority, explicitly rather than leaning on the column default, so a discovered item is queued whatever the database's history; migration 17→18 requeues the rows the default had already stranded. A test builds a fresh database and a genuinely migrated one and fails if a discovered item is not queued on either — [data-pipeline.md](data-pipeline.md#discovery-phase), [schema-migrations.md](schema-migrations.md)

### The TUI daemon log pane never received a line

`DaemonManagerScreen` polls `DaemonController.tail_log` every two seconds and writes the returned preview into the pane, clearing it first when the server reports `reset` (`src/tui.py:953`). Every read is bounded to `TAIL_BYTES`, the same call the web panel makes, so the pane shows the log's tail without scanning the file; the dead `tail -f` subprocess is gone — [tui.md](tui.md#daemonmanagerscreen)

### The bridge rewrote `config.yaml` every thirty seconds

The periodic push carried an unchanged cookie, and every push persisted it — a YAML serialisation and a file write per open Steam tab, plus an info line. The push is still relevant (`session.read_firefox_cookies` defaults to off, and the CSRF token feeds server-side subscribe), so it stays; it is now sent on load and whenever a value actually changes, with a slow re-push as the safety net for a restarted backend, and logged at debug — [web-ui.md](web-ui.md)

### The Windows daemon liveness check killed the daemon it was checking

`os.kill(pid, 0)` is not a probe on Windows — any signal other than the two console events goes to `TerminateProcess`, so the check terminated the process and returned True, leaving the `win32` fallback beneath it unreachable. The TUI mostly escaped it by short-circuiting on its own Popen handle, but the web panel polls status every two seconds and would have shut down an externally started daemon on the first poll. `DaemonController` now probes with `OpenProcess(SYNCHRONIZE)` and a zero-timeout wait, and a guard test fails if `os.kill` is ever used as a Windows probe again — [cross-platform.md](cross-platform.md)

### Failure captures truncated before the region that would explain them

Script and style bodies are removed before the cap, so the byte budget is spent on markup and the digest describes the document — [failure-capture.md](failure-capture.md)

### `request_delay_seconds` was accepted without deprecation

Retired: the old spelling is no longer read, and one warning per process names the replacement — [config-security.md](config-security.md)

### The userscript version had to be kept in step by hand

`tests/test_userscript_contract.py` fails when the userscript and the template disagree — [web-ui.md](web-ui.md)

### A malformed config crashed with an unhandled traceback

`load_config` raises `ConfigError` naming the file and position; every entry point reports it and exits non-zero — [config-security.md](config-security.md)

### The description scraper sent no cookies

It sends `sessionid` and `steamLoginSecure`, read from the local Firefox profile and re-read whenever a page looks gated; `/api/sessionid` can still override by persisting to config — [config-security.md](config-security.md)

### The suite needed stray `test.db` and `workshop.db` files to pass

Fixtures use an initialised temporary database, so a clean checkout is green: 15 failures to 0. The suite still creates two empty files in the working directory, which nothing reads

### The translation thread retried a failed batch with no backoff

It backs off now — 2 s→5 min for service failures, 60 s→1 h for account-level ones — [threading.md](threading.md#translation-thread-translatorthread)

### The translation backoff reset on every restart

The delay and the streak behind it lived only in the thread's memory, so restarting the daemon answered the next failure with the base delay again: an account-level rejection that had already climbed towards its hour was retried a minute after every restart, and the log repeated the same line each time. The streak and the moment the next attempt falls due are now written to `.daemon_state.yaml` beside the database, so a restart resumes the backoff and waits only the remainder instead of serving the whole delay again; the first batch that gets through removes the section, leaving nothing behind on the happy path. Both values are clamped on the way back in, because that file is input as far as the thread is concerned — an unclamped streak would build a gigantic integer before any cap applied, and a timestamp far past the cap would park the thread. The store is injected, so a `TranslatorThread` built without one is unchanged — [threading.md](threading.md#translation-thread-translatorthread)

### Full-text index missed 62.9% of the library

Migration 14→15 rebuilds it and installs sync triggers — [search-filter.md](search-filter.md), [schema-migrations.md](schema-migrations.md)

### The shipped example config was not valid YAML

Fixed, with a regression test that it parses

### Background paths re-queued already-translated fields

`translation_is_current` gates every trigger — [data-pipeline.md](data-pipeline.md#what-queues-a-field-for-translation)

### Version keys were written but never compared

`translate_version` now drives re-translation — [timestamps.md](timestamps.md)

### Silent `except ...: pass` handlers were unaudited

All 24 classified; each states why silence is safe, or logs and captures

### The `_SafeStreamHandler` startup message was unverified

Verified accurate: the handler list is complete before `basicConfig(force=True)`, and the message reads back `root.handlers` (`src/daemon_runner.py:96`)

### Priority decay was asymmetric across queues

Superseded: the intended model is change-driven, not per-queue sweeps

### Discovery was skipped while the queue held work it could not fetch

The guard counts fetchable work, and migration 15→16 requeued the rows the old policy stranded — [data-pipeline.md](data-pipeline.md#discovery-phase)

### Opening a detail pane re-queued the same item every three seconds

Detail priority is applied once, on open; the poll uses a read-only route — [web-ui.md](web-ui.md), [tui.md](tui.md)

### Transient API failures were dequeued instead of retried

Permanent failures dequeue; temporary ones step down and stay queued — [architecture.md](architecture.md#reliability), [data-pipeline.md](data-pipeline.md#failure-classification-daemon)

### Image work was re-queued on every fetch, changed or not

Both stages follow the API refresh as the change detector — [data-pipeline.md](data-pipeline.md#change-detection-across-stages)

### A throttle page was mistaken for a dead item and for a stale cookie

Recognised from the body before the selector miss; the scraper pauses for 5 minutes instead of decaying the item, and the two checks are evaluated independently so neither shadows the other — [data-pipeline.md](data-pipeline.md#web-scraping-phase)

### The subscribe bridge could not tell throttling from a missing button

The userscript detects Steam's throttle page and reports it separately, so the tab stops on throttling rather than counting it as a failure — [web-ui.md](web-ui.md)

### A page without the description was recorded as a successful scrape and dequeued

The worker tested the truthy dict instead of the `description` inside it, so a selector miss wrote `extended_description = NULL` with `needs_web_scrape = 0` and the item left the queue forever — 63,229 of the 70,378 "done" rows on the 2026-09-12 snapshot. Migration 17→18 requeues those rows at backlog priority, and the worker now tells a page that was never the item's (left queued) from the item page with no description (dequeued, since no retry can change it) — [data-pipeline.md](data-pipeline.md#web-scraping-phase), [schema-migrations.md](schema-migrations.md)

### A page that was not the item's never slowed the scraper

The selector-miss handler touched neither `web_successes` nor `web_failures`, so however many walls, error pages or missed throttle pages the scraper hit, `web_delay` never rose and it kept knocking at the same rate while the server was refusing to serve content. That outcome became a failure for pacing, through the same counters and the same rule as a transport failure (`_record_web_failure`), so repeated unrecognised walls grow the delay, while the log line names what was seen — a body that is not the item's — rather than asserting a cause. The item is still left queued at its priority, and a genuine description-less item page is neutral: the request succeeded and the item is done with, but it yielded nothing, so counting it as a success would let the delay fall again while walls continued — [data-pipeline.md](data-pipeline.md#web-scraping-phase). The back-off was later narrowed to unattributable outcomes only; see the next row

### A missing item was indistinguishable from a transport failure, and both bought a back-off

`scrape_extended_details` caught every `RequestException` and returned `None`, so `raise_for_status()`'s status and body were discarded: an HTTP 404 read as a timeout, the item kept `needs_web_scrape`, and the worker grew `web_delay` for an item that does not exist. Worse, a live probe shows the Workshop serves its item-error page with **HTTP 200** ("There was a problem accessing the item" for a well-formed but absent id), so the status was never even available to the caller. `HTTPError` is now caught separately and returned as a miss dict carrying `http_status` and `body`; that plus the page wording feeds a named outcome taxonomy (`ScrapeOutcome`), in which only an unknown outcome grows the delay, a rate limit slows the scraper like any other refusal (it served a fixed 300 s pause at the time, which a later change replaced with a rate that moves), a missing item and a description-less item page clear the item's queue flag, and a gate leaves both alone. The status and the matched wording are logged, since the page is HTTP 200 — [data-pipeline.md](data-pipeline.md#web-scraping-phase)

### The image back-off treated a missing image as rate limiting

`image_answer` now records the server's *answer* as well as a file type, and a permanent answer is neutral for pacing: it neither grows `image_delay` nor breaks a run of successes. The captures settled the question the old entry said had never been looked at. A 404 is an nginx `text/html` body of 92 bytes carrying no rate signal at all — no 429, no `Retry-After`, no throttle page — while a read timeout has no status and no headers; a real preview arrives as `Content-Type: image/jpeg` with a real length. Over one 80 MB window the split was 6,517 404s to 163 timeouts, so 97.4% of image failures belonged to the class that says nothing about our request rate, and the delay had been moved almost entirely by the wrong signal — [data-pipeline.md](data-pipeline.md#image-download-phase), [failure-capture.md](failure-capture.md)

### A 404'd image was re-queued forever, taking an API fetch with it

The status is written into `image_answer`, `image_priority` is cleared, and `api_priority` is no longer raised — that raise was what asked for the API refresh that re-flagged the image, so the cycle is broken at its cause rather than its symptom. Both re-flag gates treat a permanent answer as settled: the daemon's `_raise_scrape_and_image_priorities` and the web server's `_ensure_image_flagged`. A transient status stays retryable, a non-image content type is final, and a revised item still re-fetches a *real* image, because the preview may legitimately have been replaced. One item had been fetched twenty-five times in a single day for a preview that never existed — [data-pipeline.md](data-pipeline.md#image-download-phase), [data-model.md](data-model.md)

### A valid snapshot was discarded for being a few rows old

The verifier required the snapshot's `workshop_items` count to *equal* the source's, read after the copy had finished. The daemon writes throughout, so the source always held more: **54 such failures against 25 successes** in one window of the live log, with deltas of 52–199 rows out of ~2.1 million, which left the published snapshot hours stale for most of the day. Equality is not a testable claim against a live WAL database, and it was never the question worth asking — `VACUUM INTO` guarantees a consistent point-in-time copy, so being behind a moving source says nothing about whether the copy is valid. The count is now reported at debug level instead of enforced, and the checks that remain catch a snapshot that is *wrong* rather than merely older: corruption (`PRAGMA quick_check`), a different schema version — which does not move while the daemon runs, so it is a fair comparison and it is what now answers "is this our database" — and an empty snapshot of a populated source.

### Stopping or restarting the daemon blocked the whole TUI

`DaemonManagerScreen.on_button_pressed` called `controller.stop()` / `.restart()` straight from the button handler, and both block: `stop()` polls the process every 0.5 s until it exits or `STOP_TIMEOUT_SECONDS` (15 s) runs out, then escalates to a forced kill with up to 3 s more, and `restart()` is `stop()` followed by `start()`. For roughly eighteen seconds **nothing** ran — no keypress, no screen change and no timer, including the manager screen's own 2 s log poll, which fell silent exactly when its output was most wanted. Every transition now runs on a worker thread with the result applied through `call_from_thread`; the transition buttons are disabled while one is in flight, so a second press cannot start an overlapping shutdown; `restart` is passed as one call rather than stop-then-start so the halves cannot interleave; and a controller fault puts the controls back instead of leaving the screen disabled. `tests/test_daemon_manager_screen.py` asserts the click returns *before* the shutdown finishes, which fails against the direct call.

### `api_priority = 2` sat outside the documented scale

It is now part of it: the Queue Priorities table in [data-model.md](data-model.md#queue-priorities) lists `2` as "retry after a stage failure, `api_priority` only", which is exactly what the image worker's download-failure path (`src/image_worker.py:201`) and the web worker's request-failure path (`src/web_worker.py:336`) write it for. The alternative was to change the value, which would have re-prioritised every stage-failure retry, so the vocabulary was completed rather than the code changed.

### A web scrape was re-queued for items whose description was already current

`_raise_scrape_and_image_priorities` has two paths and only the enriching one tested the revision. The other — an item that does not match its AppID's enrichment filters — queued a scrape unconditionally, so every API refresh re-queued one for a description that was already at the item's current revision; *measured live*, 120 of the 570 items queued for a scrape held a current description. It was worse than merely redundant, because that path passes the item's `api_priority` in as the scrape's priority and opening a detail pane sets `api_priority = 10` — so **looking** at such an item queued a priority-10 scrape and drew it as pending in both front ends. Both paths now share one `description_is_current` test. The borrowed priority is deliberately left alone, since changing it would re-prioritise every scrape, and is pinned by `test_process_batch_inherits_priority` so that a later reader does not mistake it for an oversight.

### `btn-close-subscription-queue` was created by two screens

`StatsScreen` had copied the id from the subscription queue screen along with its handler, so two screens answered to one name. Textual scopes queries per screen, which is why both kept working and nothing failed — the id simply named the wrong screen. `StatsScreen`'s close button is now `btn-close-stats`, and `test_no_two_widgets_share_an_id` fails if any widget id in `src/tui.py` is ever duplicated again, so the next copy-paste is caught rather than shipped.

### `update_app_tracking` and `update_app_tracking_page` were declared but unused

`update_app_tracking_page` had no caller anywhere and no test, so it is gone. `update_app_tracking` is exercised by `tests/test_database.py` and stays, but the daemon imported both without using either — `update_app_tracking_cursor` is what the discovery loop calls (`src/daemon.py:1363`) — so both dead imports are removed. What is left is a DB-layer function whose only callers are tests, writing a column nothing reads; that is recorded as issue 30 rather than pretended away.

### `scrape_version` was written by two workers, and one overwrote the other

The column records the revision the *page* was scraped at, and the image worker wrote the item's `steam_updated_at` into it on every download — so an item whose page had never been scraped still claimed a scrape at its current revision. The web worker is the writer that gives the column its meaning; the image worker no longer touches it, pinned by `test_a_downloaded_image_does_not_rewrite_the_scrape_version`. Nothing observable changed, because nothing reads the column — which is why it went unnoticed for so long, and is now tracked as issue 30.

### The subscription reconcile walked with a dead login and blamed the page

Its one walk a day ran on a credential that expires about daily, so a daemon that had been up since yesterday arrived at the walk with a cookie that died overnight — *measured live*, 13.7 hours dead, while `_resolve_login_secure` read a process-lifetime cache that cannot notice. The only symptom was a warning that the page "may be a sign-in wall or an error page", repeated three times, and a log line asserting no flags had changed. The reconcile now re-reads the cookie from the browser before walking, refuses one whose token says it has expired without spending a request, and recognises Steam's sign-in page — `/login/`, served with a **200** — if one arrives anyway, ending the walk there rather than retrying it. Either way the reason is recorded in `.daemon_state.yaml` through `src/session_health.py`, and the web UI reads it and offers the link that fixes it — [web-ui.md](web-ui.md#the-session-warning). While that reason is recorded the walk is retried every 15 minutes rather than daily, because the daily walk has already run for the day by the time anyone responds to the warning; the retry costs no request while the token is still expired. Pinned by `tests/test_subscription_sync.py` and `tests/test_subscription_daemon.py`, whose new cases fail against the old code.

### The steamid logged for a reconcile was "unknown" for the form the config writes

`steamid_from_login_secure` split on a literal `\|\|`, while `config.login_secure_value` *writes* `%7C%7C` for the list form and a browser stores that form too — so the one value the config produced was unreadable by the one function that read it, and every reconcile log said "steamid unknown" for a cookie it was holding. `src/session_cookie.py` now reads both encodings plus the token's `exp` claim, and the reconcile uses it; the token half is still never logged.

### The enrichment filter did not deprioritise the items it excluded

An item that fails its AppID's filters is still scraped -- the filters choose priority, not membership -- but it must not *outrank* an item they selected, and it did: the dependent queues were handed the whole pre-fetch `api_priority`, which is `3` for a newly discovered item. The commit that introduced it (`04746e1`, "implement priority inheritance") meant to cascade only a *user's* own `5`/`10`, and inheriting everything else was the over-reach. *Measured live* on 2026-09-17: 868,759 items sat above backlog priority, **760,782 web and 668,269 image entries** of those belonged to excluded items, and **107,365 selected items were queued behind them** -- at the measured 5,902 successful scrapes/day, three weeks of wanted work buried inside a year of excluded work. `user_requested_priority` now inherits only what a person asked for, `USER_PRIORITY_FLOOR` names the boundary once for both the daemon and the migration, and migration 21->22 returns the already-stamped rows to backlog while leaving `5`/`10` alone, since under either rule only a user action could have written one. Pinned by `tests/test_filter_priority_migration.py`, the three priority tests in `tests/test_filters.py`, and a `process_batch` test in `tests/test_daemon.py`; each fails against the old code -- [data-model.md](data-model.md#queue-priorities), [schema-migrations.md](schema-migrations.md#v21--v22-filter-excluded-items-give-up-their-queue-priority).

### The server-side subscribe proxy discarded a subscribe Steam accepted

Steam's `success: 1` is now recorded with `mark_own_subscribed`, setting `own_subscribed` and clearing `is_queued_for_subscription` — the same write `/api/subscribed/<id>` makes — instead of the item reading `never` until the next reconcile. The route also takes its CSRF token and full cookie set from one `web_scraper._build_workshop_cookies` read (the pushed `_pushed_sessionid` and the configured id are a fallback only when that set carries none), refuses before the request when either half is missing or the credential's token has expired, records Steam's `success: 2`/`15` as a session problem, logs every early refusal, and uses the shared scraper session and the project Firefox User-Agent instead of a bare `requests.post` with a hardcoded Chrome string — [web-ui.md](web-ui.md)

### A Steam title with square brackets crashed the subscription queue

The queue row interpolated the title into `RichText.from_markup(f"[link={url}]{url}[/link] : {title}")`, so the title was parsed as markup: `[Najar]大崎甘奈 厚乳（配音2+断面）` was silently eaten and `[najar]偶像大师 樋口円香（有断面+配音版）` raised `MissingStyle: Failed to get style 'najar'` and took the whole screen down. *Measured live*, **129,533 titles** in the library hold a bracket pair and **2 of the 9 items queued for subscription** did, so this was the ordinary case rather than an edge. The one line also masked a class of injection sites: the same unescaped interpolation reached the detail pane's title, creator and tag lines, the list rows, the stats tag table — where Textual's parser silently eats an unknown tag instead of raising — the app-tracking cursor, and the subscribe notifications. Every Steam-derived value now goes through `escape_markup`, which escapes every `[` (`rich.markup.escape` only escapes tag-shaped ones, leaving an unbalanced `[` to swallow the project's own closing tag), and the queue row appends its title with `Text.append`, so it never reaches a parser at all while the URL keeps its deliberate link style. The daemon log pane was audited and deliberately left: `RichLog` defaults to `markup=False`, so its lines are already literal. `tests/test_tui_markup_injection.py` renders `[najar]`, `[bold]`, `[/]`, `[link=https://x]`, an unbalanced `[` and a bare `]` through the queue, the detail pane and the list rows, asserts the brackets are still displayed, and asserts the project's own bold title and coloured marker survive; every widget test fails against the old interpolation — [tui.md](tui.md)

### The subscriptions walk and the subscribe route made community reads with no interval gate

The walk's page reads now wait the shared adaptive interval (`daemon.web_delay_seconds`, read fresh through `src.web_worker.configured_web_delay`) before each request, the same gate the subscribe engine's item-page reads use. The subscribe POST is the button click — a browser-initiated XHR, not a page load — and is deliberately exempt, so `post_subscribe_request` and the route never wait. The fixed 5 s gate inside `scrape_extended_details` that this walk also waited on has since been removed, leaving `web_delay_seconds` as the interval's only owner — [data-pipeline.md](data-pipeline.md#web-scraping-phase), [tui.md](tui.md)

### The cached browser cookie outlived its own expiry

`steam_community_cookies()` served the first non-empty profile read for the life of the process, and nothing consulted the `exp` its own value carries — so a daemon that read a valid `steamLoginSecure` at startup kept sending it after Steam had expired it, *measured live* dead **13.7 hours** by the time a reconcile used it, against a token that lives about a day. The remembered set is now served only while that token is live: an expiry that was read and has passed forces a re-read, and the whole set is read again together so the CSRF token cannot come from a different session than the credential. An expiry that cannot be read is still served, because unknown is not expired; a live cookie still costs no file copy; and an empty read is still not cached, so a profile that gains a login is picked up on the next call — [config-security.md](config-security.md), [data-pipeline.md](data-pipeline.md). Pinned by `tests/test_firefox_cookies.py`, whose expiry case fails against the old source.

### The item-page interval had two owners

`scrape_extended_details` enforced a fixed `_WEB_DELAY = 5.0` through `_rate_limit()`, movable only by a `set_web_delay()` nothing called — so a worker scrape paid the adaptive delay *and* the fixed one, a caller outside the worker got the fixed one whether it gated itself or not, and a back-off past 5 s still paid 5 s on top. The fixed gate is gone and `web_delay_seconds` is the interval's only owner: the scraper sends as soon as it is called, and every caller that needs spacing gates itself on the configured value through `pacing.wait`, as the worker and the subscribe engine already do. A source-scanning test fails if any of the removed names reappear, and the scraper's "applies no delay of its own" test records a second back-to-back call being made with no sleep at all — [data-pipeline.md](data-pipeline.md#web-scraping-phase)

### A gated miss earned a second request straight away

`_retry_if_gated` re-read the browser login cookie after a gate-shaped miss and then scraped the item again at once, so that retry's only spacing was the scraper's fixed 5 s gate — below the worker's own 6 s floor — and it repeated what the queue already does, since `_handle_gate` leaves the item queued in its place. The re-read survives as `_refresh_login_cookie_if_gated_or_signed_out`: the worker refreshes the credential and lets the miss flow through `classify_scrape`, so the queue retries the item under the adaptive delay like any other failure. The guard keeping a throttle page from being read as a lapsed login was kept rather than dropped with the retry — [data-pipeline.md](data-pipeline.md#web-scraping-phase). Against the old source the gated case makes two requests where the test requires one

### The `language` column was never populated

Nothing this project consumes can supply it: `GetPublishedFileDetails` carries no language field, and the `language` in its request protocol is the *viewer's* localization parameter, which the client sets and never reads back. The recorded response body holds no such key under any spelling, the page parse extracts only description and tags, and every row of the live snapshot was NULL. It is gone from the model and the merge allow-list, from `CREATE TABLE`, from `_safe_add_columns` (which would otherwise re-add it on the next startup) and from migration 4→5's index list, along with the `Language ID` filter alias and the permanently-N/A tooltip line; migration 23→24 drops the index and then the column, following the `tags` drop in 5→6 and guarded on `PRAGMA table_info` so a partial run resumes — [data-model.md](data-model.md), [schema-migrations.md](schema-migrations.md)

### The pending marker did not say what it was waiting on

`STAGES` in `src/pending.py` now carries the wording beside the multiplier and the colour, and the web UI writes it to the marker's `title`; the shared table owns the wording, so the two front ends cannot describe one state differently, and the template's mirror of it is pinned to that table by `tests/test_pending.py` exactly as the durations and colours already were. The TUI still draws no text, which is why the wording belongs in the table rather than the template — [web-ui.md](web-ui.md), [tui.md](tui.md)

### Items sat in the translation queue with nothing to translate

The cause was the opposite of the guess: `queue_field_for_translation` wrote the queue row and its `translation_priority` mirror on **two separate connections**, so a translator drain landing between them deleted the row, zeroed the mirror, and the helper's second statement re-raised it from `MAX(0, priority)` — after which no producer would requeue the field, since every one of them skips a translation that is already current. Both writes are now one transaction, `translation_priority` is excluded from the daemon's read-modify-write API merge so a pre-fetch snapshot cannot resurrect a drained priority, and migration 22→23 zeroes the mirror for items with no queue row. Reproduced on a synthetic database by injecting the translator's own drain SQL between the two connections, which is what `tests/test_translation_mirror.py` now does; 4 of its 9 cases fail against the old code — [data-model.md](data-model.md), [schema-migrations.md](schema-migrations.md)

### A grid cell kept its subscription marker after the subscribe landed

The poll's id set was what missed it: the tick collected only rows showing a *stage* spinner, and the subscription queue is deliberately not a stage, so a row whose only outstanding work was the subscribe was never re-read once `mark_own_subscribed` had stamped it. The tick now takes every `.grid-cell[data-wid]` and keeps rows with a stage spinner **or** a `queued` subscription marker, `_listNeedsPoll` starts the poll for either, and toggling a marker starts it on the transition into `queued`, because a marker clicked after the batch rendered was never seen by that check. Reading `/api/items` covers every writer with one refresh — the userscript's `POST /api/subscribed/<id>`, the page's own cancel and clear-failed calls, `POST /api/subscribe/<id>` and the daemon's reconcile — where a refresh in the verifier would have needed a hook per writer and would still have missed writes landing while the overlay was closed — [web-ui.md](web-ui.md)

### The subscribe POST posted a CSRF token from an earlier session

`sessionid` is a session cookie, so Firefox never writes it to `cookies.sqlite` and the profile read can never supply one; the POST fell back to the pushed `config.session.id`, a token from whenever the bridge last pushed it. *Measured live*, across four attempts: every item-page GET authenticated (`g_steamID` and the account markers present) and every POST refused `401 {"success": 2}`, the POST carrying exactly one cookie the GET did not — `sessionid`, fingerprinting the same as the configured value, while the page's own `g_sessionID` matched neither. The token now comes from the page the attempt itself reads — `g_sessionID`, then the cookie set's own token, then the pushed fallback, with the winner written back into the jar so the form field and the cookie cannot disagree — and the route reads its page through the same `web_delay_seconds` gate. A refusal beside an authenticated page read is reported as `token_refused` and records **no** session problem, since that attempt proved the credential good; an anonymous read still records one. The log keeps the diagnostic property that found this with `token_fingerprint`, twelve hex characters of the token's SHA-256, printing the page and fallback fingerprints whenever they differ — [data-pipeline.md](data-pipeline.md#subscribe-engine-browser-free), [web-ui.md](web-ui.md)

### A TUI row's subscription marker did not follow a subscribe that landed behind it

The results list drew each row's marker from the item data captured when the row was built and nothing re-read it, so a subscribe landing behind an on-screen row left its green `pending` outline in place until a search or a scroll re-rendered the list. `ScraperApp` now runs the web grid's `_startListPoll` in TUI form: it re-reads the subscription columns of the rendered rows that are still `pending`, redraws them, and re-arms only while one still is — and because it reads the shared database, every writer is covered (the TUI's own pass, the web UI, the daemon's reconcile), with the pass's own result refreshing its row at once as a fast path rather than as the mechanism. The subscription queue screen's rows were a separate matter, not a staleness bug: they hardcoded the pending glyph, so no outcome could change one; they now resolve the state from `src/subscription.py`, as the detail pane and the list already did. The same screen also gained the web overlay's per-row estimate of when the pass will reach each item, at two gated reads per item — [tui.md](tui.md), [web-ui.md](web-ui.md)

### A transient lock on the database ended the TUI session

`get_connection` ran `PRAGMA journal_mode=WAL` on every connection, and that statement is not covered by the connection's busy timeout — so a moment of write contention in the daemon raised on a plain read, and because the reader was a Textual timer (`DetailsPane.refresh_data`, every two seconds) the exception ended the session instead of failing one refresh. The journal mode is a persistent property of the file and is now set once by `initialize_database`. Unattended TUI reads go through `guard_db_poll`, which catches only `sqlite3.OperationalError`, skips the tick and leaves widget state untouched, while a user-initiated action still raises to the person who asked for it. A lock that will not clear does not flood the log: the first failure of a run warns and repeats drop to debug until a read succeeds, through `RepeatFailureLog`, which the statistics screen's per-metric retries use too. The browser's list and detail polls had the same shape from the other side — one failed read stopped them for the session — and now re-arm after a failure. A test walks every timer callback in `src/tui.py` and requires each to be guarded or listed with the reason it does not read the database, so the next poll cannot forget — [architecture.md](architecture.md#the-data-layer-sqlite), [tui.md](tui.md)

### A metric registered without its TUI wiring took the stats screen down

Adding a metric meant editing `src/metrics.py` **and** `src/tui.py`, and nothing said so: `StatsScreen._compose_metric_section` indexes `METRIC_CONTENT_IDS[name]` for every name the catalogue reports (`src/tui.py:253`), so a metric registered without a content id raised `KeyError` inside `compose` and ended the whole stats screen rather than one chunk. The web panel degraded instead, its renderer falling back to the JSON it was handed. *Reproduced* while adding the two handoff counters — `KeyError: 'dead_queued'` at the old screen, and one reverted test run left eight crash dumps behind — so a metric now has to carry the wiring both front ends need, or be named in an explicit exemption list with its reason. Two guards enforce it: one walks the registry and names every missing piece, the other composes the real screen and fails if any chunk is still "Computing…", which is how a missing renderer hides from a static check — [tui.md](tui.md), [web-ui.md](web-ui.md)

### The subscription queue's estimate ran well below what a pass costs

It priced two gated page reads at the configured delay, which counts the interval a read waits but not the request, and treats the exempt POST as clock-free — *measured live* about a third low (7.7–8.6 s gaps against a 6.0 s delay, a POST round trip of 1.1–1.8 s, one item at about 18 s against an estimate of 12 s). The configured guess still seeds it, and a running mean over the items the pass has finished now nudges it for the rows still waiting: one virtual observation from the seed, then each real item, so the first moves it a lot and later ones less. That is a progress bar's arithmetic rather than a rate learned from history, deliberately — the pane is up for a minute or two, and the items in front of it are the only evidence worth pricing the rest with. The processing row keeps its distinct `subscribing…` and is where each duration is timed from. The web overlay's countdown has no counterpart and should not acquire one: it is a fixed tab-open schedule rather than a projection from item costs, so correcting it would either disagree with when tabs actually open or change the pace that keeps Steam from throttling — the reason is recorded in [web-ui.md](web-ui.md) — [tui.md](tui.md)

### An item the page already showed as subscribed stayed in the queue

`subscribe_item`'s `ALREADY_SUBSCRIBED` short-circuit returned the observation without recording it: an item whose page already shows `toggled` costs no POST, which is right, but it also left `is_queued_for_subscription` set, so `get_subscription_queue_items` kept listing it and every pass spent one gated page read rediscovering it. It now records through `mark_own_subscribed` — which sets `own_subscribed`, clears the queue entry and stamps the sticky first-seen time — and still sends no request. The entry as first written also blamed the daily reconcile, and that half was wrong: `apply_own_subscriptions` has cleared the same flag for every id its walk saw since `7a9f42e`, pinned by `test_a_stale_queue_flag_is_cleared_when_the_item_is_found_subscribed`, and the entry had been written from the `own_subscribed = 1` statement without reading the one directly above it. Both halves are now stated in that function's docstring, and it gained a guard for the bound — [data-pipeline.md](data-pipeline.md#subscribe-engine-browser-free), [data-model.md](data-model.md)

### Zero read as "N/A" in the TUI and "0" in the web

**Was issue 51.** `format_count` treated a measured zero, a missing value and an unparsable one
alike, so the TUI read every zero as unknown while the web read it as a zero. A zero now prints `0`
in the gray band, matching the web's `fmtCount(0)`; only a value that is missing (`None` or `""`) or
cannot be coerced falls back to `N/A`, so the two meanings are no longer collapsed. `format_ts(0)` is
deliberately unchanged — an epoch-zero timestamp is still meaningless. The remaining divergence on
the *missing* case is recorded as issue 58.

### `translation_queue` had no index

**Was issue 57.** Every per-field lookup and the translator's completion count scanned the whole
queue — 8.6 ms each on the production-scale queue — and the 22→23 repair's correlated
`NOT EXISTS` took 8.7 minutes. The table now carries `idx_translation_queue_lookup` on
`(item_type, item_id, field)`, created in `_create_schema` so that it exists *before* the migration
loop: the repair runs inside that loop and `_ensure_indexes` runs after it. The lookup measures
**3.2 µs**, and a v22→v29 upgrade chain takes about 14–21 s instead of 652 s — a cost
production has already paid, kept here as the evidence that the index is used. [schema-migrations.md]
(schema-migrations.md), [data-model.md](data-model.md)

### A stop that arrived before the daemon's first PID-file check was ignored

**Was issue 59.** The daemon took the disappearance of `.daemon.pid` as its stop signal but only acted on an absence it had already seen a presence for, so a stop landing before the first check was discarded for the life of the process and the controller killed it mid-call. `daemon_runner` now tells the daemon a PID file is *expected*, which makes a missing file a stop from the first check, while a directly-constructed daemon keeps the old guard. Around it, every worker is signalled before any join is attempted and the joins share one budget, naming any survivor; the duplicate per-worker stop lines are gone and the idle waits wake for a stop. *Measured*: the removed-before-first-check case now exits in **1.04 s** where it previously ran past 20 s; two daemons sharing one file stop in **2.0 s** instead of 9.97 s. The controller's grace is now derived rather than guessed — 15 s for the longest main-thread block, plus the join budget, plus 5 s of margin, which is **40 s** at the 20 s budget the joins now share — and a test pins the budget coupling so the two numbers cannot drift apart. [threading.md](threading.md), [tui.md](tui.md), [cross-platform.md](cross-platform.md)

### A stale PID file made the controller kill an unrelated process

**Was issue 60.** `stop()` read a PID from the file and escalated against it, so a stale or corrupted file aimed that escalation at whatever process now held the number — and the controller reported success while the real daemon ran on. It now signals only a process it started itself, through the `Popen` handle it holds; a PID read from the file is never signalled. On timeout with nothing owned it removes the file and reports that the daemon did not exit, naming the PID it left alone, and the Windows `TerminateProcess` branch is gone with it. *Measured*: the bystander that was previously SIGTERM'd is now untouched, and the real daemon still stops through the file. [threading.md](threading.md), [tui.md](tui.md)

### Two columns were written and never read

**Was issue 30.** `scrape_version` and `app_discovery.last_historical_date_scanned` were written and read by nothing in `src/`, `window_size` was written alongside them, and the only caller of the function that wrote them was a test. Removing any of the three needed a schema migration, which is why the entry sat open as a decision rather than a fix. Migration 34→35 drops all three and removes `update_app_tracking` with them, taking the two indexes that embedded `scrape_version` (`idx_scraped_version`, `idx_fetch_status_scraped_version`) as well, so the schema no longer invites a future reader to trust a value nothing maintains. Its one real cost is that SQLite implements `DROP COLUMN` by rewriting the table — **14.9 s measured** on the 2.5 M-row copy, where every rename in the same batch was metadata-only. [data-model.md](data-model.md), [schema-migrations.md](schema-migrations.md)

### A build older than the database ran against a newer schema

**Was issue 62.** The driver only compared downward — `db_version < version` inside the `MIGRATIONS` loop — so a build whose `EXPECTED_VERSION` was below the file's recorded version ran normally: it applied no migrations and then read and wrote a schema it did not understand, which is the silent split Batch 6a measured rather than an error. `initialize_database` now reads `PRAGMA user_version` before even the journal-mode switch and raises `SchemaVersionError` when the database is newer, naming the path and both versions and telling the operator to replace the build rather than delete the file. The connection is closed before the raise, so a refused start writes nothing — *measured*: `user_version`, all 48 schema objects and the file's SHA-256 identical, with no `-wal`/`-shm` committed. The daemon runner and the web runner log the sentence and exit 2, and the TUI prints it and refuses to mount. [schema-migrations.md](schema-migrations.md), [architecture.md](architecture.md)

### The 34→35 rewrite could outlast the busy timeout

**Was issue 61, and the hazard was not reachable.** Every connection is opened with a 15 s busy timeout, and the 34→35 `DROP COLUMN` rewrote `workshop_items` — 14.9 s on the copy here and **283 s in production** — so a second entry point calling `initialize_database` during the rewrite would have waited out its timeout. The owner showed that no second entry point can: the UI blocks on `initialize_database` before it becomes available, and the daemon is started from that UI, so the two are sequential by construction. The measurement stands as the rewrite's cost, not as a reachable failure. The reverse order — a UI launched *under* a running daemon — is real and is recorded as issue 63. [schema-migrations.md](schema-migrations.md), [threading.md](threading.md)

### Relaunching the UI under a running daemon migrated the schema beneath it

**Was issue 63.** Both UI entry points called `initialize_database` at startup with nothing stopping the detached daemon, so a relaunch after an update applied pending migrations under a live writer — the 34→35 rewrite took 283 s in production — and the daemon then ran old code against the new schema, which Batch 6a measured as a silent split rather than an error. `initialize_database_with_daemon_stopped` now gates it in this order: read the version and refuse a *newer* database before touching the daemon, so a refused start does not take the service down; do nothing at all when nothing is pending, which is the common relaunch and stays free; stop the daemon when a migration is pending, logging the migration as the reason; refuse to migrate if the stop did not succeed; migrate; and restart the daemon if it had been running. A migration that raises leaves the daemon stopped and says so. Both entry points go through the one helper, so the stop, the gate, the restart and the refusals are identical on the TUI and the web. [threading.md](threading.md), [schema-migrations.md](schema-migrations.md)

### A field the model did not translate kept its place at the head

**Was issue 64.** A reply that missed some rows still counted as a success, and the missed rows kept both their entry and their priority, so a field the model omitted was re-sent from the head ahead of work that had not been attempted yet. The miss branch now lowers the row by one step — `UPDATE translation_queue SET priority = priority - 1` — inside the batch's own transaction, and logs one line carrying the key and the new priority. The row stays queued and is retried like any other: it is never removed and never given up on, because at temperature 0 the input still differs between attempts (the batch's other fields change, and the activation levels with them), so one miss is not evidence of permanent failure. `queued_at` is left alone, keeping the honest queue time and the ordering tiebreaker, and the `translation_priority` mirror is deliberately *not* demoted, since it means “still queued” rather than “how wanted”. The schema is unchanged — `EXPECTED_VERSION` stays 35 — and **the `priority` column carries no CHECK**, so a demoted value may go negative and sorts last rather than being rejected. A **whole-batch** failure is left exactly as it was: that failure is unlikely to be content-related, so its rows are neither demoted nor named in the log, which is a decision rather than an oversight — the rows are unchanged by definition and the retry already logs the failure. The one behavioural consequence is measured and pinned by a test: `urgent = any(row.get("priority", 0) >= 5 ...)` gates only the starved-batch wait, so a row demoted from 5 to 4 stops making a partial batch urgent and waits out the bounded fill window instead. *Measured*: on a migrated copy of the production-scale database a real four-row batch translated three and left the fourth, which went **priority 10 → 9** with `queued_at` unchanged and stayed queued behind the still-priority-10 head. [data-pipeline.md](data-pipeline.md), [data-model.md](data-model.md), [threading.md](threading.md)

### A lock that outlived the busy timeout ended the daemon

**Was issue 44.** Every connection is opened with a 15 s busy timeout, so a writer that holds the lock longer than that makes the next call raise `sqlite3.OperationalError: database is locked` — and in the daemon that exception had nowhere to go. The main loop called `process_batch()` bare, `WebScraperThread` had no loop-level `try` at all, and `ImageDownloadThread` wrapped only its request, leaving `get_next_image_item` and the no-URL flag write outside it. *Measured*: four occurrences in the 34 hours from 2026-09-19 04:06 to 2026-09-20 13:52 UTC, read from the outbox crash dumps. Two escaped the main loop and **killed the daemon** (at `raise_web_scrape_priority` and, earlier, the `flag_for_web_scrape` site). Two escaped the image worker and reached `threading.excepthook`, which records a dump and returns — the daemon ran on with **the image worker silently gone**, so previews stopped until a manual restart, because nothing checks worker liveness. The main loop and both workers now catch `sqlite3.OperationalError` per iteration, log one warning, and pause responsively for `pacing.DB_LOCK_RETRY_SECONDS` (5 s) before the next attempt; only a lock is tolerated, and any other exception still propagates. The row is left exactly as it was, because the aborted pass never reached its write, so there is no per-item recovery and no queue flag is cleared. The PID-file stop check sits outside the `try`, so a persistent lock cannot postpone a graceful stop. One thing is deliberately left unanswered: **what held a write lock past 15 s** is still unidentified, and none of the dumps names a holder. [threading.md](threading.md)

### The TUI printed its own markup in the translation notice

**Was issue 42.** The detail pane prepended the notice to the item's description as a markdown blockquote carrying Rich markup — `f"> *[yellow]Translation requested…[/yellow]*"` — and the widget rendering it is Textual's `Markdown`, which does not interpret Rich tags, so the reader saw `[yellow]` and `[/yellow]` as literal text while the blockquote and italics rendered. It was a parity gap as well as a rendering defect: the page already drew the same fact as its own styled element. The notice is now its own element on both sides — a `Label` (`#translation-notice`) above the description in the TUI, taking its muted italic from a stylesheet rule, and the `.translation-notice` paragraph in the page — and the wording is one constant, `TRANSLATION_REQUESTED_NOTICE` in `src/pending.py`, read directly by the TUI and injected into the template, with a test that refuses a retyped copy. The show condition is unchanged and identical on both sides: `translation_priority > 0` with no stored translation. [tui.md](tui.md), [web-ui.md](web-ui.md)

### A dead item kept its translation queue rows

**Was issue 66.** The permanent-failure path marked an item dead and cleared the four item-level queue flags, but it left the item's rows in `translation_queue` — and the translator's poll hands out **every** row of that table, with no dead-item guard, so a dead item's fields were still translated and paid for. Nothing reported it either: the detector's union was built from the item-level mirror, while the handoff table itself names the translation consumer's predicate as “any `translation_queue` row”. *Measured*: on the schema-v35 snapshot `dead_queued` read **0** while **910 dead items held 1,016 rows**, every one of them with the mirror cleared. The one runtime producer now deletes those rows in the same transaction as the status write — scoped to `entity_type = 'item'`, because a creator's row is a different entity whose numeric id may collide — the union now asks the consumer's queue, and `dead_items_by_queue`'s translation column was brought to the same test so the scalar and the breakdown cannot disagree (it read 0 where the scalar read 910). Migration **35→36** removes the rows already stranded: 1,016 on a copy of the snapshot, taking the detector from 910 to 0. The step is data-only, so the fresh-schema path is unchanged, but `EXPECTED_VERSION` is **36**. [data-pipeline.md](data-pipeline.md), [schema-migrations.md](schema-migrations.md)

### TUI tests built the app from the checkout's real database

**Was issue 67.** Four tests constructed `ScraperApp()` without patching `src.tui.load_config`, so the app took the checkout's configuration and opened `workshop.db` in the repository root — a gitignored artefact, not a fixture. Three were in `tests/test_tui_accessibility.py`, which took a `mock_config` fixture it never used; the fourth, `test_tui_show_subscription_queue`, had the same unused fixture. It surfaced when `EXPECTED_VERSION` moved 35→36: the leftover v35 `workshop.db` sent `test_main_ui_contrast` down the pending-migration path of `initialize_database_with_daemon_stopped`, which refuses rather than migrate under a daemon it believes is running, and it failed with `SystemExit: 2` for a reason unrelated to contrast — a failure the re-run hid, because the failed run had itself migrated the artefact. The three contrast tests now build from a `tmp_path` database behind a mocked `load_config` plus a mocked `initialize_database_with_daemon_stopped`, and the subscriptions test follows its own file's house pattern of mocking `load_config` alone; each asserts its app came from the fixture, so the patch cannot be dropped silently. *Measured*: the pristine files passed on a clean worktree while writing a 196 KB `workshop.db`; the fixed files leave none. The accessibility fixture keeps a real database because the app queries one as it mounts — a no-op init fails with `no such table: workshop_items`. [schema-migrations.md](schema-migrations.md), [tui.md](tui.md)

### The cursor scan could not tell when it had run out of new items

**Was issue 68.** The cursor walk had exactly two stops — `fill_target` new items found, or Steam returning an empty `next_cursor` — and once the pages it walked were already known neither was reachable, so it requested a page every `api_delay` and discovered nothing. *Measured live* on 2026-09-21 for AppID 431960 (~3.21 M items, ~32,000 pages): passes of **13,487 / 1,724 / 5,624 / 6,964 pages** each adding **0** new items, at ~2.3 pages a second, every pass ending only because the API refused, and `Cursor exhausted` never appears in 400,000 lines because the exhaustion path was unreachable. The walk now stops after **five consecutive pages that add nothing** — a page that adds anything resets the count, and the `fill_target` and API-error exits are deliberately not stalls — and records `app_discovery.cursor_walk_finished` per AppID, so it is not resumed and a restart cannot re-enable it (`_page_discovery_eligible` reads the persisted latch, not only the in-memory flag). *Reproduced* against the real copy: the old loop issued **31 requests for 0 new items**, stopping only on a refusal; five is the new stop. The accepted trade is stated in the docs: a finished walk is never re-armed, so newly published items come from the daily page-based (updated-order) scan — **up to ~24 hours' latency**, normally far less. Migration **36→37** adds the column; `EXPECTED_VERSION` is **37**. [data-pipeline.md](data-pipeline.md), [data-model.md](data-model.md), [schema-migrations.md](schema-migrations.md)
