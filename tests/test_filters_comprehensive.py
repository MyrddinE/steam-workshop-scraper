"""Comprehensive filter operator tests against a deterministic 10k-item database."""
import pytest
from src.database import search_items, compute_wilson_cutoffs, get_connection


# The fixture's Chinese items are identified by their non-ASCII source text.
# They used to carry language = 6 as the marker, but that column could never be
# populated from any Steam response and was dropped in migration 23->24, so the
# tests select the same population from the text that actually distinguishes it.
# Same predicate as metrics._ASCII_SQL_TEMPLATE, inverted.
_NON_ASCII_TITLE = (
    "length(CAST(COALESCE(title, '') AS BLOB)) != length(COALESCE(title, ''))"
)


# ── Tag operators (contains, does_not_contain) ────────────────────────────────

def test_tag_contains_matches(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "contains", "value": "mod"}
    ])
    assert len(result) > 0
    tag_sets = _get_tag_sets(deterministic_db, [r["workshop_id"] for r in result])
    for tags in tag_sets:
        assert "mod" in tags


def test_tag_contains_no_match(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "contains", "value": "zzz_nonexistent_tag_zzz"}
    ])
    assert len(result) == 0


def test_tag_does_not_contain_excludes(deterministic_db):
    all_items = search_items(deterministic_db)
    filtered = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "does_not_contain", "value": "mod"}
    ])
    assert len(filtered) > 0
    assert len(filtered) < len(all_items)
    tag_sets = _get_tag_sets(deterministic_db, [r["workshop_id"] for r in filtered])
    for tags in tag_sets:
        assert "mod" not in tags


def test_tag_contains_and(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "contains", "value": "mod", "logic": "AND"},
        {"field": "Tags", "op": "contains", "value": "skin", "logic": "AND"},
    ])
    assert len(result) > 0
    tag_sets = _get_tag_sets(deterministic_db, [r["workshop_id"] for r in result])
    for tags in tag_sets:
        assert "mod" in tags and "skin" in tags


def test_tag_is_empty(deterministic_db):
    """is_empty on tags shows items that have no tags at all."""
    result = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "is_empty", "value": ""}
    ])
    assert len(result) >= 0  # may be zero; just confirm no crash


def test_tag_is_not_empty(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "is_not_empty", "value": ""}
    ])
    assert len(result) > 0


# ── Full Text operators (contains, does_not_contain) ──────────────────────────

def test_fts_contains_matches(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Full Text", "op": "contains", "value": "lorem"}
    ])
    assert len(result) > 0


def test_fts_contains_no_match(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Full Text", "op": "contains", "value": "xyznonexistent12345"}
    ])
    assert len(result) == 0


def test_fts_does_not_contain(deterministic_db):
    all_items = search_items(deterministic_db)
    filtered = search_items(deterministic_db, filters=[
        {"field": "Full Text", "op": "does_not_contain", "value": "lorem"}
    ])
    assert len(filtered) > 0
    assert len(filtered) < len(all_items)


def test_fts_contains_chinese(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Full Text", "op": "contains", "value": "天地"}
    ])
    assert len(result) > 0
    conn = get_connection(deterministic_db)
    for r in result:
        row = conn.execute(
            f"SELECT {_NON_ASCII_TITLE} AS non_ascii FROM workshop_items WHERE workshop_id=?",
            (r["workshop_id"],),
        ).fetchone()
        if row:
            assert row["non_ascii"]
    conn.close()


def test_fts_contains_translation(deterministic_db):
    """Searching for a lorem word also matches Chinese items with _en translations."""
    result = search_items(deterministic_db, filters=[
        {"field": "Full Text", "op": "contains", "value": "dolor"}
    ])
    # Should find both lorem items and translated-chinese items
    conn = get_connection(deterministic_db)
    has_en = has_raw = False
    for r in result:
        row = conn.execute(
            f"SELECT title_en, {_NON_ASCII_TITLE} AS non_ascii "
            "FROM workshop_items WHERE workshop_id=?",
            (r["workshop_id"],),
        ).fetchone()
        if row and row["non_ascii"] and row["title_en"]:
            has_en = True
        elif row and not row["non_ascii"]:
            has_raw = True
    conn.close()
    assert has_raw  # English items match directly
    assert has_en   # Translated Chinese items match via _en


# ── Title / Description operators (contains, does_not_contain, is, is_not) ──

def test_title_contains(deterministic_db):
    conn = get_connection(deterministic_db)
    sample = conn.execute("SELECT title FROM workshop_items WHERE title LIKE '%lorem%' LIMIT 1").fetchone()
    conn.close()
    assert sample
    result = search_items(deterministic_db, filters=[
        {"field": "Title", "op": "contains", "value": "lorem"}
    ])
    assert len(result) > 0


def test_title_is_exact(deterministic_db):
    conn = get_connection(deterministic_db)
    row = conn.execute("SELECT workshop_id, title FROM workshop_items WHERE title LIKE '%lorem%' LIMIT 1").fetchone()
    conn.close()
    assert row
    result = search_items(deterministic_db, filters=[
        {"field": "Title", "op": "is", "value": row["title"]}
    ])
    assert any(r["workshop_id"] == row["workshop_id"] for r in result)


def test_title_is_not(deterministic_db):
    conn = get_connection(deterministic_db)
    row = conn.execute("SELECT title FROM workshop_items WHERE title LIKE '%lorem%' LIMIT 1").fetchone()
    conn.close()
    assert row
    all_items = search_items(deterministic_db)
    result = search_items(deterministic_db, filters=[
        {"field": "Title", "op": "is_not", "value": row["title"]}
    ])
    assert len(result) > 0
    assert len(result) < len(all_items)


def test_title_does_not_contain(deterministic_db):
    all_items = search_items(deterministic_db)
    result = search_items(deterministic_db, filters=[
        {"field": "Title", "op": "does_not_contain", "value": "lorem"}
    ])
    assert len(result) > 0
    assert len(result) < len(all_items)


def test_description_contains(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Description", "op": "contains", "value": "ipsum"}
    ])
    assert len(result) > 0


# ── Numeric operators (gt, lt, gte, lte, percentile) ─────────────────────────

def test_views_gt(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Views", "op": "gt", "value": 500000}
    ])
    assert len(result) > 0
    for r in result:
        assert (r["views"] or 0) > 500000


def test_views_lt(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Views", "op": "lt", "value": 100}
    ])
    assert len(result) > 0
    for r in result:
        assert (r["views"] or 0) < 100


def test_subs_gte_lte_range(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Subs", "op": "gte", "value": 1000},
        {"field": "Subs", "op": "lte", "value": 5000},
    ])
    for r in result:
        assert 1000 <= (r["subscriptions"] or 0) <= 5000


def test_file_size_gt(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "File Size", "op": "gt", "value": 10_000_000}
    ])
    assert len(result) > 0
    for r in result:
        assert (r["file_size"] or 0) > 10_000_000


def test_favs_lt(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Favs", "op": "lt", "value": 100}
    ])
    assert len(result) > 0
    for r in result:
        assert (r["favorited"] or 0) < 100


def test_score_gt(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Subscriber Score", "op": "gt", "value": 0.5}
    ])
    assert len(result) > 0
    for r in result:
        assert (r["wilson_subscription_score"] or 0) > 0.5


def test_views_percentile_approx(deterministic_db):
    """p50 should return roughly half the items; p90 roughly 10%."""
    all_items = search_items(deterministic_db)
    p50 = search_items(deterministic_db, filters=[
        {"field": "Views", "op": "percentile", "value": 50}
    ])
    p90 = search_items(deterministic_db, filters=[
        {"field": "Views", "op": "percentile", "value": 90}
    ])
    # With percentile, the filter keeps top N%. So p50 keeps top 50% = ~5000 items.
    assert 0.30 * len(all_items) <= len(p50) <= 0.70 * len(all_items)
    assert 0.03 * len(all_items) <= len(p90) <= 0.17 * len(all_items)


def test_subs_percentile_p0_returns_all(deterministic_db):
    """p0 should return all items (no filter)."""
    all_items = search_items(deterministic_db)
    result = search_items(deterministic_db, filters=[
        {"field": "Subs", "op": "percentile", "value": 0}
    ])
    assert len(result) == len(all_items)


def test_subs_percentile_p99_returns_few(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Subs", "op": "percentile", "value": 99}
    ])
    assert 1 <= len(result) <= 300  # top 1% should be ~100 items


def test_percentile_with_tag_filter(deterministic_db):
    """Percentile combined with tag filter — exercises _compute_percentile_threshold
    with tag clauses in base_filters (w.workshop_id in the subquery)."""
    result = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "contains", "value": "mod"},
        {"field": "Subs", "op": "percentile", "value": 50},
    ])
    assert len(result) > 0
    tag_sets = _get_tag_sets(deterministic_db, [r["workshop_id"] for r in result])
    for tags in tag_sets:
        assert "mod" in tags


# ── ID operators (is, is_not) ─────────────────────────────────────────────────

def test_author_id_is(deterministic_db):
    conn = get_connection(deterministic_db)
    author = conn.execute("SELECT creator FROM workshop_items LIMIT 1").fetchone()["creator"]
    conn.close()
    result = search_items(deterministic_db, filters=[
        {"field": "Author ID", "op": "is", "value": author}
    ])
    assert len(result) > 0
    for r in result:
        assert r["creator"] == author


def test_author_id_is_not(deterministic_db):
    conn = get_connection(deterministic_db)
    author = conn.execute("SELECT creator FROM workshop_items LIMIT 1").fetchone()["creator"]
    conn.close()
    all_items = search_items(deterministic_db)
    result = search_items(deterministic_db, filters=[
        {"field": "Author ID", "op": "is_not", "value": author}
    ])
    assert len(result) > 0
    assert len(result) < len(all_items)
    for r in result:
        assert r["creator"] != author


def test_workshop_id_is(deterministic_db):
    conn = get_connection(deterministic_db)
    wid = conn.execute("SELECT workshop_id FROM workshop_items LIMIT 1").fetchone()["workshop_id"]
    conn.close()
    result = search_items(deterministic_db, filters=[
        {"field": "Workshop ID", "op": "is", "value": wid}
    ])
    assert len(result) == 1
    assert result[0]["workshop_id"] == wid


def test_workshop_id_is_not(deterministic_db):
    conn = get_connection(deterministic_db)
    wid = conn.execute("SELECT workshop_id FROM workshop_items LIMIT 1").fetchone()["workshop_id"]
    conn.close()
    all_items = search_items(deterministic_db)
    result = search_items(deterministic_db, filters=[
        {"field": "Workshop ID", "op": "is_not", "value": wid}
    ])
    assert len(result) == len(all_items) - 1


def test_app_id_is(deterministic_db):
    conn = get_connection(deterministic_db)
    appid = conn.execute("SELECT consumer_appid FROM workshop_items LIMIT 1").fetchone()["consumer_appid"]
    conn.close()
    result = search_items(deterministic_db, filters=[
        {"field": "App ID", "op": "is", "value": appid}
    ])
    assert len(result) > 0
    for r in result:
        assert r["consumer_appid"] == appid


def test_app_id_is_not(deterministic_db):
    conn = get_connection(deterministic_db)
    appid = conn.execute("SELECT consumer_appid FROM workshop_items LIMIT 1").fetchone()["consumer_appid"]
    conn.close()
    all_items = search_items(deterministic_db)
    result = search_items(deterministic_db, filters=[
        {"field": "App ID", "op": "is_not", "value": appid}
    ])
    assert len(result) > 0
    assert len(result) < len(all_items)
    for r in result:
        assert r["consumer_appid"] != appid


# ── Percentile shift with filters ─────────────────────────────────────────────

def test_percentile_cutoffs_shift_with_filter(deterministic_db):
    """Filtering to 'Subs > 100k' should dramatically raise subscription score cutoffs."""
    baseline = compute_wilson_cutoffs(deterministic_db)
    filtered = compute_wilson_cutoffs(deterministic_db, filters=[
        {"field": "Subs", "op": "gt", "value": 100000}
    ])
    # Subscription score cutoffs should rise (higher subs → higher retention scores)
    assert filtered["wilson_subscription_p50"] > baseline["wilson_subscription_p50"]
    assert filtered["wilson_subscription_p90"] > baseline["wilson_subscription_p90"]
    assert filtered["wilson_subscription_p99"] > baseline["wilson_subscription_p99"]
    # Favorite scores may go down (high lifetime_subs inflates denominator)
    # Just confirm the filtered set is strictly different from baseline
    assert filtered["wilson_favorite_p50"] != baseline["wilson_favorite_p50"]
    assert filtered["wilson_favorite_p90"] != baseline["wilson_favorite_p90"]


# ── Cross-field combinations ──────────────────────────────────────────────────

def test_and_logic_two_fields(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "contains", "value": "mod", "logic": "AND"},
        {"field": "Views", "op": "gt", "value": 1000, "logic": "AND"},
    ])
    assert len(result) > 0
    tag_sets = _get_tag_sets(deterministic_db, [r["workshop_id"] for r in result])
    for r, tags in zip(result, tag_sets):
        assert "mod" in tags
        assert (r["views"] or 0) > 1000


def test_or_logic(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "Tags", "op": "contains", "value": "mod", "logic": "OR"},
        {"field": "Views", "op": "gt", "value": 500000, "logic": "OR"},
    ])
    assert len(result) > 0


def test_unknown_field_graceful(deterministic_db):
    result = search_items(deterministic_db, filters=[
        {"field": "NoSuchField", "op": "contains", "value": "anything"}
    ])
    assert len(result) >= 0  # not a crash


def test_sort_with_filter(deterministic_db):
    result = search_items(deterministic_db,
                          filters=[{"field": "Subs", "op": "gt", "value": 100}],
                          sort_by="views", sort_order="DESC")
    assert len(result) > 0
    for i in range(len(result) - 1):
        assert (result[i]["views"] or 0) >= (result[i + 1]["views"] or 0)


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_zero_views_items_exist(deterministic_db):
    conn = get_connection(deterministic_db)
    cnt = conn.execute("SELECT COUNT(*) FROM workshop_items WHERE views = 0").fetchone()[0]
    conn.close()
    assert cnt > 0


def test_chinese_items_have_non_ascii_titles(deterministic_db):
    conn = get_connection(deterministic_db)
    cnt = conn.execute(
        f"SELECT COUNT(*) FROM workshop_items WHERE {_NON_ASCII_TITLE}"
    ).fetchone()[0]
    conn.close()
    assert cnt > 0


def test_translated_items_have_en_fields(deterministic_db):
    conn = get_connection(deterministic_db)
    cnt = conn.execute(
        f"SELECT COUNT(*) FROM workshop_items WHERE title_en IS NOT NULL AND {_NON_ASCII_TITLE}"
    ).fetchone()[0]
    conn.close()
    assert cnt > 0


# ── Helper ────────────────────────────────────────────────────────────────────

def _get_tag_sets(db_path, wids):
    conn = get_connection(db_path)
    tag_sets = []
    for wid in wids:
        rows = conn.execute(
            "SELECT t.tag_name FROM workshop_tags wt JOIN tags t USING(tag_id) WHERE wt.workshop_id=?",
            (wid,)
        ).fetchall()
        tag_sets.append({r["tag_name"] for r in rows})
    conn.close()
    return tag_sets


# ── Fuzz test: random filter combinations must not crash ──────────────────────

def test_random_filter_combinations_no_crash(deterministic_db):
    """Call search_items with random filter combinations to catch SQL errors."""
    import random
    from src.database import get_connection, FILTER_SCHEMA

    rng = random.Random(42)

    # Build field→ops map and type map from central schema
    field_ops = {f["field"]: f["ops"] for f in FILTER_SCHEMA}
    ID_FIELDS = [f["field"] for f in FILTER_SCHEMA if f["type"] == "id"]
    STRING_FIELDS = [f["field"] for f in FILTER_SCHEMA if f["type"] == "string"]

    # Pre-fetch some real values for "is" operators
    conn = get_connection(deterministic_db)
    sample_title = conn.execute(
        "SELECT title FROM workshop_items WHERE title LIKE '%lorem%' LIMIT 1"
    ).fetchone()
    sample_author = conn.execute("SELECT creator FROM workshop_items LIMIT 1").fetchone()
    sample_wid = conn.execute("SELECT workshop_id FROM workshop_items LIMIT 1").fetchone()
    sample_appid = conn.execute("SELECT consumer_appid FROM workshop_items LIMIT 1").fetchone()
    conn.close()

    for _ in range(50):
        num_filters = rng.randint(1, 5)
        filters = []
        for i in range(num_filters):
            field = rng.choice(list(field_ops.keys()))
            op = rng.choice(field_ops[field])

            # Pick a reasonable value for the operator
            if op == "percentile":
                val = rng.randint(0, 99)
            elif op in ("is", "is_not"):
                if field == "Title":
                    val = sample_title["title"] if sample_title else "lorem"
                elif field == "Author ID":
                    val = sample_author["creator"] if sample_author else 1_000_000_000
                elif field == "Workshop ID":
                    val = sample_wid["workshop_id"] if sample_wid else 1_000_000
                elif field == "App ID":
                    val = sample_appid["consumer_appid"] if sample_appid else 10000
                else:
                    val = "lorem"
            elif field in STRING_FIELDS:
                val = rng.choice(["mod", "lorem", "ipsum", "天地", "dolor"])
            else:
                val = rng.randint(0, 1_000_000)

            f = {"field": field, "op": op, "value": val}
            if i > 0:
                f["logic"] = rng.choice(["AND", "OR"])
            filters.append(f)

        # Must not crash
        try:
            results = search_items(deterministic_db, filters=filters)
            assert isinstance(results, list)
        except Exception as e:
            pytest.fail(f"search_items crashed with filters={filters}: {e}")
