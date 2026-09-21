# Schema & Migrations

The database uses SQLite with WAL mode. Schema evolution follows a `PRAGMA user_version` increment pattern. Each migration is its own function in `src/database.py`, named `_migration_<from>_to_<to>`, and the ordered `MIGRATIONS` table maps each target version to its function. `initialize_database` is a short driver, and the path it takes is decided by the database's **recorded version**, never by whether the file exists:

- a database **newer** than `EXPECTED_VERSION` is refused outright with `SchemaVersionError` — before the journal mode is set and before any schema statement — so a refused start leaves the file byte-for-byte untouched. The message names the file and both versions and says the remedy: replace this build with one that expects at least the recorded version. The database is not corrupt, so it must not be deleted or rewritten, and rolling back to an older build is not a way out once the schema renames have run (see [`SchemaVersionError`](#schemaversionerror-database));
- a **fresh** database (`user_version = 0`) is built directly at `EXPECTED_VERSION` by `_create_current_schema`, with no migrations replayed;
- a fresh database built with `legacy_chain=True` takes the historical shape from `_create_legacy_schema` and runs every migration — this is how the chain stays exercised;
- an **existing** database (`user_version > 0`) always takes `_create_legacy_schema` followed by its pending migrations, whatever the flag says, because the chain is the only thing that can carry it forward.

A **pending** migration is DDL that rewrites tables — 34→35's `DROP COLUMN` rewrote `workshop_items` and took *measured live* **283 s** in production — and the daemon is a detached process that may still be writing to them. So the two UI entry points do not call `initialize_database` directly. They call `initialize_database_with_daemon_stopped` (`src/daemon_control.py`), which reads the recorded `user_version` read-only first, stops a running daemon through `DaemonController.stop()` when — and only when — a migration is pending, refuses to migrate when that stop did not succeed, migrates, and restarts the daemon afterwards. A database already at `EXPECTED_VERSION` leaves a running daemon untouched: that is the ordinary relaunch. The daemon's own startup still calls `initialize_database` directly, because a daemon performs its own pending migrations before it begins writing; see [threading.md](threading.md#a-pending-migration-and-a-running-daemon).

The two endpoints must be identical. `tests/test_fresh_schema_path.py::test_schema_equivalence` builds one database each way and fails the moment they diverge; see [Adding the next migration](#adding-the-next-migration-target-v38) for what that means when you add one.

---

## Current Schema (v37)

The application-level reference for every table and column is
[data-model.md](data-model.md); the timestamp conventions are in
[timestamps.md](timestamps.md). This is the storage-level summary.

### `workshop_items` — main item table

Primary key: `workshop_id INTEGER PRIMARY KEY` (aliased from rowid). Columns:

| Column | Type | Purpose |
|---|---|---|
| workshop_id | INTEGER PK | Steam published file ID |
| title, title_en | TEXT | Original and English-translated title |
| creator_steamid | INTEGER | Reference to `creators.steamid` (no FK constraint; joined with `LEFT JOIN`) |
| creator_appid, consumer_appid | INTEGER | App that created/uses the item |
| filename, file_size | TEXT, INTEGER | File metadata |
| preview_url | TEXT | Preview image URL from Steam API |
| short_description, short_description_en | TEXT | Short description and translation |
| extended_description, extended_description_en | TEXT | Full description (populated by web scraper) and translation |
| steam_created_at, steam_updated_at | INTEGER | Steam's clock, Unix epoch seconds |
| first_seen_at, api_fetched_at, last_fetch_attempted_at, translate_version | INTEGER | Our clocks and the stored Steam version key |
| web_scraped_at, image_fetched_at, translated_at | INTEGER | Our completion clocks for the web scrape, image download and translation stages (v27). NULL on every row that predates v27 and on any stage that has not succeeded since |
| subscriptions, lifetime_subscriptions | INTEGER | Current and lifetime subscriber counts |
| favorited, lifetime_favorited, views | INTEGER | Engagement metrics |
| visibility, banned, ban_reason, app_name, file_type | Various | Steam metadata |
| fetch_status | INTEGER | 200 = fetched, -1 = dead, 500 = retry, NULL = discovered but never fetched |
| api_priority | INTEGER | Steam API fetch queue priority |
| translation_priority | INTEGER | Translation-queue mirror (0 = no queued fields) |
| wilson_favorite_score, wilson_subscription_score | REAL | Wilson lower-bound scores (0-1), NULL default |
| web_scrape_priority | INTEGER | Priority for web scraping (10=detail, 5=list, 3=new, 1=backlog, 0=done). Renamed from `needs_web_scrape` in v33: it holds a priority, not a boolean |
| image_priority | INTEGER | Priority for image download (same scale as web_scrape_priority). Renamed from `needs_image` in v33 |
| image_answer | TEXT | The download's answer, not only a file extension: a real extension (e.g. "jpg"), a wholly numeric HTTP status, or a served non-image token; NULL if nothing is recorded. Renamed from `image_extension` in v33 |
| is_queued_for_subscription | INTEGER | Subscription queue flag, set by the TUI and web UI and cleared when the userscript reports an outcome. Transient: reads 0 when nothing is queued |
| own_subscribed | INTEGER | Whether the owner (the account whose key and cookies are configured) is subscribed to this item right now. Reconciled from Steam; not the item-wide `subscriptions` count |
| own_first_subscribed_at | INTEGER | When we first *saw* the owner subscribed; sticky, and the only source of the `previously` state |
| steam_download_seen_at | INTEGER | One-way local latch: when this app first saw Steam's downloaded copy of a subscribed item on disk (v26). Set only by `src/workshop_folders`, cleared only beside `own_subscribed` when the item leaves the subscription list. NULL means not confirmed on disk. Renamed from `downloaded_at` in v33: it is a sighting latch, not a completion clock |

The columns above are what the database holds at v37, and they are reached two ways.
`_create_current_schema` creates them directly, so a fresh database starts at `EXPECTED_VERSION`
with these names. `_create_legacy_schema`'s `CREATE TABLE` instead declares the historical names
(`dt_found`, `dt_updated`, `dt_attempted`, `dt_translated`, `time_created`, `time_updated`) and a
legacy `tags` column, because a `legacy_chain` database starts at `user_version = 0` and runs the
entire migration chain, whose earlier steps read those names; migration 13→14 renames them and 5→6
drops `tags`. The two endpoints must agree, which
`tests/test_fresh_schema_path.py::test_schema_equivalence` enforces.

### `creators` — creator profiles

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
| entity_type | TEXT | "item" or "user" |
| entity_id | INTEGER | workshop_id or steamid, per entity_type |
| field | TEXT | Column name to translate (e.g., "title_en") |
| original_text | TEXT | Source text |
| priority | INTEGER | Priority level |
| queued_at | INTEGER | Our clock: when queued (epoch). NULL on pre-v14 rows, where the time is unknown |

Indexed by `idx_translation_queue_lookup` on `(entity_type, entity_id, field)`, created by both schema
builders rather than by a migration (see [Indexes](#indexes) for why).

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

### `app_discovery` — per-AppID discovery state

| Column | Type | Purpose |
|---|---|---|
| appid | INTEGER PK | Steam AppID |
| last_cursor | TEXT | Cursor for cursor-based discovery. Kept when a walk finishes |
| cursor_walk_finished | INTEGER NOT NULL DEFAULT 0 | `1` once the cursor walk stopped for lack of new items (issue 68, v37). `seed_database` skips a finished AppID's cursor scan; never reset automatically |
| filter_text, required_tags, excluded_tags | TEXT | Legacy filter columns |
| enrichment_filters | TEXT | JSON filter array for enrichment gating |

---

## Indexes

| Index | Column(s) | Purpose |
|---|---|---|
| idx_time_created | steam_created_at | "Created Time" sort (historical index name) |
| idx_time_updated | steam_updated_at | "Updated Time" sort (historical index name) |
| idx_api_fetched_at | api_fetched_at | "Fetched Time" sort, user staleness |
| idx_creator_steamid_api_fetched_at | (creator_steamid, api_fetched_at) | Author filtering with staleness |
| idx_appid_fetch_status | (consumer_appid, fetch_status) | AppID + status filtering |
| idx_consumer_appid | consumer_appid | AppID filtering |
| idx_fetch_status | fetch_status | Status filtering |
| idx_creator_steamid | creator_steamid | Author ID search |
| idx_title | title | Title search/sort |
| idx_title_en | title_en | Translated title search |
| idx_short_description | short_description | Description search |
| idx_short_description_en | short_description_en | Translated description search |
| idx_extended_description | extended_description | Extended description search |
| idx_extended_description_en | extended_description_en | Translated extended description search |
| idx_filename | filename | Filename search |
| idx_file_size | file_size | File Size sort |
| idx_subscriptions | subscriptions | "Subs" sort |
| idx_favorited | favorited | "Favs" sort |
| idx_views | views | "Views" sort |
| idx_wilson_subscription_score | wilson_subscription_score | "Subscriber Score" sort |
| idx_wilson_favorite_score | wilson_favorite_score | "Favorite Score" sort |
| idx_translation_priority | translation_priority | Translation queue scanning |
| idx_translation_queue_lookup | translation_queue (entity_type, entity_id, field) | Per-field queue lookup and the 22→23 repair's two-column `NOT EXISTS` (that step names the columns' pre-rename spelling; SQLite rewrites the definition in place at 33→34). Created in `_create_legacy_schema` (unversioned, resolving the two column names for whichever side of the rename it finds) and mirrored in `_create_current_schema`, so the index exists when the repair runs |
| idx_translation_queue_poll | translation_queue (priority DESC, queued_at ASC) | Translation poll (`get_next_batch_for_translation`) ordering. Created in `_ensure_indexes`, which runs after 13→14 renames `dt_queued` to `queued_at` |
| idx_web_scrape_queue | (web_scrape_priority DESC, api_fetched_at ASC) WHERE web_scrape_priority > 0 | Web scrape worker poll and web queue breakdown (v25). Named for the queue, not the column, so its name stays across v33 |
| idx_image_queue | (image_priority DESC, api_fetched_at ASC) WHERE image_priority > 0 | Image worker poll and image queue breakdown (v25). Named for the queue, not the column, so its name stays across v33 |
| idx_api_queue | (api_priority DESC, api_fetched_at ASC) WHERE api_priority > 0 | API fetch worker poll and fetchable count (v25) |
| idx_web_scraped_at | web_scraped_at WHERE web_scraped_at IS NOT NULL | Web scrape throughput and last-success metric (v27) |
| idx_image_fetched_at | image_fetched_at WHERE image_fetched_at IS NOT NULL | Image throughput and last-success metric (v27) |
| idx_translated_at | translated_at WHERE translated_at IS NOT NULL | Translation throughput and last-success metric (v27) |
| idx_is_queued | is_queued_for_subscription | Subscription queue scan |
| idx_workshop_tags_tag_id | workshop_tags.tag_id | Reverse tag lookup |

---

## Migration History

### Migration system (`initialize_database`)

`initialize_database` (`src/database.py`) is a short driver. It:

1. opens the connection and reads `PRAGMA user_version` — read *first*, before
   the journal mode and before any schema statement, so the refusal below can
   leave the file untouched;
2. refuses a database whose recorded version is **higher** than
   `EXPECTED_VERSION` by raising `SchemaVersionError`, and writes nothing: no
   journal-mode switch, no `CREATE`/`ALTER`, no `_ensure_indexes`, no version
   write. The connection is closed first, so the refused file is not left locked
   (see [`SchemaVersionError`](#schemaversionerror-database));
3. sets `PRAGMA journal_mode=WAL`;
4. branches on the recorded version:
   - a fresh file (`user_version = 0`) with `legacy_chain=False` calls
     `_create_current_schema(cursor, conn)`, which builds the current schema
     directly and records `EXPECTED_VERSION`;
   - otherwise it calls `_create_legacy_schema(cursor, conn)` and then runs every
     entry in the module-level `MIGRATIONS` table whose target version is above
     the recorded one, in ascending order. `legacy_chain=True` is the only way a
     fresh file arrives here; an existing database (`user_version > 0`) always
     does;
5. calls `_ensure_indexes(cursor)`, then commits and closes.

`MIGRATIONS` is an ordered list of `(target version, function)` pairs, from
`(1, _migration_0_to_1)` to `(37, _migration_36_to_37)`. The functions are
defined in `src/database.py` immediately above the table, in that same ascending
order, so the file still reads as the schema's history top to bottom; each
function body is the migration exactly as it stood at its version.

The functions live beside the driver rather than in a separate `migrations`
module because most of them call helpers defined in `database.py`
(`normalize_tags`, `_ensure_tag_ids`, `compact_tag_ids`, `get_image_subdirs`,
`_demote_filtered_out_queue_priorities`). A module that imported those helpers
while `database.py` imported the migration table would be circular, so the
extraction keeps them together.

A migration is callable in isolation now: `_migration_19_to_20(cursor, conn,
db_path)` runs one step against a database whose schema already sits at its base,
which is what lets a test exercise one migration without replaying the chain.
Each migration still sets `PRAGMA user_version = N` on completion. The connection
is not wrapped in a single transaction across migrations — each migration commits
independently, allowing crash recovery on a per-migration basis.
`tests/test_migration_table.py` pins that the table is contiguous and ascending
from 1 to `EXPECTED_VERSION`, that each entry names the function for its own
version, and that a fresh database reaches `EXPECTED_VERSION`.

### Adding the next migration (target v38)

1. bump `EXPECTED_VERSION` in `src/database.py` to `38`;
2. append `def _migration_37_to_38(cursor, conn, db_path): ...` immediately
   after `_migration_36_to_37`, keeping the body self-contained and preserving
   what the step meant at v37 (no tidying an older step, no changing a
   `PRAGMA user_version = N` target);
3. append `(38, _migration_37_to_38)` as the last entry of `MIGRATIONS`;
4. add a `### v37 → v38: ...` entry below, in the same shape as the others;
5. **mirror the step in `_create_current_schema`.** It is the shape a fresh
   database is created at now, so a schema change that lands only in the chain
   moves the legacy endpoint and not the fresh one. Update the table, index or
   trigger definition there to the step's terminal shape, exactly as the step
   leaves it. A pure data migration (no DDL) needs no change here. A step that
   *drops* an object has two halves: remove the declaration from
   `_create_current_schema`, and, if the object was created by
   `_ensure_indexes`, remove it there too — 34→35 did both;
6. if the step adds a column or table that the historical schema must also
   start with, add it to `_create_legacy_schema` too — a `legacy_chain`
   database begins at `user_version = 0` and runs the whole table, so the two
   builders must agree on the terminal schema. A step that drops a column the
   legacy builder still declares or `_safe_add_columns` still lists needs a
   guard there as well — see `_DROPPED_COLUMN_NAMES`;
7. run `tests/test_fresh_schema_path.py::test_schema_equivalence`, which builds
   one database each way and fails while the two endpoints disagree. This is
   the check that makes the forward rule mechanical rather than a habit.

`tests/test_migration_table.py` fails if the table and `EXPECTED_VERSION`
disagree, so a step cannot be half-added (function without entry, or entry
without function) silently. `test_schema_equivalence` fails if the step is
added to the chain without being mirrored in the current builder, and
`test_default_fresh_path_does_not_replay_the_chain` fails if the chain leaks
back into the fresh default.

### v0 → v1: Wilson scores

Adds `wilson_favorite_score` and `wilson_subscription_score` columns via `_safe_add_columns`. Computes initial values for all existing items using `wilson_lower` with their subscription/favorite counts.

### v1 → v2: Tag normalization

Iterates all items with non-empty tags, validates JSON, and normalizes malformed entries via `normalize_tags`. Handles dict-format (`{"tag": "name"}`) and list-format tags.

### v2 → v3: Web scrape flag, translation queue

Adds `needs_web_scrape` column. Sets `needs_web_scrape = 1` for items missing extended_description. Creates `translation_queue` table. Backfills existing `translation_priority` values into translation_queue entries.

### v3 → v4: Image download flag

Adds `needs_image` and `image_extension` columns. Sets `needs_image = 1` for items with `preview_url` but no `image_extension`.

### v4 → v5: FTS5 full-text search

Creates `workshop_fts` virtual table (content-sync with `workshop_items`). Populates via `INSERT INTO workshop_fts(workshop_fts) VALUES ('rebuild')`. Adds indexes on the `_en` translated fields, `filename`, and `file_size`.

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
  `priority DESC, queued_at ASC`, whose implicit NULL-first ordering keeps those
  unknown-time rows ahead of newly queued work at the same priority. (The old
  explicit `queued_at IS NOT NULL` term was redundant and is gone; see
  [data-model.md](data-model.md#translation_queue).)

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
### v18 → v19: Requeue items stranded by cursor discovery

Requeues rows that were discovered but never fetched, the population cursor
discovery stranded by leaning on the `api_priority` column default:

```sql
UPDATE workshop_items SET api_priority = 1
WHERE status IS NULL AND api_fetched_at IS NULL AND api_priority = 0
```

`CREATE TABLE` declares `api_priority INTEGER NOT NULL DEFAULT 3`, but the
`ALTER TABLE` in migration 11→12 that adds the column to an older database uses
`DEFAULT 0`. Cursor discovery inserted only `{"workshop_id": wid}` and let the
default decide, so on a migrated database — which production is — every
discovered row landed at `0`, meaning "not queued", and the fetch queue
(`api_priority > 0`) never handed it out. Migration 15→16 requeued the rows this
had already produced with the same predicate and priority, but left the cause in
place, so it kept stranding more; the daemon change in the same release passes
the priority explicitly at the cursor discovery insert site, so the two database
histories can no longer diverge. Dead rows (`status = -1`) are excluded by the
`status` predicate, and `needs_web_scrape`, `needs_image` and
`translation_priority` are deliberately untouched: this is an API-fetch queue
repair, not a scrape, image or translation decision. The reported count is the
number of rows the statement matched; the update is idempotent, so re-running
leaves already-queued rows queued.

### v19 → v20: Dead items give up their queue priority

Clears the fetch priority the rows that died before the permanent-failure path
learned to clear it:

```sql
UPDATE workshop_items SET api_priority = 0
WHERE status = -1 AND api_priority > 0
```

This is not an ongoing leak — `_settle_api_failure` clears `api_priority` when it
marks an item dead — it is the rows that were already dead when that line was
added. They matter because `api_priority > 0` is what every count of "queued for
a fetch" reads, and the statistics screen reports dead items still holding a
queue flag as `dead_items_by_queue`. Leaving ten thousand of them there would peg a
detector whose whole value is that it reads zero unless something has regressed.
Only `api_priority` is touched: the other queue flags were cleared by 16→17, and
`status` is what makes an item dead in the first place.

### v20 → v21: The owner's subscription columns

Adds the two columns behind the per-item subscription marker:

| Column | Type | Meaning |
|---|---|---|
| `own_subscribed` | INTEGER DEFAULT 0 | Whether the owner is subscribed to this item right now. |
| `own_first_subscribed_at` | INTEGER DEFAULT NULL | When we first *saw* the owner subscribed; sticky, and the only source of the `previously` state. |

Both are added by `_safe_add_columns(cursor, "workshop_items", [...])` — a fresh
database gets them from the `CREATE TABLE` block, an existing one from the
`ALTER TABLE` — which is the same convention every other later column follows.

The migration body then defaults `own_subscribed` to `0` where SQLite's `ALTER
TABLE` left it NULL:

```sql
UPDATE workshop_items SET own_subscribed = 0 WHERE own_subscribed IS NULL
```

`own_first_subscribed_at` is deliberately left NULL everywhere. A pre-existing row
has no subscription observation behind it, and inventing one would claim we had
seen a subscription we had not — which is exactly the honesty constraint the
`previously` state is documented under in
[data-model.md](data-model.md). Steam exposes no per-account subscription
history, so this column can only mean "first seen by us".

No index is added: the columns are read with the row (the grid and detail
payloads select them outright) and nothing filters or sorts on them.

---

### v21 → v22: Filter-excluded items give up their queue priority

No schema change — the whole migration is data. `needs_web_scrape` and
`needs_image` are priority columns, and the daemon used to hand them the item's
entire pre-fetch `api_priority`, which is `3` for a newly discovered item. An item
that fails its AppID's enrichment filters is still scraped (the filters choose
priority, not membership), so that inheritance put every new item the filters
excluded into the *same band* as the ones they selected — and `MAX(stored, new)`
means nothing ever downgrades it again, so those rows could not repair
themselves.

The daemon no longer inherits a priority the daemon itself set (see
`user_requested_priority` in `src/daemon.py`); this migration repairs the rows it
had already written. For every AppID with a readable filter set
(`get_enrichment_filters`), it walks the items above backlog priority and demotes
the ones the filters exclude:

| Stored | After | Why |
|---|---|---|
| `0` | `0` | Not queued. |
| `1` | `1` | Already backlog. |
| `2`, `3` | `1` | The daemon's own bookkeeping: a stage-failure retry, a discovery. |
| `5`, `10` | `5`, `10` | A person asked for this item. Left alone; see below. |

Only the columns the filter set actually reads are selected alongside the two
priorities, tags are fetched in batches of 900, and the predicate is
`_evaluate_filters` — the same function the fetch path uses — rather than an SQL
translation of the filters. The two evaluators already disagree (SQL search also
searches each field's `_en` counterpart), and a migration whose answer differed
from the runtime would leave a queue the runtime immediately re-stamps. An
unreadable filter set means *enrich everything*, so it demotes nothing: reading a
malformed list as "excludes everything" would be the far more expensive mistake.

A `5` or a `10` is deliberately untouched. Under both the old rule and the new
one, nothing but a user action could have written one there, so demoting it would
overrule a person in order to tidy up after the daemon — and re-queueing an item
someone is looking at at backlog priority is a worse outcome than leaving one
stale entry. *Measured live* on 2026-09-17, before the migration: 868,759 items
sat above backlog priority, of which 760,782 web entries and 668,269 image ones
belonged to excluded items while 107,365 selected items waited behind them.

`EXPECTED_VERSION` is now module level in `src/database.py` rather than a local
inside `initialize_database`, so the migration tests can assert the chain reaches
it without nine files each repeating the number.

---

### v22 → v23: Stranded translation-queue mirrors

No schema change — the whole migration is data. `translation_priority` is a
mirror of `translation_queue`: a producer raises it when it queues a field, and
the translator zeroes it when the item's last queue row is deleted. Before this
version `queue_field_for_translation` wrote the queue row and the mirror on **two
separate connections**, so the translator — which drains the queue on its own
thread — could delete the row and zero the mirror between the two commits, after
which the helper's second statement raised the mirror again from
`MAX(0, priority)`.

An item left that way reads as permanently pending in both front ends and nothing
can clear it: every producer skips a translation that is already current, so the
field that was just translated is never re-queued, and the translator only looks
at `translation_queue`, which is empty. *Measured live* on 2026-09-16: 39 items
carried `translation_priority >= 5` with every non-empty source field translated
at the item's current `steam_updated_at` and no field left to translate.

```sql
UPDATE workshop_items SET translation_priority = 0
WHERE translation_priority > 0
  AND NOT EXISTS (
      SELECT 1 FROM translation_queue q
      WHERE q.item_type = 'item' AND q.item_id = workshop_items.workshop_id
  )
```

Only `workshop_items` is touched. At this version a creator's name translation
lived on `users.translation_priority` alone and never got a `translation_queue`
row, so that mirror was not expected to match this table; **v27 → v28** makes it a
mirror of the queue as well and repairs the creator rows this one skipped. Only
the high direction is repaired: a queue row whose mirror is zero still has its
work picked up, because the translator selects on the queue and not on the
mirror, so re-raising it would be a separate decision. Dead rows are not
special-cased — a mirror with nothing queued is wrong for them too, and
`status = -1` is the dead flag, not `translation_priority`.

The helper change in the same release makes the two writes one transaction, so
the interleaving cannot recur: while the helper holds the write lock, the
translator can only drain before it (row re-queued, mirror raised) or after it
(row deleted, mirror zeroed). The daemon's API merge also drops
`translation_priority` now: it is a read-modify-write around the API call, and a
snapshot carried through it could undo a drain the same way. Migration tests that
seeded a translation priority without a queue row were updated to seed both
halves of the pair, since that was the inconsistency this migration exists to
remove.

---

### v23 → v24: Drop the never-populated `language` column

Removes a Steam-provided column that had no source. `language` was added
expecting the API to return it, but no response this project consumes can:

* `ISteamRemoteStorage/GetPublishedFileDetails` — the detail endpoint the daemon
  calls — has no language field in its response message. The recorded real-world
  body in `tests/test_steam_api.py` carries none.
* The request protocol does have a `language` field, but it is the **viewer's**
  localization parameter: it selects the language the returned `title` and
  `description` are rendered in. The client would set it and never read it back,
  so it is not a property of the item and cannot be stored as one.
* The HTML page parse (`scrape_extended_details`) extracts only the description
  and tags. `QueryFiles` is asked for the fields the merge consumes and no
  language is among them.
* *Measured live*: NULL for all 1,725,544 rows of the 2026-09-12 snapshot
  ([live-data-profile.md](live-data-profile.md)).

So the column advertised data Steam does not provide, drew a permanently
`Language: N/A` line in the web tooltip, and backed a `Language ID` filter that
could never match. The migration drops the index that referenced the column and
then the column itself, following migration 5→6's pattern for the legacy `tags`
column (`ALTER TABLE ... DROP COLUMN` after the dependent index is gone; SQLite
3.53 is in use, so no table rebuild is needed):

```sql
DROP INDEX IF EXISTS idx_language;
ALTER TABLE workshop_items DROP COLUMN language;
```

The drop is guarded on `PRAGMA table_info` so it is idempotent and resumable: a
fresh database never grows the column (it was removed from `CREATE TABLE`, from
`_safe_add_columns`, and from the index list in migration 4→5), and a database
that already dropped it skips cleanly. `ALTER TABLE ... DROP COLUMN` is not
metadata-only internally, so the migration commits it and checkpoints the WAL
before continuing, the same rule migrations 13→14 and 14→15 follow.

`language` is removed from `WORKSHOP_ITEM_COLUMNS`, so it is no longer in the
API merge allow-list either, and the `Language ID` filter alias is gone
(`FILTER_FIELD_TO_COLUMN`). A saved filter that named it now falls through as an unknown
field and is ignored rather than erroring, which is what it effectively did
already: no row ever matched.

---

### v24 → v25: Partial composite indexes for the three work queues

Purely additive — no table or column changes. The web, image and API workers
find the head of their own queue by scanning `workshop_items` and sorting it on
**every poll**, and the web/image statistics breakdowns pay for the same missing
indexes. Each query asks for one of these shapes:

```sql
-- get_next_web_scrape_item / get_next_image_item
SELECT * FROM workshop_items
WHERE needs_web_scrape > 0
ORDER BY needs_web_scrape DESC, api_fetched_at ASC LIMIT 1

-- get_next_items_to_fetch (plus `AND (status IS NULL OR status != -1)`)
SELECT * FROM workshop_items
WHERE api_priority > 0
ORDER BY api_priority DESC, api_fetched_at ASC LIMIT ?

-- _priority_breakdowns (web and image)
SELECT needs_web_scrape AS prio, COUNT(*) AS cnt FROM workshop_items
WHERE needs_web_scrape > 0 AND (status IS NULL OR status <> -1)
GROUP BY needs_web_scrape ORDER BY prio DESC
```

So the migration creates one partial composite index per queue, in exactly the
order the poll's `ORDER BY` names, with a predicate the query's `WHERE` contains
as a subexpression:

```sql
CREATE INDEX idx_web_scrape_queue ON workshop_items
  (needs_web_scrape DESC, api_fetched_at ASC) WHERE needs_web_scrape > 0;
CREATE INDEX idx_image_queue ON workshop_items
  (needs_image DESC, api_fetched_at ASC) WHERE needs_image > 0;
CREATE INDEX idx_api_queue ON workshop_items
  (api_priority DESC, api_fetched_at ASC) WHERE api_priority > 0;
```

`EXPLAIN QUERY PLAN` confirms all six consumers plan through the index —
`SEARCH workshop_items USING INDEX idx_... (...>?)` and, for the polls, no
`USE TEMP B-TREE FOR ORDER BY`. `TEMP B-TREE` is the proof that the `ORDER BY`
matches the index's column order rather than being sorted afterwards. A
`GROUP BY` over a queue still costs time proportional to the queued rows, so
the breakdowns improve rather than becoming free. This is pinned by
`tests/test_queue_indexes.py`.

*Measured on a copy of the production snapshot* (1,725,544 items; the plan's
"Queue indexes" section carries the full table):

| Query | Before | After | Index size |
|---|---|---|---|
| Next web scrape item (worker poll) | 258 ms | ~0 ms | 28.4 MB |
| Next image item (worker poll) | 252 ms | ~0 ms | 23.8 MB |
| Web queue breakdown | 470 ms | 70 ms | *(same index)* |
| Image queue breakdown | 401 ms | 59 ms | *(same index)* |
| API fetch queue | 219 ms | 0.4 ms | 0.19 MB |

Build time 2.3 s; total 52.4 MB, or 2.8% of the database. Index maintenance is
1.0 µs per completion update — 0.13% duty at the crawler's pace. The web queue's
`> 0` predicate covers about 89% of rows, so its partial index is nearly
full-size; that is the queue's shape, not a defect. The one-time build is
deliberately not benchmarked or optimised here.

The API queue's breakdown shape above has no shipped caller: the statistics
`priority_breakdowns` metric covers `translation_priority`, `needs_image` and
`needs_web_scrape` only. The API queue is read by the fetch poll and by
`count_fetchable_items` (the daemon's "is there work?" count), both of which the
new index serves; the 219 ms → 0.4 ms row is the queue's own `GROUP BY` as
measured for the plan.

Adding the index left `translation_priority` alone: it already had
`idx_translation_priority`, and the translation breakdown continues to use it.

---

### v25 → v26: The local `downloaded_at` latch

Adds one column behind the `downloaded` subscription marker:

| Column | Type | Meaning |
|---|---|---|
| `downloaded_at` | INTEGER DEFAULT NULL | When this app first saw Steam's downloaded copy of a subscribed item on disk. NULL means the subscription (if any) has not been confirmed on disk. |

Like `own_subscribed`, the column is added by `_safe_add_columns(cursor,
"workshop_items", [...])`: a fresh database gets it from `CREATE TABLE`, an
existing one from `ALTER TABLE`, and a pre-existing row is left NULL.

**It is local state with one writer and one clearer.** `src.workshop_folders`
stamps it when a periodic scan finds the item's folder
(`<library>/steamapps/workshop/content/<consumer_appid>/<workshop_id>/`) for an
item that is `own_subscribed = 1` and not yet confirmed; the scan never clears.
The only clearer is the subscription walk in `src.database.apply_own_subscriptions`,
in the same transaction that clears `own_subscribed` when the item leaves the
owner's subscription list. A missing folder, an unplugged drive or a moved
library therefore cannot take the green star away, and re-subscribing re-earns
the stamp on the next scan because the files are usually still on disk.

No data is written by the migration itself: no item has been observed downloaded
on the run that introduces the column, and inventing a stamp would claim an
observation never made — the same rule migration 20→21 followed for
`own_first_subscribed_at`.

The column is deliberately **excluded from the API merge allow-list**
(`daemon.MERGE_EXCLUDED_KEYS`), like the queue-owned columns: it is not a Steam
field, and keeping it out of the merge means a stray API key of that name can
never set the marker. No index is added; the scan's predicate is
`own_subscribed = 1 AND downloaded_at IS NULL` over the owner's subscriptions,
which is a small set, and the columns are read with the row by the grid and
detail payloads.

---

### v26 → v27: Per-queue completion clocks

Adds our completion time to the three work queues that had none:

| Column | Type | Meaning |
|---|---|---|
| `web_scraped_at` | INTEGER DEFAULT NULL | Our clock: when the web worker last scraped this item's page successfully. |
| `image_fetched_at` | INTEGER DEFAULT NULL | Our clock: when the image worker last fetched this item's preview successfully. |
| `translated_at` | INTEGER DEFAULT NULL | Our clock: when the translator finished the item's last queued field. One stamp per item, not per field. |

All three are added by `_safe_add_columns(cursor, "workshop_items", [...])`: a
fresh database gets them from `CREATE TABLE`, an existing one from `ALTER TABLE`,
and every pre-existing row is left NULL.

**They are our clock, not Steam's.** The web and translation stages already
stored `scrape_version` / `translate_version`, which are `steam_updated_at`
values — the revision the work was done at, not when we did it — and the image
stage recorded no time at all. Throughput, burn-down and ETA need the wall clock,
so these are named `*_at` under the convention in [timestamps.md](timestamps.md)
and are never written from a Steam field. They are therefore also **excluded from
the API merge allow-list** (`daemon.MERGE_EXCLUDED_KEYS`), so a stray API key
under one of those names can never set them.

**No data is written by the migration.** No stage recorded its completion time
before this version, so none can be reconstructed and every existing row keeps
NULL. The metrics that read the columns treat an all-NULL column as **no history
yet** rather than as zero throughput; see [timestamps.md](timestamps.md). No
backfill exists, by design.

**One partial index per column**, shaped for the only reader that is not a
worker — the per-queue throughput metric:

```sql
CREATE INDEX ... ON workshop_items(<column>) WHERE <column> IS NOT NULL
```

The metric asks for the rows inside the last-hour and last-day windows and for
the newest stamp. Each part is served by the index: the range counts read only
the window, and `MAX` reads the newest entry. The `IS NOT NULL` predicate keeps
the index empty on the run that creates it (every row is NULL then) and makes it
proportional to recorded completions afterwards, which a plain index over the
2.6M-row table would not be. Measured on a 2.6M-row copy with a third of the
column stamped, the full scan each statement would otherwise run costs 73–85 ms;
the partial index serves the whole three-part metric in 0.2 ms (`MAX` needs an
explicit `WHERE <column> IS NOT NULL` to use a partial index at all). The
one-time build is not benchmarked: the index is empty when it is built. The
metric's `EXPLAIN QUERY PLAN` is pinned by `tests/test_queue_completion_times.py`.

---

### v27 → v28: A creator's name returns to the translation queue

No schema change — the whole migration is data, and it is the user-side
counterpart of **v22 → v23** above and of the item backfill in migration 2→3.

A creator's name was queued by raising `users.translation_priority`: while
`get_next_translation_item` scanned `workshop_items` and `users` by that flag, the
mirror **was** the queue, so the daemon raising it was a complete producer. When
the per-field `translation_queue` replaced that scan, the producer was never
ported, and the flag has had no consumer since. *Measured in the 2026-09-18
backup*: `translation_queue` held 127,385 rows, **every one `item_type='item'`**,
and 5,540 creators held a translated name — the last written 45 minutes after the
commit that replaced the scan, and none since. The regression is recorded as
entries 45 and 46 in [code-issues.md](code-issues.md); `_build_user_record` now
queues through `queue_field_for_translation`, and the translator's completion pass
clears the user mirror.

Two statements, in this order:

```sql
INSERT INTO translation_queue (item_type, item_id, field, original_text, priority, queued_at)
SELECT 'user', steamid, 'personaname_en', personaname, translation_priority, :now
FROM users
WHERE translation_priority > 0
  AND personaname IS NOT NULL AND personaname <> ''
  AND NOT (length(CAST(personaname AS BLOB)) = length(personaname))
  AND NOT (COALESCE(personaname_en, '') <> '' AND (
             api_fetched_at IS NULL
             OR (translated_at IS NOT NULL AND translated_at >= api_fetched_at)))
  AND NOT EXISTS (
      SELECT 1 FROM translation_queue q
      WHERE q.item_type = 'user' AND q.item_id = users.steamid
        AND q.field = 'personaname_en')

UPDATE users SET translation_priority = 0
WHERE translation_priority > 0
  AND NOT EXISTS (
      SELECT 1 FROM translation_queue q
      WHERE q.item_type = 'user' AND q.item_id = users.steamid
  )
```

The first statement gives every flagged creator whose name genuinely needs
translating the queue row the producer owed it; the second clears the flags with
nothing left to translate, so the mirror is a mirror again. The predicates are
**inlined rather than imported**: a migration must keep meaning what it meant at
this version, and a helper it imported could change under it. The non-ASCII test
is the one `metrics._ascii_sql` documents — UTF-8 bytes equal characters exactly
when the text is ASCII — and the currency rule is the one
`metrics._creator_current_sql` applies to the Creator Translation bar, so the
migration, the coverage bar and the new producer agree on what "needs
translating" means. The mirror's own value carries into the queue row's
`priority`, as in migration 2→3.

*Measured in the 2026-09-18 backup*: of the 7,237 creators carrying the flag,
**7,233** have a non-ASCII name and no current translation, so the first statement
queues them; **4** have an ASCII name, which the second clears; and **0** creators
needing translation were unflagged, so the flag was a complete census of the
backlog and this migration need not look beyond it. No creator had a current
translation *and* a raised flag, so the currency term changes no count today — it
is there so the statement means "needs translating" rather than "is flagged".
Both statements are idempotent, which `tests/test_user_translation.py` pins.

---

### v28 → v29: Drop the never-written page counter

A schema change with nothing to backfill. `app_tracking.last_page_scanned` counted
pages while discovery walked them by number; `88397b7` replaced page-numbered
discovery with cursor-based discovery, which resumes from `last_cursor`, and
deleted `update_app_tracking_page` and the `page = last_page + 1` resume logic
with it — but left every reader. So the TUI's "Last Page" column, the web table's
equivalent, and the `app_discovery` metric that feeds both had, ever since, read a
column nothing writes and displayed its `DEFAULT 0`. Recorded as issue 47 and
removed here. The stored value is not preserved because it stopped meaning
anything the moment discovery moved to cursors.

```sql
ALTER TABLE app_tracking DROP COLUMN last_page_scanned
```

No index names the column, so the column alone goes. A `PRAGMA table_info` guard
keeps the step idempotent and resumable — a fresh database never has the column,
and a re-run neither finds it nor fails reaching for it — which is the shape
migration 23→24 established for `language`. All three readers are removed in the
same change, in both front ends, so parity holds by both losing the same column.
`tests/test_last_page_column_migration.py` pins the drop, the surviving row, the
fresh-database case and the idempotent re-run.

---

### v29 → v30: `users` → `creators`, `app_tracking` → `app_discovery`

The `users` table holds Steam creators — there are no application users anywhere in the
project — so it becomes `creators`; its `steamid` primary key already says whose id it is.
`app_tracking`'s live columns are the discovery cursor and the enrichment filters, not
"tracking", so it becomes `app_discovery`. Both are pure table renames: the columns and the
`item_type = 'user'` queue value deliberately keep their names here (migration 33→34 later
renames the column to `entity_type`; the `'user'` value stays). The `creator`
foreign-key column is renamed to `creator_steamid` by migration 31→32 instead.

```sql
ALTER TABLE users RENAME TO creators;
ALTER TABLE app_tracking RENAME TO app_discovery;
```

Each statement is guarded on "the old table exists and the new one does not", so the step is
idempotent and a re-run is a no-op — which also covers the crash window where SQLite has
committed the renaming DDL but not the version bump. Neither table carries an index or a
trigger, so the rename needs nothing recreated (measured with `PRAGMA index_list` against the
real v22 and v29 schemas).

The unusual part is `_create_legacy_schema`, which runs on **every** startup before the versioned
migrations and therefore sees both sides of this step. It must keep building a *fresh*
database with the historical names, because the chain it is about to replay names them at
6→7, 13→14, 21→22 and 27→28; it must not run `CREATE TABLE IF NOT EXISTS users` once the table has
become `creators`, or every startup would grow an empty `users`; and `_safe_add_columns`
re-raises anything that is not a duplicate-column error, so an `ALTER TABLE app_tracking`
against a renamed database would break startup. `_create_legacy_schema` therefore resolves each name
with `_current_table_name(cursor, new, old)` and routes the `CREATE TABLE`, the
`_safe_add_columns` call, the populate step and the legacy-filter conversion through the
resolved name. `_demote_filtered_out_queue_priorities`, which migration 21→22 calls and a test
also calls against a current database, resolves the name the same way. Every migration before
this one keeps its historical SQL byte-identical, so the chain still means what it meant at
its version. `tests/test_table_rename_migration.py` pins the fresh path, the v29 upgrade, the
re-initialisation (including the resurrection trap of initialising twice) and the
already-renamed-under-the-old-marker case.

---

### v30 → v31: `status` → `fetch_status`

`status` in `workshop_items` is unqualified: it competes with the HTTP
status code, the subscribe outcome, the daemon-controller status and the `status_counts`
metric, and a bare `status` does not say which one a reader means. The column holds this
app's synthetic fetch outcome — `200` fetched, `206` partial, `-1` dead, `500` retry,
`NULL` discovered but never fetched — not an HTTP response code, so it becomes
`fetch_status`. The stored **values are unchanged**; only the name moves. The metric key
`status_counts` and the unrelated `SubscribeOutcome.status` / `DaemonController.status`
names deliberately keep theirs.

```sql
ALTER TABLE workshop_items RENAME COLUMN status TO fetch_status;

DROP INDEX IF EXISTS idx_status;
CREATE INDEX IF NOT EXISTS idx_fetch_status ON workshop_items (fetch_status);

DROP INDEX IF EXISTS idx_appid_status;
CREATE INDEX IF NOT EXISTS idx_appid_fetch_status ON workshop_items (consumer_appid, fetch_status);

DROP INDEX IF EXISTS idx_status_scraped_version;
CREATE INDEX IF NOT EXISTS idx_fetch_status_scraped_version ON workshop_items (fetch_status, scrape_version);
```

SQLite rewrites an index *definition* on `RENAME COLUMN` but keeps the index *name*, so the
three indexes whose names embed the old column would otherwise be left named for a column
that no longer exists. Their names are recreated to match what they index.
`idx_web_scrape_queue` and `idx_image_queue` are named for the queue rather than the column,
so their names stay; SQLite rewrites their definitions in place. `_ensure_indexes` creates
the three under their new names too, because it runs on every startup after the migration
chain and would otherwise fail with "no such column: status".

The step is guarded on the column that is present, so a re-run is harmless: a crash between
the DDL commit and the version bump leaves the column renamed under the old marker, and the
step must then be a no-op rather than raise `no such column: status`. The index
drop/create pairs are idempotent for the same reason. Every migration before this one keeps
its historical SQL byte-identical, including those that name `status`, so the chain still
means what it meant at its version.

`_create_current_schema` declares the column as `fetch_status`, so a fresh database is
created at the new endpoint and `tests/test_fresh_schema_path.py::test_schema_equivalence`
stays green. `tests/test_status_column_migration.py` pins the fresh path, the v30 upgrade,
the re-initialisation and the already-renamed-under-the-old-marker case.

---

### v31 → v32: `creator` → `creator_steamid`

`creator` in `workshop_items` holds the author's SteamID64 and joins `creators.steamid`, but
the bare name reads as a display name or an object rather than the id it is — the
neighbouring `creator_appid` is a different column and `creators` is a different table — so
it becomes `creator_steamid`. The stored **values are unchanged**; only the name moves. The
unrelated `creator_appid` column, the `creators` table and the entity vocabulary
(`CREATOR_COLUMNS`, `get_creator`, `insert_or_update_creator`, `creator_id`, and so on)
deliberately keep their names, as do the metric keys and wire keys.

```sql
ALTER TABLE workshop_items RENAME COLUMN creator TO creator_steamid;

DROP INDEX IF EXISTS idx_creator;
CREATE INDEX IF NOT EXISTS idx_creator_steamid ON workshop_items (creator_steamid);

DROP INDEX IF EXISTS idx_creator_api_fetched_at;
CREATE INDEX IF NOT EXISTS idx_creator_steamid_api_fetched_at ON workshop_items (creator_steamid, api_fetched_at);
```

SQLite rewrites an index *definition* on `RENAME COLUMN` but keeps the index *name*, so the
two indexes whose names embed the old column would otherwise be left named for a column that
no longer exists. No other index on `workshop_items` embeds `creator` in its name.
`_ensure_indexes` creates the two under their new names too, because it runs on every startup
after the migration chain and would otherwise fail with "no such column: creator".

The step is guarded on the column that is present, so a re-run is harmless: a crash between
the DDL commit and the version bump leaves the column renamed under the old marker, and the
step must then be a no-op rather than raise `no such column: creator`. The index drop/create
pairs are idempotent for the same reason. Every migration before this one keeps its
historical SQL byte-identical, including migrations 6→7 and 13→14, which build indexes on
the historical `creator` column.

The Steam API still sends the author under the field name `creator`, so
`_merge_and_clean_api_data` remaps that wire key onto the `creator_steamid` column exactly as
it remaps `creator_app_id` and `time_created`.

`_create_current_schema` declares the column as `creator_steamid` and `_ensure_indexes`
creates the two renamed indexes, so a fresh database is created at the new endpoint and
`tests/test_fresh_schema_path.py::test_schema_equivalence` stays green.
`tests/test_creator_column_migration.py` pins the fresh path, the v31 upgrade (row counts,
NULL and distinct counts and a checksum over the column), the index names, the
re-initialisation and the already-renamed-under-the-old-marker case.

---

### v32 → v33: the four priority, image-answer and download-latch columns

Four `workshop_items` names no longer said what the columns hold. The stored **values are
unchanged**; only the names move:

* `needs_web_scrape` → **`web_scrape_priority`** and `needs_image` → **`image_priority`**.
  The columns carry a 1-10 priority set with `MAX`, not a boolean, and the queue predicates
  and the docs already call them priorities; `needs_` is a documented historical exception.
* `image_extension` → **`image_answer`**. The column carries the server's *answer* — a real
  file extension, a wholly numeric HTTP status, or a served non-image token — and
  `images.image_state()` is already the classifier with `image_state` as the derived payload
  key, so the column is the answer that classifier reads. The classifier's own helper
  `images.is_image_extension()` and the uppercase `IMAGE_EXTENSIONS` frozenset keep their
  names: they describe one *kind* of answer, not the column.
* `downloaded_at` → **`steam_download_seen_at`**. It is a one-way latch stamped when the
  folder scan first sees Steam's downloaded copy on disk, not a per-stage completion clock.

```sql
ALTER TABLE workshop_items RENAME COLUMN needs_web_scrape TO web_scrape_priority;
ALTER TABLE workshop_items RENAME COLUMN needs_image TO image_priority;
ALTER TABLE workshop_items RENAME COLUMN image_extension TO image_answer;
ALTER TABLE workshop_items RENAME COLUMN downloaded_at TO steam_download_seen_at;
```

SQLite rewrites an index *definition* on `RENAME COLUMN` but keeps the index *name*. No index
on `workshop_items` embeds any of these four in its **name**: `idx_web_scrape_queue` and
`idx_image_queue` are named for their queue, so their names stay and their definitions follow
the renamed columns in place. There is **no index to drop or recreate** here, and
`_ensure_indexes` names none of the four columns.

The step is guarded on the column that is present, so a re-run is harmless: a crash between
the DDL commit and the version bump leaves the columns renamed under the old marker, and the
step must then be a no-op rather than raise `no such column`. Every migration before this one
keeps its historical SQL byte-identical, and `_create_legacy_schema` still declares the
historical names inside its `_safe_add_columns` list, because the chain it replays names them
at earlier versions.

`_create_current_schema` declares the four new columns and the two queue indexes on them, so
a fresh database is created at the new endpoint and
`tests/test_fresh_schema_path.py::test_schema_equivalence` stays green. Because
`_create_legacy_schema` runs on every startup, its historical `_safe_add_columns` entries for
these four are skipped once their renamed form is present — otherwise re-initialising a v33
database would resurrect the old column beside the new one. That guard lives in
`_safe_add_columns`, keyed by `_RENAMED_COLUMN_NAMES`, so `_create_legacy_schema` itself stays
byte-identical.

None of the four is a Steam API field: all four are in `daemon.MERGE_EXCLUDED_KEYS`, so the
API merge never carries a value into them and `_merge_and_clean_api_data` needs no wire-key
remap for this step.

`tests/test_queue_priority_columns_migration.py` pins the fresh path, the v32 upgrade (row
counts, per-column NULL and distinct counts and a checksum over every one of the four), the
in-place index rewrite, the re-initialisation and the already-renamed-under-the-old-marker
case.

### v33 → v34: the `translation_queue` discriminator columns

`translation_queue` holds one row per text field awaiting translation, and its first two
columns are the row's identity: a discriminator and the id it belongs to. `item_type` and
`item_id` read as if every row described a workshop item — but a creator's row is
`item_type = 'user'`, and the bare `item_id` collides with the workshop-id vocabulary the
workers use everywhere else — so they become **`entity_type`** and **`entity_id`**. The
stored **values are unchanged**, including the discriminators `'item'` and `'user'`; only
the names move.

```sql
ALTER TABLE translation_queue RENAME COLUMN item_type TO entity_type;
ALTER TABLE translation_queue RENAME COLUMN item_id TO entity_id;
```

SQLite rewrites an index *definition* on `RENAME COLUMN` but keeps the index *name*.
`idx_translation_queue_lookup` is named for the queue rather than a column, so its name
stays and its definition follows both columns in place: there is **no index to drop or
recreate**.

The step is guarded on the column that is present, so a re-run is harmless: a crash between
the DDL commit and the version bump leaves the columns renamed under the old marker, and the
step must then be a no-op rather than raise `no such column`. Every migration before this one
keeps its historical SQL byte-identical — that includes 22→23's repair and 27→28's creator
repair, which both name `item_type`/`item_id`.

`_create_current_schema` declares the two new columns and its copy of the lookup index on
them, so a fresh database is created at the new endpoint and
`tests/test_fresh_schema_path.py::test_schema_equivalence` stays green. The other half of the
forward rule is subtler here: `_create_legacy_schema` builds the lookup index on **every**
startup, before the migration loop, so on a database already at v34 the table carries
`entity_type`/`entity_id`. It therefore resolves each column with `_current_column_name`
(new name if present, else the historical one) rather than hardcoding either pair — naming
`item_type` unconditionally would raise `no such column` on the next start, and naming
`entity_type` would fail on a fresh chain database the same way. Issue 57 is why the index
stays in the legacy builder at all: 22→23's repair runs before `_ensure_indexes`.

`tests/test_translation_queue_entity_columns_migration.py` pins the fresh path, the v33
upgrade (row counts and both columns' NULL and distinct counts and a checksum over every
value), the in-place index rewrite, the re-initialisation, the
already-renamed-under-the-old-marker case, and both sides of the legacy builder — including
a `legacy_chain=True` fresh database that starts with the historical names and still reaches
v34.

### v34 → v35: the three write-only columns

Three columns were written and never read (issue 30), so they are dropped:

* `workshop_items.scrape_version` — written by the web worker, and (until issue 7)
  overwritten by the image worker; no runtime code ever compared it, and only migration
  13→14's cleanup read it;
* `app_discovery.last_historical_date_scanned` and `app_discovery.window_size` — written
  only by `update_app_tracking`, a function nothing but a test called, which this step
  also removes.

A column nothing maintains on purpose is worse than an absent one: it invites a reader to
trust it, which is exactly how `scrape_version` came to be overwritten by the image worker
(issue 7). The stored **data is discarded**, not moved — it had no reader.

```sql
DROP INDEX IF EXISTS idx_scraped_version;
DROP INDEX IF EXISTS idx_fetch_status_scraped_version;

ALTER TABLE workshop_items DROP COLUMN scrape_version;
ALTER TABLE app_discovery DROP COLUMN last_historical_date_scanned;
ALTER TABLE app_discovery DROP COLUMN window_size;
```

**The two indexes go first.** Both are defined on `scrape_version` at v34, and SQLite
refuses to drop an indexed column, so `DROP INDEX` is a precondition, not tidying. They
are not recreated: the column they indexed is gone, and `_ensure_indexes` no longer names
them. Migrations 13→14 and 30→31 keep their historical creations of
`idx_scraped_version` / `idx_status_scraped_version` / `idx_fetch_status_scraped_version`
byte-identical — the chain decides who *starts* with them; this step decides who ends with
them.

Each drop is guarded on the column that is present, so a re-run is harmless: a crash
between the DDL commit and the version bump leaves the columns dropped under the old
marker, and this step must then be a no-op rather than raise `no such column`.

`_create_current_schema` loses the three column declarations, so a fresh database is
created at the new endpoint and `tests/test_fresh_schema_path.py::test_schema_equivalence`
stays green. `_create_legacy_schema` **keeps** its historical declarations — a
`legacy_chain` database starts at `user_version = 0` and the chain names them at earlier
versions — which is exactly why the drop needs guards on that side:

* `_safe_add_columns` runs on every startup, so `window_size` would be re-added to a v35
  database on the next start. `_DROPPED_COLUMN_NAMES` is the mirror of
  `_RENAMED_COLUMN_NAMES`: it maps a dropped name to the version that dropped it, and
  `_safe_add_columns` skips it once the database is at or past that version.
  `scrape_version` and `last_historical_date_scanned` are in no safe-add list, so
  `window_size` is the only entry there.
* the populate step (`INSERT INTO app_discovery (appid, last_historical_date_scanned) …`)
  names the dropped column, so it now builds the column list from the table's actual
  columns: a v35 database seeds the AppID rows alone, while a database rewound below 35
  keeps the historical write. Naming the gone column raises `no such column` on any v35
  database whose discovery table is empty — a fresh current-schema database takes that
  branch on its second start.

`tests/test_drop_write_only_columns_migration.py` pins the fresh path, the v34 upgrade
(row counts across every app table and the two column sets), the two index removals, the
re-initialisation, the `window_size` safe-add guard, the empty-discovery populate step,
the already-dropped-under-the-old-marker case, and a `legacy_chain=True` fresh database
that still reaches `EXPECTED_VERSION`.

### v35 → v36: dead items give up their translation queue rows

The defect is issue 66. `_settle_api_failure` marked an item dead
(`fetch_status = -1`) and cleared the four item-level queue flags, but it did not delete
the item's rows from `translation_queue`. The translation poll selects **every** row of
that table (`translation_queue_predicate()` is `1`) with no dead-item guard, so a dead
item's fields were still handed out and paid for. Nothing detected it either:
`queued_anywhere_predicate` was built from `translation_priority_predicate`, the
item-level mirror, so `dead_queued` counted only the flag half of the handoff.

*Measured* on the v35 snapshot (1,725,544 items, 68,323 dead): `dead_queued` read **0**
while **910 dead items held 1,016 `translation_queue` rows**, every one of them with the
mirror already cleared. The v22 backup shows 914 items and 1,022 rows. Left alone, the
translator pays to translate dead items' fields, and the detector meant to notice cannot.

The step is **data-only** — no table, column or index changes:

```sql
DELETE FROM translation_queue
WHERE entity_type = 'item'
  AND entity_id IN (SELECT workshop_id FROM workshop_items WHERE fetch_status = -1);
```

Only `entity_type = 'item'` rows keyed to a dead `workshop_id`. A creator row
(`entity_type = 'user'`) is a different entity whose numeric id may collide with a dead
item's id; it is not the dead item's work. `_create_current_schema` needs no mirror,
because nothing about the shape changes, but `EXPECTED_VERSION` moves to 36 and the
current-schema docstring and heading move with it — `test_schema_equivalence` compares the
version marker both paths leave, so a data-only step that left the marker at 35 would fail
it on the next schema change. `tests/test_dead_translation_rows_migration.py` pins the
dead-row deletion, the live and creator rows it leaves, the logged count, the rewind
idempotency, and the version.

The ongoing half of the fix is not in the migration. Every producer that marks an item
dead deletes the item's rows *in the same transaction as the status write*
(`insert_or_update_item(..., clear_translation_queue=True)`), so the clear cannot race a
translator drain; and `queued_anywhere_predicate` now asks the consumer's real question —
a flag **or** a `translation_queue` row — which is what makes `dead_queued` see a
row-without-a-flag and keeps `queued_nowhere` from calling an item with outstanding
translation work stranded. `dead_items_by_queue` makes the same translation test (mirror
**or** row) in its `translation` column, so the scalar and the per-queue diagnostic that
exists to say *which* queue holds the item cannot disagree. There is deliberately no
poll-side dead guard: the producer's clear is the mechanism the stage-handoff plan names,
so the translator's select is unchanged. Pinned by
`tests/test_daemon.py::test_process_item_404_deletes_the_items_translation_queue_rows`,
two tests in `tests/test_handoff_contract.py`, and three in `tests/test_metrics.py`.

### v36 → v37: an AppID's cursor walk can be finished

The defect is issue 68. `seed_database`'s cursor walk stopped only on `fill_target` new
items or an empty `next_cursor`. Once the pages it walked were all already known, neither
was reachable: a pass paged an exhausted catalogue at ~2.3 pages a second until the API
refused, and the next pass resumed from the saved cursor and repeated. *Measured live* on
2026-09-21 (AppID 431960): passes of 13,487 / 1,724 / 5,624 / 6,964 pages, each adding **0**
new items, every one ended by an API refusal; `Cursor exhausted` never appears in 400,000
log lines, so the page-based fall-back it enables never fired.

The step adds the latch that keeps a finished walk from being resumed:

```sql
ALTER TABLE app_discovery ADD COLUMN cursor_walk_finished INTEGER NOT NULL DEFAULT 0;
```

`0` for every existing row: under the new rule no AppID's walk has finished yet, and
defaulting them to finished would disable cursor discovery outright. The walk now stops after
five consecutive pages that add nothing (`CURSOR_STALL_PAGES`); a page that adds anything
resets the count; and only that stall — not `fill_target` and not an API error — sets the
flag through `mark_cursor_walk_finished`. `last_cursor` is deliberately untouched: it records
how far the walk reached, while the flag decides whether it may resume. `seed_database` skips
a finished AppID's cursor scan, and `_page_discovery_eligible` returns true once any target
AppID's flag is set (it is also still true on an empty cursor, on the `.fetch_new` trigger,
or after 500 items have been scraped), so page mode carries new and changed items from then
on.

The column is declared by both schema builders — `_create_legacy_schema`'s `CREATE TABLE`
and `_safe_add_columns`, and `_create_current_schema` — so on the ordinary upgrade the
builder adds it and this step only records the version. The guarded `ALTER` is what makes a
database that reaches the step without the column gain it; it resolves the table name with
`_current_table_name` because the migration tests rewind the version marker over a database
that still carries the historical `app_tracking` name, and naming only `app_discovery` would
raise `no such table` there. `EXPECTED_VERSION` moves to 37 and the current-schema heading
and docstring move with it.

`tests/test_cursor_walk_stall.py` pins the five-page stop, the reset by a page that adds
items, the two exits that must not mark the walk finished, the skip after a restart,
page-mode eligibility surviving the restart, the kept cursor, the migration and its `0`
default, and the fresh path; `tests/test_fresh_schema_path.py::test_schema_equivalence` pins
that both paths leave the same shape.

---

## Database Utility Functions

### `get_connection` (database)

Opens a new SQLite connection with `row_factory = sqlite3.Row` for dict-like row access and a 15 s busy timeout. It issues no `PRAGMA`: the journal mode is a persistent property of the file, set once by `initialize_database`, and running `PRAGMA journal_mode` on every connection was refused outright while another process held a lock — a statement the busy timeout does not cover (issue 43). Foreign-key enforcement is **not** enabled (`PRAGMA foreign_keys` is left at its default), which matches the schema: no foreign-key constraints are declared. Each caller is responsible for closing the connection.

### `initialize_database` (database)

The driver described under [Migration system](#migration-system-initialize_database): reads the recorded version, refuses it with `SchemaVersionError` if it is higher than `EXPECTED_VERSION`, then sets WAL mode and builds a fresh file at `EXPECTED_VERSION` with `_create_current_schema` or runs `_create_legacy_schema` plus the pending entries of `MIGRATIONS` in ascending order, calls `_ensure_indexes`, and commits. The keyword-only `legacy_chain` (default `False`) selects the chain for a fresh file only; every existing `initialize_database(db_path)` call site keeps working untouched. It is called on every startup — by the daemon directly, and by the TUI and the standalone web runner through `initialize_database_with_daemon_stopped` (below), which stops a running daemon first when a migration is pending — before anything reads or writes, so the one call covers every later connection. Idempotent and safe to call on an existing database: on a database already in WAL the statement is a no-op.

### `read_schema_version` (database)

Returns the database's recorded `PRAGMA user_version` over a **read-only** connection (`mode=ro`), so a caller may read it while the daemon is writing: a WAL reader takes no write lock and never switches the journal mode. A path that does not exist yet reads as `0`, which is correctly "a migration is pending" because `initialize_database` would build a schema for it. The read goes through SQLite rather than the file header on purpose: the header is only updated on a checkpoint, so in WAL mode a version applied moments ago can still be sitting in the `-wal` file. Used by the UI startup gate to decide whether a running daemon has to be stopped before `initialize_database`.

### `initialize_database_with_daemon_stopped` (daemon_control)

The UI's startup gate, called by `ScraperApp.__init__` and `web_runner.main()` with the controller the process shares with its daemon panel. It calls `read_schema_version` and refuses a newer database with `newer_schema_error` **before** touching the daemon, so a refused start does not take the service down on its way out. When nothing is pending it calls `initialize_database` and leaves the daemon alone. When a migration is pending it stops a running daemon through `DaemonController.stop()` (logging the migration as the reason), and if that stop did not succeed it raises `DaemonStillRunningError` without migrating. It migrates, restarts the daemon when it had been running, and if the migration raised it does not restart and raises `SchemaMigrationFailedError`, whose message says the daemon was stopped and has not been restarted. Pinned by `tests/test_daemon_stop_before_migration.py`, which asserts the exact order stop → migrate → start.

### `SchemaVersionError` (database)

Raised by `initialize_database` when the database's recorded `user_version` is **higher** than `EXPECTED_VERSION`: the build is older than the file, so it cannot know what the newer schema means. The read happens before the journal-mode statement and before any `CREATE`/`ALTER`/`_ensure_indexes`/version write, and the connection is closed before the raise, so a refused start leaves the file byte-for-byte unchanged — `tests/test_schema_version_refusal.py` pins that with a SHA-256, the full `sqlite_master` and every table's columns, and the WAL sidecars.

The exception exists rather than a bare `ValueError` because the three entry points must all report it rather than print a traceback: `daemon_runner.main()` and `web_runner.main()` catch it, log the sentence at error level and exit 2 (the same handoff as `ConfigError`), and `ScraperApp.__init__` prints it to stderr and raises `SystemExit(2)` before the screen mounts. Its message (built by `newer_schema_error`) names the file, the recorded version, the version this build expects, and the remedy: replace the build with one that expects at least the recorded version. It is explicit that the database is not corrupt — so it must not be deleted, rewritten or treated as damage — and that rolling back to an older build is not a way out once the schema renames have run, because that build would keep using tables the database no longer has.

### `_create_current_schema`, `_create_legacy_schema`, `_ensure_indexes`, `MIGRATIONS` (database)

`_create_current_schema(cursor, conn)` creates a brand-new database directly at `EXPECTED_VERSION`. Every table, index and trigger definition it holds was dumped from `sqlite_master` of a database the migration chain itself produced at v35 — not written from reading the migrations — so the index SQL it creates is the exact text SQLite stores. v36 was data-only, so the dump stayed the terminal shape for that step; v37 adds `app_discovery.cursor_walk_finished`, which is now declared in the `app_discovery` `CREATE TABLE` here as the migration leaves it. It deliberately does **not** repeat the query indexes `_ensure_indexes` owns, because that runs after it on both paths; those are the ones with historical names such as `idx_time_created`, whose definitions a `RENAME COLUMN` rewrote. It does create the indexes a *migration* owns, because no migration runs on this path. It does not carry the three columns 34→35 dropped.

`_create_legacy_schema(cursor, conn)` creates the tables (`IF NOT EXISTS`) and the baseline columns in their historical form, and runs the legacy data conversions every database history shares. It is the unversioned part of the schema, run before the versioned steps. Because it runs on every startup for an existing database, it also runs on both sides of migration 29→30: it resolves the creator and discovery table names once with `_current_table_name` (new name if it exists, else the historical one, else the historical one for a brand-new file) and routes its `CREATE TABLE`, `_safe_add_columns`, populate step and legacy-filter conversion through the resolved name.

`_ensure_indexes(cursor)` creates the query indexes. It is separate from the schema builders because several index columns (`api_fetched_at`) only exist after migration 13→14's renames, so it must run last; every statement is `IF NOT EXISTS`. One queue index is the exception and lives in both schema builders instead: `idx_translation_queue_lookup` on `translation_queue (entity_type, entity_id, field)`, because migration 22→23's repair runs *inside* the `MIGRATIONS` loop and an index created here would be too late to serve it. Its columns have existed since the table was created, so it is safe at every version; after migration 33→34 renamed them, `_create_legacy_schema` resolves the current spelling with `_current_column_name` rather than hardcoding either pair. The two `scrape_version` indexes it used to create were removed in 34→35 with the column.

`MIGRATIONS` is the ordered `[(target version, function), ...]` table the driver walks. The functions are `_migration_<from>_to_<to>(cursor, conn, db_path)` and sit above the table in ascending order. See [Migration system](#migration-system-initialize_database) for the shape and for how to add the next one.

### `_safe_add_columns` (database)

Adds columns to an existing table, catching `OperationalError` for duplicates. It is the unversioned builder's half of the forward rule, since it runs on every startup: a historical name whose renamed current form is already present is skipped (`_RENAMED_COLUMN_NAMES`), and a name a migration has dropped is skipped once the database is at or past the dropping version (`_DROPPED_COLUMN_NAMES`). Without the second guard, re-initialising a database at or past v35 would add `window_size` back beside the columns the migration left. `scrape_version` and `last_historical_date_scanned` are in no safe-add list, so `window_size` is the only dropped-name entry.

### `insert_or_update_item` (database)

Upserts an item row using `INSERT ... ON CONFLICT(workshop_id) DO UPDATE SET`. Filters keys against `WORKSHOP_ITEM_COLUMNS` frozenset before building the SQL. Handles tags via junction table (parses JSON, calls `_ensure_tag_ids`, updates `workshop_tags`). Tags are excluded from the INSERT column list since they're no longer a workshop_items column. The keyword-only `clear_translation_queue` (default `False`) also deletes the item's `translation_queue` rows on the same connection before the single commit; the producer that marks an item dead passes it so the status write and the queue clear are one transaction (v36).

### `insert_or_update_creator` (database)

Same upsert pattern for the `creators` table, using `CREATOR_COLUMNS` frozenset for filtering.

### `get_item_details` (database)

Returns all columns for a single workshop_id, joined with the creators table. Tags are returned as comma-separated via a correlated `GROUP_CONCAT` subquery against the junction table.

### `count_never_fetched_items` (database)

Counts items where `api_fetched_at IS NULL` (never successfully fetched) — used to determine if the processing queue needs more items.

### `toggle_subscription_queue` / `get_subscription_queue_items` (database)

Simple toggle and retrieval for the subscription queue feature.
