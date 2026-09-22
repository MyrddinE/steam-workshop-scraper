"""Every column in ``VALID_SORT_COLS`` must have a query index.

The owner's report ("I don't think subscriber score is indexed") turned out to
be false for both score columns -- they were added together in the same commit
-- but the question is the right one to ask permanently, because a sortable
column that ships without an index is a full temp B-tree sort on every page.
``own_first_subscribed_at`` was exactly that until this table existed.

The expected set is :data:`src.database.QUERY_INDEXES`, the same data
``_ensure_indexes`` iterates and the startup diagnostic checks the live file
against. Two paths build the schema (``_create_current_schema`` and the legacy
chain), so both are checked.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.database import (
    VALID_SORT_COLS,
    _build_sort_clause,
    initialize_database,
    live_fetch_status_predicate,
)


def _leading_index_columns(db_path: str, table: str = "workshop_items") -> dict[str, list[str]]:
    """Map each indexed column that *leads* an index to the index names."""
    conn = sqlite3.connect(db_path)
    try:
        leaders: dict[str, list[str]] = {}
        for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
            name = row[1]
            columns = conn.execute(f"PRAGMA index_info({name})").fetchall()
            if columns:
                leaders.setdefault(columns[0][2], []).append(name)
        return leaders
    finally:
        conn.close()


def _rowid_alias_columns(db_path: str, table: str = "workshop_items") -> set[str]:
    """Columns that are the table's rowid alias (single INTEGER PRIMARY KEY).

    ``workshop_id`` is one: its B-tree *is* the table, so ordering by it needs
    no separate index and ``PRAGMA index_list`` rightly lists none.
    """
    conn = sqlite3.connect(db_path)
    try:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        pk = [row for row in info if row[5]]
        if len(pk) == 1 and pk[0][2].upper() == "INTEGER":
            return {pk[0][1]}
        return set()
    finally:
        conn.close()


@pytest.fixture
def fresh_db_path(tmp_path):
    path = str(tmp_path / "fresh.db")
    initialize_database(path, legacy_chain=False)
    return path


@pytest.mark.parametrize("path_fixture", ["fresh_db_path", "db_path"])
def test_every_sortable_column_is_indexed(request, path_fixture):
    """The invariant: a leading-column index exists for every sortable column."""
    db_path = request.getfixturevalue(path_fixture)
    leaders = _leading_index_columns(db_path)
    rowid_alias = _rowid_alias_columns(db_path)

    missing = [
        column for column in sorted(VALID_SORT_COLS)
        if column not in leaders and column not in rowid_alias
    ]
    assert not missing, (
        f"{path_fixture}: sortable columns with no leading index: {missing}; "
        f"add them to database.QUERY_INDEXES"
    )


@pytest.mark.parametrize("path_fixture", ["fresh_db_path", "db_path"])
def test_every_expected_query_index_exists(request, path_fixture):
    """Each ``QUERY_INDEXES`` row is actually created, with its leading column."""
    from src.database import QUERY_INDEXES

    db_path = request.getfixturevalue(path_fixture)

    for name, table, columns in QUERY_INDEXES:
        leaders = _leading_index_columns(db_path, table)
        leading = columns[0].split()[0]
        assert name in leaders.get(leading, []), (
            f"{path_fixture}: {name} is not present leading on {table}.{leading}"
        )


def test_sort_index_data_covers_every_sortable_column():
    """The data table itself names a leading index for each sortable column."""
    from src.database import QUERY_INDEXES

    leading_columns = {
        columns[0].split()[0]
        for _name, table, columns in QUERY_INDEXES
        if table == "workshop_items"
    }
    missing = sorted(VALID_SORT_COLS - leading_columns - {"workshop_id"})
    assert not missing, (
        f"VALID_SORT_COLS entries absent from QUERY_INDEXES: {missing}"
    )


def test_own_first_subscribed_at_sort_uses_the_index(fresh_db_path):
    """The concrete regression: that sort was a full temp B-tree before."""
    conn = sqlite3.connect(fresh_db_path)
    try:
        conn.executemany(
            "INSERT INTO workshop_items (workshop_id, fetch_status, own_first_subscribed_at) "
            "VALUES (?, 200, ?)",
            [(i, i if i % 3 else None) for i in range(1, 501)],
        )
        conn.commit()
        # The exact ORDER BY `search_items` builds, with the live-status clause.
        sql = (
            "SELECT w.workshop_id FROM workshop_items w WHERE 1=1 "
            f"AND {live_fetch_status_predicate('w.fetch_status')}"
            f"{_build_sort_clause('own_first_subscribed_at', 'DESC')}"
        )
        plan = " | ".join(
            row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
        )
    finally:
        conn.close()

    assert "TEMP B-TREE" not in plan.upper(), plan
    assert "idx_own_first_subscribed_at" in plan, plan
