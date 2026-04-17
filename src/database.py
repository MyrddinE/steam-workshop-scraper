import sqlite3
import shlex
import re

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
        creator TEXT,
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
        lifetime_favorited INTEGER
    )
    """)

    # Safe migrations for existing databases
    for col in ["language", "lifetime_subscriptions", "lifetime_favorited"]:
        try:
            cursor.execute(f"ALTER TABLE workshop_items ADD COLUMN {col} INTEGER")
        except sqlite3.OperationalError:
            pass # Column already exists

    # Create app_state table for pagination
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_state (
        appid INTEGER PRIMARY KEY,
        current_page INTEGER DEFAULT 1
    )
    """)

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
    Returns a list of workshop_ids that need to be scraped, prioritizing
    items that have never been attempted (NULL) or have the oldest attempt time.
    """
    conn = get_connection(db_path)
    cursor = conn.execute(
        "SELECT workshop_id FROM workshop_items ORDER BY dt_attempted ASC LIMIT ?", 
        (limit,)
    )
    results = [row["workshop_id"] for row in cursor.fetchall()]
    conn.close()
    return results


def get_app_page(db_path: str, appid: int) -> int:
    """Returns the last page scraped for a given appid (defaults to 1)."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT current_page FROM app_state WHERE appid = ?", (appid,))
    row = cursor.fetchone()
    conn.close()
    return row["current_page"] if row else 1

def update_app_page(db_path: str, appid: int, page: int):
    """Updates the last page scraped for a given appid."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO app_state (appid, current_page) VALUES (?, ?) ON CONFLICT(appid) DO UPDATE SET current_page=excluded.current_page",
        (appid, page)
    )
    conn.commit()
    conn.close()

def count_unscraped_items(db_path: str) -> int:
    """Returns the number of items that have never been scraped (dt_attempted is NULL)."""
    conn = get_connection(db_path)
    cursor = conn.execute("SELECT COUNT(workshop_id) as count FROM workshop_items WHERE dt_attempted IS NULL")
    row = cursor.fetchone()
    conn.close()
    return row["count"] if row else 0

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

def search_items(db_path: str, query: str = "", appid: int = None, 
                 title_query: str = "", desc_query: str = "", filename_query: str = "", tags_query: str = "",
                 creator: str = "", numeric_filters: dict = None, tags: str = None) -> list[dict]:
    """
    Searches the database for items matching the criteria.
    Now supports complex multi-field searching with negative terms and numeric inequalities.
    """
    conn = get_connection(db_path)
    
    sql = "SELECT * FROM workshop_items WHERE 1=1"
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
