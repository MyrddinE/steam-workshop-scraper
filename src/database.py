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
        ("translation_priority", "INTEGER DEFAULT 0"),
        ("is_queued_for_subscription", "INTEGER DEFAULT 0")
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
        excluded_tags TEXT DEFAULT '[]',
        window_size INTEGER DEFAULT 2592000
    )
    """)

    # Safe migrations for existing databases to add new filter columns
    app_tracking_new_cols = [
        ("filter_text", "TEXT DEFAULT ''"),
        ("required_tags", "TEXT DEFAULT '[]'"),
        ("excluded_tags", "TEXT DEFAULT '[]'"),
        ("window_size", "INTEGER DEFAULT 2592000")
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

def get_db_stats(db_path: str) -> dict:
    """Returns comprehensive statistics about the database."""
    from datetime import datetime, timedelta
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # 1. Record count by status
    cursor.execute("SELECT status, COUNT(*) as count FROM workshop_items GROUP BY status")
    status_counts = [dict(row) for row in cursor.fetchall()]

    # 2. Translation and date processing
    cursor.execute("""
        SELECT 
            dt_attempted, 
            dt_translated, 
            title, 
            short_description, 
            extended_description, 
            translation_priority, 
            title_en, 
            short_description_en, 
            extended_description_en 
        FROM workshop_items
    """)
    all_items = cursor.fetchall()

    translation_status = {
        "No data (dt_translated is empty)": 0,
        "No translation needed (is_ascii==True)": 0,
        "Queued": 0,
        "Translated": 0,
        "Needs Translation (Unicode detected)": 0
    }
    
    dt_attempted_counts = {
        "blank": 0,
        "less than 7 days ago": 0,
        "more than 7 days ago": 0,
    }

    seven_days_ago_dt = datetime.now() - timedelta(days=7)

    for item in all_items:
        # Translation Status
        title = item["title"] or ""
        short_desc = item["short_description"] or ""
        ext_desc = item["extended_description"] or ""

        if not item["dt_translated"]:
            translation_status["No data (dt_translated is empty)"] += 1
        elif item["title_en"] or item["short_description_en"] or item["extended_description_en"]:
            translation_status["Translated"] += 1
        elif item["translation_priority"] and item["translation_priority"] > 0:
            translation_status["Queued"] += 1
        elif title.isascii() and short_desc.isascii() and ext_desc.isascii():
            translation_status["No translation needed (is_ascii==True)"] += 1
        else:
            translation_status["Needs Translation (Unicode detected)"] += 1

        # Date updated logic
        try:
            if not item["dt_attempted"]:
                dt_attempted_counts["blank"] += 1
            else:
                attempted_dt = datetime.fromisoformat(item["dt_attempted"].replace("Z", "+00:00"))
                if attempted_dt.tzinfo is None:
                    attempted_dt = attempted_dt.replace(tzinfo=seven_days_ago_dt.tzinfo)

                if attempted_dt >= seven_days_ago_dt:
                    dt_attempted_counts["less than 7 days ago"] += 1
                else:
                    dt_attempted_counts["more than 7 days ago"] += 1
        except (ValueError, TypeError):
            dt_attempted_counts["blank"] += 1

    # 3. Tag counts
    cursor.execute("SELECT tags FROM workshop_items WHERE tags IS NOT NULL AND tags != ''")
    tag_counts = {}
    for row in cursor.fetchall():
        try:
            tags_list = json.loads(row["tags"])
            if isinstance(tags_list, list):
                for tag_item in tags_list:
                    tag_value = tag_item.get('tag') if isinstance(tag_item, dict) else tag_item
                    if tag_value:
                        tag_counts[tag_value] = tag_counts.get(tag_value, 0) + 1
        except: continue

    # 4. Global tracking info
    cursor.execute("SELECT MAX(dt_updated) FROM workshop_items")
    highest_dt_updated = cursor.fetchone()[0]

    # 5. App tracking info (Window Size, Last Scanned)
    cursor.execute("SELECT appid, last_historical_date_scanned, window_size FROM app_tracking")
    app_stats = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return {
        "status_counts": status_counts,
        "translation_status": translation_status,
        "tag_counts": tag_counts,
        "dt_attempted_counts": dt_attempted_counts,
        "highest_dt_updated": highest_dt_updated,
        "app_stats": app_stats
    }

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
