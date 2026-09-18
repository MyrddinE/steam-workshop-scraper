"""``translation_priority`` must mirror ``translation_queue``, not approximate it.

The column is documented as a mirror of the queue: a producer raises it when it
queues a field, and the translator zeroes it when the item's last queue row is
deleted. Issue 29 found items in the live database carrying a priority with no
queue row behind them, so the item reads as permanently pending and no producer
will queue it again -- every producer skips a translation that is already
current.

The cause was in `flag_field_for_translation`: it wrote the queue row and the
parent mirror on two separate connections, so a translator drain landing between
the two committed writes deleted the row and zeroed the mirror, and the second
write raised the mirror again from `MAX(0, priority)`. These tests pin the
invariant that the two must be written atomically, that the daemon's API merge
does not carry a stale mirror back over a drain, and that the migration repairs
the rows the old helper stranded.
"""

import json
from unittest.mock import MagicMock, patch

from src.database import (
    EXPECTED_VERSION,
    flag_field_for_translation,
    get_connection,
    initialize_database,
    insert_or_update_item,
    insert_or_update_user,
)


def _queue_count(db_path, item_id):
    conn = get_connection(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM translation_queue WHERE item_type='item' AND item_id=?",
        (item_id,),
    ).fetchone()[0]
    conn.close()
    return count


def _mirror(db_path, item_id):
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT translation_priority FROM workshop_items WHERE workshop_id=?",
        (item_id,),
    ).fetchone()
    conn.close()
    return row["translation_priority"] if row else None


def _drain_as_the_translator(db_path, item_id, translated_text="Test", version=1000):
    """Do what `TranslatorThread._translate_batch` does when it finishes a field.

    The real batch deletes the queue row and, when no rows remain for the item,
    zeroes the mirror. Reproducing the SQL keeps the schedule injection in the
    test honest: it is exactly the drain the translator runs on its own thread.
    """
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE workshop_items SET title_en=?, translate_version=? WHERE workshop_id=?",
        (translated_text, version, item_id),
    )
    conn.execute(
        "DELETE FROM translation_queue WHERE item_type='item' AND item_id=?",
        (item_id,),
    )
    conn.execute(
        "UPDATE workshop_items SET translation_priority=0 WHERE workshop_id=?",
        (item_id,),
    )
    conn.commit()
    conn.close()


# ── the cause: the helper's two writes were not atomic ───────────────────────

def test_flag_is_atomic_against_a_translator_drain(db_path):
    """Inject the drain into the exact gap the two-transaction version exposed.

    The old helper called `get_connection` twice: once for the queue row and
    once for the mirror. This hook runs the translator's drain on the second
    call, which is the interleaving that stranded the live rows. With both
    writes in one transaction there is no second call, the drain cannot land
    between them, and the mirror and the queue stay in step.
    """
    import src.database as dbmod

    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "テスト", "status": 200, "steam_updated_at": 1000,
    })

    real_get_connection = dbmod.get_connection
    calls = {"n": 0}

    def get_connection_with_a_drain(path):
        calls["n"] += 1
        if calls["n"] == 2:
            _drain_as_the_translator(db_path, 1)
        return real_get_connection(path)

    with patch.object(dbmod, "get_connection", get_connection_with_a_drain):
        flag_field_for_translation(db_path, "item", 1, "title_en", "テスト", 5)

    # The invariant in both directions: a priority implies a queued field, and
    # an empty queue implies no priority.
    queued = _queue_count(db_path, 1)
    mirror = _mirror(db_path, 1)
    assert (mirror > 0) == (queued > 0), (
        f"mirror={mirror} with {queued} queued field(s): the mirror and the "
        "queue disagree"
    )


def test_flag_raises_the_mirror_and_queues_the_field(db_path):
    """The ordinary case still does both, and the queue row is the source."""
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト"})

    flag_field_for_translation(db_path, "item", 1, "title_en", "テスト", 5)

    assert _queue_count(db_path, 1) == 1
    assert _mirror(db_path, 1) == 5


def test_reflagging_never_downgrades(db_path):
    """A lower priority after a higher one leaves both queue and mirror alone."""
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト"})

    flag_field_for_translation(db_path, "item", 1, "title_en", "テスト", 10)
    flag_field_for_translation(db_path, "item", 1, "title_en", "テスト", 3)

    conn = get_connection(db_path)
    queued = conn.execute(
        "SELECT priority FROM translation_queue WHERE item_id=1"
    ).fetchone()[0]
    conn.close()
    assert queued == 10
    assert _mirror(db_path, 1) == 10


# ── the daemon merge must not carry the mirror either ────────────────────────

def test_api_merge_does_not_carry_translation_priority(db_path):
    """`translation_priority` is queue-owned, so the API merge drops it.

    The daemon reads the item before the API call and writes it back after, so
    a snapshot carried through the merge would overwrite a translator drain
    that happened while the request was in flight.
    """
    from src.daemon import Daemon, MERGE_EXCLUDED_KEYS, MERGE_ITEM_KEYS

    assert "translation_priority" in MERGE_EXCLUDED_KEYS
    assert "translation_priority" not in MERGE_ITEM_KEYS

    with patch("src.daemon.save_config"):
        daemon = Daemon({"database": {"path": db_path}, "api": {"key": "K"},
                         "daemon": {"target_appids": [1]}})
    merged = daemon._merge_and_clean_api_data(
        {"title": "Test"}, {"workshop_id": 1, "translation_priority": 5}, 1, 999)

    assert "translation_priority" not in merged


# ── the repair migration ─────────────────────────────────────────────────────

def _age_to_v22(db_path):
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 22")
    conn.commit()
    conn.close()


def _raw_queue_row(db_path, item_id, field="title_en", priority=5):
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO translation_queue (item_type, item_id, field, original_text, priority, queued_at) "
        "VALUES ('item', ?, ?, ?, ?, 1)",
        (item_id, field, "テスト", priority),
    )
    conn.commit()
    conn.close()


def test_migration_clears_a_mirror_with_no_queued_field(db_path):
    """The rows issue 29 measured: priority set, nothing left to translate."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "テスト", "title_en": "Test",
        "status": 200, "steam_updated_at": 1000, "translate_version": 1000,
        "translation_priority": 5,
    })
    _age_to_v22(db_path)

    initialize_database(db_path)

    conn = get_connection(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == EXPECTED_VERSION
    assert _mirror(db_path, 1) == 0
    assert _queue_count(db_path, 1) == 0


def test_migration_leaves_a_genuinely_queued_item_alone(db_path):
    """A mirror with a queue row behind it is work, not debris."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "テスト", "status": 200,
        "translation_priority": 5,
    })
    _raw_queue_row(db_path, 1)
    _age_to_v22(db_path)

    initialize_database(db_path)

    assert _mirror(db_path, 1) == 5
    assert _queue_count(db_path, 1) == 1


def test_migration_does_not_touch_user_translation_priority(db_path):
    """Users have no `translation_queue` rows by design; their mirror is separate."""
    insert_or_update_user(db_path, {
        "steamid": 76561198000000000, "personaname": "テスト", "translation_priority": 1,
    })
    _age_to_v22(db_path)

    initialize_database(db_path)

    conn = get_connection(db_path)
    priority = conn.execute(
        "SELECT translation_priority FROM users WHERE steamid=?",
        (76561198000000000,),
    ).fetchone()[0]
    conn.close()
    assert priority == 1


def test_migration_only_repairs_the_high_direction(db_path):
    """A queue row whose mirror is zero keeps both: the translator reads the queue."""
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "テスト"})
    _raw_queue_row(db_path, 1)
    _age_to_v22(db_path)

    initialize_database(db_path)

    assert _mirror(db_path, 1) == 0
    assert _queue_count(db_path, 1) == 1


def test_migration_is_idempotent(db_path):
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "テスト", "status": 200, "translation_priority": 5,
    })
    _age_to_v22(db_path)
    initialize_database(db_path)
    assert _mirror(db_path, 1) == 0

    _age_to_v22(db_path)
    initialize_database(db_path)

    assert _mirror(db_path, 1) == 0
