# Data Pipeline

The data pipeline moves a Steam Workshop item from initial discovery through enrichment stages to user-facing display. An item progresses through: discovery → API detail fetch → web scraping → image download → translation → search visibility.

---

## Discovery Phase

### `process_batch` (daemon)

The main loop entry point called repeatedly by `run()`. Each invocation:

1. Calls `get_next_items_to_scrape` to retrieve up to `batch_size` items due for processing. Items are prioritized: never-attempted (NULL `dt_attempted`) first, then oldest-successfully-scraped that exceed `item_staleness_days`.
2. If no items are available, triggers discovery: first tries `_run_page_discovery` if eligible, otherwise falls back to `seed_database` (cursor-based). After discovery, retries `get_next_items_to_scrape`.
3. For each retrieved item, calls the Steam Web API via `get_workshop_details_api` to fetch metadata (title, description, tags, file_size, preview_url, creator, subscriptions, etc.).
4. Merges API data with existing DB row via `_merge_and_clean_api_data`, which filters to `allowed_keys` (a hardcoded set of known-safe column names), remaps `creator_app_id`/`consumer_app_id` to `creator_appid`/`consumer_appid`, and remaps `description` to `short_description`. Unknown API keys are discarded with a log message.
5. Computes Wilson scores via `wilson_lower` (a binomial-proportion confidence interval using a 95% z-score of 1.96). Sets `wilson_favorite_score` from `(favorited, views)` and `wilson_subscription_score` from `(subscriptions, lifetime_subscriptions)`.
6. Evaluates enrichment filters via `_should_enrich`. Checks the stored `enrichment_filters` for each AppID against the item using `_evaluate_filters` (an in-memory filter evaluator that mirrors the SQL builder's semantics). If no filters are configured, all items are enriched.
7. If enrichment is approved, calls `flag_for_web_scrape` at priority 3 (new item). Also calls `flag_for_image` at priority 3 if `preview_url` is present. Sets `status = 200`. Calls `insert_or_update_item` to persist.
8. For enriched items, flags individual fields for translation via `flag_field_for_translation` at priority 3 (inserts into `translation_queue`). The `translation_priority` column on `workshop_items` is vestigial — the daemon no longer sets it (the translator operates from `translation_queue` directly and resets `translation_priority` to 0 when done). Users with non-ASCII names get `translation_priority = 1` set via `_build_user_record`.
9. Also fetches the creator's profile via `get_player_summaries` if the user record is stale (exceeds `user_staleness_days`).

**Error handling**: API 404/429/timeout results are stored with the status returned (the item is re-queued). API 500 errors are treated as transient and the item's `dt_attempted` is stamped without changing its status. Web API responses that return `result != 1` (permission errors, deleted items) are stored with status 404.

**Dynamic delay**: `api_delay` adjusts via a compounding mechanism. 100 consecutive successes reduce delay by 5% (minimum 0.01s). 2 consecutive failures after a streak increase it by `1.05^10 ≈ 63%`. The adjusted value is persisted to config.

### `seed_database` (daemon)

Cursor-based discovery using `IPublishedFileService/QueryFiles` with `query_type=1` (rank by publication date, newest first). For each target AppID:

- Resumes from the last stored cursor (`app_tracking.last_cursor`), or `*` for the first page.
- Fetches `numperpage=100` items per request. Each item's `publishedfileid` is inserted into `workshop_items` as a bare row (status NULL, no metadata).
- Stops when `target_new` unscraped items are accumulated, or when the cursor returns empty (no more pages).
- Persists the cursor after each page via `update_app_tracking_cursor`.
- When the cursor is empty after a successful scan, sets `_cursor_exhausted = True`, enabling the page-based discovery mode.

This is called when `get_next_items_to_scrape` returns empty — meaning the processing queue is drained and new items need to be discovered.

### `_run_page_discovery` (daemon)

A periodic alternative to cursor-based discovery. Enabled once `_cursor_exhausted` is True OR at least 500 items have been scraped (`dt_attempted IS NOT NULL`). Runs at most once per 24 hours (tracked via `_last_page_discovery`).

Uses `query_workshop_page_updated` which calls QueryFiles with `query_type=2` (rank by last updated, most recent first) and page-based pagination. Iterates pages 1 through 500 per AppID. Each page inserts new `publishedfileid` values as bare rows. Stops when a page returns 0 new items.

After page mode completes, the daemon resumes normal cursor-based discovery.

### `query_workshop_files` (steam_api)

Calls `IPublishedFileService/QueryFiles/v1/` with cursor-based pagination. Parameters: `query_type=1` (publication date), `cursor`, `numperpage=100`, `appid`. Returns `{total, items, next_cursor}`. Rate-limited via `_rate_limit()`.

### `query_workshop_page_updated` (steam_api)

Same API endpoint but with `query_type=2` (last updated) and `page`-based pagination. Used exclusively by `_run_page_discovery`. Returns the same `{total, items, next_cursor}` shape but the cursor is typically empty in page mode.

---

## API Detail Fetch Phase

### `get_workshop_details_api` (steam_api)

Calls `ISteamRemoteStorage/GetPublishedFileDetails/v1/` to fetch full metadata for a single `publishedfileid`. Returns a dict containing title, description, tags, file_size, preview_url, creator, subscriptions, favorited, views, time_created, time_updated, and more.

Returns `{status: 500}` on network/timeout errors (caller stamps `dt_attempted` and re-queues). Returns `{status: 404}` when the item is not found or `result != 1`.

### `_merge_and_clean_api_data` (daemon)

Merges API response data into the existing DB row. Applies column-name remapping (`creator_app_id` → `creator_appid`, `description` → `short_description`). Filters to `allowed_keys` to prevent unknown API columns from polluting the DB. Discards known-but-handled-externally keys (`needs_web_scrape`, `image_extension`, `needs_image`, etc.). Normalizes tags via `normalize_tags`. Stamps `dt_attempted = now_ts` (Unix epoch).

### `_should_enrich` (daemon)

Checks whether an item passes the enrichment filter for its AppID. Reads `enrichment_filters` from `app_tracking` (a JSON array of filter dicts in the same format as the TUI search builder). Feeds the item dict through `_evaluate_filters`, which uses `_evaluate_single_filter` for each criterion and `_evaluate_tag_filter` for tag-based filters. Returns True if no filters are configured for the AppID (enrich everything).

---

## Web Scraping Phase

### `WebScraperThread` (web_worker)

A daemon thread that picks up items from `get_next_web_scrape_item`, ordered by `needs_web_scrape DESC, dt_attempted ASC` (highest priority first, oldest-attempted within priority). For each item:

1. Calls `scrape_extended_details(workshop_id)` which fetches the Steam Community workshop page and parses the extended description, full-size preview URL, and tags.
2. If successful, updates the item with extended_description, preview_url, and tags. Sets `needs_web_scrape = 0`.
3. Flags non-ASCII extended_description for translation at priority 3 via `flag_field_for_translation`.
4. Stamps `dt_attempted`.

**Dynamic delay**: Same 100-success / 2-failure compounding pattern as the daemon, but with its own `web_delay_seconds` config key.

### `scrape_extended_details` (web_scraper)

Fetches the HTML page `steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}`. Parses the DOM using `requests-html` to extract:
- `description`: the full extended description (from the `div.workshopItemDescription` element)
- `preview_url`: the full-size preview image URL
- `tags`: normalized tag list from the workshop page sidebar

Returns a dict with these three keys. Returns None on any request failure.

---

## Image Download Phase

### `ImageScraperThread` (image_worker)

A daemon thread that picks up items from `get_next_image_item`, ordered by `needs_image DESC, dt_attempted ASC`. For each item:

1. Checks `preview_url`. If absent, clears `needs_image = 0` (no image to download).
2. Downloads the image via `requests.get(stream=True)`. Detects MIME type from Content-Type header, mapping known types (`image/jpeg` → `jpg`, `image/png` → `png`, etc.) via `MIME_MAP`.
3. If Content-Type is unrecognized, uses `puremagic` (a file-magic detection library) on the first 8KB of the response body to guess the extension. Maps puremagic extensions via `MAGIC_EXT_MAP` (includes `.jfif` → `jpg` for JPEG variants).
4. If extension can't be determined, logs a warning (including the puremagic guess) and clears `needs_image = 0` without incrementing the failure counter (unknown MIME is not a transient error).
5. Saves the image to `images/{workshop_id}.{ext}`.
6. Updates `image_extension` and sets `needs_image = 0`.
7. On failure, decrements `needs_image` by 1 (down to a minimum of 0) so transient errors are retried with decreasing priority.

**Dynamic delay**: Same compounding pattern with `image_delay_seconds`. Unknown MIME types do NOT count as failures (they clear the flag without delay penalties).

---

## Translation Phase

### `TranslatorThread` (translator)

A daemon thread that batch-translates text fields. Uses OpenAI-compatible API with configurable endpoint and model.

**Batching strategy**: Fetches up to 20 fields from `translation_queue` via `get_next_batch_for_translation` (ordered by `priority DESC, dt_queued ASC`). If the batch has fewer than 20 fields AND no field has priority >= 5, waits 10s to accumulate more low-priority items. If any field has priority >= 5 (detail-view bump), translates immediately regardless of batch size.

**Translation process** (`_translate_batch`):
1. Builds a prompt containing all fields as a JSON array with `{id, field, text}` entries.
2. Sends to the OpenAI API. Parses the response, accepting both `"translated"` and `"text"` keys.
3. For each successfully translated field, updates the corresponding `_en` column on `workshop_items` or `users`, stamps `dt_translated`, and deletes the queue entry.
4. After the batch, for each item that had translations processed, checks if any remaining queue entries exist. If none remain, sets `translation_priority = 0`.

**Error recovery**: API failures log an error and retry on the next cycle. Individual field failures (no translation returned) are counted and logged.

### `flag_for_translation` (database)

Sets `translation_priority` on a `workshop_items` or `users` row. Used by the daemon when a non-ASCII field is first detected.

### `flag_field_for_translation` (database)

Inserts or bumps an entry in `translation_queue`. Checks if the field already exists in the queue; if so, bumps its priority (never downgrades). If new, inserts with the given priority and `dt_queued = NULL`.

### `bump_translation_for_list` / `bump_translation_for_detail` (database)

Called when items are displayed in the list or detail view. For each non-ASCII text field (title, short_description, extended_description), checks if the `_en` translated counterpart is already populated. If not, flags the field for translation at priority 5 (list) or 10 (detail). This ensures viewed items get translated promptly.

---

## Display & Search Visibility

Items become visible in search once they have `status = 200` (API details fetched) and their metadata is in the database. The TUI and Web UI both call `search_items` with structured filters.

### Summary fields

The `summary_only` SELECT returns: `workshop_id, title, title_en, creator, consumer_appid, dt_translated, is_queued_for_subscription, needs_web_scrape, needs_image, translation_priority, file_size, image_extension, wilson_subscription_score, wilson_favorite_score, personaname, personaname_en`. Tags are returned via a subquery joining `workshop_tags` and `tags` as a comma-separated string.

### Detail fields

`get_item_details` returns `w.*` (all columns) plus `personaname`, `personaname_en`, `user_dt_translated`, and tags from the junction table.

---

## Item Lifecycle State Machine

```
[Discovered: status=NULL, dt_attempted=NULL]
    │
    ▼ seed_database / _run_page_discovery
    │
[Discovered: workshop_id exists, no metadata]
    │
    ▼ process_batch: get_workshop_details_api
    │
[Fetched: status=200, dt_attempted=now, has title/tags/preview_url]
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

Each thread operates independently. The daemon's `_ensure_image_flagged` sets `needs_image=5` for list-viewed items, and `flag_for_image(priority=3)` for newly discovered items with preview_url.

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

Used to compute `wilson_favorite_score` (using `favorited / views`) and `wilson_subscription_score` (using `subscriptions / lifetime_subscriptions`). The subscriber metric measures retention — what fraction of lifetime subscribers remain subscribed. Both scores are REAL values between 0 and 1, stored with `NULL` default.

### `compute_wilson_cutoffs` (database)

Computes p99, p90, p50 percentile thresholds for both Wilson scores across the filtered dataset. Uses SQLite's `NTILE(100)` window function to divide scores into 100 buckets (sorted descending, nulls last). Returns the minimum value in buckets 1, 10, and 50 as the p99, p90, and p50 cutoffs respectively. These are used by both UIs for color-coded score display (gold for top 1%, yellow for top 10%, white for top 50%, gray below).

Filters for the cutoffs exclude any filter with the `percentile` operator (to avoid circularity — the percentile query can't reference itself) and route tag filters through `_build_json_tag_clause` (junction table).
