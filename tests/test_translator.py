import pytest
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
    assert is_ascii("Hello café") is False
    assert is_ascii("你好世界") is False

def test_translator_thread_no_config():
    thread = TranslatorThread({"database": {"path": "test.db"}})
    thread.run()

@patch("src.translator.get_next_batch_for_translation")
@patch("time.sleep")
def test_translator_thread_loop(mock_sleep, mock_get_batch, mock_config):
    mock_get_batch.side_effect = [
        [{"id": 1, "item_type": "item", "item_id": 123, "field": "title_en", "original_text": "안녕", "priority": 10}],
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
