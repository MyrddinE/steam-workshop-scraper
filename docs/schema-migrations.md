# Schema & Migrations

The database uses SQLite with WAL mode. Schema evolution follows a `PRAGMA user_version` increment pattern. Each migration is its own function in `src/database.py`, named `_migration_<from>_to_<to>`, and the ordered `MIGRATIONS` table maps each target version to its function. `initialize_database` is a short driver: it creates the schema, reads `PRAGMA user_version`, and runs every pending entry in ascending order on startup. Fresh databases run all migrations; existing databases run only pending ones.

---

## Current Schema (v29)

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
| web_scraped_at, image_fetched_at, translated_at | INTEGER | Our completion clocks for the web scrape, image download and translation stages (v27). NULL on every row that predates v27 and on any stage that has not succeeded since |
| subscriptions, lifetime_subscriptions | INTEGER | Current and lifetime subscriber counts |
| favorited, lifetime_favorited, views | INTEGER | Engagement metrics |
| visibility, banned, ban_reason, app_name, file_type | Various | Steam metadata |
| status | INTEGER | 200 = fetched, -1 = dead, 500 = retry, NULL = discovered but never fetched |
| api_priority | INTEGER | Steam API fetch queue priority |
| translation_priority | INTEGER | Translation-queue mirror (0 = no queued fields) |
| wilson_favorite_score, wilson_subscription_score | REAL | Wilson lower-bound scores (0-1), NULL default |
| needs_web_scrape | INTEGER | Priority for web scraping (10=detail, 5=list, 3=new, 1=backlog, 0=done) |
| needs_image | INTEGER | Priority for image download (same scale as needs_web_scrape) |
| image_extension | TEXT | File extension of downloaded image (e.g., "jpg"), NULL if not downloaded |
| is_queued_for_subscription | INTEGER | Subscription queue flag, set by the TUI and web UI and cleared when the userscript reports an outcome. Transient: reads 0 when nothing is queued |
| own_subscribed | INTEGER | Whether the owner (the account whose key and cookies are configured) is subscribed to this item right now. Reconciled from Steam; not the item-wide `subscriptions` count |
| own_first_subscribed_at | INTEGER | When we first *saw* the owner subscribed; sticky, and the only source of the `previously` state |
| downloaded_at | INTEGER | Local latch: when this app first saw Steam's downloaded copy of a subscribed item on disk (v26). Set only by `src/workshop_folders`, cleared only beside `own_subscribed` when the item leaves the subscription list. NULL means not confirmed on disk |

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
| idx_file_size | file_size | File Size sort |
| idx_subscriptions | subscriptions | "Subs" sort |
| idx_favorited | favorited | "Favs" sort |
| idx_views | views | "Views" sort |
| idx_wilson_subscription_score | wilson_subscription_score | "Subscriber Score" sort |
| idx_wilson_favorite_score | wilson_favorite_score | "Favorite Score" sort |
| idx_translation_priority | translation_priority | Translation queue scanning |
| idx_web_scrape_queue | (needs_web_scrape DESC, api_fetched_at ASC) WHERE needs_web_scrape > 0 | Web scrape worker poll and web queue breakdown (v25) |
| idx_image_queue | (needs_image DESC, api_fetched_at ASC) WHERE needs_image > 0 | Image worker poll and image queue breakdown (v25) |
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

1. opens the connection and sets `PRAGMA journal_mode=WAL`;
2. calls `_create_schema(cursor, conn)`, which creates the tables and the
   unversioned baseline columns every database history shares;
3. reads `PRAGMA user_version` and runs every entry in the module-level
   `MIGRATIONS` table whose target version is above it, in ascending order;
4. calls `_ensure_indexes(cursor)`, then commits and closes.

`MIGRATIONS` is an ordered list of `(target version, function)` pairs, from
`(1, _migration_0_to_1)` to `(29, _migration_28_to_29)`. The functions are
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

**Adding the next migration (target v30):**

1. bump `EXPECTED_VERSION` in `src/database.py` to `30`;
2. append `def _migration_29_to_30(cursor, conn, db_path): ...` immediately
   after `_migration_28_to_29`, keeping the body self-contained and preserving
   what the step meant at v30 (no tidying an older step, no changing a
   `PRAGMA user_version = N` target);
3. append `(30, _migration_29_to_30)` as the last entry of `MIGRATIONS`;
4. add a `### v29 → v30: ...` entry below, in the same shape as the others;
5. if the step adds a column or table that a fresh database must also start
   with, add it to `_create_schema` too — a fresh database begins at
   `user_version = 0` and runs the whole table, so the two paths must agree on
   the terminal schema.

`tests/test_migration_table.py` fails if the table and `EXPECTED_VERSION`
disagree, so a step cannot be half-added (function without entry, or entry
without function) silently.

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
queue flag as `stuck_work`. Leaving ten thousand of them there would peg a
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
(`enrichment_filters_for`), it walks the items above backlog priority and demotes
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
version `flag_field_for_translation` wrote the queue row and the mirror on **two
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
(`FIELD_NAME_MAP`). A saved filter that named it now falls through as an unknown
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
queues through `flag_field_for_translation`, and the translator's completion pass
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
equivalent, and the `app_tracking` metric that feeds both had, ever since, read a
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

## Database Utility Functions

### `get_connection` (database)

Opens a new SQLite connection with `row_factory = sqlite3.Row` for dict-like row access and a 15 s busy timeout. It issues no `PRAGMA`: the journal mode is a persistent property of the file, set once by `initialize_database`, and running `PRAGMA journal_mode` on every connection was refused outright while another process held a lock — a statement the busy timeout does not cover (issue 43). Foreign-key enforcement is **not** enabled (`PRAGMA foreign_keys` is left at its default), which matches the schema: no foreign-key constraints are declared. Each caller is responsible for closing the connection.

### `initialize_database` (database)

The driver described under [Migration system](#migration-system-initialize_database): sets WAL mode, calls `_create_schema`, runs the pending entries of `MIGRATIONS` in ascending order, calls `_ensure_indexes`, and commits. This is called on every startup by the daemon, TUI, and web runner — before anything reads or writes — so the one call covers every later connection. Idempotent and safe to call on an existing database: on a database already in WAL the statement is a no-op.

### `_create_schema`, `_ensure_indexes`, `MIGRATIONS` (database)

`_create_schema(cursor, conn)` creates the tables (`IF NOT EXISTS`) and the baseline columns, and runs the legacy data conversions every database history shares. It is the unversioned part of the schema, run before the versioned steps.

`_ensure_indexes(cursor)` creates the query indexes. It is separate from `_create_schema` because several index columns (`api_fetched_at`, `scrape_version`) only exist after migration 13→14's renames, so it must run last; every statement is `IF NOT EXISTS`.

`MIGRATIONS` is the ordered `[(target version, function), ...]` table the driver walks. The functions are `_migration_<from>_to_<to>(cursor, conn, db_path)` and sit above the table in ascending order. See [Migration system](#migration-system-initialize_database) for the shape and for how to add the next one.

### `_safe_add_columns` (database)

Adds columns to an existing table, catching `OperationalError` for duplicates. Used by migrations 0→1 and others to add columns that may already exist from a previous partial run.

### `insert_or_update_item` (database)

Upserts an item row using `INSERT ... ON CONFLICT(workshop_id) DO UPDATE SET`. Filters keys against `WORKSHOP_ITEM_COLUMNS` frozenset before building the SQL. Handles tags via junction table (parses JSON, calls `_ensure_tag_ids`, updates `workshop_tags`). Tags are excluded from the INSERT column list since they're no longer a workshop_items column.

### `insert_or_update_user` (database)

Same upsert pattern for `users` table, using `USER_COLUMNS` frozenset for filtering.

### `get_item_details` (database)

Returns all columns for a single workshop_id, joined with users table. Tags are returned as comma-separated via a correlated `GROUP_CONCAT` subquery against the junction table.

### `count_never_fetched_items` (database)

Counts items where `api_fetched_at IS NULL` (never successfully fetched) — used to determine if the processing queue needs more items.

### `toggle_subscription_queue_status` / `get_subscription_queue_items` (database)

Simple toggle and retrieval for the subscription queue feature.
