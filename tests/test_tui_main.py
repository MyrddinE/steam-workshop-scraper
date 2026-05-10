import pytest
import sys
import logging
from unittest.mock import patch, MagicMock

def test_tui_module_compiles():
    """Catch syntax/indent errors in tui.py immediately."""
    import src.tui
    assert src.tui.main is not None

def test_tui_main_execution():
    with patch('src.tui.ScraperApp') as mock_app, \
         patch('sys.argv', ['tui.py']), \
         patch('src.tui.load_config', return_value={"database": {"path": "test.db"}}):
        import src.tui
        src.tui.main()
        mock_app.return_value.run.assert_called_once()

def test_scraperapp_config_not_found():
    from src.tui import ScraperApp
    with patch('src.tui.load_config', side_effect=FileNotFoundError), \
         patch('src.tui.initialize_database') as mock_init, \
         patch('src.tui.ScraperApp._start_webserver'):
        app = ScraperApp("nonexistent.yaml")
        assert app.config["database"] == {"path": "workshop.db"}
        mock_init.assert_called_once_with("workshop.db")

def test_tui_main_logging_configured(tmp_path):
    config_file = tmp_path / "config.yaml"
    log_file = tmp_path / "tui.log"
    config_file.write_text(f"""logging:
  level: 'WARNING'
  file: '{log_file}'
""")

    with patch('sys.argv', ['tui.py', str(config_file)]), \
         patch('src.tui.ScraperApp') as mock_app, \
         patch('logging.basicConfig') as mock_basic_config, \
         patch('logging.FileHandler') as mock_file_handler:

        mock_fh_instance = MagicMock()
        mock_file_handler.return_value = mock_fh_instance

        import src.tui
        src.tui.main()

        mock_file_handler.assert_called_once_with(str(log_file))
        kwargs = mock_basic_config.call_args.kwargs
        assert kwargs["level"] == logging.WARNING
        assert mock_fh_instance in kwargs["handlers"]
        mock_app.assert_called_once_with(str(config_file))

def test_tui_main_no_logging(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""database:
  path: 'test.db'
""")

    with patch('sys.argv', ['tui.py', str(config_file)]), \
         patch('src.tui.ScraperApp') as mock_app, \
         patch('logging.basicConfig') as mock_basic_config, \
         patch('logging.getLogger') as mock_get_logger:

        import src.tui
        src.tui.main()

        mock_basic_config.assert_not_called()
        mock_get_logger.return_value.addHandler.assert_called_once()
        added_handler = mock_get_logger.return_value.addHandler.call_args[0][0]
        assert isinstance(added_handler, logging.NullHandler)
