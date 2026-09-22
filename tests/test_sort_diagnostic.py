"""The sort diagnostic: read-only, gated by the UI-trace switch, answers the sort question.

The owner's "subscriber score is not indexed" report cannot be settled from this
repository, so ``GET /api/search_diagnostic`` reports on the live file instead:
expected indexes, the real query's plan per sort column, score coverage,
``sqlite_stat1``, and first/deep page timings. It rides
``daemon.capture_web_ui_trace`` rather than a switch of its own.

Four properties carry the weight: it is **inert when the switch is off**, it is
**read-only**, it reports each expected index's **actual definition** rather than
its name alone -- a same-named index on the wrong column is a live failure mode,
because ``_ensure_indexes`` uses ``CREATE INDEX IF NOT EXISTS`` and never repairs
it -- and the summary is the one the startup log already writes.
"""

from __future__ import annotations

import logging
import os
import sqlite3

import pytest

from src import capture, sort_diagnostic
from src.database import VALID_SORT_COLS, initialize_database, insert_or_update_item
from src.webserver import app, init_webserver

ITEMS = 300


@pytest.fixture(autouse=True)
def _reset_capture():
    yield
    capture.configure(None)


def _seed(db_path: str, count: int = ITEMS):
    initialize_database(db_path)
    for i in range(1, count + 1):
        insert_or_update_item(db_path, {
            "workshop_id": i,
            "title": f"Item {i}",
            "fetch_status": 200,
            "wilson_favorite_score": (i % 97) / 97.0,
            "wilson_subscription_score": (i % 89) / 89.0,
        })


def _client(db_path, config):
    init_webserver(db_path, {"database": {"path": db_path}, "daemon": config})
    app.config["TESTING"] = True
    return app.test_client()


def test_route_is_inert_when_the_switch_is_off(tmp_path):
    """With no switch the route answers 404 and never touches the database."""
    db_path = str(tmp_path / "real.db")
    _seed(db_path)
    client = _client(db_path, {})
    # Point the server at a path that does not exist: if the route opened it the
    # read would fail, and a 404 here proves it never got that far.
    import src.webserver as webserver
    missing = str(tmp_path / "not-created.db")
    webserver._db_path = missing

    response = client.get("/api/search_diagnostic")

    assert response.status_code == 404
    assert not os.path.exists(missing), "the refused route created a database file"


def test_route_reports_the_live_file_when_the_switch_is_on(tmp_path):
    db_path = str(tmp_path / "diagnostic.db")
    _seed(db_path)
    outbox = tmp_path / "outbox"
    client = _client(db_path, {"outbox_dir": str(outbox), "capture_web_ui_trace": True})

    response = client.get("/api/search_diagnostic")

    assert response.status_code == 200
    report = response.get_json()
    assert report["rows"] == ITEMS
    assert report["indexes"]["missing"] == []
    assert "idx_wilson_favorite_score" in report["indexes"]["expected"]
    assert "idx_wilson_subscription_score" in report["indexes"]["expected"]

    assert report["score_coverage"]["wilson_favorite_score"]["non_null"] == ITEMS
    assert report["score_coverage"]["wilson_favorite_score"]["fraction"] == 1.0

    assert report["sqlite_stat1"]["present"] is False

    favorite_plan = report["plans"]["wilson_favorite_score"]
    assert favorite_plan["uses_index"] is True
    assert "idx_wilson_favorite_score" in favorite_plan["plan"]
    subscription_plan = report["plans"]["wilson_subscription_score"]
    assert subscription_plan["uses_index"] is True
    assert "idx_wilson_subscription_score" in subscription_plan["plan"]

    assert set(report["timings"]) == VALID_SORT_COLS
    for timing in report["timings"].values():
        assert "first_page_seconds" in timing
        assert "deep_page_seconds" in timing


def test_the_report_is_read_only(tmp_path):
    """Every table's contents are identical after the diagnostic runs."""
    db_path = str(tmp_path / "readonly.db")
    _seed(db_path, count=40)
    before = _dump(db_path)

    sort_diagnostic.run(db_path)

    assert _dump(db_path) == before


def test_a_missing_index_is_named_and_logged(tmp_path, caplog):
    db_path = str(tmp_path / "missing-index.db")
    _seed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP INDEX idx_own_first_subscribed_at")
    conn.commit()
    conn.close()

    report = sort_diagnostic.run(db_path)
    assert "idx_own_first_subscribed_at" in report["indexes"]["missing"]

    with caplog.at_level(logging.WARNING):
        sort_diagnostic.log_startup_report(db_path)
    assert any(
        "idx_own_first_subscribed_at" in record.getMessage()
        for record in caplog.records
    )


def _recreate_index(db_path: str, name: str, definition: str):
    """Drop an expected index and recreate it with a deliberately wrong shape."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DROP INDEX {name}")
        conn.execute(f"CREATE INDEX {name} ON {definition}")
        conn.commit()
    finally:
        conn.close()


def test_a_correct_schema_reports_no_index_mismatches(tmp_path):
    """Nothing to report on a schema ``_ensure_indexes`` built itself."""
    db_path = str(tmp_path / "correct-indexes.db")
    _seed(db_path, count=10)

    report = sort_diagnostic.run(db_path)

    assert report["indexes"]["missing"] == []
    assert report["indexes"]["wrong_definition"] == []
    assert {d["status"] for d in report["indexes"]["details"].values()} == {"present"}


def test_an_index_with_the_right_name_on_the_wrong_column_is_wrong_definition(tmp_path):
    """The name-only check called this ``present``; the definition check must not.

    ``_ensure_indexes`` uses ``CREATE INDEX IF NOT EXISTS``, so a live file with a
    same-named index on another column is never repaired and a name-only report
    cannot see it.
    """
    db_path = str(tmp_path / "wrong-definition.db")
    _seed(db_path, count=10)
    _recreate_index(
        db_path,
        "idx_wilson_favorite_score",
        "workshop_items (title)",
    )

    report = sort_diagnostic.run(db_path)

    entry = report["indexes"]["details"]["idx_wilson_favorite_score"]
    assert entry["status"] == "wrong_definition"
    assert entry["expected_columns"] == ["wilson_favorite_score"]
    assert entry["columns"] == ["title"]
    assert "idx_wilson_favorite_score" in report["indexes"]["wrong_definition"]
    assert "idx_wilson_favorite_score" not in report["indexes"]["missing"]


def test_a_partial_index_on_the_right_column_is_flagged(tmp_path):
    """Right name, right column, but a ``WHERE`` clause means it covers no promise."""
    db_path = str(tmp_path / "partial-index.db")
    _seed(db_path, count=10)
    _recreate_index(
        db_path,
        "idx_wilson_subscription_score",
        "workshop_items (wilson_subscription_score) WHERE fetch_status = 200",
    )

    report = sort_diagnostic.run(db_path)

    entry = report["indexes"]["details"]["idx_wilson_subscription_score"]
    assert entry["status"] == "wrong_definition"
    assert entry["partial"] is True
    assert entry["columns"] == ["wilson_subscription_score"]
    assert entry["expected_columns"] == ["wilson_subscription_score"]
    assert "idx_wilson_subscription_score" in report["indexes"]["wrong_definition"]


def test_the_plan_boolean_reports_whether_the_index_is_used(tmp_path):
    """The derived boolean the owner would otherwise read out of the plan text."""
    db_path = str(tmp_path / "plan-index.db")
    _seed(db_path)

    report = sort_diagnostic.run(db_path)
    plan = report["plans"]["wilson_favorite_score"]
    assert plan["expected_index"] == "idx_wilson_favorite_score"
    assert plan["uses_index"] is True
    assert "idx_wilson_favorite_score" in plan["plan"]

    conn = sqlite3.connect(db_path)
    conn.execute("DROP INDEX idx_wilson_favorite_score")
    conn.commit()
    conn.close()

    report = sort_diagnostic.run(db_path)
    plan = report["plans"]["wilson_favorite_score"]
    assert plan["uses_index"] is False
    assert "TEMP B-TREE" in plan["plan"].upper()


def test_a_temp_btree_sort_is_logged_loudly(tmp_path, caplog):
    db_path = str(tmp_path / "temp-btree.db")
    _seed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP INDEX idx_own_first_subscribed_at")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING):
        sort_diagnostic.log_startup_report(db_path)

    assert any(
        "own_first_subscribed_at" in record.getMessage()
        and "does not use an index" in record.getMessage()
        for record in caplog.records
    )


def test_a_missing_database_is_an_error_not_an_exception(tmp_path):
    report = sort_diagnostic.run(str(tmp_path / "absent.db"))
    assert "error" in report


def _dump(db_path: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()
