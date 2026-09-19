import os
import sqlite3
import shlex
import re
import json
import time
import logging
from datetime import datetime, timedelta, timezone

WORKSHOP_ITEM_COLUMNS = frozenset({
    "workshop_id", "first_seen_at", "api_fetched_at", "last_fetch_attempted_at",
    "scrape_version", "translate_version",
    "fetch_status", "title", "title_en", "creator_steamid", "creator_appid", "consumer_appid",
    "filename", "file_size", "preview_url", "hcontent_file", "hcontent_preview",
    "short_description", "short_description_en", "steam_created_at", "steam_updated_at",
    "visibility", "banned", "ban_reason", "app_name", "file_type",
    "subscriptions", "favorited", "views",
    "extended_description", "extended_description_en",
    "lifetime_subscriptions", "lifetime_favorited", "translation_priority",
    "is_queued_for_subscription", "wilson_favorite_score",
    "wilson_subscription_score", "web_scrape_priority",
    "image_answer", "image_priority", "api_priority",
    "own_subscribed", "own_first_subscribed_at", "steam_download_seen_at",
    # Our completion clocks for the three stages that had none: when *we*
    # scraped the page, fetched the image and finished translating the item.
    # Distinct from scrape_version/translate_version, which store Steam's
    # revision. See docs/timestamps.md.
    "web_scraped_at", "image_fetched_at", "translated_at",
})

# ``users.dt_translated`` was renamed to ``translated_at`` (not
# ``translate_version``) because it holds our wall-clock time for users, not a
# Steam ``steam_updated_at`` version key: users have no ``steam_updated_at``.
CREATOR_COLUMNS = frozenset({
    "steamid", "personaname", "personaname_en",
    "api_fetched_at", "translated_at", "translation_priority",
})

def get_connection(db_path: str):
    """
    Returns a SQLite connection with a Row factory.

    The journal mode is deliberately *not* set here. It is a persistent property
    of the database file, established once by ``initialize_database``; running
    ``PRAGMA journal_mode=WAL`` on every connection makes a read-only caller
    reach for a mode change, and that statement is not covered by the
    connection's busy timeout, so a lock held by another process turns into
    ``sqlite3.OperationalError`` on a plain read. After that change, WAL allows
    simultaneous readers and writers as before.
    """
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn

def normalize_tags(raw_tags) -> str:
    """Accepts tags as list-of-dicts, list-of-strings, JSON string, or bare string.
    Returns a sorted, deduplicated JSON string."""
    normalized = []
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, dict) and "tag" in t:
                normalized.append(t["tag"])
            elif isinstance(t, str):
                normalized.append(t)
    elif isinstance(raw_tags, str):
        try:
            t_list = json.loads(raw_tags)
            if isinstance(t_list, list):
                for t in t_list:
                    if isinstance(t, dict) and "tag" in t:
                        normalized.append(t["tag"])
                    elif isinstance(t, str):
                        normalized.append(t)
            else:
                normalized = [raw_tags]
        except json.JSONDecodeError:
            normalized = [raw_tags]
    return json.dumps(sorted(set(normalized)), ensure_ascii=False)

# --- the Subscribed filter field -----------------------------------------------
#
# Every other field filters one column with free text. `Subscribed` is the first
# whose value is chosen from a list, and whose predicate reads four columns at
# once, so it gets its own type ("enum") and its own value table. That table is
# the single source both evaluators read -- the SQL builder and the in-memory
# mirror -- because six cases written twice is exactly how the two would drift.
SUBSCRIBED_FIELD = "Subscribed"
# A virtual column name: no workshop_items column is called this. It exists so
# the schema entry can carry one db_col like every other field while its
# predicate spans the four real columns in SUBSCRIBED_FILTER_COLUMNS.
SUBSCRIBED_VIRTUAL_COLUMN = "subscription_state"
SUBSCRIBED_VALUES = ["any", "never", "subscribed", "previously", "queued", "downloaded"]
SUBSCRIBED_FILTER_COLUMNS = (
    "own_subscribed", "own_first_subscribed_at",
    "is_queued_for_subscription", "steam_download_seen_at",
)

# The marker vocabulary unified two values a saved view or a saved filter may
# still carry: the overlay's `currently` is now the `subscribed` value, and the
# marker's `pending` is now `queued`. They are read and mapped onto the current
# table rather than migrated, because an unknown value constrains nothing and a
# stale saved view would otherwise silently widen to `any`.
LEGACY_SUBSCRIBED_VALUES = {"currently": "subscribed", "pending": "queued"}


def normalise_subscribed_value(value) -> str:
    """``value`` in the current value table's spelling, mapping legacy words."""
    text = str(value)
    return LEGACY_SUBSCRIBED_VALUES.get(text, text)


def _bool_column(item: dict, column: str) -> int:
    """A boolean/queue column read as 0 or 1, with NULL and a missing key both 0.

    The SQL side wraps the same columns in COALESCE, so a stored NULL and a row
    dict that never carried the key evaluate the same way in both evaluators.
    """
    try:
        return 1 if int(item.get(column) or 0) else 0
    except (TypeError, ValueError):
        return 0


# value -> its predicate, in both spellings, in one place. `sql` is the fragment
# the SELECT path uses; `matches` is the same predicate over an in-memory row.
# `columns` names the real columns the value reads, so a caller that has to load
# a partial row (the demotion walk) can load all of them.
SUBSCRIBED_VALUE_SPECS = {
    "any": {
        "sql": "1 = 1",
        "columns": (),
        "matches": lambda item: True,
    },
    "never": {
        "sql": "own_first_subscribed_at IS NULL",
        "columns": ("own_first_subscribed_at",),
        "matches": lambda item: item.get("own_first_subscribed_at") is None,
    },
    "subscribed": {
        "sql": "COALESCE(own_subscribed, 0) = 1",
        "columns": ("own_subscribed",),
        "matches": lambda item: _bool_column(item, "own_subscribed") == 1,
    },
    # `previously` is the complement of `never OR subscribed`, so its negation is
    # exactly `never OR subscribed` -- see _build_subscribed_clause.
    "previously": {
        "sql": "(own_first_subscribed_at IS NOT NULL AND COALESCE(own_subscribed, 0) = 0)",
        "columns": ("own_subscribed", "own_first_subscribed_at"),
        "matches": lambda item: (
            item.get("own_first_subscribed_at") is not None
            and _bool_column(item, "own_subscribed") == 0
        ),
    },
    "queued": {
        "sql": "COALESCE(is_queued_for_subscription, 0) = 1",
        "columns": ("is_queued_for_subscription",),
        "matches": lambda item: _bool_column(item, "is_queued_for_subscription") == 1,
    },
    "downloaded": {
        "sql": "steam_download_seen_at IS NOT NULL",
        "columns": ("steam_download_seen_at",),
        "matches": lambda item: item.get("steam_download_seen_at") is not None,
    },
}

SEARCH_FILTER_SCHEMA = [
    {"field": "Full Text",        "db_col": "full_text",                 "type": "string", "ops": ["contains", "does_not_contain"]},
    {"field": "Title",            "db_col": "title",                     "type": "string", "ops": ["contains", "does_not_contain", "is", "is_not"]},
    {"field": "Description",      "db_col": "short_description",          "type": "string", "ops": ["contains", "does_not_contain", "is", "is_not"]},
    {"field": "Tags",             "db_col": "tags",                      "type": "string", "ops": ["contains", "does_not_contain"]},
    {"field": "Subscriber Score", "db_col": "wilson_subscription_score", "type": "number", "ops": ["gt", "lt", "gte", "lte", "percentile"]},
    {"field": "Favorite Score",   "db_col": "wilson_favorite_score",     "type": "number", "ops": ["gt", "lt", "gte", "lte", "percentile"]},
    {"field": "Author ID",        "db_col": "creator_steamid",           "type": "id",     "ops": ["is", "is_not"]},
    {"field": "Workshop ID",      "db_col": "workshop_id",               "type": "id",     "ops": ["is", "is_not"]},
    {"field": "App ID",           "db_col": "consumer_appid",            "type": "id",     "ops": ["is", "is_not"]},
    {"field": "Subs",             "db_col": "subscriptions",             "type": "number", "ops": ["gt", "lt", "gte", "lte", "percentile"]},
    {"field": "Favs",             "db_col": "favorited",                 "type": "number", "ops": ["gt", "lt", "gte", "lte", "percentile"]},
    {"field": "Views",            "db_col": "views",                     "type": "number", "ops": ["gt", "lt", "gte", "lte", "percentile"]},
    {"field": "File Size",        "db_col": "file_size",                  "type": "number", "ops": ["gt", "lt", "gte", "lte"]},
    {"field": SUBSCRIBED_FIELD,   "db_col": SUBSCRIBED_VIRTUAL_COLUMN,    "type": "enum",   "values": SUBSCRIBED_VALUES, "ops": ["is", "is_not"]},
]

# Build FILTER_FIELD_TO_COLUMN and ALL_FILTER_FIELDS from the schema
ALL_FILTER_FIELDS = [f["field"] for f in SEARCH_FILTER_SCHEMA]
FILTER_FIELD_TO_COLUMN = {f["field"]: f["db_col"] for f in SEARCH_FILTER_SCHEMA}
# AppID backwards-compat alias
FILTER_FIELD_TO_COLUMN["AppID"] = "consumer_appid"
FILTER_FIELD_TO_COLUMN["Filename"] = "filename"

# Fields that have a translated _en counterpart; these are dual-searched
# when the operator is a text-matching one (contains, is, etc.)
_EN_COLUMN_FOR = {
    "title": "title_en",
    "short_description": "short_description_en",
    "extended_description": "extended_description_en",
}

# Operators that trigger dual-field (original + translated) search
_TEXT_OPS = {"contains", "does_not_contain", "is", "is_not"}
# Positive operators join with OR (match if either column matches);
# negative operators join with AND (match only if neither column matches).
_TEXT_NEG_OPS = {"does_not_contain", "is_not"}

VALID_SORT_COLS = {
    "title", "file_size", "subscriptions", "favorited", "views",
    "workshop_id", "steam_created_at", "steam_updated_at", "api_fetched_at",
    "wilson_favorite_score", "wilson_subscription_score",
    # The sticky first-seen-subscribed stamp: the only subscription timestamp
    # there is. NULL means never subscribed; SQLite orders NULL below every
    # value, so descending puts the never-subscribed rows last.
    "own_first_subscribed_at",
}

# The lowest priority a *user request* carries, on the shared queue vocabulary
# (see the Queue Priorities table in data-model.md). Everything below it belongs
# to the daemon: 1 backlog, 2 a retry after a stage failure, 3 a newly discovered
# item. Only 5 (the item was shown in a list) and 10 (it is open in a detail
# pane) mean a person asked for this item, which is why the dependent stages and
# the migration that demotes filter-excluded rows both draw the line here.
USER_PRIORITY_FLOOR = 5

# The terminal schema version the migration chain reaches. Module level rather
# than local to `initialize_database` because the migration tests assert that
# the chain reaches it, and a magic number repeated in nine test files is a
# number that will be wrong after the next migration.
EXPECTED_VERSION = 33

def _build_text_search_clauses(sql: str, params: list, query_string: str, cols: list[str]) -> tuple[str, list]:
    """Applies positive/negative text search tokens to SQL via LIKE clauses."""
    pos_tokens, neg_tokens = _parse_query(query_string)
    for token in pos_tokens:
        clauses = [f"{col} LIKE ?" for col in cols]
        sql += f" AND ({' OR '.join(clauses)})"
        params.extend([f"%{token}%"] * len(cols))
    for token in neg_tokens:
        for col in cols:
            sql += f" AND ({col} IS NULL OR {col} NOT LIKE ?)"
            params.append(f"%{token}%")
    return sql, params

def _build_subscribed_clause(op: str, val) -> tuple[str, list]:
    """SQL for the Subscribed field, read from the shared value table.

    ``is_not`` is exactly the complement of ``is``. That includes ``is_not any``,
    which the front ends do not offer -- a NOT over "everything" matches nothing
    -- but a saved filter or an API call can still carry. An unknown value is
    treated the same way (``is`` matches nothing, ``is_not`` matches everything),
    so the pair stays complementary rather than one side silently matching the
    whole table.
    """
    if op not in ("is", "is_not"):
        return ("", [])
    spec = SUBSCRIBED_VALUE_SPECS.get(normalise_subscribed_value(val))
    if spec is None:
        return ("0 = 1", []) if op == "is" else ("1 = 1", [])
    clause = spec["sql"]
    if op == "is_not":
        clause = f"NOT ({clause})"
    return (clause, [])


def subscribed_overlay_clause(value) -> tuple[str, list]:
    """The single predicate a Subscribed overlay control ANDs onto a search.

    ``any`` (and no value at all) is the control's off switch: no constraint. An
    unknown value constrains nothing either -- the overlay is view state a client
    sends back, and a value this build does not know must not silently hide the
    whole library. The overlay is always a positive selection, which is why it
    has no operator: the field's own builder rows carry ``is``/``is_not``.
    """
    if value is None or value == "any":
        return ("", [])
    spec = SUBSCRIBED_VALUE_SPECS.get(normalise_subscribed_value(value))
    if spec is None:
        return ("", [])
    return (spec["sql"], [])


def _build_single_filter_clause(db_col: str, op: str, val) -> tuple[str, list]:
    """Converts an operator and value into a SQL clause string and param list."""
    if db_col == SUBSCRIBED_VIRTUAL_COLUMN:
        return _build_subscribed_clause(op, val)
    op_map = {
        "contains": (f"{db_col} LIKE ?", [f"%{val}%"]),
        "does_not_contain": (f"({db_col} IS NULL OR {db_col} NOT LIKE ?)", [f"%{val}%"]),
        "is": (f"{db_col} = ?", [val]),
        "is_not": (f"{db_col} != ?", [val]),
        "gt": (f"{db_col} > ?", [val]),
        "lt": (f"{db_col} < ?", [val]),
        "gte": (f"{db_col} >= ?", [val]),
        "lte": (f"{db_col} <= ?", [val]),
        "is_empty": (f"({db_col} IS NULL OR {db_col} = '')", []),
        "is_not_empty": (f"({db_col} IS NOT NULL AND {db_col} != '')", []),
    }
    return op_map.get(op, ("", []))


def _build_fts_clause(op: str, val) -> tuple[str, list]:
    """Builds a WHERE clause using FTS5 MATCH for the workshop_fts table.
    Returns a clause suitable for:  w.rowid IN (SELECT rowid FROM workshop_fts WHERE <clause>)
    or:  w.rowid NOT IN (SELECT rowid FROM workshop_fts WHERE <clause>)"""
    if not val or not str(val).strip():
        return ("", [])  # empty string is invalid FTS5 syntax
    if op == "contains":
        return ("workshop_fts MATCH ?", [str(val)])
    if op == "does_not_contain":
        return ("workshop_fts MATCH ?", [str(val)])
    if op == "is":
        return ('workshop_fts MATCH ?', [f'"{val}"'])
    if op == "is_not":
        return ('workshop_fts MATCH ?', [f'"{val}"'])
    if op == "is_empty":
        return ("1 = 1", [])  # all rows — handled differently in search_items
    if op == "is_not_empty":
        return ("1 = 1", [])  # all rows — handled differently in search_items
    return ("", [])


def _compute_percentile_threshold(db_path: str, db_col: str, percentile, base_filters: list[dict] = None) -> float | None:
    """Computes the threshold score for items above the given percentile.
    percentile: 0-99 (clamped). 0 returns None (no filter).
    base_filters: non-percentile filters for the base dataset."""
    try:
        percentile = int(float(percentile)) if percentile is not None else 0
    except (ValueError, TypeError):
        return None
    percentile = max(0, min(99, percentile))
    if percentile == 0:
        return None

    tile = 100 - percentile
    conn = get_connection(db_path)

    where_sql = ""
    params = []
    if base_filters:
        clauses = []
        for f in base_filters:
            logic = f.get("logic", "AND").upper()
            field = f.get("field")
            op = f.get("op")
            val = f.get("value")
            if not field or not op:
                continue
            filter_db_col = FILTER_FIELD_TO_COLUMN.get(field, field)
            if filter_db_col == "tags":
                if op in ("is", "is_not"):
                    continue
                clause, clause_params = _build_tag_clause(op, val)
            elif filter_db_col == "full_text":
                continue  # FTS5 virtual column, not a real column
            else:
                clause, clause_params = _build_single_filter_clause(filter_db_col, op, val)
            if clause:
                params.extend(clause_params)
                clauses.append((logic, clause))
        if clauses:
            where_sql = "WHERE "
            for idx, (logic, clause) in enumerate(clauses):
                where_sql += f" {logic} " if idx > 0 else ""
                where_sql += clause

    sql = f"""
        SELECT COALESCE(MIN({db_col}), 0) FROM (
            SELECT {db_col}, NTILE(100) OVER (ORDER BY {db_col} DESC) as tile
            FROM workshop_items w
            {where_sql}
        ) WHERE tile = {tile}
    """
    threshold = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return threshold if threshold else None

def _build_tag_clause(op: str, val) -> tuple[str, list]:
    """Tag queries use the junction table (workshop_tags + tags); the JSON column
    this was named for is gone at migration 5->6. Contains/does_not_contain match
    exact tag names."""
    if op == "contains":
        return ("EXISTS (SELECT 1 FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id = w.workshop_id AND t.tag_name = ?)", [val])
    if op == "does_not_contain":
        return ("NOT EXISTS (SELECT 1 FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id = w.workshop_id AND t.tag_name = ?)", [val])
    if op == "is_empty":
        return ("NOT EXISTS (SELECT 1 FROM workshop_tags WHERE workshop_id = w.workshop_id)", [])
    if op == "is_not_empty":
        return ("EXISTS (SELECT 1 FROM workshop_tags WHERE workshop_id = w.workshop_id)", [])
    return ("", [])


def build_filters_sql(filters: list[dict]) -> tuple[str, list]:
    """Translate a filter list into one SQL predicate, exactly as search does.

    This is the one filter-to-SQL builder. ``search_items`` calls it for its
    filter group, and the metrics layer calls it to express an AppID's stored
    enrichment filters as SQL, so the coverage figure and the search are the
    same translation rather than two copies that drift.

    Returns ``(clause, params)`` where ``clause`` is the filters joined by each
    one's own ``logic`` (AND/OR) and ``params`` are its bound values. The caller
    supplies the enclosing parentheses. Returns ``("", [])`` when nothing in the
    list produces a predicate, so a caller can AND it in only when it is not
    empty.

    The translation is **not** identical to the daemon's in-memory
    :func:`_evaluate_filters`, and deliberately so: a text operator searches
    each field's ``_en`` counterpart as well (:data:`_EN_COLUMN_FOR`), while the
    in-memory evaluator reads the original column alone. They can therefore
    disagree on an item whose original text does not match but whose translation
    does. Where they disagree, this is the search builder's answer.

    A ``percentile`` filter is skipped: a percentile is relative to the result
    set it is computed over and has no fixed predicate, and the in-memory
    evaluator likewise treats it as matching everything. ``full_text`` filters
    are translated through the FTS index, the same as in a search.
    """
    clauses = []
    params = []
    for f in filters:
        if not isinstance(f, dict) or f.get("op") == "percentile":
            continue
        logic = f.get("logic", "AND").upper()
        field = f.get("field")
        op = f.get("op")
        val = f.get("value")
        if not field or not op:
            continue
        db_col = FILTER_FIELD_TO_COLUMN.get(field, field)
        if db_col not in FILTER_FIELD_TO_COLUMN.values() and db_col not in ("tags", "full_text"):
            continue
        if db_col == "tags":
            if op in ("is", "is_not"):
                continue
            clause, clause_params = _build_tag_clause(op, val)
        elif db_col == "full_text":
            if op in ("is_empty", "is_not_empty"):
                clause = f"w.rowid {'IN' if op == 'is_not_empty' else 'NOT IN'} (SELECT rowid FROM workshop_fts)"
                clause_params = []
            else:
                fts_clause, fts_params = _build_fts_clause(op, val)
                if fts_clause:
                    negate = op in ("does_not_contain", "is_not")
                    clause = f"w.rowid {'NOT IN' if negate else 'IN'} (SELECT rowid FROM workshop_fts WHERE {fts_clause})"
                    clause_params = fts_params
                else:
                    clause, clause_params = "", []
        elif db_col in _EN_COLUMN_FOR and op in _TEXT_OPS:
            en_col = _EN_COLUMN_FOR[db_col]
            clause_original, params_original = _build_single_filter_clause(db_col, op, val)
            clause_translated, params_translated = _build_single_filter_clause(en_col, op, val)
            joiner = " AND " if op in _TEXT_NEG_OPS else " OR "
            if clause_original and clause_translated:
                clause = f"({clause_original}{joiner}{clause_translated})"
                clause_params = params_original + params_translated
            elif clause_original:
                clause, clause_params = clause_original, params_original
            else:
                clause, clause_params = clause_translated, params_translated
        else:
            clause, clause_params = _build_single_filter_clause(db_col, op, val)
        if clause:
            params.extend(clause_params)
            clauses.append((logic, clause))
    if not clauses:
        return "", []
    sql = ""
    for idx, (logic, clause) in enumerate(clauses):
        sql += f" {logic} " if idx > 0 else ""
        sql += clause
    return sql, params


def _ensure_tag_ids(db_path: str, tag_names: list[str]) -> list[int]:
    """Given a list of unique tag name strings, returns their tag_ids.
    Inserts any new tags into the tags table.  This is the single canonical
    path for tag creation — the migration exercises it at scale."""
    if not tag_names:
        return []
    conn = get_connection(db_path)
    conn.executemany("INSERT OR IGNORE INTO tags (tag_name) VALUES (?)", [(t,) for t in tag_names])
    conn.commit()
    placeholders = ",".join(["?"] * len(tag_names))
    rows = conn.execute(
        f"SELECT tag_id FROM tags WHERE tag_name IN ({placeholders}) ORDER BY tag_id",
        tag_names
    ).fetchall()
    conn.close()
    return [r["tag_id"] for r in rows]


def swap_tag_ids(db_path: str, id_a: int, id_b: int):
    """Atomically swaps two tag IDs in both the tags table and all
    workshop_tags associations.  Uses a temporary negative ID to avoid
    conflicts during the swap."""
    TEMP = -99999
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE workshop_tags SET tag_id = ? WHERE tag_id = ?", (TEMP, id_a))
        conn.execute("UPDATE tags SET tag_id = ? WHERE tag_id = ?", (TEMP, id_a))
        conn.execute("UPDATE workshop_tags SET tag_id = ? WHERE tag_id = ?", (id_a, id_b))
        conn.execute("UPDATE tags SET tag_id = ? WHERE tag_id = ?", (id_a, id_b))
        conn.execute("UPDATE workshop_tags SET tag_id = ? WHERE tag_id = ?", (id_b, TEMP))
        conn.execute("UPDATE tags SET tag_id = ? WHERE tag_id = ?", (id_b, TEMP))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def compact_tag_ids(db_path: str, tag_counts: dict = None):
    """Reorders tag IDs so the 127 most frequently used tags occupy IDs
    1-127 (the 1-byte SQLite varint range).  Uses the same frequency data
    as the stats screen.  If tag_counts is None, fetches frequencies from
    the database.  Logs each swap."""
    PIVOT = 127
    conn = get_connection(db_path)
    if tag_counts is None:
        tag_counts = _compute_tag_frequencies(conn.cursor())
    if len(tag_counts) <= PIVOT:
        conn.close()
        return

    current_map = {r["tag_name"]: r["tag_id"] for r in conn.execute("SELECT tag_name, tag_id FROM tags").fetchall()}

    # The PIVOT most common tags (by frequency) should be in the low-ID range.
    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    top_names = {name for name, _ in sorted_tags[:PIVOT]}

    # Tags that SHOULD be in the low range but currently aren't
    misplaced = [(n, current_map[n]) for n in top_names if current_map.get(n, 9999) > PIVOT]
    # Tags currently in the low range that DON'T belong there (available slots)
    low_slot_tags = [(n, current_map[n]) for n, cid in current_map.items()
                 if cid <= PIVOT and n not in top_names]

    swaps = 0
    for (hi_name, hi_id), (lo_name, lo_id) in zip(misplaced, low_slot_tags):
        swap_tag_ids(db_path, hi_id, lo_id)
        current_map[hi_name] = lo_id
        current_map[lo_name] = hi_id
        swaps += 1
        logging.debug(f"  compact_tag_ids: swapped '{hi_name}' (id {hi_id}) ↔ '{lo_name}' (id {lo_id})")
    conn.close()
    if swaps:
        logging.info(f"compact_tag_ids: {swaps} tag IDs reordered for space efficiency")

def _evaluate_subscribed_filter(item: dict, op: str, val) -> bool:
    """The in-memory half of the Subscribed field, from the same value table.

    Negation is applied here rather than spelled out per value, so
    ``_build_subscribed_clause``'s SQL and this predicate cannot disagree about
    what the complement of a value is.
    """
    if op not in ("is", "is_not"):
        return True
    spec = SUBSCRIBED_VALUE_SPECS.get(normalise_subscribed_value(val))
    matched = spec["matches"](item) if spec is not None else False
    return not matched if op == "is_not" else matched


def _evaluate_single_filter(item: dict, db_col: str, op: str, val) -> bool:
    """Checks whether an in-memory item dict matches a single filter criterion."""
    if db_col == SUBSCRIBED_VIRTUAL_COLUMN:
        return _evaluate_subscribed_filter(item, op, val)
    is_tags = db_col == "tags"
    if is_tags:
        return _evaluate_tag_filter(item, op, val)

    item_val = item.get(db_col)
    numeric_cols = {"file_size", "subscriptions", "favorited", "views"}

    if db_col in numeric_cols and op not in ("is_empty", "is_not_empty", "contains", "does_not_contain"):
        try:
            item_val = int(item_val or 0)
            val = int(val)
        except (ValueError, TypeError):
            return True

    if op == "contains":
        if item_val is None:
            return False
        return str(val).lower() in str(item_val).lower()
    if op == "does_not_contain":
        if item_val is None:
            return True
        return str(val).lower() not in str(item_val).lower()
    if op == "is":
        return str(item_val) == str(val)
    if op == "is_not":
        return str(item_val) != str(val)
    if op == "gt":
        return item_val > val
    if op == "lt":
        return item_val < val
    if op == "gte":
        return item_val >= val
    if op == "lte":
        return item_val <= val
    if op == "is_empty":
        return item_val is None or str(item_val).strip() == ""
    if op == "is_not_empty":
        return item_val is not None and str(item_val).strip() != ""
    if op == "percentile":
        return True
    return True

def _evaluate_tag_filter(item: dict, op: str, val) -> bool:
    """Evaluates a filter against the item's tags field (JSON array)."""
    tags_raw = item.get("tags") or "[]"
    tag_set = set()
    try:
        tags_list = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
        if isinstance(tags_list, list):
            tag_set = {str(t) for t in tags_list}
    except Exception:
        logging.debug("Failed to parse tags in filter evaluation")
        pass

    if op == "contains":
        return val in tag_set
    if op == "does_not_contain":
        return val not in tag_set
    if op == "is_empty":
        return len(tag_set) == 0
    if op == "is_not_empty":
        return len(tag_set) > 0
    return True

def get_enrichment_filters(tracking: dict) -> list[dict] | None:
    """The enrichment filters stored for an AppID, or ``None`` when unreadable.

    One reader, because two things now have to agree: the daemon's per-item
    enrichment decision, and the migration that moves the queue priority of items
    the filters exclude. Two readers would be two answers.

    ``None`` means "cannot tell", which a caller must read as *enrich
    everything*: a malformed filter list must not silently stop all enrichment,
    and reading it as "excludes everything" would be the far more expensive
    mistake. An empty list means what it says -- no filters, so everything
    matches -- and the legacy `filter_text` / `required_tags` / `excluded_tags`
    columns are the fallback for rows written before `enrichment_filters`
    existed.
    """
    raw = tracking.get("enrichment_filters") or "[]"
    try:
        filters = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(filters, list):
        # `true`, `{}`, a bare string: valid JSON, but not a filter list.
        return None
    if filters:
        return filters

    filter_text = (tracking.get("filter_text") or "").strip()
    try:
        required_tags = json.loads(tracking.get("required_tags") or "[]")
        excluded_tags = json.loads(tracking.get("excluded_tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        # A hand-written legacy column can be malformed. Treat the set as
        # unreadable rather than raising: this runs inside a migration as well
        # as on the fetch path, and an exception there would abort the upgrade.
        return None
    if not isinstance(required_tags, list) or not isinstance(excluded_tags, list):
        return None

    filters = []
    if filter_text:
        filters.append({"field": "Title", "op": "contains", "value": filter_text})
    for tag in required_tags:
        filters.append({"field": "Tags", "op": "contains", "value": tag})
    for tag in excluded_tags:
        filters.append({"field": "Tags", "op": "does_not_contain", "value": tag})
    return filters


def _evaluate_filters(item: dict, filters: list[dict]) -> bool:
    """True if an in-memory item dict matches all specified filters.
    Filters use the same format as get_filters() in the TUI search builder."""
    if not filters:
        return True
    for f in filters:
        field = f.get("field")
        op = f.get("op")
        val = f.get("value")
        if not field or not op:
            continue
        db_col = FILTER_FIELD_TO_COLUMN.get(field, field)
        if not _evaluate_single_filter(item, db_col, op, val):
            return False
    return True

def _build_sort_clause(sort_by: str, sort_order: str) -> str:
    """Returns an ORDER BY clause with whitelist validation."""
    if sort_by not in VALID_SORT_COLS:
        return ""
    order = "DESC" if sort_order.upper() == "DESC" else "ASC"
    return f" ORDER BY w.{sort_by} {order}"

def _build_limit_offset(limit: int, offset: int) -> tuple[str, list]:
    """Returns (LIMIT...OFFSET clause, params list)."""
    params = []
    if limit is not None:
        params.append(limit)
        if offset is not None:
            return f" LIMIT ? OFFSET ?", params + [offset]
        return f" LIMIT ?", params
    return "", []

# Historical column names a Batch 6 migration has renamed. ``_safe_add_columns``
# runs on every startup through ``_create_legacy_schema``, so once a database is
# at the current version it must not resurrect the old name beside the new one.
# The mapping is what keeps ``_create_legacy_schema``'s historical column list
# byte-identical: a fresh chain database needs the old name (the migrations it is
# about to replay name it), while a current database must keep only the new one.
_RENAMED_COLUMN_NAMES = {
    "needs_web_scrape": "web_scrape_priority",
    "needs_image": "image_priority",
    "image_extension": "image_answer",
    "downloaded_at": "steam_download_seen_at",
}


def _safe_add_columns(cursor, table: str, columns: list[tuple[str, str]]):
    """Safely adds columns to an existing table, ignoring duplicate-column errors.

    A historical name whose renamed current form is already present is skipped:
    ``_create_legacy_schema`` runs on every startup, so without this a database
    already at the current version would gain a stray legacy column next to the
    renamed one.
    """
    existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
    for col_name, col_type in columns:
        renamed_to = _RENAMED_COLUMN_NAMES.get(col_name)
        if renamed_to is not None and renamed_to in existing:
            continue
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise

HEX_CHARS = "0123456789abcdef"

def get_image_subdirs(workshop_id) -> tuple[str, str, str]:
    """
    Returns the three levels of subdirectory names for a given workshop_id using
    a 3-level 12-bit hexadecimal hashing scheme (1 char / 4 bits per level).
    """
    try:
        wid = int(workshop_id)
    except (ValueError, TypeError):
        return "0", "0", "0"
    
    bucket1 = HEX_CHARS[wid & 0xF]
    bucket2 = HEX_CHARS[(wid >> 4) & 0xF]
    bucket3 = HEX_CHARS[(wid >> 8) & 0xF]
    return bucket1, bucket2, bucket3

def get_image_path(base_dir: str, workshop_id, ext: str) -> str:
    """
    Returns the full nested path to an image file.
    """
    bucket1, bucket2, bucket3 = get_image_subdirs(workshop_id)
    return os.path.join(base_dir, bucket1, bucket2, bucket3, f"{workshop_id}.{ext}")

def _current_table_name(cursor, new_name: str, old_name: str) -> str:
    """Resolve a table's current name across the Batch 6 rename.

    ``_create_legacy_schema`` runs on every startup, before the versioned migrations,
    so it sees a database on both sides of migration 29->30. It has to keep
    building a *fresh* database with the historical name -- the chain it is
    about to replay names these tables at earlier versions (13->14, 22->23,
    27->28) -- and it must not resurrect that name once the table has been
    renamed. Prefer the new name when it exists, then the old one, and default
    to the old name so a brand-new file gets the shape the chain expects.
    """
    names = {row[0] for row in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    if new_name in names:
        return new_name
    if old_name in names:
        return old_name
    return old_name


def _demote_filtered_out_queue_priorities(conn) -> tuple[int, int]:
    """Move filter-excluded items back to backlog priority. Returns (web, image).

    An item that fails its AppID's enrichment filters is still scraped -- the
    filters choose priority, not membership -- but it must not *outrank* an item
    they did select, and between May and September 2026 it did: the daemon
    inherited its whole pre-fetch `api_priority` into the dependent queues, and a
    newly discovered item carries `3`, so every new item the filters excluded was
    queued in the same band as the ones they chose. *Measured live* on
    2026-09-17, 868,759 items sat above backlog; 760,782 web entries and 668,269
    image ones of those belonged to excluded items, with 107,365 selected items
    waiting behind them.

    The predicate is :func:`_evaluate_filters`, the same one the fetch path uses,
    rather than an SQL translation of the filters: the two evaluators differ
    already (the SQL search also searches each field's `_en` counterpart), and a
    migration that disagreed with the runtime would leave the queue in a state
    the runtime immediately contradicts.

    Only the daemon's own priorities are demoted, and only downwards: a value of
    2 or 3 becomes 1, while `0` (not queued), `1`, and anything at or above
    :data:`USER_PRIORITY_FLOOR` are left exactly as they are. A 5 or a 10 in these
    columns is a person looking at the item, and under both the old rule and the
    new one nothing else could have put it there -- so the migration undoes the
    daemon's bookkeeping, it does not overrule a user. Every AppID with a readable
    filter set is walked; an AppID that is not a configured target has no
    enrichment path of its own, so the daemon would treat its items as enriched
    and re-stamp them on the next fetch -- a no-op there rather than a wrong
    answer.
    """
    cursor = conn.cursor()

    # Resolved, not hard-coded: this helper runs inside migration 21->22, when
    # the table still carries its historical name, and is also called directly
    # by a test against a current-schema database, where it does not.
    table = _current_table_name(cursor, "app_discovery", "app_tracking")
    rules = []
    for row in cursor.execute(f"SELECT * FROM {table}"):
        tracking = dict(row)
        filters = get_enrichment_filters(tracking)
        if tracking.get("appid") is not None and filters:
            rules.append((tracking["appid"], filters))
    if not rules:
        return 0, 0

    columns = {row[1] for row in cursor.execute("PRAGMA table_info(workshop_items)")}
    # Same resolution as the table above: this helper runs inside migration
    # 21->22, when the two priority columns still carry their historical names,
    # and is also called directly by tests against a current-schema database,
    # where they do not.
    web_col = ("web_scrape_priority" if "web_scrape_priority" in columns
               else "needs_web_scrape")
    image_col = ("image_priority" if "image_priority" in columns
                 else "needs_image")
    web_demoted = 0
    image_demoted = 0

    for appid, filters in rules:
        # Only the columns these filters actually read, plus the two priorities:
        # the candidate set is large and the rows carry long text.
        referenced = {FILTER_FIELD_TO_COLUMN.get(f.get("field"), f.get("field"))
                      for f in filters if isinstance(f, dict) and f.get("field")}
        referenced.discard("tags")
        # The Subscribed field's virtual column spans four real ones. The row
        # must carry every one the chosen value reads, or _evaluate_single_filter
        # would read missing keys and answer from their NULLs -- `never`, a false
        # `queued`, a false `downloaded` -- instead of the stored state.
        if SUBSCRIBED_VIRTUAL_COLUMN in referenced:
            referenced.discard(SUBSCRIBED_VIRTUAL_COLUMN)
            referenced.update(SUBSCRIBED_FILTER_COLUMNS)
        selected = sorted(c for c in referenced
                          if c and c.isidentifier() and c in columns)
        fields = ", ".join(["workshop_id", web_col, image_col] + selected)

        rows = cursor.execute(
            "SELECT %s FROM workshop_items WHERE consumer_appid = ? "
            "AND (%s > 1 OR %s > 1)" % (fields, web_col, image_col),
            (appid,),
        ).fetchall()
        if not rows:
            continue
        logging.info(
            "Migration 21->22: checking %d item(s) above backlog priority for appid %s...",
            len(rows), appid,
        )

        # In batches, so a large library does not need its tags held all at once.
        for start in range(0, len(rows), 900):
            batch = rows[start:start + 900]
            ids = [r["workshop_id"] for r in batch]
            tags: dict[int, list[str]] = {}
            for tag_row in cursor.execute(
                    "SELECT wt.workshop_id AS wid, t.tag_name AS name "
                    "FROM workshop_tags wt JOIN tags t ON t.tag_id = wt.tag_id "
                    "WHERE wt.workshop_id IN (%s)" % ",".join("?" * len(ids)), ids):
                tags.setdefault(tag_row["wid"], []).append(tag_row["name"])

            web_ids = []
            image_ids = []
            for row in batch:
                item = dict(row)
                item["tags"] = tags.get(row["workshop_id"], [])
                if _evaluate_filters(item, filters):
                    continue
                if 2 <= row[web_col] < USER_PRIORITY_FLOOR:
                    web_ids.append(row["workshop_id"])
                if 2 <= row[image_col] < USER_PRIORITY_FLOOR:
                    image_ids.append(row["workshop_id"])

            for column, column_ids in ((web_col, web_ids),
                                       (image_col, image_ids)):
                if column_ids:
                    cursor.execute(
                        "UPDATE workshop_items SET %s = 1 WHERE workshop_id IN (%s)"
                        % (column, ",".join("?" * len(column_ids))), column_ids)
            web_demoted += len(web_ids)
            image_demoted += len(image_ids)

    if web_demoted or image_demoted:
        conn.commit()
    return web_demoted, image_demoted


def _create_legacy_schema(cursor, conn):
    """Create every table and baseline column that predates v1.

    This is the unversioned part of the schema: the tables and columns that
    a fresh database starts with, plus the best-effort ``_safe_add_columns``
    calls and legacy data conversions that every history shares. It runs on
    every call, before the versioned migrations in ``MIGRATIONS``.
    """
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS workshop_items (
        workshop_id INTEGER PRIMARY KEY,
        -- NOTE: the timestamp columns below deliberately keep their HISTORICAL
        -- names here. A brand-new database starts at user_version = 0 and runs
        -- the entire migration chain, and migrations 6->7, 7->8, 10->11 and
        -- 11->12 all read these old names (dt_found, dt_updated, dt_attempted,
        -- dt_translated, time_created, time_updated) before migration 13->14
        -- renames them. Renaming them here would break fresh databases, exactly
        -- like removing the legacy `tags` column below would. After the chain
        -- runs, a fresh database has the same final columns as a migrated one.
        dt_found INTEGER,
        dt_updated INTEGER,
        dt_attempted INTEGER,
        -- Our clock: set on every API fetch attempt, success or failure. Added in
        -- migration 13->14. api_fetched_at only moves on success, so this is the
        -- only record of when we last tried; get_db_stats reports fetch recency
        -- from it.
        last_fetch_attempted_at INTEGER,
        status INTEGER,
        title TEXT,
        creator INTEGER, -- FK to creators.steamid (LEFT JOIN used; FK omitted
                        --   because items are discovered before creators are fetched)
        creator_appid INTEGER,
        consumer_appid INTEGER,
        filename TEXT,
        file_size INTEGER,
        preview_url TEXT,
        hcontent_file TEXT,
        hcontent_preview TEXT,
        short_description TEXT,
        time_created INTEGER,
        time_updated INTEGER,
        visibility INTEGER,
        banned INTEGER,
        ban_reason TEXT,
        app_name TEXT,
        file_type INTEGER,
        subscriptions INTEGER,
        favorited INTEGER,
        views INTEGER,
        -- Legacy JSON tags column. Deliberately present here even though tags now
        -- live in the workshop_tags junction table: migrations 1->2 and 5->6 both
        -- read it, and on a fresh database user_version starts at 0 so the whole
        -- migration chain runs. Migration 5->6 drops it at the end of the chain.
        -- Removing it from this statement breaks fresh databases.
        tags TEXT,
        extended_description TEXT,
        lifetime_subscriptions INTEGER,
        lifetime_favorited INTEGER,
        title_en TEXT,
        short_description_en TEXT,
        extended_description_en TEXT,
        dt_translated INTEGER,
        translation_priority INTEGER DEFAULT 0,
        wilson_favorite_score REAL DEFAULT NULL,
        wilson_subscription_score REAL DEFAULT NULL,
        needs_web_scrape INTEGER DEFAULT 0,
        image_extension TEXT DEFAULT NULL,
        needs_image INTEGER DEFAULT 0,
        api_priority INTEGER NOT NULL DEFAULT 3,
        -- The owner's (the account whose key and cookies are configured)
        -- subscription relationship to this item. own_subscribed is reconciled
        -- from Steam; own_first_subscribed_at is sticky and is the only source
        -- of the "we have seen this subscribed" state. Neither is the item-wide
        -- lifetime_subscriptions count above, which cannot be attributed to an
        -- account.
        own_subscribed INTEGER DEFAULT 0,
        own_first_subscribed_at INTEGER DEFAULT NULL,
        -- Local latch: when this app first saw Steam's downloaded copy of a
        -- subscribed item on disk. Set only by src.workshop_folders, cleared
        -- only beside own_subscribed when the item leaves the owner's
        -- subscription list, and never derived from Steam data. NULL means the
        -- subscription (if any) has not been confirmed on disk.
        downloaded_at INTEGER DEFAULT NULL,
        -- Our clock: when the web worker last scraped this item's page
        -- successfully (v27). Not scrape_version, which records Steam's
        -- revision; these are the completion times throughput and freshness
        -- are measured from. NULL means no completion has been recorded since
        -- the column existed -- historical rows are deliberately not
        -- backfilled, so a rate is not reported for them.
        web_scraped_at INTEGER DEFAULT NULL,
        -- Our clock: when the image worker last downloaded this item's preview
        -- successfully (v27).
        image_fetched_at INTEGER DEFAULT NULL,
        -- Our clock: when the translator finished the last queued field for
        -- this item (v27). The creators table's translated_at has the same
        -- meaning; this is the item-row equivalent.
        translated_at INTEGER DEFAULT NULL
    )
    """)

    # The creator table. Its name is resolved rather than hard-coded because
    # this runs on both sides of migration 29->30: a fresh database must start
    # with `users` so the chain's earlier steps (6->7's timestamp conversion,
    # 13->14's column renames and 27->28's translation mirror repair) still
    # find it, and an already-renamed one must not get an empty `users`
    # resurrected beside `creators`.
    _creators_table = _current_table_name(cursor, "creators", "users")
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {_creators_table} (
        steamid INTEGER PRIMARY KEY,
        personaname TEXT,
        personaname_en TEXT,
        dt_updated INTEGER,
        dt_translated INTEGER,
        translation_priority INTEGER DEFAULT 0
    )
    """)

    # Create translation queue table for batched per-field translation
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS translation_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_type TEXT NOT NULL,
        item_id INTEGER NOT NULL,
        field TEXT NOT NULL,
        original_text TEXT NOT NULL,
        priority INTEGER DEFAULT 0,
        dt_queued INTEGER
    )
    """)

    # The per-field lookup (`queue_field_for_translation`) and migration 22->23's
    # stranded-mirror repair both predicate on this table, and 22->23 runs *inside*
    # the MIGRATIONS loop below while `_ensure_indexes` runs after it -- so this
    # index cannot live there: on the upgrade from a pre-v23 backup the repair
    # would still scan. It is created here, in the unversioned schema every caller
    # runs before the loop, which also means an existing database picks it up on
    # the next startup with no version bump and no new migration step.
    # `IF NOT EXISTS` matches `_ensure_indexes`' idempotence. `item_type` and
    # `item_id` have carried these names since the table was created (migration
    # 6->7 only converts the `dt_queued` timestamp), so this is safe at every
    # history, including one old enough to run the whole chain. The name avoids
    # the column names deliberately: batch 6 renames these columns, and SQLite
    # rewrites an index *definition* on RENAME COLUMN but keeps its *name*, so
    # `idx_translation_queue_item` would outlive its columns.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_translation_queue_lookup "
        "ON translation_queue (item_type, item_id, field)"
    )

    # Safe migrations for existing databases
    _safe_add_columns(cursor, "workshop_items", [
        ("lifetime_subscriptions", "INTEGER"),
        ("lifetime_favorited", "INTEGER"),
        ("title_en", "TEXT"),
        ("short_description_en", "TEXT"),
        ("extended_description_en", "TEXT"),
        ("translation_priority", "INTEGER DEFAULT 0"),
        ("is_queued_for_subscription", "INTEGER DEFAULT 0"),
        ("wilson_favorite_score", "REAL DEFAULT NULL"),
        ("wilson_subscription_score", "REAL DEFAULT NULL"),
        ("needs_web_scrape", "INTEGER DEFAULT 0"),
        ("image_extension", "TEXT DEFAULT NULL"),
        ("needs_image", "INTEGER DEFAULT 0"),
        ("own_subscribed", "INTEGER DEFAULT 0"),
        ("own_first_subscribed_at", "INTEGER DEFAULT NULL"),
        ("downloaded_at", "INTEGER DEFAULT NULL"),
        ("web_scraped_at", "INTEGER DEFAULT NULL"),
        ("image_fetched_at", "INTEGER DEFAULT NULL"),
        ("translated_at", "INTEGER DEFAULT NULL"),
    ])

    # dt_translated was renamed to translate_version in migration 13->14. A
    # pre-v14 database still needs the old column to exist before migration 6->7
    # converts it from TEXT to INTEGER, but an already-renamed database must not
    # get it added back (that would be a stray duplicate of translate_version).
    _item_cols_now = {r[1] for r in cursor.execute("PRAGMA table_info(workshop_items)").fetchall()}
    if "dt_translated" not in _item_cols_now and "translate_version" not in _item_cols_now:
        cursor.execute("ALTER TABLE workshop_items ADD COLUMN dt_translated TEXT")

    # The AppID discovery table. Resolved for the same reason as the creator
    # table above: fresh databases must build it as `app_tracking` for
    # migration 21->22, and a renamed database must not gain an empty
    # `app_tracking` beside `app_discovery`.
    _app_discovery_table = _current_table_name(cursor, "app_discovery", "app_tracking")
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {_app_discovery_table} (
        appid INTEGER PRIMARY KEY,
        last_historical_date_scanned INTEGER,
        filter_text TEXT DEFAULT '',
        required_tags TEXT DEFAULT '[]',
        excluded_tags TEXT DEFAULT '[]',
        window_size INTEGER DEFAULT 2592000,
        enrichment_filters TEXT DEFAULT '[]',
        last_cursor TEXT DEFAULT ''
    )
    """)

    # Safe migrations for existing databases to add new filter columns
    _safe_add_columns(cursor, _app_discovery_table, [
        ("filter_text", "TEXT DEFAULT ''"),
        ("required_tags", "TEXT DEFAULT '[]'"),
        ("excluded_tags", "TEXT DEFAULT '[]'"),
        ("window_size", "INTEGER DEFAULT 2592000"),
        ("enrichment_filters", "TEXT DEFAULT '[]'"),
        ("last_cursor", "TEXT DEFAULT ''"),
    ])

    # Data Migration: Populate the discovery table from existing workshop_items
    # if empty, and drop the obsolete app_state table.
    cursor.execute(f"SELECT COUNT(*) FROM {_app_discovery_table}")
    if cursor.fetchone()[0] == 0:
        # The Steam-side "last updated" column is named time_updated before
        # migration 13->14 and steam_updated_at after it. This block runs before
        # the migrations, so on an already-migrated database it must use the new
        # name (otherwise re-initializing a v14 database with an empty
        # discovery table would reference a column that no longer exists).
        _cols_now = {r[1] for r in cursor.execute("PRAGMA table_info(workshop_items)").fetchall()}
        _steam_updated = "time_updated" if "time_updated" in _cols_now else "steam_updated_at"
        cursor.execute(f"""
            INSERT INTO {_app_discovery_table} (appid, last_historical_date_scanned)
            SELECT consumer_appid, MAX({_steam_updated})
            FROM workshop_items
            WHERE consumer_appid IS NOT NULL AND {_steam_updated} IS NOT NULL
            GROUP BY consumer_appid
        """)
    
    cursor.execute("DROP TABLE IF EXISTS app_state")

    # Legacy filter migration: convert old filter columns to unified enrichment_filters JSON
    cursor.execute(f"""
        SELECT appid, filter_text, required_tags, excluded_tags, enrichment_filters
        FROM {_app_discovery_table}
        WHERE (enrichment_filters IS NULL OR enrichment_filters = '' OR enrichment_filters = '[]')
    """)
    for row in cursor.fetchall():
        filter_text = (row["filter_text"] or "").strip()
        try:
            required_tags = json.loads(row["required_tags"] or "[]")
        except Exception: required_tags = []
        try:
            excluded_tags = json.loads(row["excluded_tags"] or "[]")
        except Exception: excluded_tags = []
        if not filter_text and not required_tags and not excluded_tags:
            continue
        filters = []
        if filter_text:
            filters.append({"field": "Title", "op": "contains", "value": filter_text})
        for tag in required_tags:
            filters.append({"field": "Tags", "op": "contains", "value": tag})
        for tag in excluded_tags:
            filters.append({"field": "Tags", "op": "does_not_contain", "value": tag})
        cursor.execute(
            f"UPDATE {_app_discovery_table} SET enrichment_filters = ? WHERE appid = ?",
            (json.dumps(filters), row["appid"])
        )
        conn.commit()

def _create_current_schema(cursor, conn):
    """Create a brand-new database directly at :data:`EXPECTED_VERSION`.

    The statements below are the *terminal* shape the migration chain leaves
    behind, dumped verbatim from ``sqlite_master`` of a database the chain
    itself produced at ``user_version = 33`` -- no definition here was written
    by reading the migrations. The index SQL in particular is the exact text
    SQLite stores, so the fresh database's ``sqlite_master`` matches what the
    chain leaves, including the early indexes whose definitions a ``RENAME
    COLUMN`` rewrote (``idx_time_created``, ``idx_time_updated``; those two are
    owned by :func:`_ensure_indexes` and are not repeated below).

    A fresh database takes this path by default, so it never replays the
    thirty-three migrations. An existing database always takes the legacy path,
    because only the chain can carry it forward. The two endpoints must be
    identical. ``_ensure_indexes`` still runs after this function, exactly as
    it does after the chain, so the query indexes it owns are deliberately not
    repeated here; the indexes a *migration* owns are created below, because
    no migration runs on this path.

    **Forward rule:** when a migration changes the schema, mirror it here as
    well -- update the definition below and bump :data:`EXPECTED_VERSION` --
    so both paths still end at the same shape.
    :func:`tests.test_fresh_schema_path.test_schema_equivalence` builds one
    database each way and fails the moment they diverge, which is what makes
    this mechanical rather than a habit to remember.
    """
    # ── Tables ────────────────────────────────────────────────────────────
    # The current column names, the current types and the current defaults.
    # The legacy path reaches this same shape by renaming dt_found ->
    # first_seen_at, dt_updated -> api_fetched_at and so on in migration
    # 13->14, by dropping the legacy `tags` column in 5->6 and by ALTER-adding
    # is_queued_for_subscription later.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS workshop_items (
        workshop_id INTEGER PRIMARY KEY,
        first_seen_at INTEGER,
        api_fetched_at INTEGER,
        scrape_version INTEGER,
        last_fetch_attempted_at INTEGER,
        fetch_status INTEGER,
        title TEXT,
        creator_steamid INTEGER,
        creator_appid INTEGER,
        consumer_appid INTEGER,
        filename TEXT,
        file_size INTEGER,
        preview_url TEXT,
        hcontent_file TEXT,
        hcontent_preview TEXT,
        short_description TEXT,
        steam_created_at INTEGER,
        steam_updated_at INTEGER,
        visibility INTEGER,
        banned INTEGER,
        ban_reason TEXT,
        app_name TEXT,
        file_type INTEGER,
        subscriptions INTEGER,
        favorited INTEGER,
        views INTEGER,
        extended_description TEXT,
        lifetime_subscriptions INTEGER,
        lifetime_favorited INTEGER,
        title_en TEXT,
        short_description_en TEXT,
        extended_description_en TEXT,
        translate_version INTEGER,
        translation_priority INTEGER DEFAULT 0,
        wilson_favorite_score REAL DEFAULT NULL,
        wilson_subscription_score REAL DEFAULT NULL,
        web_scrape_priority INTEGER DEFAULT 0,
        image_answer TEXT DEFAULT NULL,
        image_priority INTEGER DEFAULT 0,
        api_priority INTEGER NOT NULL DEFAULT 3,
        own_subscribed INTEGER DEFAULT 0,
        own_first_subscribed_at INTEGER DEFAULT NULL,
        steam_download_seen_at INTEGER DEFAULT NULL,
        web_scraped_at INTEGER DEFAULT NULL,
        image_fetched_at INTEGER DEFAULT NULL,
        translated_at INTEGER DEFAULT NULL,
        is_queued_for_subscription INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS creators (
        steamid INTEGER PRIMARY KEY,
        personaname TEXT,
        personaname_en TEXT,
        api_fetched_at INTEGER,
        translated_at INTEGER,
        translation_priority INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS translation_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_type TEXT NOT NULL,
        item_id INTEGER NOT NULL,
        field TEXT NOT NULL,
        original_text TEXT NOT NULL,
        priority INTEGER DEFAULT 0,
        queued_at INTEGER
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_discovery (
        appid INTEGER PRIMARY KEY,
        last_historical_date_scanned INTEGER,
        filter_text TEXT DEFAULT '',
        required_tags TEXT DEFAULT '[]',
        excluded_tags TEXT DEFAULT '[]',
        window_size INTEGER DEFAULT 2592000,
        enrichment_filters TEXT DEFAULT '[]',
        last_cursor TEXT DEFAULT ''
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS tags (
        tag_id   INTEGER PRIMARY KEY,
        tag_name TEXT UNIQUE NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS workshop_tags (
        workshop_id INTEGER NOT NULL,
        tag_id      INTEGER NOT NULL,
        PRIMARY KEY (workshop_id, tag_id)
    ) WITHOUT ROWID
    """)

    # ── Full-text search ─────────────────────────────────────────────────
    # Content-sync FTS5 over the six translated/searchable columns. The
    # shadow tables and the sync triggers are created here too: a fresh
    # database never runs migrations 4->5 and 14->15, which created them on
    # the chain path.
    cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS workshop_fts USING fts5(
        title, title_en,
        short_description, short_description_en,
        extended_description, extended_description_en,
        content='workshop_items', content_rowid='workshop_id'
    )
    """)
    # The trigger bodies below keep migration 14->15's exact whitespace: the
    # equivalence test compares the stored ``sqlite_master.sql``, so a
    # reindented body would read as a divergence.
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS workshop_items_fts_insert AFTER INSERT ON workshop_items BEGIN
            INSERT INTO workshop_fts(rowid, title, title_en, short_description, short_description_en, extended_description, extended_description_en)
            VALUES (new.workshop_id, new.title, new.title_en, new.short_description, new.short_description_en, new.extended_description, new.extended_description_en);
        END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS workshop_items_fts_delete AFTER DELETE ON workshop_items BEGIN
            INSERT INTO workshop_fts(workshop_fts, rowid, title, title_en, short_description, short_description_en, extended_description, extended_description_en)
            VALUES ('delete', old.workshop_id, old.title, old.title_en, old.short_description, old.short_description_en, old.extended_description, old.extended_description_en);
        END
    """)
    cursor.execute("""
        CREATE TRIGGER IF NOT EXISTS workshop_items_fts_update
        AFTER UPDATE OF title, title_en, short_description, short_description_en, extended_description, extended_description_en ON workshop_items BEGIN
            INSERT INTO workshop_fts(workshop_fts, rowid, title, title_en, short_description, short_description_en, extended_description, extended_description_en)
            VALUES ('delete', old.workshop_id, old.title, old.title_en, old.short_description, old.short_description_en, old.extended_description, old.extended_description_en);
            INSERT INTO workshop_fts(rowid, title, title_en, short_description, short_description_en, extended_description, extended_description_en)
            VALUES (new.workshop_id, new.title, new.title_en, new.short_description, new.short_description_en, new.extended_description, new.extended_description_en);
        END
    """)

    # ── Indexes a migration owns ─────────────────────────────────────────
    # These are created by migrations 4->5, 5->6, 24->25 and 26->27 on the
    # legacy path, so they must be created here or the fresh path would have
    # no copy of them. The query indexes `_ensure_indexes` owns are left to
    # it, since it runs after this function on both paths.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_title_en ON workshop_items (title_en)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_short_description_en ON workshop_items (short_description_en)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_extended_description_en ON workshop_items (extended_description_en)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_filename ON workshop_items (filename)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workshop_tags_tag_id ON workshop_tags (tag_id)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_scrape_queue "
        "ON workshop_items (web_scrape_priority DESC, api_fetched_at ASC) "
        "WHERE web_scrape_priority > 0"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_queue "
        "ON workshop_items (image_priority DESC, api_fetched_at ASC) "
        "WHERE image_priority > 0"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_queue "
        "ON workshop_items (api_priority DESC, api_fetched_at ASC) "
        "WHERE api_priority > 0"
    )
    for _column in ("web_scraped_at", "image_fetched_at", "translated_at"):
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_column} "
            f"ON workshop_items ({_column}) WHERE {_column} IS NOT NULL"
        )

    # Migration 22->23's repair runs *inside* the migration loop and needs
    # this index before `_ensure_indexes` runs, which is why the legacy builder
    # creates it too rather than leaving it to `_ensure_indexes`.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_translation_queue_lookup "
        "ON translation_queue (item_type, item_id, field)"
    )

    conn.commit()
    cursor.execute(f"PRAGMA user_version = {EXPECTED_VERSION}")

def _migration_0_to_1(cursor, conn, db_path):
    logging.info("Running migration 0→1: recalculating Wilson subscriber scores...")
    import math
    # Migration 0→1: recalculate Wilson subscriber score with correct formula
    # (subscriptions/lifetime_subscriptions instead of lifetime_subscriptions/views)
    cursor.execute("""
        SELECT workshop_id, favorited, subscriptions, lifetime_subscriptions, views
        FROM workshop_items
    """)
    for row in cursor.fetchall():
        def wl(s, v):
            if v == 0:
                return 0.0
            p = min(float(s) / v, 1.0)
            z2 = 1.96 * 1.96
            d = 1 + z2 / v
            n = p + z2 / (2*v) - 1.96 * math.sqrt(max(0.0, p*(1-p)/v) + z2/(4*v*v))
            return max(0.0, min(1.0, n / d))
        fav_score = wl(row["favorited"] or 0, row["views"] or 0)
        sub_score = wl(row["subscriptions"] or 0, row["lifetime_subscriptions"] or 0)
        cursor.execute(
            "UPDATE workshop_items SET wilson_favorite_score = ?, wilson_subscription_score = ? WHERE workshop_id = ?",
            (fav_score, sub_score, row["workshop_id"])
        )
    conn.commit()
    cursor.execute("PRAGMA user_version = 1")
    logging.info("Migration 0→1 complete.")

def _migration_1_to_2(cursor, conn, db_path):
    logging.info("Running migration 1→2: normalizing malformed JSON tags...")
    cursor.execute("""
        SELECT workshop_id, tags FROM workshop_items
        WHERE tags IS NOT NULL AND tags != '' AND tags != '[]'
    """)
    fixed = 0
    for row in cursor.fetchall():
        try:
            json.loads(row["tags"])
        except (json.JSONDecodeError, TypeError):
            cursor.execute(
                "UPDATE workshop_items SET tags = ? WHERE workshop_id = ?",
                (normalize_tags(row["tags"]), row["workshop_id"])
            )
            fixed += 1
    if fixed:
        conn.commit()
    cursor.execute("PRAGMA user_version = 2")
    logging.info(f"Migration 1→2 complete. Fixed {fixed} malformed tag entries.")

def _migration_2_to_3(cursor, conn, db_path):
    logging.info("Running migration 2→3: adding web scrape flag and translation queue...")
    # Set needs_web_scrape=1 for items missing extended descriptions
    cursor.execute("""
        UPDATE workshop_items SET needs_web_scrape = 1
        WHERE extended_description IS NULL AND status IN (200, 206)
    """)
    updated = cursor.rowcount
    # Backfill existing translation_priority into translation_queue
    cursor.execute("""
        SELECT workshop_id, title, short_description, extended_description,
               translation_priority
        FROM workshop_items WHERE translation_priority > 0
    """)
    for row in cursor.fetchall():
        now_iso = datetime.now(timezone.utc).isoformat()
        for field, text in [("title_en", row["title"]),
                             ("short_description_en", row["short_description"]),
                             ("extended_description_en", row["extended_description"])]:
            if text and not text.isascii():
                cursor.execute(
                    "INSERT INTO translation_queue (item_type, item_id, field, original_text, priority, dt_queued) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("item", row["workshop_id"], field, text, row["translation_priority"], now_iso)
                )
    conn.commit()
    cursor.execute("PRAGMA user_version = 3")
    logging.info(f"Migration 2→3 complete. Set needs_web_scrape=1 on {updated} items.")

def _migration_3_to_4(cursor, conn, db_path):
    logging.info("Running migration 3→4: adding image download flag...")
    cursor.execute("""
        UPDATE workshop_items SET needs_image = 1
        WHERE preview_url IS NOT NULL AND preview_url != ''
          AND image_extension IS NULL
    """)
    updated = cursor.rowcount
    cursor.execute("PRAGMA user_version = 4")
    logging.info(f"Migration 3→4 complete. Set needs_image=1 on {updated} items.")

def _migration_4_to_5(cursor, conn, db_path):
    logging.info("Running migration 4→5: FTS5 full-text search + missing indexes...")

    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS workshop_fts USING fts5(
            title, title_en,
            short_description, short_description_en,
            extended_description, extended_description_en,
            content='workshop_items', content_rowid='workshop_id'
        )
    """)

    # Populate FTS5 from existing data (content-sync needs initial rebuild)
    cursor.execute("""
        INSERT INTO workshop_fts(workshop_fts) VALUES ('rebuild')
    """)
    logging.info("FTS5 table created and populated (content-sync with workshop_items)")

    # New indexes for translated fields and other searchable columns
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_title_en ON workshop_items (title_en)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_short_description_en ON workshop_items (short_description_en)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_extended_description_en ON workshop_items (extended_description_en)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_filename ON workshop_items (filename)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_size ON workshop_items (file_size)")

    conn.commit()
    cursor.execute("PRAGMA user_version = 5")
    logging.info("Migration 4→5 complete.")

def _migration_5_to_6(cursor, conn, db_path):
    logging.info("Running migration 5→6: normalized tag schema (tags + workshop_tags tables)...")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            tag_id   INTEGER PRIMARY KEY,
            tag_name TEXT UNIQUE NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS workshop_tags (
            workshop_id INTEGER NOT NULL,
            tag_id      INTEGER NOT NULL,
            PRIMARY KEY (workshop_id, tag_id)
        ) WITHOUT ROWID
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_workshop_tags_tag_id ON workshop_tags (tag_id)")
    conn.commit()

    # Populate via _ensure_tag_ids — stress-test the runtime code path.
    # Defensive: if the tags column was already dropped by a previous
    # partial run, skip population (tables exist but no JSON to convert).
    cols = [c[1] for c in cursor.execute("PRAGMA table_info(workshop_items)").fetchall()]
    if "tags" in cols:
        import json as _json
        cursor.execute("SELECT workshop_id, tags FROM workshop_items WHERE tags IS NOT NULL AND tags != '' AND tags != '[]'")
        rows = cursor.fetchall()
        logging.info(f"Migrating tags for {len(rows)} items...")

        # Phase 1: collect all unique tag names and bulk-create IDs
        all_tag_names = set()
        phase1_failures = 0
        phase1_last_error = None
        for i, row in enumerate(rows):
            try:
                tag_names = _json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
                if isinstance(tag_names, list):
                    for t in tag_names:
                        all_tag_names.add(t.get("tag") if isinstance(t, dict) else str(t))
            # A malformed row is counted and reported once after the loop: capture is
            # not configured yet this early, so a per-row record would be a no-op.
            except Exception as exc:
                pass
                phase1_failures += 1
                phase1_last_error = exc
            if (i + 1) % 50000 == 0:
                logging.debug(f"  tag collection progress: {i + 1}/{len(rows)}")
        if phase1_failures:
            # initialize_database runs before failure capture is configured, so a
            # capture call here would be a no-op; aggregate instead of per-row logs.
            logging.warning(
                "Tags migration 5→6 phase 1 (collect tag names): %d of %d rows "
                "could not be parsed; the legacy tags column is dropped later, so "
                "those tags are lost (last error: %s)",
                phase1_failures, len(rows), phase1_last_error,
            )
        logging.info(f"  Phase 1: creating IDs for {len(all_tag_names)} unique tag names...")
        _ensure_tag_ids(db_path, list(all_tag_names))
        logging.info("  Tag IDs created.")

        # Phase 2: insert workshop_tags associations in batches using in-memory lookup
        tag_lookup = {r["tag_name"]: r["tag_id"] for r in cursor.execute("SELECT tag_id, tag_name FROM tags").fetchall()}
        logging.info(f"  Phase 2: inserting associations ({len(rows)} items)...")
        batch_size = 10000
        sub_batch = 1000
        phase2_failures = 0
        phase2_last_error = None
        for i, row in enumerate(rows):
            try:
                tag_names = _json.loads(row["tags"]) if isinstance(row["tags"], str) else row["tags"]
                if not isinstance(tag_names, list):
                    continue
                for t in tag_names:
                    name = t.get("tag") if isinstance(t, dict) else str(t)
                    tid = tag_lookup.get(name)
                    if tid is not None:
                        cursor.execute(
                            "INSERT OR IGNORE INTO workshop_tags (workshop_id, tag_id) VALUES (?, ?)",
                            (row["workshop_id"], tid)
                        )
            # A malformed row is counted and reported once after the loop: capture is
            # not configured yet this early, so a per-row record would be a no-op.
            except Exception as exc:
                pass
                phase2_failures += 1
                phase2_last_error = exc
            if (i + 1) % sub_batch == 0:
                logging.debug(f"  tag progress: {i + 1}/{len(rows)}")
            if (i + 1) % batch_size == 0:
                conn.commit()
                logging.info(f"  migrated {i + 1}/{len(rows)} items")

        if phase2_failures:
            # initialize_database runs before failure capture is configured, so a
            # capture call here would be a no-op; aggregate instead of per-row logs.
            logging.warning(
                "Tags migration 5→6 phase 2 (insert workshop_tags associations): "
                "%d of %d rows failed; the legacy tags column is dropped later, so "
                "those associations are lost (last error: %s)",
                phase2_failures, len(rows), phase2_last_error,
            )
        conn.commit()
        logging.info(f"Tags: {cursor.execute('SELECT COUNT(*) FROM tags').fetchone()[0]} unique tags, "
                     f"{cursor.execute('SELECT COUNT(*) FROM workshop_tags').fetchone()[0]} associations")

        # Drop the legacy JSON column in a tight transaction
        conn.execute("BEGIN")
        cursor.execute("DROP INDEX IF EXISTS idx_tags")
        cursor.execute("ALTER TABLE workshop_items DROP COLUMN tags")
        cursor.execute("PRAGMA user_version = 6")
        conn.commit()
        logging.info("Dropped legacy tags column — migration 5→6 complete.")
    else:
        cursor.execute("PRAGMA user_version = 6")
        conn.commit()
        logging.info("Migration 5→6 complete (tags column already dropped, skipping population).")

    compact_tag_ids(db_path)

def _migration_6_to_7(cursor, conn, db_path):
    logging.info("Running migration 6→7: converting dt_* columns from TEXT (ISO) to INTEGER (Unix epoch)...")

    # Drop indexes that reference dt_* columns — required before DROP COLUMN
    for idx in ["idx_dt_updated", "idx_dt_attempted", "idx_status_dt_attempted", "idx_creator_dt_updated"]:
        cursor.execute(f"DROP INDEX IF EXISTS {idx}")

    tables_cols = {
        "workshop_items": ["dt_found", "dt_updated", "dt_attempted", "dt_translated"],
        "users": ["dt_updated", "dt_translated"],
        "translation_queue": ["dt_queued"],
    }
    for table, cols in tables_cols.items():
        for col in cols:
            col_info = {r[1]: r[2] for r in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
            new_col = col + "_new"

            if col in col_info and col_info[col].upper() == "INTEGER":
                logging.debug(f"  {table}.{col} already INTEGER, skipping")
                continue

            if new_col in col_info:
                logging.debug(f"  {table}.{col}: {new_col} exists from partial run, finishing rename")
                cursor.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
                cursor.execute(f"ALTER TABLE {table} RENAME COLUMN {new_col} TO {col}")
                continue

            logging.debug(f"  converting {table}.{col}")
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {new_col} INTEGER")
            cursor.execute(f"UPDATE {table} SET {new_col} = CAST(strftime('%s', {col}) AS INTEGER) WHERE {col} IS NOT NULL")
            cursor.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
            cursor.execute(f"ALTER TABLE {table} RENAME COLUMN {new_col} TO {col}")

    # Rebuild indexes dropped with their TEXT columns
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dt_updated ON workshop_items (dt_updated)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dt_attempted ON workshop_items (dt_attempted)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status_dt_attempted ON workshop_items (status, dt_attempted)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_dt_updated ON workshop_items (creator, dt_updated)")

    conn.commit()
    cursor.execute("PRAGMA user_version = 7")
    logging.info("Migration 6→7 complete.")

def _migration_7_to_8(cursor, conn, db_path):
    logging.info("Running migration 7→8: adding indexes on all sortable columns...")
    sort_indexes = [
        "time_created", "time_updated",
        "file_size", "subscriptions", "favorited", "views",
        "wilson_subscription_score", "wilson_favorite_score",
    ]
    for col in sort_indexes:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{col} ON workshop_items ({col})")
    conn.commit()
    cursor.execute("PRAGMA user_version = 8")
    logging.info("Migration 7→8 complete.")

def _migration_8_to_9(cursor, conn, db_path):
    logging.info("Running migration 8→9: recalculating favorite scores with lifetime_subscriptions denominator...")
    import math
    def wl(s, v):
        if v == 0:
            return 0.0
        p = min(float(s) / v, 1.0)
        z2 = 1.96 * 1.96
        d = 1 + z2 / v
        n = p + z2 / (2*v) - 1.96 * math.sqrt(max(0.0, p*(1-p)/v) + z2/(4*v*v))
        return max(0.0, min(1.0, n / d))

    cursor.execute("""
        SELECT workshop_id, favorited, lifetime_subscriptions
        FROM workshop_items WHERE favorited IS NOT NULL
    """)
    updated = 0
    for row in cursor.fetchall():
        fav_score = wl(row["favorited"] or 0, row["lifetime_subscriptions"] or 0)
        cursor.execute(
            "UPDATE workshop_items SET wilson_favorite_score = ? WHERE workshop_id = ?",
            (fav_score, row["workshop_id"])
        )
        updated += 1
    conn.commit()
    cursor.execute("PRAGMA user_version = 9")
    logging.info(f"Migration 8→9 complete. Recalculated {updated} favorite scores.")

def _migration_9_to_10(cursor, conn, db_path):
    logging.info("Running migration 9->10: adding index on is_queued_for_subscription...")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_is_queued ON workshop_items (is_queued_for_subscription)"
    )
    conn.commit()
    cursor.execute("PRAGMA user_version = 10")
    logging.info("Migration 9→10 complete.")

def _migration_10_to_11(cursor, conn, db_path):
    logging.info("Running migration 10->11: repurposing dt_* columns...")

    # Step 1: dt_attempted (fetch time) → dt_found where dt_found is NULL.
    # Preserves our best approximation of when the item was first found,
    # since most items have only been fetched once.
    cursor.execute(
        "UPDATE workshop_items SET dt_found = dt_attempted "
        "WHERE dt_found IS NULL AND dt_attempted IS NOT NULL"
    )
    found_count = cursor.rowcount
    logging.info(f"  Step 1: dt_attempted -> dt_found for {found_count} items")

    # Step 2: dt_attempted (fetch time) → dt_updated where dt_updated is NULL.
    cursor.execute(
        "UPDATE workshop_items SET dt_updated = dt_attempted "
        "WHERE dt_updated IS NULL AND dt_attempted IS NOT NULL"
    )
    updated_count = cursor.rowcount
    logging.info(f"  Step 2: dt_attempted -> dt_updated for {updated_count} items")

    # Step 3: dt_attempted → time_updated (version marker for web scrape).
    cursor.execute(
        "UPDATE workshop_items SET dt_attempted = time_updated "
        "WHERE time_updated IS NOT NULL"
    )
    attempted_count = cursor.rowcount
    logging.info(f"  Step 3: dt_attempted = time_updated for {attempted_count} items")

    # Step 4: dt_translated → time_updated where translation exists.
    cursor.execute(
        "UPDATE workshop_items SET dt_translated = time_updated "
        "WHERE dt_translated IS NOT NULL AND time_updated IS NOT NULL"
    )
    trans_count = cursor.rowcount

    # For items with translations but no time_updated, leave as-is (epoch already).
    cursor.execute(
        "SELECT COUNT(*) FROM workshop_items "
        "WHERE dt_translated IS NOT NULL AND time_updated IS NULL"
    )
    trans_skipped = cursor.fetchone()[0]
    logging.info(f"  Step 4: dt_translated = time_updated for {trans_count} items, "
                 f"{trans_skipped} items with translation but no time_updated left as-is")

    conn.commit()
    cursor.execute("PRAGMA user_version = 11")
    logging.info("Migration 10->11 complete.")

def _migration_11_to_12(cursor, conn, db_path):
    logging.info("Running migration 11->12: adding api_priority column...")
    staleness_days = 30 # Use default for migration
    threshold = int(time.time()) - staleness_days * 86400

    cols = {r[1] for r in cursor.execute("PRAGMA table_info(workshop_items)").fetchall()}
    if "api_priority" not in cols:
        cursor.execute(
            "ALTER TABLE workshop_items ADD COLUMN api_priority INTEGER NOT NULL DEFAULT 0"
        )

    # Never-scraped items (status IS NULL) → priority 3 (default new-item)
    cursor.execute(
        "UPDATE workshop_items SET api_priority = 3 WHERE status IS NULL"
    )
    never_count = cursor.rowcount

    # Stale items → priority 1 (periodic refresh)
    cursor.execute(
        "UPDATE workshop_items SET api_priority = 1 "
        "WHERE status = 200 AND dt_updated IS NOT NULL AND dt_updated < ?",
        (threshold,)
    )
    stale_count = cursor.rowcount

    conn.commit()
    cursor.execute("PRAGMA user_version = 12")
    logging.info(f"Migration 11->12 complete. "
                  f"Never-scraped={never_count}, Stale={stale_count}")

def _migration_12_to_13(cursor, conn, db_path):
    logging.info("Running migration 12->13: migrating image folder structure to 3-level hexadecimal hash bucket folders...")
    db_dir = os.path.dirname(os.path.abspath(db_path))
    base_images_dir = os.path.join(db_dir, "images")
    if not os.path.isdir(base_images_dir):
        base_images_dir = "images"

    cursor.execute("SELECT workshop_id, image_extension FROM workshop_items WHERE image_extension IS NOT NULL AND image_extension != ''")
    rows = cursor.fetchall()

    migrated_count = 0
    already_migrated_count = 0
    missing_count = 0

    for row in rows:
        wid = row["workshop_id"]
        ext = row["image_extension"]

        old_path = os.path.join(base_images_dir, f"{wid}.{ext}")

        bucket1, bucket2, bucket3 = get_image_subdirs(wid)
        new_dir = os.path.join(base_images_dir, bucket1, bucket2, bucket3)
        new_path = os.path.join(new_dir, f"{wid}.{ext}")

        if os.path.exists(old_path):
            os.makedirs(new_dir, exist_ok=True)
            os.rename(old_path, new_path)
            migrated_count += 1
        elif os.path.exists(new_path):
            already_migrated_count += 1
        else:
            missing_count += 1

    cursor.execute("PRAGMA user_version = 13")
    conn.commit()
    logging.info(f"Migration 12->13 complete. Migrated: {migrated_count}, Already: {already_migrated_count}, Missing: {missing_count}")

def _migration_13_to_14(cursor, conn, db_path):
    logging.info("Running migration 13->14: renaming timestamp columns to the three-clock vocabulary...")

    def _table_columns(table):
        return {r[1] for r in cursor.execute(f"PRAGMA table_info({table})").fetchall()}

    def _rename_column(table, old_name, new_name):
        """Rename `old_name` -> `new_name` if needed.

        Idempotent/resumable: returns True only when the rename was actually
        performed. A re-run after a partial migration (or on a fresh database
        whose CREATE TABLE already carries the name) is a safe no-op.
        """
        cols = _table_columns(table)
        if old_name in cols and new_name not in cols:
            cursor.execute(f"ALTER TABLE {table} RENAME COLUMN {old_name} TO {new_name}")
            return True
        return False

    # --- Step 1: last_fetch_attempted_at ---------------------------------
    # Our attempt clock. Add the column and backfill it from the OLD
    # dt_updated, which was written on every attempt (success or failure),
    # so that value genuinely IS the attempt time. This must happen before
    # the rename below removes the dt_updated name.
    item_cols = _table_columns("workshop_items")
    if "last_fetch_attempted_at" not in item_cols:
        cursor.execute("ALTER TABLE workshop_items ADD COLUMN last_fetch_attempted_at INTEGER")
    if "dt_updated" in item_cols:
        cursor.execute(
            "UPDATE workshop_items SET last_fetch_attempted_at = dt_updated "
            "WHERE last_fetch_attempted_at IS NULL AND dt_updated IS NOT NULL"
        )
        logging.info("  last_fetch_attempted_at backfilled from dt_updated for %d rows",
                     cursor.rowcount)
        # Commit the backfill on its own before the (metadata-only) renames.
        # On the production database this UPDATE touches ~1.7M rows, and
        # holding that plus the DDL in one transaction grows the WAL without
        # bound. Commit, then force a checkpoint so the (multi-hundred-MB)
        # WAL is flushed back into the database before the later DDL runs:
        # leaving it un-checkpointed makes a subsequent DROP INDEX fail with
        # SQLITE_CANTOPEN on this filesystem.
        conn.commit()
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # --- Step 2: rename every clock column -------------------------------
    renames = {
        "workshop_items": [
            ("dt_found", "first_seen_at"),          # our clock: first insert
            ("dt_updated", "api_fetched_at"),       # our clock: last SUCCESS
            ("dt_attempted", "scrape_version"),     # Steam value: version key
            ("dt_translated", "translate_version"), # Steam value: version key
            ("time_created", "steam_created_at"),   # Steam clock
            ("time_updated", "steam_updated_at"),   # Steam clock
        ],
        "users": [
            ("dt_updated", "api_fetched_at"),       # our clock
            ("dt_translated", "translated_at"),     # our clock (NOT a version)
        ],
        "translation_queue": [
            ("dt_queued", "queued_at"),             # our clock: queue time
        ],
    }
    for table, pairs in renames.items():
        for old_name, new_name in pairs:
            if _rename_column(table, old_name, new_name):
                logging.info("  renamed %s.%s -> %s", table, old_name, new_name)

    # queued_at is deliberately NOT backfilled: we genuinely do not know when
    # the pre-existing queue rows were queued, and inventing a timestamp in a
    # migration whose purpose is removing misleading values would defeat it.
    # get_next_batch_for_translation keeps NULL (= unknown) ahead of dated
    # rows, so ordering within the legacy backlog stays arbitrary until it
    # drains.
    queued_null_count = cursor.execute(
        "SELECT COUNT(*) FROM translation_queue WHERE queued_at IS NULL"
    ).fetchone()[0]
    logging.info("  left queued_at NULL on %d pre-existing translation_queue rows; "
                 "their ordering stays arbitrary until the backlog drains",
                 queued_null_count)

    # --- Step 3: clear the migration artefact ----------------------------
    # Migration 10->11 repurposed dt_attempted into a Steam version key by
    # setting it to time_updated, but rows with no Steam payload kept their
    # pre-migration fetch time. Now that the column is scrape_version those
    # stale values are meaningless. They are already preserved in
    # first_seen_at, so clearing them loses nothing.
    cursor.execute(
        "UPDATE workshop_items SET scrape_version = NULL "
        "WHERE steam_updated_at IS NULL AND scrape_version IS NOT NULL"
    )
    logging.info("  cleared stale scrape_version on %d rows that have no Steam payload",
                 cursor.rowcount)

    # --- Step 4: api_fetched_at means "last SUCCESSFUL API content pull" --
    # The old dt_updated was written on every attempt, so rows that never
    # received API content (steam_updated_at IS NULL: the 500/404/-1 rows)
    # would otherwise inherit pure attempt times under a name that promises
    # success. This is the best available approximation: for a row that
    # succeeded once and then failed a later attempt, the old dt_updated
    # holds the FAILURE time and the true last-success time cannot be
    # recovered from the existing data. It self-corrects on the next
    # successful fetch.
    cursor.execute(
        "UPDATE workshop_items SET api_fetched_at = NULL "
        "WHERE steam_updated_at IS NULL AND api_fetched_at IS NOT NULL"
    )
    logging.info(
        "  api_fetched_at cleared on %d rows that never received API content "
        "(approximation: true last-success time is unrecoverable where a later attempt failed)",
        cursor.rowcount)

    # --- Step 5: repair the anomalous first_seen_at row ------------------
    # A caller once passed first_seen_at=None explicitly, suppressing the
    # insert default; one live row ended up with first_seen_at IS NULL. Its
    # api_fetched_at is a usable lower bound on when it was first seen.
    cursor.execute(
        "UPDATE workshop_items SET first_seen_at = api_fetched_at "
        "WHERE first_seen_at IS NULL AND api_fetched_at IS NOT NULL"
    )
    logging.info("  first_seen_at repaired from api_fetched_at for %d rows", cursor.rowcount)

    # Commit the cleanup DML explicitly before any DDL below. Python's
    # sqlite3 module otherwise commits an open DML transaction implicitly at
    # the first DDL statement, which on a multi-hundred-MB WAL leaves the
    # connection unable to open the database for the *next* DDL
    # (SQLITE_CANTOPEN). Checkpointing keeps the WAL small as well.
    conn.commit()
    cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # --- Step 6: recreate the affected indexes under clear names ---------
    # SQLite rewrites index *definitions* on RENAME COLUMN but keeps the old
    # index *names*, so drop the stale idx_dt_* names and recreate them
    # explicitly against the new columns.
    for idx in ["idx_dt_updated", "idx_dt_attempted",
                "idx_status_dt_attempted", "idx_creator_dt_updated"]:
        cursor.execute(f"DROP INDEX IF EXISTS {idx}")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_fetched_at ON workshop_items (api_fetched_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_scraped_version ON workshop_items (scrape_version)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status_scraped_version ON workshop_items (status, scrape_version)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_api_fetched_at ON workshop_items (creator, api_fetched_at)")

    conn.commit()
    cursor.execute("PRAGMA user_version = 14")
    conn.commit()
    logging.info("Migration 13->14 complete.")

def _migration_14_to_15(cursor, conn, db_path):
    logging.info("Running migration 14->15: rebuilding the full-text index and adding sync triggers...")

    # Migration 4->5 created workshop_fts and populated it once, but installed
    # no triggers and never rebuilt it again. Every row inserted, updated or
    # deleted since is therefore missing from the index: on the production
    # database it held 640,471 documents against 1,725,544 items (62.9 %
    # absent). Rebuild it from the content table first, then keep it correct.
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS workshop_fts USING fts5(
            title, title_en,
            short_description, short_description_en,
            extended_description, extended_description_en,
            content='workshop_items', content_rowid='workshop_id'
        )
    """)

    # The rebuild rewrites a large part of the index, so commit and checkpoint
    # it on its own before the DDL below starts. This mirrors migration 13->14:
    # holding a multi-hundred-MB WAL open across later DDL is what produced
    # SQLITE_CANTOPEN on this filesystem.
    cursor.execute("INSERT INTO workshop_fts(workshop_fts) VALUES ('rebuild')")
    conn.commit()
    cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    logging.info("  full-text index rebuilt from workshop_items")

    # External-content FTS5 has no way to look up a row's old tokens, so
    # removal must go through the special 'delete' command with the OLD
    # column values. A plain DELETE FROM workshop_fts would silently leave the
    # old tokens in the index and corrupt every later MATCH.
    _FTS_COLUMNS = ("title", "title_en", "short_description",
                    "short_description_en", "extended_description",
                    "extended_description_en")
    fts_cols = ", ".join(_FTS_COLUMNS)
    fts_new = ", ".join(f"new.{c}" for c in _FTS_COLUMNS)
    fts_old = ", ".join(f"old.{c}" for c in _FTS_COLUMNS)

    cursor.execute("DROP TRIGGER IF EXISTS workshop_items_fts_insert")
    cursor.execute("DROP TRIGGER IF EXISTS workshop_items_fts_delete")
    cursor.execute("DROP TRIGGER IF EXISTS workshop_items_fts_update")

    cursor.execute(f"""
        CREATE TRIGGER workshop_items_fts_insert AFTER INSERT ON workshop_items BEGIN
            INSERT INTO workshop_fts(rowid, {fts_cols})
            VALUES (new.workshop_id, {fts_new});
        END
    """)
    cursor.execute(f"""
        CREATE TRIGGER workshop_items_fts_delete AFTER DELETE ON workshop_items BEGIN
            INSERT INTO workshop_fts(workshop_fts, rowid, {fts_cols})
            VALUES ('delete', old.workshop_id, {fts_old});
        END
    """)
    # Scoped to the six indexed columns on purpose: most writes to
    # workshop_items are queue/priority updates that touch none of them, and an
    # unscoped trigger would rewrite a chunk of the index on every priority
    # bump. insert_or_update_item builds its SET list from the keys actually
    # supplied, so this fires exactly when an indexed column is written.
    cursor.execute(f"""
        CREATE TRIGGER workshop_items_fts_update
        AFTER UPDATE OF {fts_cols} ON workshop_items BEGIN
            INSERT INTO workshop_fts(workshop_fts, rowid, {fts_cols})
            VALUES ('delete', old.workshop_id, {fts_old});
            INSERT INTO workshop_fts(rowid, {fts_cols})
            VALUES (new.workshop_id, {fts_new});
        END
    """)

    conn.commit()
    cursor.execute("PRAGMA user_version = 15")
    conn.commit()
    logging.info("Migration 14->15 complete.")

def _migration_15_to_16(cursor, conn, db_path):
    logging.info("Running migration 15->16: requeueing items stranded by transient API failures...")

    # Before this version a transient API failure (status 500) cleared
    # api_priority, and _promote_stale_items promotes only rows at status 200.
    # A failed item therefore left every queue with nothing able to bring it
    # back: not queued, and ineligible for the staleness sweep. On the
    # production database that stranded 2,581 rows. Requeue them at backlog
    # priority so they are retried rather than abandoned.
    cursor.execute(
        "UPDATE workshop_items SET api_priority = 1 "
        "WHERE status = 500 AND api_priority = 0"
    )
    stranded_failures = cursor.rowcount

    # Rows discovered but never attempted are unreachable for the same
    # structural reason: not queued, and the sweep only promotes rows that
    # have succeeded at least once. Permanent failures (status -1) are left
    # alone -- they are correctly dequeued.
    cursor.execute(
        "UPDATE workshop_items SET api_priority = 1 "
        "WHERE status IS NULL AND api_fetched_at IS NULL AND api_priority = 0"
    )
    stranded_unattempted = cursor.rowcount

    conn.commit()
    cursor.execute("PRAGMA user_version = 16")
    conn.commit()
    logging.info(
        "Migration 15->16 complete. Requeued %d transient failures and %d never-attempted items.",
        stranded_failures, stranded_unattempted,
    )

def _migration_16_to_17(cursor, conn, db_path):
    logging.info("Running migration 16->17: removing dead items from the work queues...")

    # A dead item (status -1) can never complete, but the permanent-failure
    # path only cleared api_priority. needs_web_scrape, needs_image and
    # translation_priority were left set, and those queues select on their
    # flag alone with no dead-item guard, so the rows were retried forever
    # and the queues could never drain. About ten thousand rows on the
    # production database. api_priority is deliberately not touched here:
    # the 404 path already zeroes it, and a dead row still holding an API
    # priority is a separate defect.
    cursor.execute(
        "UPDATE workshop_items "
        "SET needs_web_scrape = 0, needs_image = 0, translation_priority = 0 "
        "WHERE status = -1"
    )
    dequeued_dead = cursor.rowcount

    conn.commit()
    cursor.execute("PRAGMA user_version = 17")
    conn.commit()
    logging.info(
        "Migration 16->17 complete. Removed %d dead items from the work queues.",
        dequeued_dead,
    )

def _migration_17_to_18(cursor, conn, db_path):
    logging.info("Running migration 17->18: requeueing items dequeued without a description...")

    # Before 17894f7 the web worker tested the *dict* the scraper returned
    # rather than the description inside it. A page whose description selector
    # did not match comes back as a truthy dict with description None, so the
    # item was written with extended_description NULL and
    # needs_web_scrape = 0 -- recorded as a finished scrape and permanently
    # out of the queue. On the 2026-09-12 snapshot that stranded 63,229 rows.
    #
    # Priority 1 is the backlog level migration 15->16 used for the rows a
    # transient failure had stranded: high enough that the item is retried,
    # but below the 3/5/10 of new and current work, so it cannot jump ahead
    # of the live queue. Dead items (status -1) are excluded because they can
    # never complete and issue 17 keeps them out of every queue.
    #
    # SQLite's cursor.rowcount counts the rows the UPDATE *matched*, not the
    # rows whose value actually changed. That cannot inflate this count: every
    # matched row moves from 0 to 1 (and a re-run matches nothing), so the
    # count below is exact.
    cursor.execute(
        "UPDATE workshop_items SET needs_web_scrape = 1 "
        "WHERE needs_web_scrape = 0 "
        "AND COALESCE(extended_description, '') = '' "
        "AND (status IS NULL OR status <> -1)"
    )
    stranded_descriptionless = cursor.rowcount

    conn.commit()
    cursor.execute("PRAGMA user_version = 18")
    conn.commit()
    logging.info(
        "Migration 17->18 complete. Requeued %d description-less items.",
        stranded_descriptionless,
    )

def _migration_18_to_19(cursor, conn, db_path):
    logging.info("Running migration 18->19: requeueing items stranded by cursor discovery...")

    # Cursor discovery inserted bare rows and let the api_priority column
    # default decide whether they were queued. That default is not stable
    # across database histories: CREATE TABLE declares DEFAULT 3, but the
    # ALTER TABLE in migration 11->12 gives an existing database DEFAULT 0.
    # On a migrated database -- the production one -- every discovered row
    # therefore landed at 0, which means "not queued", and the fetch queue
    # selects api_priority > 0, so nothing ever fetched them. Migration
    # 15->16 requeued the rows already stranded by this but left the cause
    # in place, so it kept stranding more; the daemon now passes the
    # priority explicitly. Requeue the same never-attempted population at
    # the same backlog priority as 15->16's second statement. The status
    # predicate excludes dead rows (status = -1) and the other queue flags
    # are deliberately untouched: this is an API-fetch queue repair, not a
    # scrape, image or translation decision.
    cursor.execute(
        "UPDATE workshop_items SET api_priority = 1 "
        "WHERE status IS NULL AND api_fetched_at IS NULL AND api_priority = 0"
    )
    stranded_unattempted = cursor.rowcount

    conn.commit()
    cursor.execute("PRAGMA user_version = 19")
    conn.commit()
    logging.info(
        "Migration 18->19 complete. Requeued %d never-attempted items stranded by cursor discovery.",
        stranded_unattempted,
    )

def _migration_19_to_20(cursor, conn, db_path):
    logging.info("Running migration 19->20: clearing queue priority from dead items...")

    # The permanent-failure path clears api_priority when it marks an item
    # dead, so this is not an ongoing leak -- it is the rows that were already
    # dead before that line existed. They matter because api_priority > 0 is
    # what every count of "queued for a fetch" looks at, and the statistics
    # screen reports dead items still holding a queue flag as `dead_items_by_queue`.
    # Leaving ten thousand of them there would peg a detector whose whole
    # value is that it reads zero unless something has regressed.
    #
    # Only api_priority: the other queue flags were cleared by 16->17, and
    # status is what makes an item dead in the first place.
    cursor.execute(
        "UPDATE workshop_items SET api_priority = 0 "
        "WHERE status = -1 AND api_priority > 0"
    )
    dead_priority_cleared = cursor.rowcount

    conn.commit()
    cursor.execute("PRAGMA user_version = 20")
    conn.commit()
    logging.info(
        "Migration 19->20 complete. Cleared the queue priority of %d dead items.",
        dead_priority_cleared,
    )

def _migration_20_to_21(cursor, conn, db_path):
    logging.info("Running migration 20->21: recording the owner's subscription columns...")

    # The two columns are added by _safe_add_columns above (a fresh database
    # gets them in CREATE TABLE, an existing one by ALTER). This migration
    # records the version bump and does one defensive thing. `own_subscribed`
    # is a boolean, and a NULL in it is the wrong value: the state derivation
    # would read it as false, but only by accident of truthiness.
    #
    # SQLite fills existing rows from the column's DEFAULT when it ALTERs, so
    # with `DEFAULT 0` declared in both schema places there is normally nothing
    # to fix and the UPDATE below matches no rows. It is kept anyway because it
    # costs one scan and the alternative is trusting that the default was always
    # declared -- which is the assumption that produced issue 20, where
    # `CREATE TABLE` and `ALTER` disagreed about `api_priority`'s default.
    cursor.execute(
        "UPDATE workshop_items SET own_subscribed = 0 WHERE own_subscribed IS NULL"
    )
    defaulted = cursor.rowcount

    conn.commit()
    cursor.execute("PRAGMA user_version = 21")
    conn.commit()
    logging.info(
        "Migration 20->21 complete. own_subscribed needed defaulting on %d row(s) "
        "(normally none -- the column default already covers them); "
        "own_first_subscribed_at stays NULL on every row, because no item has been "
        "observed subscribed yet and a stamp would claim an observation never made.",
        defaulted,
    )

def _migration_21_to_22(cursor, conn, db_path):
    logging.info("Running migration 21->22: demoting queue priority of filter-excluded items...")

    # The queue flags are priority columns (`needs_web_scrape` and
    # `needs_image`), and the daemon used to hand them the item's whole
    # pre-fetch `api_priority` -- so a newly discovered item, which carries 3,
    # put an item the enrichment filters excluded into the same band as the
    # ones they selected. The daemon no longer does that; this is the rows it
    # already stamped that way. It matters because those rows are served
    # first: measured live, 760,782 web entries and 668,269 image ones from
    # excluded items were queued ahead of 107,365 items the filters had
    # chosen.
    #
    # `MAX(stored, new)` is why these rows cannot fix themselves: a priority
    # is never downgraded by a later fetch, so an item stamped 3 stays 3 until
    # something scrapes it, and the queue that was already a year deep only
    # gets deeper. Nothing here needs a schema change, so the version bump is
    # the whole of the schema work.
    web_demoted, image_demoted = _demote_filtered_out_queue_priorities(conn)

    conn.commit()
    cursor.execute("PRAGMA user_version = 22")
    conn.commit()
    logging.info(
        "Migration 21->22 complete. Returned %d web and %d image queue entries to "
        "backlog priority; items the filters select kept the priority they had.",
        web_demoted, image_demoted,
    )

def _migration_22_to_23(cursor, conn, db_path):
    logging.info("Running migration 22->23: repairing stranded translation-queue mirrors...")

    # `translation_priority` is a mirror of `translation_queue`: it is raised
    # when a field is queued and the translator zeroes it when the item's
    # last queue row is deleted. Before this version
    # `queue_field_for_translation` wrote the queue row and the mirror on two
    # separate connections, so a translator drain landing between them could
    # delete the row and zero the mirror, after which the helper's second
    # statement raised the mirror again from `MAX(0, priority)`. The item was
    # then permanently drawn as having translation work with nothing queued
    # behind it, because every producer skips a translation that is already
    # current, so nothing ever re-queues the field to clear it. The helper is
    # now a single transaction; this repairs the rows the old one stranded.
    #
    # Only `workshop_items`: at this version a user's name translation was
    # tracked on `users.translation_priority` alone and never got a
    # `translation_queue` row, so that mirror was not expected to match this
    # table. v27->v28 makes the user mirror a mirror of the queue as well and
    # repairs the creator rows this migration deliberately skipped. Only the
    # high direction is repaired -- a queue row whose mirror is zero still has
    # its work picked up, because the translator selects on the queue, not the
    # mirror -- and dead rows are not special-cased: a mirror with nothing
    # queued is wrong for them too.
    cursor.execute(
        "UPDATE workshop_items SET translation_priority = 0 "
        "WHERE translation_priority > 0 "
        "AND NOT EXISTS ("
        "SELECT 1 FROM translation_queue q "
        "WHERE q.item_type = 'item' AND q.item_id = workshop_items.workshop_id"
        ")"
    )
    stranded_mirrors = cursor.rowcount

    conn.commit()
    cursor.execute("PRAGMA user_version = 23")
    conn.commit()
    logging.info(
        "Migration 22->23 complete. Cleared the translation mirror on %d item(s) "
        "with no field left in translation_queue.",
        stranded_mirrors,
    )

def _migration_23_to_24(cursor, conn, db_path):
    logging.info("Running migration 23->24: dropping the never-populated language column...")

    # `language` was added expecting the Steam API to return a field for it.
    # No response this project consumes can: GetPublishedFileDetails carries
    # no language field, and `language` exists in the request protocol only
    # as the *viewer's* localization parameter, which the client sets and
    # never reads back. Every row in the live database is NULL (see
    # docs/live-data-profile.md), so the column only advertised a Steam field
    # that does not exist, drew a permanently "N/A" tooltip line, and backed
    # a "Language ID" filter that could not match.
    #
    # Dropping it follows migration 5->6's pattern for the legacy `tags`
    # column: drop the index that references the column first (SQLite refuses
    # to drop a column an index depends on), then the column itself. The
    # PRAGMA guard keeps the migration idempotent and resumable -- a fresh
    # database never has the column, and a database that already dropped it
    # (or a partial run) skips cleanly.
    cols = {r[1] for r in cursor.execute("PRAGMA table_info(workshop_items)").fetchall()}
    if "language" in cols:
        cursor.execute("DROP INDEX IF EXISTS idx_language")
        cursor.execute("ALTER TABLE workshop_items DROP COLUMN language")
        conn.commit()
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logging.info("  dropped idx_language and workshop_items.language")
    else:
        logging.info("  language column already absent; nothing to drop")

    cursor.execute("PRAGMA user_version = 24")
    conn.commit()
    logging.info("Migration 23->24 complete.")

def _migration_24_to_25(cursor, conn, db_path):
    logging.info("Running migration 24->25: indexing the three work queues...")

    # The web, image and API workers each find the head of their own queue
    # with a full scan plus a sort on **every poll** -- not only when the
    # statistics screen is open -- and the statistics breakdowns pay for the
    # same missing indexes. Each query asks for exactly
    # `WHERE <queue_column> > 0 ORDER BY <queue_column> DESC,
    # api_fetched_at ASC`, so a partial composite index in that shape lets
    # the poll read one index entry and stop and lets the breakdown walk the
    # index instead of sorting the table.
    #
    # Measured on a copy of the production snapshot (see docs/future-plans.md,
    # "Queue indexes"): the web and image polls drop from 258 ms and 252 ms to
    # ~0 ms, the web and image breakdowns from 470/401 ms to 70/59 ms, and the
    # API queue from 219 ms to 0.4 ms. The three indexes total 52.4 MB (2.8%
    # of the database) and took 2.3 s to build; index maintenance is 1.0 us
    # per completion update. The one-time build is deliberately not treated
    # as a cost to optimise.
    #
    # The web queue's `> 0` predicate covers about 89% of rows, so its
    # partial index is nearly full-size -- the shape of that queue, not a
    # defect. `IF NOT EXISTS` (and the separate commit for the version bump)
    # keeps a partial run resumable and re-runnable.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_web_scrape_queue "
        "ON workshop_items (needs_web_scrape DESC, api_fetched_at ASC) "
        "WHERE needs_web_scrape > 0"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_image_queue "
        "ON workshop_items (needs_image DESC, api_fetched_at ASC) "
        "WHERE needs_image > 0"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_queue "
        "ON workshop_items (api_priority DESC, api_fetched_at ASC) "
        "WHERE api_priority > 0"
    )

    conn.commit()
    cursor.execute("PRAGMA user_version = 25")
    conn.commit()
    logging.info(
        "Migration 24->25 complete. Indexed the web, image and API fetch queues."
    )

def _migration_25_to_26(cursor, conn, db_path):
    logging.info("Running migration 25->26: adding the local downloaded-at latch...")

    # `downloaded_at` records when this app first saw Steam's downloaded copy
    # of a subscribed item on disk. It is added by `_safe_add_columns` above
    # (a fresh database gets it from CREATE TABLE, an existing one by ALTER),
    # so this migration normally only records the version bump.
    #
    # Every pre-existing row is left NULL on purpose: the latch is a local
    # observation, and no item has been observed downloaded yet on the run
    # that introduces the column. The first scan after startup fills it in
    # for items that are both subscribed and on disk.
    cursor.execute("PRAGMA user_version = 26")
    conn.commit()
    logging.info(
        "Migration 25->26 complete. downloaded_at stays NULL on every row; the "
        "first folder scan fills it in for subscribed items Steam has on disk."
    )

def _migration_26_to_27(cursor, conn, db_path):
    logging.info(
        "Running migration 26->27: adding the per-queue completion clocks..."
    )

    # `web_scraped_at`, `image_fetched_at` and `translated_at` are our clock
    # for the three stages that had no completion time. They are added by
    # `_safe_add_columns` above (a fresh database gets them from CREATE
    # TABLE, an existing one by ALTER), so the columns exist by the time
    # this block runs.
    #
    # Every pre-existing row is left NULL, deliberately and permanently:
    # the stages did not record this, so no value can be reconstructed for
    # them, and inventing one would fabricate a rate. A migration must not
    # backfill, and the metrics report "no history yet" for a column with no
    # stamps rather than reading the absence as zero throughput.
    #
    # The three partial indexes are shaped for the one reader that is not a
    # worker: the throughput metrics. Each new metric asks for the rows
    # inside a recent window and the newest stamp, and a plain index over
    # 2.6M rows would also index the NULL history that can never match. The
    # `IS NOT NULL` predicate keeps the index empty on the run that creates
    # it and proportional to recorded completions afterwards. Measured on a
    # 2.6M-row copy: the full scan the metric would otherwise run costs
    # 73-85 ms per statement, the partial index serves each in 0.0-0.2 ms.
    for column in ("web_scraped_at", "image_fetched_at", "translated_at"):
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{column} "
            f"ON workshop_items ({column}) WHERE {column} IS NOT NULL"
        )

    conn.commit()
    cursor.execute("PRAGMA user_version = 27")
    conn.commit()
    logging.info(
        "Migration 26->27 complete. The three completion clocks stay NULL on "
        "every existing row; the stages fill them in from now on."
    )

def _migration_27_to_28(cursor, conn, db_path):
    logging.info(
        "Running migration 27->28: returning stranded creator names to the "
        "translation queue..."
    )

    # Issue 45: a creator's name was queued by raising
    # `users.translation_priority`, which was the whole producer while
    # `get_next_translation_item` (since removed) scanned both tables by that
    # flag. When the per-field `translation_queue` replaced that scan the
    # producer was never ported, so every creator flagged since has had no
    # queue row behind it and `translation_queue` has never held an
    # `item_type='user'` row. This
    # is the user-side counterpart of v22->v23, which repaired the item side
    # of the same stranded-mirror state, and of migration 2->3, which
    # backfilled raised item mirrors into per-field queue rows.
    #
    # Two statements, in this order:
    #   1. every flagged creator whose name genuinely needs translating gets
    #      the queue row the producer owed it;
    #   2. every remaining flagged creator loses the flag, because after (1)
    #      a raised mirror with no queue row is stranded again.
    #
    # The predicates are inlined rather than imported: a migration must keep
    # meaning what it meant at this version, so it cannot track a helper that
    # may change later. The ASCII test is the one `metrics._ascii_sql`
    # documents -- UTF-8 bytes equal characters exactly when the text is
    # ASCII -- and the currency rule is the one `metrics._creator_current_sql`
    # applies to the Creator Translation bar, so the migration, the bar and
    # the producer agree on what "needs translating" means. The mirror's own
    # value is carried into the row's priority, as in 2->3.
    #
    # *Measured in the 2026-09-18 backup*: 7,237 creators carried the flag,
    # 7,233 of them with a non-ASCII name and no current translation -- which
    # statement (1) queues -- and 4 whose name is ASCII, which statement (2)
    # clears. No creator needing translation was unflagged, so the flag is a
    # complete census of the backlog and this migration need look no further.
    cursor.execute(
        "INSERT INTO translation_queue "
        "(item_type, item_id, field, original_text, priority, queued_at) "
        "SELECT 'user', steamid, 'personaname_en', personaname, "
        "       translation_priority, ? "
        "FROM users "
        "WHERE translation_priority > 0 "
        "  AND personaname IS NOT NULL AND personaname <> '' "
        "  AND NOT (length(CAST(personaname AS BLOB)) = length(personaname)) "
        "  AND NOT (COALESCE(personaname_en, '') <> '' AND ("
        "             api_fetched_at IS NULL "
        "             OR (translated_at IS NOT NULL "
        "                 AND translated_at >= api_fetched_at))) "
        "  AND NOT EXISTS ("
        "      SELECT 1 FROM translation_queue q "
        "      WHERE q.item_type = 'user' AND q.item_id = users.steamid "
        "        AND q.field = 'personaname_en')",
        (int(time.time()),),
    )
    queued = cursor.rowcount

    cursor.execute(
        "UPDATE users SET translation_priority = 0 "
        "WHERE translation_priority > 0 "
        "AND NOT EXISTS ("
        "    SELECT 1 FROM translation_queue q "
        "    WHERE q.item_type = 'user' AND q.item_id = users.steamid"
        ")"
    )
    cleared = cursor.rowcount

    conn.commit()
    cursor.execute("PRAGMA user_version = 28")
    conn.commit()
    logging.info(
        "Migration 27->28 complete. Queued %d creator name(s) whose flag had "
        "no queue row behind it, and cleared %d flag(s) with nothing left to "
        "translate.",
        queued, cleared,
    )

def _migration_28_to_29(cursor, conn, db_path):
    logging.info("Running migration 28->29: dropping the dead page counter...")

    # `app_tracking.last_page_scanned` counted pages while discovery walked
    # them by number. `88397b7` replaced that with cursor discovery, which
    # resumes from `last_cursor`, and the writer went with it -- so ever
    # since, the TUI column, the web table and the `app_discovery` metric have
    # all read a column nothing sets, and displayed its DEFAULT 0. Nothing
    # references an index on it, so the column alone is dropped. The PRAGMA
    # guard keeps this idempotent and resumable, matching the `language`
    # drop: a fresh database never has the column, and a partial run that
    # already dropped it skips cleanly.
    cols = {r[1] for r in cursor.execute("PRAGMA table_info(app_tracking)").fetchall()}
    if "last_page_scanned" in cols:
        cursor.execute("ALTER TABLE app_tracking DROP COLUMN last_page_scanned")
        conn.commit()
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logging.info("  dropped app_tracking.last_page_scanned")
    else:
        logging.info("  app_tracking.last_page_scanned already absent; nothing to drop")

    cursor.execute("PRAGMA user_version = 29")
    conn.commit()
    logging.info("Migration 28->29 complete.")

def _migration_29_to_30(cursor, conn, db_path):
    logging.info("Running migration 29->30: renaming the creator and discovery tables...")

    # The `users` table holds Steam creators and there are no application
    # users, so it becomes `creators`; its `steamid` primary key already says
    # which id it is. `app_tracking`'s live columns are the discovery cursor
    # and the enrichment filters, not "tracking", so it becomes
    # `app_discovery`.
    #
    # The rename satisfies three constraints that pull in opposite directions,
    # all handled by `_create_legacy_schema`'s `_current_table_name` probe:
    #   * a fresh database still builds `users`/`app_tracking`, because the
    #     chain this file replays from 0 names them at earlier versions
    #     (6->7, 13->14, 21->22, 27->28);
    #   * an already-renamed database does not get the old names resurrected
    #     by `CREATE TABLE IF NOT EXISTS`;
    #   * `_safe_add_columns` re-raises anything that is not a duplicate-column
    #     error, so it too is routed through the resolved name.
    #
    # Neither table carries an index or a trigger, so `ALTER TABLE ... RENAME
    # TO` is the whole change; the index names Batch 6b's column renames must
    # recreate do not include any from these two tables.
    #
    # Guarded on "old exists and new does not" so a re-run is harmless: a
    # crash between the DDL commit and the version bump leaves the tables
    # renamed under the old marker, and this step must then be a no-op rather
    # than raise "no such table: users".
    for old_name, new_name in (("users", "creators"), ("app_tracking", "app_discovery")):
        tables = {row[0] for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        if new_name in tables:
            logging.info("  %s already renamed to %s; nothing to do", old_name, new_name)
        elif old_name in tables:
            cursor.execute(f"ALTER TABLE {old_name} RENAME TO {new_name}")
            logging.info("  renamed %s -> %s", old_name, new_name)
        else:
            logging.info("  neither %s nor %s exists; nothing to rename",
                         old_name, new_name)

    conn.commit()
    cursor.execute("PRAGMA user_version = 30")
    conn.commit()
    logging.info("Migration 29->30 complete.")

def _migration_30_to_31(cursor, conn, db_path):
    logging.info("Running migration 30->31: renaming workshop_items.status to fetch_status...")

    # `status` in a 47-column `workshop_items` table is unqualified: it competes
    # with the HTTP status code, the subscribe outcome, the controller status and
    # the `status_counts` metric, and a reader cannot tell which one a bare
    # `status` means. The column holds this app's synthetic fetch outcome
    # (200 fetched, 206 partial, -1 dead, 500 retry, NULL never fetched), not an
    # HTTP response code, so it becomes `fetch_status`. The *values* are
    # untouched; only the name moves.
    #
    # SQLite rewrites an index *definition* on RENAME COLUMN but keeps the index
    # *name*, so the three indexes whose names embed the old column would be left
    # named for a column that no longer exists. Drop and recreate each under a
    # name that matches what it indexes. `idx_web_scrape_queue` and
    # `idx_image_queue` are named for their queue rather than the column, so their
    # names may stay; SQLite rewrites their definitions in place.
    #
    # Guarded on the column that is present so a re-run is harmless: a crash
    # between the DDL commit and the version bump leaves the column renamed under
    # the old marker, and this step must then be a no-op rather than raise
    # "no such column: status". The index drop/create pair is likewise idempotent.
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(workshop_items)").fetchall()}
    if "fetch_status" in columns:
        logging.info("  workshop_items.fetch_status already present; nothing to rename")
    elif "status" in columns:
        cursor.execute("ALTER TABLE workshop_items RENAME COLUMN status TO fetch_status")
        logging.info("  renamed workshop_items.status -> fetch_status")
    else:
        logging.info("  neither status nor fetch_status exists; nothing to rename")

    for old_name, new_name, columns_sql in (
        ("idx_status", "idx_fetch_status", "fetch_status"),
        ("idx_appid_status", "idx_appid_fetch_status", "consumer_appid, fetch_status"),
        ("idx_status_scraped_version", "idx_fetch_status_scraped_version",
         "fetch_status, scrape_version"),
    ):
        cursor.execute(f"DROP INDEX IF EXISTS {old_name}")
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {new_name} ON workshop_items ({columns_sql})"
        )
        logging.info("  recreated %s as %s", old_name, new_name)

    conn.commit()
    cursor.execute("PRAGMA user_version = 31")
    conn.commit()
    logging.info("Migration 30->31 complete.")

def _migration_31_to_32(cursor, conn, db_path):
    logging.info("Running migration 31->32: renaming workshop_items.creator to creator_steamid...")

    # `creator` holds the author's SteamID64 and joins `creators.steamid`, but
    # the bare name reads as a display name or an object rather than the id it
    # is -- the neighbouring `creator_appid` is a different column and
    # `creators` is a different table. It becomes `creator_steamid`; the stored
    # values are untouched.
    #
    # SQLite rewrites an index *definition* on RENAME COLUMN but keeps the index
    # *name*, so the two indexes whose names embed the old column would be left
    # named for a column that no longer exists. Drop and recreate each under a
    # name that matches what it indexes. No other index on `workshop_items`
    # embeds `creator` in its name.
    #
    # Guarded on the column that is present so a re-run is harmless: a crash
    # between the DDL commit and the version bump leaves the column renamed
    # under the old marker, and this step must then be a no-op rather than raise
    # "no such column: creator". The index drop/create pairs are likewise
    # idempotent.
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(workshop_items)").fetchall()}
    if "creator_steamid" in columns:
        logging.info("  workshop_items.creator_steamid already present; nothing to rename")
    elif "creator" in columns:
        cursor.execute("ALTER TABLE workshop_items RENAME COLUMN creator TO creator_steamid")
        logging.info("  renamed workshop_items.creator -> creator_steamid")
    else:
        logging.info("  neither creator nor creator_steamid exists; nothing to rename")

    for old_name, new_name, columns_sql in (
        ("idx_creator", "idx_creator_steamid", "creator_steamid"),
        ("idx_creator_api_fetched_at", "idx_creator_steamid_api_fetched_at",
         "creator_steamid, api_fetched_at"),
    ):
        cursor.execute(f"DROP INDEX IF EXISTS {old_name}")
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {new_name} ON workshop_items ({columns_sql})"
        )
        logging.info("  recreated %s as %s", old_name, new_name)

    conn.commit()
    cursor.execute("PRAGMA user_version = 32")
    conn.commit()
    logging.info("Migration 31->32 complete.")

def _migration_32_to_33(cursor, conn, db_path):
    logging.info("Running migration 32->33: renaming the four workshop_items queue, image and download columns...")

    # Four columns whose names no longer say what they hold:
    #
    #   * `needs_web_scrape` and `needs_image` hold a 1-10 priority, not a
    #     boolean, and the queue predicates and the docs already call them
    #     priorities; `needs_` is a historical exception. They become
    #     `web_scrape_priority` and `image_priority`.
    #   * `image_extension` holds the server's *answer* -- a real extension, an
    #     HTTP status or a served non-image type -- not only a file extension.
    #     `images.image_state()` is already the classifier and `image_state` is
    #     the derived payload key, so the column becomes the `image_answer`
    #     that classifier reads.
    #   * `downloaded_at` is a one-way latch stamped when the folder scan first
    #     sees Steam's downloaded copy on disk, not a completion clock, so it
    #     becomes `steam_download_seen_at`.
    #
    # The stored values are untouched; only the names move.
    #
    # SQLite rewrites an index *definition* on RENAME COLUMN but keeps the index
    # *name*. No index on `workshop_items` embeds any of these four in its name:
    # `idx_web_scrape_queue` and `idx_image_queue` are named for their queue, so
    # their names stay and SQLite rewrites their definitions in place. There is
    # therefore nothing to drop or recreate here.
    #
    # Each rename is guarded on the column that is present, so a re-run is
    # harmless: a crash between the DDL commit and the version bump leaves the
    # columns renamed under the old marker, and this step must then be a no-op
    # rather than raise "no such column".
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(workshop_items)").fetchall()}
    for old_name, new_name in (
        ("needs_web_scrape", "web_scrape_priority"),
        ("needs_image", "image_priority"),
        ("image_extension", "image_answer"),
        ("downloaded_at", "steam_download_seen_at"),
    ):
        if new_name in columns:
            logging.info("  workshop_items.%s already present; nothing to rename", new_name)
        elif old_name in columns:
            cursor.execute(f"ALTER TABLE workshop_items RENAME COLUMN {old_name} TO {new_name}")
            logging.info("  renamed workshop_items.%s -> %s", old_name, new_name)
        else:
            logging.info("  neither %s nor %s exists; nothing to rename",
                         old_name, new_name)

    conn.commit()
    cursor.execute("PRAGMA user_version = 33")
    conn.commit()
    logging.info("Migration 32->33 complete.")

def _ensure_indexes(cursor):
    """Create the query indexes, after the column renames migrations perform.

    Separate from :func:`_create_legacy_schema` because several of these name columns
    that only exist once migration 13->14 has renamed them (``api_fetched_at``,
    ``scrape_version``), so they must run last. Idempotent: every statement is
    ``IF NOT EXISTS``.
    """
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_consumer_appid ON workshop_items (consumer_appid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fetch_status ON workshop_items (fetch_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_fetched_at ON workshop_items (api_fetched_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_scraped_version ON workshop_items (scrape_version)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_title ON workshop_items (title)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_steamid ON workshop_items (creator_steamid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_short_description ON workshop_items (short_description)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_extended_description ON workshop_items (extended_description)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fetch_status_scraped_version ON workshop_items (fetch_status, scrape_version)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_appid_fetch_status ON workshop_items (consumer_appid, fetch_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_steamid_api_fetched_at ON workshop_items (creator_steamid, api_fetched_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_translation_priority ON workshop_items (translation_priority)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_is_queued ON workshop_items (is_queued_for_subscription)")
    # Sort-column indexes — avoid expensive full-table sorts. idx_time_created /
    # idx_time_updated keep their historical names (SQLite rewrote their
    # definitions to the renamed columns); only the target columns matter here.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_time_created ON workshop_items (steam_created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_time_updated ON workshop_items (steam_updated_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_size ON workshop_items (file_size)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions ON workshop_items (subscriptions)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_favorited ON workshop_items (favorited)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_views ON workshop_items (views)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_wilson_subscription_score ON workshop_items (wilson_subscription_score)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_wilson_favorite_score ON workshop_items (wilson_favorite_score)")

    # The translation poll (`get_next_batch_for_translation`) orders the whole
    # outstanding queue by priority then queue time on every pass, so that sort
    # belongs in an index. It has to be created here rather than in
    # `_create_legacy_schema`: on a fresh database `_create_legacy_schema` runs while the
    # column is still called `dt_queued` (migration 13->14 renames it to
    # `queued_at`), so an index naming `queued_at` there fails with
    # "no such column". `_ensure_indexes` runs after the migration chain, which
    # is the reason it exists at all.
    #
    # The directions are the query's -- `priority DESC, queued_at ASC`. A
    # mixed-direction sort cannot be satisfied by a single-direction index
    # scanned in reverse, so the DESC on `priority` is not optional.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_translation_queue_poll "
        "ON translation_queue (priority DESC, queued_at ASC)"
    )

# Ordered schema migrations: (target user_version, function). The functions
# above are defined in this same order, and each one runs only when the file's
# recorded version is below its target. Add a new migration at the end and a
# new entry here -- see docs/schema-migrations.md.
MIGRATIONS = [
    (1, _migration_0_to_1),
    (2, _migration_1_to_2),
    (3, _migration_2_to_3),
    (4, _migration_3_to_4),
    (5, _migration_4_to_5),
    (6, _migration_5_to_6),
    (7, _migration_6_to_7),
    (8, _migration_7_to_8),
    (9, _migration_8_to_9),
    (10, _migration_9_to_10),
    (11, _migration_10_to_11),
    (12, _migration_11_to_12),
    (13, _migration_12_to_13),
    (14, _migration_13_to_14),
    (15, _migration_14_to_15),
    (16, _migration_15_to_16),
    (17, _migration_16_to_17),
    (18, _migration_17_to_18),
    (19, _migration_18_to_19),
    (20, _migration_19_to_20),
    (21, _migration_20_to_21),
    (22, _migration_21_to_22),
    (23, _migration_22_to_23),
    (24, _migration_23_to_24),
    (25, _migration_24_to_25),
    (26, _migration_25_to_26),
    (27, _migration_26_to_27),
    (28, _migration_27_to_28),
    (29, _migration_28_to_29),
    (30, _migration_29_to_30),
    (31, _migration_30_to_31),
    (32, _migration_31_to_32),
    (33, _migration_32_to_33),
]

def initialize_database(db_path: str, *, legacy_chain: bool = False):
    """
    Initializes the SQLite database and creates the workshop_items table and indexes.

    This is also the one place the journal mode is set. WAL is a persistent
    property of the file rather than of a connection, so establishing it here
    covers every later ``get_connection`` -- the daemon, the TUI and the web
    runner all call this before they read or write. Setting it here rather than
    per connection matters: a journal-mode transition needs a moment where
    nothing else holds a lock, which the connection's busy timeout does not
    wait out, and a reader that only wants a row must not risk it.

    The path is chosen by the database's *recorded version*, never by whether
    the file exists:

    - a **fresh** database (``user_version = 0``) is built directly at
      :data:`EXPECTED_VERSION` by :func:`_create_current_schema`, with no
      migrations replayed;
    - a fresh database with ``legacy_chain=True`` takes the historical shape
      from :func:`_create_legacy_schema` and runs the whole ``MIGRATIONS``
      table, exactly as every database did before the current-schema path
      existed -- this is how the chain stays exercised;
    - an **existing** database (``user_version > 0``) always runs
      :func:`_create_legacy_schema` followed by its pending migrations, whatever
      ``legacy_chain`` says, because the chain is the only thing that can carry
      it forward.

    ``_ensure_indexes`` runs last on every path.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")

    # Schema versioning: run migrations cumulatively from current to expected version
    db_version = cursor.execute("PRAGMA user_version").fetchone()[0]
    logging.info(f"Database schema version: {db_version} (expected: {EXPECTED_VERSION})")

    if db_version == 0 and not legacy_chain:
        _create_current_schema(cursor, conn)
    else:
        _create_legacy_schema(cursor, conn)
        for version, migrate in MIGRATIONS:
            if db_version < version:
                migrate(cursor, conn, db_path)

    _ensure_indexes(cursor)
    conn.commit()
    conn.close()


def toggle_subscription_queue(db_path: str, workshop_id: int):
    """Toggles the subscription queue status for a workshop item."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    # Use NOT to flip the boolean value (0 to 1, 1 to 0)
    cursor.execute(
        "UPDATE workshop_items SET is_queued_for_subscription = NOT is_queued_for_subscription WHERE workshop_id = ?",
        (workshop_id,)
    )
    conn.commit()
    conn.close()

def clear_subscription_queue(db_path: str, workshop_id: int):
    """Explicitly clears the subscription queue flag for a workshop item."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET is_queued_for_subscription = 0 WHERE workshop_id = ?",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def mark_own_subscribed(db_path: str, workshop_id: int, seen_at: int | None = None) -> bool:
    """Record that the owner is subscribed to ``workshop_id`` right now.

    ``own_subscribed`` is set (idempotently), and ``own_first_subscribed_at`` is
    stamped only when it is still NULL: it is deliberately sticky, because it is
    the sole source of the "we have seen this subscribed" state and must not move
    on a later re-subscription.

    The subscription queue flag is cleared in the same statement. There is
    nothing pending for an item that is now subscribed, and doing it here rather
    than as a separate call is what keeps ``/api/subscribed`` from having to
    remember two writes when it already knows the answer.

    Returns True only when this call was the one that set the timestamp, which is
    what makes the first-seen claim testable.
    """
    if seen_at is None:
        seen_at = int(time.time())
    conn = get_connection(db_path)
    try:
        # Read the stored timestamp *before* the update rather than comparing
        # afterwards: SQLite reports matched rows, not changed ones, so a
        # rowcount alone cannot say whether this call was the first to see it.
        row = conn.execute(
            "SELECT own_first_subscribed_at FROM workshop_items WHERE workshop_id = ?",
            (workshop_id,)
        ).fetchone()
        if row is None:
            return False
        stamped = row[0] is None
        conn.execute(
            "UPDATE workshop_items SET own_subscribed = 1, "
            "is_queued_for_subscription = 0, "
            "own_first_subscribed_at = COALESCE(own_first_subscribed_at, ?) "
            "WHERE workshop_id = ?",
            (seen_at, workshop_id)
        )
        conn.commit()
        return stamped
    finally:
        conn.close()


def apply_own_subscriptions(db_path: str, appid: int, subscribed_ids,
                            seen_at: int | None = None, complete: bool = True) -> dict:
    """Reconcile one app's ``own_subscribed`` flags against a subscription list.

    ``subscribed_ids`` is the list of the owner's subscriptions for ``appid`` a
    page walk collected. When ``complete`` is True it must be the *whole* list,
    because the complement -- every other item of the app -- is then cleared back
    to not-subscribed. The caller is responsible for establishing that
    completeness (see ``src.subscription_sync``).

    Every id on the list is written with ``mark_own_subscribed``'s contract,
    whether the read is complete or partial: marked subscribed, its first-seen
    stamp set if it is still NULL, and its ``is_queued_for_subscription`` flag
    cleared -- there is nothing pending for an item the walk saw subscribed, so
    leaving the flag set would list it for ever and cost a page read a pass. The
    queue clear is bounded to the ids the walk saw; an item it did not see keeps
    its queue flag.

    When ``complete`` is False the list is treated as a partial observation and
    only the one-way facts are applied: items on it are marked subscribed, their
    first-seen stamp is set if it is still NULL, and their queue flag is cleared.
    Nothing is cleared, because a list whose completeness could not be verified
    is no evidence about the items it omits -- and wrongly clearing
    ``own_subscribed`` would turn a live subscription into ``previously``.

    **The downloaded latch is cleared here and only here.** ``steam_download_seen_at`` is
    set by one writer (``src.workshop_folders``, which only ever stamps) and
    cleared by one event: the item leaving the owner's subscription list, in the
    same transaction that clears ``own_subscribed`` below. A missing folder, an
    unplugged drive or a moved library must never take the green star away, and a
    confirmed item is never revisited by the scan, so this is the sole clearing
    path. Re-subscribing re-earns the stamp on the next scan, because the files
    are usually still on disk.

    Returns ``{"subscribed", "stamped", "cleared", "queued_cleared",
    "downloads_cleared"}`` counts so the daemon can log what a sync actually did.
    """
    if seen_at is None:
        seen_at = int(time.time())
    ids = [int(wid) for wid in subscribed_ids]

    conn = get_connection(db_path)
    try:
        if ids:
            placeholders = ",".join("?" * len(ids))
            queued_cleared = conn.execute(
                f"UPDATE workshop_items SET is_queued_for_subscription = 0 "
                f"WHERE workshop_id IN ({placeholders}) AND is_queued_for_subscription = 1",
                ids
            ).rowcount
            stamped = conn.execute(
                f"UPDATE workshop_items SET own_first_subscribed_at = ? "
                f"WHERE workshop_id IN ({placeholders}) AND own_first_subscribed_at IS NULL",
                [seen_at, *ids]
            ).rowcount
            conn.execute(
                f"UPDATE workshop_items SET own_subscribed = 1 WHERE workshop_id IN ({placeholders})",
                ids
            )
        else:
            queued_cleared = stamped = 0

        if not complete:
            conn.commit()
            return {
                "subscribed": len(ids),
                "stamped": stamped,
                "cleared": 0,
                "queued_cleared": queued_cleared,
                "downloads_cleared": 0,
            }

        # The complement of the list. Scoped to this appid so a sync for one app
        # can never clear another app's flags.
        #
        # steam_download_seen_at is cleared in the same transaction as own_subscribed --
        # leaving the subscription list is the one event that takes the green
        # star away. The separate statement is what makes the count of cleared
        # latches measurable; the two run in one transaction, so a reader never
        # sees a subscribed item whose latch is already gone.
        if ids:
            placeholders = ",".join("?" * len(ids))
            downloads_cleared = conn.execute(
                f"UPDATE workshop_items SET steam_download_seen_at = NULL "
                f"WHERE consumer_appid = ? AND own_subscribed = 1 "
                f"AND steam_download_seen_at IS NOT NULL AND workshop_id NOT IN ({placeholders})",
                [appid, *ids]
            ).rowcount
            cleared = conn.execute(
                f"UPDATE workshop_items SET own_subscribed = 0 "
                f"WHERE consumer_appid = ? AND own_subscribed = 1 "
                f"AND workshop_id NOT IN ({placeholders})",
                [appid, *ids]
            ).rowcount
        else:
            downloads_cleared = conn.execute(
                "UPDATE workshop_items SET steam_download_seen_at = NULL "
                "WHERE consumer_appid = ? AND own_subscribed = 1 "
                "AND steam_download_seen_at IS NOT NULL",
                (appid,)
            ).rowcount
            cleared = conn.execute(
                "UPDATE workshop_items SET own_subscribed = 0 "
                "WHERE consumer_appid = ? AND own_subscribed = 1",
                (appid,)
            ).rowcount

        conn.commit()
    finally:
        conn.close()

    return {
        "subscribed": len(ids),
        "stamped": stamped,
        "cleared": cleared,
        "queued_cleared": queued_cleared,
        "downloads_cleared": downloads_cleared,
    }


def get_subscription_queue_items(db_path: str) -> list[dict]:
    """Retrieves all items currently queued for subscription.

    The subscription columns travel with the row so each front end can render
    the shared state from ``src/subscription.py``: the TUI's queue screen draws
    each row's real marker from them, and the web overlay renders the same
    marker from the same payload.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT workshop_id, title, title_en, is_queued_for_subscription, "
        "own_subscribed, own_first_subscribed_at, steam_download_seen_at "
        "FROM workshop_items WHERE is_queued_for_subscription = 1 ORDER BY title"
    )
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items


def get_subscription_states(db_path: str, workshop_ids) -> dict[int, dict]:
    """The shared subscription columns for a batch of items, keyed by id.

    The TUI's list poll re-reads only the rendered rows whose marker is still
    ``pending``; one query answers the whole batch, the same way the web grid's
    poll reads its rows back through one ``/api/items`` call, so the poll costs
    one read rather than one per row. The four columns are exactly the inputs
    ``src.subscription.subscription_state`` resolves.
    """
    ids = [int(wid) for wid in workshop_ids]
    if not ids:
        return {}
    conn = get_connection(db_path)
    try:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT workshop_id, own_subscribed, is_queued_for_subscription, "
            f"own_first_subscribed_at, steam_download_seen_at FROM workshop_items "
            f"WHERE workshop_id IN ({placeholders})",
            ids,
        ).fetchall()
    finally:
        conn.close()
    return {row["workshop_id"]: dict(row) for row in rows}

def insert_or_update_item(db_path: str, item_data: dict) -> bool:
    """
    Inserts a new item or updates an existing item.
    Returns True if a new item was discovered (inserted), False if it was updated.
    Tags are processed into the workshop_tags junction table and removed from
    the workshop_items column list (the JSON column no longer exists).
    """
    conn = get_connection(db_path)

    # Process tags into junction table before the main write
    if "tags" in item_data:
        try:
            tags_raw = item_data["tags"]
            tag_names = []
            if isinstance(tags_raw, list):
                tag_names = [t.get("tag") if isinstance(t, dict) else str(t) for t in tags_raw]
            elif isinstance(tags_raw, str):
                try:
                    parsed = json.loads(tags_raw)
                    if isinstance(parsed, list):
                        tag_names = [t.get("tag") if isinstance(t, dict) else str(t) for t in parsed]
                except (json.JSONDecodeError, TypeError) as json_exc:
                    # Handle Python-repr strings like "['fruit', 'sweet']"
                    import ast
                    try:
                        parsed = ast.literal_eval(tags_raw)
                        if isinstance(parsed, list):
                            tag_names = [t.get("tag") if isinstance(t, dict) else str(t) for t in parsed]
                    except (ValueError, SyntaxError) as exc:
                        pass
                        # Both parses failed: the item is written with no tags, so keep
                        # the unparseable payload for a regression test. Never raises.
                        from src import capture
                        capture.record_failure(
                            kind="api_unparseable_tags", stage="item_write",
                            workshop_id=item_data.get("workshop_id"),
                            body=tags_raw, content_type="application/json",
                            context={"errors": [f"json: {json_exc}", f"literal_eval: {exc}"]},
                        )
            if tag_names:
                tag_ids = _ensure_tag_ids(db_path, tag_names)
                conn.execute("DELETE FROM workshop_tags WHERE workshop_id = ?", (item_data["workshop_id"],))
                for tid in tag_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO workshop_tags (workshop_id, tag_id) VALUES (?, ?)",
                        (item_data["workshop_id"], tid)
                    )
        except Exception:
            logging.warning("Failed to sync tags for item %s", item_data.get("workshop_id"))
            pass
    
    columns = [col for col in item_data.keys() if col in WORKSHOP_ITEM_COLUMNS]

    cursor = conn.execute("SELECT 1 FROM workshop_items WHERE workshop_id = ?", (item_data["workshop_id"],))
    is_new = cursor.fetchone() is None

    if is_new and not item_data.get("first_seen_at"):
        item_data["first_seen_at"] = int(time.time())
        columns = [col for col in item_data.keys() if col in WORKSHOP_ITEM_COLUMNS]

    placeholders = ",".join(["?"] * len(columns))
    # Build the values list using the FILTERED column order
    values = [item_data[col] for col in columns]

    # We update all columns EXCEPT the primary key if there's a conflict
    update_cols = [col for col in columns if col != "workshop_id"]

    if not update_cols:
        sql = f"""
            INSERT INTO workshop_items ({",".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(workshop_id) DO NOTHING
        """
    else:
        updates = ",".join([f"{col}=excluded.{col}" for col in update_cols])
        sql = f"""
            INSERT INTO workshop_items ({",".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(workshop_id) DO UPDATE SET {updates}
        """

    conn.execute(sql, values)
    conn.commit()
    conn.close()
    return is_new

# ── Stage-handoff consumer predicates ─────────────────────────────────────────
#
# The five stage handoffs are enumerated in docs/data-pipeline.md. Each stage
# hands an item on by writing the column the next stage's own query selects on,
# and the two halves used to live only in the producer and the consumer -- so a
# write the consumer could not see looked like success. These functions name the
# consumer's half once. The worker poll interpolates the fragment into its SQL
# and the handoff contract tests (tests/test_handoff_contract.py) interpolate the
# same fragment scoped to one row, so the two cannot drift; a test that restated
# the SQL could not notice the worker's copy changing.
#
# Each returns a WHERE fragment and nothing else. Ordering and the limit belong
# to the selector, never here: those are the hot path of four stages and are
# covered by tests/test_queue_indexes.py against the partial indexes.

def api_fetch_queue_predicate() -> str:
    """Discovery → API fetch: the fetch queue's entry condition.

    Queued at a positive priority and not dead. A discovered row whose
    ``api_priority`` was left at the column default is issue 20: in no queue at
    all.
    """
    return "api_priority > 0 AND (fetch_status IS NULL OR fetch_status != -1)"


def web_scrape_queue_predicate() -> str:
    """API fetch → web scrape: the web scrape queue's entry condition.

    The producer is ``_raise_scrape_and_image_priorities``; the worker's own success test is
    that it stored a description, not merely that it cleared the flag, which is
    issue 19's shape.
    """
    return "web_scrape_priority > 0"


def image_queue_predicate() -> str:
    """API fetch → image: the image queue's entry condition.

    The producer is ``_raise_scrape_and_image_priorities``; ``image_answer`` records the
    answer, so a permanent 404 or a non-image type also settles the stage.
    """
    return "image_priority > 0"


def translation_queue_predicate() -> str:
    """API fetch and web scrape → translation: the translation poll's condition.

    The poll hands out every row of ``translation_queue``: a row is outstanding
    by virtue of existing, and the producer's counterpart (the translator)
    deletes it when the text is stored. The fragment is deliberately vacuous
    because that is the consumer's real predicate; naming it keeps the poll and
    its contract test asking one question.
    """
    return "1"


def translation_priority_predicate() -> str:
    """The item-level reading of the translation queue's mirror flag.

    ``queue_field_for_translation`` raises ``translation_priority`` with MAX when
    it queues a field and the translator clears it when the queue empties, so
    the mirror is the row-level answer to "queued for translation" that the
    handoff invariant (``queued_nowhere``/``dead_queued``) counts. The worker's
    own question is the queue row; this is the row's.
    """
    return "translation_priority > 0"


def queued_anywhere_predicate() -> str:
    """Any stage → dead: the union a dead item has to fail.

    ``_settle_api_failure`` clears all four flags when it writes ``fetch_status = -1``;
    the web, image and translation polls have no dead-item guard of their own, so
    this union is how "in no queue" is stated at the item level. It is built from
    the named queue predicates so a change to one of them moves this with it.
    """
    return " OR ".join(
        f"({predicate})"
        for predicate in (
            api_fetch_queue_predicate(),
            web_scrape_queue_predicate(),
            image_queue_predicate(),
            translation_priority_predicate(),
        )
    )


def get_next_items_to_fetch(db_path: str, limit: int = 10) -> list[dict]:
    """
    Retrieves the next batch of workshop items to be scraped.
    Prioritizes by api_priority (higher = more urgent), then oldest api_fetched_at
    (NULLs first: never-successfully-fetched items come first).
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    sql = f"""
        SELECT * FROM workshop_items
        WHERE {api_fetch_queue_predicate()}
        ORDER BY api_priority DESC, api_fetched_at ASC
        LIMIT ?
    """
    cursor.execute(sql, (limit,))
    
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items

def count_never_fetched_items(db_path: str) -> int:
    """Returns the number of items that have never been fetched via API (api_fetched_at is NULL)."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT COUNT(workshop_id) as count FROM workshop_items WHERE api_fetched_at IS NULL")
    row = cursor.fetchone()
    conn.close()
    return row["count"] if row else 0


def count_fetchable_items(db_path: str) -> int:
    """Returns how many items the API fetch queue can actually hand out.

    This is the population ``get_next_items_to_fetch`` selects: queued and not
    dead. It is deliberately distinct from ``count_never_fetched_items``, which
    counts items never successfully fetched regardless of whether they are
    queued. Those two populations do not overlap, and treating the second as a
    measure of the first is how discovery came to be suppressed permanently
    while the fetch queue held a single item.
    """
    conn = get_connection(db_path)
    cursor = conn.execute(
        "SELECT COUNT(workshop_id) as count FROM workshop_items "
        f"WHERE {api_fetch_queue_predicate()}"
    )
    row = cursor.fetchone()
    conn.close()
    return row["count"] if row else 0

def insert_or_update_creator(db_path: str, user_data: dict):
    """Inserts or updates a creator in the creators table."""
    conn = get_connection(db_path)
    columns = [col for col in user_data.keys() if col in CREATOR_COLUMNS]
    placeholders = ",".join(["?"] * len(columns))
    updates = ",".join([f"{col}=excluded.{col}" for col in columns if col != "steamid"])
    
    sql = f"""
        INSERT INTO creators ({",".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(steamid) DO UPDATE SET {updates}
    """
    conn.execute(sql, list(user_data.values()))
    conn.commit()
    conn.close()

def get_creator(db_path: str, steamid: int) -> dict | None:
    """Fetches a creator by steamid."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT * FROM creators WHERE steamid = ?", (steamid,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def _parse_query(query: str) -> tuple[list[str], list[str]]:
    """
    Parses a query string into positive and negative tokens.
    Respects quotes for phrases.
    Example: 'Apple -Banana -"Rotten Core"' -> (['Apple'], ['Banana', 'Rotten Core'])
    """
    if not query:
        return [], []
    
    try:
        tokens = shlex.split(query)
    except ValueError:
        # Fallback if quotes are mismatched
        tokens = query.split()

    positive = []
    negative = []
    for token in tokens:
        if token.startswith('-') and len(token) > 1:
            negative.append(token[1:])
        else:
            positive.append(token)
    return positive, negative

def _apply_numeric_filter(sql: str, params: list, col: str, filter_value: str) -> tuple[str, list]:
    """Parses operators from a string and applies them to the SQL."""
    match = re.match(r'^\s*([<>!=]=?|>|<)?\s*(\d+(?:\.\d+)?)\s*$', str(filter_value))
    if match:
        op = match.group(1) or '='
        val = float(match.group(2))
        sql += f" AND {col} {op} ?"
        params.append(val)
    return sql, params

def get_item_details(db_path: str, workshop_id: int) -> dict | None:
    """Fetches all columns for a single workshop item, joined with user info."""
    conn = get_connection(db_path)
    sql = """
        SELECT w.*, u.personaname, u.personaname_en, u.translated_at as user_translated_at,
               (SELECT GROUP_CONCAT(t.tag_name, ', ') FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id = w.workshop_id) as tags
        FROM workshop_items w
        LEFT JOIN creators u ON w.creator_steamid = u.steamid
        WHERE w.workshop_id = ?
    """
    cursor = conn.execute(sql, (workshop_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def search_items(db_path: str, query: str = "", appid: int = None, 
                 title_query: str = "", desc_query: str = "", filename_query: str = "",
                 tags: str = "", filters: list[dict] = None,
                 creator: str = "", numeric_filters: dict = None, 
                 summary_only: bool = False, 
                 sort_by: str = None, sort_order: str = "ASC",
                 limit: int = None, offset: int = None,
                 subscribed_overlay: str = None) -> list[dict]:
    """
    Searches the database for items matching the criteria.
    Joins with the creators table to provide names.
    If summary_only is True, returns only essential columns for list view display.

    ``subscribed_overlay`` is the view control's value, ANDed onto the builder's
    rows as one extra clause. It is a separate argument rather than an appended
    filter on purpose: it must sit outside the builder's parenthesised group, so
    an OR row cannot absorb it, and it must never be mistaken for a row the
    "Save Filter for Scraper" action writes.
    """
    conn = get_connection(db_path)
    
    if summary_only:
        cols = ("w.workshop_id, w.title, w.title_en, w.creator_steamid, w.consumer_appid, "
                "w.translate_version, w.is_queued_for_subscription, w.web_scrape_priority, "
                "w.image_priority, w.translation_priority, w.file_size, w.image_answer, "
                "w.wilson_subscription_score, w.wilson_favorite_score, "
                # Both subscription columns travel with the list rows: the grid
                # draws its marker from this payload, and a cell that had
                # own_subscribed without own_first_subscribed_at could not tell
                # `never` from `previously` after the marker was toggled.
                # steam_download_seen_at travels too, or the grid could not draw the
                # `downloaded` state for a row that is already subscribed.
                "w.own_subscribed, w.own_first_subscribed_at, w.steam_download_seen_at, "
                "u.personaname, u.personaname_en,"
                "(SELECT GROUP_CONCAT(t.tag_name, ', ') FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id = w.workshop_id) as tags")
    else:
        cols = ("w.*, u.personaname, u.personaname_en,"
                "(SELECT GROUP_CONCAT(t.tag_name, ', ') FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id = w.workshop_id) as tags")
        
    sql = f"SELECT {cols} FROM workshop_items w LEFT JOIN creators u ON w.creator_steamid = u.steamid WHERE 1=1"
    params = []

    if query:
        sql, params = _build_text_search_clauses(sql, params, query, ["title", "short_description", "extended_description"])
    if title_query:
        sql, params = _build_text_search_clauses(sql, params, title_query, ["title"])
    if desc_query:
        sql, params = _build_text_search_clauses(sql, params, desc_query, ["short_description", "extended_description"])
    if filename_query:
        sql, params = _build_text_search_clauses(sql, params, filename_query, ["filename"])
    if tags:
        clause, clause_params = _build_tag_clause("contains", tags)
        sql += f" AND {clause}"
        params.extend(clause_params)

    if creator:
        sql += " AND creator_steamid = ?"
        params.append(creator)
        
    if appid is not None:
        sql += " AND consumer_appid = ?"
        params.append(appid)

    if numeric_filters:
        valid_cols = {"file_size", "subscriptions", "favorited", "views"}
        for col, filter_value in numeric_filters.items():
            if col in valid_cols and filter_value:
                sql, params = _apply_numeric_filter(sql, params, col, filter_value)

    if filters:
        pct_filters = [f for f in filters if f.get("op") == "percentile"]
        regular_filters = [f for f in filters if f.get("op") != "percentile"]

        # One shared translation (see `build_filters_sql`), so the SQL the
        # search runs and the SQL the coverage metric runs cannot drift apart.
        group_sql, group_params = build_filters_sql(regular_filters)
        if group_sql:
            sql += f" AND ({group_sql})"
            params.extend(group_params)

        for f in pct_filters:
            field = f.get("field")
            val = f.get("value")
            if not field or not val:
                continue
            db_col = FILTER_FIELD_TO_COLUMN.get(field, field)
            if db_col == "tags":
                continue
            threshold = _compute_percentile_threshold(db_path, db_col, val, regular_filters)
            if threshold is not None:
                sql += f" AND w.{db_col} >= {threshold}"

    # The overlay is ANDed after the builder's group, and outside it, so a row
    # whose logic is OR cannot pull a row back in that the overlay excluded.
    overlay_clause, overlay_params = subscribed_overlay_clause(subscribed_overlay)
    if overlay_clause:
        sql += f" AND ({overlay_clause})"
        params.extend(overlay_params)

    sort_sql = _build_sort_clause(sort_by, sort_order) if sort_by else ""
    sql += sort_sql
    limit_sql, limit_params = _build_limit_offset(limit, offset) if limit is not None else ("", [])
    sql += limit_sql
    params.extend(limit_params)
        
    cursor = conn.execute(sql, params)
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results

def get_all_creator_ids(db_path: str) -> list[str]:
    """Returns a list of all unique creator IDs currently in the database."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT DISTINCT creator_steamid FROM workshop_items WHERE creator_steamid IS NOT NULL ORDER BY creator_steamid")
    results = [row["creator_steamid"] for row in cursor.fetchall()]
    conn.close()
    return results

def _compute_tag_frequencies(cursor) -> dict:
    """Returns a {tag_name: count} frequency dictionary from the junction table.

    The statistics screen gets the same data from the `tag_counts` metric; this
    stays because `compact_tag_ids` needs frequencies while already holding a
    cursor, and opening a second connection there would be worse.
    """
    cursor.execute("""
        SELECT t.tag_name, COUNT(*) as cnt
        FROM workshop_tags wt JOIN tags t USING(tag_id)
        GROUP BY t.tag_name ORDER BY cnt DESC
    """)
    return {row["tag_name"]: row["cnt"] for row in cursor.fetchall()}

_LEGACY_STAT_KEYS = (
    ("status_counts", "status_counts"),
    ("translation_status", "translation_status"),
    ("tag_counts", "tag_counts"),
    ("fetch_recency_counts", "fetch_recency"),
    ("highest_api_fetched_at", "high_water"),
    ("app_stats", "app_discovery"),
    ("priority_breakdowns", "priority_breakdowns"),
)


def get_db_stats(db_path: str, staleness_days: int = 30) -> dict:
    """Every statistic in one dict, under the key names its callers already use.

    Kept as the single-dict entry point for callers that want the lot. A caller
    that needs only part of it should use `metrics.compute` with the names it
    wants, or `metrics.iter_metrics` to take them one at a time: this computes
    every metric, which is how the tag endpoint came to spend about five seconds
    producing a payload it threw away.
    """
    # Imported here rather than at module scope: metrics imports this module for
    # its connection helper, so a top-level import would be circular.
    from src import metrics

    result = metrics.compute(
        db_path,
        [metric_name for _, metric_name in _LEGACY_STAT_KEYS],
        {"staleness_days": staleness_days},
    )
    return {key: result[metric_name]["value"] for key, metric_name in _LEGACY_STAT_KEYS}


def compute_wilson_cutoffs(db_path: str, filters: list[dict] = None,
                           subscribed_overlay: str = None) -> dict:
    """Returns percentile cutoff scores for Wilson metrics across items matching filters.
    Uses NTILE(100) — returns p99, p90, p50 thresholds for both scores.
    Returns empty dict if fewer than 10 items in the filtered set.

    ``subscribed_overlay`` follows :func:`search_items`: the overlay constrains
    the same population the grid shows, so the percentiles must be computed over
    it too or the colours would describe a different set of rows.
    """
    conn = get_connection(db_path)
    sql = "SELECT w.workshop_id, w.wilson_favorite_score, w.wilson_subscription_score FROM workshop_items w"
    params = []
    if filters:
        filter_clauses = []
        for f in filters:
            if f.get("op") == "percentile":
                continue
            if FILTER_FIELD_TO_COLUMN.get(f.get("field", "")) == "full_text":
                continue  # FTS5 MATCH can't be applied to Wilson score computation
            field = f.get("field")
            op = f.get("op")
            val = f.get("value")
            if not field or not op:
                continue
            db_col = FILTER_FIELD_TO_COLUMN.get(field, field)
            if db_col == "tags":
                if op in ("is", "is_not"):
                    continue
                clause, clause_params = _build_tag_clause(op, val)
            else:
                clause, clause_params = _build_single_filter_clause(db_col, op, val)
            if clause:
                params.extend(clause_params)
                filter_clauses.append((f.get("logic", "AND").upper(), clause))
        if filter_clauses:
            sql += " WHERE "
            for idx, (logic, clause) in enumerate(filter_clauses):
                sql += f" {logic} " if idx > 0 else ""
                sql += clause

    overlay_clause, overlay_params = subscribed_overlay_clause(subscribed_overlay)
    if overlay_clause:
        sql += (" AND " if " WHERE " in sql else " WHERE ") + f"({overlay_clause})"
        params.extend(overlay_params)

    cutoff_sql = f"""
        WITH base AS (
            {sql}
            ORDER BY workshop_id
        ),
        scores AS (
            SELECT wilson_favorite_score, wilson_subscription_score FROM base
        ),
        fav_ntile AS (
            SELECT wilson_favorite_score,
                   NTILE(100) OVER (ORDER BY wilson_favorite_score DESC NULLS LAST) AS bucket
            FROM scores WHERE wilson_favorite_score IS NOT NULL
        ),
        sub_ntile AS (
            SELECT wilson_subscription_score,
                   NTILE(100) OVER (ORDER BY wilson_subscription_score DESC NULLS LAST) AS bucket
            FROM scores WHERE wilson_subscription_score IS NOT NULL
        )
        SELECT 'wilson_favorite_p99' as key, COALESCE(MIN(wilson_favorite_score), 0) as val
        FROM fav_ntile WHERE bucket = 1
        UNION ALL SELECT 'wilson_favorite_p90', COALESCE(MIN(wilson_favorite_score), 0)
        FROM fav_ntile WHERE bucket = 10
        UNION ALL SELECT 'wilson_favorite_p50', COALESCE(MIN(wilson_favorite_score), 0)
        FROM fav_ntile WHERE bucket = 50
        UNION ALL SELECT 'wilson_subscription_p99', COALESCE(MIN(wilson_subscription_score), 0)
        FROM sub_ntile WHERE bucket = 1
        UNION ALL SELECT 'wilson_subscription_p90', COALESCE(MIN(wilson_subscription_score), 0)
        FROM sub_ntile WHERE bucket = 10
        UNION ALL SELECT 'wilson_subscription_p50', COALESCE(MIN(wilson_subscription_score), 0)
        FROM sub_ntile WHERE bucket = 50
        UNION ALL SELECT 'wilson_favorite_min', COALESCE(MIN(wilson_favorite_score), 0)
        FROM fav_ntile WHERE bucket = 100
        UNION ALL SELECT 'wilson_favorite_max', COALESCE(MAX(wilson_favorite_score), 0)
        FROM fav_ntile WHERE bucket = 1
        UNION ALL SELECT 'wilson_subscription_min', COALESCE(MIN(wilson_subscription_score), 0)
        FROM sub_ntile WHERE bucket = 100
        UNION ALL SELECT 'wilson_subscription_max', COALESCE(MAX(wilson_subscription_score), 0)
        FROM sub_ntile WHERE bucket = 1
    """
    try:
        cursor = conn.execute(cutoff_sql, params)
        result = {row["key"]: row["val"] for row in cursor.fetchall()}
        conn.close()
        return result
    except Exception:
        logging.exception("compute_wilson_cutoffs failed")
        conn.close()
        return {}

def get_app_tracking(db_path: str, appid: int) -> dict | None:
    """
    Returns the AppID discovery data for a given appid, including scan date and filters.
    Returns a dictionary of all columns if found, otherwise None.
    """
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT * FROM app_discovery WHERE appid = ?", (appid,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_next_web_scrape_item(db_path: str) -> dict | None:
    """Returns the highest-priority item needing web scraping, or None."""
    conn = get_connection(db_path)
    cursor = conn.execute(f"""
        SELECT * FROM workshop_items
        WHERE {web_scrape_queue_predicate()}
        ORDER BY web_scrape_priority DESC, api_fetched_at ASC
        LIMIT 1
    """)
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def raise_web_scrape_priority(db_path: str, workshop_id: int, priority: int):
    """Sets web_scrape_priority to MAX(current, priority). Never downgrades."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET web_scrape_priority = MAX(web_scrape_priority, ?) WHERE workshop_id = ?",
        (priority, workshop_id)
    )
    conn.commit()
    conn.close()


def raise_web_scrape_priority_for_list(db_path: str, workshop_id: int):
    """Bumps web scrape priority to 5 for list items if currently < 5 and > 0."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET web_scrape_priority = 5 "
        "WHERE workshop_id = ? AND web_scrape_priority > 0 AND web_scrape_priority < 5",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def raise_web_scrape_priority_for_detail(db_path: str, workshop_id: int):
    """Bumps web scrape priority to 10 for detail items if currently < 10 and > 0."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET web_scrape_priority = 10 "
        "WHERE workshop_id = ? AND web_scrape_priority > 0 AND web_scrape_priority < 10",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def get_next_image_item(db_path: str) -> dict | None:
    """Returns the highest-priority item needing image download, or None."""
    conn = get_connection(db_path)
    cursor = conn.execute(f"""
        SELECT * FROM workshop_items
        WHERE {image_queue_predicate()}
        ORDER BY image_priority DESC, api_fetched_at ASC
        LIMIT 1
    """)
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def raise_image_priority(db_path: str, workshop_id: int, priority: int):
    """Sets image_priority to MAX(current, priority). Never downgrades."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET image_priority = MAX(image_priority, ?) WHERE workshop_id = ?",
        (priority, workshop_id)
    )
    conn.commit()
    conn.close()


def raise_image_priority_for_list(db_path: str, workshop_id: int):
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET image_priority = 5 "
        "WHERE workshop_id = ? AND image_priority > 0 AND image_priority < 5",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def raise_image_priority_for_detail(db_path: str, workshop_id: int):
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET image_priority = 10 "
        "WHERE workshop_id = ? AND image_priority > 0 AND image_priority < 10",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def translation_is_current(translated_text, translate_version, steam_updated_at) -> bool:
    """Whether a stored translation still matches the item's current revision.

    ``translate_version`` records the item's ``steam_updated_at`` at the moment
    the translation was stored (see ``TranslatorThread._translate_batch``), so a
    translation is current when it exists and was taken at the item's current
    Steam revision.

    The background paths use this to decide whether to re-queue a field. Without
    it they re-translate unchanged text on every staleness sweep; with only an
    "is it translated" check, a genuine source edit would never refresh.

    Unknown provenance (``translate_version`` NULL) counts as stale. That costs
    one re-translation per row and is currently empty in the live database
    (0 of 142,748 translated titles). Items with no Steam revision at all
    (``steam_updated_at`` NULL) cannot have a change detected, so their
    translations are treated as current rather than re-translated forever.
    """
    if not translated_text:
        return False
    if steam_updated_at is None:
        return True
    if translate_version is None:
        return False
    return translate_version >= steam_updated_at


def queue_field_for_translation(db_path: str, item_type: str, item_id: int, field: str, text: str, priority: int):
    """Inserts a field into translation_queue, or bumps its priority. Never downgrades.
    Also bumps translation_priority on the parent item/user table.

    The queue row and the parent mirror are written in ONE transaction on ONE
    connection. Splitting them was a live defect: the translator drains the
    queue on its own thread, so a drain landing between the two writes deleted
    the row and zeroed the mirror, and the second write then raised the mirror
    again with nothing queued behind it. The item read as permanently pending
    and no producer would re-queue it, because the translation it now had was
    current. Holding the write lock across both statements makes the pair
    atomic: a drain either happens before the row is re-queued (mirror raised,
    row present) or after it (row deleted, mirror zeroed).
    """
    if not text or text.isascii():
        return
    conn = get_connection(db_path)
    # Check if already exists
    existing = conn.execute(
        "SELECT id, priority FROM translation_queue WHERE item_type=? AND item_id=? AND field=?",
        (item_type, item_id, field)
    ).fetchone()
    if existing:
        if existing["priority"] < priority:
            conn.execute(
                "UPDATE translation_queue SET priority=? WHERE id=?",
                (priority, existing["id"])
            )
    else:
        # queued_at is our queue clock (Unix epoch INTEGER). It is written here
        # for NEW rows only; pre-v14 rows keep it NULL because we genuinely do
        # not know when they were queued. SQLite's NULL-first ascending order in
        # get_next_batch_for_translation keeps that legacy backlog ahead of
        # newly queued work.
        conn.execute(
            "INSERT INTO translation_queue (item_type, item_id, field, original_text, priority, queued_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_type, item_id, field, text, priority, int(time.time()))
        )
    # Sync translation_priority on the parent — use MAX so multiple fields
    # each set their priority without downgrading
    table = "creators" if item_type == "user" else "workshop_items"
    id_col = "workshop_id" if table == "workshop_items" else "steamid"
    conn.execute(
        f"UPDATE {table} SET translation_priority = MAX(translation_priority, ?) WHERE {id_col} = ?",
        (priority, item_id)
    )
    conn.commit()
    conn.close()


def raise_translation_priority_for_list(db_path: str, workshop_id: int):
    """For enriched items in the list view: flag non-ASCII fields at priority 5."""
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT title, title_en, short_description, short_description_en, extended_description, extended_description_en "
        "FROM workshop_items WHERE workshop_id=?",
        (workshop_id,)
    ).fetchone()
    conn.close()
    if not row:
        return
    for field, text, translated in [
        ("title_en", row["title"] or "", row["title_en"]),
        ("short_description_en", row["short_description"] or "", row["short_description_en"]),
        ("extended_description_en", row["extended_description"] or "", row["extended_description_en"]),
    ]:
        if text and not text.isascii() and not translated:
            queue_field_for_translation(db_path, "item", workshop_id, field, text, 5)


def raise_translation_priority_for_detail(db_path: str, workshop_id: int):
    """For detail view: flag ALL non-ASCII fields at priority 10, regardless of enrichment."""
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT title, title_en, short_description, short_description_en, extended_description, extended_description_en "
        "FROM workshop_items WHERE workshop_id=?",
        (workshop_id,)
    ).fetchone()
    conn.close()
    if not row:
        return
    for field, text, translated in [
        ("title_en", row["title"] or "", row["title_en"]),
        ("short_description_en", row["short_description"] or "", row["short_description_en"]),
        ("extended_description_en", row["extended_description"] or "", row["extended_description_en"]),
    ]:
        if text and not text.isascii() and not translated:
            queue_field_for_translation(db_path, "item", workshop_id, field, text, 10)


def get_next_batch_for_translation(db_path: str, limit: int = 20) -> list[dict]:
    """Returns up to `limit` highest-priority fields for translation.

    Ordering is ``priority DESC`` then oldest-queued first, with rows whose
    ``queued_at`` is unknown ahead of dated rows at the same priority: SQLite
    sorts NULL first in ascending order, so ``queued_at ASC`` already puts the
    legacy backlog ahead of newly queued work. The former
    ``queued_at IS NOT NULL`` term was redundant with that, and it prevented
    ``idx_translation_queue_poll`` from serving the sort because a term that
    matches no index forces a temp B-tree.
    """
    conn = get_connection(db_path)
    cursor = conn.execute(
        "SELECT * FROM translation_queue "
        f"WHERE {translation_queue_predicate()} "
        "ORDER BY priority DESC, queued_at ASC LIMIT ?",
        (limit,)
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def save_enrichment_filters(db_path: str, appid: int, filter_text: str = "", required_tags: list[str] = None,
                     excluded_tags: list[str] = None, enrichment_filters: str = None) -> None:
    """
    Saves the filter settings for a given appid in the app_discovery table.
    If enrichment_filters is provided (JSON string), it is used as the canonical filter spec.
    Legacy columns (filter_text, required_tags, excluded_tags) are kept for backward compat.
    """
    conn = get_connection(db_path)
    json_required_tags = json.dumps(required_tags) if required_tags is not None else '[]'
    json_excluded_tags = json.dumps(excluded_tags) if excluded_tags is not None else '[]'
    enrichment = enrichment_filters if enrichment_filters is not None else '[]'

    conn.execute(
        "INSERT INTO app_discovery (appid, filter_text, required_tags, excluded_tags, enrichment_filters) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET "
        "filter_text = excluded.filter_text, "
        "required_tags = excluded.required_tags, "
        "excluded_tags = excluded.excluded_tags, "
        "enrichment_filters = excluded.enrichment_filters",
        (appid, filter_text, json_required_tags, json_excluded_tags, enrichment)
    )
    conn.commit()
    conn.close()

def update_app_tracking(db_path: str, appid: int, last_date: int, window_size: int) -> None:
    """Updates the last_historical_date_scanned for a given appid."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO app_discovery (appid, last_historical_date_scanned, window_size) VALUES (?, ?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET last_historical_date_scanned = excluded.last_historical_date_scanned, window_size = excluded.window_size",
        (appid, last_date, window_size)
    )
    conn.commit()
    conn.close()

def update_app_tracking_cursor(db_path: str, appid: int, cursor: str) -> None:
    """Updates the last_cursor for a given appid."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO app_discovery (appid, last_cursor) VALUES (?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET last_cursor = excluded.last_cursor",
        (appid, cursor)
    )
    conn.commit()
    conn.close()

def delete_never_fetched_items(db_path: str) -> int:
    """
    Removes all workshop items that are 'pending' (never successfully scraped).
    Criteria: (fetch_status IS NULL OR fetch_status = 404) AND api_fetched_at IS NULL.
    Returns the number of rows deleted.
    """
    conn = get_connection(db_path)
    cursor = conn.execute(
        "DELETE FROM workshop_items WHERE (fetch_status IS NULL OR fetch_status = 404) AND api_fetched_at IS NULL"
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def raise_api_priority_for_list(db_path: str, workshop_id: int):
    """Bumps api_priority to 5 for list items if currently < 5."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET api_priority = 5 "
        "WHERE workshop_id = ? AND api_priority < 5 AND (fetch_status IS NULL OR fetch_status != -1)",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def raise_api_priority_for_detail(db_path: str, workshop_id: int):
    """Bumps api_priority to 10 for detail items if currently < 10."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET api_priority = 10 "
        "WHERE workshop_id = ? AND api_priority < 10 AND (fetch_status IS NULL OR fetch_status != -1)",
        (workshop_id,)
    )
    conn.commit()
    conn.close()
