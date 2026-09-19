"""Migration 24->25: the three queue indexes, and the queries that must use them.

The web, image and API workers each find the head of their queue with a full
scan plus a sort on every poll, and the web/image statistics breakdowns pay for
the same missing indexes. Migration 24->25 adds one partial composite index per
queue, shaped exactly like the query: `WHERE <queue_column> > 0 ORDER BY
<queue_column> DESC, api_fetched_at ASC`.

A migration test that only asserts the index exists would pass while the poll
still scanned, so the important half of this file runs `EXPLAIN QUERY PLAN` over
the queries the code actually executes. The SQL is captured from the real
functions with a SQLite trace callback rather than restated here: a query
rewritten so it no longer matches the index then fails the test, instead of
leaving a stale copy of the old text passing.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest import mock

from src import database, metrics
from tests.conftest import restore_pre_rename_table_names

WEB_INDEX = "idx_web_scrape_queue"
IMAGE_INDEX = "idx_image_queue"
API_INDEX = "idx_api_queue"
QUEUE_INDEXES = {WEB_INDEX, IMAGE_INDEX, API_INDEX}


# ── schema helpers ────────────────────────────────────────────────────────────


def _index_sql(db_path) -> dict[str, str]:
    conn = database.get_connection(db_path)
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name='workshop_items'"
    ).fetchall()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return {row["name"]: row["sql"] for row in rows} | {"__version__": version}


def _regress_to_v24(db_path):
    """Undo the migration: drop the three indexes and the version marker."""
    conn = database.get_connection(db_path)
    for name in QUEUE_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    restore_pre_rename_table_names(conn)
    conn.execute("PRAGMA user_version = 24")
    conn.commit()
    conn.close()


def _seed(db_path, count: int = 400):
    """A table big enough that a full scan plus sort is a real alternative."""
    conn = database.get_connection(db_path)
    conn.executemany(
        "INSERT INTO workshop_items "
        "(workshop_id, title, fetch_status, api_priority, web_scrape_priority, "
        " image_priority, translation_priority, api_fetched_at) "
        "VALUES (?, ?, 200, ?, ?, ?, ?, ?)",
        [
            (i, f"item {i}", i % 4, i % 5, i % 3, i % 2, 1000 + i)
            for i in range(1, count + 1)
        ],
    )
    conn.commit()
    conn.close()


# ── query capture and EXPLAIN helpers ─────────────────────────────────────────


@contextmanager
def _capture_sql():
    """Run work against the module's connections while recording every statement."""
    seen: list[str] = []
    real_get_connection = database.get_connection

    def recording_get_connection(path):
        conn = real_get_connection(path)
        conn.set_trace_callback(seen.append)
        return conn

    with mock.patch.object(database, "get_connection", recording_get_connection):
        yield seen


def _plan(db_path, sql: str, params=()) -> str:
    """The EXPLAIN QUERY PLAN detail lines for one statement, joined."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    finally:
        conn.close()
    return " | ".join(row[3] for row in rows)


def _assert_served_by(db_path, sql, index, params=(), why=""):
    plan = _plan(db_path, sql, params)
    assert f"USING INDEX {index}" in plan, (
        f"{why}: expected {index}, got: {plan}"
    )
    assert "SCAN workshop_items" not in plan, (
        f"{why}: the table must not be scanned, got: {plan}"
    )
    return plan


# ── the migration ─────────────────────────────────────────────────────────────


def test_fresh_database_carries_the_three_queue_indexes(db_path):
    info = _index_sql(db_path)
    assert info["__version__"] == database.EXPECTED_VERSION
    assert database.EXPECTED_VERSION >= 25, "the queue indexes arrived at v25"
    assert QUEUE_INDEXES <= set(info)


def test_indexes_have_the_measured_partial_composite_shape(db_path):
    """The predicate and column order are what the queries need, not incidental."""
    info = _index_sql(db_path)
    assert "web_scrape_priority DESC, api_fetched_at ASC" in info[WEB_INDEX]
    assert "WHERE web_scrape_priority > 0" in info[WEB_INDEX]
    assert "image_priority DESC, api_fetched_at ASC" in info[IMAGE_INDEX]
    assert "WHERE image_priority > 0" in info[IMAGE_INDEX]
    assert "api_priority DESC, api_fetched_at ASC" in info[API_INDEX]
    assert "WHERE api_priority > 0" in info[API_INDEX]


def test_upgrading_a_v24_database_adds_the_indexes(db_path):
    _regress_to_v24(db_path)
    assert _index_sql(db_path)["__version__"] == 24
    assert not (QUEUE_INDEXES & set(_index_sql(db_path)))

    database.initialize_database(db_path)

    info = _index_sql(db_path)
    assert info["__version__"] == database.EXPECTED_VERSION
    assert QUEUE_INDEXES <= set(info)


def test_migration_is_idempotent(db_path):
    _regress_to_v24(db_path)
    database.initialize_database(db_path)
    database.initialize_database(db_path)
    info = _index_sql(db_path)
    assert info["__version__"] == database.EXPECTED_VERSION
    assert QUEUE_INDEXES <= set(info)


# ── the worker polls ──────────────────────────────────────────────────────────


def test_web_scrape_poll_reads_the_web_queue_index(db_path):
    _seed(db_path)
    with _capture_sql() as seen:
        database.get_next_web_scrape_item(db_path)
    sql = [s for s in seen if "FROM workshop_items" in s][-1]

    # The index column order has to match this ORDER BY, or the poll sorts.
    assert "ORDER BY web_scrape_priority DESC, api_fetched_at ASC" in " ".join(sql.split())
    plan = _assert_served_by(db_path, sql, WEB_INDEX, why="web scrape poll")
    assert "TEMP B-TREE" not in plan, "the ORDER BY must come from the index"


def test_image_poll_reads_the_image_queue_index(db_path):
    _seed(db_path)
    with _capture_sql() as seen:
        database.get_next_image_item(db_path)
    sql = [s for s in seen if "FROM workshop_items" in s][-1]

    assert "ORDER BY image_priority DESC, api_fetched_at ASC" in " ".join(sql.split())
    plan = _assert_served_by(db_path, sql, IMAGE_INDEX, why="image poll")
    assert "TEMP B-TREE" not in plan, "the ORDER BY must come from the index"


def test_api_fetch_poll_reads_the_api_queue_index(db_path):
    _seed(db_path)
    with _capture_sql() as seen:
        database.get_next_items_to_fetch(db_path, limit=5)
    sql = [s for s in seen if "FROM workshop_items" in s][-1]

    assert "ORDER BY api_priority DESC, api_fetched_at ASC" in " ".join(sql.split())
    # The trace callback hands back the statement with its bound LIMIT expanded,
    # so the captured text is already self-contained for EXPLAIN.
    plan = _assert_served_by(db_path, sql, API_INDEX, why="API fetch poll")
    assert "TEMP B-TREE" not in plan, "the ORDER BY must come from the index"


def test_fetchable_count_reads_the_api_queue_index(db_path):
    """`count_fetchable_items` is the daemon's "is there work?" gate.

    `src/daemon.py` documents that this count used to scan the whole table
    because nothing indexed `api_priority`; the API queue index now serves it.
    """
    _seed(db_path)
    with _capture_sql() as seen:
        database.count_fetchable_items(db_path)
    sql = [s for s in seen if "FROM workshop_items" in s][-1]
    _assert_served_by(db_path, sql, API_INDEX, why="fetchable count")


# ── the statistics breakdowns ─────────────────────────────────────────────────


def _breakdown_queries(db_path) -> dict[str, str]:
    conn = database.get_connection(db_path)
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    metrics._priority_breakdowns(conn, {})
    conn.close()
    queries = [s for s in seen if "GROUP BY" in s]
    assert len(queries) == 3, queries
    return {
        column: next(s for s in queries if f"{column} AS prio" in s)
        for column in ("translation_priority", "image_priority", "web_scrape_priority")
    }


def test_web_queue_breakdown_reads_the_web_queue_index(db_path):
    _seed(db_path)
    sql = _breakdown_queries(db_path)["web_scrape_priority"]
    plan = _assert_served_by(db_path, sql, WEB_INDEX, why="web queue breakdown")
    assert "TEMP B-TREE" not in plan, "the GROUP BY must come from the index"


def test_image_queue_breakdown_reads_the_image_queue_index(db_path):
    _seed(db_path)
    sql = _breakdown_queries(db_path)["image_priority"]
    plan = _assert_served_by(db_path, sql, IMAGE_INDEX, why="image queue breakdown")
    assert "TEMP B-TREE" not in plan, "the GROUP BY must come from the index"


def test_translation_breakdown_keeps_its_existing_index(db_path):
    """No new index is claimed for translation; its existing one must still serve it."""
    _seed(db_path)
    sql = _breakdown_queries(db_path)["translation_priority"]
    _assert_served_by(
        db_path, sql, "idx_translation_priority", why="translation breakdown"
    )


def test_api_breakdown_shape_has_no_shipped_caller_but_is_served(db_path):
    """The plan's API-queue breakdown, as measured.

    The shipped `priority_breakdowns` metric covers translation, image and web
    only, so this query exists in no code path; it is the shape the plan priced
    at 219 ms -> 0.4 ms and it is asserted here so the API row is not silently
    dropped. The API queue *is* read in shipped code by the fetch poll and by
    `count_fetchable_items`, both covered above.
    """
    _seed(db_path)
    sql = (
        "SELECT api_priority AS prio, COUNT(*) AS cnt FROM workshop_items "
        "WHERE api_priority > 0 AND (fetch_status IS NULL OR fetch_status <> -1) "
        "GROUP BY api_priority ORDER BY prio DESC"
    )
    _assert_served_by(db_path, sql, API_INDEX, why="API queue breakdown shape")
