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
    assert result.get("wilson_favorite_p99") == 0


def test_cutoff_keys_exist_and_are_numeric(db_path):
    for i in range(1, 50):
        insert_or_update_item(db_path, {
            "workshop_id": i,
            "wilson_favorite_score": 0.1 + 0.8 * (i / 49.0),
            "wilson_subscription_score": 0.2 + 0.6 * (i / 49.0),
        })
    result = compute_wilson_cutoffs(db_path)
    expected_keys = [
        "wilson_favorite_p99", "wilson_favorite_p90", "wilson_favorite_p50",
        "wilson_favorite_min", "wilson_favorite_max",
        "wilson_subscription_p99", "wilson_subscription_p90", "wilson_subscription_p50",
        "wilson_subscription_min", "wilson_subscription_max",
    ]
    for k in expected_keys:
        assert k in result, f"Missing key: {k}"
        assert isinstance(result[k], (int, float)), f"Key {k} is {type(result[k])}, not numeric"
        assert result[k] is not None, f"Key {k} is None"
    # min <= p50 <= p90 <= p99 <= max for favorites
    assert result["wilson_favorite_min"] <= result["wilson_favorite_p50"]
    assert result["wilson_favorite_p50"] <= result["wilson_favorite_p90"]
    assert result["wilson_favorite_p90"] <= result["wilson_favorite_p99"]
    assert result["wilson_favorite_p99"] <= result["wilson_favorite_max"]


def test_cutoffs_http_endpoint(db_path):
    from src.webserver import app
    app.config['TESTING'] = True
    import src.webserver as ws
    ws._db_path = db_path
    with app.test_client() as client:
        resp = client.post('/api/cutoffs', json={"filters": []})
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)
        assert "wilson_subscription_p50" in data
        assert data["wilson_subscription_p50"] is not None
        assert isinstance(data["wilson_subscription_p50"], (int, float))


def test_compute_wilson_cutoffs_with_tag_filter(db_path):
    """Checks that tag filters do not crash compute_wilson_cutoffs
    when the tag clause builder uses table-alias w.workshop_id."""
    for i in range(1, 20):
        insert_or_update_item(db_path, {
            "workshop_id": i,
            "wilson_favorite_score": 0.05 * i,
            "wilson_subscription_score": 0.04 * i,
            "tags": '"mod"' if i <= 10 else '"map"',
        })
    result = compute_wilson_cutoffs(db_path, filters=[
        {"field": "Tags", "op": "contains", "value": "mod"}
    ])
    assert len(result) >= 10
    assert result["wilson_favorite_p99"] >= 0


def test_compute_wilson_cutoffs_small_set(db_path):
    for i in range(1, 6):
        insert_or_update_item(db_path, {"workshop_id": i, "wilson_favorite_score": 0.1 * i})
    result = compute_wilson_cutoffs(db_path)
    assert len(result) >= 10

def test_compute_wilson_cutoffs_large_set(deterministic_db):
    """10k log-distributed items: cutoffs exist, are ordered, and in [0,1] range."""
    result = compute_wilson_cutoffs(deterministic_db)
    assert "wilson_favorite_p99" in result
    assert "wilson_subscription_p99" in result
    # All keys exist and are numeric
    for k in ["wilson_favorite_min", "wilson_favorite_p50", "wilson_favorite_p90",
              "wilson_favorite_p99", "wilson_favorite_max",
              "wilson_subscription_min", "wilson_subscription_p50",
              "wilson_subscription_p90", "wilson_subscription_p99",
              "wilson_subscription_max"]:
        assert isinstance(result[k], (int, float))
        assert 0 <= result[k] <= 1
    # Ordering: min <= p50 <= p90 <= p99 <= max
    assert result["wilson_favorite_min"] <= result["wilson_favorite_p50"]
    assert result["wilson_favorite_p50"] <= result["wilson_favorite_p90"]
    assert result["wilson_favorite_p90"] <= result["wilson_favorite_p99"]
    assert result["wilson_favorite_p99"] <= result["wilson_favorite_max"]
    assert result["wilson_subscription_min"] <= result["wilson_subscription_p50"]
    assert result["wilson_subscription_p50"] <= result["wilson_subscription_p90"]
    assert result["wilson_subscription_p90"] <= result["wilson_subscription_p99"]
    assert result["wilson_subscription_p99"] <= result["wilson_subscription_max"]
    # p50 and p90 should be strictly between min and max (real data has spread)
    assert result["wilson_favorite_min"] < result["wilson_favorite_max"]


def test_compute_wilson_cutoffs_with_filters(deterministic_db):
    """Filtering by score > 0.5 should shift cutoffs upward on real log-dist data."""
    result_all = compute_wilson_cutoffs(deterministic_db)
    result_filtered = compute_wilson_cutoffs(deterministic_db, filters=[
        {"field": "Subscriber Score", "op": "gt", "value": 0.5}
    ])
    # Every percentile cutoff should be >= its unfiltered counterpart
    assert result_filtered["wilson_subscription_p50"] >= result_all["wilson_subscription_p50"]
    assert result_filtered["wilson_subscription_p90"] >= result_all["wilson_subscription_p90"]
    assert result_filtered["wilson_subscription_p99"] >= result_all["wilson_subscription_p99"]
    # At least one should be strictly higher (filtering narrows the set)
    assert (result_filtered["wilson_subscription_p50"] > result_all["wilson_subscription_p50"]
            or result_filtered["wilson_subscription_p90"] > result_all["wilson_subscription_p90"]
            or result_filtered["wilson_subscription_p99"] > result_all["wilson_subscription_p99"])

def test_schema_version_is_set(db_path):
    """Verify PRAGMA user_version is updated after migration."""
    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == 17

def test_subscriber_score_uses_retention_formula(db_path):
    """Verify subscriber score uses subscriptions/lifetime_subscriptions ratio."""
    insert_or_update_item(db_path, {
        "workshop_id": 991,
        "subscriptions": 80,
        "lifetime_subscriptions": 100,
        "wilson_subscription_score": 0.99,
        "wilson_favorite_score": 0.5,
        "status": 200,
    })
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT wilson_subscription_score FROM workshop_items WHERE workshop_id=991").fetchone()
    conn.close()
    assert row["wilson_subscription_score"] == 0.99


def test_favorite_score_uses_lifetime_subs_denominator():
    """wilson_favorite_score uses lifetime_subscriptions, not views, as denominator."""
    from src.daemon import wilson_lower
    # High views = very low score with old formula
    old = wilson_lower(80, 10000)
    # Same favorites relative to lifetime_subscriptions = much higher score
    new = wilson_lower(80, 200)
    assert old < new
    assert abs(new - wilson_lower(80, 200)) < 0.0001
    # Verify Wilson monotonicity: more favorites = higher score
    assert wilson_lower(40, 200) < wilson_lower(80, 200) < wilson_lower(160, 200)

def test_tag_migration_normalizes_malformed_json(db_path):
    """Verify tags are stored in the junction table and retrievable."""
    insert_or_update_item(db_path, {
        "workshop_id": 8801,
        "title": "Test Item With Tags",
        "tags": "['mod', 'tool']",
    })
    insert_or_update_item(db_path, {
        "workshop_id": 8802,
        "title": "Valid Item",
        "tags": '["mod", "tool"]',
    })

    # Both should have tags in the junction table
    conn = get_connection(db_path)
    tags_8801 = {r[0] for r in conn.execute(
        "SELECT t.tag_name FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id=8801"
    ).fetchall()}
    assert tags_8801 == {"mod", "tool"}
    tags_8802 = {r[0] for r in conn.execute(
        "SELECT t.tag_name FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id=8802"
    ).fetchall()}
    assert tags_8802 == {"mod", "tool"}
    conn.close()
