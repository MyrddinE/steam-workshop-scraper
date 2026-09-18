"""Coverage at two scopes: the whole live library, and what the owner cares about.

The owner wants the existing coverage figure and, beside it, the same figure
restricted to the items the target AppIDs' stored `enrichment_filters` select.
That second population is the one the daemon calls *enriched*.

The tests pin the properties the design rests on:

* it is the **union** of what any target AppID's filters select, not the
  intersection and not the first AppID's;
* an unreadable or empty filter set means *everything* for that AppID, so the
  two figures coincide and the scope note says why rather than looking broken;
* the population is the search builder's SQL translation, which also searches
  each text field's ``_en`` counterpart and therefore can disagree with the
  daemon's in-memory ``_evaluate_filters`` -- the documented difference;
* the filtered query plans through the ``consumer_appid`` index rather than
  scanning the item table, which is why no new index was added.

The old `coverage` returned a single unscoped dict; the scoped figure is new, so
the "fails against the old behaviour" run is the one recorded in the commit
message: against the old code `cov["filtered"]` raised ``KeyError``.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src import metrics
from src.database import (
    _evaluate_filters,
    build_filter_clause_sql,
    get_connection,
    insert_or_update_item,
)


def _set_filters(db_path, appid, filters):
    """Store an AppID's enrichment filters, verbatim when a string is given."""
    raw = filters if isinstance(filters, str) else json.dumps(filters)
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO app_tracking (appid, enrichment_filters) VALUES (?, ?)",
        (appid, raw),
    )
    conn.commit()
    conn.close()


def _item(db_path, workshop_id, appid, tags=(), **over):
    record = {"workshop_id": workshop_id, "title": f"item {workshop_id}",
              "status": 200, "consumer_appid": appid, "tags": list(tags)}
    record.update(over)
    insert_or_update_item(db_path, record)


def _coverage(db_path, target_appids):
    return metrics.values(
        metrics.compute(db_path, ["coverage"], {"target_appids": target_appids})
    )["coverage"]


def _tag_filter(tag):
    return [{"field": "Tags", "op": "contains", "value": tag}]


# --------------------------------------------------------------------------
# the two figures
# --------------------------------------------------------------------------


def test_the_filtered_figure_differs_from_the_total_when_filters_exclude(db_path):
    """The whole point: the two numbers are not the same number twice."""
    _set_filters(db_path, 294100, _tag_filter("Mature"))
    _item(db_path, 1, 294100, tags=["Mature"])
    _item(db_path, 2, 294100)                       # the AppID's, but no tag
    _item(db_path, 3, 4000, tags=["Mature"])        # the tag, but another AppID

    cov = _coverage(db_path, [294100])

    assert cov["total"] == 3, "the first figure is every live item"
    assert cov["filtered"]["total"] == 1, "the second is the filters' population"
    assert cov["filtered"]["appids"] == [294100]
    assert cov["filtered"]["with_filters"] == [294100]
    assert cov["filtered"]["restricting"] == [294100]


def test_both_figures_are_reported_with_the_same_stage_shape(db_path):
    _set_filters(db_path, 294100, _tag_filter("Mature"))
    _item(db_path, 1, 294100, tags=["Mature"], api_fetched_at=1000,
          extended_description="desc", image_extension="jpg",
          translate_version=1, creator=7)
    _item(db_path, 2, 294100)

    cov = _coverage(db_path, [294100])

    stages = {"total", "api_fetched", "described", "imaged", "translated", "attributed"}
    assert stages <= set(cov)
    assert stages <= set(cov["filtered"])
    assert cov["api_fetched"] == 1
    assert cov["filtered"]["api_fetched"] == 1
    assert cov["filtered"]["described"] == 1


def test_multiple_target_appids_are_unioned(db_path):
    """What I care about is whatever *any* target's filters select."""
    _set_filters(db_path, 1, _tag_filter("A"))
    _set_filters(db_path, 2, _tag_filter("B"))
    _item(db_path, 10, 1, tags=["A"])
    _item(db_path, 11, 2, tags=["B"])
    _item(db_path, 12, 1)                    # target AppID, filters exclude
    _item(db_path, 13, 9, tags=["A", "B"])   # matches both, but is not a target

    cov = _coverage(db_path, [1, 2])

    assert cov["total"] == 4
    assert cov["filtered"]["total"] == 2
    assert cov["filtered"]["appids"] == [1, 2]
    assert cov["filtered"]["restricting"] == [1, 2]


# --------------------------------------------------------------------------
# the "everything" contract
# --------------------------------------------------------------------------


def test_an_unreadable_filter_set_means_everything(db_path):
    """A malformed set must not read as "excludes everything"."""
    _set_filters(db_path, 294100, "{ this is not json")
    _item(db_path, 1, 294100)
    _item(db_path, 2, 294100)

    cov = _coverage(db_path, [294100])

    assert cov["total"] == cov["filtered"]["total"] == 2
    assert cov["filtered"]["unreadable"] == [294100]
    assert cov["filtered"]["with_filters"] == []
    assert cov["filtered"]["restricting"] == []


def test_an_empty_filter_set_means_everything(db_path):
    _set_filters(db_path, 294100, [])
    _item(db_path, 1, 294100)
    _item(db_path, 2, 294100)

    cov = _coverage(db_path, [294100])

    assert cov["total"] == cov["filtered"]["total"] == 2
    assert cov["filtered"]["with_filters"] == []
    assert cov["filtered"]["unreadable"] == []


def test_no_target_appids_means_everything(db_path):
    """With nothing to restrict to, both figures count the whole live library."""
    _item(db_path, 1, 294100)
    _item(db_path, 2, 4000)

    cov = _coverage(db_path, [])

    assert cov["total"] == cov["filtered"]["total"] == 2
    assert cov["filtered"]["appids"] == []


def test_a_percentile_filter_has_no_fixed_predicate_and_restricts_nothing(db_path):
    """It is relative to the result set, and the daemon ignores it too."""
    _set_filters(db_path, 294100,
                 [{"field": "Subs", "op": "percentile", "value": 90}])
    _item(db_path, 1, 294100, subscriptions=5)
    _item(db_path, 2, 294100, subscriptions=1)

    cov = _coverage(db_path, [294100])

    assert cov["total"] == cov["filtered"]["total"] == 2
    assert cov["filtered"]["with_filters"] == [294100]
    assert cov["filtered"]["restricting"] == []


# --------------------------------------------------------------------------
# the documented difference from the daemon's per-item decision
# --------------------------------------------------------------------------


def test_the_translation_searches_the_en_counterpart_where_the_daemon_does_not(db_path):
    """The SQL builder dual-searches `_en`; `_evaluate_filters` reads the original.

    The owner asked for the figure to be *the search builder's translation* of
    the filters, with the difference documented rather than hidden. This pins
    both sides so the claim cannot quietly become false: an item whose stored
    translation matches is inside the scoped figure, while the daemon's own
    predicate rejects it.
    """
    filters = [{"field": "Title", "op": "contains", "value": "Hello"}]
    _set_filters(db_path, 294100, filters)
    _item(db_path, 1, 294100, title="こんにちは", title_en="Hello world")
    _item(db_path, 2, 294100, title="Hello")

    cov = _coverage(db_path, [294100])

    assert cov["filtered"]["total"] == 2, \
        "the search builder searches title OR title_en"

    daemon_item = {"title": "こんにちは", "title_en": "Hello world"}
    assert _evaluate_filters(daemon_item, filters) is False, \
        "the daemon's in-memory check reads the original column alone"


def test_the_shared_builder_is_what_the_search_uses():
    """`search_items` and the metric call one function, so they cannot drift."""
    clause, params = build_filter_clause_sql(
        [{"field": "Title", "op": "contains", "value": "Hello"}])
    assert "title LIKE ?" in clause and "title_en LIKE ?" in clause
    assert params == ["%Hello%", "%Hello%"]


# --------------------------------------------------------------------------
# cost: the filtered query reaches its rows through the AppID index
# --------------------------------------------------------------------------


def _plan(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    finally:
        conn.close()
    return " | ".join(row[3] for row in rows)


def test_the_filtered_query_uses_the_appid_index(db_path):
    """No full scan for the scoped figure, so no new index is warranted.

    The overall figure is one scan of `workshop_items`, exactly as the old
    coverage was. The scoped figure is an OR of per-AppID branches; each branch
    must reach its rows by `consumer_appid` rather than scanning the table.
    """
    _set_filters(db_path, 1, _tag_filter("A"))
    _set_filters(db_path, 2, _tag_filter("B"))
    conn = get_connection(db_path)
    conn.executemany(
        "INSERT INTO workshop_items (workshop_id, status, consumer_appid) "
        "VALUES (?, 200, ?)",
        [(i, 1 if i % 2 else 2) for i in range(1, 401)],
    )
    conn.commit()

    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        metrics._coverage(conn, {"target_appids": [1, 2]})
    finally:
        conn.close()

    filtered_sql = next(s for s in seen if "consumer_appid" in s)
    plan = _plan(db_path, filtered_sql)
    assert "SEARCH w USING" in plan, plan
    assert "SCAN w" not in plan, plan
