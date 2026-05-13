import sqlite3
import shlex
import re
import json
import logging
from datetime import datetime, timedelta, timezone

WORKSHOP_ITEM_COLUMNS = frozenset({
    "workshop_id", "dt_found", "dt_updated", "dt_attempted", "dt_translated",
    "status", "title", "title_en", "creator", "creator_appid", "consumer_appid",
    "filename", "file_size", "preview_url", "hcontent_file", "hcontent_preview",
    "short_description", "short_description_en", "time_created", "time_updated",
    "visibility", "banned", "ban_reason", "app_name", "file_type",
    "subscriptions", "favorited", "views", "tags", "tags_text",
    "extended_description", "extended_description_en", "language",
    "lifetime_subscriptions", "lifetime_favorited", "translation_priority",
    "is_queued_for_subscription", "wilson_favorite_score",
    "wilson_subscription_score", "needs_web_scrape",
    "image_extension", "needs_image",
})

USER_COLUMNS = frozenset({
    "steamid", "personaname", "personaname_en",
    "dt_updated", "dt_translated", "translation_priority",
})

def get_connection(db_path: str):
    """
    Returns a SQLite connection with WAL mode enabled and Row factory.
    WAL allows simultaneous readers and writers.
    """
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
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

FIELD_NAME_MAP = {
    "Title": "title", "Description": "short_description", "Filename": "filename",
    "Tags": "tags", "Author ID": "creator", "File Size": "file_size",
    "Subs": "subscriptions", "Favs": "favorited", "Views": "views",
    "Workshop ID": "workshop_id", "AppID": "consumer_appid", "Language ID": "language",
    "Subscriber Score": "wilson_subscription_score", "Favorite Score": "wilson_favorite_score",
    "Full Text": "full_text",
}

# Fields that have a translated _en counterpart; these are dual-searched
# when the operator is a text-matching one (contains, is, etc.)
_EN_FIELDS = {
    "title": "title_en",
    "short_description": "short_description_en",
    "extended_description": "extended_description_en",
}

# Full Text search spans all these columns
_FULL_TEXT_COLS = [
    "title", "title_en",
    "short_description", "short_description_en",
    "extended_description", "extended_description_en",
]

# Operators that trigger dual-field (original + translated) search
_TEXT_OPS = {"contains", "does_not_contain", "is", "is_not"}
# Positive operators join with OR (match if either column matches);
# negative operators join with AND (match only if neither column matches).
_TEXT_NEG_OPS = {"does_not_contain", "is_not"}

VALID_SORT_COLS = {
    "title", "file_size", "subscriptions", "favorited", "views",
    "workshop_id", "time_created", "time_updated",
    "wilson_favorite_score", "wilson_subscription_score",
}

def _build_text_search_clauses(sql: str, params: list, q_str: str, cols: list[str]) -> tuple[str, list]:
    """Applies positive/negative text search tokens to SQL via LIKE clauses."""
    pos_tokens, neg_tokens = _parse_query(q_str)
    for token in pos_tokens:
        clauses = [f"{col} LIKE ?" for col in cols]
        sql += f" AND ({' OR '.join(clauses)})"
        params.extend([f"%{token}%"] * len(cols))
    for token in neg_tokens:
        for col in cols:
            sql += f" AND ({col} IS NULL OR {col} NOT LIKE ?)"
            params.append(f"%{token}%")
    return sql, params

def _build_filter_clause(db_col: str, op: str, val) -> tuple[str, list]:
    """Converts an operator and value into a SQL clause string and param list."""
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


def _compute_percentile_threshold(db_path: str, db_col: str, percentile_val, base_filters: list[dict] = None) -> float | None:
    """Computes the threshold score for items above the given percentile.
    percentile_val: 0-99 (clamped). 0 returns None (no filter).
    base_filters: non-percentile filters for the base dataset."""
    try:
        p = int(float(percentile_val)) if percentile_val is not None else 0
    except (ValueError, TypeError):
        return None
    p = max(0, min(99, p))
    if p == 0:
        return None

    tile = 100 - p
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
            f_db_col = FIELD_NAME_MAP.get(field, field)
            if f_db_col == "tags":
                if op in ("is", "is_not"):
                    continue
                clause, clause_params = _build_json_tag_clause(f_db_col, op, val)
            else:
                clause, clause_params = _build_filter_clause(f_db_col, op, val)
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
            FROM workshop_items
            {where_sql}
        ) WHERE tile = {tile}
    """
    threshold = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return threshold if threshold else None

def _build_json_tag_clause(db_col: str, op: str, val) -> tuple[str, list]:
    """Uses json_each for JSON array columns (tags).
    Contains/does_not_contain use exact tag value match (not substring),
    since tag names are discrete identifiers, not free text."""
    if op == "contains":
        return (f"EXISTS (SELECT 1 FROM json_each({db_col}) WHERE value = ?)", [val])
    if op == "does_not_contain":
        return (f"NOT EXISTS (SELECT 1 FROM json_each({db_col}) WHERE value = ?)", [val])
    if op == "is_empty":
        return (f"({db_col} IS NULL OR {db_col} = '' OR {db_col} = '[]')", [])
    if op == "is_not_empty":
        return (f"({db_col} IS NOT NULL AND {db_col} != '' AND {db_col} != '[]')", [])
    return ("", [])

def _evaluate_single_filter(item: dict, db_col: str, op: str, val) -> bool:
    """Checks whether an in-memory item dict matches a single filter criterion."""
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
    except: pass

    if op == "contains":
        return val in tag_set
    if op == "does_not_contain":
        return val not in tag_set
    if op == "is_empty":
        return len(tag_set) == 0
    if op == "is_not_empty":
        return len(tag_set) > 0
    return True

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
        db_col = FIELD_NAME_MAP.get(field, field)
        if not _evaluate_single_filter(item, db_col, op, val):
            return False
    return True

def _build_sort_clause(sort_by: str, sort_order: str) -> str:
    """Returns an ORDER BY clause with whitelist validation."""
    if sort_by not in VALID_SORT_COLS:
        return ""
    order = "DESC" if sort_order.upper() == "DESC" else "ASC"
    return f" ORDER BY {sort_by} {order}"

def _build_limit_offset(limit: int, offset: int) -> tuple[str, list]:
    """Returns (LIMIT...OFFSET clause, params list)."""
    params = []
    if limit is not None:
        params.append(limit)
        if offset is not None:
            return f" LIMIT ? OFFSET ?", params + [offset]
        return f" LIMIT ?", params
    return "", []

def _safe_add_columns(cursor, table: str, columns: list[tuple[str, str]]):
    """Safely adds columns to an existing table, ignoring duplicate-column errors."""
    for col_name, col_type in columns:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise

def initialize_database(db_path: str):
    """
    Initializes the SQLite database and creates the workshop_items table and indexes.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS workshop_items (
        workshop_id INTEGER PRIMARY KEY,
        -- dt_* columns: daemon-managed timestamps (ISO 8601 TEXT)
        -- time_* columns: Steam-side timestamps (Unix INTEGER)
        dt_found TEXT,
        dt_updated TEXT,
        dt_attempted TEXT,
        status INTEGER,
        title TEXT,
        creator INTEGER, -- FK to users.steamid (LEFT JOIN used; FK omitted
                        --   because items are discovered before users are fetched)
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
        tags TEXT,
        extended_description TEXT,
        language INTEGER,
        lifetime_subscriptions INTEGER,
        lifetime_favorited INTEGER,
        title_en TEXT,
        short_description_en TEXT,
        extended_description_en TEXT,
        dt_translated TEXT,
        translation_priority INTEGER DEFAULT 0,
        wilson_favorite_score REAL DEFAULT NULL,
        wilson_subscription_score REAL DEFAULT NULL,
        needs_web_scrape INTEGER DEFAULT 0,
        image_extension TEXT DEFAULT NULL,
        needs_image INTEGER DEFAULT 0
    )
    """)

    # Create users table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        steamid INTEGER PRIMARY KEY,
        personaname TEXT,
        personaname_en TEXT,
        dt_updated TEXT,
        dt_translated TEXT,
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
        dt_queued TEXT
    )
    """)

    # Safe migrations for existing databases
    _safe_add_columns(cursor, "workshop_items", [
        ("language", "INTEGER"),
        ("lifetime_subscriptions", "INTEGER"),
        ("lifetime_favorited", "INTEGER"),
        ("title_en", "TEXT"),
        ("short_description_en", "TEXT"),
        ("extended_description_en", "TEXT"),
        ("dt_translated", "TEXT"),
        ("translation_priority", "INTEGER DEFAULT 0"),
        ("is_queued_for_subscription", "INTEGER DEFAULT 0"),
        ("wilson_favorite_score", "REAL DEFAULT NULL"),
        ("wilson_subscription_score", "REAL DEFAULT NULL"),
        ("needs_web_scrape", "INTEGER DEFAULT 0"),
        ("image_extension", "TEXT DEFAULT NULL"),
        ("needs_image", "INTEGER DEFAULT 0"),
    ])

    # Create app_tracking table for historical scraping and filter storage
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_tracking (
        appid INTEGER PRIMARY KEY,
        last_historical_date_scanned INTEGER,
        filter_text TEXT DEFAULT '',
        required_tags TEXT DEFAULT '[]',
        excluded_tags TEXT DEFAULT '[]',
        window_size INTEGER DEFAULT 2592000,
        last_page_scanned INTEGER DEFAULT 0,
        enrichment_filters TEXT DEFAULT '[]',
        last_cursor TEXT DEFAULT ''
    )
    """)

    # Safe migrations for existing databases to add new filter columns
    _safe_add_columns(cursor, "app_tracking", [
        ("filter_text", "TEXT DEFAULT ''"),
        ("required_tags", "TEXT DEFAULT '[]'"),
        ("excluded_tags", "TEXT DEFAULT '[]'"),
        ("window_size", "INTEGER DEFAULT 2592000"),
        ("last_page_scanned", "INTEGER DEFAULT 0"),
        ("enrichment_filters", "TEXT DEFAULT '[]'"),
        ("last_cursor", "TEXT DEFAULT ''"),
    ])

    # Data Migration: Populate app_tracking from existing workshop_items if empty, 
    # and drop the obsolete app_state table.
    cursor.execute("SELECT COUNT(*) FROM app_tracking")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO app_tracking (appid, last_historical_date_scanned)
            SELECT consumer_appid, MAX(time_updated)
            FROM workshop_items
            WHERE consumer_appid IS NOT NULL AND time_updated IS NOT NULL
            GROUP BY consumer_appid
        """)
    
    cursor.execute("DROP TABLE IF EXISTS app_state")

    # Legacy filter migration: convert old filter columns to unified enrichment_filters JSON
    cursor.execute("""
        SELECT appid, filter_text, required_tags, excluded_tags, enrichment_filters
        FROM app_tracking
        WHERE (enrichment_filters IS NULL OR enrichment_filters = '' OR enrichment_filters = '[]')
    """)
    for row in cursor.fetchall():
        filter_text = (row["filter_text"] or "").strip()
        try:
            required_tags = json.loads(row["required_tags"] or "[]")
        except: required_tags = []
        try:
            excluded_tags = json.loads(row["excluded_tags"] or "[]")
        except: excluded_tags = []
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
            "UPDATE app_tracking SET enrichment_filters = ? WHERE appid = ?",
            (json.dumps(filters), row["appid"])
        )
        conn.commit()

    # Schema versioning: run migrations cumulatively from current to expected version
    EXPECTED_VERSION = 6
    db_version = cursor.execute("PRAGMA user_version").fetchone()[0]
    logging.info(f"Database schema version: {db_version} (expected: {EXPECTED_VERSION})")

    if db_version < 1:
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

    if db_version < 2:
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

    if db_version < 3:
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

    if db_version < 4:
        logging.info("Running migration 3→4: adding image download flag...")
        cursor.execute("""
            UPDATE workshop_items SET needs_image = 1
            WHERE preview_url IS NOT NULL AND preview_url != ''
              AND image_extension IS NULL
        """)
        updated = cursor.rowcount
        cursor.execute("PRAGMA user_version = 4")
        logging.info(f"Migration 3→4 complete. Set needs_image=1 on {updated} items.")

    if db_version < 5:
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_language ON workshop_items (language)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_file_size ON workshop_items (file_size)")

        conn.commit()
        cursor.execute("PRAGMA user_version = 5")
        logging.info("Migration 4→5 complete.")

    if db_version < 6:
        logging.info("Running migration 5→6: tags_text column + FTS5 rebuild...")

        _safe_add_columns(cursor, "workshop_items", [("tags_text", "TEXT DEFAULT ''")])
        conn.commit()

        cursor.execute("""
            UPDATE workshop_items SET tags_text = (
                SELECT COALESCE(GROUP_CONCAT(value, ' '), '')
                FROM json_each(workshop_items.tags)
            )
            WHERE tags_text IS NULL OR tags_text = ''
        """)
        conn.commit()
        updated = cursor.rowcount
        logging.info(f"Populated tags_text for {updated} items")

        cursor.execute("DROP TABLE IF EXISTS workshop_fts")
        cursor.execute("""
            CREATE VIRTUAL TABLE workshop_fts USING fts5(
                title, title_en,
                short_description, short_description_en,
                extended_description, extended_description_en,
                tags_text,
                content='workshop_items', content_rowid='workshop_id'
            )
        """)
        cursor.execute("INSERT INTO workshop_fts(workshop_fts) VALUES ('rebuild')")
        logging.info("FTS5 rebuilt with tags_text column")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags_text ON workshop_items (tags_text)")

        conn.commit()
        cursor.execute("PRAGMA user_version = 6")
        logging.info("Migration 5→6 complete.")

    # Create indexes for faster querying
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_consumer_appid ON workshop_items (consumer_appid)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON workshop_items (status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dt_updated ON workshop_items (dt_updated)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dt_attempted ON workshop_items (dt_attempted)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_title ON workshop_items (title)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags ON workshop_items (tags)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator ON workshop_items (creator)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_short_description ON workshop_items (short_description)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_extended_description ON workshop_items (extended_description)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_status_dt_attempted ON workshop_items (status, dt_attempted)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_appid_status ON workshop_items (consumer_appid, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_creator_dt_updated ON workshop_items (creator, dt_updated)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_translation_priority ON workshop_items (translation_priority)")

    conn.commit()
    conn.close()

def toggle_subscription_queue_status(db_path: str, workshop_id: int):
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

def get_queued_items(db_path: str) -> list[dict]:
    """Retrieves all items currently queued for subscription."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT workshop_id, title FROM workshop_items WHERE is_queued_for_subscription = 1 ORDER BY title"
    )
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items

def insert_or_update_item(db_path: str, item_data: dict) -> bool:
    """
    Inserts a new item or updates an existing item.
    Returns True if a new item was discovered (inserted), False if it was updated.
    """
    conn = get_connection(db_path)

    columns = [col for col in item_data.keys() if col in WORKSHOP_ITEM_COLUMNS]

    if "tags" in item_data and "tags_text" not in item_data:
        try:
            tags_list = json.loads(item_data["tags"]) if isinstance(item_data["tags"], str) else item_data["tags"]
            if isinstance(tags_list, list):
                item_data["tags_text"] = " ".join(str(t) for t in tags_list)
        except (json.JSONDecodeError, TypeError):
            pass

    cursor = conn.execute("SELECT 1 FROM workshop_items WHERE workshop_id = ?", (item_data["workshop_id"],))
    is_new = cursor.fetchone() is None

    columns = list(item_data.keys())
    placeholders = ",".join(["?"] * len(columns))
    
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
    
    conn.execute(sql, list(item_data.values()))
    conn.commit()
    conn.close()
    return is_new

def get_next_items_to_scrape(db_path: str, limit: int = 10, staleness_days: int = 30) -> list[dict]:
    """
    Retrieves the next batch of workshop items to be scraped.
    Prioritizes items that have never been scraped, then those with partial
    content (status 206) sorted by subscriber count (DESC), and finally
    the oldest successfully scraped items.
    Returns a list of full item data dictionaries.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    stale_param = f'-{staleness_days} days'

    sql = """
        SELECT * FROM workshop_items
        WHERE
            status IS NULL OR
            (status = 200 AND dt_attempted < datetime('now', ?))
        ORDER BY
            CASE
                WHEN status IS NULL THEN 0
                WHEN status = 200 AND dt_attempted < datetime('now', ?) THEN 2
                ELSE 3
            END ASC,
            dt_attempted ASC
        LIMIT ?
    """
    cursor.execute(sql, (stale_param, stale_param, limit))
    
    # Return full dictionaries
    items = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return items

def count_unscraped_items(db_path: str) -> int:
    """Returns the number of items that have never been scraped (dt_attempted is NULL)."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT COUNT(workshop_id) as count FROM workshop_items WHERE dt_attempted IS NULL")
    row = cursor.fetchone()
    conn.close()
    return row["count"] if row else 0

def insert_or_update_user(db_path: str, user_data: dict):
    """Inserts or updates a user in the users table."""
    conn = get_connection(db_path)
    columns = [col for col in user_data.keys() if col in USER_COLUMNS]
    placeholders = ",".join(["?"] * len(columns))
    updates = ",".join([f"{col}=excluded.{col}" for col in columns if col != "steamid"])
    
    sql = f"""
        INSERT INTO users ({",".join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(steamid) DO UPDATE SET {updates}
    """
    conn.execute(sql, list(user_data.values()))
    conn.commit()
    conn.close()

def get_user(db_path: str, steamid: int) -> dict | None:
    """Fetches a user by steamid."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT * FROM users WHERE steamid = ?", (steamid,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def flag_for_translation(db_path: str, item_id: int, priority: int, table: str = "workshop_items"):
    """Updates the translation priority for a specific item or user."""
    conn = get_connection(db_path)
    id_col = "workshop_id" if table == "workshop_items" else "steamid"
    conn.execute(
        f"UPDATE {table} SET translation_priority = ? WHERE {id_col} = ?",
        (priority, item_id)
    )
    conn.commit()
    conn.close()

def get_next_translation_item(db_path: str) -> tuple[str, int, int] | None:
    """
    Returns (type, id, priority) of the next item needing translation,
    checking both workshop_items and users, ordered by priority descending.
    """
    conn = get_connection(db_path)
    # Check workshop_items
    cursor = conn.execute(
        "SELECT workshop_id, translation_priority FROM workshop_items WHERE translation_priority > 0 ORDER BY translation_priority DESC LIMIT 1"
    )
    mod_row = cursor.fetchone()
    
    # Check users
    cursor = conn.execute(
        "SELECT steamid, translation_priority FROM users WHERE translation_priority > 0 ORDER BY translation_priority DESC LIMIT 1"
    )
    user_row = cursor.fetchone()
    conn.close()
    
    if not mod_row and not user_row:
        return None
        
    mod_prio = mod_row["translation_priority"] if mod_row else 0
    user_prio = user_row["translation_priority"] if user_row else 0
    
    if user_prio > mod_prio:
        return ("user", user_row["steamid"], user_prio)
    else:
        return ("workshop_item", mod_row["workshop_id"], mod_prio)

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

def _apply_numeric_filter(sql: str, params: list, col: str, filter_str: str) -> tuple[str, list]:
    """Parses operators from a string and applies them to the SQL."""
    match = re.match(r'^\s*([<>!=]=?|>|<)?\s*(\d+(?:\.\d+)?)\s*$', str(filter_str))
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
        SELECT w.*, u.personaname, u.personaname_en, u.dt_translated as user_dt_translated
        FROM workshop_items w
        LEFT JOIN users u ON w.creator = u.steamid
        WHERE w.workshop_id = ?
    """
    cursor = conn.execute(sql, (workshop_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def search_items(db_path: str, query: str = "", appid: int = None, 
                 title_query: str = "", desc_query: str = "", filename_query: str = "",
                 tags_query: str = "", tags: str = "", filters: list[dict] = None,
                 creator: str = "", numeric_filters: dict = None, 
                 required_tags: list[str] = None, excluded_tags: list[str] = None,
                 summary_only: bool = False, 
                 sort_by: str = None, sort_order: str = "ASC",
                 limit: int = None, offset: int = None) -> list[dict]:
    """
    Searches the database for items matching the criteria.
    Joins with users table to provide names.
    If summary_only is True, returns only essential columns for list view display.
    """
    conn = get_connection(db_path)
    
    if summary_only:
        cols = "w.workshop_id, w.title, w.title_en, w.creator, w.consumer_appid, w.dt_translated, w.is_queued_for_subscription, w.needs_web_scrape, w.needs_image, w.translation_priority, w.file_size, w.image_extension, w.wilson_subscription_score, w.wilson_favorite_score, u.personaname, u.personaname_en"
    else:
        cols = "w.*, u.personaname, u.personaname_en"
        
    sql = f"SELECT {cols} FROM workshop_items w LEFT JOIN users u ON w.creator = u.steamid WHERE 1=1"
    params = []

    if query:
        sql, params = _build_text_search_clauses(sql, params, query, ["title", "short_description", "extended_description"])
    if title_query:
        sql, params = _build_text_search_clauses(sql, params, title_query, ["title"])
    if desc_query:
        sql, params = _build_text_search_clauses(sql, params, desc_query, ["short_description", "extended_description"])
    if filename_query:
        sql, params = _build_text_search_clauses(sql, params, filename_query, ["filename"])
    if tags_query:
        sql, params = _build_text_search_clauses(sql, params, tags_query, ["tags"])
    if tags:
        clause, clause_params = _build_json_tag_clause("tags", "contains", tags)
        sql += f" AND {clause}"
        params.extend(clause_params)

    if creator:
        sql += " AND creator = ?"
        params.append(creator)
        
    if appid is not None:
        sql += " AND consumer_appid = ?"
        params.append(appid)

    if numeric_filters:
        valid_cols = {"file_size", "subscriptions", "favorited", "views"}
        for col, f_str in numeric_filters.items():
            if col in valid_cols and f_str:
                sql, params = _apply_numeric_filter(sql, params, col, f_str)

    if filters:
        pct_filters = []
        regular_filters = []
        for f in filters:
            if f.get("op") == "percentile":
                pct_filters.append(f)
            else:
                regular_filters.append(f)

        filter_clauses = []
        for f in regular_filters:
            logic = f.get("logic", "AND").upper()
            field = f.get("field")
            op = f.get("op")
            val = f.get("value")
            if not field or not op:
                continue
            db_col = FIELD_NAME_MAP.get(field, field)
            if db_col == "tags":
                if op in ("is", "is_not"):
                    continue
                clause, clause_params = _build_json_tag_clause(db_col, op, val)
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
            elif db_col in _EN_FIELDS and op in _TEXT_OPS:
                en_col = _EN_FIELDS[db_col]
                c1, p1 = _build_filter_clause(db_col, op, val)
                c2, p2 = _build_filter_clause(en_col, op, val)
                joiner = " AND " if op in _TEXT_NEG_OPS else " OR "
                if c1 and c2:
                    clause = f"({c1}{joiner}{c2})"
                    clause_params = p1 + p2
                elif c1:
                    clause, clause_params = c1, p1
                else:
                    clause, clause_params = c2, p2
            else:
                clause, clause_params = _build_filter_clause(db_col, op, val)
            if clause:
                params.extend(clause_params)
                filter_clauses.append((logic, clause))
        if filter_clauses:
            sql += " AND ("
            for idx, (logic, clause) in enumerate(filter_clauses):
                sql += f" {logic} " if idx > 0 else ""
                sql += clause
            sql += ")"

        for f in pct_filters:
            field = f.get("field")
            val = f.get("value")
            if not field or not val:
                continue
            db_col = FIELD_NAME_MAP.get(field, field)
            if db_col == "tags":
                continue
            threshold = _compute_percentile_threshold(db_path, db_col, val, regular_filters)
            if threshold is not None:
                sql += f" AND w.{db_col} >= {threshold}"

    sort_sql = _build_sort_clause(sort_by, sort_order) if sort_by else ""
    sql += sort_sql
    limit_sql, limit_params = _build_limit_offset(limit, offset) if limit is not None else ("", [])
    sql += limit_sql
    params.extend(limit_params)
        
    cursor = conn.execute(sql, params)
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results

def get_all_authors(db_path: str) -> list[str]:
    """Returns a list of all unique creator IDs currently in the database."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT DISTINCT creator FROM workshop_items WHERE creator IS NOT NULL ORDER BY creator")
    results = [row["creator"] for row in cursor.fetchall()]
    conn.close()
    return results

def _classify_translation_status(item) -> str:
    """Classifies an item's translation state into a bucket name."""
    title = item["title"] or ""
    short_desc = item["short_description"] or ""
    ext_desc = item["extended_description"] or ""

    if item["title_en"] or item["short_description_en"] or item["extended_description_en"]:
        return "Translated"
    if title.isascii() and short_desc.isascii() and ext_desc.isascii():
        return "No translation needed (ASCII)"
    if item["translation_priority"] and item["translation_priority"] > 0:
        return "Queued"
    if not title:
        return "No data (never scraped)"
    return "Needs Translation (Unicode)"

def _classify_attempted_recency(dt_attempted: str, staleness_days: int = 30) -> str:
    """Classifies a dt_attempted timestamp as 'fresh', 'stale', or 'blank'."""
    if not dt_attempted:
        return "blank"
    try:
        attempted_dt = datetime.fromisoformat(dt_attempted.replace("Z", "+00:00"))
        threshold = datetime.now(timezone.utc) - timedelta(days=staleness_days)
        if attempted_dt.tzinfo is None:
            attempted_dt = attempted_dt.replace(tzinfo=timezone.utc)
        return "fresh" if attempted_dt >= threshold else "stale"
    except (ValueError, TypeError):
        return "blank"

def _compute_tag_frequencies(cursor) -> dict:
    """Queries all tag values and returns a frequency dictionary.
    Tries json_each first, falls back to Python parsing for legacy malformed JSON."""
    try:
        cursor.execute("""
            SELECT COALESCE(json_extract(value, '$.tag'), value) as tag_value, COUNT(*) as cnt
            FROM workshop_items, json_each(workshop_items.tags)
            WHERE tags IS NOT NULL AND tags != ''
            GROUP BY tag_value
            ORDER BY cnt DESC
        """)
        return {row["tag_value"]: row["cnt"] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        cursor.execute("SELECT tags FROM workshop_items WHERE tags IS NOT NULL AND tags != ''")
        tag_counts = {}
        for row in cursor.fetchall():
            try:
                tags_list = json.loads(row["tags"])
                if isinstance(tags_list, list):
                    for tag_item in tags_list:
                        tag_value = tag_item.get('tag') if isinstance(tag_item, dict) else tag_item
                        if tag_value:
                            tag_counts[str(tag_value)] = tag_counts.get(str(tag_value), 0) + 1
            except: continue
        return tag_counts

def get_db_stats(db_path: str, staleness_days: int = 30) -> dict:
    """Returns comprehensive statistics about the database."""
    conn = get_connection(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT status, COUNT(*) as count FROM workshop_items GROUP BY status")
    status_counts = [dict(row) for row in cursor.fetchall()]

    cursor.execute("""
        SELECT dt_attempted, dt_translated, title, short_description, extended_description,
               translation_priority, title_en, short_description_en, extended_description_en
        FROM workshop_items
    """)
    all_items = cursor.fetchall()

    translation_status = {
        "No translation needed (ASCII)": 0,
        "Needs Translation (Unicode)": 0,
        "Queued": 0, "Translated": 0,
        "No data (never scraped)": 0,
    }
    dt_attempted_counts = {"fresh": 0, "stale": 0, "blank": 0}

    for item in all_items:
        translation_status[_classify_translation_status(item)] += 1
        dt_attempted_counts[_classify_attempted_recency(item["dt_attempted"], staleness_days)] += 1

    cursor.execute("SELECT MAX(dt_updated) FROM workshop_items")
    highest_dt_updated = cursor.fetchone()[0]

    cursor.execute("SELECT appid, last_page_scanned, last_cursor FROM app_tracking")
    app_stats = [dict(row) for row in cursor.fetchall()]

    tag_counts = _compute_tag_frequencies(cursor)
    conn.close()
    return {
        "status_counts": status_counts,
        "translation_status": translation_status,
        "tag_counts": tag_counts,
        "dt_attempted_counts": dt_attempted_counts,
        "highest_dt_updated": highest_dt_updated,
        "app_stats": app_stats
    }

def compute_wilson_cutoffs(db_path: str, filters: list[dict] = None) -> dict:
    """Returns percentile cutoff scores for Wilson metrics across items matching filters.
    Uses NTILE(100) — returns p99, p90, p50 thresholds for both scores.
    Returns empty dict if fewer than 10 items in the filtered set."""
    conn = get_connection(db_path)
    sql = "SELECT workshop_id, wilson_favorite_score, wilson_subscription_score FROM workshop_items"
    params = []
    if filters:
        filter_clauses = []
        for f in filters:
            if f.get("op") == "percentile":
                continue
            field = f.get("field")
            op = f.get("op")
            val = f.get("value")
            if not field or not op:
                continue
            db_col = FIELD_NAME_MAP.get(field, field)
            clause, clause_params = _build_filter_clause(db_col, op, val)
            if clause:
                params.extend(clause_params)
                filter_clauses.append((f.get("logic", "AND").upper(), clause))
        if filter_clauses:
            sql += " WHERE "
            for idx, (logic, clause) in enumerate(filter_clauses):
                sql += f" {logic} " if idx > 0 else ""
                sql += clause

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
    """
    try:
        cursor = conn.execute(cutoff_sql, params)
        result = {row["key"]: row["val"] for row in cursor.fetchall()}
        conn.close()
        return result
    except Exception:
        conn.close()
        return {}

def get_app_tracking(db_path: str, appid: int) -> dict | None:
    """
    Returns the app tracking data for a given appid, including scan date and filters.
    Returns a dictionary of all columns if found, otherwise None.
    """
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT * FROM app_tracking WHERE appid = ?", (appid,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_next_web_scrape_item(db_path: str) -> dict | None:
    """Returns the highest-priority item needing web scraping, or None."""
    conn = get_connection(db_path)
    cursor = conn.execute("""
        SELECT * FROM workshop_items
        WHERE needs_web_scrape > 0
        ORDER BY needs_web_scrape DESC, dt_attempted ASC
        LIMIT 1
    """)
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def flag_for_web_scrape(db_path: str, workshop_id: int, priority: int):
    """Sets needs_web_scrape to MAX(current, priority). Never downgrades."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET needs_web_scrape = MAX(needs_web_scrape, ?) WHERE workshop_id = ?",
        (priority, workshop_id)
    )
    conn.commit()
    conn.close()


def bump_web_priority_for_list(db_path: str, workshop_id: int):
    """Bumps web scrape priority to 5 for list items if currently < 5 and > 0."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET needs_web_scrape = 5 "
        "WHERE workshop_id = ? AND needs_web_scrape > 0 AND needs_web_scrape < 5",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def bump_web_priority_for_detail(db_path: str, workshop_id: int):
    """Bumps web scrape priority to 10 for detail items if currently < 10 and > 0."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET needs_web_scrape = 10 "
        "WHERE workshop_id = ? AND needs_web_scrape > 0 AND needs_web_scrape < 10",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def get_next_image_item(db_path: str) -> dict | None:
    """Returns the highest-priority item needing image download, or None."""
    conn = get_connection(db_path)
    cursor = conn.execute("""
        SELECT * FROM workshop_items
        WHERE needs_image > 0
        ORDER BY needs_image DESC, dt_attempted ASC
        LIMIT 1
    """)
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def flag_for_image(db_path: str, workshop_id: int, priority: int):
    """Sets needs_image to MAX(current, priority). Never downgrades."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET needs_image = MAX(needs_image, ?) WHERE workshop_id = ?",
        (priority, workshop_id)
    )
    conn.commit()
    conn.close()


def bump_image_priority_for_list(db_path: str, workshop_id: int):
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET needs_image = 5 "
        "WHERE workshop_id = ? AND needs_image > 0 AND needs_image < 5",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def bump_image_priority_for_detail(db_path: str, workshop_id: int):
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET needs_image = 10 "
        "WHERE workshop_id = ? AND needs_image > 0 AND needs_image < 10",
        (workshop_id,)
    )
    conn.commit()
    conn.close()


def flag_field_for_translation(db_path: str, item_type: str, item_id: int, field: str, text: str, priority: int):
    """Inserts a field into translation_queue, or bumps its priority. Never downgrades."""
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
        conn.execute(
            "INSERT INTO translation_queue (item_type, item_id, field, original_text, priority, dt_queued) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_type, item_id, field, text, priority, None)
        )
    conn.commit()
    conn.close()


def bump_translation_for_list(db_path: str, workshop_id: int):
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
            flag_field_for_translation(db_path, "item", workshop_id, field, text, 5)


def bump_translation_for_detail(db_path: str, workshop_id: int):
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
            flag_field_for_translation(db_path, "item", workshop_id, field, text, 10)


def get_next_batch_for_translation(db_path: str, limit: int = 20) -> list[dict]:
    """Returns up to `limit` highest-priority fields for translation."""
    conn = get_connection(db_path)
    cursor = conn.execute(
        "SELECT * FROM translation_queue ORDER BY priority DESC, dt_queued ASC LIMIT ?",
        (limit,)
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def save_app_filter(db_path: str, appid: int, filter_text: str = "", required_tags: list[str] = None,
                     excluded_tags: list[str] = None, enrichment_filters: str = None) -> None:
    """
    Saves the filter settings for a given appid in the app_tracking table.
    If enrichment_filters is provided (JSON string), it is used as the canonical filter spec.
    Legacy columns (filter_text, required_tags, excluded_tags) are kept for backward compat.
    """
    conn = get_connection(db_path)
    json_required_tags = json.dumps(required_tags) if required_tags is not None else '[]'
    json_excluded_tags = json.dumps(excluded_tags) if excluded_tags is not None else '[]'
    enrichment = enrichment_filters if enrichment_filters is not None else '[]'

    conn.execute(
        "INSERT INTO app_tracking (appid, filter_text, required_tags, excluded_tags, enrichment_filters) "
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
        "INSERT INTO app_tracking (appid, last_historical_date_scanned, window_size) VALUES (?, ?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET last_historical_date_scanned = excluded.last_historical_date_scanned, window_size = excluded.window_size",
        (appid, last_date, window_size)
    )
    conn.commit()
    conn.close()

def update_app_tracking_page(db_path: str, appid: int, last_page: int) -> None:
    """Updates the last_page_scanned for a given appid."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO app_tracking (appid, last_page_scanned) VALUES (?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET last_page_scanned = excluded.last_page_scanned",
        (appid, last_page)
    )
    conn.commit()
    conn.close()

def update_app_tracking_cursor(db_path: str, appid: int, cursor: str) -> None:
    """Updates the last_cursor for a given appid."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO app_tracking (appid, last_cursor) VALUES (?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET last_cursor = excluded.last_cursor",
        (appid, cursor)
    )
    conn.commit()
    conn.close()

def clear_pending_items(db_path: str) -> int:
    """
    Removes all workshop items that are 'pending' (never successfully scraped).
    Criteria: (status IS NULL OR status = 404) AND dt_updated IS NULL.
    Returns the number of rows deleted.
    """
    conn = get_connection(db_path)
    cursor = conn.execute(
        "DELETE FROM workshop_items WHERE (status IS NULL OR status = 404) AND dt_updated IS NULL"
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count
