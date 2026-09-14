# Data Pipeline

The data pipeline moves a Steam Workshop item from initial discovery through enrichment stages to user-facing display. An item progresses through: discovery → API detail fetch → web scraping → image download → translation → search visibility.

---

## Discovery Phase

### `process_batch` (daemon)

The main loop entry point called repeatedly by `run()`. Each invocation:

1. Calls `get_next_items_to_scrape` to retrieve up to `batch_size` items due for processing. Selection is `api_priority > 0` and `status` not `-1`, ordered by `api_priority DESC, api_fetched_at ASC`, so never-successfully-fetched items (`api_fetched_at IS NULL`) come first within a priority band.
2. If no items are available, triggers discovery: first tries `_run_page_discovery` if eligible, otherwise falls back to `seed_database` (cursor-based). After discovery, retries `get_next_items_to_scrape`.
3. For each retrieved item, calls the Steam Web API via `get_workshop_details_api` to fetch metadata (title, description, tags, file_size, preview_url, creator, subscriptions, etc.).
4. Merges API data with existing DB row via `_merge_and_clean_api_data`, which filters to `MERGE_ITEM_KEYS` (derived from `WORKSHOP_ITEM_COLUMNS`), remaps `creator_app_id`/`consumer_app_id` to `creator_appid`/`consumer_appid`, remaps `description` to `short_description`, and remaps the API's `time_created`/`time_updated` to `steam_created_at`/`steam_updated_at`. Unknown API keys are discarded with a log message.
5. Computes Wilson scores via `wilson_lower` (a binomial-proportion confidence interval using a 95% z-score of 1.96). Sets `wilson_favorite_score` from `(favorited, lifetime_subscriptions)` and `wilson_subscription_score` from `(subscriptions, lifetime_subscriptions)`.
6. Evaluates enrichment filters via `_should_enrich`. Checks the stored `enrichment_filters` for each AppID against the item using `_evaluate_filters` (an in-memory filter evaluator that mirrors the SQL builder's semantics). If no filters are configured, all items are enriched.
7. If enrichment is approved, calls `flag_for_web_scrape` at `max(3, inherited_prio)`. Also calls `flag_for_image` at the same priority if `preview_url` is present. Sets `status = 200`. Calls `insert_or_update_item` to persist. The merge sets `api_fetched_at = now_ts` and `api_priority = 0`; `last_fetch_attempted_at` was already stamped on entry.
8. For enriched items, flags `title` and `short_description` for translation via `flag_field_for_translation` at `max(3, inherited_prio)`. That function inserts into `translation_queue` and also raises the parent row's `translation_priority` (using `MAX`, so it never downgrades); the translator clears it to 0 when the item has no queue entries left. Users with non-ASCII names get `translation_priority = 1` set via `_build_user_record`.
9. Also fetches the creator's profile via `get_player_summaries` if the user record is stale (exceeds `user_staleness_days`).

**Error handling**: `get_workshop_details_api` returns `404` when the item is absent or `result != 1` (permission errors, deleted items), and `500` on any `RequestException` — timeouts and HTTP errors such as 429 included. The daemon maps a `404` to `status = -1` (dead), clears every queue flag so the item is in no queue, and persists a `500` as `status = 500` for a later attempt. Both paths stamp `last_fetch_attempted_at`; the `500` path leaves `api_fetched_at` untouched.

**Dynamic delay**: `api_delay` adjusts via a compounding mechanism. 100 consecutive successes reduce delay by 5% (minimum 0.01s). 2 consecutive failures after a streak increase it by `1.05^10 ≈ 63%`. The adjusted value is persisted to config.

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

### `get_workshop_details_api` (steam_api)

Calls `ISteamRemoteStorage/GetPublishedFileDetails/v1/` to fetch full metadata for a single `publishedfileid`. Returns a dict containing the API's `title`, `description`, `tags`, `file_size`, `preview_url`, `creator`, `subscriptions`, `favorited`, `views`, `time_created`, `time_updated`, and more (these are the raw API field names; `_merge_and_clean_api_data` renames some of them before storage).

Returns `{status: 500}` on any request failure (network, timeout, or HTTP error) — the caller stamps `last_fetch_attempted_at` and applies the failure classification below. Returns `{status: 404}` when the item is not found or `result != 1`.

### `_merge_and_clean_api_data` (daemon)

Merges API response data into the existing DB row. Applies column-name remapping (`creator_app_id` → `creator_appid`, `description` → `short_description`, `time_created`/`time_updated` → `steam_created_at`/`steam_updated_at`). Filters to `MERGE_ITEM_KEYS` (derived from `WORKSHOP_ITEM_COLUMNS`) to prevent unknown API columns from polluting the DB, and discards known-but-handled-externally keys (for example `needs_web_scrape`, `image_extension`, `needs_image`). Normalizes tags via `normalize_tags`. On the success path it stamps `api_fetched_at = now_ts` and `api_priority = 0`.

### `_should_enrich` (daemon)

Checks whether an item passes the enrichment filter for its AppID. Reads `enrichment_filters` from `app_tracking` (a JSON array of filter dicts in the same format as the TUI search builder). Feeds the item dict through `_evaluate_filters`, which uses `_evaluate_single_filter` for each criterion and `_evaluate_tag_filter` for tag-based filters. Returns True if no filters are configured for the AppID (enrich everything).

### Failure classification (daemon)

`_settle_api_failure` turns a non-success outcome into a queue decision. `404` is permanent: the failure is logged, the item is marked dead (`status = -1`) and it is removed from **every** queue — `api_priority`, `needs_web_scrape`, `needs_image` and `translation_priority` are all cleared, because a dead item can never complete and a queue flag left set would strand it in a queue that never drains. Everything else is temporary — `500`, transport exceptions (which `get_workshop_details_api` reports as `500`), and any status no branch handles. Those keep the item queued at one priority level lower, floored at `1`, because priority `0` means "not queued" and clearing it is what previously stranded transient failures with nothing able to bring them back. Unhandled statuses are captured as evidence and never fall through to the success path.

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
4. If the request failed, raises `api_priority` to 2 so the item is retried (that value has no other source); nothing is cleared, so the item stays in the scrape queue.
5. If the page loaded but the description selector did **not** match, the response is captured as evidence and the markup decides what the queue learns. If the body carried neither the item template (`workshopItem`) nor the description element (`highlightContent`), the page was not the item's — a wall, an error page, or a throttle page whose wording the marker missed — so the item is not at fault and `needs_web_scrape` is left exactly as it was; the request does count as a failure for pacing, because the server declined to serve content. If the item template is present but the description element is not, the page really is the item's and it genuinely has no extended description: no retry can change that, so `needs_web_scrape` is cleared and the item leaves the queue, which is what lets the queue drain; that outcome is neutral for pacing. See [failure-capture.md](failure-capture.md).

**Dynamic delay**: Same 100-success / 2-failure compounding pattern as the daemon, but with its own `web_delay_seconds` config key. A request the server did not serve — a transport failure or a page that was not the item's — counts as a failure and grows the delay. A found description counts as a success and resets the failure streak. The item page with no description moves neither counter: the request succeeded, so there is nothing to back off from, but it yielded nothing, so it must not reset the failure streak either — counting it as a success would let the delay fall again while walls continued.

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
check or a sign-in wall is retried once — and only if the login cookie actually changed, which is what
`session.read_firefox_cookies` refreshes. A merely broken page therefore cannot double the request rate.

The two checks are evaluated **independently**, and neither shadows the other. They overlap by
construction: a throttle page is not the item page, so it lacks the signed-in markers too, and
reading that overlap as "signed out" would refresh and then retry into a reply that may be
reporting a spent budget. Only the network retry is suppressed by throttling. The cookie is still
re-read, because that is a local file copy that spends no network budget, and it means the next
request that does go out carries the freshest credential.

**Misses**: the throttle check runs before the selector-miss handler and keeps precedence, so a
throttled page is paused and never read as a genuine absence. Otherwise a miss is neither a blanket
failure nor an empty success: returning to a description-less page clears the item only when the page
was really the item's, and a page that was not — an error page, a wall, a throttle the marker missed
— leaves the item's queue priority untouched. The two are opposites for pacing, too: a page that was
not the item's counts as a failure, while a description-less item page is neutral, as
[Dynamic delay](#web-scraping-phase) describes. Migration 17→18 requeues the rows the old
"truthy dict is success" test stranded with `extended_description = NULL` and
`needs_web_scrape = 0`; see [schema-migrations.md](schema-migrations.md).

### `scrape_extended_details` (web_scraper)

Fetches the HTML page `steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}`. Parses the DOM using `requests-html` to extract:
- `description`: the full extended description (from `DESCRIPTION_SELECTOR`, `.workshopItemDescription#highlightContent`)
- `tags`: the tag names from `TAGS_SELECTOR`, `.workshopTags a`

Returns a dict, or `None` on any request failure. A successful request whose description selector did not match returns `description: None` — truthy, so the caller must test the description and not the dict. On that miss the dict also carries the response `body`, `http_status` and `final_url` so the caller can capture it; on a successful parse `body` is `None`, since there is no reason to retain a few hundred KB of HTML on the happy path. The caller stores only `description`; the `tags` key is discarded.

**Browser-faithful requests.** The HTTP request is deliberately shaped to match a real Firefox top-level navigation, measured from a HAR capture of a signed-in item load. Both request sites (`scrape_extended_details` and `discover_items_by_date_html`) send one shared header mapping:

- `Accept`/`Accept-Language`: a navigation asks for HTML in a human language. `requests` defaults `Accept` to `*/*`, which no browser navigation sends — it reads as a client that does not care what it gets, and it is the single clearest giveaway that a request is a script.
- `Sec-Fetch-*` fetch metadata: Firefox attaches these to every request, so their complete absence is a strong non-browser signal. `Sec-Fetch-Site` is `none`, not the capture's `cross-site`, because the scraper fetches the URL directly rather than following a link from another origin; `Referer` is likewise omitted rather than invented, because a direct fetch has no referring page.
- `Upgrade-Insecure-Requests`, `Priority`, `Connection: keep-alive`, and a Firefox `User-Agent`.

`Accept-Encoding` advertises `br`/`zstd` only when this interpreter can actually decode them (a `brotli`/`brotlicffi` or `zstandard` module is importable); otherwise it advertises `gzip, deflate`. Offering a codec nothing here can decode would leave compressed bytes in the body and look like a broken page for an unrelated reason.

The `User-Agent` version is read from the Firefox profile's `compatibility.ini` (`LastVersion`) using the same profile discovery that supplies the cookies, so the claimed version matches the browser actually signed in; it falls back to a constant Firefox string when no profile is readable. A single `requests-html` session is created lazily and shared by both request sites, so the TCP connection and its TLS handshake are reused the way a browser reuses them instead of a fresh handshake per call.

The cookies are the profile's whole `steamcommunity.com` set when `session.read_firefox_cookies` is enabled — the ten names a real navigation sends, not two hand-picked ones plus a hardcoded third — and nothing is invented for a name the profile does not have. When the setting is off, the configured `sessionid`/`login_secure` pair is sent exactly as before, and the login cookie is omitted when unset.

This browser shape was prompted by the "too many requests" page described above: the suspicion is that it may be bot deterrence rather than genuine throttling. That suspicion is **not proven**, and it changes nothing about detection or the pause — the reply is still not the item, so the worker still treats it as "no content" and still leaves the item untouched.

---

## Image Download Phase

### `ImageScraperThread` (image_worker)

A daemon thread that picks up items from `get_next_image_item`, ordered by `needs_image DESC, api_fetched_at ASC`. For each item:

1. Checks `preview_url`. If absent, clears `needs_image = 0` (no image to download).
2. Downloads the image via `requests.get(stream=True)`. Detects MIME type from Content-Type header, mapping known types (`image/jpeg` → `jpg`, `image/png` → `png`, etc.) via `MIME_MAP`.
3. If Content-Type is unrecognized, uses `puremagic` (a file-magic detection library) on the first 8KB of the response body to guess the extension. Maps puremagic extensions via `MAGIC_EXT_MAP` (includes `.jfif` → `jpg` for JPEG variants).
4. If extension can't be determined, logs a warning (including the puremagic guess) and clears `needs_image = 0` without incrementing the failure counter (unknown MIME is not a transient error).
5. Saves the image to `images/{workshop_id}.{ext}`.
6. Updates `image_extension`, sets `needs_image = 0`, and records `scrape_version = steam_updated_at`.
7. On failure, decrements `needs_image` by 1 (down to a minimum of 0) and raises `api_priority` to 2, so transient errors are retried with decreasing priority.

**Dynamic delay**: Same compounding pattern with `image_delay_seconds`. Unknown MIME types do NOT count as failures (they clear the flag without delay penalties).

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
| Daemon enriches an item via the API | `daemon.py`, `_flag_translations` (from `_process_item`) | `title_en`, `short_description_en` | `max(3, inherited)` | Yes |
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
    ▼ process_batch: get_workshop_details_api
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

Each thread operates independently. The web server's `_ensure_image_flagged` sets `needs_image=5` for list-viewed items and 10 for the detail view, and the daemon calls `flag_for_image(max(3, inherited_prio))` for newly discovered items with a `preview_url`.

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
