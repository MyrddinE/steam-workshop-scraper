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

