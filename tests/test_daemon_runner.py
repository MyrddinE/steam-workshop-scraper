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


def test_main_logging_with_file():
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
            "logging": {"level": "WARNING", "file": "test_scraper.log"}
        }
        mock_fh_instance = MagicMock()
        mock_file_handler.return_value = mock_fh_instance

        main()

        mock_file_handler.assert_called_once_with("test_scraper.log")
        mock_stream_handler.assert_not_called()
        kwargs = mock_basic_config.call_args.kwargs
        assert kwargs["level"] == logging.WARNING
        assert mock_fh_instance in kwargs["handlers"]
        assert len(kwargs["handlers"]) == 1


def test_main_logging_no_file():
    import logging
    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.initialize_database'), \
         patch('src.daemon_runner.Daemon'), \
         patch('logging.basicConfig') as mock_basic_config, \
         patch('logging.StreamHandler') as mock_stream_handler:

        mock_load.return_value = {"database": {"path": "test.db"}}
        mock_sh_instance = MagicMock()
        mock_stream_handler.return_value = mock_sh_instance

        main()

        mock_stream_handler.assert_called_once()
        kwargs = mock_basic_config.call_args.kwargs
        assert kwargs["level"] == logging.INFO
        assert mock_sh_instance in kwargs["handlers"]
        assert len(kwargs["handlers"]) == 1
