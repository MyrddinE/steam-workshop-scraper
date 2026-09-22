"""An AND of ``Tags contains`` rows must drive the scan, not be checked per row.

Issue 84. Each ``contains`` row renders a correlated ``EXISTS`` over the tag
junction, so a conjunction of them can only be *checked* against the rows
whatever index the planner walks; along a sort index whose order does not match
the filter that approaches a full scan. Collapsing the conjunction into one
``workshop_id IN (SELECT ... HAVING COUNT(DISTINCT tag_name) = N)`` subquery
lets the tag set drive instead.

``tests/test_filters_comprehensive.py`` already asserts the *rows* a tag filter
returns; the new thing here is the equivalence of the two clause forms across
every shape the builder emits -- so the collapse can be trusted -- and the plan
the owner's filter shape produces. See
:func:`src.database._tag_contains_all_clause` and docs/search-filter.md.
"""
import sqlite3

import pytest

from src import database
from src.database import (
    build_filters_sql,
    get_connection,
    initialize_database,
    insert_or_update_item,
    search_items,
)


# ---------------------------------------------------------------------------
# a fixture whose tag sets overlap, so a union/count mistake changes the rows
# ---------------------------------------------------------------------------

# workshop_id -> tag names. A is common, B less so, and every pairwise and
# triple intersection is a proper subset, so a form that got the conjunction
# wrong (an OR, or a count short of the number of names) selects a different
# set. ``a`` sits next to ``A`` because the ``tag_name = ?`` this replaces is
# BINARY: the two are different tags and must stay different tags. 101 carries
# no tags at all. Sizes split the set so a tag pair can be separated by a
# non-tag predicate.
_TAG_FIXTURE = {
    101: ([], 5),
    102: (["A"], 50),
    103: (["B"], 50),
    104: (["C"], 50),
    105: (["A", "B"], 50),
    106: (["A", "C"], 50),
    107: (["B", "C"], 50),
    108: (["A", "B", "C"], 5),
    109: (["A", "B", "D"], 50),
    110: (["a"], 50),
    111: (["A", "a"], 50),
    112: (["D"], 5),
}


@pytest.fixture(scope="module")
def tag_fixture_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("tag-filter") / "tags.db")
    initialize_database(path)
    for workshop_id, (tags, file_size) in _TAG_FIXTURE.items():
        insert_or_update_item(path, {
            "workshop_id": workshop_id,
            "title": f"item {workshop_id}",
            "file_size": file_size,
            "fetch_status": 200,
            "tags": tags,
        })
    return path


# ---------------------------------------------------------------------------
# equivalence: the old per-row clause and the new builder select the same ids
# ---------------------------------------------------------------------------

# The pre-change translation, pinned here so the comparison does not depend on
# `_build_tag_clause` staying put: one clause per row, joined by its own logic.
_OLD_TAG_CONTAINS = (
    "EXISTS (SELECT 1 FROM workshop_tags wt JOIN tags t USING(tag_id) "
    "WHERE wt.workshop_id = w.workshop_id AND t.tag_name = ?)"
)
_OLD_TAG_DOES_NOT_CONTAIN = (
    "NOT EXISTS (SELECT 1 FROM workshop_tags wt JOIN tags t USING(tag_id) "
    "WHERE wt.workshop_id = w.workshop_id AND t.tag_name = ?)"
)


def _tag(value, logic="AND"):
    return {"field": "Tags", "op": "contains", "value": value, "logic": logic}


def _dnc(value, logic="AND"):
    return {"field": "Tags", "op": "does_not_contain", "value": value, "logic": logic}


def _size(value, logic="AND"):
    return {"field": "File Size", "op": "gt", "value": value, "logic": logic}


def _old_form(filters):
    clauses, params = [], []
    for f in filters:
        if f["field"] == "Tags":
            clause = (_OLD_TAG_CONTAINS if f["op"] == "contains"
                      else _OLD_TAG_DOES_NOT_CONTAIN)
        else:
            clause = "file_size > ?"
        clauses.append((f.get("logic", "AND").upper(), clause))
        params.append(f["value"])
    sql = ""
    for idx, (logic, clause) in enumerate(clauses):
        sql += f" {logic} " if idx else ""
        sql += clause
    return sql, params


def _ids(db_path, clause, params):
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT w.workshop_id FROM workshop_items w WHERE ({clause}) "
            "ORDER BY w.workshop_id", params).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


# Every shape the builder emits, each on a fixture that makes it non-trivial.
_SHAPES = {
    "one tag": [_tag("A")],
    "two ANDed tags": [_tag("A"), _tag("B")],
    "three ANDed tags": [_tag("A"), _tag("B"), _tag("C")],
    "two ORed tags": [_tag("A"), _tag("B", "OR")],
    "AND pair then OR": [_tag("A"), _tag("B"), _tag("C", "OR")],
    "OR then AND pair": [_tag("A", "OR"), _tag("B"), _tag("C")],
    "contains and does_not_contain": [_tag("A"), _dnc("D")],
    "does_not_contain between two contains": [_tag("A"), _dnc("D"), _tag("B")],
    "two contains and a does_not_contain": [_tag("A"), _tag("B"), _dnc("D")],
    "tag matching nothing": [_tag("ZZZ")],
    "a matching tag and one that matches nothing": [_tag("A"), _tag("ZZZ")],
    "duplicate tag name": [_tag("A"), _tag("A")],
    "names differing only in case": [_tag("A"), _tag("a")],
    "tags separated by a size filter": [_tag("A"), _size(10), _tag("B")],
}


@pytest.mark.parametrize("name", list(_SHAPES))
def test_the_collapsed_form_selects_the_same_rows(tag_fixture_db, name):
    """The rewrite is a plan change; it must not be a row change."""
    filters = _SHAPES[name]
    old_clause, old_params = _old_form(filters)
    new_clause, new_params = build_filters_sql(filters)

    old_ids = _ids(tag_fixture_db, old_clause, old_params)
    new_ids = _ids(tag_fixture_db, new_clause, new_params)
    assert new_ids == old_ids, (
        f"{name}: the collapsed clause returned {new_ids}, "
        f"the per-row EXISTS form returned {old_ids}")


@pytest.mark.parametrize("name", list(_SHAPES))
def test_the_comparison_is_not_vacuous(tag_fixture_db, name):
    """Guard the guard: a shape whose two forms are both empty proves nothing.

    A few shapes legitimately match nothing (the tag no item carries), but a
    *tag* shape must exercise the fixture -- otherwise a builder that returned
    "no rows" for everything would pass the equivalence test above.
    """
    filters = _SHAPES[name]
    if name in ("tag matching nothing", "a matching tag and one that matches nothing"):
        return
    new_clause, new_params = build_filters_sql(filters)
    assert _ids(tag_fixture_db, new_clause, new_params), (
        f"{name} should match rows in the fixture; an empty result hides a mistake")


# ---------------------------------------------------------------------------
# shape: what collapsed, and what deliberately did not
# ---------------------------------------------------------------------------

def test_two_anded_tags_collapse_to_one_driving_subquery():
    clause, params = build_filters_sql([_tag("A"), _tag("B")])
    assert "HAVING COUNT(DISTINCT t.tag_name) = 2" in clause
    assert "EXISTS" not in clause
    assert params == ["A", "B"]


def test_three_anded_tags_count_three_distinct_names():
    clause, params = build_filters_sql([_tag("A"), _tag("B"), _tag("C")])
    assert "HAVING COUNT(DISTINCT t.tag_name) = 3" in clause
    assert params == ["A", "B", "C"]


def test_a_single_tag_stays_a_correlated_exists():
    """One tag has no conjunction to drive, and the check is cheap when the
    sort index already matches; the collapse is for the sparse AND."""
    clause, params = build_filters_sql([_tag("A")])
    assert clause == _OLD_TAG_CONTAINS
    assert params == ["A"]


def test_an_or_of_tags_stays_a_union_of_exists():
    """An OR is a union; it cannot become a count over one group."""
    clause, params = build_filters_sql([_tag("A"), _tag("B", "OR")])
    assert clause.count("EXISTS") == 2
    assert "HAVING COUNT" not in clause
    assert " OR " in clause
    assert params == ["A", "B"]


def test_a_does_not_contain_is_not_counted_into_the_conjunction():
    clause, params = build_filters_sql([_tag("A"), _tag("B"), _dnc("D")])
    assert "HAVING COUNT(DISTINCT t.tag_name) = 2" in clause
    assert "NOT EXISTS" in clause
    assert params == ["A", "B", "D"]


def test_duplicate_tag_names_count_once():
    """``A AND A`` is ``A``; counting the literal rows would demand 2."""
    clause, params = build_filters_sql([_tag("A"), _tag("A")])
    assert "HAVING COUNT(DISTINCT t.tag_name) = 1" in clause
    assert params == ["A"]


def test_interleaved_anded_tags_still_collapse():
    """AND is commutative: a size predicate between the tags must not block the
    collapse, or the common saved filter (tag, size, tag) would miss it."""
    clause, params = build_filters_sql([_tag("A"), _size(10), _tag("B")])
    assert "HAVING COUNT(DISTINCT t.tag_name) = 2" in clause
    assert "file_size > ?" in clause
    assert params == ["A", "B", 10]


# ---------------------------------------------------------------------------
# plan: the tag set drives the owner's shape instead of the score index
# ---------------------------------------------------------------------------

_OWNER_FILTERS = [
    {"field": "Tags", "op": "contains", "value": "Mature"},
    {"field": "Tags", "op": "contains", "value": "Video", "logic": "AND"},
    {"field": "File Size", "op": "gt", "value": "100000000", "logic": "AND"},
    {"field": "Subscribed", "op": "is_not", "value": "previously", "logic": "AND"},
]


@pytest.fixture(scope="module")
def tag_plan_db(tmp_path_factory):
    """A database big enough that the planner picks the score index for the
    *old* clause, with Mature on a tenth of rows and Video on a hundredth, so
    the conjunction is sparse along the score order the way the live data is."""
    path = str(tmp_path_factory.mktemp("tag-plan") / "plan.db")
    initialize_database(path)
    for i in range(400):
        tags = []
        if i % 10 == 0:
            tags.append("Mature")
        if i % 100 == 0:
            tags.append("Video")
        insert_or_update_item(path, {
            "workshop_id": 100_000 + i,
            "title": f"item {i}",
            "file_size": 200_000_000 if i % 100 == 0 else 50_000,
            "wilson_subscription_score": (i * 7919 % 100_000) / 100_000.0,
            "own_subscribed": 1,
            "own_first_subscribed_at": 1,
            "fetch_status": 200,
            "tags": tags,
        })
    return path


def _captured_search_sql(db_path, monkeypatch, **kwargs):
    """The exact SELECT `search_items` runs, parameters expanded by the trace."""
    captured = []
    real = database.get_connection

    def traced(path):
        conn = real(path)
        conn.set_trace_callback(captured.append)
        return conn

    monkeypatch.setattr(database, "get_connection", traced)
    search_items(db_path, **kwargs)
    return next(s for s in captured if s.lstrip().upper().startswith("SELECT"))


def _plan(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return " | ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
    finally:
        conn.close()


# The pre-change tag pair and the full search shape around it, pinned here so
# the contrast below is against today's clause and not against the builder.
_PLAN_EXISTS_PAIR = (
    "EXISTS (SELECT 1 FROM workshop_tags wt JOIN tags t USING(tag_id) "
    "WHERE wt.workshop_id = w.workshop_id AND t.tag_name = 'Mature') "
    "AND EXISTS (SELECT 1 FROM workshop_tags wt JOIN tags t USING(tag_id) "
    "WHERE wt.workshop_id = w.workshop_id AND t.tag_name = 'Video')"
)


def _owner_filter_sql(tag_pair):
    return (
        "SELECT w.workshop_id FROM workshop_items w WHERE 1=1 "
        "AND (w.fetch_status IS NULL OR w.fetch_status NOT IN (-1, -2)) "
        f"AND ({tag_pair} AND file_size > '100000000' "
        "AND NOT ((own_first_subscribed_at IS NOT NULL "
        "AND COALESCE(own_subscribed, 0) = 0))) "
        "ORDER BY w.wilson_subscription_score DESC LIMIT 50 OFFSET 0"
    )


def test_the_old_per_row_pair_walks_the_score_index(tag_plan_db):
    """The contrast that makes the next assertion mean something: the clause
    being replaced is answered by walking the sort index and checking rows."""
    plan = _plan(tag_plan_db, _owner_filter_sql(_PLAN_EXISTS_PAIR))
    assert "idx_wilson_subscription_score" in plan, plan
    assert "CORRELATED SCALAR SUBQUERY" in plan, plan


def test_search_uses_the_collapsed_clause_for_the_owner_filters(tag_plan_db, monkeypatch):
    sql = _captured_search_sql(
        tag_plan_db, monkeypatch, filters=_OWNER_FILTERS,
        sort_by="wilson_subscription_score", sort_order="DESC",
        summary_only=True, limit=50, offset=0)
    assert "HAVING COUNT(DISTINCT t.tag_name) = 2" in sql


def test_the_tag_set_drives_the_owner_filter_plan(tag_plan_db, monkeypatch):
    """The score index must not be the access path for a filter it cannot answer.

    Asserted against the SQL `search_items` really runs, with its settled-hiding
    clause and sort. Against the pre-change clause the plan is
    `SCAN w USING INDEX idx_wilson_subscription_score` plus two correlated
    subqueries, which is the defect; the collapsed clause is a `LIST SUBQUERY`
    that drives `w` by rowid and never touches the score index for the tags.
    """
    sql = _captured_search_sql(
        tag_plan_db, monkeypatch, filters=_OWNER_FILTERS,
        sort_by="wilson_subscription_score", sort_order="DESC",
        summary_only=True, limit=50, offset=0)

    plan = _plan(tag_plan_db, sql)
    assert "LIST SUBQUERY" in plan, plan
    assert "INTEGER PRIMARY KEY" in plan, plan
    assert "idx_wilson_subscription_score" not in plan, plan
