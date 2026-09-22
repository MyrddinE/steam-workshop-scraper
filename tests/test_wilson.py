import pytest
import json
import sqlite3
from src import database
from src.daemon import wilson_lower
from src.database import (
    initialize_database, insert_or_update_item, _evaluate_filters,
    compute_wilson_cutoffs, normalize_tags, search_items, get_connection,
    live_fetch_status_predicate, EXPECTED_VERSION,
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
    assert "0" in format_count(0) and "N/A" not in format_count(0)
    assert "N/A" in format_count(None)
    assert "N/A" in format_count("")

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
    assert version == EXPECTED_VERSION

def test_subscriber_score_uses_retention_formula(db_path):
    """Verify subscriber score uses subscriptions/lifetime_subscriptions ratio."""
    insert_or_update_item(db_path, {
        "workshop_id": 991,
        "subscriptions": 80,
        "lifetime_subscriptions": 100,
        "wilson_subscription_score": 0.99,
        "wilson_favorite_score": 0.5,
        "fetch_status": 200,
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


# ── percentile_disc cutoffs are the NTILE cutoffs, without the windows ───────


def _record_statements(monkeypatch):
    """Route `database.get_connection` through a wrapper recording every SQL."""
    statements = []
    real = database.get_connection

    class _RecordingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            statements.append(sql)
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(
        database, "get_connection", lambda path: _RecordingConnection(real(path)))
    return statements


def _seed_scores(db_path, count, with_tags=False):
    """Modulo gives ties; the every-17th/every-13th NULLs exercise NULL inputs."""
    for i in range(1, count + 1):
        item = {
            "workshop_id": i,
            "title": f"Item {i}",
            "fetch_status": 200,
            "wilson_favorite_score": None if i % 17 == 0 else (i % 37) / 37.0,
            "wilson_subscription_score": None if i % 13 == 0 else (i % 29) / 29.0,
        }
        if with_tags:
            item["tags"] = '"mod"' if i % 2 else '"map"'
        insert_or_update_item(db_path, item)


@pytest.mark.parametrize(
    "count", [0, 1, 5, 9, 10, 49, 50, 99, 100, 101, 199, 200, 999, 1000])
def test_percentile_disc_cutoffs_match_the_ntile_window(tmp_path, count):
    """Every count that changes the bucket arithmetic, ties and NULLs included."""
    db_path = str(tmp_path / f"scores-{count}.db")
    initialize_database(db_path)
    _seed_scores(db_path, count)

    where = "1=1 AND " + live_fetch_status_predicate("w.fetch_status")
    conn = get_connection(db_path)
    try:
        expected = database._wilson_cutoffs_ntile(conn, where, [])
    finally:
        conn.close()

    assert compute_wilson_cutoffs(db_path) == expected


def test_percentile_disc_cutoffs_match_the_ntile_window_with_a_filter(tmp_path):
    db_path = str(tmp_path / "filtered.db")
    initialize_database(db_path)
    _seed_scores(db_path, 300, with_tags=True)
    filters = [{"field": "Tags", "op": "contains", "value": "mod"}]

    where, params = database._wilson_population_where(filters, None, False)
    conn = get_connection(db_path)
    try:
        expected = database._wilson_cutoffs_ntile(conn, where, params)
    finally:
        conn.close()

    assert compute_wilson_cutoffs(db_path, filters=filters) == expected


def test_cutoffs_use_percentile_disc_not_a_materialised_ntile_window(tmp_path, monkeypatch):
    """The performance change itself: exact ranks, no ``NTILE`` window."""
    db_path = str(tmp_path / "mechanism.db")
    initialize_database(db_path)
    _seed_scores(db_path, 120)

    statements = _record_statements(monkeypatch)
    compute_wilson_cutoffs(db_path)

    aggregate_sql = [s for s in statements if "workshop_items" in s]
    assert aggregate_sql, "no cutoff query was recorded"
    # `"NTILE"` alone would also match inside "perceNTILE_disc".
    assert all("NTILE(" not in s.upper() for s in aggregate_sql), aggregate_sql
    assert any("percentile_disc" in s for s in aggregate_sql)


# ── percentile_disc is probed, not assumed: the NTILE(100) fallback ──────────


@pytest.mark.parametrize("count", [0, 1, 5, 10, 99, 100, 300])
def test_cutoffs_fall_back_to_ntile_when_percentile_disc_is_absent(
        tmp_path, count, monkeypatch):
    """Forcing the capability absent must not change any of the ten values.

    SQLite 3.49.1 (production) has no ``percentile_disc``; the fallback is not
    a second set of numbers.
    """
    db_path = str(tmp_path / f"fallback-{count}.db")
    initialize_database(db_path)
    _seed_scores(db_path, count)

    monkeypatch.setattr(database, "_PERCENTILE_DISC_AVAILABLE", None)
    present = compute_wilson_cutoffs(db_path)
    assert len(present) == 10

    monkeypatch.setattr(database, "_PERCENTILE_DISC_AVAILABLE", False)
    statements = _record_statements(monkeypatch)
    fallback = compute_wilson_cutoffs(db_path)

    assert fallback == present
    assert len(fallback) == 10
    aggregate_sql = [s for s in statements if "workshop_items" in s]
    assert any("NTILE(" in s.upper() for s in aggregate_sql), aggregate_sql


def test_fallback_path_sql_uses_ntile_and_never_percentile_disc(tmp_path, monkeypatch):
    """The executed SQL on the fallback path is the portable NTILE form."""
    db_path = str(tmp_path / "fallback-sql.db")
    initialize_database(db_path)
    _seed_scores(db_path, 120)

    monkeypatch.setattr(database, "_PERCENTILE_DISC_AVAILABLE", False)
    statements = _record_statements(monkeypatch)
    result = compute_wilson_cutoffs(db_path)

    assert len(result) == 10
    assert all("percentile_disc" not in s for s in statements), statements
    aggregate_sql = [s for s in statements if "workshop_items" in s]
    assert aggregate_sql, "no cutoff query was recorded"
    assert all("NTILE(" in s.upper() for s in aggregate_sql), aggregate_sql


def test_percentile_disc_probe_runs_at_most_once_per_process(tmp_path, monkeypatch):
    """The capability belongs to the linked library, so it is probed once."""
    db_path = str(tmp_path / "probe-once.db")
    initialize_database(db_path)
    _seed_scores(db_path, 20)

    monkeypatch.setattr(database, "_PERCENTILE_DISC_AVAILABLE", None)
    statements = _record_statements(monkeypatch)
    compute_wilson_cutoffs(db_path)
    compute_wilson_cutoffs(db_path)

    probes = [s for s in statements if s == database._PERCENTILE_DISC_PROBE]
    assert len(probes) == 1, statements
    assert database._PERCENTILE_DISC_AVAILABLE is not None


def test_probe_reads_a_missing_function_error_as_absent(tmp_path, monkeypatch):
    """The 3.49.1 path itself: a probe raising `no such function` means absent.

    This container's SQLite has ``percentile_disc``, so the real probe cannot
    take this branch; the wrapper makes the connection answer the way the
    production host's does.
    """
    db_path = str(tmp_path / "probe-missing.db")
    initialize_database(db_path)
    _seed_scores(db_path, 30)

    monkeypatch.setattr(database, "_PERCENTILE_DISC_AVAILABLE", None)
    real = database.get_connection

    class _MissingPercentileDisc:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if sql == database._PERCENTILE_DISC_PROBE:
                raise sqlite3.OperationalError("no such function: percentile_disc")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(
        database, "get_connection", lambda path: _MissingPercentileDisc(real(path)))
    result = compute_wilson_cutoffs(db_path)

    assert len(result) == 10
    assert database._PERCENTILE_DISC_AVAILABLE is False

