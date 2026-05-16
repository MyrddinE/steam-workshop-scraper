# Web UI Architecture

The Web UI is a single-page application served by Flask and styled with Pico.css. It provides search, grid-based result display, detail viewing, and Steam Workshop subscription. The JS communicates exclusively with the Flask API endpoints via fetch.

---

## Layout

A flex-based layout with three zones:

- **Header**: title and port display
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

---

## Subscribe Feature

### Userscript Bridge (`userscripts/steam_subscribe.user.js`)

A Tampermonkey/Greasemonkey userscript that bridges the Steam session to the web UI:
- On `steamcommunity.com`: captures `sessionid` from cookies via `GM_setValue`, shows a toast notification on change
- On the scraper web UI: stamps `document.body.dataset.userscript = '1'` and `userscriptVer` for detection, pushes the sessionid to `/api/sessionid` every 30 seconds
- Version checking: reads `<meta name="userscript-version">` from the page and compares with `GM_info.script.version` — refuses to operate if outdated

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

Detail endpoint. Server-side bumps web/image/translation at detail priority (10) before fetching. Returns full item data with BBCode-to-HTML converted description.

### `/api/items` — POST

Bulk ID lookup. Accepts `{ids: [1, 2, 3]}`. Returns the same summary fields as `/api/search` for efficiency. Used by image polling.

### `/api/cutoffs` — POST

Wilson score percentile thresholds. Accepts `{filters}` (excluding percentile filters). Returns `{wilson_favorite_p99, wilson_favorite_p90, ...}`.

### `/api/state` — GET

Reads `.tui_state.yaml` for filter/sort state restoration.

### `/api/save_filter` — POST

Saves the current enrichment filters to `app_tracking` for the configured AppID.

### `/api/subscribe/<id>` — POST

Proxies a Steam Workshop subscribe request using stored session credentials.

### `/api/sessionid` — POST

Accepts sessionid from the userscript. Stores in `_sessionid` global (for server-side subscribe) and `_config["session"]["login_secure"]` (for Steam cookie). The TUI subscribe action also calls through the server endpoint.

### `/api/stats`, `/api/tags`, `/api/authors`, `/api/analysis`

Read-only endpoints returning database statistics.

### `/userscript/<file>` — dynamic script injection

Serves the userscript with `@include` lines for the server's host (from `request.host`), enabling LAN IP access.
