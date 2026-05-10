import pytest
from unittest.mock import patch, MagicMock
import sys
from src.daemon_runner import main

def test_main_custom_config():
    with patch('sys.argv', ['daemon_runner.py', 'custom.yaml']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.initialize_database'), \
         patch('src.daemon_runner.Daemon') as mock_daemon:
        
        mock_load.return_value = {"database": {"path": "test.db"}}
        main()
        mock_load.assert_called_once_with('custom.yaml')
        mock_daemon.return_value.run.assert_called_once()

def test_main_config_not_found():
    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner.load_config', side_effect=FileNotFoundError), \
         patch('sys.exit', side_effect=SystemExit(1)) as mock_exit:
        
        with pytest.raises(SystemExit):
            main()
        mock_exit.assert_called_once_with(1)


def test_main_daemon_flag_strips_from_args():
    """--daemon flag is stripped, config path still passed through."""
    with patch('sys.argv', ['daemon_runner.py', '--daemon', 'myconfig.yaml']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.initialize_database'), \
         patch('src.daemon_runner.Daemon') as mock_daemon:

        mock_load.return_value = {"database": {"path": "test.db"}}
        main()
        mock_load.assert_called_once_with('myconfig.yaml')


def test_main_logging_no_daemon_no_file():
    """No --daemon, no log file: stdout + stderr (2 handlers)."""
    import logging
    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.initialize_database'), \
         patch('src.daemon_runner.Daemon'), \
         patch('logging.basicConfig') as mock_basic_config, \
         patch('logging.StreamHandler') as mock_stream_handler:

        mock_load.return_value = {"database": {"path": "test.db"}}
        mock_stream_handler.return_value = MagicMock()

        main()

        kwargs = mock_basic_config.call_args.kwargs
        assert kwargs["level"] == logging.INFO
        assert len(kwargs["handlers"]) == 2


def test_main_logging_daemon_with_file():
    """--daemon with log file: FileHandler + stderr (2 handlers)."""
    import logging
    with patch('sys.argv', ['daemon_runner.py', '--daemon']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.initialize_database'), \
         patch('src.daemon_runner.Daemon'), \
         patch('logging.basicConfig') as mock_basic_config, \
         patch('logging.FileHandler') as mock_file_handler, \
         patch('logging.StreamHandler') as mock_stream_handler:

        mock_load.return_value = {
            "database": {"path": "test.db"},
            "logging": {"level": "WARNING", "file": "test_scraper.log"}
        }
        mock_file_handler.return_value = MagicMock()

        main()

        kwargs = mock_basic_config.call_args.kwargs
        assert kwargs["level"] == logging.WARNING
        assert len(kwargs["handlers"]) == 2


def test_main_logging_no_daemon_with_file():
    """No --daemon with log file: FileHandler + stdout + stderr (3 handlers)."""
    import logging
    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.initialize_database'), \
         patch('src.daemon_runner.Daemon'), \
         patch('logging.basicConfig') as mock_basic_config, \
         patch('logging.FileHandler') as mock_file_handler, \
         patch('logging.StreamHandler') as mock_stream_handler:

        mock_load.return_value = {
            "database": {"path": "test.db"},
            "logging": {"level": "DEBUG", "file": "fg_scraper.log"}
        }
        mock_file_handler.return_value = MagicMock()

        main()

        kwargs = mock_basic_config.call_args.kwargs
        assert kwargs["level"] == logging.DEBUG
        assert len(kwargs["handlers"]) == 3
