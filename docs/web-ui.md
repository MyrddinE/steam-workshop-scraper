# Web UI Architecture

The Web UI is a single-page application served by Flask and styled with Pico.css. It provides search, grid-based result display, detail viewing, and Steam Workshop subscription. The JS communicates exclusively with the Flask API endpoints via fetch.

---

## Layout

A flex-based layout with three zones:

- **Header**: title and the embedded server's port. `#port-display` is filled from `location.port` on load, so it shows where the panel is actually bound — including an ephemeral or reconfigured port — without asking the server. A default port renders nothing rather than a misleading `:80`.
- **Session warning** (`#session-warning`): a strip between the header and the panes, hidden until the daemon reports that its Steam login has stopped working. It is not dismissible; see [The Session Warning](#the-session-warning).
- **Left pane** (`#results-pane`): a CSS Grid of result cards (`#results-grid`) with `repeat(auto-fill, minmax(200px, 1fr))` for responsive columns. Infinite scroll observes the newest batch's first cell directly, so no extra element sits in the grid.
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
4. `_observeNextBatch()` handles infinite scroll: it releases the previous batch's observation, then watches the first cell of the batch just rendered — or, when that cell is already on screen, asks for the next page at once

Every number the grid and the detail pane show is formatted in the browser: `fmtCount` (three
significant digits with a K/M suffix) for views and subscription counts, `fmtExact` (grouped exact
digits) where a value is read rather than scanned, and `fmtSize` for file sizes. There is no
server-side equivalent: no template passes a value through a Jinja number filter, so the
`fcount`/`fsize` filters the server used to register had no consumer and were removed.

### State Persistence

The TUI saves filter/sort state to `.tui_state.yaml`, which the web UI reads through `GET /api/state`. That file is the TUI's: it has the TUI's shape (`scroll_y`, `selected_workshop_id`) and is rewritten on the TUI's schedule, so writing the browser's view back into it would have the two front ends overwriting fields the other does not understand. The browser therefore keeps its own view in `localStorage` under `view.state.v1` — filter rows, `sort_by`, `sort_order`, the `Subscribed:` overlay value, the open item and the grid's scroll position.

The entry is versioned and shape-checked like the statistics panel's ordering entry (`_loadViewState`): a wrong `v`, a non-list `filters`, or an unreadable value reads back as "no state" rather than reaching the builder. Fields the current schema no longer has are dropped, a stored value is coerced to the string the value control holds, and a stored `subscribed` value the current build does not know reads back as `any` (no constraint) rather than hiding rows.

**Precedence is one-sided.** A browser that has been to the page before has its own record of what the user was doing, so local state wins outright and `/api/state` is not even fetched. Only when there is no usable entry — a first visit, cleared storage, or a rejected shape — does the page seed from the TUI's saved state.

**Restoring a deep view.** After the first search, `_restoreView` keeps calling `doSearch(false)` — the function that owns `currentOffset` and the infinite-scroll observation — until the grid is tall enough for the saved scroll position and the selected item is present, re-opens that item, then applies the scroll last: `showDetail` focuses the cell and focus can move the grid, so the saved position has to be the final word. Paging is capped at `MAX_RESTORE_BATCHES` so a selection that no longer matches the filters cannot walk the whole result set, and a reset `doSearch` clears the selection because a new result set may not contain it. Writes are suppressed while a restore runs (`_restoringView`), so the page cannot overwrite the state it is reading. Saves happen on a throttled `#results-grid` `scroll` listener, at the end of a reset `doSearch`, when a detail pane opens (`showDetail`), and on `pagehide`.

### Wilson Cutoffs

`loadCutoffs()` fetches percentile thresholds from `/api/cutoffs` (which calls `compute_wilson_cutoffs`). These are used by `wClass` for color-coded score display.

---

## Infinite Scroll

### `_observeNextBatch`

After each `doSearch` batch, `doSearch` passes the batch's first cell — the cell it built for index 0 — to `_observeNextBatch`:
- It releases whatever cell the observer was watching before, so an older batch's cell, still in the DOM, cannot fire a second time when it scrolls back into view
- If the first cell is already within the viewport → triggers `doSearch(false)` immediately (nothing is observed)
- If it is below the viewport → `_scrollObserver.observe(firstCell)`

Nothing is inserted into the grid, so infinite scroll consumes no grid cell and no later cell shifts column.

### `IntersectionObserver`

A single observer watches the newest batch's first cell. When that cell enters the viewport, it fires `doSearch(false)`. No `rootMargin` — the cell is the first unseen item, so the observer fires exactly when it scrolls into view.

The observer is created once at page load. `_observeNextBatch` `unobserve`s the previous batch's cell and then `_scrollObserver.observe(firstCell)` each time a batch renders. The observer guards `currentOffset > 0` to prevent firing before the initial search.

When `doSearch(reset=true)` clears the grid (`innerHTML = ''`), the observed cell detaches with the grid content, so the reset path calls `_observeNextBatch(null)` to drop the observation; the fresh batch's first cell is observed by the `_observeNextBatch` call at the end of `doSearch`.

---

## One item-update path

Every display of an item subscribes to its `workshop_id` while it is showing it: a grid cell joins the
registry when `doSearch` creates it, the detail pane when `showDetail` opens it, and each leaves when
it stops (a reset search unsubscribes the cells it clears, a new pane unsubscribes the pane's previous
item, and `dispatchItemUpdate` drops a subscriber whose element is detached). So `_itemSubscribers`
describes what is on screen, not what exists in the database, and the registry is the only coupling:
no caller has to remember which component to refresh, and a display added later is correct as soon as
it subscribes. The TUI's `src/item_updates.py` is the same mechanism on the other side.

`dispatchItemUpdate(item)` is the page's one dispatch point. A poll's block, a click's read-back, a
subscribe landing and an action all go through it, and it hands the whole block to each subscriber for
that id. A subscriber applies the fields it draws and ignores the rest: a grid cell's applier
(`_applyGridCellUpdate`) draws the title, image, size, stage marker, subscription marker and Wilson
scores, and the detail pane's subscriber (`_detailSubscriber`) merges the block into the item it holds
and calls `renderDetail`, so a summary block from a poll cannot blank a description the full payload
brought. A field added later therefore reaches every panel that renders it without a new call site.

**`_startItemUpdatePoll` — the trigger that is not conditional on anything pending.** On a 3-second
cadence (`_ITEM_UPDATE_POLL_MS`, mirroring `ITEM_UPDATE_POLL_SECONDS` in `src/item_updates.py`) it
POSTs `/api/items` with exactly the registry's ids and dispatches the answer. The bound is the on-screen
count: one batched read per tick, never a table scan and never one read per component; an empty
registry stops the timer, and a later search or pane open re-arms it. It deliberately does not stop
when nothing is `pending` — that condition is exactly what let a change written behind the page's back
(the daemon's folder scan stamping `steam_download_seen_at`) sit unfetched and leave the cell and the
pane disagreeing. One failed read is a skipped tick and the next tick retries, like every other browser
poll.

---

## Image Polling

### `_startListPoll`

An adaptive-timeout poll that keeps a rendered cell's markers in step with the database:
- Collects workshop_ids from rendered cells (`.grid-cell[data-wid]`) whose row is not settled: one
  with a stage spinner (`.has-spinner`), or one whose subscription marker is still `queued`. The
  subscription queue is deliberately not a stage, so a row queued only to subscribe has no spinner;
  selecting on `has-spinner` alone missed it, and a subscribe landing behind the cell's back left the
  green `queued` marker on it. This is the low-latency path; correctness no longer depends on it,
  because `_startItemUpdatePoll` above runs whether or not anything is pending.
- POSTs to `/api/items` (read-only bulk ID lookup)
- Hands every returned item's block to `dispatchItemUpdates`, so the cell, the pane and anything else
  subscribed to that id all draw from the same payload
- Delay: `max(1, log2(pending_count))` seconds → speeds up as work lands
- Stops when no rendered row needs re-reading
- Re-arms after *any* failed read, too: the `setTimeout` sits outside the `try`, and a non-`ok`
  response is a skipped tick rather than a stop. A 500 from a locked database is exactly the case
  that matters — the server answers it per request, so the next tick can succeed (see
  [Unattended Tolerance](#unattended-tolerance)).

**Reading the database rather than hooking each writer.** The poll is the one path that notices a
subscription change, so every writer of the flag is covered by one refresh: this page's own
cancel/clear calls to `POST /api/dequeue/<id>` (and the outcome stamps
`/api/subscribed`, `/api/unsubscribed`), and the direct `POST /api/subscribe/<id>` route (which
records on Steam `success == 1`, in either direction). The drain reads the same queue exit to mark
the overlay's rows, independently of the grid. Because `_listNeedsPoll` only runs when a batch is
rendered, `toggleDetailQueue` also starts the poll on the transition into `queued` or
`queued_remove`, so a marker clicked into the queue after the search is watched too.

### Stopping the poll

`_stopListPoll` clears the timer and resets any image placeholder still reading `pending` back to
`no image`. The failure cell (`.grid-img-failed`) is exempt: it holds a status answer, not pending
text, so an answer is never overwritten with "no image".

### `_startDetailPoll`

A fixed 3-second poll on the currently-selected detail item. Checks `translation_priority > 0` to detect when translation completes, then hands the completed item to `dispatchItemUpdate` — the pane redraws because it subscribed, not because this poll reached into it — and stops. Stops when `translation_priority` is 0, or when the item itself is gone (a 404). A *failure* to answer — a 500 from a locked database, or a dropped request — leaves the interval running so the next tick retries; treating every non-200 as a stop used to freeze the pane for the rest of the session after one transient error.

### Unattended Tolerance

The browser's polls and the TUI's polls read the same database, and the same transient lock reaches both — but not in the same shape. The Flask route isolates one request: a lock that outlives the connection's busy timeout becomes a 500 for that response and the server keeps serving. It cannot kill a thread, let alone the daemon. What it *can* do is end a client poll that treats a failed response as final, so every browser poll above — `_startItemUpdatePoll`, `_startListPoll` and `_startDetailPoll` — re-arms on any failure instead: one 500 is a skipped tick, and the write is picked up on a later one. On the TUI side the equivalent reads are wrapped in `src.db_poll.guard_db_poll`, because there an exception in a timer callback does end the session ([tui.md](tui.md#unattended-reads)). Neither side re-tries a failure inside the same tick; the retry is the next scheduled read.

---

## Detail Pane

### `renderDetail`

Builds the detail view HTML inline. Shows: title (linked to Steam), creator (a jump-to-author button), workshop ID, Wilson scores (color-coded), the `Subscribed at` line when `own_first_subscribed_at` is set, created date, file size (color-coded), updated date (if different from created), views (via `fmtCount`), subscriptions/favorites (current/lifetime via `fmtCount`), tags (comma-separated from junction table or legacy JSON), Queue/Unqueue and Subscribe buttons, and description text (BBCode-to-HTML converted server-side).

It is the detail pane subscriber's applier: `_detailSubscriber` merges each dispatched block into the item it holds and then calls this, so the pane redraws because it subscribed to the `workshop_id` rather than because a caller remembered it. The merge is what lets the summary block from `/api/items` refresh the marker without dropping the description the full payload brought.

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

### The creator-ignore toggle

The author box carries an **Ignore creator / Un-ignore creator** button
(`#btn-ignore-creator`). It sits at the far end of the bar from `Return`, with the author label and
name between them, so a press meant for `Return` cannot land on it by mistake. It acts on the creator
the view is pinned to (`_authorCreator`, set by the same `_setAuthorModeUi` call that names the
creator on screen), **not** on the selected item: the mode is one creator's whole catalogue and the
selection may be nothing at all, because an ignored view comes back empty.

Flagging a creator makes every one of their items ignored (`fetch_status = -2`), and un-flagging
restores them; new items from that creator are settled as they are scraped. It is a two-way toggle
with no provenance, so un-flagging also restores items that were ignored individually, and a dead
item (`-1`) is untouched in both directions — see [data-model.md](data-model.md#creators). The
direction and the wording are **not** this page's: the button calls
`POST /api/creator/<steamid>/ignore`, whose answer carries `ignored` and the `label` built from
`creator_ignore_label` in `src/database.py`, and the TUI action uses the same
`toggle_creator_ignored`. The label always names the next press, so a second press cannot disagree
with the first. Entering the mode reads the creator's stored flag once through the same route's GET
so the button is labelled correctly before any press.

Unlike the item `i` toggle, which patches the one row in place, this is a **whole-view** change:
every item of the creator settles at once. `toggleCreatorIgnored` therefore re-runs the author search
after the write, so the items that just settled leave the view and an un-ignore brings them back. The
author bar itself stays; only the results are re-read.

### The creator list

`#btn-authors` in the header opens a picker (`#author-modal`, `#author-list`), which
`GET /api/authors` fills. Picking a creator closes the picker and enters author mode through the same
`jumpToAuthor` the item jump uses, so there is one mode and one entry into it, not a second
implementation.

The route existed with no client before this. The item jump only reaches a creator whose item is
already on screen; the list is the way to reach one that is not, and it needs only the IDs
`/api/authors` already returns (`get_all_creator_ids`, `ORDER BY creator_steamid`) — no new endpoint and no new
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
too, from the same `/api/queued` payload. It has six states, resolved by
`subscription.subscription_state(item)` and rendered from the one table in `src/subscription.py`
(which the TUI reads too — see [tui.md](tui.md)):

| State | Glyph | Colour | Meaning | Click |
|---|---|---|---|---|
| `queued_remove` | ☆ | red | the owner is subscribed and queued for removal | un-queues |
| `downloaded` | ★ | deep green | the owner is subscribed and Steam has the item on disk | nothing |
| `subscribed` | ★ | solid yellow | the owner is subscribed now | queues a removal |
| `queued` | ☆ | green | queued to subscribe | un-queues |
| `previously` | ☆ | yellow | we have seen the owner subscribed, and they are not now | queues |
| `never` | ○ | gray | never seen subscribed | queues |

The queue flag carries no direction of its own: `queued_remove` is **derived**, because a queued
row that is subscribed is a removal and a queued row that is not is an addition. That keeps every
existing queued row meaning what it always meant and needs no schema change. `queued_remove`
outranks `downloaded` and `subscribed`, because the pending removal must stay visible over the
subscription it is about.

`downloaded` requires **both** `own_subscribed` and the local `steam_download_seen_at` latch, so a timestamp
left behind by a cleared subscription cannot claim the green star. The latch is written only by
`src/workshop_folders` (a periodic scan that finds the item's folder on disk) and cleared only when
the item leaves the owner's subscription list — so an unplugged drive or a moved library never takes
the green away. See [data-pipeline.md](data-pipeline.md) and [data-model.md](data-model.md).

There is exactly one such indicator: the old `queued` CSS class and its `★` prefix on `.grid-title`
are gone, and the pane's `Queue` / `Unqueue` button pair is replaced by the marker itself. The
marker's glyph, colour, CSS class, label, tooltip and clickability all arrive on the payload,
computed by the server from the shared table, so the page holds no copy of the state vocabulary.

**The pane's one subscription control is worded by direction too.** `subscriptionControl(item)`
renders a single button (`#btn-subscription`) whose label and action both come from the item's derived
direction, carried on the payload as `subscription_action` / `subscription_action_label` by the same
shared table (`SUBSCRIPTION_ACTIONS` in `src/subscription.py`) — so no state can show a **Subscribe**
button whose press removes:

* `never`, `previously`, `queued` → **Subscribe**, pressing `doSubscribe` (the direct subscribe);
* `subscribed`, `downloaded` → **Unsubscribe**, pressing `toggleDetailQueue`, which only *queues* the
  removal. The button never unsubscribes directly: the owner's removals are deliberate and
  recoverable, and the queue is where they are applied;
* `queued_remove` → **Cancel Unsubscribe**, pressing `toggleDetailQueue`, which cancels the queued
  removal.

That is why a subscribed item's press costs no Steam request — it queues, and the marker shows the
pending red star until the queue is run. A payload that predates the change falls back to
**Subscribe**, which is the addition direction a queued row meant before the feature.

Clicking the marker calls `toggleDetailQueue(wid)`. `subscribed` used to send nothing — "the only
action available there would be an unsubscribe, and an accidental unsubscribe is not wanted" — but
that inertness was deliberately reversed: its click now only queues a removal, and the queue
cancels, so an accidental click is recoverable. The new `queued_remove` state is clickable for the
same reason: a second click cancels the queued removal. `downloaded` stays inert — its action is
the separate open-folder button and key. `toggleDetailQueue` POSTs the existing
`/api/toggle_subscription_queue/<id>` route, which flips the database flag and answers only
`{ok: true}`. Since the route does not report which way the flag moved, the client reads the item
back through the read-only `/api/item/<id>` route and re-renders the pane and the matching cell's
marker (`_applySub`) from that payload — the same path the `s` shortcut takes. A read-back rather
than a locally flipped guess is deliberate: the `s` shortcut and the drain's own recording change
the same flag behind the pane's back, so a guess could show the wrong state. Rendering from the
item payload is also what lets the 3-second translation poll re-render the pane without reverting
the toggle. A failed request alerts and leaves the pane alone.

The click is not the only writer. A subscription or a removal can land behind a rendered cell through
`POST /api/subscribed/<id>` or `POST /api/unsubscribed/<id>` (the outcome stamps), through this page's
cancel/clear `POST /api/dequeue/<id>` calls, or through the direct `POST /api/subscribe/<id>` route,
and none of those touches the DOM. The item-update registry is what re-reads such a cell:
`_startListPoll` keeps re-reading any row whose subscription marker is still `queued` or
`queued_remove` at its fastest, and `_startItemUpdatePoll` re-reads every displayed row on its
3-second cadence whether or not anything is pending, so the marker moves on its own without a new
search ([One item-update path](#one-item-update-path)).

A cell already at `subscribed` is re-read too, for the same reason: the folder scan later stamps
`steam_download_seen_at` behind the page's back, and the cell must move to `downloaded` without the user
acting. The cost is bounded by the registry, not by the table — one batched `/api/items` read of the
rows on screen per tick — which is what makes polling a settled view acceptable now that the whole
invariant rests on it.

The marker sits inside the cell that opens the detail pane, so its click handler stops propagation:
without that, toggling the queue would also drag the pane to the item.

### Ignoring an item (`i`)

The `i` key, with a `.grid-cell` focused, toggles the owner's ignored marker on that cell's item
through `POST /api/ignore/<id>`; it mirrors `s`, acting on the same focused cell's `data-wid` and
calling `preventDefault()`. The same key restores an item that is already ignored. Where `s` leaves
focus alone, `i` then advances the way the arrow keys do — through the shared `_focusGridCell`
helper, to the next cell, or to the previous one when the focused cell is the last.

**The row is not removed.** Ignoring does not re-query the list, so a search that already returned
the row keeps it on screen until the next search hides it; the toggle has to show what it did to the
live session. The `ignored` class is set on the cell from the status the read-back reports, and
`.grid-cell.ignored .grid-title` renders the title with `text-decoration: line-through` — the web
counterpart of the TUI's underline. The class is keyed off `fetch_status == -2`, not off the
keystroke that set it, so the same path draws a row the 3-second item-update poll reports as
ignored, whether the owner, another front end or a `POST /api/ignore/<id>` settled it. A block that
carries no `fetch_status` makes no claim and leaves the class as it is.

The Web UI's subscribe action runs entirely through the server. `doSubscribe` POSTs
`/api/subscribe/<id>`, which reads the item page on the server, takes that page's own CSRF token,
posts to Steam and records the answer — the route the TUI has always driven
([data-pipeline.md](data-pipeline.md#subscribe-engine-browser-free)). The browser bridge that used to
do this from a Steam tab is **removed**: the userscript, the `autosubscribe=true` tab flow,
`/api/sessionid` and the bridge's outcome reports are gone, and nothing in the page opens a tab or
looks for an extension any more. What the drain still reads (`/api/queued`,
`/api/subscribe_failures`, `/api/subscribe_throttle`) stays mounted because it is the browser-free
flow's own bookkeeping; see [the drain](#queued-row-timing-and-why-it-differs-from-the-tuis). The
removal is recorded in
[future-plans.md](future-plans.md#removing-the-browser-bridge-from-the-subscribe-path).

`previously` can only ever mean "we have **seen** this account subscribed". Steam exposes no
per-account subscription history — `lifetime_subscriptions` is an item-wide count and
`EnumerateUserSubscribedFiles` is publisher-key-only — so on the day the marker shipped there were
zero `previously` markers regardless of real history, and they fill in over time. The marker's
tooltip says this rather than implying a complete record.

### Opening the downloaded item's folder (Windows only)

When the pane's item is in the `downloaded` state, `Open Folder` in `#detail-buttons` opens the
item's workshop folder — and the `o` key does the same for the focused grid cell, beside the `s`, `l`
and `i` shortcuts. The button is **visible but disabled** for anything not downloaded, with the reason in
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
way. A pending translation is noted above the description in its own `.translation-notice`
paragraph while `translation_priority > 0` and no translation has been stored yet, matching the
TUI's notice. The sentence is `pending.TRANSLATION_REQUESTED_NOTICE` in `src/pending.py`, rendered
into the page as the `TRANSLATION_NOTICE` constant rather than retyped in the template, so the two
detail panes print the same words.

---

## Maintenance Actions

The row under the detail pane (`#detail-buttons`) holds the queue and database actions: **Fetch New**, **Update Visible**, and **Delete Never Fetched**.

**Delete Never Fetched** (`#btn-delete-never-fetched`, `doDeleteNeverFetched`) mirrors the TUI's command-palette action. `confirm()` names the exact set before anything is sent — items with no status or a 404 status whose API data was never fetched — because the delete is destructive and irreversible; declining sends no request at all. On a 2xx it reports the count returned by the route and re-runs the search, on a rejected response it shows the status, and on a dead backend it shows the error, so a failed clear is never presented as a successful one.

---

## Daemon Panel

The header toolbar's **Daemon** button (`#btn-daemon`) opens `#daemon-modal`, a modal panel with the running status and PID, Start / Stop / Restart buttons, a log size readout with a **Rotate Log** button, and a live log view (`#daemon-log`).

While the panel is open, `_refreshDaemonStatus` polls `/api/daemon` and `_pollDaemonLog` polls `/api/daemon/log?since_offset=<byte-offset>` every 2 seconds. The offset is the byte position returned by the previous response, so each poll transfers only new lines. The view is a bounded preview: the server reads at most 64 KiB and returns at most 500 lines, so a first poll against a large log shows its tail rather than the whole file. A `reset: true` response means the returned lines do not continue the caller's view — a first call that had to seek to the tail, a rotation or truncation, or the client having fallen more than one window behind — and `_pollDaemonLog` clears the pane before showing them, so a gap is never rendered as if it were continuous. `_closeDaemonPanel` clears the interval, so nothing polls while the panel is hidden.

The same `/api/daemon` poll fills `#daemon-log-size`, `#daemon-log-message` and the state of `#daemon-rotate`: the readout line, the last rotation's outcome and the button's enabled state are the server's own strings and flags — the values the TUI's daemon page draws — so the two front ends cannot disagree on the size or the wording. The button is rendered with the TUI's `ROTATE_BUTTON_LABEL` constant rather than retyped in the template.

**Rotation is manual only** — nothing rotates on a timer, on a size threshold, or at startup. Pressing **Rotate Log** posts `/api/daemon/rotate`; the route renames the live log to `logs/<stem>-<UTC stamp>.log.gz` beside it and leaves a fresh empty file before it answers, so the next poll's readout already reads `Rotating… (<size>)` while the gzip runs on a background thread in the server process. When it finishes, `rotation_message` carries `Rotated: logs/<name> (<size>)` and the button comes back; a refusal (nothing to rotate, one already in progress) or a filesystem fault is shown in the same line and appended to the log pane. **A `tail -f` on the log follows the old descriptor and will appear to stop; a follow-by-name tail picks the new file up** — the rotation is manual precisely because only the operator can accept that. The mechanism that makes the daemon and the TUI reopen the fresh file is in [config-security.md](config-security.md#manual-rotation-and-why-it-is-manual). Rotation awareness never blocks logging: a marker read that fails for any reason warns and degrades to no rotation awareness, and a rotation-aware handler that cannot be built falls back to a plain `logging.FileHandler`, so a front end still logs.

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

* **coverage** — seven bars (API Data, Translations, Extended Web, Extended Web Translation, Images, Creator, Creator Translation), at two scopes: the whole live library (dead items excluded), and the items the target AppIDs' stored `enrichment_filters` select — what the owner cares about. A bar is a **track with two fills**, because `<progress>` cannot show two segments: the coloured fill is the share done, a gray fill is the slots that need no translation at all, and the track shows through for the work still to do. Each bar's population is the flagging rule, mirrored in SQL, and its track is 100% of the unit its stage works in: **Translations** is per field (`title`, `short_description`) over the filter-selected items, so its track is two translation slots per entry — 100% is two translations per entry — because `_queue_translations` returns early unless the item was enriched; **Extended Web** is the scrape's coverage and its maximum excludes the pages that answered with no description, whose count and ceiling are printed with the bar (that bar is not a translation bar and keeps its sentence); **Extended Web Translation** hangs off it and can never be longer than it, its population is any scraped item, not only the filter-selected ones, and its track is one slot per described item; **Creator** and **Creator Translation** are in **author units** — the scope's unique authors, not the items they made. The three translation bars carry the metric's own short note (`need / slots`, for example "25% need translation"), drawn at half the standard track's CSS height and sitting flush under their parent — no margin, padding or row between them — with the same left edge, so a shorter bar still means less coverage. A bar whose population is zero reads "Nothing to translate" rather than a stuck 0.0%, with no note and no gray segment. The second figure is the search builder's SQL translation of the filters, which also searches each text field's `_en` counterpart, so it can disagree with the daemon's in-memory per-item check; where they disagree, the search builder's answer is shown. With more than one target AppID the population is the union of what any target's filters select. A scope note names the AppIDs and says why the two figures coincide when a filter set is empty, unreadable, or has no fixed predicate (a percentile); an unreadable set means no exclusion, never "excludes everything". The labels, counts, percentages, gray shares and notes come from the metric, so the two front ends make the same claim about the same data in the same words.
* **item_counts** — alive, dead and ignored counts with the overall total. Alive is the live population, so the two settled statuses (`-1` dead, `-2` ignored) are both excluded from it and each reported beside the other, and the three counts account for every row.
* **dead_items_by_queue** — flagged in red when non-zero: the number of dead items still sitting in a queue, broken down per queue (the `translation` column counts a `translation_queue` row as well as the `translation_priority` mirror). Beneath the breakdown it prints the shared `metrics.DEAD_QUEUED_MEANING`, injected exactly as the TUI prints it: the API fetch poll excludes dead rows, so that queue still drains, while the web, image and translation polls select on their flag alone and would keep spending requests on a page that no longer exists. A zero value renders as an all-clear.
* **dead_queued**, **queued_nowhere** — the two handoff-invariant counters, rendered like `dead_items_by_queue`: red with the count when non-zero, a green all-clear sentence at the healthy zero. `dead_queued` counts dead items still sitting in a work queue — a flag left set, or a `translation_queue` row the poll still holds; `queued_nowhere` counts live items in no queue that the pipeline never completed, where a `translation_queue` row counts as being queued. `dead_queued` and `dead_items_by_queue` are one question at two resolutions — the scalar invariant that must read zero, and the per-queue breakdown that says where to look, which makes the same translation test — so both are kept, and they agree on every dead item.
* **priority_breakdowns** — one "queue: N waiting" block per queue with the priority mix.
* **translation_status**, **status_counts** — labelled count lists.
* **fetch_recency** — fresh / stale / never-attempted counts, each labelled with the window the metric itself returned (`window_days`, the configured `daemon.item_staleness_days`; the server passes that key and the metric returns the window it used, so the panel cannot name a different one). Below the list it prints the shared caveat, injected from `metrics.FETCH_RECENCY_MEANING` exactly as the TUI prints it: the figure is the age of **our last attempt**, not the fetch queue, it includes settled rows (dead and legacy `404`s) that will never be re-fetched, and a stale row may simply not be due yet at the configured threshold. [data-pipeline.md](data-pipeline.md#the-fetch-recency-figure-is-not-a-backlog) owns the detail.
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

### Browser-free subscribe

The Web UI subscribes and unsubscribes through the server. `doSubscribe` POSTs
`/api/subscribe/<id>`, and the queue drain (`_startAutoSubscribe`) calls the same route once per
queued item; the route derives the direction from the row, so the same call adds or removes. There is
no userscript, no Tampermonkey bridge and no Steam tab: the page holds no session material, and the
route builds its request from the shared helpers in `src/subscribe_engine.py`. The userscript, its
`autosubscribe=true` tab flow, the `/api/sessionid` token push, the `/userscript/<file>` install
endpoint and the `userscript-version` meta tag were removed once the route had been proven in use.
The removal is recorded in
[future-plans.md](future-plans.md#removing-the-browser-bridge-from-the-subscribe-path).

### Subscribe Flow

1. User clicks Subscribe on the web UI → `doSubscribe` POSTs `/api/subscribe/<workshop_id>`
2. Server reads the row's derived direction: queued + subscribed is a removal (it runs through `subscribe_engine.subscribe_item`, pre-read guard included), and every other queued row is an addition
3. Server reads `steamLoginSecure` (from config `session.login_secure`, which can be a YAML list joined with `%7C%7C`) and the item page it fetches on the shared web interval; the CSRF token is that page's own `g_sessionID`, with a `sessionid` in the cookie set and config `session.csrf_token` as fallbacks
4. Server POSTs to `steamcommunity.com/sharedfiles/subscribe`, or to `steamcommunity.com/sharedfiles/unsubscribe` for a removal (the same form minus `include_dependencies`, verified from Steam's own page script), with browser-like headers (User-Agent, Origin, Referer with workshop URL) and cookies
5. Steam's answer is mapped to user-facing messages: a refusal (`success: 2`/`15`, or HTTP 401) is a stale CSRF token when the same attempt's page read was authenticated — the login is not reported as expired — and a session problem only when that read was anonymous. For a removal, Steam publishes no failure codes, so a non-`1` answer is reported with the raw value and leaves the item queued
6. On `success: 1` the route records the outcome — `own_subscribed` set and the queue flag cleared for an addition, `own_subscribed` and the queue flag cleared with the sticky stamp left for a removal; the page then re-reads `/api/item/<id>` and re-renders the pane and the matching cell's marker from that payload, the same read-back a queue toggle uses

The button sends no tab and needs no userscript; a refusal shows the route's own message rather than
falling back to a tab.

**The overlay's rows name the direction.** `_startAutoSubscribe` reads `subscription_state` from each
`/api/queued` row and renders `subscribing…` or `unsubscribing…` in a `sub-queue-verb` span, with the
matching `unsubscribed` / `subscribed` / `failed` written when the call settles (the same word the
poll writes when a row clears behind the loop's back). The row's marker is the shared one, so a queued
removal draws the red empty star. Cancel and Clear Failed post `/api/dequeue/<id>`, which clears the
flag without recording an outcome in either direction.

**Throttling.** Steam answers an over-budget request with **HTTP 200** and its ordinary page shell
carrying "too many requests", so the subscribe button is simply absent. The engine's page read
recognises that wording, returns a `throttled` outcome and leaves the item **queued** for the next
drain, and its shared `WebInterval` doubles the delay so the next request is paced further apart. The
drain checks `/api/subscribe_throttle` before each item and stops the pass if a throttle is recorded,
telling the user when the rest can be retried; the bridge that used to write that state through
`/api/subscribe_throttled/<id>` is gone, so the read now reports the resting state unless something
records one. The budget is per account or address and refills over minutes.

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
exempt and a row's cost is one gated read — the same shape the TUI's queue has, now that the engine's
confirmation read is retired by default (`src/subscribe_engine.py`; `VERIFY_AFTER_SUBSCRIBE = True`
restores the second read). Nothing in the page spaces the requests: the route reads the configured
delay itself, fresh per call, so a throttle's doubling mid-pass moves the pacing without the page
knowing, and a second client-side delay would pay the interval twice per item.

The web delay is not injected into the page at all: the tab flow was its only consumer, so
`WEB_DELAY` went with the bridge rather than staying behind as a number nothing reads, and the delay
itself now lives in the daemon state file rather than in `config.yaml`.
The TUI screen's own description is in [tui.md](tui.md#subscription-queue-sl-keys).

---

## Reading a UI trace

The page's JavaScript is otherwise only observable through tests that drive
extracted functions, and the person diagnosing a web bug cannot open a browser.
`daemon.capture_web_ui_trace` turns the page into its own recorder: while the
debug switch is on the server injects `UI_TRACE_ENABLED = true`, the page
installs its instrument once and posts ordered batches to `/api/ui_trace`, and
the server writes one JSON file per batch under `<outbox_dir>/web_ui_trace/`.
With the switch off the page installs nothing at all. The switch, the bounds and
the tree's retention are in
[failure-capture.md](failure-capture.md#web-ui-trace) and
[config-security.md](config-security.md#daemon).

A file is one batch. Its envelope names the `session`, the `batch` (sequence)
number, whether the page marked it `page_final`, the `app_version`, and the
truncation fields: `events_dropped`, `events_truncated`, `bytes_truncated`,
`buffer_dropped` (what the page's ring buffer discarded) and
`truncation_reason` (`event_cap`, `byte_cap`, `session_file_cap` or
`page_ring_buffer`). `events` is the ordered timeline. Every event carries a
monotonic `t` (milliseconds since the page installed the trace) and
`loads_since_scroll`.

| `event` | Fields, and what they mean |
|---|---|
| `session` | `loaded_at`, `inner_width`/`inner_height`, `grid_client_height`/`grid_scroll_top`, `app_version` — the header that makes a trace read later interpretable |
| `keydown` | `key`, `id`/`wid` of the focused element, `consumed` (whether a handler called `preventDefault`) |
| `click` | `id`, `wid`, `label` of the clicked control |
| `scroll` | `scroll_top`, `scroll_height`, `client_height`, throttled to one record per settle window |
| `sort_change`, `overlay_change` | `id` and the new `value` |
| `do_search` | `phase` is `enter`, `dropped` or `done`; entry holds `reset`, `offset`, `filters` (count), `sort_by`, `sort_order`, `overlay`; `dropped` names the `loading` guard that swallowed it; `done` holds `batch`, `offset`, `has_more`, `loading` |
| `fetch` | `method`, `path`, `body` (a summary: `{kind: "search", filters, subscribed, sort_by, offset}`, `{kind: "ids", ids}`, or a key list — never the whole payload), `ms`, `status`, and `items` (the response array's length) where cheap |
| `observe_next_batch` | `branch` (`already_visible`, `observe`, `skip` or `reset`), `rect_top` beside `inner_height`, `scroll_top`/`client_height`, and the cell `released`/`armed` |
| `intersection` | `is_intersecting`, the entry's `target`, whether it `matched_observed` (`_observedCell`), and `entry_count` |
| `list_poll`, `item_poll` | each list-poll arming and tick, and each item-update tick, so an endless poll cannot hide among the calls |
| `jump_to_author` | the `creator` the jump pinned the view to |
| `pane_open` | the `wid` the detail pane was opened on |
| `view_restore` | `phase` is `saved_view` (`found`), `load_state`, `enter` (the saved view's `scroll`, `selected`), `load_until_enter`, `done_check` (the value the `done` predicate returned, and the pass number), `load_until_done`, `load_state_done` |

**The question it was built to answer**: "is the page loading on its own, or is
the user scrolling?" Every record carries `loads_since_scroll`; the page resets
it on a user scroll. So a rising `loads_since_scroll` across `do_search` or
`fetch` records with **no `scroll` event between them** is the runaway in one
line, and the `view_restore` events show the loop that runs on every page load
with no user action — each `done_check` value and every batch it requests —
rather than leaving those searches to look like they arrived from nowhere.

The trace is additive: a trace POST that fails, throws or is refused leaves the
action it describes behaving exactly as it did, and a refused POST is only how
the page learns to stop buffering at the session cap.

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

Bulk ID lookup. Accepts `{ids: [1, 2, 3]}`. Returns the same summary fields as `/api/search` plus the derived subscription marker. Read by `_startListPoll` and by the general `_startItemUpdatePoll` for exactly the ids on screen, so one request answers every display of them.

Both list routes attach the image classification the grid branches on before serialising: `image_state`
(from `images.image_state`) and `image_resolved`, which is `images.is_resolved(stored)` itself rather
than the server spelling out which states are settled. `src/images.py` is the one decider, so a change
to the predicate moves the page and the TUI together instead of leaving the page quietly disagreeing.

### `/api/ui_trace` — POST

Accepts one batch of the page's own trace: `{session, seq, final, reason, dropped, events}`. The
route is **inert unless `daemon.capture_web_ui_trace` is on** — with the switch off it answers 404
before touching anything, so the page cannot write a trace the operator did not ask for. With it on,
`capture.record_ui_trace` bounds the batch (events per batch, bytes per record, files per session),
writes it atomically into `<outbox_dir>/web_ui_trace/` and registers it with `kind: "ui_trace"`. A
session that has reached its file cap answers 429 with `refused: "session_cap"`, and the page treats
any non-2xx answer as "stop buffering" — so a refused trace stops the instrument, never the action it
was describing. See [Reading a UI trace](#reading-a-ui-trace).

### `/api/search_diagnostic` — GET

Read-only report on the live database's search-sort path, for answering a "sort X is slow" report with a measurement instead of a hypothesis (it opened on the owner's "subscriber score is not indexed"). The route is **inert unless `daemon.capture_web_ui_trace` is on** — it rides the UI-trace switch rather than adding a third capture key — and with the switch off it answers 404 before opening the database. With it on, `src/sort_diagnostic.py` opens the file `mode=ro` (no write, no journal-mode switch, no file creation, safe beside the running daemon) and returns `{rows, indexes: {expected, missing}, score_coverage, sqlite_stat1, plans, timings}`: which `QUERY_INDEXES` indexes are present and which are missing, `EXPLAIN QUERY PLAN` for the real summary query under each `VALID_SORT_COLS` column, the non-NULL fraction of each score column, whether `sqlite_stat1` exists and its rows for those indexes, and the wall-clock first-page and deep-page (offset 50,000) time per sort column. `init_webserver` logs the same summary once at startup, naming a missing index or a temp B-tree plan at WARNING. It writes no tree of its own; the measurements behind it are in [search-filter.md](search-filter.md#sort-indexes-the-subscriber-score-is-slow-investigation).

### `/api/cutoffs` — POST

Wilson score percentile thresholds. Accepts `{filters, subscribed}` (filters excluding percentile filters). The overlay is included so the percentiles describe the same population the grid shows. Returns `{wilson_favorite_p99, wilson_favorite_p90, ...}`.

### `/api/state` — GET

Reads `.tui_state.yaml` for filter/sort state restoration. The client uses this as the **first-visit seed only**: once the browser has its own `view.state.v1` entry, this route is not called at all. The seed includes the TUI's `subscribed_overlay` value as well as `filters`, `sort_by` and `sort_order`.

### `/api/delete_never_fetched_items` — POST

Deletes every pending item — those with no status or a 404 status and no successful API fetch (`delete_never_fetched_items`, `src/database.py:3427`) — and returns `{ok, deleted}` with the number of rows removed. This is the same predicate and the same delete as the TUI's `action_delete_never_fetched_items`; there is deliberately no dry-run mode. The UI asks for confirmation first, naming what will be deleted.

### `/api/save_filter` — POST

Saves the current enrichment filters to `app_discovery` for the configured AppID. The body is `getFilters()` — the builder's rows only. The `Subscribed:` overlay is view state and is deliberately not written here, so what the scraper enriches with stays the set the builder shows. When no target AppID is configured it answers **400** with `{"error": "No target AppID configured"}`; the client shows that message and only reports success on a 2xx, so a rejected save is never presented as a stored one.

### `/api/subscribe/<id>` — POST

Performs the subscribe against Steam directly, with no browser tab, and returns Steam's JSON body unchanged — the TUI reads `success` and `message` from it. **The direction is derived from the row**, exactly as the engine derives it (`queued_direction`): a queued row that is subscribed is a **removal**, and every other queued row is an addition. The addition branch below is unchanged. A removal is delegated whole to `subscribe_engine.subscribe_item`, which owns the pre-read guard (it posts to `/sharedfiles/unsubscribe` only when the page still shows the item subscribed, and a page that shows it unsubscribed settles as `already_unsubscribed` with no request), the throttle and session-health handling, and the `mark_own_unsubscribed` record; the route answers `{"success": 1, "status": ...}` when the removal settled and `{"success": -1, "status", "message", "stays_queued"}` otherwise, so the drain's one `success` check keeps working. The request shares the scraper's session and presents the project's own Firefox User-Agent. The cookies come from one `web_scraper._build_workshop_cookies` read: the signed-in Firefox profile's whole `steamcommunity.com` set when `session.read_firefox_cookies` is on, otherwise the configured `sessionid`/`login_secure` pair. **The CSRF token does not come from that read**, because `sessionid` is a session cookie Firefox keeps in memory and never writes to `cookies.sqlite`; it comes from the item page this route reads for the attempt (`g_sessionID`, the token belonging to the session that served that page), put back into the cookie jar so the form field and the cookie agree. A `sessionid` already in the set and `session.csrf_token` are fallbacks only for a page that carries no token; the in-memory token the old userscript pushed through `/api/sessionid` went with the bridge. That read is a page load, so it is gated on the shared web interval — the persisted `web_delay` state section read through `configured_web_delay` and `pacing.wait` — exactly like the engine's reads.

It refuses before spending a request when the set has no `steamLoginSecure` (**400**, with a message naming the remedy: sign in to Steam in the browser the daemon reads cookies from, or configure `session.login_secure`), and when `session_health.evaluate_login` says the credential's own token has expired (**400**, the reason recorded through `session_health.record_rejected` so the [session warning](#the-session-warning) shows it). A Steam `success` of `2` or `15`, or an **HTTP 401**, is a refusal of the CSRF token, and what it means depends on the page read the same attempt made: beside an **authenticated** page read the credential is proven good, so **no session problem is recorded** and the refusal is logged as a token refusal (recording one is what used to tell the owner to sign in again while the login was working); beside an **anonymous** page read it is recorded as a session problem the same way as before. The response body is passed through untouched either way. A `success` of `1` records the confirmation with `mark_own_subscribed`, setting `own_subscribed` and clearing `is_queued_for_subscription`, and clears any recorded session problem. A missing item still answers **404** `Item not found.`, an item with no AppID still **400** `Item has no AppID.`, and a transport failure still **502**. Each of those refusals logs the `workshop_id` and the reason, and the POST line carries a SHA-256 **fingerprint** of the token — never the token itself.

### `/api/subscribed/<id>` and `/api/unsubscribed/<id>` — POST

The two **outcome stamps**. `mark_own_subscribed` sets `own_subscribed`, stamps the sticky `own_first_subscribed_at` if it is still NULL and clears `is_queued_for_subscription`; `mark_own_unsubscribed` is its removal mirror — it clears `own_subscribed` and the queue flag and deliberately leaves the sticky first-seen stamp, so the marker reads `previously` rather than losing the only evidence of the subscription. The direct `/api/subscribe/<id>` route records the same facts from Steam's own answer (through the engine), so these routes are the explicit stamp for anything else that has confirmed an outcome. They are **not** what Cancel and Clear Failed call — see `/api/dequeue/<id>`.

### `/api/dequeue/<id>` — POST

Drops one queued row **without recording an outcome**, in either direction: `dequeue_subscription` clears only `is_queued_for_subscription` and leaves `own_subscribed`, the sticky `own_first_subscribed_at` and the download latch untouched. It is what the overlay's Cancel and Clear Failed post for the rows the drain leaves behind. The route they used before was `/api/subscribed`, which claimed a subscription — and stamped the sticky first-seen time — for rows that were never attempted, so a cancelled pass marked items subscribed and made them read `previously` for good. A cancellation records nothing; only an outcome does.

### `/api/toggle_subscription_queue/<id>` — POST

Flips `is_queued_for_subscription` for one item and answers `{ok: true}`. It is the route behind both the `s` shortcut on a grid cell and the detail pane's Queue/Unqueue button. It returns no new state, so the detail pane reads the item back through the read-only `/api/item/<id>` route to label its button.

### `/api/ignore/<id>` — POST

Toggles the owner's ignored marker (`fetch_status = -2`) for one item and answers `{ok: true}`, the
same shape as `/api/toggle_subscription_queue/<id>` so the page treats the two uniformly. It is the
route behind the `i` shortcut on a grid cell. The direction is not passed in: `toggle_ignored_item`
owns the rule, sending an ignored row through `unignore_item` and every other row through
`ignore_item` (which settles the item exactly as death does — all four queue priorities cleared and
its `translation_queue` rows deleted). Like the queue route, it returns no new state, so the client
reads the item back through the read-only `/api/item/<id>` route and draws the marker from that
payload rather than from a local flip.

### `/api/creator/<steamid>/ignore` — GET and POST

The creator-scoped counterpart of `/api/ignore/<id>`. **GET** answers the creator's current state and
the wording its control should carry — `{"ignored": bool, "label": ...}` — so entering author mode can
label the button before it is pressed. **POST** toggles the flag through `toggle_creator_ignored` and
answers `{"ok": true, "ignored": bool, "label": ...}`; `ignored` is the state *after* the call, so a
second press reports the reverse. The label is built by `creator_ignore_label`, one source shared
with the TUI, so the two front ends use the same words for the same direction.

The path names the **creator**, not an item: the route acts on the creator the author view is pinned
to. `ignore_creator` settles every non-dead item of theirs (`-2`, all four priorities zero, its
`translation_queue` rows deleted) and sets the creator's flag; `unignore_creator` clears the flag and
restores their `-2` items by `unignore_item`'s rule. A dead item is untouched in both directions.
Because the write moves a whole catalogue, the page re-runs the author search after it rather than
patching one row — see [The creator-ignore toggle](#the-creator-ignore-toggle).

### `/api/open_folder/<id>` — POST

Windows only. Opens the item's downloaded workshop folder in Explorer **on the host running the server** (the browser's own machine is not involved), through the shared `src.workshop_folders.open`. It refuses with **400** `{ok: false, message}` when the platform is not Windows, when the item is not in the `downloaded` state (`own_subscribed` and `steam_download_seen_at` both set), and when the folder is not on disk at click time — naming the folders it looked in, and changing nothing. A success is **200** `{ok: true, folder, message}`. The route is not rendered into the page off Windows, and no state is written or cleared either way.

### Bridge-only endpoints — removed

`POST /api/sessionid` (the userscript's token/login push), `POST /api/subscribe_failed/<id>` and
`POST /api/subscribe_throttled/<id>` (its outcome reports), and the `/userscript/<file>` dynamic
install endpoint are gone with the userscript. `GET /api/subscribe_failures` and
`GET /api/subscribe_throttle` **stay**, because the browser-free drain reads them before and during a
pass; with their only writer removed they report the resting state (an empty list, and
`throttled_at: 0`). The outcome stamps `POST /api/subscribed/<id>` and
`POST /api/unsubscribed/<id>` also stay, and `POST /api/dequeue/<id>` is the page's own Cancel and
Clear Failed call — it clears the queue flag without recording an outcome, where the old
`/api/subscribed` call wrongly claimed a subscription.

### `/api/session` — GET

Whether the daemon's Steam login is still working, for the session warning banner: `{problem, detail, detected_at, login_url}`. `detail` is the sentence the daemon recorded and `login_url` is where an operator signs in again, so neither is hard-coded in the template. `problem` is false when nothing has been recorded — including when the recorded section is present but carries no sentence, since an unexplained warning is worse than none. One small YAML file read, because the banner polls it.

### `/api/session/recheck` — POST

Re-reads `steamLoginSecure` from the browser's cookie store after the operator signs in, using the same lookup the daemon prefers. A cookie that is not already expired from its own token is saved to `config.yaml` — which the daemon re-reads per request and per batch, so nothing needs restarting — and the warning is cleared. The judgement is local: proving the cookie by spending a request would duplicate what the next scrape is about to do anyway, and the daemon's answer corrects the banner if Steam still refuses. When the browser has nothing newer, the route answers `{ok: false, problem: true, detail}` with the reason (which may be the configured cookie's expiry, or that there is no cookie at all), and a cookie found but not writable answers **500** with the failure named, so a save that did not happen is never reported as a cleared warning.

### `/api/stats`, `/api/tags`, `/api/authors`

Read-only endpoints returning database statistics. `/api/stats` still returns the old flat payload; the statistics panel uses the per-metric endpoints below instead. `/api/authors` returns the distinct creator IDs in `ORDER BY creator_steamid` and is consumed by the creator picker ([The creator list](#the-creator-list)).

### `/api/analysis` — GET

Age-bucketed view statistics, consumed by the view window analysis panel (`api_analysis`, `src/webserver.py:540`). Accepts `bucket_days=N` (default 7) and returns `{buckets: [{age_start, age_end, count, median, p10, p90}], estimated_window_days, items_analyzed}`. The bucket width is floored at one day, because the parameter comes straight from the query string and a zero width would divide by zero (the TUI clamps its input the same way). `estimated_window_days` is the knee — the first bucket whose median drops below a quarter of the early-bucket peak — and is `null` when there is too little data to find one, a distinction the panel renders honestly rather than collapsing to zero.

### `/api/metrics` — GET

The metric catalogue: `{"metrics": [{name, note, seed_ms}], "default_order": [names]}`, where `default_order` and `metrics` are both in seed order (cheapest seed hint first). The panel draws its layout from this rather than hard-coding metric names, so a metric added on the server appears without a client change (`src/webserver.py:500`). `seed_ms` is only a first-open ordering hint; the client replaces it with its own measurements.

### `/api/metrics/<name>` — GET

One metric, computed on its own: `{name, value, ms, note, seed_ms}`, where `ms` is what that metric actually cost. An unknown name returns a 404 with `{error}` (`src/webserver.py:517`). Separate requests are what make the chunks independent: whichever finishes first renders first, and a slow metric cannot hold up a fast one.

### `/api/daemon` — GET

Daemon status: `{running, pid, log_file}`, where `log_file` is the configured `logging.file` path or null. The same response carries the log panel's other fields, from `DaemonController.log_status`: `log_size` (bytes or null), `log_readout` (the exact line the readout shows, e.g. `Log size: 594.0 MB`), `can_rotate`, `rotating`, `rotation_ok` and `rotation_message` (the last rotation's outcome, or `""`). `log_readout` and `rotation_message` come from `src/log_rotation.py`, the same strings the TUI draws, so the two front ends cannot disagree on the size or the wording.

### `/api/daemon/start`, `/api/daemon/stop`, `/api/daemon/restart` — POST

Drive the background daemon through the shared `DaemonController`. Each returns `{ok, changed, message}`; starting a running daemon or stopping a stopped one is an idempotent no-op that still reports success. A start the daemon itself refuses — the runner creates the PID file exclusively, and an existing file blocks the start — comes back with `changed: false` and the refusal sentence naming the PID file and the PID inside it, so the panel reports the refusal rather than a start that did not happen. Same controller, same sentence as the TUI.

### `/api/daemon/rotate` — POST

Manual log rotation; nothing calls it on a timer or at startup. Renames the configured log to `logs/<stem>-<UTC stamp>.log.gz` beside it and leaves a fresh empty file **before answering**, then compresses the archive on a background thread, so the response is prompt whatever the file's size. Returns `{ok, started, archive, message}` (`200`) when a rotation began, and `{ok: false, started: false, message}` with **400** for a refusal or a fault (no log configured, the log missing, the log empty, one already in progress, or a filesystem error) — always that bounded JSON body, never a traceback and never the log's contents. The outcome of the background compression is read back from `/api/daemon`'s `rotation_message` and `log_readout` on the panel's next poll.

### `/api/daemon/log` — GET

Incremental log tail and bounded preview. Accepts `since_offset=<byte-offset>` and returns `{lines, offset, reset}`; the pre-rename `since` key is still accepted, because a page served before the rename may still be open in a browser. At most 64 KiB is read (`DaemonController.TAIL_BYTES`, `src/daemon_control.py:23`) and at most 500 lines are returned (`TAIL_LINES`, `src/daemon_control.py:24`), so a first call (`since_offset <= 0`) returns the tail of the file rather than the whole of it. `offset` is the byte position to pass as `since_offset` on the next poll. `reset` is true when the returned lines do not continue from `since_offset` — the first call against a file larger than the window, a rotation or truncation, or the caller having fallen more than `max_bytes` behind — so the client knows its view has a gap and starts over. A missing or unreadable log returns an empty list rather than an error.
