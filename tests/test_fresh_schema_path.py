"""The current-schema path for a brand-new database.

`initialize_database` picks its path by the database's recorded version, never
by whether the file exists. A fresh file (`user_version = 0`) is now built at
`EXPECTED_VERSION` directly by `_create_current_schema`; the historical schema
plus the whole migration chain is kept behind `legacy_chain=True`, primarily so
the chain stays exercised, and is still the only path an existing database
takes.

The linchpin is :func:`test_schema_equivalence`: the two paths must end at the
same schema. It compares the set of tables, each table's `PRAGMA table_info`,
every index by name **and** stored `sqlite_master.sql`, every trigger the same
way, and `user_version`. That is the owner's forward rule made mechanical --
adding a migration to the chain without mirroring it in
`_create_current_schema` moves one endpoint and not the other, and this test
fails.

The shared `db_path` fixture in `tests/conftest.py` deliberately stays on the
legacy path (it passes `legacy_chain=True`), so the rest of the suite keeps
replaying the chain. The current path gets its own fixtures here.
"""

from __future__ import annotations

import sqlite3

import pytest

from src import database
from src.database import (
    EXPECTED_VERSION,
    MIGRATIONS,
    get_connection,
    get_item_details,
    initialize_database,
    insert_or_update_creator,
    insert_or_update_item,
    search_items,
)
from tests.conftest import restore_pre_rename_table_names


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def fresh_db_path(tmp_path):
    """A database built by the current-schema path, named explicitly.

    `legacy_chain=False` is the default, but naming it here keeps these tests
    from silently following a future change to the default; the default itself
    is pinned by :func:`test_default_fresh_path_does_not_replay_the_chain`.
    """
    path = str(tmp_path / "fresh.db")
    initialize_database(path, legacy_chain=False)
    return path


@pytest.fixture
def legacy_db_path(tmp_path):
    """A database built by the historical schema plus the whole chain."""
    path = str(tmp_path / "legacy.db")
    initialize_database(path, legacy_chain=True)
    return path


# ── helpers ──────────────────────────────────────────────────────────────────


def _schema_snapshot(db_path: str) -> dict:
    """The comparable schema shape: tables, columns, indexes, triggers, version."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        # `sqlite_%` tables are SQLite's own (sqlite_sequence); the two paths
        # both create it, but it carries no application schema.
        tables = [row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        table_info = {
            name: cur.execute("PRAGMA table_info(%s)" % name).fetchall()
            for name in tables
        }
        indexes = cur.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            "ORDER BY name"
        ).fetchall()
        triggers = cur.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "ORDER BY name"
        ).fetchall()
        version = cur.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    return {
        "version": version,
        "tables": tables,
        "table_info": table_info,
        "indexes": indexes,
        "triggers": triggers,
    }


def _count_items(db_path: str) -> int:
    conn = get_connection(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM workshop_items").fetchone()[0]
    finally:
        conn.close()


def _record_migrations(monkeypatch) -> list[str]:
    """Record each migration the driver actually runs, by function name."""
    calls: list[str] = []

    def recording(fn):
        def wrapper(cursor, conn, db_path):
            calls.append(fn.__name__)
            return fn(cursor, conn, db_path)
        return wrapper

    monkeypatch.setattr(
        database,
        "MIGRATIONS",
        [(version, recording(fn)) for version, fn in MIGRATIONS],
    )
    return calls


def _seed_item(db_path: str, workshop_id: int = 123, title: str = "Fresh path item"):
    insert_or_update_creator(db_path, {"steamid": 7, "personaname": "Author"})
    insert_or_update_item(db_path, {
        "workshop_id": workshop_id,
        "title": title,
        "short_description": "short",
        "creator_steamid": 7,
        "consumer_appid": 4000,
        "fetch_status": 200,
        "steam_created_at": 1,
        "steam_updated_at": 2,
        "tags": ["mod", "map"],
    })


# ── the equivalence linchpin ─────────────────────────────────────────────────


def test_schema_equivalence(fresh_db_path, legacy_db_path):
    """Both paths must end at an identical schema and version.

    A future migration added to the chain but not mirrored in
    `_create_current_schema` changes only the legacy endpoint, so this fails.
    """
    fresh = _schema_snapshot(fresh_db_path)
    legacy = _schema_snapshot(legacy_db_path)

    assert fresh["version"] == legacy["version"] == EXPECTED_VERSION

    assert fresh["tables"] == legacy["tables"], "the table sets differ"
    for name in fresh["tables"]:
        assert fresh["table_info"][name] == legacy["table_info"][name], (
            f"PRAGMA table_info({name}) differs between the two paths"
        )
    assert fresh["indexes"] == legacy["indexes"], (
        "the indexes differ by name or sqlite_master.sql"
    )
    assert fresh["triggers"] == legacy["triggers"], (
        "the triggers differ by name or sqlite_master.sql"
    )


# ── the fresh path ───────────────────────────────────────────────────────────


def test_fresh_path_reaches_the_expected_version(fresh_db_path):
    assert _schema_snapshot(fresh_db_path)["version"] == EXPECTED_VERSION


def test_fresh_path_builds_the_current_names(fresh_db_path):
    """The terminal names and columns, not the historical ones the chain starts from."""
    snapshot = _schema_snapshot(fresh_db_path)

    assert "creators" in snapshot["tables"]
    assert "app_discovery" in snapshot["tables"]
    assert "users" not in snapshot["tables"]
    assert "app_tracking" not in snapshot["tables"]

    columns = {row[1] for row in snapshot["table_info"]["workshop_items"]}
    assert {"first_seen_at", "api_fetched_at",
            "steam_created_at", "steam_updated_at"} <= columns
    assert {"dt_found", "dt_updated", "dt_attempted",
            "time_created", "time_updated", "tags"}.isdisjoint(columns), (
        "the fresh path kept a historical column"
    )
    # Migration 34->35 dropped the write-only scrape_version, so the current
    # shape does not carry it.
    assert "scrape_version" not in columns


def test_fresh_path_accessors_work(fresh_db_path):
    """A small insert/read through the public API, not raw SQL."""
    _seed_item(fresh_db_path, title="ZebraFreshPathMarker")

    item = get_item_details(fresh_db_path, 123)
    assert item is not None
    assert item["title"] == "ZebraFreshPathMarker"
    assert item["personaname"] == "Author"
    assert {tag.strip() for tag in item["tags"].split(",")} == {"mod", "map"}

    # The FTS index has to be maintained by the triggers the fresh path built.
    hits = search_items(fresh_db_path, query="ZebraFreshPathMarker")
    assert [row["workshop_id"] for row in hits] == [123]


def test_fresh_path_is_idempotent(fresh_db_path):
    """A second `initialize_database` on a fresh-path database changes nothing."""
    _seed_item(fresh_db_path)
    before_schema = _schema_snapshot(fresh_db_path)
    before_rows = _count_items(fresh_db_path)

    initialize_database(fresh_db_path)

    assert _schema_snapshot(fresh_db_path) == before_schema
    assert _count_items(fresh_db_path) == before_rows


def test_default_fresh_path_does_not_replay_the_chain(tmp_path, monkeypatch):
    """The default for a fresh file is the current schema, with no migrations."""
    calls = _record_migrations(monkeypatch)
    path = str(tmp_path / "default.db")

    initialize_database(path)

    assert calls == [], f"the default fresh path replayed {calls}"
    assert _schema_snapshot(path)["version"] == EXPECTED_VERSION


# ── the legacy path is still the chain ───────────────────────────────────────


def test_legacy_chain_flag_replays_the_chain(tmp_path, monkeypatch):
    """`legacy_chain=True` is how the suite keeps the chain exercised."""
    calls = _record_migrations(monkeypatch)
    path = str(tmp_path / "legacy.db")

    initialize_database(path, legacy_chain=True)

    assert calls == [fn.__name__ for _, fn in MIGRATIONS]
    assert calls[0] == "_migration_0_to_1"


def test_legacy_chain_still_reaches_the_expected_version(legacy_db_path):
    snapshot = _schema_snapshot(legacy_db_path)
    assert snapshot["version"] == EXPECTED_VERSION
    assert "creators" in snapshot["tables"]
    assert "app_discovery" in snapshot["tables"]


def test_an_existing_database_takes_the_chain_whatever_the_flag_says(tmp_path):
    """`legacy_chain` selects a path for a fresh file only.

    An existing database has a version that only the chain can carry forward,
    so it replays its pending migrations even with `legacy_chain=False`. Rewind
    a current database to v29 by undoing every Batch 6 rename and restoring the
    columns 34->35 dropped (the shared helper does all of it), then confirm the
    default call still runs 29->30.
    """
    path = str(tmp_path / "existing.db")
    initialize_database(path, legacy_chain=True)

    conn = get_connection(path)
    restore_pre_rename_table_names(conn)
    conn.execute("PRAGMA user_version = 29")
    conn.commit()
    conn.close()

    initialize_database(path, legacy_chain=False)

    snapshot = _schema_snapshot(path)
    assert snapshot["version"] == EXPECTED_VERSION
    assert "creators" in snapshot["tables"]
    assert "app_discovery" in snapshot["tables"]
    assert "users" not in snapshot["tables"]
    assert "app_tracking" not in snapshot["tables"]
