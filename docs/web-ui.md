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

Uses the same field/operator/value structure as the TUI. Operator options change dynamically when the field dropdown changes (`updateOps`). Three operator categories (text, numeric, id) plus an enum category, with `percentile` added to numeric for score-based filtering. Logic buttons (AND/OR) set `data-logic` attributes on filter rows.

**The value control follows the field's type.** `updateValueControl` builds an
`<input>` for every free-text field and a `<select>` of the schema's `values` for
an enum one (`Subscribed` is the only one), swapping the element in place so the
row keeps its slot and flex width. The enum choice list drops `any` while the
operator is `is_not` (a NOT over "everything" matches nothing); a value restored
from a saved view that the list does not offer is added as an extra option so the
view round-trips instead of being rewritten.

### The `Subscribed:` overlay

A labelled `<select id="subscribed-overlay">` beside the sort menus, defaulting to
`any` (no constraint) so the dropdown is its own on/off switch — there is no
separate checkbox. `doSearch` sends its value as `subscribed` beside `filters`, and
the server ANDs it as one predicate outside the builder's group (so an OR row
cannot undo it). It is deliberately not a builder row: changing the builder does
not clear it, `getFilters()` never returns it, and "Save for scraper" posts only
the builder's rows.

`_syncSubscribedOverlay` greys the control out (with a `title` giving the reason)
whenever a builder row names `Subscribed`, because a second constraint on the same
field is redundant or contradictory and silently ANDing two of them can empty the
result with no visible reason. While greyed out, `_overlayValue()` returns `any`,
so it contributes nothing. The overlay also travels to `loadCutoffs`, so the
percentiles describe the same population the grid shows.

### Percentile Validation

Two layers of validation: a capture-phase `blur` event listener on the document clamps values to 0-99 when the input loses focus, and `getFilters()` clamps again as a safety net before sending.

### Search Flow

1. `doSearch(reset=true)` fetches `/api/search` with the current filters, sort, and pagination state
2. Results are rendered as `.grid-cell` divs inside `#results-grid`
3. Each cell shows: preview image, or "pending"/"no image" when nothing has been recorded, or — when the server has already answered for that preview — the answer itself drawn in red at half the cell's height (a `404`, a `410`, or the content type it served instead). Plus title (2-line clamp), file size (color-coded via `sizeClass`), and Wilson subscriber/favorite scores (color-coded via `wClass`)
4. A pending marker in the corner names the stage the item is waiting on, by speed, colour and — on hover — a `title`: image rotates at 1x and is vivid green, translation 4x slower and mid green, the web scrape 16x slower and grey. `_pendingStage` picks the **fastest** stage that is pending, so a marker about to clear is never hidden behind a slower one, and the slow grey marker — which may stay for hours — is the quietest thing on the cell. `_applyPending` sets one `pending-<stage>` class, clearing the others, and writes the stage's wording onto the marker's `title`. The durations, colours and wording are mirrored from `src/pending.py`, and `tests/test_pending.py` fails if any of them disagree — the wording is kept in the shared table rather than the template, because the TUI draws the same state and a page-local description could drift from it. `api_priority` is deliberately not a stage: a list only shows items the API has already returned, so a pending refresh is not content anyone is waiting on.
4. `_placeSentinel()` handles infinite scroll by checking whether the first item of the batch is visible and placing or removing the scroll sentinel accordingly

Every number the grid and the detail pane show is formatted in the browser: `fmtCount` (three
significant digits with a K/M suffix) for views and subscription counts, `fmtExact` (grouped exact
digits) where a value is read rather than scanned, and `fmtSize` for file sizes. There is no
server-side equivalent: no template passes a value through a Jinja number filter, so the
`fcount`/`fsize` filters the server used to register had no consumer and were removed.

### State Persistence

The TUI saves filter/sort state to `.tui_state.yaml`, which the web UI reads through `GET /api/state`. That file is the TUI's: it has the TUI's shape (`scroll_y`, `selected_workshop_id`) and is rewritten on the TUI's schedule, so writing the browser's view back into it would have the two front ends overwriting fields the other does not understand. The browser therefore keeps its own view in `localStorage` under `view.state.v1` — filter rows, `sort_by`, `sort_order`, the `Subscribed:` overlay value, the open item and the grid's scroll position.

The entry is versioned and shape-checked like the statistics panel's ordering entry (`_loadViewState`): a wrong `v`, a non-list `filters`, or an unreadable value reads back as "no state" rather than reaching the builder. Fields the current schema no longer has are dropped, a stored value is coerced to the string the value control holds, and a stored `subscribed` value the current build does not know reads back as `any` (no constraint) rather than hiding rows.

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

An adaptive-timeout poll that keeps a rendered cell's markers in step with the database:
- Collects workshop_ids from rendered cells (`.grid-cell[data-wid]`) whose row is not settled: one
  with a stage spinner (`.has-spinner`), or one whose subscription marker is still `queued`. The
  subscription queue is deliberately not a stage, so a row queued only to subscribe has no spinner;
  selecting on `has-spinner` alone missed it, and a subscribe landing behind the cell's back left the
  green `queued` marker on it.
- POSTs to `/api/items` (read-only bulk ID lookup)
- For each returned item: updates the title, replaces the image placeholder with `_imageCellHtml(item)`
  when the server has an answer, re-applies the stage marker (`_applyPending`) and the subscription
  marker (`_applySub`), and rewrites the Wilson scores
- Delay: `max(1, log2(pending_count))` seconds → speeds up as work lands
- Stops when no rendered row needs re-reading
- Re-arms after *any* failed read, too: the `setTimeout` sits outside the `try`, and a non-`ok`
  response is a skipped tick rather than a stop. A 500 from a locked database is exactly the case
  that matters — the server answers it per request, so the next tick can succeed (see
  [Unattended Tolerance](#unattended-tolerance)).

**Reading the database rather than hooking each writer.** The poll is the one path that notices a
subscription change, so every writer of the flag is covered by one refresh: the userscript bridge's
`POST /api/subscribed/<id>`, this page's own cancel/clear calls to the same route, and the direct
`POST /api/subscribe/<id>` route (which stamps on Steam `success == 1`). The autosubscribe verifier
reads the same queue exit to mark the overlay's rows, independently of the grid. Because
`_listNeedsPoll` only runs when a batch is rendered, `toggleDetailQueue` also starts the poll on the
transition into `queued`, so a marker clicked into the queue after the search is watched too.

### Stopping the poll

`_stopListPoll` clears the timer and resets any image placeholder still reading `pending` back to
`no image`. The failure cell (`.grid-img-failed`) is exempt: it holds a status answer, not pending
text, so an answer is never overwritten with "no image".

### `_startDetailPoll`

A fixed 3-second poll on the currently-selected detail item. Checks `translation_priority > 0` to detect when translation completes, then re-renders the detail pane. Stops when `translation_priority` is 0, or when the item itself is gone (a 404). A *failure* to answer — a 500 from a locked database, or a dropped request — leaves the interval running so the next tick retries; treating every non-200 as a stop used to freeze the pane for the rest of the session after one transient error.

### Unattended Tolerance

The browser's polls and the TUI's polls read the same database, and the same transient lock reaches both — but not in the same shape. The Flask route isolates one request: a lock that outlives the connection's busy timeout becomes a 500 for that response and the server keeps serving. It cannot kill a thread, let alone the daemon. What it *can* do is end a client poll that treats a failed response as final, so both browser polls above re-arm on any failure instead: one 500 is a skipped tick, and the write is picked up on a later one. On the TUI side the equivalent reads are wrapped in `src.db_poll.guard_db_poll`, because there an exception in a timer callback does end the session ([tui.md](tui.md#unattended-reads)). Neither side re-tries a failure inside the same tick; the retry is the next scheduled read.

---

## Detail Pane

### `renderDetail`

Builds the detail view HTML inline. Shows: title (linked to Steam), creator (a jump-to-author button), workshop ID, Wilson scores (color-coded), the `Subscribed at` line when `own_first_subscribed_at` is set, created date, file size (color-coded), updated date (if different from created), views (via `fmtCount`), subscriptions/favorites (current/lifetime via `fmtCount`), tags (comma-separated from junction table or legacy JSON), Queue/Unqueue and Subscribe buttons, and description text (BBCode-to-HTML converted server-side).

**Subscribed at** is rendered as a `.stat-row` directly beneath the marker/title
line, formatted with the same `toISOString().slice(0,10)` convention the pane's
created/updated dates use. It is the sticky first-seen-subscribed stamp the
`Subscribed at` sort reads, worded with the marker's own "subscribed" vocabulary.
When `own_first_subscribed_at` is NULL the row is omitted entirely — an item never
seen subscribed must not gain a dated line implying an observation nobody made.
The stamp is on the payload because `_detail_payload` returns every `w.*` column
of the item `get_item_details` reads.

Stats are in a single-column vertical layout (`.stat-row`), not the previous two-column grid.

### Jump to author, and author mode

The creator in the heading is a button, the web equivalent of the TUI's `btn-jump-author`. It calls
`jumpToAuthor(creatorId)`, which enters the web's **author mode** — the counterpart of the TUI's
single-creator mode (`is_author_mode`, `action_return_from_author_mode`, `btn-return` in
`src/tui.py`). The filter rows are replaced by one `Author ID` / `is` row for that creator and the
search re-runs, so the view is everything by that author and nothing the previous filters would have
excluded. The same field, operator and value the TUI's jump builds.

The sort menus and the `Subscribed:` overlay are **not** part of the filter builder, so the jump
leaves them alone — exactly as the TUI leaves its sort alone. The filter area is swapped for an
author box (`#author-mode-bar`) naming the creator, with a `Return` button
(`#btn-return-author`), and Save Filter is hidden: the author row is not a filter set the user
assembled, and saving it would overwrite the scraper's stored filter with one they never chose. The
TUI hides the same button for the same reason.

`Return` restores the view exactly as the jump took it. Before the jump, `_viewSnapshot()` captures
the filter rows, both sort values, the overlay value, the open item and the grid's scroll position;
`returnFromAuthor()` puts them back and then restores the item and scroll through `_restoreView`, the
same routine a reload of a saved view runs. The snapshot is **in memory**, like the TUI's
`_pre_jump_filters`: `_saveViewState` is a no-op while `_authorMode` is set, so the value in
`localStorage` stays the view Return restores even if the search or the scroll listener runs in
between. Returning re-opens the item and puts the scroll back, which the TUI's Return does not; a
browser can afford it and the mode's point is that a half-restore is worse than none.

`creatorId` comes from the payload's `creator_id`, which is a string. A SteamID64 is seventeen digits
— beyond the range a JavaScript number represents exactly — so a numeric field would round in
`JSON.parse` and the filter would name a different account (`src/webserver.py`, `_detail_payload`). An
item with no creator renders the name plainly and offers no jump.

### The creator list

`#btn-authors` in the header opens a picker (`#author-modal`, `#author-list`), which
`GET /api/authors` fills. Picking a creator closes the picker and enters author mode through the same
`jumpToAuthor` the item jump uses, so there is one mode and one entry into it, not a second
implementation.

The route existed with no client before this. The item jump only reaches a creator whose item is
already on screen; the list is the way to reach one that is not, and it needs only the IDs
`/api/authors` already returns (`get_all_creator_ids`, `ORDER BY creator`) — no new endpoint and no new
query.

The TUI has no equivalent list, and this is recorded rather than glossed: `src.tui` imports
`get_all_creator_ids` and never calls it (the only caller is `/api/authors` in `src/webserver.py`), so the
TUI's only route to a creator is typing an `Author ID` into a filter row or jumping from one of that
creator's items — both of which the browser has too. This is therefore not a function one side has and
the other lacks; the picker is a third route to the same end, earned by the fact that a creator ID is
not something a person types or remembers. The parity position is stated in [tui.md](tui.md#jump-to-author).

### The subscription marker (queue / unqueue)

`renderDetail` draws the owner's subscription marker immediately before the title, and the grid
cell draws the same marker at its top-right. The subscription-queue overlay draws it on each row
too, from the same `/api/queued` payload. It has five states, resolved by
`subscription.subscription_state(item)` and rendered from the one table in `src/subscription.py`
(which the TUI reads too — see [tui.md](tui.md)):

| State | Glyph | Colour | Meaning | Click |
|---|---|---|---|---|
| `downloaded` | ★ | deep green | the owner is subscribed and Steam has the item on disk | nothing |
| `subscribed` | ★ | solid yellow | the owner is subscribed now | nothing |
| `queued` | ☆ | green | queued to subscribe | un-queues |
| `previously` | ☆ | yellow | we have seen the owner subscribed, and they are not now | queues |
| `never` | ○ | gray | never seen subscribed | queues |

`downloaded` requires **both** `own_subscribed` and the local `downloaded_at` latch, so a timestamp
left behind by a cleared subscription cannot claim the green star. The latch is written only by
`src/workshop_folders` (a periodic scan that finds the item's folder on disk) and cleared only when
the item leaves the owner's subscription list — so an unplugged drive or a moved library never takes
the green away. See [data-pipeline.md](data-pipeline.md) and [data-model.md](data-model.md).

There is exactly one such indicator: the old `queued` CSS class and its `★` prefix on `.grid-title`
are gone, and the pane's `Queue` / `Unqueue` button pair is replaced by the marker itself. The
marker's glyph, colour, CSS class, label, tooltip and clickability all arrive on the payload,
computed by the server from the shared table, so the page holds no copy of the state vocabulary.

Clicking the marker calls `toggleDetailQueue(wid)`, except for `subscribed`, which sends nothing —
the only action available there would be an unsubscribe, and an accidental unsubscribe is not
wanted. `toggleDetailQueue` POSTs the existing `/api/toggle_subscription_queue/<id>` route, which flips the
database flag and answers only `{ok: true}`. Since the route does not report which way the flag
moved, the client reads the item back through the read-only `/api/item/<id>` route and re-renders
the pane and the matching cell's marker (`_applySub`) from that payload — the same path the `s`
shortcut takes. A read-back rather than a locally flipped guess is deliberate: the `s` shortcut and
the subscribe drain's `/api/subscribed` calls change the same flag behind the pane's back, so a
guess could show the wrong state. Rendering from the item payload is also what lets the 3-second
translation poll re-render the pane without reverting the toggle. A failed request alerts and leaves
the pane alone.

The click is not the only writer. A subscribe can land behind a rendered cell through
`POST /api/subscribed/<id>` (the userscript bridge, or this page's cancel/clear calls) or through the
direct `POST /api/subscribe/<id>` route, and none of those touches the DOM. The list poll is what
re-reads such a cell: it keeps re-reading any row whose subscription marker is still `queued`, so
the marker moves to `subscribed` on its own next tick ([Image Polling](#image-polling)).

One transition is deliberately not a poll trigger: a cell already at `subscribed` is not re-read
when the folder scan later stamps `downloaded_at`, so its star turns green when the row is next
rendered (a new search or a rebuilt grid) rather than on a timer. Polling every subscribed row for
ever, just to catch a download, would cost a request per second on a settled view; the marker is
correct whenever it is drawn, which is the same promise the TUI's marker poll makes.

The marker sits inside the cell that opens the detail pane, so its click handler stops propagation:
without that, toggling the queue would also drag the pane to the item.

The Web UI's subscribe action now runs through the server, not the browser bridge. `doSubscribe` POSTs
`/api/subscribe/<id>`, which reads the item page on the server, takes that page's own CSRF token,
posts to Steam and records the answer — the route the TUI has always driven
([data-pipeline.md](data-pipeline.md#subscribe-engine-browser-free)). **The bridge is not removed.**
The userscript, `/api/sessionid`, `/api/subscribed`, the verification poll and the throttle endpoints
all stay exactly as they were; the page has simply stopped using the tab flow as its path. The owner
wants the new route proven in use before anything is deleted, so the bridge stays installed and the
deprecation stays recorded in
[future-plans.md](future-plans.md#removing-the-browser-bridge-from-the-subscribe-path).

`previously` can only ever mean "we have **seen** this account subscribed". Steam exposes no
per-account subscription history — `lifetime_subscriptions` is an item-wide count and
`EnumerateUserSubscribedFiles` is publisher-key-only — so on the day the marker shipped there were
zero `previously` markers regardless of real history, and they fill in over time. The marker's
tooltip says this rather than implying a complete record.

### Opening the downloaded item's folder (Windows only)

When the pane's item is in the `downloaded` state, `Open Folder` in `#detail-buttons` opens the
item's workshop folder — and the `o` key does the same for the focused grid cell, beside the `s` and
`l` shortcuts. The button is **visible but disabled** for anything not downloaded, with the reason in
its label (`Open Folder (not downloaded)`) and title, so the affordance is discoverable rather than
invisible; the key path shows the same refusal as an alert. The button and the shortcut are rendered
**only on Windows** (`open_folder_enabled`, computed by the server from
`src.workshop_folders`), so off Windows the page does not advertise an action that cannot happen.

The click POSTs to `POST /api/open_folder/<id>`, which uses the same shared helper as the TUI. **The
folder opens in Explorer on the host running the server, not in the browser** — the click travels to
the server and the window appears on that machine's desktop, which is the only desktop that has the
Steam library. The route refuses off Windows, for an item that is not in the `downloaded` state, and
when the folder is not on disk at click time (an unplugged drive, a moved library, Steam cleaned up);
the last case names the places it looked and changes nothing, so a stale marker is warned about
rather than launching anything into an error. A refusal is **400** with `{ok: false, message}`; a
success is **200** with `{ok: true, folder, message}`. The helper stamps no timestamps and clears
none — the only clearer of the green state is the subscription walk in
[data-pipeline.md](data-pipeline.md).

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

The row under the detail pane (`#detail-buttons`) holds the queue and database actions: **Fetch New**, **Update Visible**, and **Delete Never Fetched**.

**Delete Never Fetched** (`#btn-delete-never-fetched`, `doDeleteNeverFetched`) mirrors the TUI's command-palette action. `confirm()` names the exact set before anything is sent — items with no status or a 404 status whose API data was never fetched — because the delete is destructive and irreversible; declining sends no request at all. On a 2xx it reports the count returned by the route and re-runs the search, on a rejected response it shows the status, and on a dead backend it shows the error, so a failed clear is never presented as a successful one.

---

## Daemon Panel

The header toolbar's **Daemon** button (`#btn-daemon`) opens `#daemon-modal`, a modal panel with the running status and PID, Start / Stop / Restart buttons, and a live log view (`#daemon-log`).

While the panel is open, `_refreshDaemonStatus` polls `/api/daemon` and `_pollDaemonLog` polls `/api/daemon/log?since_offset=<byte-offset>` every 2 seconds. The offset is the byte position returned by the previous response, so each poll transfers only new lines. The view is a bounded preview: the server reads at most 64 KiB and returns at most 500 lines, so a first poll against a large log shows its tail rather than the whole file. A `reset: true` response means the returned lines do not continue the caller's view — a first call that had to seek to the tail, a rotation or truncation, or the client having fallen more than one window behind — and `_pollDaemonLog` clears the pane before showing them, so a gap is never rendered as if it were continuous. `_closeDaemonPanel` clears the interval, so nothing polls while the panel is hidden.

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

The 📊 button (`#btn-stats`, `templates/index.html:128`) opens `#stats-modal` (`templates/index.html:235`), a modal panel modelled on the daemon overlay. It keeps the button's id and position; clicking it no longer navigates to the raw `/api/stats` JSON.

`_openStatsPanel` (`templates/index.html:2467`) fetches `/api/metrics` for the catalogue — each metric's name, note and `seed_ms` hint — then builds one `<section class="stats-section" data-metric="...">` per metric, each with its own body element, and requests **every metric independently** through `GET /api/metrics/<name>` (`_loadMetric`, `templates/index.html:2437`). It deliberately does not `Promise.all` the requests: each section is filled and its own refresh timer armed the moment that metric lands, so a fast section draws while a slow one is still running. Every metric shows a human label, its note, and a value rendered to suit it, with its measured `ms` shown quietly in the heading:

* **coverage** — seven progress bars on one width (API Data, Translations, Extended Web, Extended Web Translation, Images, Creator, Creator Translation), at two scopes: the whole live library (dead items excluded), and the items the target AppIDs' stored `enrichment_filters` select — what the owner cares about. Each bar's population is the flagging rule, mirrored in SQL: **Translations** is per field (`title`, `short_description`) over the filter-selected items, because `_queue_translations` returns early unless the item was enriched; **Extended Web** is the scrape's coverage and its maximum excludes the pages that answered with no description, whose count and ceiling are printed with the bar; **Extended Web Translation** hangs off it and can never be longer than it, and its population is any scraped item, not only the filter-selected ones; **Creator Translation** counts items attributed to a creator whose name is non-ASCII. The three translation bars are drawn at half the standard bar's CSS height and sit flush under their parent — no margin, padding or row between them — with the same left edge, so a shorter bar still means less coverage. A bar whose population is zero reads "Nothing to translate" rather than a stuck 0.0%. The second figure is the search builder's SQL translation of the filters, which also searches each text field's `_en` counterpart, so it can disagree with the daemon's in-memory per-item check; where they disagree, the search builder's answer is shown. With more than one target AppID the population is the union of what any target's filters select. A scope note names the AppIDs and says why the two figures coincide when a filter set is empty, unreadable, or has no fixed predicate (a percentile); an unreadable set means no exclusion, never "excludes everything". The labels and counts come from the metric, so the two front ends make the same claim about the same data in the same words.
* **item_counts** — alive and dead counts with the overall total.
* **dead_items_by_queue** — flagged in red when non-zero: the number of dead items still sitting in a queue, broken down per queue. A zero value renders as an all-clear.
* **dead_queued**, **queued_nowhere** — the two handoff-invariant counters, rendered like `dead_items_by_queue`: red with the count when non-zero, a green all-clear sentence at the healthy zero. `dead_queued` counts dead items still holding a queue flag; `queued_nowhere` counts live items in no queue that the pipeline never completed. `dead_queued` and `dead_items_by_queue` are one question at two resolutions — the scalar invariant that must read zero, and the per-queue breakdown that says where to look — so both are kept.
* **priority_breakdowns** — one "queue: N waiting" block per queue with the priority mix.
* **translation_status**, **status_counts**, **fetch_recency** — labelled count lists.
* **web_throughput**, **image_throughput**, **translation_throughput** — completions in the last hour and the last day, then the last success as a formatted timestamp. When the queue's completion column holds no stamp at all the chunk reads "no history yet" rather than showing `0`: a stage that finished before the column existed has no recorded time, and a fabricated zero would read as an idle queue. Same wording as the TUI, so the two front ends make the same claim about the same data.
* **queue_eta** — one row per queue with outstanding depth, the rate in `per_day`, and the time to drain as `53d ± 30%`. The uncertainty is always a percentage, never an absolute span, and the time is one coarse unit so the percentage is the only second number; a queue with no completions in the window reads "no rate yet" and one with nothing outstanding reads "drained". The rate is in active time (pauses excluded), and the figure is marked `(gross)` for the three queues whose inflow nothing records. Same wording and same `_fmtDuration`/`_fmtUncertainty` shape as the TUI. The row carries the rate window, paused time and API inflow subtracted, and [data-pipeline.md](data-pipeline.md#queue-state-outstanding-rate-and-time-to-drain) owns the detail.
* **tag_counts** — a two-column table (Tag, Count) over every tag, sorted by count descending and scrolling inside a bounded box so a long list cannot push the chunks below it off the panel. Counts print in full (`fmtExact`) rather than through `fmtCount`: a tag count is read and compared, not merely scanned, and `fmtCount` reports 5,000 as "5.00K".
* **high_water**, **app_discovery** — a formatted timestamp and a per-AppID table.

**Ordering is learned.** The request order is seeded from each metric's `seed_ms` on the first ever open; on every later open it is sorted by the durations measured on the previous open, persisted in `localStorage` under `stats.metric-order.v1`. The stored shape is deliberately tiny and versioned — `{v: 1, ms: {metric: milliseconds}}` — so a stale or corrupt entry from an older build is ignored rather than breaking the panel (`_loadStatsOrder`, `templates/index.html:2398`; `_statsOrder`). The DOM order is fixed when the panel opens; this open's measurements feed the next open.

**Refresh is per metric.** Each chunk re-requests itself after `max(2 s, 50 × its own measured duration)` (`_intervalFor`, `templates/index.html:2394`), armed with `setTimeout` only once the previous response has landed, so a metric that is still computing is never started twice and a slow metric cannot hold up a fast one. `_closeStatsPanel` (`templates/index.html:2501`) clears every per-metric timer and bumps `_statsToken`, so responses still in flight are discarded rather than written into the closed panel.

---

## View Window Analysis Panel

The header toolbar's **Analysis** button (`#btn-analysis`, `templates/index.html:129`) opens `#analysis-modal` (`templates/index.html:243`), the web half of the TUI's `ctrl+?` screen. It mirrors that screen's content: a bucket-size box in days, a **Recalculate** button, a per-bucket table, and a summary line.

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
drain retries it, whereas a genuine failure is cleared. The UI polls `/api/subscribe_throttle` once per tab it
opens and stops opening more while Steam is refusing us, telling the user when the rest can be retried.
The budget is per account or address and refills over minutes.

### Detection (`_userscriptPresent`)

Checks `document.body.dataset.userscript` for presence and `userscriptVer` against the page's expected version from the meta tag. If outdated, offers to open the install URL.

**Nothing calls it on the subscribe path any more**, because that path is the server route and needs no
userscript. The function, the `userscript-version` meta and the endpoint it points at are kept as
part of the bridge, which stays installed; the button's own install prompt is gone with its last
caller, so the script is reachable by visiting `/userscript/steam_subscribe.user.js` directly. The
detection contract — the meta tag and the script's own `@version` agreeing — is still asserted by
`tests/test_userscript_contract.py`.

### Install URL

`/userscript/steam_subscribe.user.js` is a dynamic endpoint that injects `@include` lines for the server's host IP and port, so the script works on LAN IPs as well as localhost. It was the "Subscribe" button's install link; that button no longer opens tabs, so the URL is reached directly.

### Subscribe Flow

1. User clicks Subscribe on the web UI → `doSubscribe` POSTs `/api/subscribe/<workshop_id>`
2. Server reads `steamLoginSecure` (from config `session.login_secure`, which can be a YAML list joined with `%7C%7C`) and the item page it fetches on the shared web interval; the CSRF token is that page's own `g_sessionID`, with the pushed `_pushed_sessionid` and config `session.csrf_token` as fallbacks
3. Server POSTs to `steamcommunity.com/sharedfiles/subscribe` with browser-like headers (User-Agent, Origin, Referer with workshop URL) and cookies
4. Steam's answer is mapped to user-facing messages: a refusal (`success: 2`/`15`, or HTTP 401) is a stale CSRF token when the same attempt's page read was authenticated — the login is not reported as expired — and a session problem only when that read was anonymous
5. On `success: 1` the route stamps `own_subscribed` and clears the queue flag; the page then re-reads `/api/item/<id>` and re-renders the pane and the matching cell's marker from that payload, the same read-back a queue toggle uses

The button sends no tab and needs no userscript. `_userscriptPresent` and its install prompt remain in the page, but nothing in the subscribe action calls them any more: the bridge is kept for its own endpoints, not as this button's fallback. A refusal shows the route's own message rather than falling back to a tab.

**A drain serialises; it does not schedule.** `_startAutoSubscribe` loops over `/api/queued` and
awaits `/api/subscribe/<id>` for each item before starting the next. The route's page read is gated on
the shared web interval and its POST is exempt, so the interval is already paid once per item inside
the call; a second client-side delay would pay it twice, and firing the items without awaiting would
run two gated reads concurrently against one shared interval. This is why the row timer shows elapsed
time for the row being asked about rather than a countdown to a scheduled tab.

### Queued-row timing, and why it differs from the TUI's

The autosubscribe overlay keeps one timer (`_subScheduleIv`) whose only job is to tick an elapsed
figure on the row currently being asked about; the row's outcome is written from the route's own
answer. There is deliberately **no schedule of opens**: the drain awaits each
`/api/subscribe/<id>` and the route's page read pays the shared interval, so the POST it sends is
exempt and a row's cost is one gated read — the same shape the TUI's queue has for its POST, though
the TUI additionally spends a confirmation read per item (`src/subscribe_engine.py`). Nothing in the
page spaces the requests: the route reads the configured delay itself, fresh per call, so a throttle's
doubling mid-pass moves the pacing without the page knowing, and a second client-side delay would pay
the interval twice per item.

`daemon.web_delay_seconds` is still injected as `WEB_DELAY`, but the page no longer uses it to space
anything. It is left in place rather than removed: it is served from `src/webserver.py` and is the
number a reader would expect the page's own timing to be built from, and the removal of the tab flow
that used it is the separate workstream recorded in
[future-plans.md](future-plans.md#removing-the-browser-bridge-from-the-subscribe-path). The TUI
screen's own description is in [tui.md](tui.md#subscription-queue-sl-keys).

---

## Server Endpoints

### `/api/search` — POST

Main search endpoint. Accepts `{filters, subscribed, sort_by, sort_order, offset, limit}`. `subscribed` is the `Subscribed:` overlay value and is ANDed as one predicate outside the builder's group; `any` or a value the build does not know adds nothing. Server-side bumps web/image/translation priorities and re-queries priority fields to include updated values. Returns 50 items with summary fields. `sort_by` accepts the `VALID_SORT_COLS` whitelist, including `own_first_subscribed_at` (**Subscribed at**; descending leaves never-subscribed rows last).

### `/api/item/<id>` — GET

Read-only detail fetch. Returns full item data with the BBCode-to-HTML converted description. It applies no priority bumps by design: the detail pane polls this every three seconds while it is open, and applying detail priority here re-armed the fetch queue on every poll, so the daemon re-fetched whatever was on screen indefinitely.

Both language variants are returned — `description_html` beside `description_html_original`, and `display_title` beside `display_title_original` — so the client's toggle costs no request. The payload also carries `has_translation` (whether `translate_version` is set) so the client does not have to infer it from the text, and `creator_id` (the creator's SteamID64 as a string, so a seventeen-digit ID survives `JSON.parse` and can be fed back into an Author ID filter).

### `/api/item/<id>/open` — POST

The same detail payload, but applies detail-level priority (web, image, translation and API) first. This is the path the UI takes when a pane opens, and the only one that re-queues the item.

### `/api/items` — POST

Bulk ID lookup. Accepts `{ids: [1, 2, 3]}`. Returns the same summary fields as `/api/search` for efficiency. Used by image polling.

Both list routes attach the image classification the grid branches on before serialising: `image_state`
(from `images.image_state`) and `image_resolved`, which is `images.is_resolved(stored)` itself rather
than the server spelling out which states are settled. `src/images.py` is the one decider, so a change
to the predicate moves the page and the TUI together instead of leaving the page quietly disagreeing.

### `/api/cutoffs` — POST

Wilson score percentile thresholds. Accepts `{filters, subscribed}` (filters excluding percentile filters). The overlay is included so the percentiles describe the same population the grid shows. Returns `{wilson_favorite_p99, wilson_favorite_p90, ...}`.

### `/api/state` — GET

Reads `.tui_state.yaml` for filter/sort state restoration. The client uses this as the **first-visit seed only**: once the browser has its own `view.state.v1` entry, this route is not called at all. The seed includes the TUI's `subscribed_overlay` value as well as `filters`, `sort_by` and `sort_order`.

### `/api/delete_never_fetched_items` — POST

Deletes every pending item — those with no status or a 404 status and no successful API fetch (`delete_never_fetched_items`, `src/database.py:3427`) — and returns `{ok, deleted}` with the number of rows removed. This is the same predicate and the same delete as the TUI's `action_delete_never_fetched_items`; there is deliberately no dry-run mode. The UI asks for confirmation first, naming what will be deleted.

### `/api/save_filter` — POST

Saves the current enrichment filters to `app_discovery` for the configured AppID. The body is `getFilters()` — the builder's rows only. The `Subscribed:` overlay is view state and is deliberately not written here, so what the scraper enriches with stays the set the builder shows. When no target AppID is configured it answers **400** with `{"error": "No target AppID configured"}`; the client shows that message and only reports success on a 2xx, so a rejected save is never presented as a stored one.

### `/api/subscribe/<id>` — POST

Performs the subscribe against Steam directly, with no browser tab, and returns Steam's JSON body unchanged — the TUI reads `success` and `message` from it. The request shares the scraper's session and presents the project's own Firefox User-Agent. The cookies come from one `web_scraper._build_workshop_cookies` read: the signed-in Firefox profile's whole `steamcommunity.com` set when `session.read_firefox_cookies` is on, otherwise the configured `sessionid`/`login_secure` pair. **The CSRF token does not come from that read**, because `sessionid` is a session cookie Firefox keeps in memory and never writes to `cookies.sqlite`; it comes from the item page this route reads for the attempt (`g_sessionID`, the token belonging to the session that served that page), put back into the cookie jar so the form field and the cookie agree. A `sessionid` already in the set, the pushed `_pushed_sessionid` global and `session.csrf_token` are fallbacks only for a page that carries no token. That read is a page load, so it is gated on the shared web interval — `daemon.web_delay_seconds` through `configured_web_delay` and `pacing.wait` — exactly like the engine's reads.

It refuses before spending a request when the set has no `steamLoginSecure` (**400**, with a message naming the remedy: sign in to Steam in the browser the daemon reads cookies from, or configure `session.login_secure`), and when `session_health.evaluate_login` says the credential's own token has expired (**400**, the reason recorded through `session_health.record_rejected` so the [session warning](#the-session-warning) shows it). A Steam `success` of `2` or `15`, or an **HTTP 401**, is a refusal of the CSRF token, and what it means depends on the page read the same attempt made: beside an **authenticated** page read the credential is proven good, so **no session problem is recorded** and the refusal is logged as a token refusal (recording one is what used to tell the owner to sign in again while the login was working); beside an **anonymous** page read it is recorded as a session problem the same way as before. The response body is passed through untouched either way. A `success` of `1` records the confirmation with `mark_own_subscribed`, setting `own_subscribed` and clearing `is_queued_for_subscription` exactly as `/api/subscribed/<id>` does, and clears any recorded session problem. A missing item still answers **404** `Item not found.`, an item with no AppID still **400** `Item has no AppID.`, and a transport failure still **502**. Each of those refusals logs the `workshop_id` and the reason, and the POST line carries a SHA-256 **fingerprint** of the token — never the token itself.

### `/api/toggle_subscription_queue/<id>` — POST

Flips `is_queued_for_subscription` for one item and answers `{ok: true}`. It is the route behind both the `s` shortcut on a grid cell and the detail pane's Queue/Unqueue button. It returns no new state, so the detail pane reads the item back through the read-only `/api/item/<id>` route to label its button.

### `/api/open_folder/<id>` — POST

Windows only. Opens the item's downloaded workshop folder in Explorer **on the host running the server** (the browser's own machine is not involved), through the shared `src.workshop_folders.open`. It refuses with **400** `{ok: false, message}` when the platform is not Windows, when the item is not in the `downloaded` state (`own_subscribed` and `downloaded_at` both set), and when the folder is not on disk at click time — naming the folders it looked in, and changing nothing. A success is **200** `{ok: true, folder, message}`. The route is not rendered into the page off Windows, and no state is written or cleared either way.

### `/api/sessionid` — POST

Accepts sessionid from the userscript. Stores it in the `_pushed_sessionid` global (for server-side subscribe, where it is now only the fallback for a page that carries no `g_sessionID` — see `/api/subscribe/<id>` above); if the payload also carries a `login_secure` value that differs from the configured one, that is written to `_config["session"]["login_secure"]` (for the Steam cookie) and persisted so the daemon picks it up. The TUI subscribe action also calls through the server endpoint.

A push whose `login_secure` matches what is already configured writes nothing. The bridge re-pushes on a timer, so without that guard an open Steam tab rewrote `config.yaml` — a YAML serialisation and a file write — every thirty seconds with a value that had not moved. The CSRF token is still taken from every push, because it lives only in memory. A push that *does* carry a changed cookie is also the best local evidence that the login works again — it comes from the operator's own signed-in browser — so a value that is not already expired clears the [session warning](#the-session-warning) without waiting for a scrape to confirm it.

### `/api/session` — GET

Whether the daemon's Steam login is still working, for the session warning banner: `{problem, detail, detected_at, login_url}`. `detail` is the sentence the daemon recorded and `login_url` is where an operator signs in again, so neither is hard-coded in the template. `problem` is false when nothing has been recorded — including when the recorded section is present but carries no sentence, since an unexplained warning is worse than none. One small YAML file read, because the banner polls it.

### `/api/session/recheck` — POST

Re-reads `steamLoginSecure` from the browser's cookie store after the operator signs in, using the same lookup the daemon prefers. A cookie that is not already expired from its own token is saved to `config.yaml` — which the daemon re-reads per request and per batch, so nothing needs restarting — and the warning is cleared. The judgement is local: proving the cookie by spending a request would duplicate what the next scrape is about to do anyway, and the daemon's answer corrects the banner if Steam still refuses. When the browser has nothing newer, the route answers `{ok: false, problem: true, detail}` with the reason (which may be the configured cookie's expiry, or that there is no cookie at all), and a cookie found but not writable answers **500** with the failure named, so a save that did not happen is never reported as a cleared warning.

### `/api/stats`, `/api/tags`, `/api/authors`

Read-only endpoints returning database statistics. `/api/stats` still returns the old flat payload; the statistics panel uses the per-metric endpoints below instead. `/api/authors` returns the distinct creator IDs in `ORDER BY creator` and is consumed by the creator picker ([The creator list](#the-creator-list)).

### `/api/analysis` — GET

Age-bucketed view statistics, consumed by the view window analysis panel (`api_analysis`, `src/webserver.py:540`). Accepts `bucket_days=N` (default 7) and returns `{buckets: [{age_start, age_end, count, median, p10, p90}], estimated_window_days, items_analyzed}`. The bucket width is floored at one day, because the parameter comes straight from the query string and a zero width would divide by zero (the TUI clamps its input the same way). `estimated_window_days` is the knee — the first bucket whose median drops below a quarter of the early-bucket peak — and is `null` when there is too little data to find one, a distinction the panel renders honestly rather than collapsing to zero.

### `/api/metrics` — GET

The metric catalogue: `{"metrics": [{name, note, seed_ms}], "default_order": [names]}`, where `default_order` and `metrics` are both in seed order (cheapest seed hint first). The panel draws its layout from this rather than hard-coding metric names, so a metric added on the server appears without a client change (`src/webserver.py:500`). `seed_ms` is only a first-open ordering hint; the client replaces it with its own measurements.

### `/api/metrics/<name>` — GET

One metric, computed on its own: `{name, value, ms, note, seed_ms}`, where `ms` is what that metric actually cost. An unknown name returns a 404 with `{error}` (`src/webserver.py:517`). Separate requests are what make the chunks independent: whichever finishes first renders first, and a slow metric cannot hold up a fast one.

### `/api/daemon` — GET

Daemon status: `{running, pid, log_file}`, where `log_file` is the configured `logging.file` path or null.

### `/api/daemon/start`, `/api/daemon/stop`, `/api/daemon/restart` — POST

Drive the background daemon through the shared `DaemonController`. Each returns `{ok, changed, message}`; starting a running daemon or stopping a stopped one is an idempotent no-op that still reports success.

### `/api/daemon/log` — GET

Incremental log tail and bounded preview. Accepts `since_offset=<byte-offset>` and returns `{lines, offset, reset}`; the pre-rename `since` key is still accepted, because a page served before the rename may still be open in a browser. At most 64 KiB is read (`DaemonController.TAIL_BYTES`, `src/daemon_control.py:23`) and at most 500 lines are returned (`TAIL_LINES`, `src/daemon_control.py:24`), so a first call (`since_offset <= 0`) returns the tail of the file rather than the whole of it. `offset` is the byte position to pass as `since_offset` on the next poll. `reset` is true when the returned lines do not continue from `since_offset` — the first call against a file larger than the window, a rotation or truncation, or the caller having fallen more than `max_bytes` behind — so the client knows its view has a gap and starts over. A missing or unreadable log returns an empty list rather than an error.

### `/userscript/<file>` — dynamic script injection

Serves the userscript with `@include` lines for the server's host (from `request.host`), enabling LAN IP access.
