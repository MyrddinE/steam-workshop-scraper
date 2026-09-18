# Web UI Architecture

The Web UI is a single-page application served by Flask and styled with Pico.css. It provides search, grid-based result display, detail viewing, and Steam Workshop subscription. The JS communicates exclusively with the Flask API endpoints via fetch.

---

## Layout

A flex-based layout with three zones:

- **Header**: title and the embedded server's port. `#port-display` is filled from `location.port` on load, so it shows where the panel is actually bound — including an ephemeral or reconfigured port — without asking the server. A default port renders nothing rather than a misleading `:80`.
- **Session warning** (`#session-warning`): a strip between the header and the panes, hidden until the daemon reports that its Steam login has stopped working. It is not dismissible; see [The Session Warning](#the-session-warning).
- **Left pane** (`#results-pane`): a CSS Grid of result cards (`#results-grid`) with `repeat(auto-fill, minmax(200px, 1fr))` for responsive columns. A `#scroll-sentinel` element inside the grid drives infinite scroll.
- **Right pane** (`#right-pane`): fixed 360px width containing the search builder at top and detail pane below, separated by a left border

On viewports < 768px, the layout stacks vertically with the right pane below.

---

## Search & Filters

### Filter Builder

Uses the same field/operator/value structure as the TUI. Operator options change dynamically when the field dropdown changes (`updateOps`). Three operator categories (text, numeric, id) with `percentile` added to numeric for score-based filtering. Logic buttons (AND/OR) set `data-logic` attributes on filter rows.

### Percentile Validation

Two layers of validation: a capture-phase `blur` event listener on the document clamps values to 0-99 when the input loses focus, and `getFilters()` clamps again as a safety net before sending.

### Search Flow

1. `doSearch(reset=true)` fetches `/api/search` with the current filters, sort, and pagination state
2. Results are rendered as `.grid-cell` divs inside `#results-grid`
3. Each cell shows: preview image, or "pending"/"no image" when nothing has been recorded, or — when the server has already answered for that preview — the answer itself drawn in red at half the cell's height (a `404`, a `410`, or the content type it served instead). Plus title (2-line clamp), file size (color-coded via `sizeClass`), and Wilson subscriber/favorite scores (color-coded via `wClass`)
4. A pending marker in the corner names the stage the item is waiting on, by speed, colour and — on hover — a `title`: image rotates at 1x and is vivid green, translation 4x slower and mid green, the web scrape 16x slower and grey. `_pendingStage` picks the **fastest** stage that is pending, so a marker about to clear is never hidden behind a slower one, and the slow grey marker — which may stay for hours — is the quietest thing on the cell. `_applyPending` sets one `pending-<stage>` class, clearing the others, and writes the stage's wording onto the marker's `title`. The durations, colours and wording are mirrored from `src/pending.py`, and `tests/test_pending.py` fails if any of them disagree — the wording is kept in the shared table rather than the template, because the TUI draws the same state and a page-local description could drift from it. `api_priority` is deliberately not a stage: a list only shows items the API has already returned, so a pending refresh is not content anyone is waiting on.
4. `_placeSentinel()` handles infinite scroll by checking whether the first item of the batch is visible and placing or removing the scroll sentinel accordingly

### State Persistence

The TUI saves filter/sort state to `.tui_state.yaml`, which the web UI reads through `GET /api/state`. That file is the TUI's: it has the TUI's shape (`scroll_y`, `selected_workshop_id`) and is rewritten on the TUI's schedule, so writing the browser's view back into it would have the two front ends overwriting fields the other does not understand. The browser therefore keeps its own view in `localStorage` under `view.state.v1` — filter rows, `sort_by`, `sort_order`, the open item and the grid's scroll position.

The entry is versioned and shape-checked like the statistics panel's ordering entry (`_loadViewState`): a wrong `v`, a non-list `filters`, or an unreadable value reads back as "no state" rather than reaching the builder. Fields the current schema no longer has are dropped, and a stored value is coerced to the string the text input holds.

**Precedence is one-sided.** A browser that has been to the page before has its own record of what the user was doing, so local state wins outright and `/api/state` is not even fetched. Only when there is no usable entry — a first visit, cleared storage, or a rejected shape — does the page seed from the TUI's saved state.

**Restoring a deep view.** After the first search, `_restoreView` keeps calling `doSearch(false)` — the function that owns `currentOffset` and the sentinel — until the grid is tall enough for the saved scroll position and the selected item is present, re-opens that item, then applies the scroll last: `showDetail` focuses the cell and focus can move the grid, so the saved position has to be the final word. Paging is capped at `MAX_RESTORE_BATCHES` so a selection that no longer matches the filters cannot walk the whole result set, and a reset `doSearch` clears the selection because a new result set may not contain it. Writes are suppressed while a restore runs (`_restoringView`), so the page cannot overwrite the state it is reading. Saves happen on a throttled `#results-grid` `scroll` listener, at the end of a reset `doSearch`, when a detail pane opens (`showDetail`), and on `pagehide`.

### Wilson Cutoffs

`loadCutoffs()` fetches percentile thresholds from `/api/cutoffs` (which calls `compute_wilson_cutoffs`). These are used by `wClass` for color-coded score display.

---

## Infinite Scroll

### `_placeSentinel`

After each `doSearch` batch, marks the first item with `data-batch-first` and runs `_placeSentinel`:
- Gets the bounding rect of the first batch item
- If it's already within the viewport → triggers `doSearch(false)` immediately (no sentinel placed)
- If it's below the viewport → inserts `<div id="scroll-sentinel">` before it

### `IntersectionObserver`

A single observer watches `#scroll-sentinel`. When the sentinel enters the viewport, it fires `doSearch(false)`. No `rootMargin` — the sentinel sits before the first unseen item, so the observer fires exactly when that item scrolls into view.

The observer is created once at page load. `_placeSentinel` calls `_scrollObserver.observe(sentinel)` each time a new sentinel is placed (since the sentinel is dynamically created and destroyed). The observer guards `currentOffset > 0` to prevent firing before the initial search.

When `doSearch(reset=true)` clears the grid (`innerHTML = ''`), the old sentinel is destroyed with the grid content. `_placeSentinel` creates a new one after the fresh batch renders.

---

## Image Polling

### `_startListPoll`

An adaptive-timeout poll that updates grid cells as images and translations arrive:
- Collects workshop_ids from DOM elements with `.grid-img-placeholder`
- POSTs to `/api/items` (bulk ID lookup)
- For each returned item: updates title text, then replaces the placeholder with `_imageCellHtml(item)` — the `<img>` once a real extension arrives, or the red failure cell once the server reports a final answer — and appends `.` to pending text for visual progress
- Delay: `max(1, log2(pending_count))` seconds → speeds up as images arrive
- Stops when no pending placeholders remain in the DOM

### Dot Animation

Each poll cycle appends a `.` to the placeholder text via `ph.textContent += '.'`. This gives visual feedback that the poll is iterating over the cell. When an image arrives, the entire placeholder div is replaced by an `<img>`, so dots naturally clear. The failure cell is exempt: it holds a `<span>` with the status, not pending text, so `_stopListPoll` skips `.grid-img-failed` rather than overwriting an answer with "no image".

### `_startDetailPoll`

A fixed 3-second poll on the currently-selected detail item. Checks `translation_priority > 0` to detect when translation completes, then re-renders the detail pane. Stops when `translation_priority` is 0.

---

## Detail Pane

### `renderDetail`

Builds the detail view HTML inline. Shows: title (linked to Steam), creator (a jump-to-author button), workshop ID, Wilson scores (color-coded), created date, file size (color-coded), updated date (if different from created), views (via `fmtCount`), subscriptions/favorites (current/lifetime via `fmtCount`), tags (comma-separated from junction table or legacy JSON), Queue/Unqueue and Subscribe buttons, and description text (BBCode-to-HTML converted server-side).

Stats are in a single-column vertical layout (`.stat-row`), not the previous two-column grid.

### Jump to author

The creator in the heading is a button, the web equivalent of the TUI's `btn-jump-author`. It calls `jumpToAuthor(creatorId)`, which sets an ordinary `Author ID` / `is` filter row through `addRow` and re-runs the search — the same field, operator and value the TUI's jump builds. If the builder already holds an Author ID row, that row is switched to `is` instead of a second, contradictory one being added.

There is deliberately **no single-creator mode and no Return button**. The TUI needs them because its jump replaces a fixed set of rows and cannot undo that; the web builder is always visible and its rows are individually removable, so deleting the author row and searching again restores the previous view. That also means no in-memory filter snapshot has to survive a detail re-render.

`creatorId` comes from the payload's `creator_id`, which is a string. A SteamID64 is seventeen digits — beyond the range a JavaScript number represents exactly — so a numeric field would round in `JSON.parse` and the filter would name a different account (`src/webserver.py`, `_detail_payload`). An item with no creator renders the name plainly and offers no jump.

`/api/authors` (the full author list) is still not consumed: a single jump needs only the one ID already in the payload, and an author picker was out of scope, so no request was added for it.

### The subscription marker (queue / unqueue)

`renderDetail` draws the owner's subscription marker immediately before the title, and the grid
cell draws the same marker at its top-right. It has four states, resolved by
`subscription.subscription_state(item)` and rendered from the one table in `src/subscription.py`
(which the TUI reads too — see [tui.md](tui.md)):

| State | Glyph | Colour | Meaning | Click |
|---|---|---|---|---|
| `subscribed` | ★ | solid yellow | the owner is subscribed now | nothing |
| `pending` | ☆ | green | queued to subscribe | un-queues |
| `previously` | ☆ | yellow | we have seen the owner subscribed, and they are not now | queues |
| `never` | ○ | gray | never seen subscribed | queues |

There is exactly one such indicator: the old `queued` CSS class and its `★` prefix on `.grid-title`
are gone, and the pane's `Queue` / `Unqueue` button pair is replaced by the marker itself. The
marker's glyph, colour, CSS class, label, tooltip and clickability all arrive on the payload,
computed by the server from the shared table, so the page holds no copy of the state vocabulary.

Clicking the marker calls `toggleDetailQueue(wid)`, except for `subscribed`, which sends nothing —
the only action available there would be an unsubscribe, and an accidental unsubscribe is not
wanted. `toggleDetailQueue` POSTs the existing `/api/toggle_sub/<id>` route, which flips the
database flag and answers only `{ok: true}`. Since the route does not report which way the flag
moved, the client reads the item back through the read-only `/api/item/<id>` route and re-renders
the pane and the matching cell's marker (`_applySub`) from that payload — the same path the `s`
shortcut takes. A read-back rather than a locally flipped guess is deliberate: the `s` shortcut and
the subscribe drain's `/api/subscribed` calls change the same flag behind the pane's back, so a
guess could show the wrong state. Rendering from the item payload is also what lets the 3-second
translation poll re-render the pane without reverting the toggle. A failed request alerts and leaves
the pane alone.

The marker sits inside the cell that opens the detail pane, so its click handler stops propagation:
without that, toggling the queue would also drag the pane to the item.

`previously` can only ever mean "we have **seen** this account subscribed". Steam exposes no
per-account subscription history — `lifetime_subscriptions` is an item-wide count and
`EnumerateUserSubscribedFiles` is publisher-key-only — so on the day the marker shipped there were
zero `previously` markers regardless of real history, and they fill in over time. The marker's
tooltip says this rather than implying a complete record.

### Translated and original text

The pane can show either language, mirroring the TUI's `ctrl+w` toggle. `_showTranslated` is the
equivalent of the TUI's `show_translated` reactive: it defaults to showing the translation, persists
for the session rather than per item, and switching it re-renders from the cached payload
(`_currentDetail`) without another request.

The toggle appears only when the server reports `has_translation`, which is `translate_version` being
set — the same test the TUI uses. An item can hold translated text that happens to match the
original, so the presence of the field is not a reliable signal. When an item has no translation the
two variants are identical rather than one being empty, so the pane has something to render either
way. A pending translation is noted above the description while `translation_priority > 0` and no
translation has been stored yet, matching the TUI's notice.

---

## Maintenance Actions

The row under the detail pane (`#detail-buttons`) holds the queue and database actions: **Fetch New**, **Update Visible**, and **Clear Pending**.

**Clear Pending** (`#btn-clear-pending`, `doClearPending`) mirrors the TUI's command-palette action. `confirm()` names the exact set before anything is sent — items with no status or a 404 status whose API data was never fetched — because the delete is destructive and irreversible; declining sends no request at all. On a 2xx it reports the count returned by the route and re-runs the search, on a rejected response it shows the status, and on a dead backend it shows the error, so a failed clear is never presented as a successful one.

---

## Daemon Panel

The header toolbar's **Daemon** button (`#btn-daemon`) opens `#daemon-overlay`, a modal panel with the running status and PID, Start / Stop / Restart buttons, and a live log view (`#daemon-log`).

While the panel is open, `_refreshDaemonStatus` polls `/api/daemon` and `_pollDaemonLog` polls `/api/daemon/log?since=<byte-offset>` every 2 seconds. The offset is the byte position returned by the previous response, so each poll transfers only new lines. The view is a bounded preview: the server reads at most 64 KiB and returns at most 500 lines, so a first poll against a large log shows its tail rather than the whole file. A `reset: true` response means the returned lines do not continue the caller's view — a first call that had to seek to the tail, a rotation or truncation, or the client having fallen more than one window behind — and `_pollDaemonLog` clears the pane before showing them, so a gap is never rendered as if it were continuous. `_closeDaemonPanel` clears the interval, so nothing polls while the panel is hidden.

---

## The Session Warning

The daemon signs its Workshop requests with a `steamLoginSecure` cookie, and that cookie expires: *measured live*, the token Steam issues carries `exp - iat` of 24.1 hours, so it dies about once a day while a daemon runs for weeks. When it dies, nothing else on the page looks wrong — the subscriptions simply stop updating, and before this banner existed the only sign was an absence of stars. The failure is therefore reported where it happens and shown where it is read.

**The fact is recorded by whoever finds it.** `src/session_health.py` owns one section (`session`) of `.daemon_state.yaml` beside the database — the same transient, best-effort store as the pacing backoff, for the same reasons (`src/daemon_state.py`). Two paths write it:

* the **subscription reconcile** (`src/subscription_sync.py`), which refuses a cookie whose token has already expired and recognises Steam's sign-in page if one arrives anyway; and
* the **web worker** (`src/web_worker.py`), which records the same reason when a scrape comes back signed out and a re-read of the browser's cookie store produced nothing newer.

Either path clears it the moment an authenticated page is seen, so the warning disappears on its own once the login works again.

**`#session-warning` is a strip between the header and the panes**, holding a sentence and two controls. The sentence comes from the server verbatim — `the saved login cookie expired 14h ago (at 2026-09-17 00:32)` — because the reason is known where the failure is discovered and the client should not re-derive it. **Sign in to Steam** is an ordinary link to the page the scraper itself uses (`/my/`, which redirects to the login form when signed out and to the profile when signed in), so signing in there mints the cookie the daemon reads; it opens in a new tab so the panel is not lost. **Recheck** posts `/api/session/recheck` and then re-reads `/api/session` rather than trusting the POST's own body, so the banner has exactly one source of truth.

The strip has no close button on purpose: the condition is a silent data outage, and the only thing that clears it is a working login. It is polled every 30 seconds (`_refreshSessionWarning`) because the fact is written by a worker thread in another process — a push would need a channel that does not exist — and half a minute is far more often than the condition can change. The poll is started on load and is never released, unlike the daemon panel's, because the warning has to be visible without opening anything.

**Fixing the login brings the markers back, not just the banner.** The reconcile normally walks once a day, and that walk has already happened for the day by the time an operator responds to the warning — so waiting out the interval again would leave every `own_subscribed` flag wrong for another day *after* it was fixed. While a session problem is recorded, the daemon walks on `SUBSCRIPTION_RECONCILE_RETRY_SECONDS` (15 minutes) instead of the daily interval, returning to the daily cadence as soon as a walk authenticates. The retries cost no requests while the token is still expired: the expiry check is local and the browser re-read is a file copy. So the sequence is: sign in, Recheck, and the stars are back within a quarter of an hour.

---

## Statistics Panel

The 📊 button (`#btn-stats`, `templates/index.html:99`) opens `#stats-overlay` (`templates/index.html:136`), a modal panel modelled on the daemon overlay. It keeps the button's id and position; clicking it no longer navigates to the raw `/api/stats` JSON.

`_openStatsPanel` (`templates/index.html:1316`) fetches `/api/metrics` for the catalogue — each metric's name, note and `seed_ms` hint — then builds one `<section class="stats-chunk" data-metric="...">` per metric, each with its own body element, and requests **every metric independently** through `GET /api/metrics/<name>` (`_loadMetric`, `templates/index.html:1286`). It deliberately does not `Promise.all` the requests: each section is filled and its own refresh timer armed the moment that metric lands, so a fast chunk draws while a slow one is still running. Every metric shows a human label, its note, and a value rendered to suit it, with its measured `ms` shown quietly in the heading:

* **coverage** — a progress bar per stage (API data, description, image, translation, creator) with the count and the live-item total.
* **totals** — alive and dead counts with the overall total.
* **stuck_work** — flagged in red when non-zero: the number of dead items still sitting in a queue, broken down per queue. A zero value renders as an all-clear.
* **priority_breakdowns** — one "queue: N waiting" block per queue with the priority mix.
* **translation_status**, **status_counts**, **fetch_recency** — labelled count lists.
* **tag_counts** — a two-column table (Tag, Count) over every tag, sorted by count descending and scrolling inside a bounded box so a long list cannot push the chunks below it off the panel. Counts print in full (`fmtExact`) rather than through `fmtCount`: a tag count is read and compared, not merely scanned, and `fmtCount` reports 5,000 as "5.00K".
* **high_water**, **app_tracking** — a formatted timestamp and a per-AppID table.

**Ordering is learned.** The request order is seeded from each metric's `seed_ms` on the first ever open; on every later open it is sorted by the durations measured on the previous open, persisted in `localStorage` under `stats.metric-order.v1`. The stored shape is deliberately tiny and versioned — `{v: 1, ms: {metric: milliseconds}}` — so a stale or corrupt entry from an older build is ignored rather than breaking the panel (`_loadStatsOrder`, `templates/index.html:1247`; `_statsOrder`). The DOM order is fixed when the panel opens; this open's measurements feed the next open.

**Refresh is per metric.** Each chunk re-requests itself after `max(2 s, 50 × its own measured duration)` (`_intervalFor`, `templates/index.html:1243`), armed with `setTimeout` only once the previous response has landed, so a metric that is still computing is never started twice and a slow metric cannot hold up a fast one. `_closeStatsPanel` (`templates/index.html:1350`) clears every per-metric timer and bumps `_statsToken`, so responses still in flight are discarded rather than written into the closed panel.

---

## View Window Analysis Panel

The header toolbar's **Analysis** button (`#btn-analysis`, `templates/index.html:64`) opens `#analysis-overlay` (`templates/index.html:144`), the web half of the TUI's `ctrl+?` screen. It mirrors that screen's content: a bucket-size box in days, a **Recalculate** button, a per-bucket table, and a summary line.

`_openAnalysisPanel` resets the box to the TUI's default of 7 days and calls `_runAnalysis`, which requests `GET /api/analysis?bucket_days=N` — one request per open or recalculate. There is deliberately no polling: the query is expensive and the shape it measures moves slowly. The bucket box is read the way the TUI reads its input (`_analysisBucketDays`): anything that is not an integer falls back to 7, and a real number is clamped to at least one day.

`_renderAnalysisTable` draws one row per bucket with its age range, item count and median/p10/p90 views, all through `fmtCount`. The median column is paired with a bar sized to `median / peak median`, so buckets whose absolute medians differ by orders of magnitude can still be compared at a glance — the point of the screen. `_renderAnalysisSummary` states the knee plainly ("Estimated view window: ~N days") alongside the analysed item count and bucket count; when `estimated_window_days` is `null` it says there is insufficient data rather than printing a misleading `0`.

**In-flight responses are discarded.** `_loadAnalysis` carries the token it was started with, and `_analysisToken` is bumped on open, on every recalculate, and on close (`_closeAnalysisPanel`). A response armed under an older token is dropped, so closing or recalculating mid-request can never repaint a panel the user has left — the same guard as `_statsToken` in the statistics panel.

---

## Subscribe Feature

### Userscript Bridge (`userscripts/steam_subscribe.user.js`)

A Tampermonkey/Greasemonkey userscript that bridges the Steam session to the web UI:
- On `steamcommunity.com`: captures `sessionid` and `steamLoginSecure` via `GM_setValue`, shows a toast notification on change
- On the scraper web UI: stamps `document.body.dataset.userscript = '1'` and `userscriptVer` for detection, and pushes both cookies to `/api/sessionid` — on load, then re-checked every 30 seconds but sent only when a value has actually changed, with a slow re-push as the safety net for a backend that restarted and lost its in-memory session
- Retries a failed push with a bounded backoff rather than a fixed five-second loop: five seconds, doubling to a one-minute cap, at most six retries, then it gives up. A success resets the backoff, and the thirty-second interval remains the slow probe for a backend that comes back later
- Version checking: reads `<meta name="userscript-version">` from the page and compares with `GM_info.script.version` — refuses to operate if outdated

**Reading the login cookie needs `GM_cookie`, and HttpOnly.** Steam marks `steamLoginSecure`
HttpOnly, so `document.cookie` can never contain it — which is why an earlier version reported
`login_secure: missing` on every push regardless of how the session was configured. `GM_cookie.list`
does return HttpOnly cookies, but the vendor's documentation states that support is
**BETA builds of Tampermonkey only**; on a stable build the script silently falls back to
`document.cookie` and captures `sessionid` alone. The two cookies differ in more than visibility:
`sessionid` is a CSRF token, while `steamLoginSecure` is the thing that authenticates the session.

The version literal appears in both this file and `templates/index.html`; nothing links them, so
`tests/test_userscript_contract.py` asserts they agree. Installed copies update from the script's
`@updateURL`, so bumping the literal is also how an installed bridge receives a behaviour fix.

**Retrying a down backend.** A failed session push is retried, but not forever: six retries
scheduled five, ten, twenty, forty, sixty and sixty seconds later (doubling, capped at one minute and
capped again by the attempt limit), after which the fast chain stops. Only one chain runs at a time,
so a thirty-second interval tick cannot start a parallel stream. The interval itself keeps its
original job — a slow probe that re-syncs a backend that restarted — and a successful push resets the
schedule, so a transient failure later retries promptly. The change-detection is untouched: an
unchanged value still costs no request until the stale re-push window elapses.

**Throttling.** Steam answers an over-budget request with **HTTP 200** and its ordinary page shell
carrying "too many requests", so the subscribe button is simply absent. The plugin checks for that
wording before concluding anything, and reports it to `/api/subscribe_throttled/<id>` rather than
`/api/subscribe_failed/<id>`. The difference matters: a throttled item is **left queued** so the next
drain retries it, whereas a genuine failure is cleared. The UI polls `/api/sub_health` once per tab it
opens and stops opening more while Steam is refusing us, telling the user when the rest can be retried.
The budget is per account or address and refills over minutes.

### Detection (`_userscriptPresent`)

Checks `document.body.dataset.userscript` for presence and `userscriptVer` against the page's expected version from the meta tag. If outdated, offers to open the install URL.

### Install URL

The "Subscribe" button's install link points to `/userscript/steam_subscribe.user.js` — a dynamic endpoint that injects `@include` lines for the server's host IP and port, so the script works on LAN IPs as well as localhost.

### Subscribe Flow

1. User clicks Subscribe on the web UI → `doSubscribe` checks `_userscriptPresent()`
2. If userscript absent: shows install instructions
3. If present: POSTs to `/api/subscribe/<workshop_id>`
4. Server reads the sessionid (from the userscript push or config `session.id`) and `steamLoginSecure` (from config `session.login_secure`, which can be a YAML list joined with `%7C%7C`)
5. Server POSTs to `steamcommunity.com/sharedfiles/subscribe` with browser-like headers (User-Agent, Origin, Referer with workshop URL) and cookies
6. Steam response codes are mapped to user-facing messages

---

## Server Endpoints

### `/api/search` — POST

Main search endpoint. Accepts `{filters, sort_by, sort_order, offset, limit}`. Server-side bumps web/image/translation priorities and re-queries priority fields to include updated values. Returns 50 items with summary fields.

### `/api/item/<id>` — GET

Read-only detail fetch. Returns full item data with the BBCode-to-HTML converted description. It applies no priority bumps by design: the detail pane polls this every three seconds while it is open, and applying detail priority here re-armed the fetch queue on every poll, so the daemon re-fetched whatever was on screen indefinitely.

Both language variants are returned — `description_html` beside `description_html_original`, and `display_title` beside `display_title_original` — so the client's toggle costs no request. The payload also carries `has_translation` (whether `translate_version` is set) so the client does not have to infer it from the text, and `creator_id` (the creator's SteamID64 as a string, so a seventeen-digit ID survives `JSON.parse` and can be fed back into an Author ID filter).

### `/api/item/<id>/open` — POST

The same detail payload, but applies detail-level priority (web, image, translation and API) first. This is the path the UI takes when a pane opens, and the only one that re-queues the item.

### `/api/items` — POST

Bulk ID lookup. Accepts `{ids: [1, 2, 3]}`. Returns the same summary fields as `/api/search` for efficiency. Used by image polling.

### `/api/cutoffs` — POST

Wilson score percentile thresholds. Accepts `{filters}` (excluding percentile filters). Returns `{wilson_favorite_p99, wilson_favorite_p90, ...}`.

### `/api/state` — GET

Reads `.tui_state.yaml` for filter/sort state restoration. The client uses this as the **first-visit seed only**: once the browser has its own `view.state.v1` entry, this route is not called at all.

### `/api/clear_pending` — POST

Deletes every pending item — those with no status or a 404 status and no successful API fetch (`clear_pending_items`, `src/database.py:2354`) — and returns `{ok, deleted}` with the number of rows removed. This is the same predicate and the same delete as the TUI's `action_clear_pending`; there is deliberately no dry-run mode. The UI asks for confirmation first, naming what will be deleted.

### `/api/save_filter` — POST

Saves the current enrichment filters to `app_tracking` for the configured AppID. When no target AppID is configured it answers **400** with `{"error": "No target AppID configured"}`; the client shows that message and only reports success on a 2xx, so a rejected save is never presented as a stored one.

### `/api/subscribe/<id>` — POST

Performs the subscribe against Steam directly, with no browser tab, and returns Steam's JSON body unchanged — the TUI reads `success` and `message` from it. The request shares the scraper's session and presents the project's own Firefox User-Agent, and both its cookie jar and the form's `sessionid` come from one `web_scraper._build_workshop_cookies` read: the signed-in Firefox profile's whole `steamcommunity.com` set when `session.read_firefox_cookies` is on, otherwise the configured `sessionid`/`login_secure` pair. The pushed `_sessionid` global and `session.id` are a fallback only when that set carries no `sessionid`, so the userscript-driven flow is unchanged.

It refuses before spending a request when the set has no CSRF token or no `steamLoginSecure` (**400**, with a message naming the remedy: sign in to Steam in the browser the daemon reads cookies from, or configure `session.login_secure`), and when `session_health.evaluate_login` says the credential's own token has expired (**400**, the reason recorded through `session_health.record_rejected` so the [session warning](#the-session-warning) shows it). A Steam `success` of `2` or `15` — the answers the TUI reads as session expired / permission denied — is recorded as a session problem the same way, while the response body is passed through untouched. A `success` of `1` records the confirmation with `mark_own_subscribed`, setting `own_subscribed` and clearing `is_queued_for_subscription` exactly as `/api/subscribed/<id>` does, and clears any recorded session problem. A missing item still answers **404** `Item not found.`, an item with no AppID still **400** `Item has no AppID.`, and a transport failure still **502**. Each of those refusals logs the `workshop_id` and the reason.

### `/api/toggle_sub/<id>` — POST

Flips `is_queued_for_subscription` for one item and answers `{ok: true}`. It is the route behind both the `s` shortcut on a grid cell and the detail pane's Queue/Unqueue button. It returns no new state, so the detail pane reads the item back through the read-only `/api/item/<id>` route to label its button.

### `/api/sessionid` — POST

Accepts sessionid from the userscript. Stores it in the `_sessionid` global (for server-side subscribe); if the payload also carries a `login_secure` value that differs from the configured one, that is written to `_config["session"]["login_secure"]` (for the Steam cookie) and persisted so the daemon picks it up. The TUI subscribe action also calls through the server endpoint.

A push whose `login_secure` matches what is already configured writes nothing. The bridge re-pushes on a timer, so without that guard an open Steam tab rewrote `config.yaml` — a YAML serialisation and a file write — every thirty seconds with a value that had not moved. The CSRF token is still taken from every push, because it lives only in memory. A push that *does* carry a changed cookie is also the best local evidence that the login works again — it comes from the operator's own signed-in browser — so a value that is not already expired clears the [session warning](#the-session-warning) without waiting for a scrape to confirm it.

### `/api/session` — GET

Whether the daemon's Steam login is still working, for the session warning banner: `{problem, detail, detected_at, login_url}`. `detail` is the sentence the daemon recorded and `login_url` is where an operator signs in again, so neither is hard-coded in the template. `problem` is false when nothing has been recorded — including when the recorded section is present but carries no sentence, since an unexplained warning is worse than none. One small YAML file read, because the banner polls it.

### `/api/session/recheck` — POST

Re-reads `steamLoginSecure` from the browser's cookie store after the operator signs in, using the same lookup the daemon prefers. A cookie that is not already expired from its own token is saved to `config.yaml` — which the daemon re-reads per request and per batch, so nothing needs restarting — and the warning is cleared. The judgement is local: proving the cookie by spending a request would duplicate what the next scrape is about to do anyway, and the daemon's answer corrects the banner if Steam still refuses. When the browser has nothing newer, the route answers `{ok: false, problem: true, detail}` with the reason (which may be the configured cookie's expiry, or that there is no cookie at all), and a cookie found but not writable answers **500** with the failure named, so a save that did not happen is never reported as a cleared warning.

### `/api/stats`, `/api/tags`, `/api/authors`

Read-only endpoints returning database statistics. `/api/stats` still returns the old flat payload; the statistics panel uses the per-metric endpoints below instead. `/api/authors` still has no client.

### `/api/analysis` — GET

Age-bucketed view statistics, consumed by the view window analysis panel (`api_analysis`, `src/webserver.py:394`). Accepts `bucket_days=N` (default 7) and returns `{buckets: [{age_start, age_end, count, median, p10, p90}], estimated_window_days, items_analyzed}`. The bucket width is floored at one day, because the parameter comes straight from the query string and a zero width would divide by zero (the TUI clamps its input the same way). `estimated_window_days` is the knee — the first bucket whose median drops below a quarter of the early-bucket peak — and is `null` when there is too little data to find one, a distinction the panel renders honestly rather than collapsing to zero.

### `/api/metrics` — GET

The metric catalogue: `{"metrics": [{name, note, seed_ms}], "default_order": [names]}`, where `default_order` and `metrics` are both in seed order (cheapest seed hint first). The panel draws its layout from this rather than hard-coding metric names, so a metric added on the server appears without a client change (`src/webserver.py:359`). `seed_ms` is only a first-open ordering hint; the client replaces it with its own measurements.

### `/api/metrics/<name>` — GET

One metric, computed on its own: `{name, value, ms, note, seed_ms}`, where `ms` is what that metric actually cost. An unknown name returns a 404 with `{error}` (`src/webserver.py:376`). Separate requests are what make the chunks independent: whichever finishes first renders first, and a slow metric cannot hold up a fast one.

### `/api/daemon` — GET

Daemon status: `{running, pid, log_file}`, where `log_file` is the configured `logging.file` path or null.

### `/api/daemon/start`, `/api/daemon/stop`, `/api/daemon/restart` — POST

Drive the background daemon through the shared `DaemonController`. Each returns `{ok, changed, message}`; starting a running daemon or stopping a stopped one is an idempotent no-op that still reports success.

### `/api/daemon/log` — GET

Incremental log tail and bounded preview. Accepts `since=<byte-offset>` and returns `{lines, offset, reset}`. At most 64 KiB is read (`DaemonController.TAIL_BYTES`, `src/daemon_control.py:23`) and at most 500 lines are returned (`TAIL_LINES`, `src/daemon_control.py:24`), so a first call (`since <= 0`) returns the tail of the file rather than the whole of it. `offset` is the byte position to pass as `since` on the next poll. `reset` is true when the returned lines do not continue from `since` — the first call against a file larger than the window, a rotation or truncation, or the caller having fallen more than `max_bytes` behind — so the client knows its view has a gap and starts over. A missing or unreadable log returns an empty list rather than an error.

### `/userscript/<file>` — dynamic script injection

Serves the userscript with `@include` lines for the server's host (from `request.host`), enabling LAN IP access.
