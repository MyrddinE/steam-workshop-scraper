"""Migration 21->22: filter-excluded items give up their queue priority.

`web_scrape_priority` and `image_priority` *are* priority columns, and the daemon used
to hand them the item's whole pre-fetch `api_priority`. A newly discovered item
carries `3`, so every new item the enrichment filters excluded was queued in the
same band as the ones they selected -- and `MAX(stored, new)` means nothing ever
downgrades it again, so the rows cannot fix themselves.

*Measured live* on 2026-09-17: 868,759 items sat above backlog priority, and
760,782 web entries and 668,269 image ones of those belonged to excluded items,
with 107,365 selected items waiting behind them. At the measured drain rate that
is the difference between three weeks of wanted work and a year of it.

The migration evaluates the filters with `_evaluate_filters` -- the same function
the fetch path uses -- rather than an SQL translation, because the two evaluators
already disagree (the search also searches each field's `_en` counterpart) and a
migration that contradicted the runtime would leave a queue the runtime
immediately re-stamps.
"""

import json
import logging

from src.database import (EXPECTED_VERSION, get_connection, initialize_database,
                          insert_or_update_item)
from tests.conftest import restore_pre_rename_table_names


def _age_to_v21(db_path):
    """Rewind so the next initialize_database runs only migration 21->22.

    A fresh test database is already at the terminal version and this migration
    adds no columns, so rewinding the marker is how the other migration tests
    build a pre-migration database.
    """
    conn = get_connection(db_path)
    restore_pre_rename_table_names(conn)
    conn.execute("PRAGMA user_version = 21")
    conn.commit()
    conn.close()


def _filters(*tags):
    return json.dumps([{"field": "Tags", "op": "contains", "value": t} for t in tags])


def _filters_for(db_path, appid, *tags):
    conn = get_connection(db_path)
    conn.execute("INSERT OR REPLACE INTO app_tracking (appid, enrichment_filters) "
                 "VALUES (?, ?)", (appid, _filters(*tags)))
    conn.commit()
    conn.close()


def _tag(db_path, workshop_id, name):
    conn = get_connection(db_path)
    conn.execute("INSERT OR IGNORE INTO tags (tag_name) VALUES (?)", (name,))
    tag_id = conn.execute("SELECT tag_id FROM tags WHERE tag_name = ?", (name,)).fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO workshop_tags (workshop_id, tag_id) VALUES (?, ?)",
                 (workshop_id, tag_id))
    conn.commit()
    conn.close()


def _queues(db_path, workshop_id):
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT web_scrape_priority, image_priority FROM workshop_items WHERE workshop_id = ?",
        (workshop_id,)).fetchone()
    conn.close()
    return row["web_scrape_priority"], row["image_priority"]


def _item(db_path, workshop_id, **over):
    record = {"workshop_id": workshop_id, "title": f"Item {workshop_id}", "fetch_status": 200,
              "consumer_appid": 294100}
    record.update(over)
    insert_or_update_item(db_path, record)


def test_an_excluded_item_at_discovery_priority_goes_back_to_backlog(db_path):
    # The row is written while the database is still current (`fetch_status`);
    # `_age_to_v21` then rewinds it to the v21 shape (the old `status` column and
    # the `app_tracking` table) before the migration is replayed.
    _item(db_path, 1, web_scrape_priority=3, image_priority=3)   # matches nothing: no tags
    _age_to_v21(db_path)
    _filters_for(db_path, 294100, "Mature")

    initialize_database(db_path)

    assert _queues(db_path, 1) == (1, 1)


def test_an_item_the_filters_select_keeps_its_priority(db_path):
    """The whole point is the ordering between the two, not a blanket reset."""
    _item(db_path, 1, web_scrape_priority=3, image_priority=3)
    _tag(db_path, 1, "Mature")
    _age_to_v21(db_path)
    _filters_for(db_path, 294100, "Mature")

    initialize_database(db_path)

    assert _queues(db_path, 1) == (3, 3)


def test_backlog_and_idle_entries_are_left_alone(db_path):
    """`MIN(column, 1)` means 1 stays 1 and 0 (not queued) stays 0."""
    _item(db_path, 1, web_scrape_priority=1, image_priority=0)
    _age_to_v21(db_path)
    _filters_for(db_path, 294100, "Mature")

    initialize_database(db_path)

    assert _queues(db_path, 1) == (1, 0)


def test_a_user_requested_priority_is_kept(db_path):
    """5 and 10 are a person looking at the item, not the daemon's bookkeeping.

    Both the old rule and the new one can only have written 5 or 10 here because
    someone had the item on screen, so demoting those would overrule a user to
    tidy up after the daemon. The migration undoes the daemon's own priorities
    and nothing else.
    """
    _item(db_path, 1, web_scrape_priority=10, image_priority=5)
    _age_to_v21(db_path)
    _filters_for(db_path, 294100, "Mature")

    initialize_database(db_path)

    assert _queues(db_path, 1) == (10, 5)


def test_an_appid_without_filters_is_not_touched(db_path):
    """No filters means everything matches, so nothing is excluded."""
    _item(db_path, 1, web_scrape_priority=3, image_priority=3)
    _age_to_v21(db_path)

    initialize_database(db_path)

    assert _queues(db_path, 1) == (3, 3)


def test_an_unreadable_filter_set_enriches_rather_than_excludes(db_path):
    """A malformed filter list must not be read as "excludes everything"."""
    _item(db_path, 1, web_scrape_priority=3, image_priority=3)
    _age_to_v21(db_path)
    conn = get_connection(db_path)
    conn.execute("INSERT OR REPLACE INTO app_tracking (appid, enrichment_filters) "
                 "VALUES (?, ?)", (294100, "{not json"))
    conn.commit()
    conn.close()

    initialize_database(db_path)

    assert _queues(db_path, 1) == (3, 3)


def test_the_migration_reports_what_it_returned(db_path, caplog):
    _item(db_path, 1, web_scrape_priority=3, image_priority=3)
    _item(db_path, 2, web_scrape_priority=3, image_priority=0)
    _age_to_v21(db_path)
    _filters_for(db_path, 294100, "Mature")

    with caplog.at_level(logging.INFO):
        initialize_database(db_path)

    assert "Returned 2 web and 1 image queue entries to backlog priority" in caplog.text


def test_the_migration_runs_once_and_leaves_no_rows_behind(db_path):
    """A second call has nothing left to do, which is what makes it idempotent."""
    _item(db_path, 1, web_scrape_priority=3, image_priority=3)
    _age_to_v21(db_path)
    _filters_for(db_path, 294100, "Mature")
    initialize_database(db_path)

    # Nothing is above backlog for an excluded item now, so re-running the
    # migration (by rewinding the marker) finds no candidates at all.
    _age_to_v21(db_path)
    initialize_database(db_path)

    assert _queues(db_path, 1) == (1, 1)


def test_the_terminal_version_is_reached(db_path):
    _item(db_path, 1, web_scrape_priority=3)
    _age_to_v21(db_path)

    initialize_database(db_path)

    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == EXPECTED_VERSION
