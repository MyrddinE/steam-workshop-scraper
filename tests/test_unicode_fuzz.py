import sqlite3
import json
import pytest
from hypothesis import given, strategies as st
from src.database import initialize_database, insert_or_update_item, search_items, get_connection

@pytest.fixture(scope="module")
def fuzzy_db(tmp_path_factory):
    """Module-scoped DB for fuzzing to avoid initialization overhead."""
    db_path = str(tmp_path_factory.mktemp("fuzzy") / "fuzzy.db")
    initialize_database(db_path)
    return db_path

@given(
    workshop_id=st.integers(min_value=1, max_value=2**63-1),
    title=st.text(),
    description=st.text(),
    tags=st.lists(st.text())
)
def test_database_unicode_fuzzing(fuzzy_db, workshop_id, title, description, tags):
    """
    Property-based test to ensure any string (Unicode, emoji, control chars) 
    can be stored and retrieved without crashing.
    """
    item_data = {
        "workshop_id": workshop_id,
        "title": title,
        "short_description": description,
        "tags": json.dumps(tags, ensure_ascii=False)
    }
    
    # Should not raise any encoding errors
    insert_or_update_item(fuzzy_db, item_data)
    
    # Verify retrieval preserves the exact content
    results = search_items(fuzzy_db, query=title if title else None)
    # Note: SQLite LIKE might be case-insensitive or handle certain unicode differently, 
    # but we just want to ensure the specific record is readable.
    conn = get_connection(fuzzy_db)
    row = conn.execute("SELECT * FROM workshop_items WHERE workshop_id = ?", (workshop_id,)).fetchone()
    conn.close()
    
    assert row["workshop_id"] == workshop_id
    assert row["title"] == title
    assert row["short_description"] == description
    assert row["tags"] == json.dumps(tags, ensure_ascii=False)

def test_manual_complex_unicode(fuzzy_db):
    """Explicit test for known complex scripts."""
    complex_item = {
        "workshop_id": 999999,
        "title": "Здравствуй, мир! 🚀 世界你好",
        "extended_description": "नमस्ते - 한국어 - ⛔️ \uf8ff",
        "tags": json.dumps(["🔥", "русский", "中文"], ensure_ascii=False)
    }
    insert_or_update_item(fuzzy_db, complex_item)
    
    results = search_items(fuzzy_db, query="мир")
    assert len(results) >= 1
    assert results[0]["title"] == complex_item["title"]
    assert "🚀" in results[0]["title"]
    assert "한국어" in results[0]["extended_description"]
