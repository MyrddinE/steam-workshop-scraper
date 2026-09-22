"""The owner's creator-ignore flag: schema, cascade and the shared restore rule.

Flagging a creator as ignored makes every one of their workshop items ignored,
and new items from that creator ignored as they are scraped. It is a **two-way
toggle with no provenance**: un-ignoring a creator restores every item of theirs
currently at ``-2`` -- including items that were ignored individually -- because
no column records which ignore was inherited. Items the API settled as dead
(``-1``) are untouched in both directions.

The new names are reached through the ``database`` module rather than imported at
module scope on purpose: this file has to *collect* against the pre-change source
so each test can be shown failing on its own, and a top-level import of a name
that did not exist would turn every test into one collection error.

The restore half is deliberately not a second implementation: ``unignore_creator``
applies ``unignore_item``'s assignment list, and
:func:`test_a_creator_restore_matches_the_single_item_restore` pins that the two
produce identical rows.
"""

from src import database
from src.database import (
    EXPECTED_VERSION,
    get_connection,
    initialize_database,
    insert_or_update_creator,
    insert_or_update_item,
)


# ── helpers ──────────────────────────────────────────────────────────────────

FLAG_COLUMNS = (
    "api_priority", "web_scrape_priority", "image_priority", "translation_priority",
)


def _live(db_path, workshop_id, creator, **over):
    record = {"workshop_id": workshop_id, "title": f"item {workshop_id}",
              "fetch_status": 200, "creator_steamid": creator}
    record.update(over)
    return insert_or_update_item(db_path, record)


def _raw(db_path, workshop_id, creator=None, **over):
    record = {"workshop_id": workshop_id, "title": f"item {workshop_id}"}
    if creator is not None:
        record["creator_steamid"] = creator
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


def _creator_columns(db_path):
    conn = get_connection(db_path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(creators)")}
    finally:
        conn.close()


def _version(db_path):
    conn = get_connection(db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _regress_to_v38(db_path):
    """The v38 shape: the creators table without the flag, the marker at 38."""
    conn = get_connection(db_path)
    conn.execute("ALTER TABLE creators DROP COLUMN ignored_at")
    conn.execute("PRAGMA user_version = 38")
    conn.commit()
    conn.close()


# ── the schema change and its migration ──────────────────────────────────────


def test_the_expected_version_is_39():
    assert EXPECTED_VERSION == 39


def test_a_fresh_database_declares_the_creator_flag(tmp_path):
    """The current-schema builder mirrors the migration, or the endpoints differ."""
    path = str(tmp_path / "fresh.db")
    initialize_database(path, legacy_chain=False)

    assert _version(path) == EXPECTED_VERSION
    assert "ignored_at" in _creator_columns(path)
    assert database.CREATOR_COLUMNS >= {"ignored_at"}, \
        "the upsert whitelist moves with the schema"


def test_migration_38_to_39_adds_the_flag_defaulting_to_not_ignored(db_path):
    insert_or_update_creator(db_path, {"steamid": 7, "personaname": "Author"})
    _regress_to_v38(db_path)
    assert "ignored_at" not in _creator_columns(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert "ignored_at" in _creator_columns(db_path)
    row = database.get_creator(db_path, 7)
    assert row["ignored_at"] is None, "an existing creator is not ignored"
    assert database.creator_is_ignored(db_path, 7) is False


def test_the_step_reaches_its_own_target_when_called_directly(db_path):
    """Called directly, the step sets 39 -- its target, not the build's version."""
    conn = get_connection(db_path)
    conn.execute("ALTER TABLE creators DROP COLUMN ignored_at")
    conn.commit()
    database._migration_38_to_39(conn.cursor(), conn, db_path)
    conn.commit()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert version == 39
    assert "ignored_at" in _creator_columns(db_path)


def test_the_step_is_a_no_op_when_the_column_is_already_present(db_path):
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 38")
    conn.commit()
    database._migration_38_to_39(conn.cursor(), conn, db_path)
    conn.commit()
    conn.close()

    assert _version(db_path) == 39
    assert "ignored_at" in _creator_columns(db_path)


# ── creator_is_ignored ───────────────────────────────────────────────────────


def test_creator_is_ignored_reads_the_flag(db_path):
    insert_or_update_creator(db_path, {"steamid": 7, "personaname": "Author"})

    assert database.creator_is_ignored(db_path, 7) is False
    database.ignore_creator(db_path, 7)
    assert database.creator_is_ignored(db_path, 7) is True
    assert database.creator_is_ignored(db_path, 999) is False, \
        "a creator with no row was never flagged"


# ── ignore_creator: the cascade ──────────────────────────────────────────────


def test_ignore_creator_settles_every_live_item_of_that_creator(db_path):
    _live(db_path, 1, 111, api_priority=5, web_scrape_priority=5,
          image_priority=5, translation_priority=5)
    _live(db_path, 2, 111, api_priority=3)
    _raw(db_path, 3, 111, fetch_status=None, api_priority=3)
    _live(db_path, 4, 222, api_priority=5)

    assert database.ignore_creator(db_path, 111) is True

    for wid in (1, 2, 3):
        row = _row(db_path, wid)
        assert row["fetch_status"] == -2, f"item {wid} is ignored"
        assert all(row[column] == 0 for column in FLAG_COLUMNS), \
            f"an ignored item holds no queue flag ({wid})"
    assert _row(db_path, 4)["fetch_status"] == 200, \
        "another creator's item is not touched"
    assert _row(db_path, 4)["api_priority"] == 5


def test_ignore_creator_flags_a_creator_with_no_row(db_path):
    """The button must persist the flag even when the profile was never fetched."""
    _live(db_path, 1, 111)
    assert database.get_creator(db_path, 111) is None

    assert database.ignore_creator(db_path, 111) is True

    assert database.creator_is_ignored(db_path, 111) is True
    assert database.get_creator(db_path, 111)["ignored_at"] is not None


def test_ignore_creator_deletes_its_items_translation_rows_only(db_path):
    _live(db_path, 1, 111)
    _queue_row(db_path, 1)
    # A creator row shares the numeric id space and is a different entity.
    _queue_row(db_path, 1, entity_type="user")
    _live(db_path, 2, 222)
    _queue_row(db_path, 2)

    database.ignore_creator(db_path, 111)

    assert _queue_rows(db_path) == [
        ("item", 2, "title_en"), ("user", 1, "title_en"),
    ], "the item's rows go; a creator row and another creator's item stay"


def test_ignore_creator_leaves_a_dead_item_dead_and_its_rows_alone(db_path):
    _raw(db_path, 1, 111, fetch_status=-1, api_priority=0)
    _queue_row(db_path, 1)
    _live(db_path, 2, 111)

    database.ignore_creator(db_path, 111)

    row = _row(db_path, 1)
    assert row["fetch_status"] == -1, "dead stays dead; it must not become -2"
    assert _queue_rows(db_path) == [("item", 1, "title_en")], \
        "a dead item's translation rows are not this direction's business"
    assert _row(db_path, 2)["fetch_status"] == -2


def test_ignore_creator_is_idempotent(db_path):
    _live(db_path, 1, 111, api_priority=5)
    assert database.ignore_creator(db_path, 111) is True
    assert database.ignore_creator(db_path, 111) is False, \
        "nothing changed on the second press"
    assert database.creator_is_ignored(db_path, 111) is True


def test_ignore_creator_reports_a_stranded_queue_row_as_a_change(db_path):
    """A cleanly ignored item with a leftover queue row still reports changed.

    That is the shape ``ignore_item`` reports as changed too: the row is already
    at ``-2`` but its translation work is still queued, so the call did do
    something (delete the row).
    """
    _raw(db_path, 1, 111, fetch_status=-2, api_priority=0)
    _queue_row(db_path, 1)
    insert_or_update_creator(db_path, {"steamid": 111, "personaname": "Author"})

    assert database.ignore_creator(db_path, 111) is True
    assert _queue_rows(db_path) == []


# ── unignore_creator: the restore rule ───────────────────────────────────────


def test_unignore_creator_restores_fetched_never_fetched_and_description_less(db_path):
    database.ignore_creator(db_path, 111)
    _raw(db_path, 1, 111, fetch_status=-2, api_fetched_at=123,
         extended_description="present", api_priority=0, web_scrape_priority=0)
    _raw(db_path, 2, 111, fetch_status=-2, api_fetched_at=None,
         extended_description=None, api_priority=0, web_scrape_priority=0)
    _raw(db_path, 3, 111, fetch_status=-2, api_fetched_at=123,
         extended_description=None, api_priority=0, web_scrape_priority=0)

    assert database.unignore_creator(db_path, 111) is True

    fetched = _row(db_path, 1)
    assert fetched["fetch_status"] == 200
    assert fetched["api_priority"] == 0
    assert fetched["web_scrape_priority"] == 0

    never = _row(db_path, 2)
    assert never["fetch_status"] is None, "never fetched comes back as discovered"
    assert never["api_priority"] == 1
    assert never["web_scrape_priority"] == 3

    description_less = _row(db_path, 3)
    assert description_less["fetch_status"] == 200
    assert description_less["web_scrape_priority"] == 3, \
        "ignoring cleared the scrape flag; the row's data says it is still due"

    assert database.creator_is_ignored(db_path, 111) is False


def test_unignore_creator_leaves_a_dead_item_dead(db_path):
    database.ignore_creator(db_path, 111)
    _raw(db_path, 1, 111, fetch_status=-1, api_fetched_at=None, api_priority=0)

    assert database.unignore_creator(db_path, 111) is True, \
        "the flag moves back, but the dead item is outside the restore"
    assert _row(db_path, 1)["fetch_status"] == -1


def test_unignore_creator_is_idempotent(db_path):
    _raw(db_path, 1, 111, fetch_status=-2, api_fetched_at=None, extended_description=None)
    insert_or_update_creator(db_path, {"steamid": 111, "personaname": "Author"})

    assert database.unignore_creator(db_path, 111) is True
    assert database.unignore_creator(db_path, 111) is False, "already restored"
    assert database.creator_is_ignored(db_path, 111) is False


def test_ignore_then_unignore_creator_round_trips(db_path):
    _live(db_path, 1, 111, api_fetched_at=123, extended_description="present",
          api_priority=0)

    assert database.ignore_creator(db_path, 111) is True
    assert database.unignore_creator(db_path, 111) is True

    row = _row(db_path, 1)
    assert row["fetch_status"] == 200
    assert all(row[column] == 0 for column in FLAG_COLUMNS)
    assert database.creator_is_ignored(db_path, 111) is False


def test_a_creator_restore_matches_the_single_item_restore(db_path):
    """The shared assignment list, pinned by the two paths agreeing.

    Four rows with the same starting data: two ignored individually and two
    ignored through their creator. Restoring one of each pair by the single-item
    and creator paths must leave two byte-identical rows -- a second copy of the
    rule in ``unignore_creator`` would drift here.
    """
    for wid in (1, 2):
        _raw(db_path, wid, 111, fetch_status=-2, api_fetched_at=123,
             extended_description=None, api_priority=0, web_scrape_priority=0)
    for wid in (3, 4):
        _raw(db_path, wid, 222, fetch_status=-2, api_fetched_at=123,
             extended_description=None, api_priority=0, web_scrape_priority=0)

    database.unignore_item(db_path, 1)
    database.unignore_creator(db_path, 222)

    restored_columns = (
        "fetch_status", "api_priority", "web_scrape_priority",
        "image_priority", "translation_priority",
    )
    assert _row(db_path, 1, ", ".join(restored_columns)) == \
        _row(db_path, 3, ", ".join(restored_columns)), \
        "the single-item and creator restores must apply the same rule"
    assert _row(db_path, 1)["fetch_status"] == 200


# ── the one toggle rule and the one label ────────────────────────────────────


def test_toggle_creator_ignored_reports_the_direction(db_path):
    _live(db_path, 1, 111, api_fetched_at=123, extended_description="present")

    assert database.toggle_creator_ignored(db_path, 111) is True
    assert database.creator_is_ignored(db_path, 111) is True
    assert _row(db_path, 1)["fetch_status"] == -2

    assert database.toggle_creator_ignored(db_path, 111) is False
    assert database.creator_is_ignored(db_path, 111) is False
    assert _row(db_path, 1)["fetch_status"] == 200


def test_the_creator_ignore_label_names_the_next_move():
    assert database.creator_ignore_label(False) == "Ignore creator"
    assert database.creator_ignore_label(True) == "Un-ignore creator"
    assert database.creator_ignore_label(None) == "Ignore creator", \
        "a missing flag is not ignored"
