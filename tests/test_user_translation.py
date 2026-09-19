"""Creator names are translated through the queue, like every other field.

Issue 45: `_build_user_record` raised `users.translation_priority` for a
non-ASCII persona and nothing queued the name. That line was a complete producer
while the (since removed) `get_next_translation_item` scanned `users` by the flag;
when the per-field
`translation_queue` replaced that scan the producer was never ported, so no
`item_type='user'` row was ever written and no creator name was translated after
2026-05-10. Issue 46: the translator's completion pass was hard-coded to items, so
a steamid would have counted against `item_type='item'` and completed against
`workshop_items` keyed by that id, while the creator's own mirror was never
cleared.

These tests pin both halves and the repair: the daemon queues the name whenever
it fetches a persona, the translator writes `personaname_en` and clears the
user's mirror without touching `workshop_items`, an ASCII name is never queued,
the item path is unchanged, and migration 27->28 returns the flags that were
raised without a queue row behind them.
"""

from unittest.mock import MagicMock, patch

from src.daemon import Daemon
from src.database import (
    EXPECTED_VERSION,
    queue_field_for_translation,
    get_connection,
    initialize_database,
    insert_or_update_item,
    insert_or_update_creator,
)
from src.translator import TranslatorThread

CREATOR = 111


def _daemon(db_path, tmp_path):
    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"api_batch_size": 1, "target_appids": [1]},
    }
    return Daemon(config, config_path=str(tmp_path / "config.yaml"))


def _rows(db_path, sql, params=()):
    conn = get_connection(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _user(db_path, steamid):
    rows = _rows(db_path, "SELECT * FROM users WHERE steamid=?", (steamid,))
    return dict(rows[0]) if rows else None


def _user_queue_rows(db_path, steamid):
    return [
        tuple(row)
        for row in _rows(
            db_path,
            "SELECT field, original_text, priority FROM translation_queue "
            "WHERE item_type='user' AND item_id=?",
            (steamid,),
        )
    ]


def _age_to_v27(db_path):
    conn = get_connection(db_path)
    conn.execute("PRAGMA user_version = 27")
    conn.commit()
    conn.close()


def _version(db_path):
    return _rows(db_path, "PRAGMA user_version")[0][0]


def _translate(db_path, batch, text="Author"):
    """Run the real `_translate_batch` against a stubbed OpenAI client.

    The boundary phrase is drawn per request, so it is held still here and the stub
    reply is written in the shape the request asks for: boundary lines copied, the
    translation beneath each one.
    """
    from src.translator import field_label

    thread = TranslatorThread({
        "database": {"path": db_path},
        "openai": {"api_key": "SK-TEST", "endpoint": "https://test/v1", "model": "gpt-test"},
    })
    thread.db_path = db_path
    row = batch[0]
    phrase = "goat smelt bob and"
    client = MagicMock()
    client.chat.completions.create.return_value.choices = [MagicMock()]
    client.chat.completions.create.return_value.choices[0].message.content = (
        f"{phrase} {row['item_id']} {field_label(row['field'])}\n{text}"
    )
    with patch("src.translator.choose_phrase", return_value=phrase):
        thread._translate_batch(batch, client, "gpt-test")


# ── the producer: a fetched persona queues its name ──────────────────────────

def test_a_stale_creator_refresh_queues_a_non_ascii_name(db_path, tmp_path):
    """The refresh path is the steady-state producer."""
    daemon = _daemon(db_path, tmp_path)
    with patch("src.daemon.get_player_summaries",
               return_value={222: {"personaname": "作者"}}):
        daemon._refresh_creators([222])

    assert _user_queue_rows(db_path, 222) == [("personaname_en", "作者", 1)]
    assert _user(db_path, 222)["translation_priority"] == 1
    assert _user(db_path, 222)["personaname_en"] is None



def test_an_ascii_creator_is_not_queued(db_path, tmp_path):
    """A name that needs no translation writes only the profile."""
    daemon = _daemon(db_path, tmp_path)
    with patch("src.daemon.get_player_summaries",
               return_value={444: {"personaname": "PlainName"}}):
        daemon._refresh_creators([444])

    assert _user(db_path, 444)["personaname"] == "PlainName"
    assert _user(db_path, 444)["translation_priority"] == 0
    assert _user_queue_rows(db_path, 444) == []


# ── the consumer: a creator completes on `users`, never on `workshop_items` ──

def _seed_creator_with_a_namesake_item(db_path):
    """A creator and an item sharing the id 111, plus the creator's queue row.

    The item also carries a raised mirror with no queue row, so the old
    completion pass -- keyed on the id alone, with its count filtered to
    `item_type='item'` -- would find "no fields left" for the creator, zero the
    item and stamp the item's completion.
    """
    insert_or_update_item(db_path, {
        "workshop_id": CREATOR, "status": 200, "translation_priority": 5,
    })
    insert_or_update_creator(db_path, {
        "steamid": CREATOR, "personaname": "作者", "translation_priority": 1,
    })
    queue_field_for_translation(db_path, "user", CREATOR, "personaname_en", "作者", 1)


def _translate_the_creator(db_path, text="Author"):
    _translate(
        db_path,
        [{"id": 1, "item_type": "user", "item_id": CREATOR,
          "field": "personaname_en", "original_text": "作者", "priority": 1}],
        text,
    )


def test_a_creator_never_completes_against_workshop_items(db_path):
    """A steamid must never reach the item completion write."""
    _seed_creator_with_a_namesake_item(db_path)

    _translate_the_creator(db_path)

    item = dict(_rows(
        db_path,
        "SELECT translation_priority, translated_at FROM workshop_items WHERE workshop_id=?",
        (CREATOR,),
    )[0])
    assert item["translation_priority"] == 5, "a steamid is not a workshop_id"
    assert item["translated_at"] is None
    assert _user(db_path, CREATOR)["personaname_en"] == "Author"


def test_a_creator_completion_clears_the_users_mirror(db_path):
    """The per-field write stamps `translated_at`; only the mirror is left."""
    _seed_creator_with_a_namesake_item(db_path)

    _translate_the_creator(db_path)

    assert _user(db_path, CREATOR)["translated_at"] is not None
    assert _user(db_path, CREATOR)["translation_priority"] == 0
    assert _user_queue_rows(db_path, CREATOR) == []


def test_the_item_completion_path_is_unchanged(db_path):
    """An item still completes only when its last queued field drains."""
    insert_or_update_item(db_path, {
        "workshop_id": 7, "status": 200, "steam_updated_at": 1000,
    })
    queue_field_for_translation(db_path, "item", 7, "title_en", "テスト", 3)
    queue_field_for_translation(db_path, "item", 7, "short_description_en", "説明", 3)

    _translate(
        db_path,
        [{"id": 1, "item_type": "item", "item_id": 7, "field": "title_en",
          "original_text": "テスト", "priority": 3}],
        "Test",
    )
    partial = dict(_rows(
        db_path,
        "SELECT translation_priority, translated_at FROM workshop_items WHERE workshop_id=7",
    )[0])
    assert partial["translation_priority"] == 3, "one field still queued is a partial stage"
    assert partial["translated_at"] is None

    _translate(
        db_path,
        [{"id": 2, "item_type": "item", "item_id": 7,
          "field": "short_description_en", "original_text": "説明", "priority": 3}],
        "Description",
    )
    done = _rows(
        db_path,
        "SELECT title_en, short_description_en, translation_priority, translated_at "
        "FROM workshop_items WHERE workshop_id=7",
    )[0]
    assert done["title_en"] == "Test"
    assert done["short_description_en"] == "Description"
    assert done["translation_priority"] == 0
    assert done["translated_at"] is not None


# ── the repair: flags raised with no queue row behind them ───────────────────

def test_migration_queues_a_creator_name_whose_flag_had_no_work(db_path):
    insert_or_update_creator(db_path, {
        "steamid": 555, "personaname": "作者", "translation_priority": 1,
    })
    _age_to_v27(db_path)

    initialize_database(db_path)

    assert _version(db_path) == EXPECTED_VERSION
    assert _user_queue_rows(db_path, 555) == [("personaname_en", "作者", 1)]
    assert _user(db_path, 555)["translation_priority"] == 1


def test_migration_clears_a_creator_flag_with_nothing_to_translate(db_path):
    """A name that is ASCII now can never be re-queued, so its flag must go."""
    insert_or_update_creator(db_path, {
        "steamid": 666, "personaname": "Plain", "translation_priority": 1,
    })
    _age_to_v27(db_path)

    initialize_database(db_path)

    assert _user(db_path, 666)["translation_priority"] == 0
    assert _user_queue_rows(db_path, 666) == []


def test_migration_leaves_a_current_creator_translation_alone(db_path):
    """Translated since the profile was last fetched means there is no work."""
    insert_or_update_creator(db_path, {
        "steamid": 777, "personaname": "作者", "personaname_en": "Author",
        "translated_at": 2000, "api_fetched_at": 1000, "translation_priority": 1,
    })
    _age_to_v27(db_path)

    initialize_database(db_path)

    assert _user_queue_rows(db_path, 777) == []
    assert _user(db_path, 777)["translation_priority"] == 0


def test_migration_does_not_touch_an_item_mirror(db_path):
    """The item side is migration 22->23's repair, not this one's."""
    insert_or_update_item(db_path, {
        "workshop_id": 9, "title": "テスト", "status": 200, "translation_priority": 5,
    })
    _age_to_v27(db_path)

    initialize_database(db_path)

    assert _rows(
        db_path, "SELECT translation_priority FROM workshop_items WHERE workshop_id=9"
    )[0][0] == 5


def test_the_creator_migration_is_idempotent(db_path):
    insert_or_update_creator(db_path, {
        "steamid": 888, "personaname": "作者", "translation_priority": 1,
    })
    _age_to_v27(db_path)
    initialize_database(db_path)
    _age_to_v27(db_path)
    initialize_database(db_path)

    assert _user_queue_rows(db_path, 888) == [("personaname_en", "作者", 1)]
