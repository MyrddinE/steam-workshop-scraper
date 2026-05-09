import pytest
import json
from src.daemon import wilson_lower
from src.database import (
    initialize_database, insert_or_update_item, _evaluate_filters,
    compute_wilson_cutoffs, normalize_tags, search_items, get_connection
)

# ── wilson_lower ─────────────────────────────────────────────────────────────

def test_wilson_zero_trials():
    assert wilson_lower(0, 0) == 0.0

def test_wilson_perfect_score():
    s = wilson_lower(100, 100)
    assert 0.95 < s < 1.0

def test_wilson_low_sample_penalty():
    a = wilson_lower(5, 10, z=1.96)
    b = wilson_lower(50, 100, z=1.96)
    assert a < b

def test_wilson_monotonic():
    s1 = wilson_lower(10, 100)
    s2 = wilson_lower(20, 100)
    s3 = wilson_lower(50, 100)
    assert s1 < s2 < s3

def test_wilson_bounds():
    for s, t in [(10, 100), (100, 1000), (500, 2000), (10000, 20000)]:
        score = wilson_lower(s, t)
        assert 0.0 <= score <= 1.0

# ── format_count ─────────────────────────────────────────────────────────────

def test_format_count():
    from src.tui import format_count
    assert "344" in format_count(344)
    assert "3.44K" in format_count(3440)
    assert "34.4K" in format_count(34400)
    assert "344K" in format_count(344000)
    assert "3.44M" in format_count(3440000)
    assert "34.4M" in format_count(34400000)
    assert "N/A" in format_count(0)
    assert "N/A" in format_count(None)

# ── Wilson score filter evaluation ───────────────────────────────────────────

def test_evaluate_wilson_filters(db_path):
    item = {"subscriptions": 100, "views": 200, "wilson_favorite_score": 0.85, "wilson_subscription_score": 0.72}
    assert _evaluate_filters(item, [
        {"field": "Subscriber Score", "op": "gte", "value": 0.5}
    ]) is True
    assert _evaluate_filters(item, [
        {"field": "Favorite Score", "op": "gte", "value": 0.9}
    ]) is False

def test_wilson_score_sort(db_path):
    for i in range(1, 6):
        insert_or_update_item(db_path, {
            "workshop_id": i, "title": f"Item {i}",
            "wilson_favorite_score": 0.1 * i,
            "wilson_subscription_score": 0.2 * i,
        })
    results = search_items(db_path, sort_by="wilson_favorite_score", sort_order="DESC")
    ids = [r["workshop_id"] for r in results]
    assert ids[0] == 5
    assert ids[-1] == 1

# ── compute_wilson_cutoffs ──────────────────────────────────────────────────

def test_compute_wilson_cutoffs_empty(db_path):
    result = compute_wilson_cutoffs(db_path)
    assert result.get("fav_p99") == 0

def test_compute_wilson_cutoffs_small_set(db_path):
    for i in range(1, 6):
        insert_or_update_item(db_path, {"workshop_id": i, "wilson_favorite_score": 0.1 * i})
    result = compute_wilson_cutoffs(db_path)
    assert len(result) >= 6

def test_compute_wilson_cutoffs_large_set(db_path):
    for i in range(10001):
        score = 0.2 + 0.6 * (i / 10000.0)
        insert_or_update_item(db_path, {
            "workshop_id": i + 1,
            "wilson_favorite_score": score,
            "wilson_subscription_score": score * 0.8,
        })

    result = compute_wilson_cutoffs(db_path)
    assert "fav_p99" in result
    assert "sub_p99" in result

    expected_p99 = 0.2 + 0.6 * (9900 / 10000.0)
    assert abs(result["fav_p99"] - expected_p99) < 0.01

    expected_p50 = 0.2 + 0.6 * (5000 / 10000.0)
    assert abs(result["fav_p50"] - expected_p50) < 0.02

def test_compute_wilson_cutoffs_with_filters(db_path):
    """Filtering by score > 0.5 should shift all cutoffs upward vs. unfiltered."""
    for i in range(1000):
        insert_or_update_item(db_path, {
            "workshop_id": i + 1,
            "wilson_favorite_score": 0.1 + 0.8 * (i / 999.0),
            "wilson_subscription_score": 0.1 + 0.7 * (i / 999.0),
        })
    result_all = compute_wilson_cutoffs(db_path)
    result_filtered = compute_wilson_cutoffs(db_path, filters=[
        {"field": "Favorite Score", "op": "gt", "value": 0.5}
    ])
    assert result_filtered["fav_p50"] > result_all["fav_p50"]
    assert result_filtered["sub_p50"] > result_all["sub_p50"]
    assert result_filtered["fav_p99"] > result_all["fav_p99"]

def test_schema_version_is_set(db_path):
    """Verify PRAGMA user_version is updated after migration."""
    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == 1

def test_subscriber_score_uses_retention_formula(db_path):
    """Verify subscriber score uses subscriptions/lifetime_subscriptions ratio."""
    insert_or_update_item(db_path, {
        "workshop_id": 991,
        "subscriptions": 80,
        "lifetime_subscriptions": 100,
        "wilson_favorite_score": 0.5,
        "wilson_subscription_score": 0.99,  # old/wrong formula
    })
    # Force migration by resetting version
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    initialize_database(db_path)
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT wilson_subscription_score FROM workshop_items WHERE workshop_id=991").fetchone()
    conn.close()
    assert row["wilson_subscription_score"] < 0.99
    assert row["wilson_subscription_score"] > 0.5
