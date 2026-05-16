import pytest
import json
from unittest.mock import patch, MagicMock
from src.translator import TranslatorThread, is_ascii

@pytest.fixture
def mock_config():
    return {
        "database": {"path": "test.db"},
        "openai": {
            "api_key": "SK-TEST",
            "endpoint": "https://api.openai.com/v1",
            "model": "gpt-4o-mini"
        }
    }

def test_is_ascii():
    assert is_ascii("Hello World") is True
    assert is_ascii("") is True
    assert is_ascii("123!@#") is True
    assert is_ascii("Hello caf\u00e9") is False
    assert is_ascii("\u4f60\u597d\u4e16\u754c") is False

def test_translator_thread_no_config():
    thread = TranslatorThread({"database": {"path": "test.db"}})
    thread.run()

@patch("src.translator.get_next_batch_for_translation")
@patch("time.sleep")
def test_translator_thread_loop(mock_sleep, mock_get_batch, mock_config):
    mock_get_batch.side_effect = [
        [{"id": 1, "item_type": "item", "item_id": 123, "field": "title_en", "original_text": "\uc548\ub155", "priority": 10}],
        None,
    ]
    thread = TranslatorThread(mock_config)
    with patch.object(thread, "_translate_batch") as mock_translate:
        def stop(*a, **kw):
            thread.running = False
        mock_translate.side_effect = stop
        thread.start()
        thread.join(timeout=2)
        assert mock_translate.called

def test_translator_daemon_exception():
    thread = TranslatorThread({"database": {"path": "dummy"}, "openai": {"api_key": "dummy"}})
    thread.running = True
    with patch("src.translator.get_next_batch_for_translation", side_effect=Exception("Test Error")):
        def sleep_mock(*args):
            thread.running = False
        with patch("time.sleep", side_effect=sleep_mock):
            thread.run()
            assert not thread.running


def test_translate_batch_writes_translation_and_resets_priority(tmp_path):
    """Verify _translate_batch updates _en columns, stamps dt_translated,
    deletes from translation_queue, and resets translation_priority."""
    import sqlite3
    from src.database import initialize_database, insert_or_update_item, flag_field_for_translation

    db_path = str(tmp_path / "test_trans.db")
    initialize_database(db_path)

    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "\u30c6\u30b9\u30c8", "short_description": "test",
        "subscriptions": 10, "lifetime_subscriptions": 20, "favorited": 5,
        "views": 100, "status": 200,
    })
    flag_field_for_translation(db_path, "item", 1, "title_en", "\u30c6\u30b9\u30c8", 10)

    conn = sqlite3.connect(db_path)
    prio = conn.execute(
        "SELECT translation_priority FROM workshop_items WHERE workshop_id = 1"
    ).fetchone()[0]
    assert prio == 10
    conn.close()

    config = {
        "database": {"path": db_path},
        "openai": {"api_key": "SK-TEST", "endpoint": "https://test/v1", "model": "gpt-test"},
    }
    thread = TranslatorThread(config)
    thread.db_path = db_path

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = json.dumps([
        {"id": "item_1_title_en", "translated": "Hello"},
    ])
    mock_client.chat.completions.create.return_value = mock_response

    batch = [{
        "id": 1, "item_type": "item", "item_id": 1, "field": "title_en",
        "original_text": "\u30c6\u30b9\u30c8", "priority": 10,
    }]
    thread._translate_batch(batch, mock_client, "gpt-test")

    conn = sqlite3.connect(db_path)
    result = conn.execute(
        "SELECT title_en, dt_translated, translation_priority FROM workshop_items WHERE workshop_id = 1"
    ).fetchone()
    assert result[0] == "Hello"
    assert result[1] is not None
    assert result[2] == 0
    queue_count = conn.execute("SELECT COUNT(*) FROM translation_queue WHERE item_id = 1").fetchone()[0]
    assert queue_count == 0
    conn.close()
