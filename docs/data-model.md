# Data Model

The database is a single SQLite file in WAL mode. Its current schema version is 20
(`EXPECTED_VERSION` in `src/database.py`). All application state lives in three tables —
`workshop_items`, `users`, and `translation_queue` — plus two tables that hold tags,
`tags` and `workshop_tags`.

Column names follow two conventions, described in full in [timestamps.md](timestamps.md):

* `steam_*` is Steam's clock; `*_at` is our clock; `*_version` is a stored Steam value used as a
  version key.
* Work-queue columns end in `_priority` (with historical exceptions: `api_priority`,
  `needs_web_scrape`, `needs_image`).

## Provenance Classes

Every column has exactly one owner. This is the classification the naming alone does not carry.

| Class | Meaning | Owner |
|---|---|---|
| **STEAM** | Passed through from the Steam API, sometimes under a different name | Steam |
| **SCRAPED** | Extracted from Workshop HTML | web scraper |
| **DERIVED** | Computed from other columns | daemon |
| **TRANSLATED** | Produced by the translation service | translator |
| **QUEUE** | Work-queue priority or progress counters | daemon |
| **STATE** | Lineage and outcome bookkeeping | daemon |

## `workshop_items`

One row per Steam Workshop item, keyed by `workshop_id` (the Steam `publishedfileid`).

### Steam-provided columns

| Column | Source | Notes |
|---|---|---|
| `workshop_id` | `publishedfileid` | Primary key. The API key supplied alongside it is dropped before storage. |
| `creator` | `creator` | SteamID of the author. No foreign key is declared: items are discovered before their creator is fetched, and the join to `users` is a `LEFT JOIN`. |
| `creator_appid` | `creator_app_id` | Renamed in `_merge_and_clean_api_data`. |
| `consumer_appid` | `consumer_app_id` | The game the item belongs to. |
| `filename` | `filename` | |
| `file_size` | `file_size` | Bytes. |
| `preview_url` | `preview_url` | |
| `hcontent_file` | `hcontent_file` | Opaque content handle. |
| `hcontent_preview` | `hcontent_preview` | Opaque content handle. |
| `title` | `title` | |
| `short_description` | `description` | Renamed in `_merge_and_clean_api_data`; Steam calls it `description`. |
| `steam_created_at` | `time_created` | Steam epoch seconds. |
| `steam_updated_at` | `time_updated` | Steam epoch seconds. Also the value recorded as the scrape and translation version key. |
| `visibility` | `visibility` | |
| `banned` | `banned` | |
| `ban_reason` | `ban_reason` | |
| `app_name` | `app_name` | |
| `file_type` | `file_type` | |
| `subscriptions` | `subscriptions` | Current subscriber count. |
| `favorited` | `favorited` | Current favorite count. |
| `views` | `views` | |
| `lifetime_subscriptions` | `lifetime_subscriptions` | |
| `lifetime_favorited` | `lifetime_favorited` | |
| `language` | `language` | **Never populated.** The merge would store it if a Steam response included it, but none has; NULL for every row in the live database. |

### Scraped and translated columns

| Column | Class | Source |
|---|---|---|
| `extended_description` | SCRAPED | `div.workshopItemDescription#highlightContent` on the item's HTML page. Never comes from the API. |
| `title_en` | TRANSLATED | OpenAI. |
| `short_description_en` | TRANSLATED | OpenAI. |
| `extended_description_en` | TRANSLATED | OpenAI. |

Tags are not a column on `workshop_items`. They live in `tags(tag_id, tag_name)` and
`workshop_tags(workshop_id, tag_id)`.

### Derived and bookkeeping columns

| Column | Class | Meaning |
|---|---|---|
| `wilson_favorite_score` | DERIVED | `wilson_lower(favorited, lifetime_subscriptions)`. |
| `wilson_subscription_score` | DERIVED | `wilson_lower(subscriptions, lifetime_subscriptions)`. |
| `status` | STATE | Fetch outcome. `200` = fetched, `206` = partial (see gaps below), `-1` = dead, `500` = transient failure to retry, `NULL` = discovered but never fetched. Synthetic, not an actual HTTP response code. |
| `first_seen_at` | STATE (ours) | Set when the row is first inserted. |
| `api_fetched_at` | STATE (ours) | Set only when the API returned content (see [timestamps.md](timestamps.md)). |
| `last_fetch_attempted_at` | STATE (ours) | Set on every API attempt, success or failure. |
| `scrape_version` | STATE (Steam value) | `steam_updated_at` at the moment the web scraper ran. |
| `translate_version` | STATE (Steam value) | `steam_updated_at` at the moment the translator ran. |
| `image_extension` | STATE | Extension of the downloaded preview image, or NULL if not downloaded. |
| `is_queued_for_subscription` | QUEUE | Subscription queue flag. Set by the TUI (`s`) and by `POST /api/toggle_sub/<id>`; cleared by `POST /api/subscribed/<id>` and `POST /api/subscribe_failed/<id>` when the userscript reports an outcome. Transient working state — it reads `0` whenever nothing is queued, which is the normal resting state, not evidence of disuse. |

### Queue columns

| Column | Queue |
|---|---|
| `api_priority` | Steam API fetch queue. |
| `needs_web_scrape` | HTML scrape queue. |
| `needs_image` | Preview image download queue. |
| `translation_priority` | Translation queue mirror. Kept in step with `translation_queue` by `flag_field_for_translation` (which takes the `MAX` of the stored and new priority). |

## `users`

One row per Steam creator whose profile has been fetched, keyed by `steamid`.

| Column | Class | Meaning |
|---|---|---|
| `steamid` | STEAM | SteamID64, primary key. |
| `personaname` | STEAM | Display name. |
| `personaname_en` | TRANSLATED | Translated display name. |
| `api_fetched_at` | STATE (ours) | Our clock: when the profile was last refreshed. |
| `translated_at` | TRANSLATED/STATE | Our wall-clock time of the last translation. This is **not** a Steam version key: users have no `steam_updated_at`. |
| `translation_priority` | QUEUE | Name translation priority. |

## `translation_queue`

One row per text field awaiting translation.

| Column | Meaning |
|---|---|
| `id` | Auto-increment queue entry ID. |
| `item_type` | `"item"` or `"user"`. |
| `item_id` | `workshop_id` or `steamid`. |
| `field` | Target column, for example `title_en`. |
| `original_text` | Source text. |
| `priority` | Work priority; the translator drains highest first. |
| `queued_at` | Our clock: when the entry was created. Written for new rows. Rows that predate migration 13→14 keep NULL because their queue time is genuinely unknown. |

Ordering (`get_next_batch_for_translation`) is `priority DESC`, then NULL-`queued_at` rows ahead
of dated rows, then `queued_at ASC`. The explicit NULL-first term keeps the legacy backlog from
being jumped by newly queued work at the same priority.

## Queue Priorities

The work-queue columns share a vocabulary but not a single distribution. The intended scale is:

| Value | Meaning |
|---|---|
| 0 | Idle / not queued. |
| 1 | Backlog or stale background refresh. |
| 3 | Newly discovered item. |
| 5 | Visible in a list. |
| 10 | Open in the detail pane. |

Higher wins, and writers use `MAX(stored, new)` so a flag is never downgraded. The queues do not
each use every value. One value sits outside the scale: `api_priority = 2` is written by the image
worker's failure path (`image_worker.py`), making it a worker-specific retry marker rather than
part of the shared vocabulary. See [live-data-profile.md](live-data-profile.md) for the measured
distribution of each queue.

## Known Gaps

These are properties of the current implementation, stated so a reader does not infer behaviour
that is not there.

* **`translate_version` drives re-translation; `scrape_version` is only a record.** A translation
  is current when its `translate_version` is not older than the item's `steam_updated_at`, and every
  translation trigger applies that rule (see [timestamps.md](timestamps.md)). `scrape_version` is
  written by the web scraper and the image worker, but no code compares it: the daemon decides
  whether to re-queue the HTML scrape from `steam_updated_at` and whether `extended_description` is
  already present, so the HTML scrape refreshes on an item update and the image worker uses its own
  priority queue.
* **`language` is never populated.** It is in `WORKSHOP_ITEM_COLUMNS` and the merge allow-list, so
  the API merge would store it if a response included it — none has, and it is NULL for every row
  in the live database.
* **`is_queued_for_subscription` is `0` in an idle database, but it is not dead.** It backs a
  working feature: the TUI (`s`) and the web UI toggle it, the userscript polls
  `GET /api/queued` and clears each entry once it has subscribed or failed. Because it is
  transient, an all-zero snapshot only means nothing was queued at that moment.
* **`status = 206` has never occurred.** The schema and one migration query allow a partial-data
  status, but the production database contains zero rows with it.
