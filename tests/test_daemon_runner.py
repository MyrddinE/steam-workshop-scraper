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


def test_main_tells_the_daemon_a_pid_file_is_expected():
    """The runner wrote the file, so a stop before the first check must count.

    Passing this explicitly is what closes the race where the controller deletes
    the file during config load or migrations: the daemon then already treats its
    absence as the stop, instead of waiting to observe a file that is gone.
    """
    with patch('sys.argv', ['daemon_runner.py', 'custom.yaml']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.initialize_database'), \
         patch('src.daemon_runner.Daemon') as mock_daemon:

        mock_load.return_value = {"database": {"path": "test.db"}}
        main()

        assert mock_daemon.call_args.kwargs.get("expect_pid_file") is True

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
         patch('src.daemon_runner._daemonize'), \
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
         patch('src.daemon_runner._daemonize'), \
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


# ── the log file must be written in UTF-8 ─────────────────────────────────────
#
# The handler used the platform default, which on Windows is cp1252. That
# corrupted every non-ASCII character once the file was read back as UTF-8 -- the
# em dash in the "enriching" marker became a single 0x97 byte, which the reader
# turns into the replacement character -- and silently dropped any record cp1252
# cannot represent at all, which is every log line naming a Japanese or Chinese
# item. Both failures are invisible from inside the process, so they are pinned
# here rather than left to a future reader to notice.

def test_log_file_handler_pins_utf8():
    from src.daemon_runner import _log_file_handler

    handler = _log_file_handler("unused-for-this-assertion.log")
    try:
        assert handler.encoding.lower().replace("-", "") == "utf8", (
            "the log file must not be written in the platform default encoding"
        )
    finally:
        handler.close()


def test_a_cjk_record_survives_the_log_file(tmp_path):
    """The record a cp1252 file would have dropped is written and reads back."""
    import logging
    from src.daemon_runner import _log_file_handler

    log_path = tmp_path / "scraper.log"
    handler = _log_file_handler(str(log_path))
    logger = logging.getLogger("test_cjk_survives")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # The failure mode is silent, so silence the handler's own error reporting
    # the way the daemon does and assert on the bytes that actually landed.
    previous = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        # The marker is the sample the reader must survive: this is the daemon's
        # own discovery line, with its em dash and its green SGR escape.
        logger.info('[A:2063560223] "鸣潮-爱弥丝" — \033[32menriching\033[0m')
    finally:
        logging.raiseExceptions = previous
        logger.removeHandler(handler)
        handler.close()

    text = log_path.read_text(encoding="utf-8")
    assert "鸣潮-爱弥丝" in text, "a CJK title must survive the log file"
    assert "—" in text, "the em dash must survive as an em dash"
    assert "\ufffd" not in text, "nothing may be replaced on the way through"
