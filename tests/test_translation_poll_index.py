"""`idx_translation_queue_poll`: the sort the translation poll runs every pass.

`get_next_batch_for_translation` hands out the head of the whole outstanding
queue, ordered `priority DESC, queued_at ASC`, at most once a second while
translating. Without an index that is a full scan plus a temp B-tree over every
queue row on each pass.

`idx_translation_queue_poll` on `(priority DESC, queued_at ASC)` serves it. It is
created by `_ensure_indexes` and not by `_create_legacy_schema`, because a fresh
database calls the column `dt_queued` while `_create_legacy_schema` runs -- migration
13->14 renames it -- so `CREATE INDEX ... (queued_at)` there raises "no such
column". `test_the_poll_index_is_created_after_the_column_rename` pins that
ordering; the `EXPLAIN QUERY PLAN` test captures the poll's real SQL with a trace
callback and proves the planner reaches for the index instead of sorting.

The ordering contract gets its own test on a small fixture with a tie and a NULL:
dropping the old `queued_at IS NOT NULL` term is only safe if NULL-`queued_at`
rows still come first at equal priority, and a plan assertion alone would not
catch a change that quietly reordered rows.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest import mock

from src import database

INDEX = "idx_translation_queue_poll"


# ── helpers ───────────────────────────────────────────────────────────────────


def _index_sql(db_path) -> dict[str, str]:
    conn = database.get_connection(db_path)
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name='translation_queue'"
    ).fetchall()
    conn.close()
    return {row["name"]: row["sql"] for row in rows}


def _index_count(db_path) -> int:
    conn = database.get_connection(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
        (INDEX,),
    ).fetchone()[0]
    conn.close()
    return n


def _plan(db_path, sql: str, params=()) -> str:
    """The EXPLAIN QUERY PLAN detail lines for one statement, joined."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    finally:
        conn.close()
    return " | ".join(row[3] for row in rows)


@contextmanager
def _capture_sql():
    """Record every statement the code runs on the module's own connections."""
    seen: list[str] = []
    real_get_connection = database.get_connection

    def recording_get_connection(path):
        conn = real_get_connection(path)
        conn.set_trace_callback(seen.append)
        return conn

    with mock.patch.object(database, "get_connection", recording_get_connection):
        yield seen


def _seed(db_path, count: int = 1500):
    """A queue big enough that a full scan plus sort is a real alternative.

    The mix matters too: `priority` varies so the leading term has something to
    order, and a third of the rows keep `queued_at` NULL (the legacy backlog) so
    the plan test exercises the NULL-first contract as well.
    """
    conn = database.get_connection(db_path)
    conn.executemany(
        "INSERT INTO translation_queue "
        "(item_type, item_id, field, original_text, priority, queued_at) "
        "VALUES ('item', ?, 'title_en', 'テキスト', ?, ?)",
        [(i, i % 5, None if i % 3 == 0 else 1000 + i) for i in range(1, count + 1)],
    )
    conn.commit()
    conn.close()


# ── schema ────────────────────────────────────────────────────────────────────


def test_a_fresh_database_has_the_poll_index(db_path):
    indexes = _index_sql(db_path)
    assert INDEX in indexes, f"fresh database has these queue indexes: {sorted(indexes)}"
    assert "ON translation_queue (priority DESC, queued_at ASC)" in " ".join(
        indexes[INDEX].split()
    ), indexes[INDEX]


def test_the_poll_index_is_created_after_the_column_rename(tmp_path):
    """`_create_legacy_schema` alone cannot create it: the column is still `dt_queued`.

    This is the placement argument, not just the outcome. On a fresh file the
    unversioned schema creates `translation_queue.dt_queued`; only migration
    13->14 renames it to `queued_at`, and `_ensure_indexes` runs after that
    chain. An index naming `queued_at` in `_create_legacy_schema` would raise
    "no such column: queued_at" here.
    """
    path = str(tmp_path / "fresh_schema.db")
    conn = database.get_connection(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    database._create_legacy_schema(conn.cursor(), conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(translation_queue)")}
    conn.commit()
    conn.close()

    assert "dt_queued" in columns and "queued_at" not in columns, columns
    assert INDEX not in _index_sql(path), "_create_legacy_schema must not create it before the rename"

    database.initialize_database(path, legacy_chain=True)

    assert INDEX in _index_sql(path)
    conn = database.get_connection(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(translation_queue)")}
    conn.close()
    assert "queued_at" in columns and "dt_queued" not in columns, columns


def test_initialize_database_is_still_idempotent(db_path):
    """The index is `IF NOT EXISTS`, like every index `_ensure_indexes` manages."""
    database.initialize_database(db_path)
    database.initialize_database(db_path)
    assert _index_count(db_path) == 1, "IF NOT EXISTS must not accumulate copies"
    conn = database.get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == database.EXPECTED_VERSION


# ── the poll's plan ───────────────────────────────────────────────────────────


def test_the_poll_reads_the_poll_index(db_path):
    """The real SELECT, captured, must be planned through the index and not sorted.

    The SQL comes from a trace callback rather than a copy in this file: a
    rewrite that no longer matches the index fails here, and the assertion that
    the redundant `IS NOT NULL` term is gone is made against what the code
    really runs.
    """
    _seed(db_path)
    with _capture_sql() as seen:
        database.get_next_batch_for_translation(db_path, limit=20)
    sql = next(s for s in seen if "FROM translation_queue" in s)

    normalised = " ".join(sql.split())
    assert "ORDER BY priority DESC, queued_at ASC" in normalised, sql
    assert "IS NOT NULL" not in normalised, (
        f"the redundant term makes the index unusable: {sql}"
    )

    plan = _plan(db_path, sql)
    assert f"USING INDEX {INDEX}" in plan, f"expected {INDEX}, got: {plan}"
    assert "TEMP B-TREE" not in plan, f"the ORDER BY must come from the index: {plan}"


# ── the ordering contract ─────────────────────────────────────────────────────


def _seed_ordering(db_path) -> dict[str, int]:
    """Controlled rows: a tie, a NULL per priority, and a higher-priority head."""
    conn = database.get_connection(db_path)
    rows = [
        ("tie_a", 5, 100),
        ("null_p5", 5, None),
        ("older", 5, 50),
        ("tie_b", 5, 100),
        ("null_p4", 4, None),
        ("top", 6, 999),
    ]
    ids: dict[str, int] = {}
    for label, priority, queued_at in rows:
        cursor = conn.execute(
            "INSERT INTO translation_queue "
            "(item_type, item_id, field, original_text, priority, queued_at) "
            "VALUES ('item', 1, ?, 'テキスト', ?, ?)",
            (label, priority, queued_at),
        )
        ids[label] = cursor.lastrowid
    conn.commit()
    conn.close()
    return ids


def test_the_poll_orders_nulls_first_then_oldest_queued_at_equal_priority(db_path):
    """The safety net for dropping `queued_at IS NOT NULL`.

    At equal priority the unknown-time rows stay ahead of dated rows, then dated
    rows ascend. The tie (two rows with the same priority and `queued_at`) is
    asserted as an unordered pair, because SQLite does not promise an order
    between rows whose sort keys are equal.
    """
    ids = _seed_ordering(db_path)

    batch = database.get_next_batch_for_translation(db_path, limit=10)
    pairs = [(row["priority"], row["queued_at"]) for row in batch]
    assert pairs == [
        (6, 999),
        (5, None),
        (5, 50),
        (5, 100),
        (5, 100),
        (4, None),
    ], pairs

    # The order above is only meaningful if the NULL row really is the NULL row.
    assert batch[1]["id"] == ids["null_p5"]
    assert batch[2]["id"] == ids["older"]
    assert {batch[3]["id"], batch[4]["id"]} == {ids["tie_a"], ids["tie_b"]}
    assert batch[5]["id"] == ids["null_p4"]
    assert batch[0]["id"] == ids["top"]
