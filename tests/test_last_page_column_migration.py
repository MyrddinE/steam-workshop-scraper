"""Migration 28->29: `app_tracking.last_page_scanned` is dropped.

The column counted pages while discovery walked them by number. `88397b7`
replaced page-numbered discovery with cursor-based discovery, which resumes from
`last_cursor`, and the writer went with it -- but three readers stayed: the TUI's
"Last Page" column, the web table's equivalent, and the `app_tracking` metric
both front ends render. All three displayed the column's DEFAULT 0 forever: a
figure that looks like a measurement and never moves. Recorded as issue 47.

These tests pin what the migration has to guarantee: a fresh database never grows
the column, an upgraded one loses it without disturbing the surviving row, a
second run skips cleanly, and the metric that fed both front ends no longer
selects it.
"""

from src import metrics
from src.database import EXPECTED_VERSION, get_connection, initialize_database


def _app_tracking_shape(db_path):
    conn = get_connection(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(app_tracking)")}
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    return columns, version


def _regress_to_v28(db_path):
    """Put the pre-29 shape back: the column, a value, and the version marker."""
    conn = get_connection(db_path)
    conn.execute(
        "ALTER TABLE app_tracking ADD COLUMN last_page_scanned INTEGER DEFAULT 0"
    )
    conn.execute(
        "INSERT INTO app_tracking (appid, last_page_scanned, last_cursor) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(appid) DO UPDATE SET "
        "last_page_scanned = excluded.last_page_scanned, "
        "last_cursor = excluded.last_cursor",
        (431960, 13, "AoJckZidMXaL38lT"),
    )
    conn.execute("PRAGMA user_version = 28")
    conn.commit()
    conn.close()


def test_migration_drops_the_dead_page_counter(db_path):
    _regress_to_v28(db_path)

    initialize_database(db_path)

    columns, version = _app_tracking_shape(db_path)
    assert version == EXPECTED_VERSION
    assert "last_page_scanned" not in columns

    # The row itself survives the drop; only the dead column goes.
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT appid, last_cursor FROM app_tracking WHERE appid = 431960"
    ).fetchone()
    conn.close()
    assert dict(row) == {"appid": 431960, "last_cursor": "AoJckZidMXaL38lT"}


def test_a_fresh_database_never_has_the_column(db_path):
    columns, version = _app_tracking_shape(db_path)
    assert version == EXPECTED_VERSION
    assert "last_page_scanned" not in columns


def test_the_migration_is_idempotent(db_path):
    _regress_to_v28(db_path)
    initialize_database(db_path)
    columns, _ = _app_tracking_shape(db_path)
    assert "last_page_scanned" not in columns

    # Rewind the marker alone. The column is already gone, so the second run has
    # to skip cleanly rather than fail reaching for a column that is not there.
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 28")
    conn.commit()
    conn.close()
    initialize_database(db_path)

    columns, version = _app_tracking_shape(db_path)
    assert version == EXPECTED_VERSION
    assert "last_page_scanned" not in columns


def test_the_app_tracking_metric_no_longer_returns_the_counter(db_path):
    """The reader that fed both front ends is gone with the column."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO app_tracking (appid, last_cursor) VALUES (?, ?)",
        (431960, "AoJckZidMXaL38lT"),
    )
    conn.commit()

    assert metrics._app_tracking(conn, {}) == [
        {"appid": 431960, "last_cursor": "AoJckZidMXaL38lT"}
    ]
    conn.close()
