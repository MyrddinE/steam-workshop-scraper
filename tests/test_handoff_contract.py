"""The stage handoffs' contract: the producer's write vs the consumer's predicate.

Three defects (issues 17, 19 and 20) shared one shape. A stage finished with an
item, wrote its new state, reported success -- and the state it wrote was one the
next stage's query could not see. The loss showed up much later as work that
never happened. Two counters (`queued_nowhere`, `dead_queued`) now notice that a
divergence exists; they cannot say *which* handoff diverged, because each stage's
exit condition was written down only in the producer while the next stage's entry
condition lived in a different function.

These tests are the other half: for each handoff they take an item the producer
has just finished with and ask the consumer's own predicate about it. The
predicate is never restated here -- it is the same named fragment the worker poll
interpolates (`src/database.py`, the stage-handoff predicates), so a test that
would otherwise "prove" a rewritten copy of the SQL instead proves the real one.

The plan names three outcomes, and only the third is a bug:

* selected, because the work is genuinely outstanding;
* not selected, and the stage's output is stored;
* not selected, and nothing stored -- the item is in no queue with nothing to
  show for the stage. Issue 19 and issue 20 are this shape.

`_assert_handoff` states that rule once. The per-handoff tests below drive the
real producers where they can be driven without network (`seed_database` with a
synthetic page, `_process_item` with a synthetic API payload, `_settle_api_failure`
directly), and each has a sensitivity test that flips the predicate and shows the
contract check catch it. The contract check itself is shown failing on the
third-outcome shape explicitly.
"""

from __future__ import annotations

from unittest import mock

import pytest

from src import database
from src.daemon import Daemon
from src.database import (
    flag_field_for_translation,
    get_connection,
    insert_or_update_item,
)


# ── driving the real producers ────────────────────────────────────────────────


def _daemon(db_path, tmp_path) -> Daemon:
    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST"},
        "daemon": {"batch_size": 1, "target_appids": [1], "request_delay_seconds": 0},
    }
    return Daemon(config, config_path=str(tmp_path / "config.yaml"))


def _discover(db_path, tmp_path, workshop_id: int = 999) -> int:
    """Run real discovery over one synthetic page result (no network)."""
    with mock.patch("src.daemon.query_workshop_files") as query, \
            mock.patch("src.daemon.time.sleep"):
        query.return_value = {
            "total": 1,
            "items": [{"publishedfileid": str(workshop_id)}],
            "next_cursor": "",
        }
        _daemon(db_path, tmp_path).seed_database(target_new=100)
    return workshop_id


def _fetch(db_path, tmp_path, *, api_data: dict, existing: dict | None = None,
           enrich: bool = True, workshop_id: int = 1) -> int:
    """Run the real `_process_item` over a synthetic API payload (no network)."""
    row = {"workshop_id": workshop_id, "title": "Sample", "status": 200, "api_priority": 5}
    row.update(existing or {})
    insert_or_update_item(db_path, row)

    daemon = _daemon(db_path, tmp_path)
    payload = {"title": "Sample", "status": 200}
    payload.update(api_data)
    with mock.patch.object(daemon, "_should_enrich", return_value=enrich):
        daemon._process_item(row, api_data=payload)
    return workshop_id


def _settle_dead(db_path, tmp_path, workshop_id: int = 1) -> int:
    """Run the real permanent-failure producer, then report the item dead."""
    conn = get_connection(db_path)
    row = dict(conn.execute(
        "SELECT * FROM workshop_items WHERE workshop_id = ?", (workshop_id,)
    ).fetchone())
    conn.close()
    _daemon(db_path, tmp_path)._settle_api_failure(row, workshop_id, 404,
                                                   row.get("api_priority") or 0)
    return workshop_id


# ── asking a consumer's own question ──────────────────────────────────────────


def _row_value(db_path, workshop_id: int, column: str):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT {column} FROM workshop_items WHERE workshop_id = ?",
            (workshop_id,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[column]


def _selected(db_path, predicate: str, workshop_id: int) -> bool:
    """Whether a consumer's predicate selects one item.

    The predicate is interpolated, never restated: it is the worker poll's own
    fragment, scoped here to a primary key so one item can be asked about.
    """
    conn = get_connection(db_path)
    try:
        return conn.execute(
            f"SELECT 1 FROM workshop_items WHERE workshop_id = ? AND ({predicate})",
            (workshop_id,),
        ).fetchone() is not None
    finally:
        conn.close()


def _selected_for_translation(db_path, workshop_id: int, field: str) -> bool:
    """Whether the translation poll has the given field's work for one item."""
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT 1 FROM translation_queue "
            "WHERE item_type = 'item' AND item_id = ? AND field = ? "
            f"AND ({database.translation_queue_predicate()})",
            (workshop_id, field),
        ).fetchone() is not None
    finally:
        conn.close()


def _assert_handoff(selected: bool, stored: bool, what: str) -> None:
    """The plan's rule in one place. The third outcome is the bug."""
    assert selected or stored, (
        f"{what}: the consumer's predicate does not select the item and the "
        "stage's output is not stored -- in no queue with nothing to show for "
        "the stage (the third outcome the plan names as the bug)"
    )


# ── handoff 1: discovery -> API fetch ─────────────────────────────────────────


def _assert_fetch_handoff(db_path, workshop_id: int) -> None:
    """Discovery's only product is fetch-queue membership.

    Discovery has no other output an item could be resting on, so there is no
    "not selected but stored" branch here: a discovered live item that the fetch
    queue cannot select is issue 20.
    """
    assert _selected(db_path, database.api_fetch_queue_predicate(), workshop_id), (
        "discovery -> API fetch: the discovered item is not selected by the "
        "fetch queue, and discovery stores no output that could settle it "
        "(issue 20's shape)"
    )


def test_discovery_hands_a_new_item_to_the_fetch_queue(db_path, tmp_path):
    workshop_id = _discover(db_path, tmp_path)

    assert _row_value(db_path, workshop_id, "status") is None
    assert _row_value(db_path, workshop_id, "api_fetched_at") is None
    _assert_fetch_handoff(db_path, workshop_id)


def test_a_discovered_item_queued_nowhere_fails_the_fetch_contract(db_path, tmp_path):
    """Issue 20's shape: discovered, no fetch priority, nothing fetched."""
    with mock.patch.object(database, "api_fetch_queue_predicate",
                           return_value="api_priority > 100"):
        with pytest.raises(AssertionError, match="discovery -> API fetch"):
            _assert_fetch_handoff(db_path, _discover(db_path, tmp_path))


# ── handoff 2: API fetch -> web scrape ────────────────────────────────────────


def _assert_web_handoff(db_path, workshop_id: int) -> None:
    selected = _selected(db_path, database.web_scrape_queue_predicate(), workshop_id)
    stored = bool(_row_value(db_path, workshop_id, "extended_description"))
    _assert_handoff(selected, stored, "API fetch -> web scrape")


def test_the_fetch_hands_a_changed_item_to_the_web_scrape_queue(db_path, tmp_path):
    workshop_id = _fetch(
        db_path, tmp_path,
        api_data={"time_updated": 2000},
        existing={"steam_updated_at": 1000},
    )

    assert _row_value(db_path, workshop_id, "needs_web_scrape") > 0
    _assert_web_handoff(db_path, workshop_id)


def test_a_current_item_is_settled_by_the_web_stage(db_path, tmp_path):
    """Not selected, and the description the stage would produce is stored."""
    workshop_id = _fetch(
        db_path, tmp_path,
        api_data={"time_updated": 1000},
        existing={"steam_updated_at": 1000, "extended_description": "stored"},
    )

    assert _row_value(db_path, workshop_id, "needs_web_scrape") == 0
    _assert_web_handoff(db_path, workshop_id)


def test_the_web_contract_catches_issue_19s_shape(db_path):
    """Dequeued as scraped, description never stored, nothing queued.

    This is the third outcome, constructed directly: not selected by the scrape
    queue and nothing to show for the stage.
    """
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "fetched", "status": 200, "api_fetched_at": 1000,
        "api_priority": 0, "needs_web_scrape": 0,
    })

    with pytest.raises(AssertionError, match="API fetch -> web scrape"):
        _assert_web_handoff(db_path, 1)


def test_a_flipped_web_predicate_is_caught(db_path):
    """A predicate the producer's write no longer satisfies must be reported."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "queued", "status": 200, "api_fetched_at": 1000,
        "needs_web_scrape": 3,
    })

    with mock.patch.object(database, "web_scrape_queue_predicate",
                           return_value="needs_web_scrape > 100"):
        with pytest.raises(AssertionError, match="API fetch -> web scrape"):
            _assert_web_handoff(db_path, 1)


# ── handoff 3: API fetch -> image ─────────────────────────────────────────────


def _assert_image_handoff(db_path, workshop_id: int) -> None:
    selected = _selected(db_path, database.image_queue_predicate(), workshop_id)
    stored = _row_value(db_path, workshop_id, "image_extension") is not None
    _assert_handoff(selected, stored, "API fetch -> image")


def test_the_fetch_hands_a_preview_to_the_image_queue(db_path, tmp_path):
    workshop_id = _fetch(
        db_path, tmp_path,
        api_data={"time_updated": 2000, "preview_url": "https://example.invalid/p.png"},
        existing={"steam_updated_at": 1000},
    )

    assert _row_value(db_path, workshop_id, "needs_image") > 0
    _assert_image_handoff(db_path, workshop_id)


def test_a_served_image_extension_settles_the_image_handoff(db_path, tmp_path):
    """Not selected, and the answer the stage records is stored."""
    workshop_id = _fetch(
        db_path, tmp_path,
        api_data={"time_updated": 2000, "preview_url": "https://example.invalid/p.png"},
        existing={"steam_updated_at": 2000, "image_extension": "png"},
    )

    assert _row_value(db_path, workshop_id, "needs_image") == 0
    _assert_image_handoff(db_path, workshop_id)


def test_the_image_contract_catches_a_preview_queued_nowhere(db_path):
    """A preview the API reported, no image queued, and no answer recorded."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "with preview", "status": 200, "api_fetched_at": 1000,
        "api_priority": 0, "needs_image": 0, "image_extension": None,
        "preview_url": "https://example.invalid/p.png",
    })

    with pytest.raises(AssertionError, match="API fetch -> image"):
        _assert_image_handoff(db_path, 1)


def test_a_flipped_image_predicate_is_caught(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "queued", "status": 200, "api_fetched_at": 1000,
        "needs_image": 3,
    })

    with mock.patch.object(database, "image_queue_predicate",
                           return_value="needs_image > 100"):
        with pytest.raises(AssertionError, match="API fetch -> image"):
            _assert_image_handoff(db_path, 1)


# ── handoff 4: API fetch and web scrape -> translation ────────────────────────


def _assert_translation_handoff(db_path, workshop_id: int, field: str) -> None:
    selected = _selected_for_translation(db_path, workshop_id, field)
    stored = _row_value(db_path, workshop_id, field) is not None
    _assert_handoff(selected, stored, f"API fetch/web scrape -> translation ({field})")


def test_the_fetch_hands_a_non_ascii_title_to_the_translation_queue(db_path, tmp_path):
    workshop_id = _fetch(
        db_path, tmp_path,
        api_data={"time_updated": 2000, "title": "Caf\u00e9 \u4e16\u754c"},
        existing={"steam_updated_at": 1000},
    )

    assert _row_value(db_path, workshop_id, "translation_priority") > 0
    _assert_translation_handoff(db_path, workshop_id, "title_en")


def test_a_stored_translation_settles_the_handoff(db_path):
    """Not selected, and the translated text the stage would produce is stored."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "Caf\u00e9", "title_en": "Cafe",
        "status": 200, "api_fetched_at": 1000, "translation_priority": 0,
    })

    _assert_translation_handoff(db_path, 1, "title_en")


def test_the_translation_contract_catches_a_mirror_with_no_queue_row(db_path):
    """The mirror is up, the queue is empty and nothing is stored.

    This is the drift `flag_field_for_translation` documents: the mirror and the
    queue row must move together, so a raised mirror with no row behind it is a
    field queued nowhere.
    """
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "Caf\u00e9",
        "status": 200, "api_fetched_at": 1000, "translation_priority": 3,
    })

    with pytest.raises(AssertionError, match="translation \\(title_en\\)"):
        _assert_translation_handoff(db_path, 1, "title_en")


def test_a_flipped_translation_predicate_is_caught(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "Caf\u00e9", "status": 200, "api_fetched_at": 1000,
    })
    flag_field_for_translation(db_path, "item", 1, "title_en", "Caf\u00e9", 3)

    with mock.patch.object(database, "translation_queue_predicate",
                           return_value="priority > 100"):
        with pytest.raises(AssertionError, match="translation \\(title_en\\)"):
            _assert_translation_handoff(db_path, 1, "title_en")


# ── handoff 5: any stage -> dead ──────────────────────────────────────────────


def _assert_dead_handoff(db_path, workshop_id: int) -> None:
    assert _row_value(db_path, workshop_id, "status") == -1, "the producer marked it dead"
    assert not _selected(db_path, database.queued_anywhere_predicate(), workshop_id), (
        "any stage -> dead: the item is dead and the union of the queue "
        "predicates still selects it (issue 17's shape)"
    )


def test_a_missing_item_is_settled_dead_and_in_no_queue(db_path, tmp_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "status": 200, "api_priority": 5,
        "needs_web_scrape": 5, "needs_image": 5, "translation_priority": 5,
    })

    _settle_dead(db_path, tmp_path)

    _assert_dead_handoff(db_path, 1)


def test_the_dead_contract_catches_issue_17s_shape(db_path):
    """Dead, yet still holding a queue flag -- the third outcome for this handoff."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "status": -1, "api_priority": 0,
        "needs_image": 5,
    })

    with pytest.raises(AssertionError, match="any stage -> dead"):
        _assert_dead_handoff(db_path, 1)


def test_a_flipped_dead_predicate_is_caught(db_path):
    """A union that no longer asks each queue lets a dead item slip through."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "gone", "status": -1, "api_priority": 0,
    })

    with mock.patch.object(database, "queued_anywhere_predicate",
                           return_value="status = -1"):
        with pytest.raises(AssertionError, match="any stage -> dead"):
            _assert_dead_handoff(db_path, 1)


# ── the extraction is real: the polls use the named predicates ────────────────


def _captured_sql(db_path, call):
    """The SQL the given work actually runs, captured from the real functions."""
    seen: list[str] = []
    real_get_connection = database.get_connection

    def recording_get_connection(path):
        conn = real_get_connection(path)
        conn.set_trace_callback(seen.append)
        return conn

    with mock.patch.object(database, "get_connection", recording_get_connection):
        call()
    return seen


def test_each_worker_poll_builds_its_query_from_the_named_predicate(db_path):
    """Patching the predicate must reach the poll's SQL.

    If a selector had inlined its own copy of the condition, the patch would not
    change the statement and this fails -- which is exactly the drift the
    extraction removes, and what a test restating the SQL could not notice.
    """
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "queued", "status": 200, "api_fetched_at": 1000,
        "api_priority": 1, "needs_web_scrape": 1, "needs_image": 1,
        "translation_priority": 1,
    })
    flag_field_for_translation(db_path, "item", 1, "title_en", "Caf\u00e9", 3)

    # (what, predicate name, a valid-but-distinctive replacement, table, call)
    probes = [
        ("api fetch", "api_fetch_queue_predicate", "workshop_id = -12345",
         "FROM workshop_items",
         lambda: database.get_next_items_to_scrape(db_path, limit=1)),
        ("fetchable count", "api_fetch_queue_predicate", "workshop_id = -12345",
         "FROM workshop_items",
         lambda: database.count_fetchable_items(db_path)),
        ("web scrape", "web_scrape_queue_predicate", "workshop_id = -12345",
         "FROM workshop_items",
         lambda: database.get_next_web_scrape_item(db_path)),
        ("image", "image_queue_predicate", "workshop_id = -12345",
         "FROM workshop_items",
         lambda: database.get_next_image_item(db_path)),
        ("translation", "translation_queue_predicate", "item_id = -12345",
         "FROM translation_queue",
         lambda: database.get_next_batch_for_translation(db_path, limit=1)),
    ]

    for what, name, replacement, table, call in probes:
        with mock.patch.object(database, name, return_value=replacement):
            sql = [s for s in _captured_sql(db_path, call) if table in s][-1]
        assert replacement in sql, f"{what} poll does not use {name}: {sql}"
