# Schema & Migrations

The database uses SQLite with WAL mode. Schema evolution follows a `PRAGMA user_version` increment pattern where each migration is a discrete `if db_version < N:` block within `initialize_database`, run sequentially on startup. Fresh databases run all migrations; existing databases run only pending ones.

---

## Current Schema (v14)

The application-level reference for every table and column is
[data-model.md](data-model.md); the timestamp conventions are in
[timestamps.md](timestamps.md). This is the storage-level summary.

### `workshop_items` — main item table

Primary key: `workshop_id INTEGER PRIMARY KEY` (aliased from rowid). Columns:

| Column | Type | Purpose |
|---|---|---|
| workshop_id | INTEGER PK | Steam published file ID |
| title, title_en | TEXT | Original and English-translated title |
| creator | INTEGER | Reference to `users.steamid` (no FK constraint; joined with `LEFT JOIN`) |
| creator_appid, consumer_appid | INTEGER | App that created/uses the item |
| filename, file_size | TEXT, INTEGER | File metadata |
| preview_url | TEXT | Preview image URL from Steam API |
| short_description, short_description_en | TEXT | Short description and translation |
| extended_description, extended_description_en | TEXT | Full description (populated by web scraper) and translation |
| steam_created_at, steam_updated_at | INTEGER | Steam's clock, Unix epoch seconds |
| first_seen_at, api_fetched_at, last_fetch_attempted_at, scrape_version, translate_version | INTEGER | Our clocks and the two stored Steam version keys |
| subscriptions, lifetime_subscriptions | INTEGER | Current and lifetime subscriber counts |
| favorited, lifetime_favorited, views | INTEGER | Engagement metrics |
| language | INTEGER | Steam language ID. Dead: NULL in the live database |
| visibility, banned, ban_reason, app_name, file_type | Various | Steam metadata |
| status | INTEGER | 200 = fetched, -1 = dead, 500 = retry, NULL = discovered but never fetched |
| api_priority | INTEGER | Steam API fetch queue priority |
| translation_priority | INTEGER | Translation-queue mirror (0 = no queued fields) |
| wilson_favorite_score, wilson_subscription_score | REAL | Wilson lower-bound scores (0-1), NULL default |
| needs_web_scrape | INTEGER | Priority for web scraping (10=detail, 5=list, 3=new, 1=backlog, 0=done) |
| needs_image | INTEGER | Priority for image download (same scale as needs_web_scrape) |
| image_extension | TEXT | File extension of downloaded image (e.g., "jpg"), NULL if not downloaded |
| is_queued_for_subscription | INTEGER | Subscription queue flag, set by the TUI and web UI and cleared when the userscript reports an outcome. Transient: reads 0 when nothing is queued |

The `CREATE TABLE` statement still declares the historical names (`dt_found`, `dt_updated`,
`dt_attempted`, `dt_translated`, `time_created`, `time_updated`) and a legacy `tags` column. A fresh
database starts at `user_version = 0` and runs the entire migration chain, whose earlier steps read
those names, so the statement is deliberately historical; after the chain runs, the table has the
v14 columns above.

### `users` — creator profiles

| Column | Type | Purpose |
|---|---|---|
| steamid | INTEGER PK | Steam user ID |
| personaname, personaname_en | TEXT | Display name and translation |
| api_fetched_at, translated_at | INTEGER | Our clocks: profile refresh and translation time |
| translation_priority | INTEGER | Priority for name translation |

### `translation_queue` — batched translation work items

| Column | Type | Purpose |
|---|---|---|
| id | INTEGER PK AUTO | Queue entry ID |
| item_type | TEXT | "item" or "user" |
| item_id | INTEGER | workshop_id or steamid |
| field | TEXT | Column name to translate (e.g., "title_en") |
| original_text | TEXT | Source text |
| priority | INTEGER | Priority level |
| queued_at | INTEGER | Our clock: when queued (epoch). NULL on pre-v14 rows, where the time is unknown |

### `tags` — normalized tag names

| Column | Type | Purpose |
|---|---|---|
| tag_id | INTEGER PK | Tag ID (varint-encoded, top 127 most common optimized to 1 byte) |
| tag_name | TEXT UNIQUE | Exact tag name string |

### `workshop_tags` — item-to-tag junction

| Column | Type | Purpose |
|---|---|---|
| workshop_id | INTEGER | FK to workshop_items |
| tag_id | INTEGER | FK to tags |
| PRIMARY KEY | (workshop_id, tag_id) | WITHOUT ROWID for space efficiency |

Index on `(tag_id)` for reverse lookups ("all items with this tag").

### `workshop_fts` — FTS5 full-text search

Virtual table (content-sync with `workshop_items`, `content_rowid='workshop_id'`). Columns: `title, title_en, short_description, short_description_en, extended_description, extended_description_en`. Added in v5, where it is populated once with an FTS5 `'rebuild'`.

**The index is kept in sync by triggers.** Added in v5, where it is populated once with an FTS5 `'rebuild'`; v15 rebuilds it again and installs `workshop_items_fts_insert`, `workshop_items_fts_delete` and `workshop_items_fts_update`, so the index tracks every write to the six indexed columns. Before v15 it drifted: it held 640,471 documents against 1,725,544 items. See [search-filter.md](search-filter.md) for how matches are built from it.

### `app_tracking` — per-AppID discovery state

| Column | Type | Purpose |
|---|---|---|
| appid | INTEGER PK | Steam AppID |
| last_page_scanned | INTEGER | Page number for page-based discovery |
| last_cursor | TEXT | Cursor for cursor-based discovery |
| window_size | INTEGER | View window size |
| filter_text, required_tags, excluded_tags | TEXT | Legacy filter columns |
| enrichment_filters | TEXT | JSON filter array for enrichment gating |

---

## Indexes

| Index | Column(s) | Purpose |
|---|---|---|
| idx_time_created | steam_created_at | "Created Time" sort (historical index name) |
| idx_time_updated | steam_updated_at | "Updated Time" sort (historical index name) |
| idx_api_fetched_at | api_fetched_at | "Fetched Time" sort, user staleness |
| idx_scraped_version | scrape_version | Scrape staleness, priority ordering |
| idx_status_scraped_version | (status, scrape_version) | Scrape item selection |
| idx_creator_api_fetched_at | (creator, api_fetched_at) | Author filtering with staleness |
| idx_appid_status | (consumer_appid, status) | AppID + status filtering |
| idx_consumer_appid | consumer_appid | AppID filtering |
| idx_status | status | Status filtering |
| idx_creator | creator | Author ID search |
| idx_title | title | Title search/sort |
| idx_title_en | title_en | Translated title search |
| idx_short_description | short_description | Description search |
| idx_short_description_en | short_description_en | Translated description search |
| idx_extended_description | extended_description | Extended description search |
| idx_extended_description_en | extended_description_en | Translated extended description search |
| idx_filename | filename | Filename search |
| idx_language | language | Language ID search |
| idx_file_size | file_size | File Size sort |
| idx_subscriptions | subscriptions | "Subs" sort |
| idx_favorited | favorited | "Favs" sort |
| idx_views | views | "Views" sort |
| idx_wilson_subscription_score | wilson_subscription_score | "Subscriber Score" sort |
| idx_wilson_favorite_score | wilson_favorite_score | "Favorite Score" sort |
| idx_translation_priority | translation_priority | Translation queue scanning |
| idx_is_queued | is_queued_for_subscription | Subscription queue scan |
| idx_workshop_tags_tag_id | workshop_tags.tag_id | Reverse tag lookup |

---

## Migration History

### Migration system (`initialize_database`)

On startup, reads `PRAGMA user_version` and runs all unapplied migrations sequentially within the same database connection. Each migration sets `PRAGMA user_version = N` on completion. The connection is not wrapped in a single transaction across migrations — each migration commits independently, allowing crash recovery on a per-migration basis.

### v0 → v1: Wilson scores

Adds `wilson_favorite_score` and `wilson_subscription_score` columns via `_safe_add_columns`. Computes initial values for all existing items using `wilson_lower` with their subscription/favorite counts.

### v1 → v2: Tag normalization

Iterates all items with non-empty tags, validates JSON, and normalizes malformed entries via `normalize_tags`. Handles dict-format (`{"tag": "name"}`) and list-format tags.

### v2 → v3: Web scrape flag, translation queue

Adds `needs_web_scrape` column. Sets `needs_web_scrape = 1` for items missing extended_description. Creates `translation_queue` table. Backfills existing `translation_priority` values into translation_queue entries.

### v3 → v4: Image download flag

Adds `needs_image` and `image_extension` columns. Sets `needs_image = 1` for items with `preview_url` but no `image_extension`.

### v4 → v5: FTS5 full-text search

Creates `workshop_fts` virtual table (content-sync with `workshop_items`). Populates via `INSERT INTO workshop_fts(workshop_fts) VALUES ('rebuild')`. Adds indexes on `_en` translated fields, `filename`, `language`, and `file_size`.

### v5 → v6: Normalized tag schema

Creates `tags` and `workshop_tags` tables. Populates in two phases: Phase 1 collects all unique tag names and bulk-creates IDs via `_ensure_tag_ids`. Phase 2 iterates all items, builds an in-memory `{name: id}` lookup, and inserts `workshop_tags` rows in batches of 10K with progress logging and intermediate commits. Drops the JSON `tags` column from `workshop_items`. Calls `compact_tag_ids` to reorder for space efficiency.

**Idempotency**: Phase 2 uses `INSERT OR IGNORE`. Phase 1 checks `PRAGMA table_info` for `tags` column existence before attempting population. If the column was already dropped by a previous partial run, population is skipped.

### v6 → v7: Unix epoch timestamps

Converts all 8 `dt_*` columns across 3 tables from ISO 8601 TEXT to Unix epoch INTEGER. For each column: drops dependent indexes, adds an `_new` INTEGER column, converts via `strftime('%s', col)`, drops the old TEXT column, renames `_new` to the original name. Rebuilds indexes. Idempotent — checks `PRAGMA table_info` before each column to skip already-converted columns or resume partial runs (where `_new` exists but rename didn't complete).

### v7 → v8: Sort column indexes

Creates indexes on `time_created`, `time_updated`, `file_size`, `subscriptions`, `favorited`, `views`, `wilson_subscription_score`, and `wilson_favorite_score` for fast ORDER BY.

### v8 → v9: Favorite-score denominator

Recalculates `wilson_favorite_score` for every item with a non-NULL `favorited` count, using `lifetime_subscriptions` as the denominator. The Wilson lower bound is re-implemented inline in the migration.

### v9 → v10: Subscription-queue index

Creates `idx_is_queued` on `is_queued_for_subscription`.

### v10 → v11: Repurposing the `dt_*` columns

A data migration that reassigns meaning rather than renaming:

1. `dt_attempted` → `dt_found` where `dt_found` is NULL (best approximation of first discovery).
2. `dt_attempted` → `dt_updated` where `dt_updated` is NULL.
3. `dt_attempted = time_updated` where `time_updated` is not NULL, turning the fetch time into a Steam version marker.
4. `dt_translated = time_updated` where a translation exists and `time_updated` is not NULL.

This is the migration that gave the old fetch-time column its dual meaning; migration 13→14 separates the two meanings again.

### v11 → v12: Fetch-queue priority

Adds `api_priority INTEGER NOT NULL DEFAULT 0` if it is absent. Sets `api_priority = 3` for never-scraped items (`status IS NULL`) and `api_priority = 1` for items whose `dt_updated` is older than 30 days.

### v12 → v13: Image folder buckets

Moves downloaded preview images from `images/{workshop_id}.{ext}` into three-level hexadecimal hash bucket directories (`images/ab/cd/ef/{workshop_id}.{ext}`), skipping files that are missing or already migrated. This is a filesystem migration, not a schema change.

### v13 → v14: Three-clock rename + fetch-semantics cleanup

Renames the timestamps so the three clocks are unambiguous, and cleans up data
that the old shared names had made misleading. All renames are
`ALTER TABLE ... RENAME COLUMN` (metadata-only; no table data is rewritten).
`workshop_fts` is untouched — none of its indexed columns are renamed.

| Table | Old | New | Meaning |
|---|---|---|---|
| workshop_items | `time_created` | `steam_created_at` | Steam clock: author created it |
| workshop_items | `time_updated` | `steam_updated_at` | Steam clock: author last updated it |
| workshop_items | `dt_found` | `first_seen_at` | our clock: row first inserted |
| workshop_items | `dt_updated` | `api_fetched_at` | our clock: last **successful** content pull |
| workshop_items | `dt_attempted` | `scrape_version` | **Steam value**: `steam_updated_at` when the web scraper ran |
| workshop_items | `dt_translated` | `translate_version` | **Steam value**: `steam_updated_at` when translation ran |
| workshop_items | — | `last_fetch_attempted_at` | our clock: last fetch attempt, success **or** failure |
| users | `dt_updated` | `api_fetched_at` | our clock |
| users | `dt_translated` | `translated_at` | our wall-clock time (users have no `steam_updated_at`) |
| translation_queue | `dt_queued` | `queued_at` | our clock: queue time (INTEGER epoch; NULL = unknown) |

Data cleanup performed by the migration:

* `last_fetch_attempted_at` is backfilled from the old `dt_updated`, whose
  history genuinely *is* attempt times.
* `scrape_version` is set NULL where `steam_updated_at IS NULL` — the failed-fetch
  rows whose old `dt_attempted` held an attempt time rather than a Steam version
  (1,016 rows in the production snapshot; see
  [live-data-profile.md](live-data-profile.md)). Those values are already
  preserved in `first_seen_at`.
* `api_fetched_at` is set NULL where `steam_updated_at IS NULL`: rows that never
  received an API payload must not inherit pure attempt times under a name that
  promises success. **This is the best available approximation** — for a row
  that succeeded once and then failed a later attempt, the old `dt_updated`
  holds the *failure* time and the true last-success time cannot be recovered
  from existing data. It self-corrects on the next successful fetch.
* `first_seen_at` is repaired from `api_fetched_at` for the single anomalous row
  that had `first_seen_at IS NULL`.
* `queued_at` is deliberately **not** backfilled: the pre-existing queue backlog
  keeps NULL (unknown), and `get_next_batch_for_translation` orders with
  `queued_at IS NOT NULL, queued_at ASC` so those unknown-time rows stay ahead
  of newly queued work.

Indexes recreated under clear names: `idx_api_fetched_at`, `idx_scraped_version`,
`idx_status_scraped_version`, `idx_creator_api_fetched_at`; the old `idx_dt_*`
names are dropped. `idx_time_created` / `idx_time_updated` keep their historical
names (SQLite rewrites their definitions to the renamed columns).

### v14 → v15: Full-text index rebuild + sync triggers

Repairs the full-text index and stops it drifting again.

* `workshop_fts` is rebuilt from `workshop_items` with the FTS5 `'rebuild'`
  command. It had been populated once by migration 4→5 with no triggers and no
  later rebuild, so it held 640,471 documents against 1,725,544 items — 62.9% of
  the library absent. After the rebuild the two counts match.
* Three triggers are installed on `workshop_items`:
  `workshop_items_fts_insert`, `workshop_items_fts_delete`, and
  `workshop_items_fts_update`.
* Because `workshop_fts` is an **external-content** table, the delete and update
  triggers remove the previous row with the FTS5 `'delete'` command carrying the
  old column values. `DELETE FROM workshop_fts` is not valid for external content
  and would leave stale tokens behind.
* The update trigger is scoped `AFTER UPDATE OF` the six indexed columns
  (`title`, `title_en`, `short_description`, `short_description_en`,
  `extended_description`, `extended_description_en`). Most writes to
  `workshop_items` are queue/priority updates that touch none of them, so an
  unscoped trigger would rewrite part of the index on every priority bump.
* The rebuild is committed and checkpointed on its own before the triggers are
  created, following migration 13→14's rule about not holding a large WAL across
  later DDL on this filesystem.

Measured on a copy of the production snapshot (1,725,544 items), through
`initialize_database`: the whole 13→15 run takes ~18 s and grows the database by
~158 MB. Re-running the migration is a no-op beyond one more rebuild, and
`initialize_database` remains idempotent.

### v16 → v17: Dead items leave every work queue

Clears the queue flags on rows already marked dead (`status = -1`):

```sql
UPDATE workshop_items
SET needs_web_scrape = 0, needs_image = 0, translation_priority = 0
WHERE status = -1
```

Before this version the permanent-failure path cleared only `api_priority` when
it marked an item dead. The web, image and translation polls select on their own
flag alone with no dead-item guard, so a row still flagged there was retried
forever: it could never complete, so the queue never drained and the scraper
spent requests on a page that no longer exists. About ten thousand rows on the
production database. `api_priority` is deliberately **not** touched — the 404
path already zeroes it, and a dead row still holding an API priority is a
separate defect. The reported count is the number of dead rows the statement
matched; the update is idempotent, so re-running leaves already-cleared rows
clear. The daemon change in the same release stops new dead rows from being
flagged in the first place.

### v17 → v18: Description-less items return to the scrape queue

Puts rows that were marked done without ever producing a description back into
the web-scrape queue:

```sql
UPDATE workshop_items SET needs_web_scrape = 1
WHERE needs_web_scrape = 0
  AND COALESCE(extended_description, '') = ''
  AND (status IS NULL OR status <> -1)
```

Before `17894f7` the web worker tested the truthy dict `scrape_extended_details`
returns rather than the `description` inside it. A page whose selector did not
match therefore came back as a completed scrape: `extended_description` NULL with
`needs_web_scrape = 0`, and the item left the queue permanently. On the
2026-09-12 snapshot that stranded 63,229 rows, against 33,966 (2.0%) that hold a
description. Priority `1` is the backlog level migration 15→16 used for its own
stranded rows: retried, but below the `3/5/10` of new and current work, so the
recovered rows cannot jump ahead of the live queue. Dead rows (`status = -1`) are
excluded because they can never complete, and `api_priority`, `needs_image` and
`translation_priority` are deliberately **not** touched — those are separate
queues with their own work. `cursor.rowcount` counts the rows the statement
matched rather than the rows it changed, but every matched row here moves from
`0` to `1`, so the reported count is exact. The web worker change in the same
release stops a page that was never the item's from being cleared the same way.

---

## Database Utility Functions

### `get_connection` (database)

Opens a new SQLite connection with WAL mode enabled and `row_factory = sqlite3.Row` for dict-like row access. Foreign-key enforcement is **not** enabled (`PRAGMA foreign_keys` is left at its default), which matches the schema: no foreign-key constraints are declared. Each caller is responsible for closing the connection.

### `initialize_database` (database)

Creates tables (using `IF NOT EXISTS`), runs all pending migrations, creates indexes. This is called on every startup by the daemon, TUI, and web runner. Idempotent and safe to call on an existing database.

### `_safe_add_columns` (database)

Adds columns to an existing table, catching `OperationalError` for duplicates. Used by migrations 0→1 and others to add columns that may already exist from a previous partial run.

### `insert_or_update_item` (database)

Upserts an item row using `INSERT ... ON CONFLICT(workshop_id) DO UPDATE SET`. Filters keys against `WORKSHOP_ITEM_COLUMNS` frozenset before building the SQL. Handles tags via junction table (parses JSON, calls `_ensure_tag_ids`, updates `workshop_tags`). Tags are excluded from the INSERT column list since they're no longer a workshop_items column.

### `insert_or_update_user` (database)

Same upsert pattern for `users` table, using `USER_COLUMNS` frozenset for filtering.

### `get_item_details` (database)

Returns all columns for a single workshop_id, joined with users table. Tags are returned as comma-separated via a correlated `GROUP_CONCAT` subquery against the junction table.

### `count_unscraped_items` (database)

Counts items where `api_fetched_at IS NULL` (never successfully fetched) — used to determine if the processing queue needs more items.

### `toggle_subscription_queue_status` / `get_queued_items` (database)

Simple toggle and retrieval for the subscription queue feature.
