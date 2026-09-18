# Data Pipeline

The data pipeline moves a Steam Workshop item from initial discovery through enrichment stages to user-facing display. An item progresses through: discovery → API detail fetch → web scraping → image download → translation → search visibility.

---

## Discovery Phase

### `process_batch` (daemon)

The main loop entry point called repeatedly by `run()`. Each invocation:

1. Calls `get_next_items_to_scrape` to retrieve up to `batch_size` items due for processing. Selection is `api_priority > 0` and `status` not `-1`, ordered by `api_priority DESC, api_fetched_at ASC`, so never-successfully-fetched items (`api_fetched_at IS NULL`) come first within a priority band.
2. If no items are available, waits for the discovery thread to refill the queue. Discovery is no longer the main loop's job: it runs on its own thread (see [threading.md](threading.md)) so that the queue is refilled while the loop is still draining it, rather than only after it has drained. The wait is woken by the thread's signal instead of polling the database, and still gives up after ten minutes so the outer loop re-checks.
3. Fetches metadata for the whole batch in one Steam Web API request via `get_workshop_details_batch` (title, description, tags, file_size, preview_url, creator, subscriptions, etc.), then processes the items **in the order the queue returned them**, matching each to its result by `publishedfileid`. The batch is chunked into several requests only if `batch_size` exceeds the endpoint's per-request id ceiling (`STEAM_API_MAX_IDS_PER_REQUEST`, 100).
4. Merges API data with existing DB row via `_merge_and_clean_api_data`, which filters to `MERGE_ITEM_KEYS` (derived from `WORKSHOP_ITEM_COLUMNS`), remaps `creator_app_id`/`consumer_app_id` to `creator_appid`/`consumer_appid`, remaps `description` to `short_description`, and remaps the API's `time_created`/`time_updated` to `steam_created_at`/`steam_updated_at`. Unknown API keys are discarded with a log message.
5. Computes Wilson scores via `wilson_lower` (a binomial-proportion confidence interval using a 95% z-score of 1.96). Sets `wilson_favorite_score` from `(favorited, lifetime_subscriptions)` and `wilson_subscription_score` from `(subscriptions, lifetime_subscriptions)`.
6. Evaluates enrichment filters via `_should_enrich`. Checks the stored `enrichment_filters` for each AppID against the item using `_evaluate_filters` (an in-memory filter evaluator that mirrors the SQL builder's semantics). If no filters are configured, all items are enriched.
7. If enrichment is approved, calls `flag_for_web_scrape` at `max(3, requested)` -- and `flag_for_image` at the same priority if `preview_url` is present -- where `requested` is the part of the item's pre-fetch `api_priority` a *user* asked for (`user_requested_priority`; `5` and `10` only). An item the filters exclude is still scraped, but at `max(1, requested)`: the filters choose priority, not membership, and an excluded item must not outrank a selected one ([data-model.md](data-model.md#queue-priorities)). Sets `status = 200`. Calls `insert_or_update_item` to persist. The merge sets `api_fetched_at = now_ts` and `api_priority = 0`; `last_fetch_attempted_at` was already stamped on entry.
8. For enriched items, flags `title` and `short_description` for translation via `flag_field_for_translation` at `max(3, requested)`. That function inserts into `translation_queue` and also raises the parent row's `translation_priority` (using `MAX`, so it never downgrades); the translator clears it to 0 when the item has no queue entries left. Users with non-ASCII names get `translation_priority = 1` set via `_build_user_record`.
9. After the batch, refreshes the batch's creator profiles in **one** `get_player_summaries` call: the distinct creators proposed by enriched items whose `users` row is missing or older than `user_staleness_days`. This was one request per item; the "only for enriched items" and staleness rules are unchanged.

**Missing ids**: `get_workshop_details_batch` keys results by each entry's `publishedfileid`, never by position, and ignores duplicate or unrequested ids. A requested id the response omits is reported as `404` — the same not-found the single-item path gives for an empty or `result != 1` entry — so it is settled as a permanent failure instead of being silently skipped at the front of the queue.

**Error handling**: the batch helper returns `None` when the *request* failed — a transport error, a timeout, an HTTP error such as 429, or a body that is not JSON — and the daemon settles every id it carried as a temporary `500`. It returns a mapping (an empty successful response included) when the request returned and parsed. The daemon maps a per-item `404` to `status = -1` (dead), clears every queue flag so the item is in no queue, and persists a `500` as `status = 500` for a later attempt. Both paths stamp `last_fetch_attempted_at`; the `500` path leaves `api_fetched_at` untouched. `get_workshop_details_api(item_id)` remains the one-id spelling and returns the same shapes.

**Dynamic delay**: `api_delay` is driven by **requests**, not items — one change per batched POST, never per item. A request that returns and parses is a success whatever its individual results say (a batch of 50 of which 10 are "not found" is a completely successful API call), and it resets the failure streak. The rule is TCP congestion control, not a safety net: every refused request doubles the delay and healthy operation walks it back down, so the client converges on the fastest rate Steam will sustain — a limit that is not published and may move. Steady state is a sawtooth around that rate.

The walk back down is measured in **time**, not in successful requests: the delay halves for every `pacing.HALF_LIFE_SECONDS` (600 s) of healthy operation, so it recovers over the same wall-clock window whatever its size. A request count cannot express that — one request is a different amount of time at every delay — which is why all three rate-seeking queues share `src/pacing.py` rather than each keeping its own arithmetic. The clock is in memory only, so a daemon restarted after a day resumes at the delay it had reached instead of treating the downtime as healthy operation.

It is floored at `API_DELAY_FLOOR = 0.01 s` and has **no ceiling**. One was kept while the back-off could still be moved by the wrong signal, but it also caps convergence — a sustainable rate above the cap can never be reached — and an uncapped delay cannot run away: it doubles only when an attempt fails, and the next attempt is a whole delay away, so after k refusals the delay is `d0 * 2**k` while the elapsed time to reach it is only `d0 * (2**k - 1)`. The delay tracks the length of the outage rather than outrunning it, which is why no bound is needed. Batch size is deliberately not folded into the delay: a refusal costs one doubling whatever ids it carried, so if Steam meters the limit per item the converged *call* delay settles at roughly N times the per-item cost and the item throughput is unchanged. The delay is a literal inter-call pause, not a target rate. It is persisted after a back-off immediately — a restart during an outage must not resume at the refused pace — and after a decay only once it has moved `pacing.PERSIST_STEP_SECONDS`, with only the persisted copy rounded to two places.

### `_promote_stale_items` (daemon)

Promotes successfully-fetched items whose `api_fetched_at` is older than `item_staleness_days` from `api_priority = 0` back to `1`, returning them to the fetch queue. It is a full-table UPDATE, so `_maybe_promote_stale_items` runs it at most once per `STALE_SWEEP_INTERVAL_SECONDS` (1 hour, monotonic clock) instead of on every batch; the first batch after startup always sweeps, so a long-idle daemon does not sit on a stale queue.

### `seed_database` (daemon)

Cursor-based discovery using `IPublishedFileService/QueryFiles` with `query_type=1` (rank by publication date, newest first). For each target AppID:

- Resumes from the last stored cursor (`app_tracking.last_cursor`), or `*` for the first page.
- Fetches `numperpage=100` items per request. Each item's `publishedfileid` is inserted into `workshop_items` as a bare row at `api_priority = 3`, the documented new-item priority (status NULL, no metadata). The priority is passed explicitly rather than left to the column default, because that default is not stable across database histories: `CREATE TABLE` declares `DEFAULT 3` while the `ALTER TABLE` in migration 11→12 gives an existing database `DEFAULT 0`, so leaning on it queues discovered items on a fresh database and strands them on a migrated one (the fetch queue selects `api_priority > 0`). `_run_page_discovery` uses `5` because it handles new *and changed* items that should refresh as if visible; cursor discovery finds genuinely new items, so it uses the documented `3`.
- Stops when `target_new` unscraped items are accumulated, or when the cursor returns empty (no more pages).
- Persists the cursor after each page via `update_app_tracking_cursor`.
- When the cursor is empty after a successful scan, sets `_cursor_exhausted = True`, enabling the page-based discovery mode.

This is called when `get_next_items_to_scrape` returns empty — meaning the processing queue is drained and new items need to be discovered.

Discovery is skipped while the daemon already has enough outstanding work: for each target AppID,
`seed_database` returns early when at least `target_new` (100) items are fetchable — queued and not
dead, the population `get_next_items_to_scrape` selects. The guard exists so that a healthy backlog
is not re-crawled. It deliberately does not count items that have never been fetched: those may not
be queued at all, and reading them as outstanding work once suppressed discovery permanently while
the fetch queue held a single item.

### `_run_page_discovery` (daemon)

A periodic alternative to cursor-based discovery. Enabled when a `.fetch_new` trigger file exists, when `_cursor_exhausted` is True, or when at least 500 items have been scraped (`api_fetched_at IS NOT NULL`). Runs at most once per 24 hours (tracked via `_last_page_discovery`), unless the trigger file bypasses the cooldown.

Uses `query_workshop_page_updated`, which calls QueryFiles with `query_type=21` (rank by last updated, most recent first) and cursor-based pagination. It walks up to 500 pages per AppID, comparing each item's returned `time_updated` against the stored `steam_updated_at` and upserting new or changed items as bare rows at `api_priority = 5`. Stops when a page yields no new or changed items.

After page mode completes, the daemon resumes normal cursor-based discovery.

### `query_workshop_files` (steam_api)

Calls `IPublishedFileService/QueryFiles/v1/` with cursor-based pagination. Parameters: `query_type=1` (publication date), `cursor`, `numperpage=100`, `appid`. Returns `{total, items, next_cursor}`. Rate-limited via `_rate_limit()`.

### `query_workshop_page_updated` (steam_api)

Same API endpoint but with `query_type=21` (last updated) and cursor-based pagination. Used exclusively by `_run_page_discovery`. Returns the same `{total, items, next_cursor}` shape.

---

## API Detail Fetch Phase

### `get_workshop_details_batch` (steam_api)

Calls `ISteamRemoteStorage/GetPublishedFileDetails/v1/` once for many ids — `itemcount=N` with `publishedfileids[0..N-1]` — and returns a `{id: detail}` mapping. Results are matched by each entry's `publishedfileid`, never by position, so a reordered, duplicated or extended response cannot mis-assign a result. A requested id the response omits is filled in as `{status: 404}`. Returns `None` when the request itself failed (transport, timeout, HTTP error, or unparseable body); that is the signal the daemon backs off on. The per-request ceiling is `STEAM_API_MAX_IDS_PER_REQUEST = 100`, taken from the documented `GetPlayerSummaries` limit and applied to `GetPublishedFileDetails` too, which publishes no cap.

### `get_workshop_details_api` (steam_api)

The one-id spelling of the batch call, kept for existing callers. Returns `{status: 500}` on a request failure and `{status: 404}` when the item is not found or `result != 1`. Returns the raw detail dict otherwise, containing the API's `title`, `description`, `tags`, `file_size`, `preview_url`, `creator`, `subscriptions`, `favorited`, `views`, `time_created`, `time_updated`, and more (these are the raw API field names; `_merge_and_clean_api_data` renames some of them before storage).

### `_merge_and_clean_api_data` (daemon)

Merges API response data into the existing DB row. Applies column-name remapping (`creator_app_id` → `creator_appid`, `description` → `short_description`, `time_created`/`time_updated` → `steam_created_at`/`steam_updated_at`). Filters to `MERGE_ITEM_KEYS` (derived from `WORKSHOP_ITEM_COLUMNS`) to prevent unknown API columns from polluting the DB, and discards known-but-handled-externally keys (for example `needs_web_scrape`, `image_extension`, `needs_image`). Normalizes tags via `normalize_tags`. On the success path it stamps `api_fetched_at = now_ts` and `api_priority = 0`.

### `_should_enrich` (daemon)

Checks whether an item passes the enrichment filter for its AppID. Reads `enrichment_filters` from `app_tracking` (a JSON array of filter dicts in the same format as the TUI search builder). Feeds the item dict through `_evaluate_filters`, which uses `_evaluate_single_filter` for each criterion and `_evaluate_tag_filter` for tag-based filters. Returns True if no filters are configured for the AppID (enrich everything).

### Failure classification (daemon)

`_settle_api_failure` turns a non-success outcome into a queue decision. `404` is permanent: the failure is logged, the item is marked dead (`status = -1`) and it is removed from **every** queue — `api_priority`, `needs_web_scrape`, `needs_image` and `translation_priority` are all cleared, because a dead item can never complete and a queue flag left set would strand it in a queue that never drains. Everything else is temporary — `500` (including every id of a request that failed and was settled as `500`), transport exceptions, and any status no branch handles. Those keep the item queued at one priority level lower, floored at `1`, because priority `0` means "not queued" and clearing it is what previously stranded transient failures with nothing able to bring them back. Unhandled statuses are captured as evidence and never fall through to the success path.

These per-item outcomes never touch `api_delay`. A batch request that returned and parsed is a success even when some of its items settle here, so only `_fetch_details` — which counts the request once — moves the delay; see the delay rule above.

### Change detection across stages

The API refresh is the change detector. It is the cheapest call and the only stage that goes stale on a timer, so the stages hanging off an item follow it. `_flag_scrape_and_image` compares the pre-fetch `steam_updated_at` against the freshly merged one: when they match it reuses the stored extended description and leaves the image alone, and when they differ it re-queues both. Translation does the equivalent with `translate_version` — see [What queues a field for translation](#what-queues-a-field-for-translation).

A stage is therefore re-queued because its source changed, or because its output is missing — never because time passed. Per-queue staleness sweeps are deliberately not used. An item whose stored revision is unknown (`steam_updated_at` NULL) counts as changed, since no change can be ruled out.

---

## Web Scraping Phase

### `WebScraperThread` (web_worker)

A daemon thread that picks up items from `get_next_web_scrape_item`, ordered by `needs_web_scrape DESC, api_fetched_at ASC` (highest priority first, oldest-fetched within priority). For each item:

1. Calls `scrape_extended_details(url)` which fetches the Steam Community workshop page and parses the extended description and tags.
2. If the description was found, updates `extended_description`, sets `needs_web_scrape = 0`, and records `scrape_version = steam_updated_at`. The tags the scraper returns are not persisted; tags in the database come from the API.
3. Flags non-ASCII `extended_description` for translation at priority 3, unless its translation is already current (see [What queues a field for translation](#what-queues-a-field-for-translation)).
4. If the request could not be completed at all — a transport failure, which `scrape_extended_details` reports as `None` — raises `api_priority` to 2 so the metadata is re-fetched (that value has no other source); nothing is cleared, so the item stays in the scrape queue.
5. Otherwise `classify_scrape` names the outcome and the run loop responds to it (see [Outcome taxonomy](#outcome-taxonomy)). Every non-success outcome that carries a served page is captured as evidence, and a miss is never a blanket failure or an empty success: the wording and markup decide what the queue learns. See [failure-capture.md](failure-capture.md).

#### Outcome taxonomy

`classify_scrape` names one outcome per attempt and the run loop switches on it, so which outcome buys which response is one readable table rather than a chain of conditions.

| Outcome | Recognised by | Item's queue flag | Pacing response |
|---|---|---|---|
| `SUCCESS` | `description` is not `None` | `needs_web_scrape = 0` | success: resets the failure streak, can decay the delay |
| `RATE_LIMITED` | the body reports "too many requests" | untouched | the delay doubles, at once |
| `ITEM_MISSING` | HTTP 404/410, or the page's item-error wording | `needs_web_scrape = 0` | no back-off |
| `ITEM_PAGE_WITHOUT_DESCRIPTION` | `workshopItem` present, `highlightContent` absent | `needs_web_scrape = 0` | neutral: neither success nor failure |
| `GATE` | no item markup, plus an age-check, sign-in or error marker | untouched | no back-off; `_retry_if_gated` has already retried if the cookie changed |
| `UNKNOWN` | a transport failure, a 5xx, or a page that is neither the item's nor a recognised condition | untouched (a transport failure also raises `api_priority` to 2) | grows `web_delay` |

A 5xx is `UNKNOWN` whatever its body says: the status is a server fault with no attributable cause, so it keeps the back-off.

**A missing item.** A live probe found that the Workshop serves its item-error page with **HTTP 200**, not 404 — a well-formed but absent id returned "There was a problem accessing the item", and a malformed id returned "That item does not exist" — so the status is not trusted and the wording is matched as well (`looks_like_missing_item`). The worker clears `needs_web_scrape` but deliberately does **not** mark the row dead: existence is the API's call, and the API makes it on its own 404. Clearing the flag is the conservative move — the API re-flags the item while its description is still missing if Steam ever serves it again — and it is what stops a gone item spinning in the queue at full pace now that it no longer backs off. Both the status (when there is one) and the matched wording are logged, and the page is captured as evidence, because there was no capture of this page before. A definitive HTTP 404/410 is not retried as a gate — no credential materialises a gone item — while the HTTP 200 wording still gets the one session-recovery retry, since there the status proves nothing.

**Dynamic delay**: The shape is shared with the other queues (`src/pacing.py`) even though the unit is not: a page scrape is one request per item and cannot be batched, so the worker keeps its own `web_delay_seconds`. Every refusal doubles the delay and healthy operation halves it for every 600 s it has been running, so the web scraper recovers over the same wall-clock window as the API and the image worker. The old per-item 100-success / 2-failure compounding rule is gone: a success count is a different amount of time at every delay, so it made the worker recover faster the faster it was already going.

Only an **unknown** outcome — a transport failure, a 5xx, or a page that is neither the item's nor a recognised condition — is unattributable, and it needs two consecutive ones after a streak before the delay moves. A **rate limit** is the budget itself speaking, so it doubles the delay immediately rather than waiting for a second strike. A missing item and a description-less item page are answers the item gave; a gate is a session problem a slower pace cannot fix. None of those reaches the failure counter, so the scraper is not slowed for a reason a lower request rate could not address. The item page with no description must not count as a success either: it yielded nothing, so counting it as one would let the delay fall again while unknown outcomes continued.

The decay stops at a **6.0 s floor** (raised from 1.0 s): the same Steam budget is shared with the owner's own hand-browsing, so when scrapes start failing the worker has to back off far enough that the Workshop is still usable manually while the daemon runs. The starting default is the floor. There is **no ceiling** any more — it went with the fixed 300 s pause, which was a second pacing rule that could not converge: the same pause however often the throttle recurred, and no slower a rate afterwards. A rate that moves on every refusal needs no separate rule, and it cannot run away (see the API rule above).

The delay is persisted as `daemon.web_delay_seconds`, and that persisted value is the only shared truth
between processes: `src.web_worker.configured_web_delay` reads it fresh from the config rather than from
a module-level snapshot, so the subscribe engine's page reads and the subscriptions walk wait the same
interval the worker is using. A page read that does not go through the worker therefore gates itself
with it; the subscribe POST does not, because it is a click rather than a page load — see
[Subscribe Engine](#subscribe-engine-browser-free). The fixed `_WEB_DELAY` gate still applied inside
`scrape_extended_details` is a separate defect, recorded as issue 34 in
[code-issues.md](code-issues.md).

**Throttling**: Steam answers many requests with **HTTP 200** and its ordinary Workshop shell
carrying "too many requests", so the status code proves nothing and the page is otherwise
indistinguishable from a content miss. It is detected separately and treated as a spent request
budget rather than a bad item: the item's priority is left alone, no retry is attempted, and the
worker pauses for minutes instead of seconds.

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
check or a sign-in wall is classified `GATE`. If the login cookie actually changed, `_retry_if_gated`
already retried once with the fresh credential; a merely broken page cannot double the request rate
because nothing is retried unless the cookie changed. The item keeps its queue place and the delay is
left alone, since a slower pace cannot fix a session that is not working.

The two checks are evaluated **independently**, and neither shadows the other. They overlap by
construction: a throttle page is not the item page, so it lacks the signed-in markers too, and
reading that overlap as "signed out" would refresh and then retry into a reply that may be
reporting a spent budget. Only the network retry is suppressed by throttling. The cookie is still
re-read, because that is a local file copy that spends no network budget, and it means the next
request that does go out carries the freshest credential.

**Misses**: the throttle outcome outranks the other page outcomes, so a throttled page is paused and
never read as a genuine absence. Otherwise a miss is neither a blanket failure nor an empty success:
a page that says the item is gone clears the item's queue flag, a description-less item page clears
it too, a page that is not the item's — an error page, a wall, a throttle the marker missed — leaves
the priority untouched, and only a page matching no recognised condition counts as a failure for
pacing. See the [outcome taxonomy](#outcome-taxonomy) and
[Dynamic delay](#web-scraping-phase). Migration 17→18 requeues the rows the old "truthy dict is
success" test stranded with `extended_description = NULL` and `needs_web_scrape = 0`; see
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

So one run is:

1. **Read** the item page (`fetch_item_page`, the scraper's own request shape via
   `scrape_extended_details(..., keep_body=True)`). `parse_button_state` reads the one element's own
   class list.
2. **Short-circuit** when `toggled` is present: the outcome is `already_subscribed`, and **no request
   is sent**. This is both the guard against the endpoint ever turning out to be a toggle and the
   reason re-running a queue is cheap.
3. **Click** (only when the page says not subscribed): `post_subscribe_request` sends the POST, built
   from the same helpers `/api/subscribe/<id>` uses. Steam's `success: 2`/`15` answers are recorded as
   a session problem with the route's own sentence.
4. **Confirm** (`confirm_subscription`): read the page again and decide from the button. `toggled`
   present means subscribed — `record_confirmed_subscription` calls `mark_own_subscribed` (which also
   clears `is_queued_for_subscription`) and clears the recorded session problem. `toggled` absent with
   `success: 1` is a **disagreement**: nothing is recorded and the item stays queued, because the page
   is the authority and the JSON is corroboration only. A throttle page on the confirmation read stays
   queued; any other button-less page reports that the result cannot be told.

**The confirmation read is evidence-gathering, not the design.** It doubles each item's page reads,
so it is isolated in `confirm_subscription` behind the module-level `VERIFY_AFTER_SUBSCRIBE` switch;
retiring it is one call site and one flag. The production run measured why it can eventually go — the
endpoint is safe on an already-subscribed item, and the body cannot distinguish the two cases — while
the pre-read keeps both of its jobs until idempotency is trusted beyond a single observation. The
retirement order, and the cheap middle ground of letting the daily reconcile be the confirmer, are
recorded in
[future-plans.md](future-plans.md#retiring-the-subscribe-confirmation-read).

**Pacing.** Both page reads are page loads, so `WebInterval` gates them on the web scraper's shared
adaptive interval — the persisted `daemon.web_delay_seconds`, read fresh through
`src.web_worker.configured_web_delay` rather than snapshotted, decayed with `pacing.decay` on a read
that carried a button and doubled with `pacing.backoff` on a throttle page, then written back through
the config the way the daemon's `_save_config_value` writes it. A pass builds one interval and threads
it through every item, so the items are spaced. **The subscribe POST is exempt**: it is the button
click, a browser-initiated XHR rather than a page load, so it never waits. The subscriptions walk in
`src/subscription_sync.py` uses the same gate, and it only waits — a reconcile is not a rate-seeking
queue and does not move the shared delay.

**Capture.** Every page read and the POST go through
`capture.record_web_download` under the `item_page` and `subscribe` kinds, covered by the existing
`web_downloads` switch; the switch's credential elision is what keeps the login cookie out of the
outbox.

---

## Image Download Phase

### `ImageScraperThread` (image_worker)

A daemon thread that picks up items from `get_next_image_item`, ordered by `needs_image DESC, api_fetched_at ASC`. For each item:

1. Checks `preview_url`. If absent, clears `needs_image = 0` (no image to download).
2. Downloads the image via `requests.get(stream=True)`. Detects MIME type from Content-Type header, mapping known types (`image/jpeg` → `jpg`, `image/png` → `png`, etc.) via `MIME_MAP`.
3. If Content-Type is unrecognized, uses `puremagic` (a file-magic detection library) on the first 8KB of the response body to guess the extension. Maps puremagic extensions via `MAGIC_EXT_MAP` (includes `.jfif` → `jpg` for JPEG variants).
4. If extension can't be determined, logs a warning (including the puremagic guess), captures the served response as an image failure, and records the **served type** in `image_extension` (`html` for an error page, `svg+xml` for a picture format the downloader cannot write) while clearing `needs_image = 0`. It does not increment the failure counter: an unrecognised type is not a transient error, and it is final — the same response is what a retry would get.
5. Saves the image to `images/{workshop_id}.{ext}`.
6. On success updates `image_extension` to the extension, sets `needs_image = 0`, and records `scrape_version = steam_updated_at`.
7. On failure, splits on whether the server actually answered. A **permanent status** (`404`, `410`) is written into `image_extension`, `needs_image` is cleared, and `api_priority` is deliberately **not** raised — raising it asked for an API refresh, the refresh re-flagged the image, and the download 404'd again, which is how one item came to be fetched twenty-five times in a day for a preview that never existed. A permanent answer is also neutral for pacing. Any **other** failure decrements `needs_image` by 1 (down to a minimum of 0) and raises `api_priority` to 2, so transient errors are retried with decreasing priority.

**Evidence capture.** Every image failure — an HTTP status, a transport exception, or an unclassifiable MIME type — is captured whenever `daemon.outbox_dir` is set, with its status, response headers, URL, content type and length, and the exception text when there is no response. A download that succeeds is captured **only** while the `daemon.capture_image_downloads` debug switch is on, with the same metadata plus the number of bytes written and the path of the saved file. The image bytes themselves are never copied into the outbox: the file under `images/` is the artefact, so a capture holds metadata only (see [failure-capture.md](failure-capture.md)).

**Dynamic delay**: The shared shape (`src/pacing.py`) with its own `image_delay_seconds`: every refusal doubles the delay and healthy operation halves it for every 600 s it has been running, floored at 0.5 s and with no ceiling. An unrecognised MIME type and a permanent status do **not** count as failures: both answer a question about the *item* rather than about our request rate, so neither grows the delay nor breaks a run of successes. The captures proved the distinction was real — 404s outnumbered timeouts 6,517 to 163 over one window, so the delay had been moved almost entirely by the class that says nothing about being refused.

---

## Translation Phase

### `TranslatorThread` (translator)

A daemon thread that batch-translates text fields. Uses OpenAI-compatible API with configurable endpoint and model.

**Batching strategy**: Fetches up to `openai.batch` fields (default 20) from `translation_queue` via `get_next_batch_for_translation` (ordered by `priority DESC`, then NULL-`queued_at` rows ahead of dated rows, then `queued_at ASC`). If the batch is smaller than the configured size AND no field has priority >= 5, it waits 30s to accumulate more low-priority items. If any field has priority >= 5 (detail-view bump), it translates immediately regardless of batch size.

**Translation process** (`_translate_batch`):
1. Builds a prompt containing all fields as a JSON array with `{id, field, text}` entries.
2. Sends to the OpenAI API. Parses the response, accepting both `"translated"` and `"text"` keys.
3. For each successfully translated field, updates the corresponding `_en` column on `workshop_items` or `users`, stamps `translate_version` (or `translated_at` on `users`), and deletes the queue entry.
4. After the batch, for each item that had translations processed, checks if any remaining queue entries exist. If none remain, sets `translation_priority = 0`.

**Error recovery**: API failures log an error and retry on the next cycle. Individual field failures (no translation returned) are counted and logged.

### `flag_for_translation` (database)

Sets `translation_priority` on a `workshop_items` or `users` row. Used by the daemon when a non-ASCII field is first detected.

### `flag_field_for_translation` (database)

Inserts or bumps an entry in `translation_queue`, and also raises the parent row's `translation_priority` via `MAX`. Checks if the field already exists in the queue; if so, bumps its priority (never downgrades). If new, inserts with the given priority and `queued_at = now`.

### `bump_translation_for_list` / `bump_translation_for_detail` (database)

Called when items are displayed in the list or detail view. For each non-ASCII text field (title, short_description, extended_description), checks if the `_en` translated counterpart is already populated. If not, flags the field for translation at priority 5 (list) or 10 (detail). This ensures viewed items get translated promptly.

### What queues a field for translation

Four events add a field to `translation_queue`. They all apply the same freshness
rule; they differ only in which fields they consider and at what priority.

| Trigger | Code path | Fields | Priority | Skips a current translation? |
|---|---|---|---|---|
| Daemon enriches an item via the API | `daemon.py`, `_flag_translations` (from `_process_item`) | `title_en`, `short_description_en` | `max(3, requested)` | Yes |
| Web scrape succeeds | `web_worker.py`, `WebScraperThread` | `extended_description_en` | 3 | Yes |
| Item appears in a list | `bump_translation_for_list` (TUI list load, `POST /api/search`) | all three | 5 | Yes |
| Item opened in the detail pane | `bump_translation_for_detail` (TUI selection, `GET /api/item/<id>`) | all three | 10 | Yes |

Two conditions apply to every trigger:

- **Only non-empty, non-ASCII text is queued.** `flag_field_for_translation` returns immediately
  for empty or ASCII text, so an ASCII-only title is never sent anywhere.
- **Priority only rises.** Flagging a field already in the queue with a higher priority updates the
  entry; a lower priority is ignored. It is never downgraded.

A field is queued when it has no translation **or** its translation is out of date.
`translation_is_current` states the rule: a translation is current when the `_en` value exists and
its `translate_version` is not older than the item's `steam_updated_at`. The translator stamps
`translate_version` with `steam_updated_at` at translation time, so a source edit makes the
translation stale and it is re-queued; unchanged text is left alone. See
[timestamps.md](timestamps.md).

This replaced a pair of defects. The two background triggers used to check only that the text was
non-ASCII, while the two user-view triggers also checked for an existing translation. Since a
successful translation **deletes** its queue row, `flag_field_for_translation`'s "already in the
queue" check offered no protection afterwards — so with the staleness sweep returning every
`status = 200` item to the fetch queue about monthly, an enriched item with a non-ASCII title was
re-translated roughly monthly whether or not anything had changed. Meanwhile nothing compared the
version keys, so *changed* text was never refreshed either. Both are fixed; the freshness rule is
what keeps the two from contradicting each other.

Because the version key is per item, not per field, a change to any Steam-visible field re-queues
all non-ASCII fields of that item. That is coarser than strictly necessary, but it is the
granularity of the only version stamp that exists.

---

## Display & Search Visibility

Items become visible in search once they have `status = 200` (API details fetched) and their metadata is in the database. The TUI and Web UI both call `search_items` with structured filters.

### Summary fields

The `summary_only` SELECT returns: `workshop_id, title, title_en, creator, consumer_appid, translate_version, is_queued_for_subscription, needs_web_scrape, needs_image, translation_priority, file_size, image_extension, wilson_subscription_score, wilson_favorite_score, personaname, personaname_en`. Tags are returned via a subquery joining `workshop_tags` and `tags` as a comma-separated string.

### Detail fields

`get_item_details` returns `w.*` (all columns) plus `personaname`, `personaname_en`, `user_translated_at`, and tags from the junction table.

---

## Item Lifecycle State Machine

```
[Discovered: status=NULL, api_fetched_at=NULL, last_fetch_attempted_at=NULL]
    │
    ▼ seed_database / _run_page_discovery
    │
[Discovered: workshop_id exists, no metadata]
    │
    ▼ process_batch: get_workshop_details_batch
    │
[Fetched: status=200, api_fetched_at=now, has title/tags/preview_url]
    │
    ├─► Web Scraper (if enriched): scrape_extended_details
    │      │
    │      ▼
    │   [Scraped: extended_description populated, needs_web_scrape=0]
    │
    ├─► Image Download (if preview_url): ImageScraperThread
    │      │
    │      ▼
    │   [Image: image_extension set, needs_image=0]
    │
    └─► Translator (if non-ASCII): TranslatorThread
           │
           ▼
        [Translated: title_en, etc. populated, translation_priority=0]
```

Each thread operates independently. The web server's `_ensure_image_flagged` sets `needs_image=5` for list-viewed items and 10 for the detail view, and the daemon calls `flag_for_image(max(3, requested))` for newly discovered items with a `preview_url`, where `requested` is the user-requested part of the item's pre-fetch `api_priority` (`user_requested_priority`).

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

Filters for the cutoffs exclude any filter with the `percentile` operator (to avoid circularity — the percentile query can't reference itself) and route tag filters through `_build_json_tag_clause` (junction table).
