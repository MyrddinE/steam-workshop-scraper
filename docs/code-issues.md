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

### A pass that claims ownership and then throws can still strand the running pass (issue 88)

Ownership of the overlay's module-scope state is taken once the pass commits to running, and handed back
when `/api/pause` rejects (`templates/index.html:2811`). Two windows remain where a claim is taken and
then abandoned without a handback, and in both the predecessor is stale by then, so its `finally`
(`templates/index.html:3030`) skips the handle clears and the `/api/resume`:

- **the pause rejects *and* the predecessor finishes inside that same await.** Restoring the predecessor's
  token no longer helps, because its `finally` has already run and skipped. The `.pauselock` beside the
  database then survives with no holder, and the daemon's web and image workers stay paused until
  something calls `end_pause` — the overlay's Cancel-then-Close, or the TUI's subscription screen. The
  window is one localhost round-trip, so a transport failure is needed to open it at all;
- **a throw between the claim and the pause's `try`.** The start-window DOM writes
  (`templates/index.html:2762-2792`) run there, so a page whose markup lost one of the modal elements
  would throw with the claim taken.

Left open rather than patched because every available fix trades one failure for another. Making the claim
provisional until the pause resolves lets a predecessor's `finally` resume a pause the successor then owns.
Releasing the pause inside the handback is worse than it looks: `/api/resume` removes `.pauselock`
unconditionally (`src/webserver.py:1075`), the file has three writers — the TUI's subscription screen holds
the same lock — so a pass that never successfully paused could release someone else's pause. The clean
close is to give the lock an owner rather than to keep patching the claim: tag `.pauselock` with whoever
took it, so a web pass can release only its own and a stranded claim can be reclaimed. That changes the
pause mechanism itself, not this overlay. Issues 86 and 87 are fixed and pinned regardless; this is the
residue of the same mechanism. See [web-ui.md](web-ui.md).

## Recently closed

Removed from the list above rather than marked resolved. Each is now documented as current
behaviour, or covered by a test:

### A still-draining pass could clear a newer pass's timer handles (issue 86)

Two module-scope handles (`_subPollIv`, `_subScheduleIv`) and `_subEstimate` belonged to whichever pass
last wrote them, and a pass cleared them on its way out — including the ones it never armed.
`_checkSubThrottle` and the loop's `finally` therefore reached a successor's handles: a predecessor still
draining when a second `l` started a successor stopped the successor's 1 s verification poll, dropped its
countdown, and released a `/api/resume` the successor owned.

The pass state now has one owner, and ownership is a **monotonic token**: `_subPassToken` is incremented
once the pass has committed to running (`templates/index.html:2755`), re-checked after the pause and the
pace read, inside the poll's tick and inside the throttle check, and every clear — the handles, the
estimate, the pause release — is guarded by it (`templates/index.html:3030`). A stale pass still stops
itself; it writes nothing. Ownership is taken only after `/api/queued` has returned a non-empty list, and
handed back if the pause rejects (`templates/index.html:2811`), because a pass that aborts must not
invalidate the one that is running — the regression the first cut of this fix introduced, and the third
test pins.

*Verified*: `test_a_stale_pass_leaves_a_newer_passs_poll_schedule_and_estimate_alone` fails against the
pre-change page (`poll_after_pass1: null` where the successor had armed handle 3, `estimate_survived:
false`, `resumes_after_pass1: 3`) and passes after it;
`test_an_empty_queue_pass_leaves_a_running_pass_its_ownership` fails against that first cut
(`resumes_after_pass1: 0`) and passes after it. [web-ui.md](web-ui.md)

### Cancel was read from a timer handle armed only after the pause (issue 87)

The first Cancel click ends the pass and the second closes the overlay, and the two were told apart by
`_subScheduleIv` being armed. But `_startAutoSubscribe` draws the overlay *before* awaiting `/api/pause`
and arms the schedule only after it, so a click landing inside that await saw a null handle, took the
Close branch, hid the overlay and resumed the daemon — while the pass it was pressed against armed its
schedule and drained rows with nothing on screen.

The handler now reads `_subPassLive` (`templates/index.html:3049`), true from the moment the overlay is
drawn until the newest pass ends or bails, and the pass re-checks `_subCanceled` after the pause and after
the pace read, before it arms the schedule or drains a row: a pass cancelled in either window arms
nothing, drains nothing and releases the pause exactly once.

*Verified*: `test_a_cancel_inside_the_pause_await_stops_the_pass_before_it_drains` fails against the
pre-change page (`button_after_click: "Cancel"` where `"Close"` was wanted, with two `/api/subscribe` and
two `/api/dequeue` calls, `resumes: 2`, `schedule_arms: 1`, overlay hidden) and passes after it
(`subscribe_calls: []`, `schedule_arms: 0`, `resumes: 1`, `display: "block"`);
`test_a_rejected_pause_hands_ownership_back_to_the_running_pass` fails against the cut without the
handback and passes after it. [web-ui.md](web-ui.md)

### A version-gated SQLite builtin was assumed rather than probed (issue 85)

`compute_wilson_cutoffs` adopted `percentile_disc` for speed on the strength of this container's
SQLite **3.53.1**, but the function was added in **3.51** and the production Windows host runs
**3.49.1** (Python 3.12.10, against 3.12.14 here). Production logged
`sqlite3.OperationalError: no such function: percentile_disc`, the function returned `{}`, and the
grid lost all score highlighting — retried on every request, because the cache correctly refuses to
store an empty result. The test suite could not see it: it runs on the container's newer SQLite. The
defect was the assumption, not the query.

The builtin's presence is now **probed once per process** (`SELECT percentile_disc(1, 0.5)`) and
logged with `sqlite3.sqlite_version` and the chosen path; an `OperationalError` naming the function
means absent, and `compute_wilson_cutoffs` then runs `_wilson_cutoffs_ntile`, the retained
two-window form, which returns the same ten keys and values. That form measures **13.24 s** against
**2.25 s** on the 2.5 M-row copy, paid once per filter set thanks to the 24-hour cutoff cache, so
falling back is cheaper than requiring 3.51. Pinned by `tests/test_wilson.py`: with the capability
forced absent the ten values are unchanged, the executed SQL contains `NTILE` and no
`percentile_disc`, the probe runs at most once per process, and a missing-function error is read as
absent. The equivalence test now compares the fast path against `src/database.py::_wilson_cutoffs_ntile`
rather than a test-local copy, so the two definitions cannot diverge.

**Rule: a version-dependent builtin is probed at runtime, never assumed from the environment the
tests happen to run in.** [search-filter.md](search-filter.md)

### A re-entered autosubscribe pass leaked the previous pass's verification poll (issue 83)

`_startAutoSubscribe` now clears `_subPollIv` and `_subScheduleIv` before arming the pass's own timers,
so a new pass can neither inherit a predecessor's handle nor be stopped by its completion branch. The
handle that is actually live there is the **poll**: the first Cancel deliberately leaves the cancelled
pass's 1 s poll running while the overlay stays open — it is what updates the rows — and only Close clears
it, so a new pass started from the grid cell that still holds focus (the `l` shortcut) used to arm its own
poll over that live handle. The old interval kept firing, re-reading the queue for its own stale `items`
list, and its completion branch's `clearInterval(_subPollIv)` then cleared the **new** pass's handle.
`_subScheduleIv` is live at a new pass's start only when a pass is re-entered mid-drain — Cancel and the
loop's own `finally` clear it on every pass that has ended — and it is cleared here anyway for the same
class of re-entry. Cancel's own behaviour is unchanged: its poll still runs until Close. Pinned by
`tests/test_subscribe_throttle.py`: three node-driven cases run the real `_startAutoSubscribe` and
`sub-cancel` handler against the served page — after a cancelled pass's poll is left live, a new pass
leaves exactly one live poll, the old interval no longer fires, and the stale completion no longer clears
the new pass's handle. All three fail against the pre-change page. [web-ui.md](web-ui.md#subscribe-flow)

### An AND of tag filters now drives the scan

**Was issue 84.** `Tags contains` rendered one correlated `EXISTS` over the tag junction per row, so a
conjunction of tags could only be *checked* against the rows whatever index the planner walked. With
the owner's filter set — Tags *Mature* **AND** Tags *Video* **AND** File Size > 100 MB **AND**
Subscribed is_not *previously*, sorted `wilson_subscription_score DESC LIMIT 50` — the plan was
`SCAN w USING INDEX idx_wilson_subscription_score` with a `CORRELATED SCALAR SUBQUERY` per tag: four
index probes per row until 50 matched, and near the top of the score order almost nothing matched, so
the walk approached a full scan. That is the owner's 30 s, and why it varied with cache warmth.

The filter join now collapses each all-AND conjunction of two or more `contains` tag rows into one
driving subquery, `w.workshop_id IN (SELECT wt.workshop_id FROM workshop_tags wt JOIN tags t
USING(tag_id) WHERE t.tag_name IN (?, ...) GROUP BY wt.workshop_id HAVING COUNT(DISTINCT t.tag_name) =
N)`, whose cost is proportional to the items carrying every tag rather than to where the sort order
first happens to match. Duplicate names are deduplicated first, so `A AND A` counts one name, and the
`IN`/`COUNT(DISTINCT ...)` pair keeps the case-sensitive `=` it replaces. A single tag keeps its
`EXISTS`; an OR stays a union of `EXISTS`; a `does_not_contain` stays a `NOT EXISTS`; a mix keeps the
existing parenthesised grouping. The same translation is shared by `search_items`, the metrics
coverage query and the Wilson cutoff population. See
[search-filter.md](search-filter.md) for the measurement and the rule.

*Measured* on the 2.5 M-row copy: **65.94 s cold / 1.37 s warm** before, **7.79 s cold / 0.79 s warm**
after (a second paired run measured 61.41/1.38 against 7.62/0.74); the candidate set is the 219,078 items
carrying both tags (8.67%). `ANALYZE` changes the cold/warm spread but not the plan shape. *Verified*: `tests/test_search_filter.py` compares both clause
forms across every shape the builder emits (one, two and three ANDed tags; two ORed; a mix; a
`does_not_contain` beside a `contains`; a tag matching nothing; duplicates; case-differing names; tags
separated by a size filter) and asserts the owner's shape plans a `LIST SUBQUERY` instead of the score
index. Against the pre-change source seven of the 38 fail — five clause-shape tests, the collapsed-SQL
assertion and the plan assertion, whose plan is `SCAN w USING INDEX idx_wilson_subscription_score`; the
equivalence tests pass on both trees, as they must. [search-filter.md](search-filter.md)

### The overlay's throttle flag outlived its pass (issue 81)

`_subThrottleStopped` records that the autosubscribe pass stopped because Steam was throttling, and the
Close handler reads it to leave that pass's unverified rows queued for a later drain instead of
dequeuing them. It was module scope and never reset at pass start — `_subCanceled` was reset there,
this one was not — so after any throttled pass every later Cancel skipped the dequeue and silently left
rows queued. `_startAutoSubscribe` now clears it beside `_subCanceled`, making it **per-pass**: the
throttled pass's own Close still keeps its rows queued, and a later pass's Cancel dequeues the rows that
pass never verified. The throttle stop itself is unchanged — it sets `_subCanceled`, releases the daemon
and leaves the remaining rows queued. Pinned by `tests/test_subscribe_throttle.py`: two node-driven
cases run the real `_startAutoSubscribe` and `sub-cancel` handler, one failing against the pre-change
page because a new pass after a throttled one still skipped the dequeue, the other holding the
throttled pass's own rows. [web-ui.md](web-ui.md#subscribe-flow)

### The backup schedule does not survive a daemon restart (issue 82)

`BackupThread` now records the moment of each successful snapshot in the daemon state file beside the
database (`.daemon_state.yaml`, section `backup`, key `last_snapshot_at`) — written only after the copy
verified and the manifest published it, so a failed snapshot leaves the previous record — and derives the
next due time from it on start: a record that is missing, unparseable or older than
`backup_interval_seconds` takes a snapshot after a short grace, one inside the interval waits out the
remainder. A daemon restarted more often than the interval therefore snapshots at least once per interval
instead of never, and a restart loop cannot take more than one copy per interval; the start line logs the
last snapshot's age and the next due time. The closing snapshot is unchanged. Pinned by
`tests/test_backup.py` — a record an interval old snapshots shortly after start, a recent one waits out
the remainder, a missing or corrupt one is treated as overdue, a failed snapshot leaves the record
untouched, and a simulated restart loop takes one snapshot per interval — each failing against the
pre-change code. [failure-capture.md](failure-capture.md#retention)

### The web overlay's row timer replaced its countdown with an elapsed readout

The `l` overlay's first implementation counted each row down (`138f759`, "L opens overlay with
countdown timers for each queued item"). The conversion to the server route (`5e78553`) deleted the
`openAt` tab schedule and the countdown branch, kept the internal `elapsed` variable that had only
existed to compare against `openAt`, and displayed it: a row's timer became seconds since the pass
began, and the rows still waiting lost their figure entirely. That commit's own message disclosed the
change ("The per-row timer now shows elapsed time, not a countdown to a scheduled tab") but it was
never surfaced to the owner as a decision, and the same commit wrote the elapsed-time rationale that
stood in [web-ui.md](web-ui.md) until the owner reported the count-up on 2026-09-22. The overlay now
counts down: it seeds an estimate from the shared persisted web delay through
`GET /api/subscribe_pace`, times each item's awaited `/api/subscribe/<id>` call, and draws a figure to
each row's *completion* and to the whole batch's, using the TUI estimator's formula plus the row's own
cost — [web-ui.md](web-ui.md#queued-row-timing-a-countdown-to-completion), [tui.md](tui.md)

### The view-restore loop paged through the result set unprompted (issue 78)

The loop is **gone**, not bounded. One page load used to run a per-pass `MAX_RESTORE_BATCHES = 40`
twice, appending up to 2,000 items and issuing up to 80 searches with no user action; it was then
bounded to `MAX_RESTORE_SCROLL_BATCHES = 5` pages with `_loadUntil` ending the walk as soon as
`hasMore` was false. The owner's later decision removed the feature outright: scroll and selection
are the same thing to them, neither should persist, and "changing the view or refreshing should
return to the top". `_restoreView`, `_loadUntil` and both caps are deleted, and `_saveViewState`
writes the view *definition* only — filters, sort and the overlay. `loadState` applies the stored
definition and runs one `doSearch(true)`: one search, one batch, at the top. An older entry's
`selected` and `scroll` are tolerated on the way in and never read back, and a reset render starts at
the top with its first item selected and its pane open through the read-only route.
`tests/test_webserver.py` drives the real `loadState`, a Return and the served controls' reset
handlers against a modelled grid; each fails against the pre-change code, which paged for the stored
position and re-opened the stored item.
[web-ui.md](web-ui.md#state-persistence)

The creator-jump failure the owner reported was collateral: the restore pinned `loading`, and
`doSearch` dropped the jump's reset at the guard, leaving author mode drawn over a grid it did not
own. Landed second as defence in depth (it is not the root cause), a reset now supersedes an
in-flight search instead of being dropped, and `doSearch` reports `'applied'`/`'superseded'`/`'failed'`
so a **failed** jump — and only a failed one — restores its `_preJumpView` snapshot through the same
path Return uses; a **superseded** jump leaves the newer action's view alone.
[web-ui.md](web-ui.md#search-flow), [web-ui.md](web-ui.md#jump-to-author-and-author-mode)

### One failed search wedged the web UI for the life of the page (issue 79)

`doSearch` is now its own guard and cleanup: it wraps the search body (`_doSearchBody`) in
`try`/`finally`, so `loading` is cleared on every exit path, and it rejects a response body that is not
an array instead of iterating `POST /api/search`'s `{"error": ...}` 500 body. A rejected `fetch` and
that error body each leave `loading` false, so the next search, sort, pagination request or author jump
runs; both are driven against stubbed responses in `tests/test_webserver.py`.
[web-ui.md](web-ui.md#search-flow)

### The infinite-scroll sentinel could be left armed out of view (issue 80)

The already-visible shortcut and the `IntersectionObserver` now use the grid's own scroll box. The
shortcut compares the cell's rect with `#results-grid`'s rect — below the grid's top edge and above its
bottom edge — instead of `window.innerHeight`, so a cell clipped below the grid's visible bottom no
longer reads as already visible in a tall window. The observer is constructed with
`root: document.getElementById('results-grid')`, its callback matches `entry.target` against
`_observedCell` rather than trusting `entries[0]`, and the shortcut records the batch it is loading for
so re-arming the same batch cannot schedule a second load. With `hasMore` false nothing is armed and
nothing loads. [web-ui.md](web-ui.md#infinite-scroll)

### The "Subscribed at" sort had no index

`own_first_subscribed_at` was in `VALID_SORT_COLS` — the **Subscribed at** option in both sort
dropdowns — but no query index led with it, so every page sorted by it ran `USE TEMP B-TREE FOR
ORDER BY` over the live set on the request the user waits for: *measured 2026-09-22* as
0.47/0.48/0.49 s at offsets 0/50,000/200,000 on the 2.5M-row copy, against 0.00/0.02/0.07 s once
`idx_own_first_subscribed_at` exists. It surfaced while testing the owner's "subscriber score is not
indexed" report, whose claim about *those* columns was false — both score indexes were added together
in `8771877` — and it is exactly the failure that report feared, on a different column. The expected
index set is now the `QUERY_INDEXES` table, and `tests/test_sort_index_invariant.py` proves every
sortable column has a leading index on both schema paths. The diagnostic that guards it was extended
in the same pass: because `_ensure_indexes` builds with `CREATE INDEX IF NOT EXISTS`, a live index
under the right name on the wrong column is never repaired, so `_index_report` now compares each
expected index's actual `PRAGMA index_info` columns and `unique`/`partial` flags against the expected
ones (`wrong_definition`) and each sort column's plan carries a derived `uses_index` boolean.
[search-filter.md](search-filter.md#sort-indexes-the-subscriber-score-is-slow-investigation)

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

The walk's page reads now wait the shared adaptive interval (the persisted web delay, read fresh through `src.web_worker.configured_web_delay`) before each request, the same gate the subscribe engine's item-page reads use. The subscribe POST is the button click — a browser-initiated XHR, not a page load — and is deliberately exempt, so `post_subscribe_request` and the route never wait. The fixed 5 s gate inside `scrape_extended_details` that this walk also waited on has since been removed, leaving the shared web delay as the interval's only owner — [data-pipeline.md](data-pipeline.md#web-scraping-phase), [tui.md](tui.md)

### The cached browser cookie outlived its own expiry

`steam_community_cookies()` served the first non-empty profile read for the life of the process, and nothing consulted the `exp` its own value carries — so a daemon that read a valid `steamLoginSecure` at startup kept sending it after Steam had expired it, *measured live* dead **13.7 hours** by the time a reconcile used it, against a token that lives about a day. The remembered set is now served only while that token is live: an expiry that was read and has passed forces a re-read, and the whole set is read again together so the CSRF token cannot come from a different session than the credential. An expiry that cannot be read is still served, because unknown is not expired; a live cookie still costs no file copy; and an empty read is still not cached, so a profile that gains a login is picked up on the next call — [config-security.md](config-security.md), [data-pipeline.md](data-pipeline.md). Pinned by `tests/test_firefox_cookies.py`, whose expiry case fails against the old source.

### The item-page interval had two owners

`scrape_extended_details` enforced a fixed `_WEB_DELAY = 5.0` through `_rate_limit()`, movable only by a `set_web_delay()` nothing called — so a worker scrape paid the adaptive delay *and* the fixed one, a caller outside the worker got the fixed one whether it gated itself or not, and a back-off past 5 s still paid 5 s on top. The fixed gate is gone and the shared web delay is the interval's only owner: the scraper sends as soon as it is called, and every caller that needs spacing gates itself on the persisted delay through `pacing.wait`, as the worker and the subscribe engine already do. A source-scanning test fails if any of the removed names reappear, and the scraper's "applies no delay of its own" test records a second back-to-back call being made with no sleep at all — [data-pipeline.md](data-pipeline.md#web-scraping-phase)

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

`sessionid` is a session cookie, so Firefox never writes it to `cookies.sqlite` and the profile read can never supply one; the POST fell back to the pushed `config.session.id`, a token from whenever the bridge last pushed it. *Measured live*, across four attempts: every item-page GET authenticated (`g_steamID` and the account markers present) and every POST refused `401 {"success": 2}`, the POST carrying exactly one cookie the GET did not — `sessionid`, fingerprinting the same as the configured value, while the page's own `g_sessionID` matched neither. The token now comes from the page the attempt itself reads — `g_sessionID`, then the cookie set's own token, then the pushed fallback, with the winner written back into the jar so the form field and the cookie cannot disagree — and the route reads its page through the same shared web delay gate. A refusal beside an authenticated page read is reported as `token_refused` and records **no** session problem, since that attempt proved the credential good; an anonymous read still records one. The log keeps the diagnostic property that found this with `token_fingerprint`, twelve hex characters of the token's SHA-256, printing the page and fallback fingerprints whenever they differ — [data-pipeline.md](data-pipeline.md#subscribe-engine-browser-free), [web-ui.md](web-ui.md)

### A TUI row's subscription marker did not follow a subscribe that landed behind it

The results list drew each row's marker from the item data captured when the row was built and nothing re-read it, so a subscribe landing behind an on-screen row left its green `pending` outline in place until a search or a scroll re-rendered the list. `ScraperApp` now runs the web grid's `_startListPoll` in TUI form: it re-reads the subscription columns of the rendered rows that are still `pending`, redraws them, and re-arms only while one still is — and because it reads the shared database, every writer is covered (the TUI's own pass, the web UI, the daemon's reconcile), with the pass's own result refreshing its row at once as a fast path rather than as the mechanism. The subscription queue screen's rows were a separate matter, not a staleness bug: they hardcoded the pending glyph, so no outcome could change one; they now resolve the state from `src/subscription.py`, as the detail pane and the list already did. The same screen also gained the web overlay's per-row estimate of when the pass will reach each item, at two gated reads per item — [tui.md](tui.md), [web-ui.md](web-ui.md)

### A transient lock on the database ended the TUI session

`get_connection` ran `PRAGMA journal_mode=WAL` on every connection, and that statement is not covered by the connection's busy timeout — so a moment of write contention in the daemon raised on a plain read, and because the reader was a Textual timer (`DetailsPane.refresh_data`, every two seconds) the exception ended the session instead of failing one refresh. The journal mode is a persistent property of the file and is now set once by `initialize_database`. Unattended TUI reads go through `guard_db_poll`, which catches only `sqlite3.OperationalError`, skips the tick and leaves widget state untouched, while a user-initiated action still raises to the person who asked for it. A lock that will not clear does not flood the log: the first failure of a run warns and repeats drop to debug until a read succeeds, through `RepeatFailureLog`, which the statistics screen's per-metric retries use too. The browser's list and detail polls had the same shape from the other side — one failed read stopped them for the session — and now re-arm after a failure. A test walks every timer callback in `src/tui.py` and requires each to be guarded or listed with the reason it does not read the database, so the next poll cannot forget — [architecture.md](architecture.md#the-data-layer-sqlite), [tui.md](tui.md)

### A metric registered without its TUI wiring took the stats screen down

Adding a metric meant editing `src/metrics.py` **and** `src/tui.py`, and nothing said so: `StatsScreen._compose_metric_section` indexes `METRIC_CONTENT_IDS[name]` for every name the catalogue reports (`src/tui.py:253`), so a metric registered without a content id raised `KeyError` inside `compose` and ended the whole stats screen rather than one chunk. The web panel degraded instead, its renderer falling back to the JSON it was handed. *Reproduced* while adding the two handoff counters — `KeyError: 'dead_queued'` at the old screen, and one reverted test run left eight crash dumps behind — so a metric now has to carry the wiring both front ends need, or be named in an explicit exemption list with its reason. Two guards enforce it: one walks the registry and names every missing piece, the other composes the real screen and fails if any chunk is still "Computing…", which is how a missing renderer hides from a static check — [tui.md](tui.md), [web-ui.md](web-ui.md)

### The subscription queue's estimate ran well below what a pass costs

It priced two gated page reads at the configured delay, which counts the interval a read waits but not the request, and treats the exempt POST as clock-free — *measured live* about a third low (7.7–8.6 s gaps against a 6.0 s delay, a POST round trip of 1.1–1.8 s, one item at about 18 s against an estimate of 12 s). The configured guess still seeds it, and a running mean over the items the pass has finished now nudges it for the rows still waiting: one virtual observation from the seed, then each real item, so the first moves it a lot and later ones less. That is a progress bar's arithmetic rather than a rate learned from history, deliberately — the pane is up for a minute or two, and the items in front of it are the only evidence worth pricing the rest with. The processing row keeps its distinct `subscribing…` and is where each duration is timed from. The web overlay's countdown was excluded here on the stated ground that it was a fixed tab-open schedule rather than a projection from item costs — a reason that was already false when written: `5e78553`, seven minutes earlier, had deleted that schedule and replaced the countdown with the elapsed readout. The overlay now uses this estimator's seed and its running mean, with the row's own cost added so the figure is time to *complete* the row rather than to reach it (see the closed entry above) — [web-ui.md](web-ui.md#queued-row-timing-a-countdown-to-completion) — [tui.md](tui.md)

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

### The migration chain names a column it dropped

**Was issue 8.** The historical `CREATE TABLE` keeps the `tags` name because migrations 1→2 and 5→6 read it, and a later migration drops it defensively while logging the skip. That is what a migration chain is for: a migration that speaks the shape of its own era is correct history, and rewriting it to match today's schema is how a chain stops being replayable. The owner's reading is the right one, and the clean-slate half now exists — `_create_current_schema` builds a fresh database at `EXPECTED_VERSION` and replays none of it. So there is nothing wrong here to fix, and the entry was *Informational* only while the old shape still had to be replayed. [schema-migrations.md](schema-migrations.md)

### A second daemon could start over the first

**Was issue 65.** `daemon_runner.main()` wrote `.daemon.pid` unconditionally and then called `initialize_database`, so a hand-started second daemon overwrote the live PID file and applied pending migrations under the running one — the hazard issue 63 closed for the UI path — after which the two shared one file and whichever exited first stopped the other. The PID file is now acquired **exclusively** (`O_CREAT|O_EXCL`) before the fork, before logging is reconfigured and before any migration, so a start that finds the file refuses with exit code **3** and one sentence naming the file and the PID inside it; a create that fails for any other reason aborts with its own reason. Because the exclusive create runs before the daemonize fork, the controller reads the refusal from the process it spawned and reports it instead of a success it did not have. Existence blocks the start, not liveness — the owner's rule, and the safe direction: a stale file left by a crash also blocks until it is removed, where a liveness probe would free the start over a recycled PID and let the second daemon take the file anyway, so the message tells the operator how to tell the two apart. *Verified*: a start with a pre-existing PID file exits 3, leaves the file unchanged and creates no database. [threading.md](threading.md), [cross-platform.md](cross-platform.md)

### The outbox had no way for anything to leave it

**Was issue 24.** The entry asked whether the outbox should ever prune, and the owner's answer split the tree by what each part is *for*. The three **debug** trees — `web_downloads/`, `image_downloads/` and the legacy `scrapes/` — are instruments, so they age out: `capture.prune_debug_captures` (`src/capture.py:294`) removes a capture whose mtime is older than `DEBUG_CAPTURE_RETENTION_DAYS` (7), and the daemon's housekeeping runs it at most once a day (`_maybe_prune_debug_captures`, `src/daemon.py:868`, `DEBUG_CAPTURE_PRUNE_INTERVAL_SECONDS = 86400`). A prune that fails is logged and abandoned, never raised: it is housekeeping on the fetch loop, and an unpruned capture is a disk cost rather than a reason to stop scraping. A removed file loses its `manifest.json` entry **in the same operation**, and the deletion is abandoned if the manifest could not be written first — a crash between the two then leaves a stray unmanaged file rather than an entry pointing at a missing one, which is the failure the puller could not recover from.

`failures/` and `crashes/` are deliberately **never pruned by age**: a failure is the evidence a regression test is built from, so it leaves when it has been reviewed, not when it gets old. The pull tool now fetches by **moving** — a verified transfer removes the local file and its manifest entry — so the reviewed tree drains by review while an unreviewed failure cannot be lost to a clock. `db/` is not this step's business: the snapshot is one path (`DB_SNAPSHOT_REL_PATH`) replaced atomically, so it does not accumulate, and an unmanaged file left there is reported by `_warn_about_stale_artefacts`. *Verified*: the one-time pass on 2026-09-21 removed the already-reviewed backlog — 5 crash dumps and 13 failure files, 390,033 B — and dropped 44,766 manifest entries, 44,748 of them dangling entries for debug files that had already gone (`image_downloads` 9,952, `scrapes` 1,249, `web_downloads` 33,547); the failure tree keeps only its unreviewed 63 files. What remains unbounded is deliberate and is the owner's call, not a defect: log archives accumulate until rotated away by hand, and unreviewed failures accumulate until they are pulled. [failure-capture.md](failure-capture.md#retention), [config-security.md](config-security.md)

### The pacing delays were configuration, not state

**Was issue 21.** The three rate-seeking delays were `config.yaml` keys the workers wrote back as the delays moved — `api_delay_seconds`, `web_delay_seconds` and `image_delay_seconds` — which is how an operator reset one by hand. They are not settings anyone chooses: each describes what its worker is currently doing about a rate Steam has never published, which is exactly the distinction `src/daemon_state.py` exists for. The owner's answer was to make them state.

Each delay now holds its own bare-scalar section of `.daemon_state.yaml` beside the database, named in `src/pacing.py:52-54` and read and written through `pacing.load_delay`/`pacing.save_delay` (`src/pacing.py:140,163`): `api_delay` (read at `src/daemon.py:404`, default `API_DELAY_DEFAULT = 1.5` at `src/daemon.py:117`, persisted by `_persist_api_delay` at `src/daemon.py:533`), `web_delay` (`src/web_worker.py:174`) and `image_delay` (`src/image_worker.py:61`, default 2.0). One section per worker, so `StateStore.save`'s merge means one worker's write cannot lose another's, and the translator's `translation_backoff` — which was already there — is never touched. The write rate is still bounded by `pacing.PERSIST_STEP_SECONDS`, with a refusal's back-off still forced to disk immediately.

**The cheap reset survives.** The reason the values were in `config.yaml` was that an operator needs to pull a misbehaving delay back down by hand, and deleting its section does that: the next start returns that worker to its default, with no Python file edited and no unusual restart, and the other workers are left alone. The migration was not allowed to trade one storage location for another while losing that property, and it did not.

**The config spellings are retired, not renamed.** All four — the original `request_delay_seconds` plus the three current ones — warn once per process through `warn_retired_key` with `current_key=None` (`src/config.py:101`): the value is no longer read, and the warning names **no successor** because there is no new config key to move it to. Saying "renamed to" would send the operator to a key that does not exist.

**One shared reader, one read per redraw.** `configured_web_delay` (`src/web_worker.py:53`) resolves the state file from the configured database path, so the daemon's worker, the subscribe engine, the subscriptions walk and both front ends read the same section, and the engine's throttle doubling is written into it rather than into a config dict. The TUI's queue estimate reads it once per redraw rather than once per row — `_EstimateBasis` in `src/tui.py` — because at the measured ~78 µs per read a long queue redrawn four times a second was re-parsing an unchanged file per row; the rendered numbers are unchanged, and a direct estimate call still reads fresh.

*Verified*: `tests/test_delay_state.py` (12 tests) fails 11 of 12 against the pre-change source and passes after, covering a moved delay surviving a restart, a section deleted returning the worker to its default, each retired spelling warning once while its value is ignored, and the translator's section surviving every delay write; `tests/test_tui_sub_estimate.py::test_one_redraw_reads_the_shared_delay_once` counts one state-file load across a six-row redraw and fails 5-vs-1 against the per-row code. [config-security.md](config-security.md#pacing-delays-are-state-not-config), [threading.md](threading.md), [data-pipeline.md](data-pipeline.md), [tui.md](tui.md)

### The discovery guard's diagnostic counted the wrong population

**Was issue 36.** The guard's skip line reported `count_never_fetched_items` as "items that have never been fetched but are not queued", but that reader counts every row with `api_fetched_at IS NULL` and applies no queue predicate at all, so the words described a population the query never measured. *Measured live* 2026-09-18 14:35 UTC, the 336 it printed were 133 dead (`fetch_status = -1`), 87 already queued at `api_priority = 3`, and 116 carrying `fetch_status = 404` — the API's permanent answer that the item does not exist, which the pipeline has settled, not stranded work. Over the 24 hours to that afternoon it oscillated 240–476 with no step at any restart: a floor of settled rows plus the queued remainder, which tracks discovery itself. The guard comment and `count_fetchable_items`'s docstring went further and called the two populations "disjoint", which is false — they overlap, and items move between them as `_promote_stale_items` re-ingests a settled row, discovery re-queues, or a later API revision settles or revives one.

The guard's *logic* was already right — it tests `count_fetchable_items`, the population `get_next_items_to_fetch` hands out — so only the diagnostic changed. `count_stranded_never_fetched_items` (`src/database.py:3711`) counts the never-fetched, live rows no queue holds, built from `queued_anywhere_predicate()` rather than a hand-written union so it cannot drift from the handoff metrics in `src/metrics.py`. A dead row is settled, a `404` row is the API's permanent answer (`PERMANENT_API_STATUSES`, `src/daemon.py:65`; the same method now persists `-1`), and a queued row is work a stage already carries. The line now reads "(N live never-fetched items are in no queue.)" (`src/daemon.py:1551`), the settled classes no longer inflate it, and the "disjoint" claims say what is true instead.

**The owner's persistence question is answered**: such a count is only authoritative if queue membership is real state, and it is. Every stage predicate reads the item's own columns (`src/database.py:3570-3623`) plus `translation_queue` rows; all four polls are pure `SELECT`s that write nothing (`get_next_items_to_fetch` :3657, `get_next_web_scrape_item` :4139, `get_next_image_item` :4188, `get_next_batch_for_translation` :4356); a queue is emptied only by the stage that finished or refused the work (the translator deletes the row it stored, `src/translator.py:752`; `_settle_api_failure` clears a refused item's flags and rows, `src/daemon.py:1112`); and no queue exists only as an in-memory list, which would have been a failure of the design. *Verified*: `tests/test_discovery_guard.py` builds the measured shape — 4 dead, 4 queued, 6 settled-`404` and 2 genuinely stranded rows — and pins the new reader at 2 where the old count gave 16, and the guard's log line at the stranded number; the guard-log test fails against the old line. [data-pipeline.md](data-pipeline.md#discovery-phase), [schema-migrations.md](schema-migrations.md)

### `fetch_status = 206` was retired with the inline web scrape

**Was issue 9.** The value was real once. The daemon's own web-scrape step, inline in the old `process_batch`, saved the row with `status = 206 # Partial Content` when `scrape_extended_details(url)` came back empty — the API data kept, the extended description not. *Measured* with `git log -S "= 206"`: written once, in the initial commit `f2c5d9f` at `src/daemon.py`; carried through `a7492cb` (`base_data` → `merged_data`, the same write); the column renamed `status` → `fetch_status` in `490e832`; and removed in `1de02cd` ("Stages 2+3: Separate web scraping into WebScraperThread"), which moved the scrape into `WebScraperThread`. The worker does not replace it with a partial status — a page without a description has its `web_scrape_priority` cleared and stays, and a missing item is the API's own `404` — so `206` is a fossil of the pre-worker design, and the live database holds zero such rows.

One reference survives in live logic: migration 2→3 flags `extended_description IS NULL AND status IN (200, 206)` as needing a web scrape (`src/database.py:1535`), plus a comment naming the column's vocabulary (`src/database.py:2693`). The migration body stays exactly as written, because it is correct history rather than a defect: a v2 database replayed through the chain can hold a `206` row, and dropping the value would leave that row unqueued for the scrape it was waiting on. That is the conclusion issue 8 reached about the `tags` name in a migration body. [data-model.md](data-model.md#known-gaps)

### A snapshot could start with no room to finish

**Was issue 25.** The artefact this entry opened on is gone: `<outbox>/db/workshop-backup.db.gz` — 672.9 MB, written 2026-09-12 — was removed by hand, and *measured live* on 2026-09-21 the directory holds only the current `workshop-backup.db` (3.44 GB). Nothing in the build ever wrote, read or deleted that file, and `_warn_about_stale_artefacts` (`src/backup.py:185`) stays as the thing that would notice the next leftover; it reports rather than deletes, because a backup artefact may have been kept on purpose.

The second half was the real defect. A snapshot is written as a complete second copy in the destination directory before `os.replace` publishes it, so one run needs room for roughly two copies of the database, and the old `_check_free_space` only **warned** when that room was missing — the snapshot then started and failed partway, spending the run. It is now `_require_free_space` (`src/backup.py:245`): when the size and disk-usage reads succeed and the free space is below `source size + _FREE_SPACE_HEADROOM` (16 MB), it raises `BackupError` naming the free bytes, the source size and the total needed. There is no second log line inside the guard, because `DBBackupThread.snapshot_now` already logs exactly one (`"Database backup failed (scrape loop unaffected): …"`) and returns `None`. It runs **before** the stale-temp cleanup and before any write (`src/backup.py:305`), so a refusal leaves the previous snapshot byte-identical and changes nothing in the destination directory — not even a pre-existing stale temp.

**Only positive evidence refuses.** An `OSError` from either read, or a reading that cannot be turned into a number, is skipped with a debug log and the snapshot proceeds: a wrong or unavailable reading must not block an otherwise valid backup. That is a fix in its own right — the pre-change comparison raised `TypeError` on a `free=None` reading, which is not an `OSError`, so it escaped the guard and aborted the snapshot rather than proceeding.

**The volume guarantee, stated because the owner asked.** Only *reads* cross from the source database's volume: the `VACUUM INTO` write, the verification of the copy and the `os.replace` publish all happen inside `<outbox_dir>/db/` itself, and no `shutil.copy`/`move` of the database exists in `src/` (the only copy is the Firefox cookie store). The source database and the outbox may therefore live on different drives, and no cross-volume move is relied on. [failure-capture.md](failure-capture.md#retention), [config-security.md](config-security.md)

*Verified*: 7 new tests in `tests/test_backup.py`. Against the pre-change source, four fail — three `DID NOT RAISE` (the old code warned and then succeeded; one also deleted the stale temp first) and `snapshot_now` returning metadata instead of `None` — and the unusable-reading test fails with the `TypeError`. Two more cannot fail by design and pin the behaviour that must not move: an unreadable volume proceeds, and free space of exactly the boundary proceeds.

### The log-rotation Windows path crashed both front ends at startup

**Was the 2026-09-21 outage.** `_windows_fd` opened the log through `_winapi.CreateFile` (`src/log_rotation.py:149`) and passed `None` for argument 4 (`security_attributes`) and argument 7 (`template_file`). CPython's Argument Clinic declares both through integer format units — `create_converter('LPSECURITY_ATTRIBUTES', '" F_POINTER "')` and `create_converter('HANDLE', '" F_HANDLE "')` in `Modules/_winapi.c`, with `F_POINTER`/`F_HANDLE` being `"k"`/`"K"` — so `None` raises `TypeError: CreateFile() argument 4 must be int, not None`. It fired from `RotationAwareFileHandler.__init__` → `_remember_generation` → `read_generation`, i.e. while logging was being configured, and both `src/tui.py` and `src/daemon_runner.py` build their file handler that way: the TUI died on launch and the daemon died on start, so `scraper.log` went untouched and nothing was scraped, translated or backed up after 09:08. The Windows path had **no test coverage at all**, which is how it shipped. The arguments are now `0` — the NULL pointer for each — with the clinic reason in the comment.

*Verified on the production host itself* (Windows, CPython 3.12.10, against the real 651 MB log): the old call reproduces that exact `TypeError`; the fixed call opens the log **with `FILE_SHARE_DELETE`** and reads a byte; and the fixed call against an absent `<log>.generation` raises `FileNotFoundError`, which `read_generation` already treats as "no marker". The Linux regression test fakes `_winapi`/`msvcrt` and enforces the same clinic contract, so the pre-fix source fails it with the production error string.

### A failure while logging was being configured was captured nowhere

**Was issue 69.** The crash hooks were installed *after* `logging.basicConfig` — `crash.install` at `src/tui.py:3548` against the handler built at `:3531`, at `src/daemon_runner.py:240` against `:220`, and at `src/web_runner.py:35` against `:17` — deliberately, because the forced `basicConfig` would drop the reporter's ring-buffer handler. The consequence was that anything raised during logging setup was recorded nowhere: no log record, because the handlers were the thing being built; no crash dump, because the hooks were not installed yet; no outbox entry, because the writer had no destination. *Observed live* on 2026-09-21: the `TypeError` above killed both front ends and the only record of it was the traceback the operator copied out of the terminal, with `crashes/` empty and `scraper.log` untouched.

The two jobs are now split. `crash.install_hooks(process_name)` records the process and installs `sys.excepthook` and `threading.excepthook` — none of which needs logging — and is called first thing in all three `main()`s, ahead of `load_config`, so a failure while loading the config is dumped too. `install(...)` keeps its signature and semantics and still runs after `basicConfig`, where it attaches the ring buffer and records the config; the hooks are identity-guarded, so the two calls chain once and the ring buffer is re-attached by the path that already existed for a forced `basicConfig`.

**The destination, and the adoption step.** Where a dump goes is `daemon.outbox_dir`, which is exactly what is unknown before the config loads. `_destination` already fell back beside the configured log file and then the working directory, printing the path; the last resort is now the **application folder** derived from the module's own location (`APP_DIR`, `src/crash.py:116`), because a scheduled-task daemon's working directory is not where the operator looks. And when the outbox *does* become known, `_adopt_stranded_dumps` (`src/crash.py:872`) moves whatever the fallback caught into `<outbox>/crashes/` and registers it, so it is pulled rather than stranded: it scans the log directory and the application folder (deduplicated, never the outbox itself), matches only this module's own dump filenames so an unrelated `.txt` is never touched, moves rather than copies with a collision-suffixed name, and registers **after** the move — a manifest entry pointing at a file that is not there is the failure the puller cannot recover from, while a file in the outbox without an entry is merely uncollected and says so loudly. It never raises, and a second call finds nothing.

*Verified*: 18 tests in `tests/test_log_rotation.py` and 27 in `tests/test_crash.py`. Against the pre-change source, the two new rotation tests fail with the production `TypeError` and an unguarded marker read, and six crash tests fail — five with `install_hooks` missing and one with the dump landing in the working directory instead of the application folder. [failure-capture.md](failure-capture.md#crash-dumps), [config-security.md](config-security.md)

### A Windows rotation is refused while a plain reader holds the log

**Was issue 71.** The rotation renames the live log (`os.replace(log_file, raw)`, `src/log_rotation.py:534`), and on Windows a rename of an open file succeeds only if **every** holder opened it with `FILE_SHARE_DELETE`. The two writers this feature had to coordinate now do share delete — `RotationAwareFileHandler._open` goes through `_windows_fd` with `FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE` — but a reader does not, and the persistent tail this feature was designed around is a reader. *Measured on the production host* (Windows, CPython 3.12.10): renaming a file held with all three share bits succeeds; renaming one held with `FILE_SHARE_READ|WRITE` fails with `PermissionError [WinError 32]`; and an ordinary Python `open()` fails the same way, because `open()` does not share delete either.

So the workflow the feature exists for — a persistent tail in another window plus a manual **Rotate Log** button — refuses the rotation while that tail is attached. It refuses **visibly**, which is why the owner accepted it rather than paying for a fallback: `rotate_log` returns `{"ok": False, "message": "Rotation failed: [WinError 32] …"}` (`src/log_rotation.py:549`), the log is untouched, and nothing is lost or half-rotated. The cost is stated in the docs: close the tail, press Rotate, reopen it. A copy-then-truncate fallback would keep the tail following the same path, but the archive boundary against a writer appending during the copy is a real design problem, and the owner judged the honest refusal better than a subtle one. [config-security.md](config-security.md#manual-rotation-and-why-it-is-manual)

### Two spacing tests measured the machine, not the spacing

**Was issue 70.** The engine decays the shared delay on every clean read by the healthy elapsed time — `WebInterval.after_read` calls `pacing.decay(self.delay, self._elapsed, WEB_DELAY_FLOOR)` (`src/subscribe_engine.py:615`) — so an assertion of the *exact* interval after a clean read is really an assertion about how fast the mocked work ran. `test_the_pass_spaces_every_item` asserted the second wait was `pytest.approx(8.0)`, whose default relative tolerance of 1e-6 is 8e-6 absolute: the decay exceeds that once the first item takes more than about 0.9 ms, so the test passed or failed with the load on the machine. *Measured* on 2026-09-21: 7 passed and 13 failed across a 20-run loop, the failures landing between 7.9999894 and 7.9999919, and it failed a full-suite run at random (`1 failed, 1887 passed`). The adjacent `test_every_page_read_waits_the_interval_and_the_post_does_not` has the same shape on `events[3][1] == pytest.approx(12.0)` and was latent only because its elapsed stays under the same threshold — 20 of 20 passed here, which is exactly why it would have failed on a busier machine rather than never.

Both now freeze the one clock-dependent term, `pacing.decay`, to its input, and say in a comment that the decay has its own coverage in `tests/test_pacing.py` while these tests pin the spacing: that one interval spans the pass, and that every page read waits it while the subscribe POST does not. No tolerance was widened and every assertion they made about which calls waited is kept — including the exact `waits[0] == 8.0` and the full event sequence. *Verified*: a 20-run loop of each test alone passes 20/20 where the flaky one was 7/20 before, and a mutated `after_read` that resets the delay to the default fails both tests (`assert 6.0 == 8.0 ± 8.0e-06` and `assert 6.0 == 12.0 ± 1.2e-05`), so freezing the decay did not make them blind to a broken shared interval.

### A finished cursor walk was announced every 30 seconds

**Was issue 72.** `seed_database` logged at INFO on *every* pass that an AppID's cursor walk was recorded as finished and skipped, and the discovery thread runs every `DISCOVERY_IDLE_SECONDS` (30 s), so a permanent fact was reported as news forever. *Measured live* on 2026-09-21: the line appeared **25 times in the 13 minutes** from 15:38:00 to 15:51:20 for AppID 431960 — once per pass — in a log already growing about 115 MB a day.

The skip itself is correct and untouched (issue 68's latch). What changed is the reporting: the `Daemon` keeps an in-memory set of the AppIDs whose finished walk it has already announced (`_cursor_walk_finished_reported`), so the fact is INFO once per process and DEBUG afterwards — the shape `warn_retired_key` and the stale-artefact warning already use. Because it is not persisted, a restart re-announces it once, which is what an operator wants. The single INFO line also stands on its own now: it says new items arrive from the page-based updated-order scan, which the 24-hour cooldown paces, so the one line explains why discovery has gone quiet instead of leaving it a mystery.

*Verified*: two consecutive `seed_database` passes with the latch set log `[INFO]` then `[DEBUG]`, and a freshly constructed `Daemon` logs INFO once more; the test fails against the pre-change source with `assert [20] == [10]` — the old code's two INFO records. The scope check that no other per-pass repeat exists in `seed_database` or `_run_page_discovery` was measured from a 4000-line tail of the live log, where the fetchable-guard and page-mode lines did not appear at all. [threading.md](threading.md)

### The fetch-recency panel contradicted the daemon's own threshold

**Was issue 73.** `_fetch_recency` bucketed every row by `last_fetch_attempted_at` against `params.get("staleness_days", DEFAULT_STALENESS_DAYS)` (`src/metrics.py:551`), and no caller ever passed `staleness_days`, so the window was always the hardcoded 30 days — while the daemon re-queues a stale item at the **configured** `daemon.item_staleness_days`, 60 in production (`src/daemon.py:930`). The TUI labelled the figure "Fresh (last 30d)" from the same constant (`src/tui.py:494`) and the web panel showed a bare "Fresh/Stale" with no window at all (`templates/index.html:2496`). So the panel called 840,685 rows "Stale" when the rule the pipeline follows made only 23,705 of them candidates, and it counted settled rows without saying so: 778,934 successful fetches 30–60 days old and not yet due, 61,521 dead (`-1`) and 230 legacy `404`s.

The window now has one owner. `metrics.item_staleness_days(daemon_config)` is the single reader of the key — the daemon's sweep uses it too (`src/daemon.py:405`), so the threshold cannot drift between what the panel reports and what the daemon does — and `_fetch_recency` returns the window it used as `window_days`, which is what both front ends render rather than a constant of their own. The TUI shows `Fresh (last 60d)` / `Stale (over 60d)` and the web panel names the same number, and both print the same sentence, `metrics.FETCH_RECENCY_MEANING`, injected into the page rather than retyped: the figure is the **age of our last attempt**, it is not the fetch queue, it includes settled rows that will never be re-fetched, and a stale row may simply not be due yet.

*Measured on the outbox snapshot* 2026-09-21 (the counts matched the owner's screen within a few hundred rows): of 3,118,157 rows, **zero** matched the sweep's criterion (`fetch_status = 200 AND api_priority = 0 AND api_fetched_at < now - 60d`), the oldest successful fetches were the 778,934 in the 30–60 day band, and the entire queue held 4 owner-requested items. The API was idle because nothing was due, not because discovery had missed anything. *Verified*: five new tests — a 45-day row is `fresh` at a 60-day window and `stale` at 30, the shared reader's default, the TUI's params and rendered text, the web route's params, and the node-rendered web label — each failing against the pre-change source for its own reason (`AttributeError` for the missing helper, TUI params without the window, `assert 30 == 60`, and the old bare `Fresh: 5`). [data-pipeline.md](data-pipeline.md#the-fetch-recency-figure-is-not-a-backlog), [tui.md](tui.md), [web-ui.md](web-ui.md)

### The infinite-scroll sentinel consumed a result cell

**Was issue 75.** `#results-grid` is a CSS grid (`templates/index.html:14`), and `_placeSentinel` inserted a bare `<div id="scroll-sentinel">` into it — with no CSS rule for it anywhere — so the element was itself a grid item: a blank square that pushed every later cell one column right. When the next batch landed, the sentinel was removed and a new one inserted before the new batch's first cell, so every cell jumped back a column at once — the owner's single blank entry and the list "jumping to the left one square".

The fix deletes the element rather than floating it. The batch's first cell already sat exactly where the sentinel was inserted (immediately before it) and was marked `data-batch-first` by the render loop (`templates/index.html:673`), so `doSearch` now hands that cell to `_observeNextBatch`, which releases the previously observed cell and then observes it — or runs the unchanged shortcut that asks for the next page at once when the cell is already on screen. Nothing is inserted, no cell is consumed, and nothing reflows. The reset path calls `_observeNextBatch(null)` after clearing the grid, so a detached cell cannot fire. The observer's guards, its default viewport root and its lack of `rootMargin` are all unchanged, and both `data-batch-first` and every `scroll-sentinel` reference are gone from the source and the docs.

*Verified*: four node-harness tests drive the served function against a fake DOM and a recording `IntersectionObserver` — nothing is inserted and the grid's child count equals the item count where the old code produced items + 1, the first cell is observed and the previous one released, the already-visible shortcut still schedules the next search, and the reset path releases the observation. The same driver run against the pre-change source reproduces the old result (`gridIds` began `["scroll-sentinel", "cell-0", …]`), and all four tests fail there. *Not verified*: no real browser was opened, so every geometry value is a stub and the scroll feel itself is the owner's to judge. [web-ui.md](web-ui.md#infinite-scroll)

### Dead items could be re-queued, and nothing cleared the flags again

**Was issue 74.** `dead_queued` and `dead_items_by_queue` both read 4 on the live database 2026-09-21: four `fetch_status = -1` rows holding `api_priority` 2, 3, 5 and 5, three of them last attempted 71–118 days ago and one 4 days ago. It was inert for scraping — the fetch poll's predicate excludes dead rows and the poll measured **0** fetchable rows — but it broke the handoff invariant, and it was not legacy: three writers raised `api_priority` on a row that already existed with no dead guard at all. The image worker's "the item changed" bump (`src/image_worker.py:225`) and the web worker's (`src/web_worker.py:417`) now carry the guard, and cursor and page discovery (`src/daemon.py:1633`, `:1781`) write through `insert_or_update_item`'s new opt-in `preserve_dead_api_priority`, which expresses the guard **inside** the UPSERT — `api_priority = CASE WHEN workshop_items.fetch_status = -1 THEN workshop_items.api_priority ELSE excluded.api_priority END` — rather than as a check before it, so the fetch thread marking a row dead between the two cannot slip a revive past it. Every other caller, `_settle_api_failure` and the `0` it writes to kill an item included, is unchanged.

The image worker guards only its `api_priority` term, deliberately: its `image_priority` decrement moves a dead row *out* of the image queue, so blocking it would leave the flag set — the state the rule wants gone. The web worker guards the whole statement, because `api_priority` is the only column it writes.

**The flags already stranded are cleared** by migration **37→38**, data-only: all four zeroed for `fetch_status = -1`, matching only rows that hold one so a re-run reports zero, and the dead items' own `translation_queue` rows deleted, scoped to `entity_type = 'item'` because a creator row's id can collide with an item's. Renamed columns resolve through `_current_column_name`, since a marker rewound to 37 presents the pre-rename spelling; the two table names were never renamed, so `_current_table_name` is deliberately not used. `EXPECTED_VERSION` is **38**.

The rendered sentence was wrong in the other direction and is now shared: `metrics.DEAD_QUEUED_MEANING` says the API queue still drains because its poll excludes dead rows, while the web, image and translation polls select on their flag alone and would keep spending requests on a page that no longer exists. It is injected into the page the way `FETCH_RECENCY_MEANING` is, so the two front ends cannot describe the same figure differently.

*Verified*: new `tests/test_dead_requeue_writers.py` and `tests/test_dead_requeue_migration.py`. Against the pre-change source 12 tests fail — the dead-row bumps read `api_priority` 2, 2, 3 and 5 where they must read 0, the two invariant metrics read the measured 4 where they must read 0, the migration step does not exist, and both front ends carry the old sentence. The four that pass on both trees are the over-correction pins (a live row is still bumped, the migration leaves a live row and a colliding creator queue row alone) and the version-consistency check. [data-model.md](data-model.md), [schema-migrations.md](schema-migrations.md), [data-pipeline.md](data-pipeline.md)

### The closing backup sat outside every shutdown budget

**Was issue 76.** `SHUTDOWN_BUDGET_SECONDS` (20 s) bounds only the worker joins; the closing snapshot then ran synchronously with no deadline (`src/daemon.py:1525`), and the controller's `STOP_TIMEOUT_SECONDS` — 40 s, spelled out as a 15 s main-thread block plus the 20 s join budget plus a 5 s margin — did not include it, which `docs/threading.md` admitted outright. On the 4.3 GB production database the owner measures the snapshot at over 30 s, so a slow join let the controller force-kill the daemon **inside `VACUUM INTO`**: the published snapshot survived (`os.replace` never writes the previous file in place) but the closing backup silently did not happen and its temp file was left behind for the next run.

The owner chose to keep the closing snapshot and enlarge the grace. `_CLOSING_SNAPSHOT_ALLOWANCE_SECONDS` (180 s) is now a **named term** in the derivation (`src/daemon_control.py`), taking `STOP_TIMEOUT_SECONDS` to **220 s**; the daemon's side is unchanged — the 20 s join budget still bounds the joins and the snapshot is still unbounded. *Measured* against the pulled 2.83 GB production snapshot: `VACUUM INTO` 11.4–12.8 s, `verify_snapshot` 29–104 s, the SHA-256 pass 2.9–5.0 s and `os.replace` under 0.01 s — 43–119 s end to end. `verify_snapshot` dominates, and it is almost entirely `PRAGMA quick_check` reading the whole file, sharply cache-sensitive (81 s cold against 26 s warm on the same file): what decides whether the closing snapshot fits is a full-file integrity read, not the copy and not the hash. Scaled to the live 3.44 GB that is about 145 s, and to 4.3 GB about 181 s — but the 181 s is this lab's number scaled up, not the owner's, whose production observation is over 30 s; the allowance is deliberately sized on the slower lab numbers because undersizing silently loses the closing backup while oversizing only delays the escalation of a genuinely stuck stop.

The daemon now logs `Starting the closing database snapshot; this runs outside the 20s shutdown budget and takes as long as a full copy of the database.` before it blocks, so a slow stop can be told apart from a stuck one — the diagnosability half of the earlier logging complaint. The residual risk is documented rather than removed: a database larger than the one measured, or a colder output volume, still outruns the grace, and the kill then lands inside the snapshot with the published backup intact and the closing one abandoned. Every other place that carried the old 40 s figure was updated with it — `docs/tui.md`, `docs/cross-platform.md`, the daemon's comments and the manager-screen test docstring. [threading.md](threading.md), [tui.md](tui.md), [cross-platform.md](cross-platform.md)

### Cancelling the subscribe overlay marked the leftover rows subscribed

**Was issue 77.** `api_subscribed` (`src/webserver.py:738`) is what the subscribe overlay posts for every row a **Cancel** or **Clear Failed** leaves behind, and it called `mark_own_subscribed` — which sets `own_subscribed = 1` and **stamps the sticky `own_first_subscribed_at`** as well as clearing the queue flag. A cancelled drain therefore recorded a subscription for every row it never attempted, and because the first-seen stamp is sticky and is the only source of the `previously` marker state, those rows claimed "we have seen you subscribed" permanently. It was current rather than undocumented — the route's own docstring described the behaviour — because a "done, drop it" call reached for the only dequeue-shaped route there was. Found while mapping the routes for the un-subscribe direction, which needed the cancel path to work in both directions.

Cancel and Clear Failed now post a new direction-agnostic `POST /api/dequeue/<id>` (`dequeue_subscription`) that clears `is_queued_for_subscription` and changes nothing else; `/api/subscribed` and its removal counterpart `/api/unsubscribed/<id>` remain explicit outcome stamps, written only when Steam has actually answered. Before changing it, the worker checked what depended on the old behaviour: the only callers were those two overlay handlers, and the three tests that pin the stamp call the route directly, so nothing load-bearing was rewritten. [web-ui.md](web-ui.md), [data-model.md](data-model.md)
