# Data Pipeline

The data pipeline moves a Steam Workshop item from initial discovery through enrichment stages to user-facing display. An item progresses through: discovery → API detail fetch → web scraping → image download → translation → search visibility.

---

## Discovery Phase

### `process_batch` (daemon)

The main loop entry point called repeatedly by `run()`. Before it takes any work it runs the daemon's
housekeeping — the staleness sweep (`_maybe_promote_stale_items`), the subscription reconcile
(`_maybe_reconcile_subscriptions`) and the downloaded-item folder scan
(`_maybe_scan_downloaded_items`), each guarded by its own clock — and all three run for certain on
the **first batch after a restart**, which is why a fresh daemon can be minutes away from its first API
fetch while its scraper, image and discovery threads are already working. Each invocation then:

1. Calls `get_next_items_to_fetch` to retrieve up to `api_batch_size` items due for processing. Selection is `api_priority > 0` and `fetch_status` not `-1`, ordered by `api_priority DESC, api_fetched_at ASC`, so never-successfully-fetched items (`api_fetched_at IS NULL`) come first within a priority band. The call takes `limit` alone: `item_staleness_days` is **not** a fetch argument. It is the window `_promote_stale_items` uses to put already-fetched items back in the queue, so it is applied by that sweep and not by this SELECT.
2. If no items are available, waits for the discovery thread to refill the queue. Discovery is no longer the main loop's job: it runs on its own thread (see [threading.md](threading.md)) so that the queue is refilled while the loop is still draining it, rather than only after it has drained. The wait is woken by the thread's signal instead of polling the database, and still gives up after ten minutes so the outer loop re-checks.
3. Fetches metadata for the whole batch in one Steam Web API request via `get_workshop_details_batch` (title, description, tags, file_size, preview_url, creator, subscriptions, etc.), then processes the items **in the order the queue returned them**, matching each to its result by `publishedfileid`. The batch is chunked into several requests only if `api_batch_size` exceeds the endpoint's per-request id ceiling (`STEAM_API_MAX_IDS_PER_REQUEST`, 100).
4. Merges API data with existing DB row via `_merge_and_clean_api_data`, which filters to `MERGE_ITEM_KEYS` (derived from `WORKSHOP_ITEM_COLUMNS`), remaps `creator_app_id`/`consumer_app_id` to `creator_appid`/`consumer_appid`, remaps `description` to `short_description`, and remaps the API's `time_created`/`time_updated` to `steam_created_at`/`steam_updated_at`. Unknown API keys are discarded with a log message.
5. Computes Wilson scores via `wilson_lower` (a binomial-proportion confidence interval using a 95% z-score of 1.96). Sets `wilson_favorite_score` from `(favorited, lifetime_subscriptions)` and `wilson_subscription_score` from `(subscriptions, lifetime_subscriptions)`.
6. Evaluates enrichment filters via `_should_enrich`. Checks the stored `enrichment_filters` for each AppID against the item using `_evaluate_filters` (an in-memory filter evaluator that mirrors the SQL builder's semantics). If no filters are configured, all items are enriched.
7. If enrichment is approved, calls `raise_web_scrape_priority` at `max(3, requested)` -- and `raise_image_priority` at the same priority if `preview_url` is present -- where `requested` is the part of the item's pre-fetch `api_priority` a *user* asked for (`user_requested_priority`; `5` and `10` only). An item the filters exclude is still scraped, but at `max(1, requested)`: the filters choose priority, not membership, and an excluded item must not outrank a selected one ([data-model.md](data-model.md#queue-priorities)). Sets `fetch_status = 200`. Calls `insert_or_update_item` to persist. The merge sets `api_fetched_at = now_ts` and `api_priority = 0`; `last_fetch_attempted_at` was already stamped on entry.
8. For enriched items, flags `title` and `short_description` for translation via `queue_field_for_translation` at `max(3, requested)`. That function inserts into `translation_queue` and also raises the parent row's `translation_priority` (using `MAX`, so it never downgrades); the translator clears it to 0 when the item has no queue entries left. Users with non-ASCII names get `translation_priority = 1` set via `_build_user_record`.
9. After the batch, refreshes the batch's creator profiles in **one** `get_player_summaries` call: the distinct creators proposed by enriched items whose `creators` row is missing or older than `creator_staleness_days`. This was one request per item; the "only for enriched items" and staleness rules are unchanged.

**Missing ids**: `get_workshop_details_batch` keys results by each entry's `publishedfileid`, never by position, and ignores duplicate or unrequested ids. A requested id the response omits is reported as a synthetic `500`, not a `404`: the response not covering an id is a different claim from Steam having deleted the item, and a `404` would mark it permanently dead. It is therefore settled as a temporary failure and retried, rather than being silently skipped at the front of the queue.

**Error handling**: the batch helper returns `None` when the *request* failed — a transport error, a timeout, an HTTP error such as 429, or a body that is not JSON — and the daemon settles every id it carried as a temporary `500`. It returns a mapping (an empty successful response included) when the request returned and parsed. The daemon maps a per-item `404` to `fetch_status = -1` (dead), clears every queue flag so the item is in no queue, and persists a `500` as `fetch_status = 500` for a later attempt. Both paths stamp `last_fetch_attempted_at`; the `500` path leaves `api_fetched_at` untouched. `get_workshop_details(item_id)` remains the one-id spelling and returns the same shapes.

**Dynamic delay**: `api_delay` is driven by **requests**, not items — one change per batched POST, never per item. A request that returns and parses is a success whatever its individual results say (a batch of 50 of which 10 are "not found" is a completely successful API call), and it resets the failure streak. The rule is TCP congestion control, not a safety net: every refused request doubles the delay and healthy operation walks it back down, so the client converges on the fastest rate Steam will sustain — a limit that is not published and may move. Steady state is a sawtooth around that rate.

The walk back down is measured in **time**, not in successful requests: the delay halves for every `pacing.HALF_LIFE_SECONDS` (600 s) of healthy operation, so it recovers over the same wall-clock window whatever its size. A request count cannot express that — one request is a different amount of time at every delay — which is why all three rate-seeking queues share `src/pacing.py` rather than each keeping its own arithmetic. The clock is in memory only, so a daemon restarted after a day resumes at the delay it had reached instead of treating the downtime as healthy operation.

It is floored at `API_DELAY_FLOOR = 0.01 s` and has **no ceiling**. One was kept while the back-off could still be moved by the wrong signal, but it also caps convergence — a sustainable rate above the cap can never be reached — and an uncapped delay cannot run away: it doubles only when an attempt fails, and the next attempt is a whole delay away, so after k refusals the delay is `d0 * 2**k` while the elapsed time to reach it is only `d0 * (2**k - 1)`. The delay tracks the length of the outage rather than outrunning it, which is why no bound is needed. Batch size is deliberately not folded into the delay: a refusal costs one doubling whatever ids it carried, so if Steam meters the limit per item the converged *call* delay settles at roughly N times the per-item cost and the item throughput is unchanged. The delay is a literal inter-call pause, not a target rate. It is persisted after a back-off immediately — a restart during an outage must not resume at the refused pace — and after a decay only once it has moved `pacing.PERSIST_STEP_SECONDS`, with only the persisted copy rounded to two places.

### `_promote_stale_items` (daemon)

Promotes successfully-fetched items whose `api_fetched_at` is older than `item_staleness_days` from `api_priority = 0` back to `1`, returning them to the fetch queue. It is a full-table UPDATE, so `_maybe_promote_stale_items` runs it at most once per `STALE_SWEEP_INTERVAL_SECONDS` (1 hour, monotonic clock) instead of on every batch; the first batch after startup always sweeps, so a long-idle daemon does not sit on a stale queue.

The sweep **makes work, it does not do work**, so it records its own `rowcount` (timestamped) in `.daemon_state.yaml` beside the database. The API queue's time-to-drain is net of that inflow: the recorded rows are subtracted from the API completions inside the rate window. Nothing else records the sweep, and a run that promoted nothing writes nothing (see [Queue state: outstanding, rate and time to drain](#queue-state-outstanding-rate-and-time-to-drain)).

### `_scan_downloaded_items` (daemon, via `src.workshop_folders`)

Marks a subscribed item as downloaded once Steam has its folder on disk, so both front ends can draw
the solid green `downloaded` star. The scan selects exactly the items that are `own_subscribed = 1 AND
steam_download_seen_at IS NULL` — subscribed and not yet confirmed — and for each one checks
`<library>/steamapps/workshop/content/<consumer_appid>/<workshop_id>/`, stamping `steam_download_seen_at` when it
exists. **It only ever writes.** A missing folder, an unplugged drive or a moved library leaves the
marker alone, and a confirmed item is never revisited; the only clearer is the subscription walk
(`apply_own_subscriptions`), in the same transaction that clears `own_subscribed` when the item leaves
the owner's subscription list. Re-subscribing re-earns the stamp on the next scan, because the files are
usually still on disk.

The libraries are discovered once per process — `SteamPath` from the registry, then every `path` in
the install's `libraryfolders.vdf`, in both the current `<steam>/steamapps/` and older
`<steam>/config/` locations, with `steam.workshop_content_dirs` added on top — and re-resolved only
when a lookup finds nothing, so a second drive that appears later is picked up without re-reading the
registry on every check. `_maybe_scan_downloaded_items` guards the scan on a monotonic
`DOWNLOADED_ITEM_SCAN_INTERVAL_SECONDS` (60 s) clock, like the staleness sweep, because the per-batch path runs
every few seconds; the scan changed nothing logs nothing, and a changing scan logs one line with its
counts. The TUI runs the same scan on the same interval but skips it while its daemon controller can see
a daemon, so the two processes never scan in parallel. On a machine where the feature cannot work (not
Windows, no Steam, no readable library file) every function is inert and every item keeps its ordinary
marker; one startup line says why it is off, and nothing logs per check.

### `reconcile_own_subscriptions` (daemon)

Brings the owner's subscription flags in line with Steam. It walks
`steamcommunity.com/my/myworkshopfiles/?browsefilter=mysubscriptions` a page at a time for each target
AppID and, for every id it sees, stamps `own_subscribed = 1`, sets the sticky `own_first_subscribed_at`
while that is still NULL, and clears `is_queued_for_subscription` — there is nothing left to queue for an
item that is already subscribed. An id the walk does *not* see is evidence only about the pages that were
read, so an incomplete walk unstamps nothing. A **complete** walk that does not see an item clears both
its `own_subscribed` and its `steam_download_seen_at` latch in one transaction: leaving the subscription list is
the one event that takes the green `downloaded` star away, and it is the only clearer the latch has.

`_maybe_reconcile_subscriptions` guards it on a monotonic clock: `SUBSCRIPTION_RECONCILE_INTERVAL_SECONDS`
(24 hours) normally, and `SUBSCRIPTION_RECONCILE_RETRY_SECONDS` (900 s) while a session problem is
recorded, because the day's walk has already run by the time an operator fixes the login. **It runs on the
first batch after startup**, so a daemon that has just come up does not present markers from whenever it
last ran. The login cookie is re-read from the browser first — a local file copy that writes nothing when
the browser's copy has not moved — and a walk that cannot authenticate ends there and records why in
`.daemon_state.yaml`; [web-ui.md](web-ui.md#the-session-warning) carries the operator-facing half.

Every page is a web read, so it waits the shared adaptive interval (`configured_web_delay` → `pacing.wait`)
exactly as the scraper's own reads do. It only waits: a reconcile is not rate-seeking, so it never moves
that delay. Its cost is therefore `pages × (interval + the request)`, and because it runs before the first
batch acquires any work, it is what a restarted daemon waits for. *Measured live* on 2026-09-18: 157
subscriptions came back ten to a page and took **16 gated reads over 114 s** with the delay at its 6 s
floor — which is where a freshly started daemon's first two minutes went, with the daemon log showing
scrapes and previews throughout and no API fetch until the walk finished.

### `seed_database` (daemon)

Cursor-based discovery using `IPublishedFileService/QueryFiles` with `query_type=1` (rank by publication date, newest first). For each target AppID:

- Resumes from the last stored cursor (`app_discovery.last_cursor`), or `*` for the first page — unless `app_discovery.cursor_walk_finished` is `1`, in which case the AppID's cursor scan is skipped outright with a log line and the walk is left to page-based discovery.
- Fetches `numperpage=100` items per request. Each item's `publishedfileid` is inserted into `workshop_items` as a bare row at `api_priority = 3`, the documented new-item priority (fetch_status NULL, no metadata). The priority is passed explicitly rather than left to the column default, because that default is not stable across database histories: `CREATE TABLE` declares `DEFAULT 3` while the `ALTER TABLE` in migration 11→12 gives an existing database `DEFAULT 0`, so leaning on it queues discovered items on a fresh database and strands them on a migrated one (the fetch queue selects `api_priority > 0`). `_run_page_discovery` uses `5` because it handles new *and changed* items that should refresh as if visible; cursor discovery finds genuinely new items, so it uses the documented `3`.
- Stops on the first of four conditions: `fill_target` new items accumulated; an API error, which ends the pass without concluding anything; an empty `next_cursor`; or **five consecutive pages that add nothing new** (`CURSOR_STALL_PAGES`). A page that adds anything resets the consecutive count, so a stall is five in a row, not five in total. Only the empty cursor and the stall are conclusions about the catalogue — reaching `fill_target` means there is more to find and an API error means nothing was learned, so neither may mark the walk finished.
- Persists the cursor after each page via `update_app_tracking_cursor`. The cursor is **kept** when a walk finishes: it records how far the walk reached, while `cursor_walk_finished` decides whether the walk may resume.
- A stall logs at warning level, naming the AppID, and calls `mark_cursor_walk_finished`, setting `app_discovery.cursor_walk_finished = 1`. `seed_database` then skips that AppID's cursor scan on every later pass, and the state survives a restart, so the deep march cannot be re-enabled by one. There is no automatic clear: a finished walk is the conclusion the run drew.
- When the cursor is empty after a successful scan, sets the in-memory `_cursor_exhausted = True`, enabling the page-based discovery mode.

*Measured before the stop rule existed* (2026-09-21, AppID 431960, ~1.7–3.2M items): recent passes scanned 13,487 / 1,724 / 5,624 / 6,964 pages, each discovering 0 new items, at ~2.3 pages a second, and every one ended only because the API refused. `Cursor exhausted` never appeared in 400,000 log lines, so the page-mode fall-back it enables never fired.

**Once an AppID's walk is finished, newly published items are found by page-based discovery, not by the cursor walk.** Page mode ranks by last-updated time and runs at most once per 24 hours per process (see below), so a newly published item can wait up to that long to be discovered — normally less, because it is at the head of updated-order and the first page carries it, and because a restart resets the in-memory cooldown. That is the trade the stop rule accepts: paging an exhausted catalogue without bound is exchanged for at most a day's discovery latency on new items.

This is called when `get_next_items_to_fetch` returns empty — meaning the processing queue is drained and new items need to be discovered.

Discovery is skipped while the daemon already has enough outstanding work: for each target AppID,
`seed_database` returns early when at least `DISCOVERY_FILL_TARGET` (300) items are fetchable — queued
and not dead, the population `get_next_items_to_fetch` selects. The same value is the per-run fill
target, so a pass that does run refills to 300. The guard exists so that a healthy backlog is not
re-crawled. The target was raised from 100 to 200 because the fetch loop drains the queue between
discovery passes: at 100 every pass found the queue already at or above the threshold and skipped it,
so the refill raced the drain instead of leading it; 200 still crossed it on occasion, so 300 is the
headroom above the crossing — three pages at the request page size of 100. It deliberately does not
count items that have never been fetched: those may not be queued at all, and reading them as
outstanding work once suppressed discovery permanently while the fetch queue held a single item.

### `_run_page_discovery` (daemon)

A periodic alternative to cursor-based discovery. Enabled when a `.fetch_new` trigger file exists, when `_cursor_exhausted` is True, when any target AppID's `app_discovery.cursor_walk_finished` is `1`, or when at least 500 items have been scraped (`api_fetched_at IS NOT NULL`). Runs at most once per 24 hours (tracked via `_last_page_discovery`), unless the trigger file bypasses the cooldown.

Uses `query_workshop_updated_page`, which calls QueryFiles with `query_type=21` (rank by last updated, most recent first) and cursor-based pagination. It walks up to 500 pages per AppID, comparing each item's returned `time_updated` against the stored `steam_updated_at` and upserting new or changed items as bare rows at `api_priority = 5`. Stops when a page yields no new or changed items.

After page mode completes, the daemon resumes normal cursor-based discovery — except for an AppID whose walk is finished, whose cursor scan stays skipped and whose new items page mode continues to carry.

### `query_workshop_newest_page` (steam_api)

Calls `IPublishedFileService/QueryFiles/v1/` with cursor-based pagination. Parameters: `query_type=1` (publication date), `cursor`, `numperpage=100`, `appid`. Returns `{total, items, next_cursor}`. Rate-limited via `_rate_limit()`.

### `query_workshop_updated_page` (steam_api)

Same API endpoint but with `query_type=21` (last updated) and cursor-based pagination. Used exclusively by `_run_page_discovery`. Returns the same `{total, items, next_cursor}` shape.

---

## API Detail Fetch Phase

### `get_workshop_details_batch` (steam_api)

Calls `ISteamRemoteStorage/GetPublishedFileDetails/v1/` once for many ids — `itemcount=N` with `publishedfileids[0..N-1]` — and returns a `{id: detail}` mapping. Results are matched by each entry's `publishedfileid`, never by position, so a reordered, duplicated or extended response cannot mis-assign a result. A requested id the response omits is filled in as `{status: 500}` — deliberately not a `404`, which would mark the item permanently dead. Returns `None` when the request itself failed (transport, timeout, HTTP error, or unparseable body); that is the signal the daemon backs off on. The per-request ceiling is `STEAM_API_MAX_IDS_PER_REQUEST = 100`, taken from the documented `GetPlayerSummaries` limit and applied to `GetPublishedFileDetails` too, which publishes no cap.

### `get_workshop_details` (steam_api)

The one-id spelling of the batch call, kept for existing callers. Returns `{status: 500}` on a request failure and `{status: 404}` when the item is not found or `result != 1`. Returns the raw detail dict otherwise, containing the API's `title`, `description`, `tags`, `file_size`, `preview_url`, `creator`, `subscriptions`, `favorited`, `views`, `time_created`, `time_updated`, and more (these are the raw API field names; `_merge_and_clean_api_data` renames some of them before storage).

### `_merge_and_clean_api_data` (daemon)

Merges API response data into the existing DB row. Applies column-name remapping (`creator_app_id` → `creator_appid`, `description` → `short_description`, `time_created`/`time_updated` → `steam_created_at`/`steam_updated_at`). Filters to `MERGE_ITEM_KEYS` (derived from `WORKSHOP_ITEM_COLUMNS`) to prevent unknown API columns from polluting the DB, and discards known-but-handled-externally keys (for example `web_scrape_priority`, `image_answer`, `image_priority`, `translation_priority`). Normalizes tags via `normalize_tags`. On the success path it stamps `api_fetched_at = now_ts` and `api_priority = 0`.

The queue-owned columns are dropped rather than carried because the merge is a read-modify-write: the existing row is read before the API call and written back after it, so a queue flag that changed while the request was in flight would be overwritten by the stale snapshot. `web_scrape_priority` and `image_priority` are set explicitly between the merge and the insert; `translation_priority` is written by `queue_field_for_translation` just after the insert, so the merge must leave the column untouched.

### `_should_enrich` (daemon)

Checks whether an item passes the enrichment filter for its AppID. Reads `enrichment_filters` from `app_discovery` (a JSON array of filter dicts in the same format as the TUI search builder). Feeds the item dict through `_evaluate_filters`, which uses `_evaluate_single_filter` for each criterion and `_evaluate_tag_filter` for tag-based filters. Returns True if no filters are configured for the AppID (enrich everything).

**The daemon's in-memory check and the search's SQL translation are not the same predicate, and that is deliberate.** `_evaluate_filters` reads the original columns, while `build_filters_sql` — the one builder `search_items` uses — also searches each text field's `_en` counterpart, so an item whose stored translation matches a `Title`/`Description` filter but whose original text does not is selected in SQL and rejected in Python. A `percentile` filter has no fixed predicate in either: it is relative to the result set it is computed over, so the in-memory check treats it as matching everything and the builder skips it. A saved `Subscribed` filter is evaluated in memory against the shared value table, not against the raw flag columns. The daemon therefore keeps using `_evaluate_filters` wherever the answer must match the fetch path, including migration 21→22's demotion walk; the coverage metric's scoped figure and its Translations bar's population are explicitly *the search builder's translation* of the filters, and say so where they are shown. See [tui.md](tui.md) and [web-ui.md](web-ui.md).

**A `Subscribed` row can now be saved as an enrichment filter**, so the daemon
evaluates it in memory against four columns. The merge above deliberately drops
`is_queued_for_subscription` and `steam_download_seen_at` (they are in `MERGE_EXCLUDED_KEYS`),
so the merged record alone would read them as NULL and a `queued`/`downloaded`
filter would silently answer "no match". `_raise_scrape_and_image_priorities` therefore
overlays the pre-fetch record's values for exactly those columns on a copy before
calling `_should_enrich`, and never writes them back. Migration 21→22's demotion
walk calls the same `_evaluate_filters` and expands the field's virtual column to
all four when it selects the columns to load — see [search-filter.md](search-filter.md).

### Failure classification (daemon)

`_settle_api_failure` turns a non-success outcome into a queue decision. `404` is permanent: the failure is logged, the item is marked dead (`fetch_status = -1`) and it is removed from **every** queue — `api_priority`, `web_scrape_priority`, `image_priority` and `translation_priority` are all cleared, because a dead item can never complete and a queue flag left set would strand it in a queue that never drains. Everything else is temporary — `500` (including every id of a request that failed and was settled as `500`), transport exceptions, and any status no branch handles. Those keep the item queued at one priority level lower, floored at `1`, because priority `0` means "not queued" and clearing it is what previously stranded transient failures with nothing able to bring them back. Unhandled statuses are captured as evidence and never fall through to the success path.

These per-item outcomes never touch `api_delay`. A batch request that returned and parsed is a success even when some of its items settle here, so only `_fetch_details` — which counts the request once — moves the delay; see the delay rule above.

### Change detection across stages

The API refresh is the change detector. It is the cheapest call and the only stage that goes stale on a timer, so the stages hanging off an item follow it. `_raise_scrape_and_image_priorities` compares the pre-fetch `steam_updated_at` against the freshly merged one: when they match it reuses the stored extended description and leaves the image alone, and when they differ it re-queues both. Translation does the equivalent with `translate_version` — see [What queues a field for translation](#what-queues-a-field-for-translation).

A stage is therefore re-queued because its source changed, or because its output is missing — never because time passed. Per-queue staleness sweeps are deliberately not used. An item whose stored revision is unknown (`steam_updated_at` NULL) counts as changed, since no change can be ruled out.

---

## Web Scraping Phase

### `WebScraperThread` (web_worker)

A daemon thread that picks up items from `get_next_web_scrape_item`, ordered by `web_scrape_priority DESC, api_fetched_at ASC` (highest priority first, oldest-fetched within priority). For each item:

1. Calls `scrape_extended_details(url)` which fetches the Steam Community workshop page and parses the extended description and tags.
2. If the description was found, updates `extended_description`, sets `web_scrape_priority = 0` and stamps `web_scraped_at` with our clock. The tags the scraper returns are not persisted; tags in the database come from the API.
3. Flags non-ASCII `extended_description` for translation at priority 3, unless its translation is already current (see [What queues a field for translation](#what-queues-a-field-for-translation)).
4. If the request could not be completed at all — a transport failure, which `scrape_extended_details` reports as `None` — raises `api_priority` to 2 so the metadata is re-fetched (that value has no other source); nothing is cleared, so the item stays in the scrape queue.
5. Otherwise `classify_scrape` names the outcome and the run loop responds to it (see [Outcome taxonomy](#outcome-taxonomy)). Every non-success outcome that carries a served page is captured as evidence, and a miss is never a blanket failure or an empty success: the wording and markup decide what the queue learns. See [failure-capture.md](failure-capture.md).

#### Outcome taxonomy

`classify_scrape` names one outcome per attempt and the run loop switches on it, so which outcome buys which response is one readable table rather than a chain of conditions.

| Outcome | Recognised by | Item's queue flag | Pacing response |
|---|---|---|---|
| `SUCCESS` | `description` is not `None` | `web_scrape_priority = 0` | success: resets the failure streak, can decay the delay |
| `RATE_LIMITED` | the body reports "too many requests" | untouched | the delay doubles, at once |
| `ITEM_MISSING` | HTTP 404/410, or the page's item-error wording | `web_scrape_priority = 0` | no back-off |
| `ITEM_PAGE_WITHOUT_DESCRIPTION` | `workshopItem` present, `highlightContent` absent | `web_scrape_priority = 0` | neutral: neither success nor failure |
| `GATED` | no item markup, plus an age-check, sign-in or error marker | untouched | no back-off; `_refresh_login_cookie_if_gated_or_signed_out` has already re-read the login cookie if the page looked gated |
| `UNKNOWN` | a transport failure, a 5xx, or a page that is neither the item's nor a recognised condition | untouched (a transport failure also raises `api_priority` to 2) | grows `web_delay` |

A 5xx is `UNKNOWN` whatever its body says: the status is a server fault with no attributable cause, so it keeps the back-off.

**A missing item.** A live probe found that the Workshop serves its item-error page with **HTTP 200**, not 404 — a well-formed but absent id returned "There was a problem accessing the item", and a malformed id returned "That item does not exist" — so the status is not trusted and the wording is matched as well (`looks_like_missing_item`). The worker clears `web_scrape_priority` but deliberately does **not** mark the row dead: existence is the API's call, and the API makes it on its own 404. Clearing the flag is the conservative move — the API re-flags the item while its description is still missing if Steam ever serves it again — and it is what stops a gone item spinning in the queue at full pace now that it no longer backs off. Both the status (when there is one) and the matched wording are logged, and the page is captured as evidence, because there was no capture of this page before. A definitive HTTP 404/410 does not even earn the cookie refresh — no credential materialises a gone item — while the HTTP 200 wording still re-reads the cookie, since there the status proves nothing.

**Dynamic delay**: The shape is shared with the other queues (`src/pacing.py`) even though the unit is not: a page scrape is one request per item and cannot be batched, so the worker keeps its own `web_delay_seconds`. Every refusal doubles the delay and healthy operation halves it for every 600 s it has been running, so the web scraper recovers over the same wall-clock window as the API and the image worker. The old per-item 100-success / 2-failure compounding rule is gone: a success count is a different amount of time at every delay, so it made the worker recover faster the faster it was already going.

Only an **unknown** outcome — a transport failure, a 5xx, or a page that is neither the item's nor a recognised condition — is unattributable, and it needs two consecutive ones after a streak before the delay moves. A **rate limit** is the budget itself speaking, so it doubles the delay immediately rather than waiting for a second strike. A missing item and a description-less item page are answers the item gave; a gate is a session problem a slower pace cannot fix. None of those reaches the failure counter, so the scraper is not slowed for a reason a lower request rate could not address. The item page with no description must not count as a success either: it yielded nothing, so counting it as one would let the delay fall again while unknown outcomes continued.

The decay stops at a **6.0 s floor** (raised from 1.0 s): the same Steam budget is shared with the owner's own hand-browsing, so when scrapes start failing the worker has to back off far enough that the Workshop is still usable manually while the daemon runs. The starting default is the floor. There is **no ceiling** any more — it went with the fixed 300 s pause, which was a second pacing rule that could not converge: the same pause however often the throttle recurred, and no slower a rate afterwards. A rate that moves on every refusal needs no separate rule, and it cannot run away (see the API rule above).

The delay is persisted as `daemon.web_delay_seconds`, and that persisted value is the only shared truth
between processes: `src.web_worker.configured_web_delay` reads it fresh from the config rather than from
a module-level snapshot, so the subscribe engine's page reads and the subscriptions walk wait the same
interval the worker is using. A page read that does not go through the worker therefore gates itself
with it; the subscribe POST does not, because it is a click rather than a page load — see
[Subscribe Engine](#subscribe-engine-browser-free).

**One owner for the interval.** `web_delay_seconds` is the web interval's only owner. The scraper used to enforce a second, fixed 5 s gate of its own — `_WEB_DELAY`/`_rate_limit()` inside `scrape_extended_details`, movable only through `set_web_delay()`, which had no callers — so every worker scrape paid the adaptive delay *and* the fixed one, and a re-scrape that bypassed the worker's pacing was spaced by the fixed gate alone. That gate is removed; the scraper now sends as soon as it is called, and any caller that needs spacing gates itself on the configured `web_delay_seconds` through `pacing.wait`, as the worker does.

**Throttling**: Steam answers many requests with **HTTP 200** and its ordinary Workshop shell
carrying "too many requests", so the status code proves nothing and the page is otherwise
indistinguishable from a content miss. It is detected separately and treated as a spent request
budget rather than a bad item: the item's priority is left alone, no immediate retry is attempted,
and the delay doubles at once, so a sustained throttle backs off geometrically.

The owner's hypothesis is that this page is **bot deterrence** — a reply that *says* "throttled"
to discourage automated clients — rather than a genuine per-account budget that refills over
minutes. That suspicion is **not proven**, and it is why the HTTP request is now shaped like a
real browser (see `scrape_extended_details` below). It changes nothing about the handling: whatever
the cause, the reply is not the item, so it stays "no content" rather than a bad item, and no retry
is made into a reply that may be reporting a spent budget.

The throttle page is served as a **bare anonymous shell even when the request carried a valid
session** — *measured*, across one run in which the same cookie produced 4 item pages carrying the
account dropdown and a real `g_steamID`, and 27 throttled pages carrying neither. The absence of
the signed-in markers is therefore caused by the throttling, not by a bad cookie, and a throttle
page must not be read as evidence that the session has lapsed.

**Gated pages**: A miss whose body carries no item markup but does look like an error page, an age
check or a sign-in wall is classified `GATED`. `_refresh_login_cookie_if_gated_or_signed_out` has already re-read the
login cookie when the page looked gated or signed out, so the next request carries the freshest
credential. Nothing is re-scraped immediately: the miss goes through `classify_scrape` and takes the
ordinary `GATED` path, which leaves the item queued in its place and the delay alone (a slower pace
cannot fix a session that is not working), and the queue retries it under the worker's own adaptive
delay. A merely broken page therefore cannot double the request rate either.

The two checks are evaluated **independently**, and neither shadows the other. They overlap by
construction: a throttle page is not the item page, so it lacks the signed-in markers too, and
reading that overlap as "signed out" would wrongly skip a refresh that spends no network budget. A
throttle page still gets the cookie re-read — a local file copy — so the next request that does go
out carries the freshest credential, but it makes **no claim about the session**: the page is
anonymous because the budget is spent, not because the login is bad, so it neither raises the
sign-in warning nor clears one.

**Misses**: the throttle outcome outranks the other page outcomes, so a throttled page is paused and
never read as a genuine absence. Otherwise a miss is neither a blanket failure nor an empty success:
a page that says the item is gone clears the item's queue flag, a description-less item page clears
it too, a page that is not the item's — an error page, a wall, a throttle the marker missed — leaves
the priority untouched, and only a page matching no recognised condition counts as a failure for
pacing. See the [outcome taxonomy](#outcome-taxonomy) and
[Dynamic delay](#web-scraping-phase). Migration 17→18 requeues the rows the old "truthy dict is
success" test stranded with `extended_description = NULL` and `web_scrape_priority = 0`; see
[schema-migrations.md](schema-migrations.md).

### `scrape_extended_details` (web_scraper)

Fetches the HTML page `steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}`. Parses the DOM using `requests-html` to extract:
- `description`: the full extended description (from `DESCRIPTION_SELECTOR`, `.workshopItemDescription#highlightContent`)
- `tags`: the tag names from `TAGS_SELECTOR`, `.workshopTags a`

Returns a dict, or `None` **only** when the request could not be completed — a connection error or a
timeout, where there is no response to inspect. An HTTP error is a served answer: `raise_for_status()`
raises `HTTPError`, which carries the response, so its `http_status` and body are returned in the same
miss dict (`description: None`) rather than being flattened into `None`. That split is what lets the
caller classify a 404/410 (the item is gone) apart from a 429 (a throttle) and a 5xx (a server fault).
A successful request whose description selector did not match returns `description: None` — truthy, so
the caller must test the description and not the dict. On any miss the dict also carries the response
`body`, `http_status` and `final_url` so the caller can capture it; on a successful parse `body` is
`None`, since there is no reason to retain a few hundred KB of HTML on the happy path. The caller
stores only `description`; the `tags` key is discarded.

The function paces nothing itself: it sends the request as soon as it is called. The interval between
requests belongs to the caller and is owned by the configured `web_delay_seconds` — the worker sleeps
it through `pacing.wait`, and any other caller must do the same (see [One owner for the
interval](#web-scraping-phase)).

**Browser-faithful requests.** The HTTP request is deliberately shaped to match a real Firefox top-level navigation, measured from a HAR capture of a signed-in item load. Both request sites (`scrape_extended_details` and `discover_items_by_date_html`) send one shared header mapping:

- `Accept`/`Accept-Language`: a navigation asks for HTML in a human language. `requests` defaults `Accept` to `*/*`, which no browser navigation sends — it reads as a client that does not care what it gets, and it is the single clearest giveaway that a request is a script.
- `Sec-Fetch-*` fetch metadata: Firefox attaches these to every request, so their complete absence is a strong non-browser signal. `Sec-Fetch-Site` is `none`, not the capture's `cross-site`, because the scraper fetches the URL directly rather than following a link from another origin; `Referer` is likewise omitted rather than invented, because a direct fetch has no referring page.
- `Upgrade-Insecure-Requests`, `Priority`, `Connection: keep-alive`, and a Firefox `User-Agent`.

`Accept-Encoding` advertises `br`/`zstd` only when this interpreter can actually decode them (a `brotli`/`brotlicffi` or `zstandard` module is importable); otherwise it advertises `gzip, deflate`. Offering a codec nothing here can decode would leave compressed bytes in the body and look like a broken page for an unrelated reason.

The `User-Agent` version is read from the Firefox profile's `compatibility.ini` (`LastVersion`) using the same profile discovery that supplies the cookies, so the claimed version matches the browser actually signed in; it falls back to a constant Firefox string when no profile is readable. A single `requests-html` session is created lazily and shared by both request sites, so the TCP connection and its TLS handshake are reused the way a browser reuses them instead of a fresh handshake per call.

The cookies are the profile's whole `steamcommunity.com` set when `session.read_firefox_cookies` is enabled — the ten names a real navigation sends, not two hand-picked ones plus a hardcoded third — and nothing is invented for a name the profile does not have. The set is remembered for the process but not past its own credential's expiry: while the `steamLoginSecure` token is live (or states no readable expiry) the cached read is sent, and once it says it has expired the profile is read again, so a daemon that has been up for days does not keep sending a credential Steam has reissued. When the setting is off, the configured `sessionid`/`login_secure` pair is sent exactly as before, and the login cookie is omitted when unset.

This browser shape was prompted by the "too many requests" page described above: the suspicion is that it may be bot deterrence rather than genuine throttling. That suspicion is **not proven**, and it changes nothing about detection or the pause — the reply is still not the item, so the worker still treats it as "no content" and still leaves the item untouched.

---

## Subscribe Engine (browser-free)

`src/subscribe_engine.py` performs the owner's subscription without a browser tab. It is the shared
implementation both front ends will use: the TUI's subscription queue drives it today, and the Web UI
adopts it next (the Tampermonkey bridge that does this from a browser tab is being retired — see
[future-plans.md](future-plans.md#removing-the-browser-bridge-from-the-subscribe-path)).

The behaviour is dictated by facts measured against production, not by what the response body seems
to say:

* The subscribe control is **server-rendered** as one element, `id="SubscribeItemBtn"`, whose class
  list carries `toggled` when and only when this account is currently subscribed. No JavaScript has
  to run to read it (both states are present in the signed-in captures under `/root/.dsh/live/scrapes`).
* `POST https://steamcommunity.com/sharedfiles/subscribe` answers `{"success": 1}` **both** when an
  item was newly subscribed and when it was already subscribed. The body confirms the request was
  accepted; it never says whether anything changed.
* A missing `#SubscribeItemBtn` is "cannot tell", never "not subscribed". An error page, a signed-out
  page and a throttle page all omit it.
* `sessionid` is a **session cookie**: Firefox keeps it in memory and never writes it to
  `cookies.sqlite`, so a profile read can never supply the current one. The page carries
  `g_sessionID` instead, which belongs to the session that served that page, and it is the token the
  POST uses. The cookie set's `sessionid`, the pushed `_pushed_sessionid` global and `session.csrf_token` are only
  fallbacks for a page that carries no token.

So one run is:

1. **Read** the item page (`fetch_item_page`, the scraper's own request shape via
   `scrape_extended_details(..., keep_body=True)`). `parse_button_state` reads the one element's own
   class list.
2. **Short-circuit** when `toggled` is present: the outcome is `already_subscribed`, and **no request
   is sent**. This is both the guard against the endpoint ever turning out to be a toggle and the
   reason re-running a queue is cheap. The page is the same authority the confirmation step trusts,
   so the observation is recorded with the same write the confirmed path uses
   (`mark_own_subscribed`): `own_subscribed` is set, `own_first_subscribed_at` is stamped if it was
   still NULL, and `is_queued_for_subscription` is cleared. Recording nothing here would leave an
   item already known to be subscribed in the queue, so every later pass would list it and spend a
   gated page read rediscovering that it is subscribed.
3. **Click** (only when the page says not subscribed): `post_subscribe_request` sends the POST, built
   from the same helpers `/api/subscribe/<id>` uses. `resolve_subscribe_token` takes the form token
   from the page just read -- `g_sessionID` first, then the cookie set, then the pushed/configured
   fallback -- and puts it back into the cookie jar so the form field and the cookie agree. Steam
   answers `success: 2`/`15` (or HTTP 401) when it refuses the request: beside an **authenticated**
   page read that is a refused CSRF token and is reported as `token_refused` with **no session
   problem recorded**, because the credential just fetched the page; beside an **anonymous** page read
   the refusal is recorded as a session problem with the route's own sentence, as before.
4. **Record** (`record_confirmed_subscription`): on the default path
   (`VERIFY_AFTER_SUBSCRIBE = False`) the POST's own `success: 1` is the record — the subscription is
   marked and the recorded session problem cleared, exactly as `/api/subscribe/<id>` records it; any
   other answer is a `failed` worded by `_steam_failure_message`. The engine does **not** read the page
   a second time. With the switch set `True` the engine instead **confirms**
   (`confirm_subscription`): read the page again and decide from the button. `toggled` present means
   subscribed — the same `record_confirmed_subscription` write. `toggled` absent with `success: 1` is a
   **disagreement**: nothing is recorded and the item stays queued, because the page is the authority
   and the JSON is corroboration only. A throttle page on the confirmation read stays queued; any other
   button-less page reports that the result cannot be told.

**Outcome vocabulary.** Each run ends in one status, and the TUI's queue row renders its phrase from
`subscribe_engine._OUTCOME_LABELS`:

| Status | Phrase | Meaning |
|---|---|---|
| `already_subscribed` | already subscribed | the pre-read's `toggled` said so; no request was sent |
| `subscribed` | subscribed | the POST answered `success: 1` (or, with the switch on, the confirmation read said so); `mark_own_subscribed` was recorded |
| `disagreement` | unverified (sources disagree) | with `VERIFY_AFTER_SUBSCRIBE` on, Steam said success but the page did not; nothing recorded |
| `throttled` | left queued (throttled) | Steam's throttle shell; the item stays queued |
| `session_problem` | session problem | a refusal beside an anonymous page read; a session problem is recorded |
| `token_refused` | refused (stale CSRF token) | a refusal beside an authenticated page read: the login works, the token was stale |
| `refused` | refused (see log) | the engine would not send or could not read the state — no session, no login, no item row, no AppID, or a page with no subscribe button. The cause differs per case and the caller logs it, so the phrase points there |
| `failed` | failed | the attempt raised |

`refused` is deliberately **not** "cannot tell": every outcome it covers was determined — either the
item cannot be subscribed or its button could not be read — and only the cause varied. It is kept
distinct from `token_refused`, whose phrase names the one cause, the stale CSRF token.

**The confirmation read is retired by default.** It doubled each item's page reads, so it is isolated
in `confirm_subscription` behind the module-level `VERIFY_AFTER_SUBSCRIBE` switch — now `False`, which
is what makes a queue of N items cost N interval-paced reads rather than 2N. The production run
measured why it can go: the endpoint is safe on an already-subscribed item, and the body cannot
distinguish "newly subscribed" from "already subscribed". Setting the switch back to `True` restores
the read unchanged. The **pre-read is not next**: it keeps both of its jobs — the guard against the
endpoint ever turning out to be a toggle, and the skip that makes re-running a queue cheap — until
idempotency is trusted beyond a single observation. The retirement order, and the cheap middle ground
of letting the daily reconcile be the confirmer, are recorded in
[future-plans.md](future-plans.md#retiring-the-subscribe-confirmation-read).

**Pacing.** Every page read is a page load, so `WebInterval` gates it on the web scraper's shared
adaptive interval — the persisted `daemon.web_delay_seconds`, read fresh through
`src.web_worker.configured_web_delay` rather than snapshotted, decayed with `pacing.decay` on a read
that carried a button and doubled with `pacing.backoff` on a throttle page, then written back through
the config the way the daemon's `_save_config_value` writes it. On the default path an item pays that
once; the confirmation read, when the switch re-enables it, honours the same gate. A pass builds one
interval and threads it through every item, so the items are spaced. **The subscribe POST is exempt**:
it is the button click, a browser-initiated XHR rather than a page load, so it never waits. The
subscriptions walk in `src/subscription_sync.py` uses the same gate, and it only waits — a reconcile is
not a rate-seeking queue and does not move the shared delay.

**Capture.** Every page read and the POST go through
`capture.record_web_download` under the `item_page` and `subscribe` kinds, covered by the existing
`web_downloads` switch; the switch's credential elision is what keeps the login cookie out of the
outbox.

---

## Image Download Phase

### `ImageDownloadThread` (image_worker)

A daemon thread that picks up items from `get_next_image_item`, ordered by `image_priority DESC, api_fetched_at ASC`. For each item:

1. Checks `preview_url`. If absent, clears `image_priority = 0` (no image to download).
2. Downloads the image via `requests.get(stream=True)`. Detects MIME type from Content-Type header, mapping known types (`image/jpeg` → `jpg`, `image/png` → `png`, etc.) via `MIME_MAP`.
3. If Content-Type is unrecognized, uses `puremagic` (a file-magic detection library) on the first 8KB of the response body to guess the extension. Maps puremagic extensions via `MAGIC_EXT_MAP` (includes `.jfif` → `jpg` for JPEG variants).
4. If extension can't be determined, logs a warning (including the puremagic guess), captures the served response as an image failure, and records the **served type** in `image_answer` (`html` for an error page, `svg+xml` for a picture format the downloader cannot write) while clearing `image_priority = 0`. It does not increment the failure counter: an unrecognised type is not a transient error, and it is final — the same response is what a retry would get.
5. Saves the image to `images/{workshop_id}.{ext}`.
6. On success updates `image_answer` to the extension, sets `image_priority = 0`, and stamps `image_fetched_at` with our clock. It writes no Steam revision; the `scrape_version` the image worker used to overwrite was dropped in migration 34→35.
7. On failure, splits on whether the server actually answered. A **permanent status** (`404`, `410`) is written into `image_answer`, `image_priority` is cleared, and `api_priority` is deliberately **not** raised — raising it asked for an API refresh, the refresh re-flagged the image, and the download 404'd again, which is how one item came to be fetched twenty-five times in a day for a preview that never existed. A permanent answer is also neutral for pacing. Any **other** failure decrements `image_priority` by 1 (down to a minimum of 0) and raises `api_priority` to 2, so transient errors are retried with decreasing priority.

**Evidence capture.** Every image failure — an HTTP status, a transport exception, or an unclassifiable MIME type — is captured whenever `daemon.outbox_dir` is set, with its status, response headers, URL, content type and length, and the exception text when there is no response. A download that succeeds is captured **only** while the `daemon.capture_image_downloads` debug switch is on, with the same metadata plus the number of bytes written and the path of the saved file. The image bytes themselves are never copied into the outbox: the file under `images/` is the artefact, so a capture holds metadata only (see [failure-capture.md](failure-capture.md)).

**Dynamic delay**: The shared shape (`src/pacing.py`) with its own `image_delay_seconds`: every refusal doubles the delay and healthy operation halves it for every 600 s it has been running, floored at 0.5 s and with no ceiling. An unrecognised MIME type and a permanent status do **not** count as failures: both answer a question about the *item* rather than about our request rate, so neither grows the delay nor breaks a run of successes. The captures proved the distinction was real — 404s outnumbered timeouts 6,517 to 163 over one window, so the delay had been moved almost entirely by the class that says nothing about being refused.

---

## Translation Phase

### `TranslatorThread` (translator)

A daemon thread that batch-translates text fields. Uses OpenAI-compatible API with configurable endpoint and model.

**Request shape**: the fields go out as a sequence of boundary blocks — a boundary line carrying a per-request phrase, the queue row's `item_id` and its field label, then the source text beneath it — and the model is asked to copy each boundary line verbatim and translate only the text under it. The request is written in the shape of the reply, so nothing asks the model to build a structure of its own.

The boundary is **a phrase of four words drawn fresh for every request** and repeated on every block in that request, not a run of punctuation: a word costs one token for three to five characters where punctuation costs one per character, and it is the phrase — a nonce — rather than the shape of the line that makes a boundary unambiguous. Ordinary prose contains dashes and can contain a line shaped like a boundary; it cannot contain this request's phrase, and `choose_phrase` redraws (a bounded number of times) when the batch's own source text does. The words come from EFF Short Wordlist #1, embedded in `src/wordlist.py`. The label is a **one-word alias** for the queue field — `title`, `short`, `long`, `user` for `title_en`, `short_description_en`, `extended_description_en`, `personaname_en` — held explicitly in `FIELD_LABELS` (`src/translator.py`) so the vocabulary the model is shown is stated in one place rather than derived by trimming a suffix. `short` and `long` stay distinct because an item can have both descriptions queued at once and the fallback alignment keys on `(item_id, label)`. A field with no alias goes out under its own name rather than raising, so a field added later cannot break alignment.

It replaced a JSON envelope (`[{id, field, text}]` in, `[{id, translated}]` out), which required the model to escape every backslash and quote inside the translated text *and* to emit the whole array in one piece. The larger saving is the bill rather than the failure count: building a JSON object is expensive in tokens — braces, quoted keys and punctuation on every field, plus an escape for every quote, backslash and newline — where a boundary reply pays for one short line per field and escapes nothing. *Measured live*: two budget windows under the same **$2** API cap produced **1,140** translated fields through the envelope and **11,293** through boundary blocks — **9.9×** for the same money. The failure history below is the same defect's visible symptom: *measured* in the live log over 300,000 lines (≈6.5 hours): 256 batches succeeded and **83 failed**, every failure a malformed envelope — a bad escape (`Invalid \escape`, `Invalid \uXXXX escape`) or a reply that stopped mid-string (`Unterminated string starting at: …`) — apart from one `database is locked`, while `No translation returned` was zero. One malformed reply in a 20-field batch discarded all 20 — billed, then re-sent — and one of those replies was 192 KB; the worst single batch was re-sent **eight times** over 45 minutes, its backoff climbing from 2 s to 256 s. The boundary format removed the class rather than shrinking it: in a comparable two-hour slice of the log after it landed, there are **2** failed attempts against **17**, both `No usable translation blocks` and neither retried more than once, with no parse error at all. It also changed what a partial reply costs — the open format lets the model omit a block without invalidating the rest, so a reply covering only part of the batch lands as a partial success (`n translated, m left queued`) instead of being lost, which is the case the demotion below handles.

**Packing**: the poll fetches `openai.batch_char_cap // PER_FIELD_OVERHEAD_CHARS + 1` candidates via `get_next_batch_for_translation` (ordered by `priority DESC`, then NULL-`queued_at` rows ahead of dated rows, then `queued_at ASC`) and packs them by an estimated **cost**. Every field is charged its source text plus `PER_FIELD_OVERHEAD_CHARS` (50, a module constant in `src/translator.py`) — a documented estimate of the boundary scaffolding the request wraps around it: the phrase, the entity id, the field label and the newline. The running total is compared with `openai.batch_char_cap` (default 4,000): the **first** candidate is taken before the cap is consulted — so a field larger than the cap is sent on its own rather than never — and from the second onwards the cap is checked **before** the candidate is taken, so no other request exceeds it. There is no item ceiling: the count per request follows from the fields' own lengths, and because every field pays at least the overhead the count cannot run away. `openai.batch_items` is no longer read; a config that still carries it (or its legacy spelling `openai.batch`) gets one warning saying so. The extra fetched row is what makes "the next candidate would exceed the cap" detectable at all, so a full request can be told from one the queue could not fill. The default cap comes from the queue's own distribution rather than a guess: *measured* in the 2026-09-18 backup (127,385 rows), median 19 characters, p90 69, p99 996, max 7,725, with `extended_description_en` median 42 and p99 4,072. At 4,000 with the 50-character overhead a request holds at most 80 fields, while the pathological reply above becomes at most ~8 KB — a single over-cap field.

**Sending**: a full or urgent request goes at once. A request the queue could not fill waits up to 30 s (`BATCH_FILL_WAIT_SECONDS`) for more work, re-polls once, and is then sent **whatever it holds**: an under-filled batch is never re-polled a second time. Any field with priority ≥ 5 (a detail-view bump) skips the wait. An empty queue keeps the 30 s idle poll. Before this, the loop translated only a batch that reached the configured size unless something was urgent, and otherwise re-polled every 30 s indefinitely — work smaller than a full batch never ran at all. That was never a deliberate "wait for a full batch" policy with a send-what-you-have fallback; there was no such fallback. A demoted row is the one interaction with this: one step down from 5 is 4, so a partial batch holding only demoted rows no longer counts as urgent and waits the bounded window for company instead of going at once.

**Translation process** (`_translate_batch`):
1. Builds the boundary blocks described above, one per field, in queue order.
2. Sends to the OpenAI API at `openai.temperature` (default 0.0) and **without `max_tokens`**: a truncated translation is a corrupt one, so the reply is not bounded by a token budget.
3. Parses the reply into blocks. **Positional first**: when the reply yields exactly as many blocks as were sent, they are assigned in order and the boundaries are corroboration only — which is what keeps a reply usable when the model translates correctly but mangles a boundary. A reply whose phrase was mangled is then retried against a tolerant boundary pattern (some short words, then an id and a field label) before any alignment is attempted. When the counts differ under both, the boundaries become an alignment guide matched on `(item_id, field label)`; a row that gets no block is left out, and a block matching no row is ignored. Blank lines at a block's edges are the boundary rather than content; internal newlines and spacing are preserved.
4. For each resolved field, updates the corresponding `_en` column on `workshop_items` or `creators`, stamps `translate_version` (or `translated_at` on `creators`), and deletes the queue entry.
5. After the batch, for each translated id — an `(entity_type, entity_id)` pair, so a creator's steamid can never be counted or completed against `workshop_items` — checks whether any queue entry for that id remains. For an **item** with none left, it sets `translation_priority = 0` and stamps the item's `translated_at` — our clock for the completion of the item as a whole — in the same statement and transaction; a field write while another field is still queued is a partial stage and does not stamp. For a **creator** with none left, it clears `creators.translation_priority`: the per-field write in step 4 already stamped `creators.translated_at`, and a creator has no version key to stamp.

**Partial replies and failures**: a reply covering only part of the batch is a **partial success**. The fields it resolved are committed, the fields it missed keep their `translation_queue` rows for a later pass, and the failure streak behind the backoff is not grown — the model did answer, so backing off would slow work it did return. Each missed row has its `priority` **lowered by one** in the same transaction and with its `queued_at` left alone; it is never removed, and it leaves the queue when it finally comes back translated. The demotion is what stops a repeatedly omitted field holding the head of the queue, and it is safe to retry because at temperature 0 the input is still not identical between attempts — the batch's other fields change, and the activation levels with them — so one miss is not evidence of permanent failure. The miss logs one line naming the row and its new priority, which is the only trace that a field is being retried rather than translated. A reply yielding **no usable block at all** is a failure instead: it raises, which is what puts the backoff in charge rather than re-sending the same request in a tight loop. A whole-batch failure is not content-related, so it demotes nothing: every row is left exactly as it stands, at the priority it had, and the same batch is rebuilt and retried. API failures log an error and retry on the next cycle.

### The superseded flag-as-queue producer (removed)

The mirror was once the queue itself. `flag_for_translation` set
`translation_priority` on a `workshop_items` or `creators` row and nothing else, and
`get_next_translation_item` scanned both tables by that flag, so raising the flag
was a complete producer and `_build_user_record` calling it directly was correct.
The per-field `translation_queue` replaced that scan, and once nothing in `src/`
called either function — only their own tests did — both were deleted. The current
design queues a field with `queue_field_for_translation` and derives the mirror
from the queue, so raising the mirror without a queue row is the stranded state
migrations 22→23 and 27→28 repair. The regression that followed from leaving the
producer unported is why migration 27→28 exists; see
[schema-migrations.md](schema-migrations.md).

### `queue_field_for_translation` (database)

Inserts or bumps an entry in `translation_queue`, and also raises the parent row's `translation_priority` via `MAX`. Checks if the field already exists in the queue; if so, bumps its priority (never downgrades). If new, inserts with the given priority and `queued_at = now`. This is the **only** producer: it queues item fields and, since issue 45, a creator's `personaname_en` as well, called by `_store_user_record` after the profile upsert (the mirror needs the `creators` row to exist).

**Both writes happen in one transaction on one connection.** They used to run on two connections, and the translator drains the queue on its own thread: a drain landing between them deleted the row and zeroed the mirror, after which the second write raised the mirror again with nothing queued behind it. The item then read as permanently pending, because every producer skips a translation that is already current, so nothing ever re-queued the field to clear it. Migration 22→23 repairs the rows the old helper stranded.

### `raise_translation_priority_for_list` / `raise_translation_priority_for_detail` (database)

Called when items are displayed in the list or detail view. For each non-ASCII text field (title, short_description, extended_description), checks if the `_en` translated counterpart is already populated. If not, flags the field for translation at priority 5 (list) or 10 (detail). This ensures viewed items get translated promptly.

### What queues a field for translation

Five events add a field to `translation_queue`. Four of them apply the same
freshness rule; they differ only in which fields they consider and at what
priority. The fifth, a creator's name, is the exception and says why below.

| Trigger | Code path | Fields | Priority | Skips a current translation? |
|---|---|---|---|---|
| Daemon enriches an item via the API | `daemon.py`, `_queue_translations` (from `_process_item`) | `title_en`, `short_description_en` | `max(3, requested)` | Yes |
| Web scrape succeeds | `web_worker.py`, `WebScraperThread` | `extended_description_en` | 3 | Yes |
| Item appears in a list | `raise_translation_priority_for_list` (TUI list load, `POST /api/search`) | all three | 5 | Yes |
| Item opened in the detail pane | `raise_translation_priority_for_detail` (TUI selection, `GET /api/item/<id>`) | all three | 10 | Yes |
| Daemon refreshes a creator's profile | `daemon.py`, `_store_user_record` (from `_refresh_creators`) | `personaname_en` | 1 | **No** |

Two conditions apply to every trigger:

- **Only non-empty, non-ASCII text is queued.** `queue_field_for_translation` returns immediately
  for empty or ASCII text, so an ASCII-only title is never sent anywhere.
- **Priority only rises.** Flagging a field already in the queue with a higher priority updates the
  entry; a lower priority is ignored. It is never downgraded.

A field is queued when it has no translation **or** its translation is out of date.
`translation_is_current` states the rule: a translation is current when the `_en` value exists and
its `translate_version` is not older than the item's `steam_updated_at`. The translator stamps
`translate_version` with `steam_updated_at` at translation time, so a source edit makes the
translation stale and it is re-queued; unchanged text is left alone. See
[timestamps.md](timestamps.md).

**A creator's name is the exception to that freshness rule, deliberately.** A
creator has no `steam_updated_at`, and the only clock the rule could compare —
`api_fetched_at` — moves on every profile refresh, so "current" cannot survive the
very refresh that calls the producer. Skipping there would leave the Creator
Translation bar counting a name as untranslated with nothing queued behind it, so
`_store_user_record` queues every non-ASCII name it is handed, right after the
profile upsert (`queue_field_for_translation` raises the mirror in the same
transaction as the queue row, so the `creators` row must exist first). The cost is at
most one re-translation per creator per refresh cycle, and a profile is only
refreshed when an enriched item proposes its creator and the profile's
`api_fetched_at` is older than `creator_staleness_days` (default 90).

This replaced a pair of defects. The two background triggers used to check only that the text was
non-ASCII, while the two user-view triggers also checked for an existing translation. Since a
successful translation **deletes** its queue row, `queue_field_for_translation`'s "already in the
queue" check offered no protection afterwards — so with the staleness sweep returning every
`fetch_status = 200` item to the fetch queue about monthly, an enriched item with a non-ASCII title was
re-translated roughly monthly whether or not anything had changed. Meanwhile nothing compared the
version keys, so *changed* text was never refreshed either. Both are fixed; the freshness rule is
what keeps the two from contradicting each other.

Because the version key is per item, not per field, a change to any Steam-visible field re-queues
all non-ASCII fields of that item. That is coarser than strictly necessary, but it is the
granularity of the only version stamp that exists.

---

## Display & Search Visibility

Items become visible in search once they have `fetch_status = 200` (API details fetched) and their metadata is in the database. The TUI and Web UI both call `search_items` with structured filters.

### Summary fields

The `summary_only` SELECT returns: `workshop_id, title, title_en, creator_steamid, consumer_appid, translate_version, is_queued_for_subscription, web_scrape_priority, image_priority, translation_priority, file_size, image_answer, wilson_subscription_score, wilson_favorite_score, personaname, personaname_en`. Tags are returned via a subquery joining `workshop_tags` and `tags` as a comma-separated string.

### Detail fields

`get_item_details` returns `w.*` (all columns) plus `personaname`, `personaname_en`, `user_translated_at`, and tags from the junction table.

---

## Stage Handoffs

Every item is, at all times, in exactly one state:

* queued for the API fetch (`api_priority > 0`), or
* queued for a web scrape (`web_scrape_priority > 0`), or
* queued for an image (`image_priority > 0`), or
* queued for translation (a `translation_queue` row exists; `translation_priority > 0` is the
  item-level mirror of it), or
* complete for the stage that owns it, or
* deliberately dead (`fetch_status = -1`) and therefore in **no** queue.

Each stage hands an item to the next by writing the column the next stage's own query selects on. The
contract is enumerated here because it is otherwise written down only in the two functions that happen
to implement it — the producer that writes the column and the consumer that reads it:

| Handoff | Producer writes | Consumer selects on | Predicate function (`src/database.py`) |
|---|---|---|---|
| Discovery → API fetch | `api_priority = 3` for a cursor-discovered item, `5` for a page-discovered new or changed one, with `fetch_status` and `api_fetched_at` left NULL (`seed_database`, `_run_page_discovery`) | `get_next_items_to_fetch`: `api_priority > 0 AND (fetch_status IS NULL OR fetch_status != -1)` | `api_fetch_queue_predicate()` |
| API fetch → web scrape | `fetch_status = 200`, `api_fetched_at = now`, then `raise_web_scrape_priority(max(3, requested))` — or `max(1, requested)` for an item the enrichment filters exclude (`_raise_scrape_and_image_priorities`) | `get_next_web_scrape_item`: `web_scrape_priority > 0`. The worker's own success test is that it stored a description, not merely that the flag is clear | `web_scrape_queue_predicate()` |
| API fetch → image | `raise_image_priority(max(3, requested))`, or `max(1, requested)` when the filters exclude, whenever `preview_url` is present (`_raise_scrape_and_image_priorities`) | `get_next_image_item`: `image_priority > 0`; `image_answer` records the answer, so a permanent `404` or a non-image type also settles the stage | `image_queue_predicate()` |
| API fetch and web scrape → translation | `queue_field_for_translation` raises the item's `translation_priority` with `MAX` and inserts a `translation_queue` row for each non-ASCII `title`/`short_description` (`_queue_translations`) or `extended_description` (`WebScraperThread`) | `get_next_batch_for_translation`: any `translation_queue` row; the translator clears the mirror to `0` when the item has no queue entries left | `translation_queue_predicate()` (the poll's; `translation_priority_predicate()` is the item-level mirror, and the invariant asks the queue row as well) |
| any stage → dead | `_settle_api_failure` on a permanent `404`: `fetch_status = -1`, `api_priority`, `web_scrape_priority`, `image_priority` and `translation_priority` all cleared, and every `translation_queue` row for the item (`entity_type = 'item'`) deleted in the same transaction | every queue predicate. The web and image polls select on their flag alone and the translation poll on the row, all with **no** dead-item guard, so the producer's clear is what keeps a dead item out; the fetch queue also tests `fetch_status != -1` on its own | `queued_anywhere_predicate()` (the four flags **or** a `translation_queue` row) |

Each predicate is a named function, not a copy of its SQL: the worker poll
interpolates the fragment into its statement and the tests in
`tests/test_handoff_contract.py` interpolate the same fragment scoped to one row,
so the two cannot drift. A test that restated the SQL would prove only its own
copy. The functions return the `WHERE` fragment and nothing else — ordering and
the limit stay in the selector, because they are the hot path of four stages and
`tests/test_queue_indexes.py` pins their query plans. The contract tests drive
the real producers (`seed_database`, `_process_item`, `_settle_api_failure`) and
assert, for each handoff, either that the consumer selects the item because the
work is outstanding or that it does not select it and the stage's output is
stored; not selected and nothing stored is the bug the counters exist to notice.

The API refresh is the change detector: it is the cheapest call and the only stage that goes stale on
a timer, so a dependent stage is re-queued because its source changed or its output is missing, never
because time passed (see [Change detection across stages](#change-detection-across-stages)).

Two statistics watch the invariant, each meant to read zero:

* `queued_nowhere` — live items in no queue that the pipeline never completed. It is the shape of
  issue 19 (dequeued as scraped with no description stored) and issue 20 (discovered with no fetch
  priority). A live item with a `translation_queue` row is queued, so it is not in this population
  even when its `translation_priority` mirror reads zero.
* `dead_queued` — dead items a work queue would still select: a queue flag left set (issue 17) or a
  `translation_queue` row the poll still holds (issue 66).

`dead_queued` and `dead_items_by_queue` are one question at two resolutions, and both are wanted:
`dead_queued` is the scalar that must read zero, and `dead_items_by_queue` is the per-queue breakdown that says
where the item is held, so a non-zero reading points at the queue to look in. Neither replaces the
other — the scalar is the invariant, the breakdown is the diagnosis — and the two must agree: the
breakdown's `translation` column counts the mirror **or** a `translation_queue` row, the same test the
scalar's union makes, so an item held only by a row is named in both.

Both count; neither repairs. The consumer's predicates are named functions shared
with the handoff contract tests, so a divergence between a predicate and the
producer's write is now caught at the handoff rather than inferred from these two
numbers; the counters remain how the same divergence is noticed in the field,
where no test is running. The producers that mark an item dead clear its
`translation_queue` rows in the same transaction as the status write, and
migration 35→36 removed the rows already stranded at the time of the fix (910
dead items held 1,016 rows on the v35 snapshot, each with its mirror already
cleared), so no dead item's fields are translated or paid for.

---

## Coverage bars

The `coverage` statistic (`src/metrics.py`, `_coverage`) answers one question per stage: how much of
the live library the stage has reached, at two scopes. It is drawn as seven bars in pipeline order.
Dead items are excluded, because they can never be covered and counting them would make coverage fall
as the library is cleaned up. Each bar's **maximum** is its reachable population and its **fill** is
the share whose output is stored and current; the two are drawn on the bar's own track, and drawing
the fill against the track rather than against the population is what stops a bar promising work that
cannot exist.

A bar's track is 100% of the unit its stage works in, which is not always the item. The item-scale
bars (API Data, Extended Web, Images) count live items. The three **translation bars** are drawn in
**translation slots**, the unit the queue works in, because their work is per field: two slots per
entry for the API's `title`/`short_description`, one per described item for the scraped description,
one per author for the creator name. A translation bar therefore has a third part — a **gray segment**
for the slots that need no translation at all — and its short note is the share that does need one
(for example "25% need translation", `need / slots`). The percentage, the gray share and the note are
all computed in `src/metrics.py` and carried in the payload, so the terminal and the browser draw the
same segments and print the same words. The two **Creator** bars are drawn in **author units**: a
persona lives once per creator and is shared by every item that creator made, so counting items under
the "Creator" label printed roughly the item count (about 3 M on production) where the population is
the scope's unique authors (2,459,796 live items gave 849,695 authors, 34.5%, of which 26,555 had a
fetched persona).

| Bar | Counts | Population — the same test that flags the work |
|---|---|---|
| API Data | live items with `api_fetched_at` | every live item |
| Translations (subsidiary) | **translation slots** — two per entry, `title` and `short_description` | the non-empty non-ASCII fields of the **filter-selected** items, because `_queue_translations` returns early unless the item was enriched. A field is filled when `translation_is_current` — stored and taken at the item's current `steam_updated_at`. The track is 2 × the scope's live items, so 100% is two translations per entry |
| Extended Web | live items with a non-empty `extended_description` | every live item except the pages that answered with no description (a scrape that stored an empty description); that legitimate-blank count and the resulting ceiling are printed with the bar, which is not a translation bar and keeps its sentence about them |
| Extended Web Translation (subsidiary) | **one slot per described item**, filled when its non-ASCII `extended_description` has a current `extended_description_en` | any **scraped** item with a non-ASCII description, not only the filter-selected ones: `WebScraperThread` flags the description regardless of enrichment. A non-ASCII description is a description, so this bar can never be longer than Extended Web above it; the ASCII descriptions are its gray segment |
| Images | live items with a recorded `image_answer` | every live item; a recorded answer settles the stage even when the preview does not exist |
| Creator | **authors** — distinct `creator_steamid` of the scope's live items, filled when we hold a `creators` row for the author | the scope's unique authors |
| Creator Translation (subsidiary) | **authors** whose `creators.personaname` is non-ASCII and whose `personaname_en` is current | one slot per author in the scope; a name that is ASCII, or an author we have never fetched, needs no translation and is the gray segment. Currency compares our clocks, `translated_at >= api_fetched_at`, because a creator has no `steam_updated_at` |

The three translation bars have **three different scopes** deliberately, because the code that feeds
them does. Each population is the flagging rule written in SQL — non-empty and non-ASCII
(`queue_field_for_translation` returns early on an empty or ASCII field), translated and current
(`translation_is_current`) — so a bar and the work it measures cannot disagree. Where a rule is
Python today and the metric is its SQL translation, the metric's docstring says so, the same way the
filtered figure already did for the enrichment filters. A population of zero is a legitimate answer
and reads "Nothing to translate" on both front ends rather than as a percentage that never moves;
because nothing needs translating there is no note and no gray segment, just the empty track.

The **second scope** restricts every bar to the items the target AppIDs' `enrichment_filters`
select, the population the daemon calls *enriched*. The Translations bar's population is already
exactly that set, so only its track changes between the two blocks — it does not follow the scope, it
*is* the scope. The whole-library view is the one whose Translations bar is the stage's reachable
share of the entire library.

**Cost.** Each scope is one pass over its live items (`_coverage_scan`), with a `LEFT JOIN creators` on
the primary key for the creator bars and the non-ASCII tests evaluated in SQL. No index was added:
the filtered scope still reaches its rows through the `consumer_appid` index, and the join is a
primary-key lookup per item.

---

## Queue state: outstanding, rate and time to drain

The `queue_eta` metric answers, for all four queues, **how much is outstanding,
how fast it has been draining, and how long it will take**. It is deliberately
computed from whatever history exists, so it appears as soon as the completion
clocks record anything rather than after a "stable" rate has accumulated. Both
front ends render it identically: `outstanding`, a `per_day` rate, and a
`53d ± 30%` time to drain.

| Queue | Outstanding predicate | Completion clock | Rate |
|---|---|---|---|
| API fetch | `api_priority > 0` (and live) | `api_fetched_at` | **net** of the staleness sweep |
| Web scrape | `web_scrape_priority > 0` (and live) | `web_scraped_at` | gross |
| Image | `image_priority > 0` (and live) | `image_fetched_at` | gross |
| Translation | `translation_priority > 0` (and live) | `translated_at` | gross |

Outstanding depth excludes dead items (matching `priority_breakdowns`): a dead
item can never complete, so leaving it in would promise a drain that cannot
happen. Completion counts are not filtered by liveness.

### Active time, not wall-clock

A rate over wall-clock **falls every time the daemon is switched off**, which is
not a slowdown. The `.pauselock` intervals are therefore recorded in
`.daemon_state.yaml` beside the database (`src/activity.py`) and subtracted from
the window. The lock's own absent/present edge is the signal, so the three
writers — the TUI's subscription screen, `POST /api/pause`, and the subscribe
engine's `PauseLock` — may nest without the same paused second being subtracted
twice; an interval still open is counted up to now, so a pause *in progress*
still leaves the rate computable from the active time before it.

The pause is applied **per queue, because the pause is per queue**: only the web
and image workers poll `.pauselock` (`src/web_worker.py`, `src/image_worker.py`),
while the API fetch loop and the translator are not gated by it. Subtracting the
pause from a stage that kept working would overstate its rate, so the API and
translation rates use the full window.

### The API rate is net; the other three are gross

The staleness sweep pushes items back into the API queue, so the API queue's
completions are reduced by the sweep's recorded rowcount for runs inside the
window — the one inflow the project measures.

**Discovery's inflow into the API queue is not counted.** `first_seen_at` is our
clock, but it is not indexed on `workshop_items`, and a window count over it
would be a full scan of a multi-million-row table; the API figure subtracts the
sweep alone.

**The web, image and translation rates are gross, not net**: their inflow is the
items an API refresh re-flags, which nothing records on our clock. A gross rate
must not be read as a time to empty, and both front ends mark those rows
`(gross)` for exactly that reason.

### The uncertainty

The completions inside the window are modelled as a Poisson count. The rate is
`completed / active_seconds`, and the relative standard error of that count is
`100/sqrt(completed)` — a percentage, never an absolute span, and one form only
(`53d ± 30%`). It is wide while the evidence is thin (100% at one completion,
50% at four) and narrows as completions accumulate (10% at a hundred). A queue
with **no completions in the window gets no rate**: `per_hour`, `per_day`,
`eta_seconds` and `uncertainty_pct` are `NULL`, shown as "no rate yet" beside the
outstanding depth. That case is not the same as an empty queue, whose time to
drain is a real zero.

### Cost

All four outstanding counts reach their rows through the queue's own partial
index (`idx_api_queue`, `idx_web_scrape_queue`, `idx_image_queue`,
`idx_translation_priority`), and the completion counts through the partial
completion indexes migration 26→27 added. No new index was required.

---

## Item Lifecycle State Machine

```
[Discovered: fetch_status=NULL, api_fetched_at=NULL, last_fetch_attempted_at=NULL]
    │
    ▼ seed_database / _run_page_discovery
    │
[Discovered: workshop_id exists, no metadata]
    │
    ▼ process_batch: get_workshop_details_batch
    │
[Fetched: fetch_status=200, api_fetched_at=now, has title/tags/preview_url]
    │
    ├─► Web Scraper (if enriched): scrape_extended_details
    │      │
    │      ▼
    │   [Scraped: extended_description populated, web_scrape_priority=0]
    │
    ├─► Image Download (if preview_url): ImageDownloadThread
    │      │
    │      ▼
    │   [Image: image_answer set, image_priority=0]
    │
    └─► Translator (if non-ASCII): TranslatorThread
           │
           ▼
        [Translated: title_en, etc. populated, translation_priority=0]
```

Each thread operates independently. The web server's `_ensure_image_flagged` sets `image_priority=5` for list-viewed items and 10 for the detail view, and the daemon calls `raise_image_priority(max(3, requested))` for newly discovered items with a `preview_url`, where `requested` is the user-requested part of the item's pre-fetch `api_priority` (`user_requested_priority`).

---

## Wilson Score Computation

### `wilson_lower` (daemon)

Implements the Wilson score interval lower bound for binomial proportions. Formula:

```
p = successes / trials
z = 1.96 (95% confidence)
denominator = 1 + z²/trials
numerator = p + z²/(2*trials) - z * sqrt(p*(1-p)/trials + z²/(4*trials²))
return max(0, min(1, numerator / denominator))
```

Used to compute `wilson_favorite_score` (using `favorited / lifetime_subscriptions`) and `wilson_subscription_score` (using `subscriptions / lifetime_subscriptions`). The favorite metric measures engagement intensity — how many current favorites exist per all-time subscription. The subscriber metric measures retention — what fraction of lifetime subscribers remain subscribed. Both scores are REAL values between 0 and 1, stored with `NULL` default.

### `compute_wilson_cutoffs` (database)

Computes p99, p90, p50 percentile thresholds for both Wilson scores across the filtered dataset. Uses SQLite's `NTILE(100)` window function to divide scores into 100 buckets (sorted descending, nulls last). Returns the minimum value in buckets 1, 10, and 50 as the p99, p90, and p50 cutoffs respectively. These are used by both UIs for color-coded score display (gold for top 1%, yellow for top 10%, white for top 50%, gray below).

Filters for the cutoffs exclude any filter with the `percentile` operator (to avoid circularity — the percentile query can't reference itself) and route tag filters through `_build_tag_clause` (junction table).
