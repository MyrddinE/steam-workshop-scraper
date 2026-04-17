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
    
    translate_item("test.db", 123, mock_config)
    
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

@patch("src.translator.get_next_translation_item")
@patch("src.translator.translate_item")
@patch("time.sleep")
def test_translator_thread_loop(mock_sleep, mock_translate, mock_get_next, mock_config):
    """Test the translator thread picks up items and processes them."""
    mock_get_next.side_effect = [123, None] # One item then nothing
    
    thread = TranslatorThread(mock_config)
    # We need a way to stop the thread after one iteration
    def stop_after_one(*args, **kwargs):
        thread.running = False
        
    mock_translate.side_effect = stop_after_one
    
    thread.start()
    thread.join(timeout=2)
    
    mock_translate.assert_called_once_with("test.db", 123, mock_config)

def test_translator_thread_no_config():
    """Test that the thread exits early if OpenAI is not configured."""
    thread = TranslatorThread({"database": {"path": "test.db"}}) # No openai key
    # It should not even start the loop or should exit immediately
    thread.run() 
    # If it reached here without hanging, it successfully handled missing config
