# Search & Filter System

The search and filter system translates user-facing field names, operators, and values into SQLite WHERE clauses. It supports text matching, numeric comparisons, JSON tag queries (via junction table), full-text search (via FTS5), percentile filtering, enum (chosen-value) filtering, and dual-field (original + translated) search.

---

## Field Name Mapping

### `FILTER_FIELD_TO_COLUMN` (database)

Maps user-facing field names (shown in TUI and Web UI dropdowns) to database column names. The mapping is used by `search_items`, `compute_wilson_cutoffs`, `_evaluate_filters`, and the web API. Key mappings:

| User-facing name | DB column |
|---|---|
| Title | title |
| Description | short_description |
| Filename | filename |
| Tags | tags |
| Author ID | creator_steamid |
| File Size | file_size |
| Subs | subscriptions |
| Favs | favorited |
| Views | views |
| Workshop ID | workshop_id |
| AppID | consumer_appid |
| Subscriber Score | wilson_subscription_score |
| Favorite Score | wilson_favorite_score |
| Full Text | full_text |
| Subscribed | subscription_state (virtual: the predicate spans `own_subscribed`, `own_first_subscribed_at`, `is_queued_for_subscription`, `steam_download_seen_at`) |

Fields not in the map fall through to the raw name (used by tests that pass DB column names directly).

### The `Subscribed` field (the one `"enum"`)

Every other field takes free text. `Subscribed` is chosen from a list, so its
schema entry carries `"type": "enum"` and `"values"`, and both front ends build a
Select/`<select>` for its value control (`is`/`is_not` are the only operators).
Its `db_col` is the virtual `subscription_state`, because no single column answers
the question: the value selects one of six predicates over four existing columns.

`SUBSCRIBED_VALUE_SPECS` is the single value table both evaluators read — `sql` is
the fragment the SELECT path uses and `matches` is the same predicate over an
in-memory row. Writing the six cases once is deliberate: they span four columns,
and a second copy in `_evaluate_single_filter` is exactly how the SQL search and
the daemon's in-memory decision would drift apart.

| value | matches |
|---|---|
| `any` | everything — no constraint |
| `never` | `own_first_subscribed_at IS NULL` — never seen subscribed (which includes queued items: they have never *been* subscribed) |
| `subscribed` | `COALESCE(own_subscribed, 0) = 1` |
| `previously` | `own_first_subscribed_at IS NOT NULL AND COALESCE(own_subscribed, 0) = 0` |
| `queued` | `COALESCE(is_queued_for_subscription, 0) = 1` |
| `downloaded` | `steam_download_seen_at IS NOT NULL` — the latch added with the green star |

`is_not` is the exact complement of `is`, applied in one place
(`_build_subscribed_clause` / `_evaluate_subscribed_filter`) rather than spelled
out per value, so `is_not never` is `subscribed` or `previously`, and so on. The
front ends **omit `any` from the value list while the operator is `is_not`**: a
NOT over "everything" matches nothing, so the combination is not offered. It is
still well defined if a saved filter or an API call carries it — `is_not any`
matches nothing in both evaluators, the same as any unknown value (`is` matches
nothing, `is_not` matches everything), keeping the pair complementary instead of
one side silently matching the whole table.

Two value spellings from before the marker vocabulary was unified are still
read: `currently` is mapped to `subscribed` and `pending` to `queued` by
`normalise_subscribed_value` before the value table is consulted, so a saved
view in `.tui_state.yaml`, a browser's stored view or a saved enrichment filter
keeps constraining the same population instead of silently falling back to
`any`. Nothing is migrated on disk.

### `_EN_COLUMN_FOR` (database)

Identifies columns with translated `_en` counterparts. When a text-matching operator is applied to a field in this set, the clause is expanded to search both the original and `_en` column. Currently: `title → title_en`, `short_description → short_description_en`, `extended_description → extended_description_en`.

### Full Text (database)

The "Full Text" field searches six text columns simultaneously: `title, title_en, short_description, short_description_en, extended_description, extended_description_en`. The set is expressed directly in the search builder rather than held in a named constant, so this page is the only place it is listed. Tags are NOT in it (they use exact matching via the junction table, not free-text search).

### `_TEXT_OPS` (database)

Operators that trigger dual-field expansion: `contains, does_not_contain, is, is_not`. Structural operators (`is_empty`, `gt`, `percentile`) use only the original column.

### `_TEXT_NEG_OPS` (database)

Negative operators that use AND (not OR) when expanding to dual-field: `does_not_contain, is_not`. The semantics differ: for `does_not_contain "Alpha"`, the item matches only if NEITHER the original NOR the translated column contains "Alpha". Using OR would match if either column lacks the term (which a NULL translated column always satisfies).

---

## Query Building Pipeline

### `search_items` (database)

The main entry point for all searches. Accepts filters as a list of dicts with keys `field`, `op`, `value`, and optional `logic` ("AND"/"OR", defaults to "AND"). The function:

1. **Column selection**: Uses `summary_only` columns (grid view) or `w.*` (full detail). Both include tags from the junction table via a correlated subquery.
2. **Filter processing**: Separates filters into three categories:
   - Percentile filters (op="percentile") — handled separately after the base WHERE clause is built
   - Tag filters (field maps to "tags") — routed through `_build_tag_clause`
   - Full Text (field maps to "full_text") — routed through `_build_fts_clause`
   - Enum filters (field maps to `subscription_state`) — routed through `_build_subscribed_clause`
   - Dual-field (field in `_EN_COLUMN_FOR` and operator in `_TEXT_OPS`) — expanded to search both columns
   - All others — routed through `_build_single_filter_clause`
3. **Base WHERE clause**: Non-percentile filters produce the base clause, wrapped in `AND (...)`.
4. **Percentile thresholds**: For each percentile filter, calls `_compute_percentile_threshold` with the base (non-percentile) filters. The threshold subquery runs NTILE(100) on the filtered dataset and returns the minimum score at the target bucket. Adds `db_col >= threshold` as a literal comparison.
5. **Sort, Limit, Offset**: Appends `ORDER BY w.{col}`, `LIMIT`, `OFFSET`.
6. **Overlay**: `search_items` also takes `subscribed_overlay`, the `Subscribed` view control's value. It is ANDed as one extra `AND (...)` *outside* the builder's parenthesised group, so an OR row cannot pull back what the overlay excluded, and it is kept out of the `filters` list so it can never be written by "Save Filter for Scraper". `any` (or no value) adds nothing; an unknown value adds nothing, because a view control must not silently hide the whole library. `compute_wilson_cutoffs` takes it too, so the percentiles describe the same population the grid shows.
7. **Settled hiding**: `search_items` takes `include_settled`, which defaults to `False`. Unless it is set, the base clause gains `AND live_fetch_status_predicate("w.fetch_status")` — `(w.fetch_status IS NULL OR w.fetch_status NOT IN (-1, -2))` — so dead (`-1`) and ignored (`-2`) items are not returned by any search. It is ANDed into the base clause rather than appended as a filter, so an OR row cannot pull a settled item back in, and it constrains the percentile thresholds with the rest of the population. The parameter is the escape hatch for the later "surface settled items" UI; no UI passes it yet. `compute_wilson_cutoffs` and `get_all_creator_ids` take the same parameter and hide by default, so the colours and the creator picker describe the visible set.

### `subscribed_overlay_clause` (database)

Builds the single overlay predicate from the same `SUBSCRIBED_VALUE_SPECS` table. The
overlay is always a positive selection (`is`), which is why it has no operator;
the field's own builder rows carry `is`/`is_not`.

### `_build_single_filter_clause` (database)

Converts a single operator-value pair into a SQL clause and parameter list. Supports:

| Operator | SQL |
|---|---|
| contains | `col LIKE '%val%'` |
| does_not_contain | `(col IS NULL OR col NOT LIKE '%val%')` |
| is | `col = val` |
| is_not | `col != val` |
| gt | `col > val` |
| lt | `col < val` |
| gte | `col >= val` |
| lte | `col <= val` |
| is_empty | `(col IS NULL OR col = '')` |
| is_not_empty | `(col IS NOT NULL AND col != '')` |

Unrecognized operators return `("", [])` and are silently skipped.

A `db_col` of `subscription_state` is handed to `_build_subscribed_clause` before
the operator table below is consulted; the enum's `is`/`is_not` are answered from
`SUBSCRIBED_VALUE_SPECS`, not by the generic `col = ?` / `col != ?` cases.

### `_build_tag_clause` (database)

Builds WHERE clauses for the tag junction table (`workshop_tags` + `tags`). The
name no longer mentions JSON because the JSON column went at migration 5→6; the
body has only ever built `EXISTS`/`NOT EXISTS` over the junction table, so it
takes the operator and value and no column name. Operators:

| Operator | Clause |
|---|---|
| contains | `EXISTS (SELECT 1 FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id = w.workshop_id AND t.tag_name = ?)` |
| does_not_contain | `NOT EXISTS (...)` |
| is_empty | `NOT EXISTS (SELECT 1 FROM workshop_tags WHERE workshop_id = w.workshop_id)` |
| is_not_empty | `EXISTS (SELECT 1 FROM workshop_tags WHERE workshop_id = w.workshop_id)` |

The `is` and `is_not` operators are intentionally skipped for tags (tag matching is always exact via `contains`/`does_not_contain`).

A single `contains` row is the clause above. When several `contains` rows are ANDed, the join level
(`_join_filter_clauses`, called by `build_filters_sql`, `_compute_percentile_threshold` and
`_wilson_population_where`) replaces the whole conjunction with one driving subquery — see
[The tag conjunction drives the scan](#the-tag-conjunction-drives-the-scan-issue-84). The function
itself is unchanged, because a lone tag keeps this `EXISTS`.

### `_build_fts_clause` (database)

Builds WHERE clauses using the FTS5 virtual table `workshop_fts`. This is only used when the user selects the "Full Text" field.

| Operator | Clause |
|---|---|
| contains | `workshop_fts MATCH ?` (free-text tokens, implicit AND) |
| does_not_contain | `workshop_fts MATCH ?` (wrapped with `NOT IN` at higher level) |
| is | `workshop_fts MATCH ?` (exact phrase via double-quoting in parameter) |
| is_not | `workshop_fts MATCH ?` (exact phrase, wrapped with `NOT IN`) |
| is_empty | `1 = 1` (all rows — handled at higher level with `NOT IN (SELECT rowid FROM workshop_fts)`) |
| is_not_empty | `1 = 1` (handled at higher level with `IN (SELECT rowid FROM workshop_fts)`) |

The `search_items` function wraps FTS clauses in `w.rowid IN (SELECT rowid FROM workshop_fts WHERE <clause>)` (or `NOT IN` for negatives). For `is_empty` and `is_not_empty`, the `1 = 1` placeholder is replaced with the appropriate subquery.

---

## Full-Text Search (FTS5)

### `workshop_fts` virtual table (database)

A content-sync FTS5 table defined in migration 4→5. Uses `content='workshop_items', content_rowid='workshop_id'`. Columns: `title, title_en, short_description, short_description_en, extended_description, extended_description_en`.

The table was populated once, by an FTS5 `'rebuild'` in that migration, and for a long time nothing maintained it afterwards — it held 640,471 documents against 1,725,544 items, so 62.9% of the library was invisible to Full Text search. Migration 14→15 rebuilds it and installs three sync triggers on `workshop_items` (insert, delete, and update scoped to the six indexed columns), so the index now tracks every write.

Because the table is *external content*, the update and delete triggers remove the old row with the FTS5 `'delete'` command carrying the previous column values. A plain `DELETE FROM workshop_fts` would leave the old tokens behind and corrupt later matches. The update trigger is scoped with `AFTER UPDATE OF` the six columns on purpose: most writes to `workshop_items` are queue and priority updates that touch none of them, and an unscoped trigger would rewrite part of the index on every priority bump. See [schema-migrations.md](schema-migrations.md) for the migration itself.

FTS5 tokenizes text by whitespace and punctuation (default unicode61 tokenizer). Multi-word searches are implicit AND. Phrase searches use double-quoting. FTS5 uses an inverted index for near-instant substring matching — dramatically faster than `LIKE '%text%'` which requires a full table scan.

### Performance

`LIKE '%text%'` cannot use a B-tree index — the leading wildcard defeats it — so the `contains` and
`does_not_contain` operators scan. `workshop_fts`'s inverted index answers the same kind of question in
O(log n), and the **Full Text** field is the only one routed through it; every other field's `contains`
is the scan.

*Measured 2026-09-21* on the pulled production snapshot (2,528,304 rows, 2.83 GB, warm page cache, this
container): `title LIKE '%genshin%'` returned 18,041 rows in **0.27 s**,
`extended_description LIKE '%genshin%'` returned 253 in **0.11 s**, and the FTS5 equivalent returned
24,060 documents in under **0.01 s**. An earlier version of this section claimed a LIKE query "could
take seconds" at 580K items; at more than four times that size it is a few hundred milliseconds. So the
scan is not a present-tense problem — but it is linear in the library and sensitive to the page cache,
where the FTS index is neither.

If substring search ever does become a bottleneck, the options in order of how much meaning they
preserve:

1. **Leave it.** The scan stays below the interaction threshold at this size.
2. **A trigram FTS5 index** (`tokenize='trigram'`) — the only option that keeps `contains` meaning
   "substring" while answering it from an index: SQLite uses a trigram index for `LIKE`/`GLOB` patterns
   of three characters or more. It costs a second index, kept current by the same triggers, which on a
   2.8 GB library is real storage.
3. **Route `contains` through the existing `workshop_fts`**, which changes the operator's meaning from
   substring to whole-token matching — a product decision, not an optimisation.

**Not verified here: the index's current integrity.** `INSERT INTO workshop_fts(workshop_fts)
VALUES('integrity-check')` is the tool, but it writes to the index's shadow tables and the pulled
snapshot is opened read-only, so it could not be run. The index was found drifted once — 640,471
documents against 1,725,544 items, 62.9% of the library invisible to Full Text — and migration 14→15
rebuilt it and installed the sync triggers that have maintained it since. Run that one statement
against a writable copy to confirm it today.

---

## Tag Junction Table

### Schema (migration 5→6)

Tags are stored in two normalized tables instead of a JSON array column:

```sql
CREATE TABLE tags (tag_id INTEGER PRIMARY KEY, tag_name TEXT UNIQUE NOT NULL);
CREATE TABLE workshop_tags (
    workshop_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (workshop_id, tag_id)
) WITHOUT ROWID;
```

The `tags` JSON column on `workshop_items` is dropped. The `WITHOUT ROWID` optimization saves space (both columns are already in the PK, no hidden rowid needed).

### `_ensure_tag_ids` (database)

The canonical tag-creation path. Given a list of tag name strings: bulk-inserts unknown names via `INSERT OR IGNORE`, then looks up all IDs. Used by both `insert_or_update_item` (runtime) and the migration (at scale).

### `_compute_tag_frequencies` (database)

Queries `workshop_tags JOIN tags` grouped by tag_name for frequency counts. Used by `get_db_stats` for the stats screen and by `compact_tag_ids` for space-efficiency reordering.

### `compact_tag_ids` (database)

Reorders tag IDs so the 127 most frequent tags occupy IDs 1-127 (the 1-byte SQLite varint range). Uses the same frequency data as the stats screen. Finds common tags with IDs > 127 and swaps them with less-common tags occupying low-ID slots. Called at migration end and on every stats screen refresh.

### `swap_tag_ids` (database)

Atomically swaps two tag IDs across both `tags` and `workshop_tags` tables using a temporary negative ID value. All six UPDATEs run in one transaction.

### `insert_or_update_item` tag handling (database)

When `tags` is present in the item data dict, the function:
1. Parses the JSON (or Python-repr fallback via `ast.literal_eval` for test compatibility)
2. Extracts tag names from dict-format (`{"tag": "name"}`) or string-format entries
3. Calls `_ensure_tag_ids` to get/create IDs
4. Deletes existing `workshop_tags` rows for the item, then inserts new ones
5. Tags are removed from the INSERT column list (they're not a workshop_items column anymore)

### The tag conjunction drives the scan (issue 84)

`Tags contains` renders one correlated `EXISTS` over the junction per row, so an AND of them can only
be *checked* against the rows whatever index the planner walks. Following the sort index means four
index probes per row until the limit is met, and when the filter is sparse along that sort order —
which is exactly what the tag conjunction is — the walk approaches a full scan. Measured on the
2.5 M-row copy (`/tmp/v35-normal.db`, 2,528,304 rows) with the owner's filter set — Tags *Mature*
**AND** Tags *Video* **AND** File Size > 100000000 **AND** Subscribed is_not *previously*, sorted
`wilson_subscription_score DESC LIMIT 50` — and with the query's pages dropped per run by
`posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)` rather than dropping system caches:

| access path | cold | warm |
|---|---|---|
| per-row correlated `EXISTS` per tag (before) | 65.94 s | 1.37 s |
| one driving subquery for the conjunction (after) | 7.79 s | 0.79 s |

A second paired run measured 61.41 s / 1.38 s against 7.62 s / 0.74 s, and the shipped builder's own
query — not the hand-substituted clause — measured 6.07 s cold / 0.89 s warm; the cold ratio is stable
near 8× and the absolute spread is the page path, not the plan.

The candidate set the driving form works from is the 219,078 items carrying **both** tags — 8.67% of the
copy — a fixed property of the data. The per-row form's cost is instead a function of where in the
score order the filter first matches: its plan is `SCAN w USING INDEX idx_wilson_subscription_score`
with one `CORRELATED SCALAR SUBQUERY` per tag, and near the top of that order almost nothing matches.
The driving plan is `SEARCH w USING INTEGER PRIMARY KEY (rowid=?)` fed by a `LIST SUBQUERY`, then a
temp B-tree sort; the score index is not scanned for the tags at all.

`ANALYZE` does not help. It changes the cold/warm *spread* but not the plan shape, because the
selectivity that matters is the cross-correlation between the tag filter and the sort order, and no
per-column statistic captures that. Only the ~500-row *unfiltered* diagnostic query is symmetric, which
is why it showed nothing.

**The ratio is a property of the copy, not a law.** The same two paths on a *compacted snapshot of the
owner's live database* (3,118,337 rows, 228,680 carrying both tags — 7.33%, pulled 2026-09-22) measured:

| access path | cold | warm |
|---|---|---|
| per-row correlated `EXISTS` per tag (before) | 5.29 s | 0.85 s |
| one driving subquery for the conjunction (after) | 6.99 s | 0.83 s |

So on compacted data the walk was *not* pathological: it was 5.3 s cold and 0.85 s warm, and the driving
form was slightly slower cold and equal warm. The 2.5M copy that showed 66 s → 8 s had a far more
scattered page layout, and a `VACUUM INTO` snapshot is contiguous by construction. Two consequences a
future reader should carry: the fix's justification is that the driving form's cost is **bounded** by the
candidate set while the walk's is **unbounded** — a function of where the filter first matches in the sort
order and of how scattered that walk's pages are — not that it is always faster; and the owner's 29.9 s
`POST /api/search` on the live file cannot be attributed to the plan alone, because a compacted copy of
that same data does not reproduce it. The route's per-item priority writes (150 transactions before the
batching in item 34) and the live file's physical state are the other measured contributors.

**The rule for a future reader: an AND of tag filters drives the scan.** The filter join
(`_join_filter_clauses`) splits the filter list into conjunctive segments — a new segment begins at
every `OR` — and when a segment carries two or more `Tags contains` rows it emits one
`_tag_contains_all_clause` in their place:

```sql
w.workshop_id IN (SELECT wt.workshop_id FROM workshop_tags wt JOIN tags t USING(tag_id)
                  WHERE t.tag_name IN (?, ...) GROUP BY wt.workshop_id
                  HAVING COUNT(DISTINCT t.tag_name) = N)
```

That is exactly "has every one of these tag names": duplicate names are deduplicated before the count
(`A AND A` is `A`, so counting the rows would demand a count no item can reach), and the
`IN`/`COUNT(DISTINCT ...)` pair uses the same BINARY, case-sensitive equality as the `t.tag_name = ?`
it replaces. The shapes left on the old form, deliberately:

- A **single** tag keeps its correlated `EXISTS`: it has no conjunction to drive, and one common tag is
  cheapest as a check when the sort index already matches it. Collapsing it would materialise a
  possibly-huge tag set to answer a question the index walk answers immediately.
- An **OR** of tag rows stays a union of `EXISTS`; a union is not a conjunction, so there is no count
  to take.
- A **`does_not_contain`** row stays a `NOT EXISTS` and is never counted into a conjunction.
- A **mix** keeps the existing parenthesised grouping, so each row's `logic` still means what it meant;
  only the all-AND segments are touched.

The same translation serves `search_items`, the metrics coverage query and the Wilson cutoff
population, so all three pick the same access path. A fixture with overlapping tag sets compares the
two clause forms row-for-row across every shape and asserts the owner's shape plans the `LIST SUBQUERY`
rather than the score index (`tests/test_search_filter.py`).

---

## Percentile Operator

### `_compute_percentile_threshold` (database)

Given a column name, a percentile value (0-99, clamped), and a set of base (non-percentile) filters: computes the minimum value in the top (100-P)% bucket using NTILE(100). P=0 returns None (no filter — all items pass).

The function builds a filtered WHERE clause from the base filters (using the same `_build_single_filter_clause` and `_build_tag_clause` routing as `search_items`), then runs:

```sql
SELECT COALESCE(MIN(col), 0) FROM (
    SELECT col, NTILE(100) OVER (ORDER BY col DESC) as tile
    FROM workshop_items WHERE <base_filters>
) WHERE tile = (100 - P)
```

The result is returned to `search_items` which adds `col >= threshold` as a literal comparison.

### `compute_wilson_cutoffs` exclusion (database)

When computing Wilson score percentile cutoffs for display coloring, any filter with `op = "percentile"` is skipped. This prevents the circular dependency where a percentile filter would reference cutoffs that are themselves computed from the filtered dataset.

The population is also restricted by `include_settled`, which defaults to hiding: the cutoffs colour the rows the grid shows, so a percentile computed over dead or ignored rows would give a visible item a colour from a distribution the owner cannot see. With `include_settled=False` (the default) the query carries `live_fetch_status_predicate("w.fetch_status")`; the filter group is parenthesised alongside it so an OR row cannot absorb the live clause and leak a settled row into the percentile.

### `compute_wilson_cutoffs` cost (database)

One pass, ten aggregates. For each score column it reports the p99/p90/p50 cutoffs — the minimum of `NTILE(100)` buckets 1, 10 and 50 — plus the min and max. The bucket boundaries are computed as exact ranks from the non-NULL counts ([`_wilson_bucket_rank`](../src/database.py)) and read back with `percentile_disc(Y, rank/N)`, so neither `NTILE` window is materialised. The two-window form is kept beside it as [`_wilson_cutoffs_ntile`](../src/database.py), and `test_percentile_disc_cutoffs_match_the_ntile_window` compares the two at every count that changes the bucket arithmetic (0, 1, 5, 9, 10, 49, 50, 99, 100, 101, 199, 200, 999, 1000), with tied scores and NULLs, and under a tag filter.

*Measured 2026-09-22* on the 2.5 M-row copy: **2.25 s** for the exact-rank form, against **13.24 s** for the `NTILE` form (best of 2 each; the earlier measurement recorded in this investigation was 9.54 s). Both returned identical values on all ten keys. The client cache key is `JSON.stringify([filters, overlay])` (`loadCutoffs`, `templates/index.html:550-559`), so the sort is not part of it and a sort change does not refetch: this is paid once per filter set and at first load, not per page. The server-side cache (`/api/cutoffs`, default TTL 86400 s) means the slower form below is paid once per filter set too.

**SQLite requirement and fallback.** `percentile_disc` needs **both** SQLite **3.51+** *and* the `SQLITE_ENABLE_PERCENTILE` compile option — the builtin is registered inside `#ifdef SQLITE_ENABLE_PERCENTILE` in the amalgamation, so the version alone is not enough. CPython's own Windows build compiles its bundled SQLite with `SQLITE_ENABLE_MATH_FUNCTIONS;SQLITE_ENABLE_FTS4;SQLITE_ENABLE_FTS5;SQLITE_ENABLE_RTREE;SQLITE_OMIT_AUTOINIT` and nothing else (`PCbuild/sqlite3.vcxproj`, the same list on the 3.12 and 3.14 branches), so the DLL a stock Windows CPython loads has never provided it: production's `no such function` was the missing compile option as much as the 3.49.1 version, and a CPython upgrade alone would not have fixed it. The official [sqlite.org release DLL](https://sqlite.org/download.html) (`sqlite-dll-win-x64-*.zip`, 3.53.4 at the time of writing) does define it, together with FTS5, RTREE and `THREADSAFE=1`, which is what makes replacing `DLLs\sqlite3.dll` the way to get the fast path on Windows. It is *probed* rather than assumed: on the first cutoff call `_percentile_disc_is_available` runs `SELECT percentile_disc(1, 0.5)` and caches the answer in a module flag for the life of the process, logging once, with `sqlite3.sqlite_version`, which path is in use. An `OperationalError` naming the function means absent — that is production's SQLite **3.49.1** under Python 3.12.10, where the query used to fail with `no such function: percentile_disc` and the grid lost all score highlighting. On such a host `compute_wilson_cutoffs` runs [`_wilson_cutoffs_ntile`](../src/database.py); the container's SQLite **3.53.1** under Python 3.12.14 provides the builtin, which is why the test suite alone could not see the regression. Both paths return the same ten keys and values, so the 13.24 s form is a one-off fallback cost rather than a reason to require 3.51 (see [schema-migrations.md](schema-migrations.md) for the SQLite versions in use).

---

## In-Memory Filter Evaluation

### `_evaluate_filters` (database)

Used by the daemon's `_should_enrich` to check whether an in-memory item dict passes the enrichment filters, and by migration 21→22's demotion walk. Iterates each filter, calls `_evaluate_single_filter`, returns False if any filter fails.

**A saved enrichment filter can now be a `Subscribed` row**, so both in-memory
sites read the four columns that field needs. The daemon's merged record drops
the queue-owned columns by design (`MERGE_EXCLUDED_KEYS`), so `_raise_scrape_and_image_priorities`
overlays the pre-fetch record's values for exactly those columns on a copy before
evaluating — never back onto the record it stores. The demotion walk selects only
the columns a filter references and expands the virtual `subscription_state` to all
four, so its rows carry what the predicate reads.

### `_evaluate_single_filter` (database)

Checks a single filter criterion against an in-memory item dict. Mirrors `_build_single_filter_clause` semantics but operates on Python values. Handles:
- Text operators (`contains`, `does_not_contain`, `is`, `is_not`, `is_empty`, `is_not_empty`)
- Numeric operators (`gt`, `lt`, `gte`, `lte`) with type coercion (item and value both cast to int)
- Tags routing to `_evaluate_tag_filter`
- The `Subscribed` field routing to `_evaluate_subscribed_filter`, which reads the same `SUBSCRIBED_VALUE_SPECS` table the SQL clause is built from
- Percentile operator (always returns True — percentile needs dataset context, not single-item evaluation)

### `_evaluate_tag_filter` (database)

Checks whether an item's tags (parsed from JSON) match a filter. Used by `_evaluate_filters` for enrichment gating. Parses the `tags` field from the item dict (which at enrichment time still contains the API JSON format, pre-junction-table conversion).

---

## Sort Validation

### `VALID_SORT_COLS` (database)

Whitelist of columns that can appear in `ORDER BY`. Any sort column not in this set produces an empty sort clause (no sorting). Contains: `title, file_size, subscriptions, favorited, views, workshop_id, steam_created_at, steam_updated_at, api_fetched_at, wilson_favorite_score, wilson_subscription_score, own_first_subscribed_at`.

`own_first_subscribed_at` is the sticky first-seen-subscribed stamp (the only
subscription timestamp there is), labelled **Subscribed at** in both sort
dropdowns. NULL means never subscribed; SQLite orders NULL below every value, so
descending leaves the never-subscribed rows last. A test pins that behaviour
rather than assuming it.

### `_build_sort_clause` (database)

Validates the sort column against `VALID_SORT_COLS`, then builds `ORDER BY w.{col} {ASC|DESC}`. The `w.` prefix prevents ambiguity in JOIN queries (both `workshop_items` and `creators` have an `api_fetched_at` column).

### Sort indexes (the "Subscriber Score is slow" investigation)

Every column in `VALID_SORT_COLS` is backed by a leading-column index that `_ensure_indexes` (`src/database.py`) creates from one table, `QUERY_INDEXES`; `tests/test_sort_index_invariant.py` proves the invariant on both schema paths (the fresh builder and the legacy chain), and `GET /api/search_diagnostic` reports the live file's set (behind `daemon.capture_web_ui_trace`, [web-ui.md](web-ui.md#api-search_diagnostic--get)). Because the expected set is data, a future sortable column added without an index fails a test rather than shipping.

A name is not enough for the live file. `_ensure_indexes` creates with `CREATE INDEX IF NOT EXISTS`, so an index that exists under the expected name but on a **different column** — or as a partial or unique index — is never repaired, and the diagnostic's original name-only check called it `present`. The route now reports each `QUERY_INDEXES` entry's actual `PRAGMA index_info` columns and its `PRAGMA index_list` `unique`/`partial` flags beside the expected ones, classifies it `present`, `missing` or `wrong_definition`, and carries a derived `uses_index` boolean beside each sort column's plan text — the question the owner was answering by eye. On a correct schema every entry is `present` and every named sort index reports `uses_index: true`; `workshop_id`, the rowid alias whose B-tree *is* the table, reports `expected_index: null` and `uses_index: null` rather than a false alarm.

The owner reported 2026-09-22 that subscriber score "is not indexed" — favourite score queried ~70 items/s against 3-4 for subscriber score. **The hypothesis does not hold in this schema**: both score indexes were created in the same commit (`8771877`, 2026-05-15) and no path creates one without the other. *Re-measured 2026-09-22* on the 2.5 M-row copy (`/tmp/v35-normal.db`, 2,528,304 rows, 68,508 settled, warm page cache, best of 3, the real summary query with the live-status clause and the creators join):

| offset | `wilson_favorite_score` | `wilson_subscription_score` |
|---|---|---|
| 0 | 0.000 s | 0.000 s |
| 50,000 | 0.129 s | 0.145 s |
| 200,000 | 0.563 s | 0.575 s |

`EXPLAIN QUERY PLAN` is `SCAN w USING INDEX idx_wilson_<column>_score` for both, and both columns are 99.98% populated (2,527,901 of 2,528,304), so neither a missing index nor a coverage difference explains a 20× gap. `sqlite_stat1` is **absent** in every copy measured here — `ANALYZE` has never been run — so there are no planner statistics to be skewed either. The two columns sort within noise of each other; the live asymmetry is a property of the live file's state or of the page path around the query, not of this repository's index set. (An earlier measurement in this investigation recorded 0.01 s at 50,000 and 0.38-0.40 s at 200,000; the difference is cache warmth, not a different query, and both runs show the same equality between the columns.) A live `idx_wilson_subscription_score` defined on the wrong column would produce exactly that asymmetry while every copy here looks correct and identical — which is why the diagnostic compares each index's definition, not only its name.

What the schema *did* have was a different sortable column with no index at all: `own_first_subscribed_at` (**Subscribed at**) sat in `VALID_SORT_COLS` without a leading index, so every page sorted by it ran `USE TEMP B-TREE FOR ORDER BY` over the live set — a full sort on the request the user waits for, at every offset. *Measured* on the same copy: 0.47/0.48/0.49 s at offsets 0/50,000/200,000 before, and 0.00/0.02/0.07 s after adding `idx_own_first_subscribed_at` (plan `SCAN w USING INDEX idx_own_first_subscribed_at`). That is the cost the invariant test and `GET /api/search_diagnostic` now guard.

---

## Frontend Filter Definitions

### Web UI (`index.html`)

Three operator categories:
- **text**: `contains, does_not_contain, is, is_not, is_empty, is_not_empty` — for Title, Description, Filename, Tags, Full Text
- **numeric**: text ops + `gt, lt, gte, lte, percentile` — for File Size, Subs, Favs, Views, Subscriber Score, Favorite Score
- **id**: `is, is_not` — for Author ID, Workshop ID, AppID
- **enum**: `is, is_not` — for Subscribed, whose value control is a `<select>` of the schema's `values` (`any` omitted while the operator is `is_not`) rather than a free-text input

The `updateOps` function switches operator options when the field dropdown
changes; `updateValueControl` then swaps the value control between an `<input>`
and a `<select>` to match the field's type. Percentile values are clamped to 0-99
on blur (via capture-phase event listener) and in `getFilters()`.

**The `Subscribed:` overlay** is a separate control beside the sort menus, not a
builder row: changing the builder does not clear it, it never appears in the
builder, and it is not written by "Save for scraper". It is greyed out (with a
tooltip saying why) whenever the builder holds a `Subscribed` row, and while it is
greyed out it contributes nothing to the search — ANDing a hidden second
constraint on the same field is how a result silently comes back empty. Its value
is persisted with the view (`view.state.v1` in the browser, `subscribed_overlay`
in `.tui_state.yaml`), so a reload and the author jump's restore keep it.

### TUI (`tui.py`)

Same operator/field definitions in `SearchBuilder.operators` and `SearchRow.on_select_changed`. `SearchRow` mounts both an `Input` and a `Select` for the value and shows the one the field's type calls for (`_sync_value_control`); the Select's options come from the schema's `values`, with `any` dropped for `is_not`. A value restored from a saved filter that the choice list does not offer (a legacy or API-written value) is kept as an extra option so the row displays and round-trips it. Percentile values are clamped on blur via `Input.Blurred` event handler with a `_clamp_percentile` helper, and again in `get_filters()`.

The `Subscribed:` overlay sits with the sort controls (`#subscribed-overlay`).
`ScraperApp._sync_subscribed_overlay` greys it out while a builder row names the
field, and `_effective_subscribed_overlay` returns `any` in that state so the
search ignores it.
