"""One composite index on `translation_queue`, created before the migration loop.

Both queries that matter predicate on this table and it had no index beyond its
primary key. The per-field lookup in `queue_field_for_translation`

    SELECT id, priority FROM translation_queue
    WHERE item_type=? AND item_id=? AND field=?

runs once per queued field, and migration 22->23's stranded-mirror repair
correlates a `NOT EXISTS` over `(item_type, item_id)` for every raised mirror.
`idx_translation_queue_lookup` on `(item_type, item_id, field)` serves both,
because SQLite can seek an index on a leftmost-columns prefix.

The repair runs *inside* the `MIGRATIONS` loop, so the index has to be created by
`_create_schema` (which runs before the loop for every caller and every version)
rather than by `_ensure_indexes` (which runs after it). A migration step cannot
help either: it would run after 22->23. `test_the_index_exists_when_the_repair_runs`
pins that ordering directly; the `EXPLAIN QUERY PLAN` tests below prove the
planner actually reaches for the index rather than scanning.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from unittest import mock

from src import database

INDEX = "idx_translation_queue_lookup"


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


def _assert_queue_is_seeked(db_path, sql, params=(), why=""):
    """The queue must be reached through the index, not scanned.

    The assertion names the index rather than the phrase `USING INDEX`: SQLite
    prints `USING INDEX` for the three-column lookup (which also fetches
    `priority`, so the index is not covering) but `USING COVERING INDEX` for the
    repair's correlated `SELECT 1`, and both are the property we want. The
    negative half names the relation, because the repair's plan legitimately
    contains `SCAN workshop_items`; only a scan of the queue is a failure.
    """
    plan = _plan(db_path, sql, params)
    assert INDEX in plan, f"{why}: expected {INDEX}, got: {plan}"
    for scanned in ("SCAN translation_queue", "SCAN q"):
        assert scanned not in plan, f"{why}: the queue must not be scanned, got: {plan}"
    return plan


def _seed(db_path, item_count: int = 1500):
    """Items whose mirrors are a mix of genuinely queued and stranded.

    A queue row exists for every third item, so the repair both keeps and clears
    rows; `item_count` is large enough that a full scan is a real plan the
    planner could otherwise choose.
    """
    conn = database.get_connection(db_path)
    conn.executemany(
        "INSERT OR REPLACE INTO workshop_items "
        "(workshop_id, title, status, api_priority, translation_priority) "
        "VALUES (?, ?, 200, 0, ?)",
        [(i, f"item {i}", (i % 3) + 1) for i in range(1, item_count + 1)],
    )
    conn.executemany(
        "INSERT INTO translation_queue "
        "(item_type, item_id, field, original_text, priority, queued_at) "
        "VALUES ('item', ?, 'title_en', 'テキスト', 3, 1)",
        [(i,) for i in range(1, item_count + 1) if i % 3 == 0],
    )
    conn.commit()
    conn.close()


def _age_to_v22_without_the_index(db_path):
    """Build the pre-fix shape: a v22 marker and no lookup index.

    Rewinding the marker alone is the pattern the neighbouring migration tests
    use, but it is not enough here: a database initialised by the current code
    already carries the index, and this test is about an upgrade creating it.
    """
    conn = database.get_connection(db_path)
    conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
    conn.execute("PRAGMA user_version = 22")
    conn.commit()
    conn.close()


def _row_counts(db_path) -> dict[str, int]:
    tables = (
        "workshop_items",
        "translation_queue",
        "users",
        "tags",
        "workshop_tags",
        "app_tracking",
    )
    conn = database.get_connection(db_path)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    conn.close()
    return counts


# ── schema ────────────────────────────────────────────────────────────────────


def test_a_fresh_database_has_the_lookup_index(db_path):
    indexes = _index_sql(db_path)
    assert INDEX in indexes, f"fresh database has these queue indexes: {sorted(indexes)}"
    assert "ON translation_queue (item_type, item_id, field)" in " ".join(
        indexes[INDEX].split()
    ), indexes[INDEX]


def test_initialize_database_is_still_idempotent(db_path):
    """The index is `IF NOT EXISTS`, like every index `_ensure_indexes` manages."""
    database.initialize_database(db_path)
    database.initialize_database(db_path)
    assert _index_count(db_path) == 1, "IF NOT EXISTS must not accumulate copies"
    conn = database.get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == database.EXPECTED_VERSION


# ── the ordering argument: the index exists when 22->23 runs ─────────────────


def test_the_index_exists_when_the_repair_runs(db_path, monkeypatch):
    """`_create_schema` must create the index before the migration loop reaches 22->23.

    An index created by `_ensure_indexes` (which runs after the loop) would leave
    this assertion False while every schema test still passed, so this is the
    test that distinguishes the two placements.
    """
    _seed(db_path, item_count=30)
    _age_to_v22_without_the_index(db_path)
    assert INDEX not in _index_sql(db_path)

    seen: dict[str, bool] = {}
    real_repair = database._migration_22_to_23

    def recording_repair(cursor, conn, path):
        seen["index"] = bool(
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (INDEX,)
            ).fetchone()
        )
        return real_repair(cursor, conn, path)

    monkeypatch.setattr(
        database,
        "MIGRATIONS",
        [(v, recording_repair if v == 23 else fn) for v, fn in database.MIGRATIONS],
    )
    database.initialize_database(db_path)

    assert seen == {"index": True}, "the repair still scanned: the index was not there yet"
    assert INDEX in _index_sql(db_path)


def test_upgrading_a_v22_database_adds_the_index_and_preserves_rows(db_path):
    _seed(db_path)
    _age_to_v22_without_the_index(db_path)
    before = _row_counts(db_path)

    database.initialize_database(db_path)

    assert INDEX in _index_sql(db_path)
    assert _row_counts(db_path) == before, "schema work must not move or drop rows"
    conn = database.get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # Every third item was given a queue row; the other raised mirrors are the
    # stranded ones 22->23 exists to clear, and it runs during this upgrade.
    kept = conn.execute(
        "SELECT COUNT(*) FROM workshop_items WHERE translation_priority > 0"
    ).fetchone()[0]
    conn.close()
    assert version == database.EXPECTED_VERSION
    assert kept == 500, "only the mirrors with a queue row behind them survive"


# ── the two predicates ────────────────────────────────────────────────────────


def _repair_sql(db_path) -> str:
    """The UPDATE `_migration_22_to_23` executes, captured from a trace callback.

    Capturing the real statement means a rewrite that no longer matches the index
    fails here, instead of an unchanged copy of the old text passing.
    """
    conn = database.get_connection(db_path)
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    database._migration_22_to_23(conn.cursor(), conn, db_path)
    conn.close()
    return next(s for s in seen if "NOT EXISTS" in s)


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


def _lookup_sql(db_path) -> str:
    """The SELECT `queue_field_for_translation` executes, captured from a trace."""
    with _capture_sql() as seen:
        database.queue_field_for_translation(
            db_path, "item", 1, "title_en", "テキスト", 3
        )
    return next(s for s in seen if "FROM translation_queue" in s)


def test_the_repairs_two_column_predicate_reads_the_index(db_path):
    """The repair constrains `(item_type, item_id)` only.

    This is the leftmost-prefix claim: `(item_type, item_id, field)` has to serve
    a two-column predicate as well as the three-column lookup, or the index does
    nothing for the step that motivated it.
    """
    _seed(db_path)
    sql = _repair_sql(db_path)
    assert "q.item_type = 'item' AND q.item_id = workshop_items.workshop_id" in " ".join(
        sql.split()
    ), sql
    _assert_queue_is_seeked(db_path, sql, why="22->23 repair")


def test_the_per_field_lookup_reads_the_index(db_path):
    _seed(db_path)
    # The trace callback expands the bound parameters, so the captured statement
    # is self-contained and needs no bindings of its own for EXPLAIN.
    sql = _lookup_sql(db_path)
    _assert_queue_is_seeked(db_path, sql, why="per-field lookup")
