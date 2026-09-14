# Web UI Architecture

The Web UI is a single-page application served by Flask and styled with Pico.css. It provides search, grid-based result display, detail viewing, and Steam Workshop subscription. The JS communicates exclusively with the Flask API endpoints via fetch.

---

## Layout

A flex-based layout with three zones:

- **Header**: title and the embedded server's port. `#port-display` is filled from `location.port` on load, so it shows where the panel is actually bound — including an ephemeral or reconfigured port — without asking the server. A default port renders nothing rather than a misleading `:80`.
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
3. Each cell shows: preview image (or "pending"/"no image" placeholder), title (2-line clamp), file size (color-coded via `sizeClass`), and Wilson subscriber/favorite scores (color-coded via `wClass`)
4. `_placeSentinel()` handles infinite scroll by checking whether the first item of the batch is visible and placing or removing the scroll sentinel accordingly

### State Persistence

The TUI saves filter/sort state to `.tui_state.yaml`. The web UI reads it via `/api/state` on load and restores the builder. Filter changes in the web UI must originate from the TUI (or be manually applied) — the web UI doesn't save state directly.

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
- For each returned item: updates title text, swaps placeholder for `<img>` if `image_extension` arrived, appends `.` to pending text for visual progress
- Delay: `max(1, log2(pending_count))` seconds → speeds up as images arrive
- Stops when no pending placeholders remain in the DOM

### Dot Animation

Each poll cycle appends a `.` to the placeholder text via `ph.textContent += '.'`. This gives visual feedback that the poll is iterating over the cell. When an image arrives, the entire placeholder div is replaced by an `<img>`, so dots naturally clear.

### `_startDetailPoll`

A fixed 3-second poll on the currently-selected detail item. Checks `translation_priority > 0` to detect when translation completes, then re-renders the detail pane. Stops when `translation_priority` is 0.

---

## Detail Pane

### `renderDetail`

Builds the detail view HTML inline. Shows: title (linked to Steam), creator, workshop ID, Wilson scores (color-coded), created date, file size (color-coded), updated date (if different from created), views (via `fmtCount`), subscriptions/favorites (current/lifetime via `fmtCount`), tags (comma-separated from junction table or legacy JSON), Open on Steam link, Subscribe button, and description text (BBCode-to-HTML converted server-side).

Stats are in a single-column vertical layout (`.stat-row`), not the previous two-column grid.

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

## Daemon Panel

The header toolbar's **Daemon** button (`#btn-daemon`) opens `#daemon-overlay`, a modal panel with the running status and PID, Start / Stop / Restart buttons, and a live log view (`#daemon-log`).

While the panel is open, `_refreshDaemonStatus` polls `/api/daemon` and `_pollDaemonLog` polls `/api/daemon/log?since=<byte-offset>` every 2 seconds. The offset is the byte position returned by the previous response, so each poll transfers only new lines. The view is a bounded preview: the server reads at most 64 KiB and returns at most 500 lines, so a first poll against a large log shows its tail rather than the whole file. A `reset: true` response means the returned lines do not continue the caller's view — a first call that had to seek to the tail, a rotation or truncation, or the client having fallen more than one window behind — and `_pollDaemonLog` clears the pane before showing them, so a gap is never rendered as if it were continuous. `_closeDaemonPanel` clears the interval, so nothing polls while the panel is hidden.

---

## Statistics Panel

The 📊 button (`#btn-stats`, `templates/index.html:99`) opens `#stats-overlay` (`templates/index.html:136`), a modal panel modelled on the daemon overlay. It keeps the button's id and position; clicking it no longer navigates to the raw `/api/stats` JSON.

`_openStatsPanel` (`templates/index.html:1316`) fetches `/api/metrics` for the catalogue — each metric's name, note and `seed_ms` hint — then builds one `<section class="stats-chunk" data-metric="...">` per metric, each with its own body element, and requests **every metric independently** through `GET /api/metrics/<name>` (`_loadMetric`, `templates/index.html:1286`). It deliberately does not `Promise.all` the requests: each section is filled and its own refresh timer armed the moment that metric lands, so a fast chunk draws while a slow one is still running. Every metric shows a human label, its note, and a value rendered to suit it, with its measured `ms` shown quietly in the heading:

* **coverage** — a progress bar per stage (API data, description, image, translation, creator) with the count and the live-item total.
* **totals** — alive and dead counts with the overall total.
* **stuck_work** — flagged in red when non-zero: the number of dead items still sitting in a queue, broken down per queue. A zero value renders as an all-clear.
* **priority_breakdowns** — one "queue: N waiting" block per queue with the priority mix.
* **translation_status**, **status_counts**, **fetch_recency** — labelled count lists.
* **tag_counts** — a summary: distinct-tag count and the most-used tags.
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

Both language variants are returned — `description_html` beside `description_html_original`, and `display_title` beside `display_title_original` — so the client's toggle costs no request. The payload also carries `has_translation` (whether `translate_version` is set) so the client does not have to infer it from the text.

### `/api/item/<id>/open` — POST

The same detail payload, but applies detail-level priority (web, image, translation and API) first. This is the path the UI takes when a pane opens, and the only one that re-queues the item.

### `/api/items` — POST

Bulk ID lookup. Accepts `{ids: [1, 2, 3]}`. Returns the same summary fields as `/api/search` for efficiency. Used by image polling.

### `/api/cutoffs` — POST

Wilson score percentile thresholds. Accepts `{filters}` (excluding percentile filters). Returns `{wilson_favorite_p99, wilson_favorite_p90, ...}`.

### `/api/state` — GET

Reads `.tui_state.yaml` for filter/sort state restoration.

### `/api/save_filter` — POST

Saves the current enrichment filters to `app_tracking` for the configured AppID. When no target AppID is configured it answers **400** with `{"error": "No target AppID configured"}`; the client shows that message and only reports success on a 2xx, so a rejected save is never presented as a stored one.

### `/api/subscribe/<id>` — POST

Proxies a Steam Workshop subscribe request using stored session credentials.

### `/api/sessionid` — POST

Accepts sessionid from the userscript. Stores it in the `_sessionid` global (for server-side subscribe); if the payload also carries a `login_secure` value that differs from the configured one, that is written to `_config["session"]["login_secure"]` (for the Steam cookie) and persisted so the daemon picks it up. The TUI subscribe action also calls through the server endpoint.

A push whose `login_secure` matches what is already configured writes nothing. The bridge re-pushes on a timer, so without that guard an open Steam tab rewrote `config.yaml` — a YAML serialisation and a file write — every thirty seconds with a value that had not moved. The CSRF token is still taken from every push, because it lives only in memory.

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
