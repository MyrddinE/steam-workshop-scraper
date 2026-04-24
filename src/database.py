import sqlite3
import shlex
import re
import json

def get_connection(db_path: str):
    """
    Returns a SQLite connection with WAL mode enabled and Row factory.
    WAL allows simultaneous readers and writers.
    """
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def initialize_database(db_path: str):
    """
    Initializes the SQLite database and creates the workshop_items table and indexes.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS workshop_items (
        workshop_id INTEGER PRIMARY KEY,
        dt_found TEXT,
        dt_updated TEXT,
        dt_attempted TEXT,
        status INTEGER,
        title TEXT,
        creator INTEGER,
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
        translation_priority INTEGER DEFAULT 0
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
    new_cols = [
        ("language", "INTEGER"),
        ("lifetime_subscriptions", "INTEGER"),
        ("lifetime_favorited", "INTEGER"),
        ("title_en", "TEXT"),
        ("short_description_en", "TEXT"),
        ("extended_description_en", "TEXT"),
        ("dt_translated", "TEXT"),
        ("translation_priority", "INTEGER DEFAULT 0")
    ]
    for col_name, col_type in new_cols:
        try:
            cursor.execute(f"ALTER TABLE workshop_items ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass # Column already exists

    # Create app_tracking table for historical scraping and filter storage
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_tracking (
        appid INTEGER PRIMARY KEY,
        last_historical_date_scanned INTEGER,
        filter_text TEXT DEFAULT '',
        required_tags TEXT DEFAULT '[]',
        excluded_tags TEXT DEFAULT '[]'
    )
    """)

    # Safe migrations for existing databases to add new filter columns
    app_tracking_new_cols = [
        ("filter_text", "TEXT DEFAULT ''"),
        ("required_tags", "TEXT DEFAULT '[]'"),
        ("excluded_tags", "TEXT DEFAULT '[]'")
    ]
    for col_name, col_type in app_tracking_new_cols:
        try:
            cursor.execute(f"ALTER TABLE app_tracking ADD COLUMN {col_name} {col_type}")
        except sqlite3.OperationalError:
            pass # Column already exists

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

    conn.commit()
    conn.close()

def insert_or_update_item(db_path: str, item_data: dict) -> bool:
    """
    Inserts a new item or updates an existing item.
    Returns True if a new item was discovered (inserted), False if it was updated.
    """
    conn = get_connection(db_path)
    
    # Check if item exists to determine if this is a new discovery
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

def get_next_items_to_scrape(db_path: str, limit: int = 10) -> list[int]:
    """
    Returns a list of workshop_ids that need to be scraped, prioritizing in exact order:
    1. Unscraped new items (status IS NULL).
    2. Partially failed items (status != 200).
    3. Successfully scraped items (status = 200), ordered by dt_attempted ASC (stalest first).
    """
    conn = get_connection(db_path)
    sql = """
        SELECT workshop_id FROM workshop_items
        ORDER BY
            CASE
                WHEN status IS NULL THEN 1
                WHEN status != 200 THEN 2
                ELSE 3
            END ASC,
            dt_attempted ASC
        LIMIT ?
    """
    cursor = conn.execute(sql, (limit,))
    results = [row["workshop_id"] for row in cursor.fetchall()]
    conn.close()
    return results

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
    columns = list(user_data.keys())
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
                 title_query: str = "", desc_query: str = "", filename_query: str = "", tags_query: str = "",
                 creator: str = "", numeric_filters: dict = None, tags: str = None,
                 summary_only: bool = False, filters: list[dict] = None,
                 sort_by: str = None, sort_order: str = "ASC",
                 limit: int = None, offset: int = None) -> list[dict]:
    """
    Searches the database for items matching the criteria.
    Joins with users table to provide names.
    If summary_only is True, returns only essential columns for list view display.
    """
    conn = get_connection(db_path)
    
    if summary_only:
        cols = "w.workshop_id, w.title, w.title_en, w.creator, w.consumer_appid, w.dt_translated, u.personaname, u.personaname_en"
    else:
        cols = "w.*, u.personaname, u.personaname_en"
        
    sql = f"SELECT {cols} FROM workshop_items w LEFT JOIN users u ON w.creator = u.steamid WHERE 1=1"
    params = []

    def apply_query_to_columns(q_str: str, cols: list[str]):
        nonlocal sql, params
        pos_tokens, neg_tokens = _parse_query(q_str)
        
        for token in pos_tokens:
            clauses = [f"{col} LIKE ?" for col in cols]
            sql += f" AND ({' OR '.join(clauses)})"
            params.extend([f"%{token}%"] * len(cols))
            
        for token in neg_tokens:
            for col in cols:
                # Need to handle NULLs, as NOT LIKE fails if field is NULL
                sql += f" AND ({col} IS NULL OR {col} NOT LIKE ?)"
                params.append(f"%{token}%")

    if query:
        apply_query_to_columns(query, ["title", "short_description", "extended_description"])
    if title_query:
        apply_query_to_columns(title_query, ["title"])
    if desc_query:
        apply_query_to_columns(desc_query, ["short_description", "extended_description"])
    if filename_query:
        apply_query_to_columns(filename_query, ["filename"])
    if tags_query:
        apply_query_to_columns(tags_query, ["tags"])
    if tags: # Legacy fallback for older test_database.py
        sql += " AND tags LIKE ?"
        params.append(f"%{tags}%")

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
        for i, f in enumerate(filters):
            logic = f.get("logic", "AND").upper()
            field = f.get("field")
            op = f.get("op")
            val = f.get("value")

            if not field or not op:
                continue
            
            # Map common names to DB columns
            field_map = {
                "Title": "title",
                "Description": "short_description",
                "Filename": "filename",
                "Tags": "tags",
                "Author ID": "creator",
                "File Size": "file_size",
                "Subs": "subscriptions",
                "Favs": "favorited",
                "Views": "views",
                "Workshop ID": "workshop_id",
                "AppID": "consumer_appid",
                "Language ID": "language"
            }
            db_col = field_map.get(field, field)
            
            clause = ""
            if op == "contains":
                clause = f"{db_col} LIKE ?"
                params.append(f"%{val}%")
            elif op == "does_not_contain":
                clause = f"({db_col} IS NULL OR {db_col} NOT LIKE ?)"
                params.append(f"%{val}%")
            elif op == "is":
                clause = f"{db_col} = ?"
                params.append(val)
            elif op == "is_not":
                clause = f"{db_col} != ?"
                params.append(val)
            elif op == "gt":
                clause = f"{db_col} > ?"
                params.append(val)
            elif op == "lt":
                clause = f"{db_col} < ?"
                params.append(val)
            elif op == "gte":
                clause = f"{db_col} >= ?"
                params.append(val)
            elif op == "lte":
                clause = f"{db_col} <= ?"
                params.append(val)
            elif op == "is_empty":
                clause = f"({db_col} IS NULL OR {db_col} = '')"
            elif op == "is_not_empty":
                clause = f"({db_col} IS NOT NULL AND {db_col} != '')"
            
            if clause:
                filter_clauses.append((logic, clause))
                
        if filter_clauses:
            sql += " AND ("
            for idx, (logic, clause) in enumerate(filter_clauses):
                if idx == 0:
                    sql += clause
                else:
                    sql += f" {logic} {clause}"
            sql += ")"

    if sort_by:
        # Simple whitelist for safety
        valid_sort_cols = {
            "title", "file_size", "subscriptions", "favorited", "views", 
            "workshop_id", "time_created", "time_updated"
        }
        if sort_by in valid_sort_cols:
            order = "DESC" if sort_order.upper() == "DESC" else "ASC"
            sql += f" ORDER BY {sort_by} {order}"
            
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
        if offset is not None:
            sql += " OFFSET ?"
            params.append(offset)
        
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


def save_app_filter(db_path: str, appid: int, filter_text: str = "", required_tags: list[str] = None, excluded_tags: list[str] = None) -> None:
    """
    Saves the filter settings for a given appid in the app_tracking table.
    Tags lists are JSON-serialized.
    """
    conn = get_connection(db_path)
    
    # Ensure tags are JSON serialized
    json_required_tags = json.dumps(required_tags) if required_tags is not None else '[]'
    json_excluded_tags = json.dumps(excluded_tags) if excluded_tags is not None else '[]'

    # Update only the filter-related columns. 
    # last_historical_date_scanned is NOT updated here; it's handled by daemon.
    conn.execute(
        "INSERT INTO app_tracking (appid, filter_text, required_tags, excluded_tags) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET "
        "filter_text = excluded.filter_text, "
        "required_tags = excluded.required_tags, "
        "excluded_tags = excluded.excluded_tags",
        (appid, filter_text, json_required_tags, json_excluded_tags)
    )
    conn.commit()
    conn.close()

def update_app_tracking(db_path: str, appid: int, last_date: int) -> None:
    """Updates the last_historical_date_scanned for a given appid."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO app_tracking (appid, last_historical_date_scanned) VALUES (?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET last_historical_date_scanned = excluded.last_historical_date_scanned",
        (appid, last_date)
    )
    conn.commit()
    conn.close()
