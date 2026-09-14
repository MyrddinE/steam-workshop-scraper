"""Tests for the named-metrics layer.

The interesting risk in this module is not that a query is wrong, it is that
moving a classification from Python into SQL silently changes which bucket an
item lands in. The equivalence tests below therefore run a literal copy of the
old Python logic over the rows that were actually stored, and compare it against
what the SQL produces.
"""

import time

import pytest

from src import metrics
from src.database import get_connection, get_db_stats, initialize_database, insert_or_update_item


# --------------------------------------------------------------------------
# reference implementations of the classifications the SQL replaced
# --------------------------------------------------------------------------


def _reference_translation_bucket(item) -> str:
    """The classification exactly as it was written in Python."""
    title = item["title"] or ""
    short_desc = item["short_description"] or ""
    ext_desc = item["extended_description"] or ""

    if item["title_en"] or item["short_description_en"] or item["extended_description_en"]:
        return "Translated"
    if title.isascii() and short_desc.isascii() and ext_desc.isascii():
        return "No translation needed (ASCII)"
    if item["translation_priority"] and item["translation_priority"] > 0:
        return "Queued"
    if not title:
        return "No data (never scraped)"
    return "Needs Translation (Unicode)"


def _reference_recency(attempted_at, staleness_days: int = 30) -> str:
    """The classification exactly as it was written in Python."""
    if not attempted_at:
        return "blank"
    try:
        threshold = int(time.time()) - staleness_days * 86400
        return "fresh" if int(attempted_at) >= threshold else "stale"
    except (ValueError, TypeError):
        return "blank"


TRANSLATION_BUCKETS = (
    "No translation needed (ASCII)",
    "Needs Translation (Unicode)",
    "Queued",
    "Translated",
    "No data (never scraped)",
)


def _case_index(label: str) -> int:
    """A stable id per parametrised case, so each lands on its own row."""
    return [c[0] for c in TRANSLATION_CASES].index(label)


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def test_registry_is_well_formed():
    assert metrics.REGISTRY, "no metrics registered"
    for name, spec in metrics.REGISTRY.items():
        assert spec.name == name, "a metric is registered under the wrong name"
        assert spec.tier in metrics.TIERS, f"{name}: bad tier {spec.tier!r}"
        assert spec.note, f"{name}: every metric explains what it answers"
        assert callable(spec.run)


def test_all_names_runs_cheapest_tier_first():
    """The order matters: it is the order a front end draws them in."""
    order = [metrics.REGISTRY[n].tier for n in metrics.all_names()]
    assert order == sorted(order, key=metrics.TIERS.index)
    assert len(metrics.all_names()) == len(metrics.REGISTRY), "a metric is missing from all_names()"


def test_compute_reports_value_cost_and_tier(db_path):
    result = metrics.compute(db_path, ["totals"])
    entry = result["totals"]
    assert entry["tier"] == metrics.INSTANT
    assert entry["value"] == {"total": 0, "dead": 0, "alive": 0}
    assert entry["ms"] >= 0.0
    assert entry["note"]


def test_compute_tier_returns_only_that_tier(db_path):
    result = metrics.compute_tier(db_path, metrics.FAST)
    assert result, "the fast tier is empty"
    assert {e["tier"] for e in result.values()} == {metrics.FAST}
    assert set(result) == set(metrics.names_in(metrics.FAST))


def test_unknown_metric_is_rejected(db_path):
    with pytest.raises(KeyError, match="unknown metric"):
        metrics.compute(db_path, ["totals", "not_a_metric"])


def test_a_broken_metric_does_not_take_down_the_others(db_path, monkeypatch):
    """The front ends render tiers independently, so one failure is local."""
    def explode(conn, params):
        raise RuntimeError("nope")

    boom = metrics.Metric(name="boom", tier=metrics.INSTANT, note="explodes", run=explode)
    monkeypatch.setitem(metrics.REGISTRY, "boom", boom)

    result = metrics.compute(db_path, ["totals", "boom"])
    assert result["totals"]["value"]["total"] == 0
    assert result["boom"]["value"] is None


def test_values_strips_the_timing_wrapper(db_path):
    assert metrics.values(metrics.compute(db_path, ["totals"])) == {
        "totals": {"total": 0, "dead": 0, "alive": 0}
    }


# --------------------------------------------------------------------------
# equivalence with the Python classification the SQL replaced
# --------------------------------------------------------------------------

#: Each case is (label, item fields). The labels name the trap each one covers.
TRANSLATION_CASES = [
    ("nothing stored at all", {}),
    ("plain ascii", {"title": "Hello", "extended_description": "World"}),
    ("ascii containing newlines and tabs", {"title": "Hello", "extended_description": "a\nb\tc"}),
    ("ascii containing control characters", {"title": "a\x01\x7fb"}),
    ("unicode title", {"title": "テスト"}),
    ("unicode only in the description", {"title": "Ok", "extended_description": "café"}),
    ("unicode and queued", {"title": "テスト", "translation_priority": 5}),
    ("translated", {"title": "テスト", "title_en": "Test"}),
    ("translated and queued at once", {"title": "テスト", "title_en": "Test", "translation_priority": 5}),
    ("empty title with a unicode description", {"extended_description": "テスト"}),
    ("emoji", {"title": "🎮"}),
    ("translated text identical to the original", {"title": "Same", "title_en": "Same"}),
    ("priority set but text already ascii", {"title": "Hello", "translation_priority": 9}),
]


@pytest.mark.parametrize("label,item", TRANSLATION_CASES, ids=[c[0] for c in TRANSLATION_CASES])
def test_translation_classification_matches_the_python_it_replaced(db_path, label, item):
    """The SQL must agree with the Python loop it was written to replace.

    The newline case is the one that matters: a GLOB over printable ASCII would
    classify every multi-line description as non-ASCII, because a newline is not
    a printable character. Python's str.isascii() accepts it, so the SQL has to
    as well.
    """
    insert_or_update_item(db_path, dict(item, workshop_id=100 + _case_index(label), status=200))

    conn = get_connection(db_path)
    rows = conn.execute("SELECT * FROM workshop_items").fetchall()
    conn.close()
    assert len(rows) == 1

    expected = _reference_translation_bucket(rows[0])
    actual = metrics.values(metrics.compute(db_path, ["translation_status"]))["translation_status"]

    for bucket in TRANSLATION_BUCKETS:
        assert actual[bucket] == (1 if bucket == expected else 0), (
            f"{label}: expected {expected!r}, metric put it in a different bucket"
        )


def test_translation_counts_agree_over_many_rows_at_once(db_path):
    """The same equivalence, but with a mix that makes the counts interesting."""
    now = int(time.time())
    for i, (_label, item) in enumerate(TRANSLATION_CASES):
        insert_or_update_item(db_path, dict(item, workshop_id=100 + i, status=200))

    conn = get_connection(db_path)
    rows = conn.execute("SELECT * FROM workshop_items").fetchall()
    conn.close()

    expected = {b: 0 for b in TRANSLATION_BUCKETS}
    for row in rows:
        expected[_reference_translation_bucket(row)] += 1

    actual = metrics.values(metrics.compute(db_path, ["translation_status"]))["translation_status"]
    assert actual == expected


@pytest.mark.parametrize("offset_days,expected", [
    (None, "blank"),
    (0, "fresh"),
    (-1, "fresh"),
    (-29, "fresh"),
    (-40, "stale"),
    (-400, "stale"),
])
def test_fetch_recency_matches_the_python_it_replaced(db_path, offset_days, expected):
    now = int(time.time())
    attempted = None if offset_days is None else now + offset_days * 86400
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "x", "status": 200,
        "last_fetch_attempted_at": attempted,
    })

    conn = get_connection(db_path)
    row = conn.execute("SELECT * FROM workshop_items").fetchone()
    conn.close()
    assert _reference_recency(row["last_fetch_attempted_at"]) == expected

    actual = metrics.values(metrics.compute(db_path, ["fetch_recency"]))["fetch_recency"]
    assert actual == {"fresh": 0, "stale": 0, "blank": 0, **{expected: 1}}


def test_fetch_recency_honours_a_non_default_staleness(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "x", "status": 200,
        "last_fetch_attempted_at": int(time.time()) - 40 * 86400,
    })

    default = metrics.values(metrics.compute(db_path, ["fetch_recency"]))["fetch_recency"]
    assert default["stale"] == 1

    relaxed = metrics.values(
        metrics.compute(db_path, ["fetch_recency"], {"staleness_days": 90})
    )["fetch_recency"]
    assert relaxed["fresh"] == 1


# --------------------------------------------------------------------------
# compatibility with get_db_stats
# --------------------------------------------------------------------------


def test_get_db_stats_still_returns_every_key_its_callers_use(db_path):
    """The TUI reads these names; the SQL underneath changed, not the contract."""
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Test", "status": 200})

    stats = get_db_stats(db_path)
    assert set(stats) == {
        "status_counts", "translation_status", "tag_counts", "fetch_recency_counts",
        "highest_api_fetched_at", "app_stats", "priority_breakdowns",
    }
    assert stats["translation_status"] == {
        "No translation needed (ASCII)": 1,
        "Needs Translation (Unicode)": 0,
        "Queued": 0,
        "Translated": 0,
        "No data (never scraped)": 0,
    }
    assert stats["fetch_recency_counts"] == {"fresh": 0, "stale": 0, "blank": 1}
    assert stats["status_counts"] == [{"status": 200, "count": 1}]


def test_get_db_stats_matches_the_metrics_it_wraps(db_path):
    """It is a wrapper, so the two must not drift apart."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "テスト", "status": 200, "translation_priority": 4,
    })

    stats = get_db_stats(db_path)
    computed = metrics.values(metrics.compute(db_path, [
        "status_counts", "translation_status", "tag_counts", "fetch_recency",
        "high_water", "app_tracking", "priority_breakdowns",
    ]))

    assert stats["status_counts"] == computed["status_counts"]
    assert stats["translation_status"] == computed["translation_status"]
    assert stats["tag_counts"] == computed["tag_counts"]
    assert stats["fetch_recency_counts"] == computed["fetch_recency"]
    assert stats["highest_api_fetched_at"] == computed["high_water"]
    assert stats["app_stats"] == computed["app_tracking"]
    assert stats["priority_breakdowns"] == computed["priority_breakdowns"]


def test_totals_separates_dead_items(db_path):
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "alive", "status": 200})
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "gone", "status": -1})

    assert metrics.values(metrics.compute(db_path, ["totals"]))["totals"] == {
        "total": 2, "dead": 1, "alive": 1,
    }


def test_coverage_counts_each_stage(db_path):
    """Coverage is the progress view: how far each stage has reached."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "full", "status": 200,
        "api_fetched_at": 1000, "extended_description": "desc",
        "image_extension": "jpg", "translate_version": 5, "creator": 42,
    })
    insert_or_update_item(db_path, {"workshop_id": 2, "title": "bare", "status": 200})

    cov = metrics.values(metrics.compute(db_path, ["coverage"]))["coverage"]
    assert cov["total"] == 2
    assert cov["api_fetched"] == 1
    assert cov["described"] == 1
    assert cov["imaged"] == 1
    assert cov["translated"] == 1
    assert cov["attributed"] == 1


def test_coverage_ignores_dead_items(db_path):
    """A dead item can never be covered, so it must not drag coverage down.

    Otherwise coverage would fall as the library is cleaned up, which is exactly
    backwards.
    """
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "live", "status": 200})
    insert_or_update_item(db_path, {
        "workshop_id": 2, "title": "gone", "status": -1, "api_fetched_at": 1000,
    })

    cov = metrics.values(metrics.compute(db_path, ["coverage"]))["coverage"]
    assert cov["total"] == 1
    assert cov["api_fetched"] == 0


def test_dead_items_are_not_counted_as_outstanding_work(db_path):
    """Regression: the backlog used to include items that could never complete."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "live", "status": 200, "needs_web_scrape": 5,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "title": "gone", "status": -1, "needs_web_scrape": 5,
    })

    breakdowns = metrics.values(
        metrics.compute(db_path, ["priority_breakdowns"])
    )["priority_breakdowns"]
    assert breakdowns["needs_web_scrape"] == [{"prio": 5, "cnt": 1}]


def test_stuck_work_surfaces_dead_items_still_queued(db_path):
    """The number the backlog deliberately leaves out must still be reported.

    The dead item is shaped the way `src/daemon.py` actually leaves one: the API
    priority is cleared by the 404 path while the other three queue flags are
    left untouched, which is what strands it.
    """
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "status": -1, "api_priority": 0,
        "needs_web_scrape": 1, "needs_image": 1, "translation_priority": 1,
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "title": "live", "status": 200, "needs_web_scrape": 5,
    })

    stuck = metrics.values(metrics.compute(db_path, ["stuck_work"]))["stuck_work"]
    assert stuck == {"web": 1, "image": 1, "translation": 1, "api": 0}


def test_tag_counts_reads_the_junction_table(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "x", "status": 200,
        "tags": ["Alpha", "Beta"],
    })
    insert_or_update_item(db_path, {
        "workshop_id": 2, "title": "y", "status": 200,
        "tags": ["Alpha"],
    })

    counts = metrics.values(metrics.compute(db_path, ["tag_counts"]))["tag_counts"]
    assert counts.get("Alpha") == 2
    assert counts.get("Beta") == 1


def test_priority_breakdowns_report_each_queue(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "x", "status": 200,
        "needs_web_scrape": 5, "needs_image": 10, "translation_priority": 3,
    })

    breakdowns = metrics.values(
        metrics.compute(db_path, ["priority_breakdowns"])
    )["priority_breakdowns"]
    assert breakdowns["needs_web_scrape"] == [{"prio": 5, "cnt": 1}]
    assert breakdowns["needs_image"] == [{"prio": 10, "cnt": 1}]
    assert breakdowns["translation_priority"] == [{"prio": 3, "cnt": 1}]


def test_every_metric_runs_against_a_real_database(db_path):
    """A smoke test: no metric may raise or return None on the real schema.

    `compute` swallows failures into None, so a broken query would otherwise
    pass silently.
    """
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "x", "status": 200,
        "api_fetched_at": int(time.time()),
    })

    result = metrics.compute(db_path)
    assert set(result) == set(metrics.all_names())
    for name, entry in result.items():
        assert entry["value"] is not None, f"{name} failed: {entry}"


def test_high_water_is_none_before_the_first_fetch(db_path):
    """No successful fetch yet is a real answer, not a failure."""
    assert metrics.values(metrics.compute(db_path, ["high_water"]))["high_water"] is None
