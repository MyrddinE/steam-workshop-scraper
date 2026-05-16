# Schema & Migrations

The database uses SQLite with WAL mode. Schema evolution follows a `PRAGMA user_version` increment pattern where each migration is a discrete `if db_version < N:` block within `initialize_database`, run sequentially on startup. Fresh databases run all migrations; existing databases run only pending ones.

---

## Current Schema (v8)

### `workshop_items` — main item table

Primary key: `workshop_id INTEGER PRIMARY KEY` (aliased from rowid). Columns:

| Column | Type | Purpose |
|---|---|---|
| workshop_id | INTEGER PK | Steam published file ID |
| title, title_en | TEXT | Original and English-translated title |
| creator | INTEGER | FK to users.steamid |
| creator_appid, consumer_appid | INTEGER | App that created/uses the item |
| filename, file_size | TEXT, INTEGER | File metadata |
| preview_url | TEXT | Preview image URL from Steam API |
| short_description, short_description_en | TEXT | Short description and translation |
| extended_description, extended_description_en | TEXT | Full description (populated by web scraper) and translation |
| time_created, time_updated | INTEGER | Steam-side Unix timestamps |
| dt_found, dt_updated, dt_attempted, dt_translated | INTEGER | Daemon-managed Unix epoch timestamps (converted from TEXT in v7) |
| subscriptions, lifetime_subscriptions | INTEGER | Current and lifetime subscriber counts |
| favorited, lifetime_favorited, views | INTEGER | Engagement metrics |
| tags_text | TEXT | Space-separated tag names (denormalized, for FTS5 — added in v6 but reverted) |
| language | INTEGER | Steam language ID |
| visibility, banned, ban_reason, app_name, file_type | Various | Steam metadata |
| status | INTEGER | 200 = fetched, 404/other = API error |
| translation_priority | INTEGER | 0 = done, > 0 = queued for translation |
| wilson_favorite_score, wilson_subscription_score | REAL | Wilson lower-bound scores (0-1), NULL default |
| needs_web_scrape | INTEGER | Priority for web scraping (10=detail, 5=list, 3=new, 1=backlog, 0=done) |
| needs_image | INTEGER | Priority for image download (same scale as needs_web_scrape) |
| image_extension | TEXT | File extension of downloaded image (e.g., "jpg"), NULL if not downloaded |
| is_queued_for_subscription | INTEGER | 0/1 toggle for subscription queue |

### `users` — creator profiles

| Column | Type | Purpose |
|---|---|---|
| steamid | INTEGER PK | Steam user ID |
| personaname, personaname_en | TEXT | Display name and translation |
| dt_updated, dt_translated | INTEGER | Daemon timestamps (epoch) |
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
| dt_queued | INTEGER | When queued (epoch), NULL until popped |

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

Virtual table (content-sync with `workshop_items`, `content_rowid='workshop_id'`). Columns: `title, title_en, short_description, short_description_en, extended_description, extended_description_en`. Automatically stays in sync with the base table — no triggers needed. Added in v5.

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

### Sort-related (added in v8)

| Index | Column | Purpose |
|---|---|---|
| idx_time_created | time_created | "Created Time" sort |
| idx_time_updated | time_updated | "Updated Time" sort |
| idx_file_size | file_size | "File Size" sort |
| idx_subscriptions | subscriptions | "Subs" sort |
| idx_favorited | favorited | "Favs" sort |
| idx_views | views | "Views" sort |
| idx_wilson_subscription_score | wilson_subscription_score | "Subscriber Score" sort |
| idx_wilson_favorite_score | wilson_favorite_score | "Favorite Score" sort |

### General

| Index | Column(s) | Purpose |
|---|---|---|
| idx_consumer_appid | consumer_appid | AppID filtering |
| idx_status | status | Status filtering |
| idx_dt_updated | dt_updated | "Fetched Time" sort, user staleness |
| idx_dt_attempted | dt_attempted | Scrape staleness, priority ordering |
| idx_title | title | Title search/sort |
| idx_creator | creator | Author ID search |
| idx_short_description | short_description | Description search |
| idx_extended_description | extended_description | Extended description search |
| idx_tags_text | tags_text | Tag text search (reverted in schema cleanup) |
| idx_translation_priority | translation_priority | Translation queue scanning |
| idx_title_en | title_en | Translated title search |
| idx_short_description_en | short_description_en | Translated description search |
| idx_extended_description_en | extended_description_en | Translated extended description search |
| idx_filename | filename | Filename search |
| idx_language | language | Language ID search |
| idx_file_size | file_size | File Size sort (duplicated in v8 migration) |

### Composite

| Index | Columns | Purpose |
|---|---|---|
| idx_status_dt_attempted | (status, dt_attempted) | Scrape item selection (status filter + staleness sort) |
| idx_appid_status | (consumer_appid, status) | AppID + status filtering |
| idx_creator_dt_updated | (creator, dt_updated) | Author filtering with staleness |
| idx_workshop_tags_tag_id | (tag_id) | Reverse tag lookup (on workshop_tags) |

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

---

## Database Utility Functions

### `get_connection` (database)

Opens a new SQLite connection with WAL mode and `foreign_keys = ON`. Returns a `sqlite3.Connection` with `row_factory = sqlite3.Row` for dict-like row access. Each caller is responsible for closing the connection.

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

Counts items where `dt_attempted IS NULL` — used to determine if the processing queue needs more items.

### `toggle_subscription_queue_status` / `get_queued_items` (database)

Simple toggle and retrieval for the subscription queue feature.
