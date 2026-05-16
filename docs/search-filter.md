# Search & Filter System

The search and filter system translates user-facing field names, operators, and values into SQLite WHERE clauses. It supports text matching, numeric comparisons, JSON tag queries (via junction table), full-text search (via FTS5), percentile filtering, and dual-field (original + translated) search.

---

## Field Name Mapping

### `FIELD_NAME_MAP` (database)

Maps user-facing field names (shown in TUI and Web UI dropdowns) to database column names. The mapping is used by `search_items`, `compute_wilson_cutoffs`, `_evaluate_filters`, and the web API. Key mappings:

| User-facing name | DB column |
|---|---|
| Title | title |
| Description | short_description |
| Filename | filename |
| Tags | tags |
| Author ID | creator |
| File Size | file_size |
| Subs | subscriptions |
| Favs | favorited |
| Views | views |
| Workshop ID | workshop_id |
| AppID | consumer_appid |
| Language ID | language |
| Subscriber Score | wilson_subscription_score |
| Favorite Score | wilson_favorite_score |
| Full Text | full_text |

Fields not in the map fall through to the raw name (used by tests that pass DB column names directly).

### `_EN_FIELDS` (database)

Identifies columns with translated `_en` counterparts. When a text-matching operator is applied to a field in this set, the clause is expanded to search both the original and `_en` column. Currently: `title → title_en`, `short_description → short_description_en`, `extended_description → extended_description_en`.

### `_FULL_TEXT_COLS` (database)

The six text columns searched simultaneously when the user selects the "Full Text" field: `title, title_en, short_description, short_description_en, extended_description, extended_description_en`. Tags are NOT in this list (they use exact matching via the junction table, not free-text search).

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
   - Tag filters (field maps to "tags") — routed through `_build_json_tag_clause`
   - Full Text (field maps to "full_text") — routed through `_build_fts_clause`
   - Dual-field (field in `_EN_FIELDS` and operator in `_TEXT_OPS`) — expanded to search both columns
   - All others — routed through `_build_filter_clause`
3. **Base WHERE clause**: Non-percentile filters produce the base clause, wrapped in `AND (...)`.
4. **Percentile thresholds**: For each percentile filter, calls `_compute_percentile_threshold` with the base (non-percentile) filters. The threshold subquery runs NTILE(100) on the filtered dataset and returns the minimum score at the target bucket. Adds `db_col >= threshold` as a literal comparison.
5. **Sort, Limit, Offset**: Appends `ORDER BY w.{col}`, `LIMIT`, `OFFSET`.

### `_build_filter_clause` (database)

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

### `_build_json_tag_clause` (database)

Builds WHERE clauses for the tag junction table (`workshop_tags` + `tags`). Operators:

| Operator | Clause |
|---|---|
| contains | `EXISTS (SELECT 1 FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id = w.workshop_id AND t.tag_name = ?)` |
| does_not_contain | `NOT EXISTS (...)` |
| is_empty | `NOT EXISTS (SELECT 1 FROM workshop_tags WHERE workshop_id = w.workshop_id)` |
| is_not_empty | `EXISTS (SELECT 1 FROM workshop_tags WHERE workshop_id = w.workshop_id)` |

The `is` and `is_not` operators are intentionally skipped for tags (tag matching is always exact via `contains`/`does_not_contain`).

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

A content-sync FTS5 table defined in migration 4→5. Uses `content='workshop_items', content_rowid='workshop_id'` to automatically read from the base table — no triggers needed. Columns: `title, title_en, short_description, short_description_en, extended_description, extended_description_en`.

FTS5 tokenizes text by whitespace and punctuation (default unicode61 tokenizer). Multi-word searches are implicit AND. Phrase searches use double-quoting. FTS5 uses an inverted index for near-instant substring matching — dramatically faster than `LIKE '%text%'` which requires a full table scan.

### Performance

`LIKE '%text%'` can't use B-tree indexes (leading wildcard defeats the index). FTS5's inverted index finds matching documents in O(log n). On 580K items, a LIKE query could take seconds; FTS5 returns in milliseconds. The FTS5 approach is used exclusively for the "Full Text" field; individual field searches still use LIKE.

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

---

## Percentile Operator

### `_compute_percentile_threshold` (database)

Given a column name, a percentile value (0-99, clamped), and a set of base (non-percentile) filters: computes the minimum value in the top (100-P)% bucket using NTILE(100). P=0 returns None (no filter — all items pass).

The function builds a filtered WHERE clause from the base filters (using the same `_build_filter_clause` and `_build_json_tag_clause` routing as `search_items`), then runs:

```sql
SELECT COALESCE(MIN(col), 0) FROM (
    SELECT col, NTILE(100) OVER (ORDER BY col DESC) as tile
    FROM workshop_items WHERE <base_filters>
) WHERE tile = (100 - P)
```

The result is returned to `search_items` which adds `col >= threshold` as a literal comparison.

### `compute_wilson_cutoffs` exclusion (database)

When computing Wilson score percentile cutoffs for display coloring, any filter with `op = "percentile"` is skipped. This prevents the circular dependency where a percentile filter would reference cutoffs that are themselves computed from the filtered dataset.

---

## In-Memory Filter Evaluation

### `_evaluate_filters` (database)

Used by the daemon's `_should_enrich` to check whether an in-memory item dict passes the enrichment filters. Iterates each filter, calls `_evaluate_single_filter`, returns False if any filter fails. The item dict contains API response fields (not DB columns) — tags are checked via `_evaluate_tag_filter`.

### `_evaluate_single_filter` (database)

Checks a single filter criterion against an in-memory item dict. Mirrors `_build_filter_clause` semantics but operates on Python values. Handles:
- Text operators (`contains`, `does_not_contain`, `is`, `is_not`, `is_empty`, `is_not_empty`)
- Numeric operators (`gt`, `lt`, `gte`, `lte`) with type coercion (item and value both cast to int)
- Tags routing to `_evaluate_tag_filter`
- Percentile operator (always returns True — percentile needs dataset context, not single-item evaluation)

### `_evaluate_tag_filter` (database)

Checks whether an item's tags (parsed from JSON) match a filter. Used by `_evaluate_filters` for enrichment gating. Parses the `tags` field from the item dict (which at enrichment time still contains the API JSON format, pre-junction-table conversion).

---

## Sort Validation

### `VALID_SORT_COLS` (database)

Whitelist of columns that can appear in `ORDER BY`. Any sort column not in this set produces an empty sort clause (no sorting). Contains: `title, file_size, subscriptions, favorited, views, workshop_id, time_created, time_updated, dt_updated, wilson_favorite_score, wilson_subscription_score`.

### `_build_sort_clause` (database)

Validates the sort column against `VALID_SORT_COLS`, then builds `ORDER BY w.{col} {ASC|DESC}`. The `w.` prefix prevents ambiguity in JOIN queries (both `workshop_items` and `users` have a `dt_updated` column).

---

## Frontend Filter Definitions

### Web UI (`index.html`)

Three operator categories:
- **text**: `contains, does_not_contain, is, is_not, is_empty, is_not_empty` — for Title, Description, Filename, Tags, Full Text
- **numeric**: text ops + `gt, lt, gte, lte, percentile` — for File Size, Subs, Favs, Views, Language ID, Subscriber Score, Favorite Score
- **id**: `is, is_not` — for Author ID, Workshop ID, AppID

The `updateOps` function switches operator options when the field dropdown changes. Percentile values are clamped to 0-99 on blur (via capture-phase event listener) and in `getFilters()`.

### TUI (`tui.py`)

Same operator/field definitions in `SearchBuilder.operators` and `SearchRow.on_select_changed`. The `SearchRow.compose` method determines field type at mount and on field change. Percentile values are clamped on blur via `Input.Blurred` event handler with a `_clamp_percentile` helper, and again in `get_filters()`.
