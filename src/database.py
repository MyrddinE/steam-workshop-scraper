import sqlite3
import shlex
import re
import json

WORKSHOP_ITEM_COLUMNS = frozenset({
    "workshop_id", "dt_found", "dt_updated", "dt_attempted", "dt_translated",
    "status", "title", "title_en", "creator", "creator_appid", "consumer_appid",
    "filename", "file_size", "preview_url", "hcontent_file", "hcontent_preview",
    "short_description", "short_description_en", "time_created", "time_updated",
    "visibility", "banned", "ban_reason", "app_name", "file_type",
    "subscriptions", "favorited", "views", "tags", "extended_description",
    "extended_description_en", "language", "lifetime_subscriptions",
    "lifetime_favorited", "translation_priority", "is_queued_for_subscription",
    "wilson_favorite_score", "wilson_subscription_score",
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
}

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
        wilson_subscription_score REAL DEFAULT NULL
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
        enrichment_filters TEXT DEFAULT '[]'
    )
    """)

    # Safe migrations for existing databases to add new filter columns
    _safe_add_columns(cursor, "app_tracking", [
        ("filter_text", "TEXT DEFAULT ''"),
        ("required_tags", "TEXT DEFAULT '[]'"),
        ("excluded_tags", "TEXT DEFAULT '[]'"),
        ("window_size", "INTEGER DEFAULT 2592000"),
        ("last_page_scanned", "INTEGER DEFAULT 0"),
        ("enrichment_filters", "TEXT DEFAULT '[]'")
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

    # Wilson score backfill: compute scores for existing items that lack them
    cursor.execute("""
        SELECT workshop_id, favorited, lifetime_subscriptions, views
        FROM workshop_items
        WHERE views > 0 AND (wilson_favorite_score IS NULL OR wilson_subscription_score IS NULL)
    """)
    to_update = cursor.fetchall()
    if to_update:
        import math
        for row in to_update:
            views = row["views"] or 0
            if views == 0:
                continue
            def wl(s, v):
                p = s / v
                z2 = 1.96 * 1.96
                d = 1 + z2 / v
                n = p + z2 / (2*v) - 1.96 * math.sqrt(p*(1-p)/v + z2/(4*v*v))
                return max(0.0, min(1.0, n / d))
            fav_score = wl(row["favorited"] or 0, views)
            sub_score = wl(row["lifetime_subscriptions"] or 0, views)
            cursor.execute(
                "UPDATE workshop_items SET wilson_favorite_score = ?, wilson_subscription_score = ? WHERE workshop_id = ?",
                (fav_score, sub_score, row["workshop_id"])
            )
        conn.commit()

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

def get_next_items_to_scrape(db_path: str, limit: int = 10) -> list[dict]:
    """
    Retrieves the next batch of workshop items to be scraped.
    Prioritizes items that have never been scraped, then those with partial
    content (status 206) sorted by subscriber count (DESC), and finally
    the oldest successfully scraped items.
    Returns a list of full item data dictionaries.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    sql = """
        SELECT * FROM workshop_items
        WHERE
            status IS NULL OR
            status = 206 OR
            (status = 200 AND dt_attempted < date('now', '-7 days'))
        ORDER BY
            CASE
                WHEN status IS NULL THEN 0
                WHEN status = 206 THEN 1
                WHEN status = 200 AND dt_attempted < date('now', '-7 days') THEN 2
                ELSE 3 -- Fallback for other statuses, e.g., 404, 500, or very new 200s
            END ASC,
            CASE
                WHEN status = 206 THEN subscriptions
                ELSE NULL
            END DESC, -- Prioritize higher subscriptions for 206 status
            dt_attempted ASC -- General secondary sort, also for status=200 old items
        LIMIT ?
    """
    cursor.execute(sql, (limit,))
    
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
        cols = "w.workshop_id, w.title, w.title_en, w.creator, w.consumer_appid, w.dt_translated, w.is_queued_for_subscription, u.personaname, u.personaname_en"
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
        filter_clauses = []
        for f in filters:
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
    if not item["dt_translated"]:
        return "No data (dt_translated is empty)"
    if item["title_en"] or item["short_description_en"] or item["extended_description_en"]:
        return "Translated"
    if item["translation_priority"] and item["translation_priority"] > 0:
        return "Queued"
    title = item["title"] or ""
    short_desc = item["short_description"] or ""
    ext_desc = item["extended_description"] or ""
    if title.isascii() and short_desc.isascii() and ext_desc.isascii():
        return "No translation needed (is_ascii==True)"
    return "Needs Translation (Unicode detected)"

def _classify_attempted_recency(dt_attempted: str, reference_dt) -> str:
    """Classifies a dt_attempted timestamp as 'blank', 'less than 7 days ago', or 'more than 7 days ago'."""
    if not dt_attempted:
        return "blank"
    try:
        attempted_dt = datetime.fromisoformat(dt_attempted.replace("Z", "+00:00"))
        if attempted_dt.tzinfo is None:
            attempted_dt = attempted_dt.replace(tzinfo=reference_dt.tzinfo)
        return "less than 7 days ago" if attempted_dt >= reference_dt else "more than 7 days ago"
    except (ValueError, TypeError):
        return "blank"

def _compute_tag_frequencies(cursor) -> dict:
    """Queries all tag values and returns a frequency dictionary using json_each.
    Handles both string arrays (normalized) and object arrays (API raw format)."""
    cursor.execute("""
        SELECT COALESCE(json_extract(value, '$.tag'), value) as tag_value, COUNT(*) as cnt
        FROM workshop_items, json_each(workshop_items.tags)
        WHERE tags IS NOT NULL AND tags != ''
        GROUP BY tag_value
        ORDER BY cnt DESC
    """)
    return {row["tag_value"]: row["cnt"] for row in cursor.fetchall()}

def get_db_stats(db_path: str) -> dict:
    """Returns comprehensive statistics about the database."""
    from datetime import datetime, timedelta
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
        "No data (dt_translated is empty)": 0,
        "No translation needed (is_ascii==True)": 0,
        "Queued": 0, "Translated": 0,
        "Needs Translation (Unicode detected)": 0
    }
    dt_attempted_counts = {"blank": 0, "less than 7 days ago": 0, "more than 7 days ago": 0}
    seven_days_ago_dt = datetime.now() - timedelta(days=7)

    for item in all_items:
        translation_status[_classify_translation_status(item)] += 1
        dt_attempted_counts[_classify_attempted_recency(item["dt_attempted"], seven_days_ago_dt)] += 1

    cursor.execute("SELECT MAX(dt_updated) FROM workshop_items")
    highest_dt_updated = cursor.fetchone()[0]

    cursor.execute("SELECT appid, last_historical_date_scanned, window_size FROM app_tracking")
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
        SELECT 'fav_p99' as key, COALESCE(MIN(wilson_favorite_score), 0) as val
        FROM fav_ntile WHERE bucket = 1
        UNION ALL SELECT 'fav_p90', COALESCE(MIN(wilson_favorite_score), 0)
        FROM fav_ntile WHERE bucket = 10
        UNION ALL SELECT 'fav_p50', COALESCE(MIN(wilson_favorite_score), 0)
        FROM fav_ntile WHERE bucket = 50
        UNION ALL SELECT 'sub_p99', COALESCE(MIN(wilson_subscription_score), 0)
        FROM sub_ntile WHERE bucket = 1
        UNION ALL SELECT 'sub_p90', COALESCE(MIN(wilson_subscription_score), 0)
        FROM sub_ntile WHERE bucket = 10
        UNION ALL SELECT 'sub_p50', COALESCE(MIN(wilson_subscription_score), 0)
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
