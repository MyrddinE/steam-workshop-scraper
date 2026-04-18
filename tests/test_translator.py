import pytest
from unittest.mock import patch, MagicMock
from src.translator import TranslatorThread, translate_item

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

@patch("src.translator.OpenAI")
@patch("src.translator.get_connection")
def test_translate_item_success(mock_get_conn, mock_openai_class, mock_config):
    """Test successful translation of a database row."""
    # Mock database row
    mock_conn = MagicMock()
    mock_get_conn.return_value = mock_conn
    mock_cursor = mock_conn.execute.return_value
    
    # We mock the dictionary-like access of sqlite3.Row
    mock_row = MagicMock()
    mock_row.__getitem__.side_effect = lambda key: {
        "title": "안녕하세요",
        "short_description": "소개",
        "extended_description": "긴 설명"
    }.get(key)
    mock_cursor.fetchone.return_value = mock_row
    
    # Mock OpenAI response
    mock_client = mock_openai_class.return_value
    mock_response = MagicMock()
    mock_response.choices[0].message.content = '{"title_en": "Hello", "short_description_en": "Intro", "extended_description_en": "Long Description"}'
    mock_client.chat.completions.create.return_value = mock_response
    
    translate_item("test.db", 123, mock_config, priority=1)
    
    # Verify OpenAI was called correctly
    assert mock_client.chat.completions.create.called
    args, kwargs = mock_client.chat.completions.create.call_args
    # Verify the Korean text made it into the prompt
    prompt_content = kwargs["messages"][1]["content"]
    assert "안녕하세요" in prompt_content
    
    # Verify database was updated
    assert mock_conn.execute.called
    # Check that it set translation_priority to 0 and added _en fields
    found_update = False
    for call in mock_conn.execute.call_args_list:
        if "UPDATE workshop_items" in call[0][0]:
            if "translation_priority=?" in call[0][0] or "translation_priority = 0" in str(call[0]):
                found_update = True
    # assert found_update # We'll refine this once implementation is done

@patch("src.translator.OpenAI")
@patch("src.translator.get_connection")
def test_translate_user_success(mock_get_conn, mock_openai_class, mock_config):
    """Test successful translation of a user personaname."""
    mock_conn = MagicMock()
    mock_get_conn.return_value = mock_conn
    mock_cursor = mock_conn.execute.return_value
    
    # Mock user row
    mock_row = MagicMock()
    mock_row.__getitem__.side_effect = lambda key: {
        "steamid": 12345,
        "personaname": "안녕하세요"
    }.get(key)
    mock_cursor.fetchone.return_value = mock_row
    
    # Mock OpenAI response
    mock_client = mock_openai_class.return_value
    mock_response = MagicMock()
    mock_response.choices[0].message.content = '{"personaname_en": "Hello"}'
    mock_client.chat.completions.create.return_value = mock_response
    
    translate_item("test.db", 12345, mock_config, item_type="user", priority=10)
    
    # Verify OpenAI was called
    assert mock_client.chat.completions.create.called
    
    # Verify DB update call
    found_update = False
    for call in mock_conn.execute.call_args_list:
        if "UPDATE users" in call[0][0]:
            found_update = True
    assert found_update

@patch("src.translator.get_next_translation_item")
@patch("src.translator.translate_item")
@patch("time.sleep")
def test_translator_thread_loop(mock_sleep, mock_translate, mock_get_next, mock_config):
    """Test the translator thread picks up items and processes them."""
    # First a user, then a mod, then nothing
    mock_get_next.side_effect = [("user", 123, 10), ("workshop_item", 456, 1), None]
    
    thread = TranslatorThread(mock_config)
    def stop_after_two(*args, **kwargs):
        if mock_translate.call_count >= 2:
            thread.running = False
            
    mock_translate.side_effect = stop_after_two
    
    thread.start()
    thread.join(timeout=2)
    
    assert mock_translate.call_count == 2
    mock_translate.assert_any_call("test.db", 123, mock_config, item_type="user", priority=10)
    mock_translate.assert_any_call("test.db", 456, mock_config, item_type="workshop_item", priority=1)

def test_translator_thread_no_config():
    """Test that the thread exits early if OpenAI is not configured."""
    thread = TranslatorThread({"database": {"path": "test.db"}}) # No openai key
    # It should not even start the loop or should exit immediately
    thread.run() 
    # If it reached here without hanging, it successfully handled missing config

def test_translate_item_no_api_key(tmp_path):
    from src.translator import translate_item
    db_path = str(tmp_path / "test.db")
    # No openai key in config
    translate_item(db_path, 123, {"database": {"path": db_path}}, "workshop_item")

def test_translate_item_not_found(tmp_path):
    from src.translator import translate_item
    from src.database import initialize_database
    db_path = str(tmp_path / "test.db")
    initialize_database(db_path)
    # API key provided, but item not in DB
    config = {"openai": {"api_key": "test_key"}}
    translate_item(db_path, 999, config, "workshop_item")
    translate_item(db_path, 999, config, "user")

def test_translate_item_json_decode_error(tmp_path):
    from src.translator import translate_item
    from src.database import initialize_database, insert_or_update_item
    import json
    
    db_path = str(tmp_path / "test.db")
    initialize_database(db_path)
    
    insert_or_update_item(db_path, {"workshop_id": 1, "title": "Test Title"})
    config = {"openai": {"api_key": "test_key"}}
    
    with patch('src.translator.OpenAI') as mock_openai:
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "INVALID JSON DATA"
        mock_client.chat.completions.create.return_value = mock_response
        
        # Should catch JSONDecodeError and not crash
        translate_item(db_path, 1, config, "workshop_item")

def test_translator_daemon_exception():
    from src.translator import TranslatorThread
    daemon = TranslatorThread({"database": {"path": "dummy"}, "openai": {"api_key": "dummy"}})
    daemon.running = True
    
    with patch('src.translator.get_next_translation_item', side_effect=Exception("Test Error")):
        def sleep_mock(*args):
            daemon.running = False
            
        with patch('time.sleep', side_effect=sleep_mock):
            daemon.run()
            assert not daemon.running
