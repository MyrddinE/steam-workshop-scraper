import pytest
import time
from src.analysis import view_window_analysis
from src.database import initialize_database, insert_or_update_item

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test_analysis.db")
    initialize_database(path)
    return path

def test_view_window_analysis_empty(db_path):
    result = view_window_analysis(db_path)
    assert result["items_analyzed"] == 0
    assert result["buckets"] == []
    assert result["estimated_window_days"] is None

def test_view_window_analysis_basic(db_path):
    now = int(time.time())
    for i in range(100):
        age_days = i * 2  # 0 to ~198 days old
        created = now - age_days * 86400
        # Items younger than 60 days get many views; older items get few
        views = 1000 - (age_days * 15) if age_days < 60 else 10
        insert_or_update_item(db_path, {
            "workshop_id": i + 1,
            "time_created": created,
            "views": max(1, int(views)),
        })

    result = view_window_analysis(db_path, bucket_days=7)
    assert result["items_analyzed"] == 100
    assert len(result["buckets"]) > 0
    # Should have detected a knee
    assert result["estimated_window_days"] is not None

def test_view_window_analysis_custom_bucket(db_path):
    now = int(time.time())
    for i in range(50):
        insert_or_update_item(db_path, {
            "workshop_id": i + 1,
            "time_created": now - i * 86400,
            "views": 500,
        })

    result_7day = view_window_analysis(db_path, bucket_days=7)
    result_1day = view_window_analysis(db_path, bucket_days=1)
    # Smaller buckets should produce more buckets
    assert len(result_1day["buckets"]) > len(result_7day["buckets"])

def test_view_window_analysis_ignores_invalid(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "time_created": 0, "views": 100,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "time_created": None, "views": 100,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 3, "title": "No views", "time_created": int(time.time()),
    })
    result = view_window_analysis(db_path)
    assert result["items_analyzed"] == 0
