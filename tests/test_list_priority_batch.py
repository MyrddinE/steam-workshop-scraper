"""The batched list-priority writes must be the per-row calls, but cheaper.

``POST /api/search`` used to open, commit and close a connection for every
priority it raised on every row it returned -- roughly 150 cycles per page on
the request the user waits for. ``raise_list_priorities`` does the same work in
one transaction.

Two claims are pinned here: the **resulting state is identical** to the per-row
sequence (every priority column and the translation queue), and the route's cost
is a **bounded number of commits**, counted rather than timed.
"""

from __future__ import annotations

import sqlite3

import pytest

from src import database
from src.database import initialize_database
from src.webserver import app, init_webserver

# (wid, web_scrape_priority, image_priority, translation_priority, preview_url,
#  image_answer, title, title_en, short_description, short_description_en,
#  extended_description, extended_description_en)
# Chosen so every branch of the per-row sequence runs: a raise from 0, from
# between 0 and 5, an already-high value, a NULL priority, a resolved image
# answer (permanent and present), a transient one, no preview URL, an
# untranslated non-ASCII field and a translated one.
_ITEMS = [
    (1, 0, 0, 0, "p1", None, "日本語タイトル", None, None, None, None, None),
    (2, 3, 3, 0, "p2", "404", "ascii title", None, None, None, None, None),
    (3, 5, 7, 0, "p3", "500", "ascii", None, "説明テキスト", "translated", None, None),
    (4, None, None, None, "p4", None, "Ünicode", None, None, None, None, None),
    (5, 1, 1, 0, None, None, None, None, None, None, "説明", None),
    (6, 7, 5, 2, "p6", "jpg", "ascii", None, None, None, None, None),
]

_IDS = [item[0] for item in _ITEMS]


def _seed(path: str) -> None:
    initialize_database(path)
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO workshop_items ("
            "workshop_id, fetch_status, web_scrape_priority, image_priority, "
            "translation_priority, preview_url, image_answer, "
            "title, title_en, short_description, short_description_en, "
            "extended_description, extended_description_en) "
            "VALUES (?, 200, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _ITEMS,
        )
        # Two pre-existing queue rows: one below 5 (must be bumped), one above
        # (must be left alone). Both fields are ones the raiser re-queues.
        conn.execute(
            "INSERT INTO translation_queue "
            "(entity_type, entity_id, field, original_text, priority, queued_at) "
            "VALUES ('item', 1, 'title_en', 'old', 3, 1)"
        )
        conn.execute(
            "INSERT INTO translation_queue "
            "(entity_type, entity_id, field, original_text, priority, queued_at) "
            "VALUES ('item', 4, 'title_en', 'old', 7, 1)"
        )
        conn.commit()
    finally:
        conn.close()


def _seed_many(path: str, count: int = 60) -> None:
    initialize_database(path)
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO workshop_items (workshop_id, fetch_status, preview_url, "
            "web_scrape_priority, image_priority, title) VALUES (?, 200, ?, ?, ?, ?)",
            [
                (i, f"p{i}", [0, 3, 5][i % 3], [0, 2, 8][i % 3], f"Item {i}")
                for i in range(1, count + 1)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _snapshot(path: str):
    """Every priority column plus the queue, in a comparison-friendly shape."""
    conn = database.get_connection(path)
    try:
        items = [tuple(row) for row in conn.execute(
            "SELECT workshop_id, web_scrape_priority, image_priority, "
            "translation_priority FROM workshop_items ORDER BY workshop_id"
        ).fetchall()]
        queue = [tuple(row) for row in conn.execute(
            # `id` is the autoincrement key, not state: insertion order across
            # the two paths is not promised, so it is excluded.
            "SELECT entity_type, entity_id, field, original_text, priority "
            "FROM translation_queue ORDER BY entity_id, field"
        ).fetchall()]
        return items, queue
    finally:
        conn.close()


def _per_row(path: str):
    """Today's per-row sequence, verbatim, including the route's own call order."""
    import src.webserver as webserver

    webserver._db_path = path
    flagged = 0
    for workshop_id in _IDS:
        database.raise_web_scrape_priority_for_list(path, workshop_id)
        database.raise_image_priority_for_list(path, workshop_id)
        if webserver._ensure_image_flagged(workshop_id, 5):
            flagged += 1
        database.raise_translation_priority_for_list(path, workshop_id)
    return flagged


def test_batched_priorities_match_the_per_row_calls(tmp_path):
    per_row_path = str(tmp_path / "per-row.db")
    batched_path = str(tmp_path / "batched.db")
    _seed(per_row_path)
    _seed(batched_path)

    per_row_flagged = _per_row(per_row_path)
    batched_flagged = database.raise_list_priorities(batched_path, _IDS)

    assert per_row_flagged == batched_flagged == 3
    assert _snapshot(batched_path) == _snapshot(per_row_path)


def test_batched_translation_queue_matches_the_per_row_calls(tmp_path):
    """The queue half on its own, so a queue-only regression is legible."""
    per_row_path = str(tmp_path / "per-row-queue.db")
    batched_path = str(tmp_path / "batched-queue.db")
    _seed(per_row_path)
    _seed(batched_path)

    _per_row(per_row_path)
    database.raise_list_priorities(batched_path, _IDS)

    assert _snapshot(batched_path)[1] == _snapshot(per_row_path)[1]


def test_the_route_commits_a_bounded_number_of_times(tmp_path, monkeypatch):
    """Count transactions, not wall-clock: the page must not pay per row."""
    db_path = str(tmp_path / "route.db")
    _seed_many(db_path, count=60)
    counter = {"commits": 0}
    real = database.get_connection

    class _CountingConnection:
        def __init__(self, conn):
            self._conn = conn

        def commit(self):
            counter["commits"] += 1
            return self._conn.commit()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(database, "get_connection", lambda path: _CountingConnection(real(path)))

    init_webserver(db_path, {"database": {"path": db_path}, "daemon": {}})
    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post("/api/search", json={"limit": 50})

    assert response.status_code == 200
    assert len(response.get_json()) == 50
    # One commit for the whole page. (The pre-batch route made roughly three per
    # returned row plus the queue writes.)
    assert counter["commits"] <= 2, counter


def test_the_route_still_returns_the_raised_priorities(tmp_path):
    """The read-back the grid draws from is unchanged."""
    db_path = str(tmp_path / "readback.db")
    _seed_many(db_path, count=5)
    init_webserver(db_path, {"database": {"path": db_path}, "daemon": {}})
    app.config["TESTING"] = True

    with app.test_client() as client:
        rows = client.post("/api/search", json={"limit": 50}).get_json()

    by_id = {row["workshop_id"]: row for row in rows}
    assert by_id[1]["web_scrape_priority"] == 5   # was 0, raised to the list floor
    assert by_id[1]["image_priority"] == 5
