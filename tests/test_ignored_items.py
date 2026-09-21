"""Stage 1 of the owner's ignored-item feature: the status, the shared
mechanics, and the default hiding.

Ignored is ``fetch_status = -2``, set by ``ignore_item`` rather than discovered.
It settles the item exactly as death does: all four queue priorities zero and the
item's ``translation_queue`` rows deleted in the same transaction. Dead and
ignored are the *settled* pair, and every reader asks one question through the
predicate pair in ``src/database.py`` instead of spelling the values out.

The new names are reached through the ``database`` module rather than imported at
module scope on purpose: this file has to *collect* against the pre-change
source so each test can be shown failing on its own, and a top-level import of a
name that did not exist would turn every test into one collection error.

The one site with no behaviour to pin is ``_promote_stale_items``: its
``fetch_status = 200`` term already excluded ``-2`` before this change, so that
test passes either way and is kept as documentation rather than as proof.
"""

from unittest.mock import patch

import pytest
from textual.widgets import ListView, Static

from src import database
from src import metrics
from src.database import (
    api_fetch_queue_predicate,
    compute_wilson_cutoffs,
    count_stranded_never_fetched_items,
    get_all_creator_ids,
    get_connection,
    get_next_items_to_fetch,
    insert_or_update_item,
    raise_api_priority_for_detail,
    raise_api_priority_for_list,
    search_items,
)
from tests.conftest import ASYNC_PAUSE, seed_pacing_delay
from tests.test_dead_requeue_writers import _image_bump_once, _web_bump_once


# ── helpers ──────────────────────────────────────────────────────────────────


def _live(db_path, workshop_id, **over):
    record = {"workshop_id": workshop_id, "title": f"item {workshop_id}",
              "fetch_status": 200}
    record.update(over)
    return insert_or_update_item(db_path, record)


def _raw(db_path, workshop_id, **over):
    record = {"workshop_id": workshop_id, "title": f"item {workshop_id}"}
    record.update(over)
    return insert_or_update_item(db_path, record)


def _row(db_path, workshop_id, columns="*"):
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            f"SELECT {columns} FROM workshop_items WHERE workshop_id = ?",
            (workshop_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _queue_row(db_path, entity_id, entity_type="item", field="title_en"):
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO translation_queue "
        "(entity_type, entity_id, field, original_text, priority, queued_at) "
        "VALUES (?, ?, ?, 'テスト', 3, 1)",
        (entity_type, entity_id, field),
    )
    conn.commit()
    conn.close()


def _queue_rows(db_path):
    conn = get_connection(db_path)
    try:
        return [
            (row["entity_type"], row["entity_id"], row["field"])
            for row in conn.execute(
                "SELECT entity_type, entity_id, field FROM translation_queue "
                "ORDER BY entity_type, entity_id, field")
        ]
    finally:
        conn.close()


def _ids(rows):
    return {row["workshop_id"] for row in rows}


def _metric(db_path, *names, **params):
    return metrics.values(metrics.compute(db_path, list(names), params or None))


FLAG_COLUMNS = (
    "api_priority", "web_scrape_priority", "image_priority", "translation_priority",
)


# ── one source for the statuses and the predicates ───────────────────────────


def test_the_settled_statuses_are_dead_and_ignored():
    assert database.DEAD_FETCH_STATUS == -1
    assert database.IGNORED_FETCH_STATUS == -2
    assert database.SETTLED_FETCH_STATUSES == (-1, -2)


def test_the_predicate_pair_partitions_every_status(db_path):
    """A live predicate and its negation cover every row exactly once.

    The NULL status is the trap: ``NULL NOT IN (-1, -2)`` is NULL, not true, so a
    predicate that forgot the ``IS NULL`` term would drop every never-fetched
    row. This pins the partition over the whole vocabulary in use -- NULL, 200,
    404 (legacy), 500, dead and ignored.
    """
    for wid, status in ((1, None), (2, 200), (3, 404), (4, 500), (5, -1), (6, -2)):
        _raw(db_path, wid, fetch_status=status)

    live_pred = database.live_fetch_status_predicate()
    settled_pred = database.settled_fetch_status_predicate()

    conn = get_connection(db_path)
    try:
        live = {r[0] for r in conn.execute(
            f"SELECT workshop_id FROM workshop_items WHERE {live_pred}")}
        settled = {r[0] for r in conn.execute(
            f"SELECT workshop_id FROM workshop_items WHERE {settled_pred}")}
    finally:
        conn.close()

    assert live == {1, 2, 3, 4}, "NULL, fetched, legacy 404 and retry are all live"
    assert settled == {5, 6}, "dead and ignored are the settled pair"
    assert live | settled == {1, 2, 3, 4, 5, 6}
    assert live & settled == set()


# ── ignore_item settles, exactly as death does ───────────────────────────────


def test_ignore_item_settles_status_flags_and_translation_rows(db_path):
    _live(db_path, 1, api_priority=5, web_scrape_priority=5,
          image_priority=5, translation_priority=5)
    _queue_row(db_path, 1)
    # A creator row shares the numeric id space; ignoring the item must not
    # touch it, exactly as `_settle_api_failure`'s clear does not.
    _queue_row(db_path, 1, entity_type="user")

    assert database.ignore_item(db_path, 1) is True

    row = _row(db_path, 1)
    assert row["fetch_status"] == -2
    for column in FLAG_COLUMNS:
        assert row[column] == 0, f"a settled item holds no queue flag ({column})"
    assert _queue_rows(db_path) == [("user", 1, "title_en")], \
        "the item's queue rows go; a creator row with the same id stays"


def test_ignore_item_is_idempotent(db_path):
    _live(db_path, 1, api_priority=5)
    assert database.ignore_item(db_path, 1) is True
    assert database.ignore_item(db_path, 1) is False, \
        "nothing changed, so the second press reports nothing changed"
    row = _row(db_path, 1)
    assert row["fetch_status"] == -2
    assert all(row[column] == 0 for column in FLAG_COLUMNS)


def test_ignore_item_reports_false_for_a_missing_row(db_path):
    assert database.ignore_item(db_path, 999) is False


# ── unignore_item reconstructs the status from the row ───────────────────────


def test_unignore_requeues_a_never_fetched_row(db_path):
    _raw(db_path, 1, fetch_status=-2, api_fetched_at=None,
         extended_description=None, api_priority=0, web_scrape_priority=0)

    assert database.unignore_item(db_path, 1) is True

    row = _row(db_path, 1)
    assert row["fetch_status"] is None, "never fetched comes back as discovered"
    assert row["api_priority"] == 1, "it is queued so it cannot land in queued_nowhere"
    assert row["web_scrape_priority"] == 3, \
        "a row with no description gets the scrape a fresh merge would queue"


def test_unignore_restores_a_fetched_row_without_requeueing_it(db_path):
    _raw(db_path, 1, fetch_status=-2, api_fetched_at=123,
         extended_description="present", api_priority=0, web_scrape_priority=0)

    assert database.unignore_item(db_path, 1) is True

    row = _row(db_path, 1)
    assert row["fetch_status"] == 200
    assert row["api_priority"] == 0, "a fetched item is not queued for a fetch"
    assert row["web_scrape_priority"] == 0, "the description is already stored"


def test_unignore_queues_a_scrape_for_a_fetched_row_with_no_description(db_path):
    _raw(db_path, 1, fetch_status=-2, api_fetched_at=123,
         extended_description=None, api_priority=0, web_scrape_priority=0)

    database.unignore_item(db_path, 1)

    row = _row(db_path, 1)
    assert row["fetch_status"] == 200
    assert row["web_scrape_priority"] == 3, \
        "ignoring cleared the scrape flag; the row's own data says it is still due"


def test_unignore_does_not_raise_a_scrape_flag_over_an_existing_one(db_path):
    _raw(db_path, 1, fetch_status=-2, api_fetched_at=None,
         extended_description=None, api_priority=0, web_scrape_priority=7)

    database.unignore_item(db_path, 1)

    assert _row(db_path, 1)["web_scrape_priority"] == 7, "MAX never downgrades"


def test_unignore_is_idempotent_and_ignores_live_rows(db_path):
    _raw(db_path, 1, fetch_status=-2, api_fetched_at=None, extended_description=None)
    _live(db_path, 2)

    assert database.unignore_item(db_path, 1) is True
    assert database.unignore_item(db_path, 1) is False, "already restored"
    assert database.unignore_item(db_path, 2) is False, "a live row is not ignored"
    assert _row(db_path, 2)["fetch_status"] == 200, "restoring does not touch a live row"


def test_ignore_then_unignore_round_trips_a_fetched_item(db_path):
    _live(db_path, 1, api_fetched_at=123, extended_description="present",
          api_priority=0)

    assert database.ignore_item(db_path, 1) is True
    assert database.unignore_item(db_path, 1) is True

    row = _row(db_path, 1)
    assert row["fetch_status"] == 200
    assert all(row[column] == 0 for column in FLAG_COLUMNS)


# ── the fetch queue, the priority writers and the discovery guard ────────────


def test_the_api_fetch_poll_does_not_hand_out_an_ignored_row(db_path):
    _raw(db_path, 1, fetch_status=-2, api_priority=5)
    _live(db_path, 2, api_priority=5)

    assert _ids(get_next_items_to_fetch(db_path)) == {2}
    assert "-2" in api_fetch_queue_predicate()


def test_both_api_priority_raises_refuse_an_ignored_row(db_path):
    _raw(db_path, 1, fetch_status=-2, api_priority=0)
    _live(db_path, 2, api_priority=0)

    raise_api_priority_for_list(db_path, 1)
    raise_api_priority_for_detail(db_path, 1)
    raise_api_priority_for_list(db_path, 2)
    raise_api_priority_for_detail(db_path, 2)

    assert _row(db_path, 1)["api_priority"] == 0, "an ignored item is in no queue"
    assert _row(db_path, 2)["api_priority"] == 10, "a live item is still raised"


def test_the_discovery_guard_preserves_an_ignored_rows_priority(db_path):
    """Discovery re-seeing an ignored item must not write a fetch priority.

    The fetch poll excludes ignored rows, so the priority could never be handed
    out -- it would only strand the row in ``dead_queued``. A live row still
    takes the new priority.
    """
    _raw(db_path, 4242, fetch_status=-2, api_priority=0)
    _live(db_path, 4243, api_priority=0)

    insert_or_update_item(db_path, {"workshop_id": 4242, "api_priority": 3},
                          preserve_dead_api_priority=True)
    insert_or_update_item(db_path, {"workshop_id": 4243, "api_priority": 3},
                          preserve_dead_api_priority=True)

    assert _row(db_path, 4242)["api_priority"] == 0
    assert _row(db_path, 4243)["api_priority"] == 3


def test_count_stranded_never_fetched_items_excludes_ignored(db_path):
    _raw(db_path, 1, fetch_status=None, api_fetched_at=None, api_priority=0)
    _raw(db_path, 2, fetch_status=-2, api_fetched_at=None, api_priority=0)
    _raw(db_path, 3, fetch_status=-1, api_fetched_at=None, api_priority=0)

    assert count_stranded_never_fetched_items(db_path) == 1, \
        "only the live never-fetched row with no queue is stranded"


# ── the worker guards ────────────────────────────────────────────────────────


def test_the_image_worker_does_not_bump_an_ignored_rows_api_priority(db_path):
    _raw(db_path, 1, fetch_status=-2, api_priority=0, image_priority=5,
         preview_url="http://example.com/img.jpg")

    _image_bump_once(db_path, {
        "workshop_id": 1, "preview_url": "http://example.com/img.jpg",
        "image_priority": 5, "steam_updated_at": 1,
    })

    row = _row(db_path, 1)
    assert row["api_priority"] == 0, "an ignored item is in no queue"
    assert row["image_priority"] == 4, \
        "the image decrement still runs -- it moves the row out of the image queue"


def test_the_image_worker_still_bumps_a_live_rows_api_priority(db_path):
    _live(db_path, 1, api_priority=0, image_priority=5,
          preview_url="http://example.com/img.jpg")

    _image_bump_once(db_path, {
        "workshop_id": 1, "preview_url": "http://example.com/img.jpg",
        "image_priority": 5, "steam_updated_at": 1,
    })

    assert _row(db_path, 1)["api_priority"] == 2


def test_the_web_worker_does_not_bump_an_ignored_rows_api_priority(db_path):
    _raw(db_path, 1, fetch_status=-2, api_priority=0)

    _web_bump_once(db_path, 1)

    assert _row(db_path, 1)["api_priority"] == 0, "an ignored item is in no queue"


def test_the_web_worker_still_bumps_a_live_rows_api_priority(db_path):
    _live(db_path, 1, api_priority=0)

    _web_bump_once(db_path, 1)

    assert _row(db_path, 1)["api_priority"] == 2


# ── the searchable library hides settled items by default ────────────────────


def test_search_items_hides_dead_and_ignored_by_default(db_path):
    _live(db_path, 1)
    _raw(db_path, 2, fetch_status=None)
    _raw(db_path, 3, fetch_status=-1)
    _raw(db_path, 4, fetch_status=-2)

    assert _ids(search_items(db_path)) == {1, 2}, \
        "the by-default view is the live library only"
    assert _ids(search_items(db_path, include_settled=True)) == {1, 2, 3, 4}, \
        "the parameter is the escape hatch the later surfacing feature uses"


def test_search_items_hiding_survives_an_or_filter_row(db_path):
    """The hiding clause is ANDed into the base, outside the filter group."""
    _raw(db_path, 1, fetch_status=-2, title="Hidden Gem")
    _live(db_path, 2, title="Visible Gem")

    filters = [
        {"field": "Title", "op": "contains", "value": "Hidden", "logic": "OR"},
        {"field": "Title", "op": "contains", "value": "Gem", "logic": "OR"},
    ]
    assert _ids(search_items(db_path, filters=filters)) == {2}


def test_the_creator_list_agrees_with_the_search(db_path):
    _live(db_path, 1, creator_steamid="111")
    _raw(db_path, 2, fetch_status=-2, creator_steamid="222")
    _raw(db_path, 3, fetch_status=-1, creator_steamid="333")

    assert get_all_creator_ids(db_path) == [111], \
        "a creator whose only items are hidden must not be offered"
    assert get_all_creator_ids(db_path, include_settled=True) == [111, 222, 333]


def test_the_wilson_cutoffs_ignore_settled_rows(db_path):
    for i in range(1, 11):
        _live(db_path, i, wilson_favorite_score=float(i),
              wilson_subscription_score=float(i))
    _raw(db_path, 100, fetch_status=-2, wilson_favorite_score=1000.0,
         wilson_subscription_score=1000.0)

    default = compute_wilson_cutoffs(db_path)
    with_settled = compute_wilson_cutoffs(db_path, include_settled=True)

    assert default["wilson_favorite_max"] == 10, \
        "a percentile computed over hidden rows colours the visible ones wrongly"
    assert with_settled["wilson_favorite_max"] == 1000


def test_the_by_id_lookups_still_resolve_a_settled_row(db_path):
    """The detail pane and the item-update poll resolve a row by id.

    A control rather than a proof: the by-id lookups were already unfiltered,
    and this pins that the hiding above did not reach them.
    """
    from src.database import get_item_details, get_items_by_ids

    _raw(db_path, 1, fetch_status=-2)

    assert get_item_details(db_path, 1)["fetch_status"] == -2
    assert {row["workshop_id"] for row in get_items_by_ids(db_path, [1])} == {1}


# ── the metrics answer over the settled set ──────────────────────────────────


def test_the_invariant_metrics_count_an_ignored_item_holding_a_flag(db_path):
    _raw(db_path, 1, fetch_status=-2, api_priority=5)
    _raw(db_path, 2, fetch_status=-2, api_priority=0)

    values = _metric(db_path, "dead_queued", "dead_items_by_queue")

    assert values["dead_queued"] == 1, \
        "an ignored item holding a flag is the same violation as a dead one"
    assert values["dead_items_by_queue"]["api"] == 1


def test_the_invariant_metrics_count_an_ignored_item_holding_a_queue_row(db_path):
    _raw(db_path, 1, fetch_status=-2, translation_priority=0)
    _queue_row(db_path, 1)

    values = _metric(db_path, "dead_queued", "dead_items_by_queue")

    assert values["dead_queued"] == 1
    assert values["dead_items_by_queue"]["translation"] == 1


def test_the_invariant_metrics_read_zero_for_a_clean_ignored_item(db_path):
    _raw(db_path, 1, fetch_status=-2, api_priority=0)

    values = _metric(db_path, "dead_queued", "dead_items_by_queue")

    assert values["dead_queued"] == 0
    assert values["dead_items_by_queue"] == {
        "web": 0, "image": 0, "translation": 0, "api": 0}


def test_coverage_excludes_ignored_items(db_path):
    _live(db_path, 1)
    _raw(db_path, 2, fetch_status=-2)

    coverage = _metric(db_path, "coverage", target_appids=[])["coverage"]

    assert coverage["total"] == 1, "an ignored item can never be covered"


def test_priority_breakdowns_exclude_ignored_items(db_path):
    _raw(db_path, 1, fetch_status=-2, web_scrape_priority=5)

    breakdowns = _metric(db_path, "priority_breakdowns")["priority_breakdowns"]

    assert breakdowns["web_scrape_priority"] == [], \
        "a flagged ignored item is not drainable work"


# ── _promote_stale_items: no behaviour to pin, but the intent is recorded ─────


def test_the_stale_sweep_does_not_promote_an_ignored_row(db_path):
    """The sweep's ``fetch_status = 200`` term already excluded ``-2``.

    Kept so the intent is pinned at the sweep too, but it passes against the
    pre-change source as well: no ignored row can satisfy ``fetch_status = 200``.
    """
    from src.daemon import Daemon

    seed_pacing_delay(db_path, "api", 0.01)
    _raw(db_path, 1, fetch_status=-2, api_priority=0, api_fetched_at=1)

    Daemon({
        "database": {"path": db_path},
        "api": {"key": "test_key"},
        "daemon": {"target_appids": [1062090], "api_batch_size": 10},
    })._promote_stale_items()

    assert _row(db_path, 1)["api_priority"] == 0


# ── the two bulk updates ─────────────────────────────────────────────────────


def test_the_web_bulk_update_does_not_requeue_an_ignored_item(tmp_path):
    from src.webserver import app, init_webserver
    from src.database import initialize_database

    db_path = str(tmp_path / "ignored_web.db")
    initialize_database(db_path)
    init_webserver(db_path, {"database": {"path": db_path},
                             "daemon": {"target_appids": [294100]}})
    _raw(db_path, 1, fetch_status=-2, api_priority=0)
    _live(db_path, 2, api_priority=0)

    response = app.test_client().post("/api/update_visible", json={"ids": [1, 2]})

    assert response.status_code == 200
    assert response.get_json()["queued"] == 1, "only the live row was queued"
    assert _row(db_path, 1)["api_priority"] == 0, "an ignored item is in no queue"
    assert _row(db_path, 2)["api_priority"] == 10


@pytest.mark.asyncio
async def test_the_tui_bulk_update_does_not_requeue_an_ignored_item(db_path):
    """The TUI carries the same guard as the web route (issue 13's shape).

    ``search_items`` hides the ignored row now, so the row is surfaced through
    the same patched search the TUI tests already use -- the point here is the
    bulk UPDATE's guard, not the visibility (pinned above).
    """
    from src.tui import ScraperApp
    from src.database import search_items as real_search

    _raw(db_path, 1, fetch_status=-2, api_priority=0, title="Ignored Item")
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"}}

    with patch("src.tui.load_config", return_value=config), \
         patch("src.tui.search_items",
               side_effect=lambda *a, **k: real_search(*a, include_settled=True, **k)), \
         patch("src.tui.get_all_creator_ids", return_value=[]):
        app = ScraperApp()
        async with app.run_test(size=(120, 200)) as pilot:
            await pilot.pause(ASYNC_PAUSE)
            assert len(app.query_one(ListView).children) == 1
            await app.action_update_visible()
            await pilot.pause(ASYNC_PAUSE)

    assert _row(db_path, 1)["api_priority"] == 0, "an ignored item is in no queue"


# ── stage 2: the Totals count stops calling an ignored row alive ─────────────


def test_item_counts_reports_ignored_separately_from_alive(db_path):
    """The Totals split must account for the settled pair, not only death.

    ``alive`` is the live population the searchable library shows, so an ignored
    row -- hidden by the same feature -- cannot be counted in it. The three
    counts partition the table.
    """
    _live(db_path, 1)
    _raw(db_path, 2, fetch_status=-1)
    _raw(db_path, 3, fetch_status=-2)

    assert _metric(db_path, "item_counts")["item_counts"] == {
        "total": 3, "alive": 1, "dead": 1, "ignored": 1,
    }, "an ignored row is settled, not alive"


@pytest.mark.asyncio
async def test_the_tui_totals_panel_draws_ignored_beside_dead(mock_config, monkeypatch):
    """The TUI Totals chunk renders the ignored count from the one metric value."""
    from src.tui import ScraperApp, StatsScreen
    from tests.test_tui import _FAKE_METRIC_VALUES, _fake_iter_metrics

    monkeypatch.setitem(
        _FAKE_METRIC_VALUES, "item_counts",
        {"total": 4, "alive": 2, "dead": 1, "ignored": 1})

    with patch("src.tui.load_config", return_value=mock_config), \
         patch("src.tui.metrics.iter_metrics", side_effect=_fake_iter_metrics()):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            app.push_screen(StatsScreen(app.db_path))
            await pilot.pause(ASYNC_PAUSE)
            screen = app.screen
            for _ in range(200):
                await pilot.pause(0.02)
                if len(screen._measured_ms) >= len(metrics.all_names()):
                    break
            rendered = str(screen.query_one("#item-counts-content", Static).render())

    assert "Ignored:" in rendered and "1" in rendered
