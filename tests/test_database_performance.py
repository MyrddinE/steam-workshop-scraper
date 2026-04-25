import pytest
import time
from src.database import initialize_database, insert_or_update_item, clear_pending_items

def test_clear_pending_items_performance(tmp_path):
    db_path = str(tmp_path / "perf.db")
    initialize_database(db_path)
    
    # Insert 5000 items, half pending, half not
    # Use a manual transaction for speed if possible, but insert_or_update_item handles it
    from src.database import get_connection
    conn = get_connection(db_path)
    
    # Faster bulk insert for testing
    data = []
    for i in range(5000):
        if i % 2 == 0:
            # Pending
            data.append((i, None, None, None))
        else:
            # Not pending
            data.append((i, "2023-01-01", "200", "2023-01-01"))
            
    conn.executemany("INSERT INTO workshop_items (workshop_id, dt_found, status, dt_updated) VALUES (?, ?, ?, ?)", data)
    conn.commit()
    conn.close()
    
    start_time = time.time()
    deleted_count = clear_pending_items(db_path)
    end_time = time.time()
    
    duration = end_time - start_time
    assert deleted_count == 2500
    # Should be very fast (under 0.5s on most systems for this size)
    assert duration < 1.0
