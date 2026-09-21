import logging
import os

import pytest
from unittest.mock import patch, MagicMock
import sys
from src.daemon_runner import main


@pytest.fixture(autouse=True)
def _isolate_the_pid_file(tmp_path, monkeypatch):
    """Point the runner's PID file into this test's tmp_path.

    ``main`` now takes the file with an exclusive create and refuses to start
    when one is already there, so tests sharing the repository's ``.daemon.pid``
    would refuse each other. ``raising=False`` keeps the fixture harmless while
    the guard does not exist yet, which is what lets the new tests fail against
    the old code for the right reason.
    """
    monkeypatch.setattr("src.daemon_runner.PID_FILE",
                        str(tmp_path / ".daemon.pid"), raising=False)


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


def test_main_installs_the_crash_hooks_before_reading_the_config():
    """The daemon's crash hooks go in before anything that can fail.

    A failure in `load_config` or in the logging setup below used to be dumped
    nowhere, because `crash.install` ran last. This pins the order: hooks,
    Windows console fix-up, then the config.
    """
    order = []

    def record_hooks(process_name):
        order.append(("hooks", process_name))

    def explode(config_path):
        order.append(("config", config_path))
        raise FileNotFoundError(config_path)

    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner._fix_windows_encoding',
               lambda: order.append(("encoding", None))), \
         patch('src.daemon_runner.crash.install_hooks', record_hooks), \
         patch('src.daemon_runner.load_config', explode), \
         patch('logging.basicConfig'):
        with pytest.raises(SystemExit) as caught:
            main()

    assert caught.value.code == 1
    assert order == [("hooks", "daemon"), ("encoding", None),
                     ("config", "config.yaml")]


def test_main_logging_daemon_with_file():
    """--daemon with log file: FileHandler + stderr (2 handlers)."""
    import logging
    with patch('sys.argv', ['daemon_runner.py', '--daemon']), \
         patch('src.daemon_runner._daemonize'), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.initialize_database'), \
         patch('src.daemon_runner.Daemon'), \
         patch('logging.basicConfig') as mock_basic_config, \
         patch('src.daemon_runner.log_rotation.log_file_handler') as mock_file_handler, \
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
         patch('src.daemon_runner.log_rotation.log_file_handler') as mock_file_handler, \
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


# ── the PID file is the guard, not just the record ────────────────────────────
#
# ``main`` used to overwrite ``.daemon.pid`` and then migrate, so a hand-started
# second daemon took the live daemon's PID file and applied migrations under it;
# the two then shared one file, and whichever exited first stopped the other.
# The file is now created with ``O_CREAT|O_EXCL`` before the logging
# reconfiguration and before ``initialize_database``, so an existing file --
# live or left by a crash -- refuses the start before the database is touched.
# See docs/threading.md and docs/cross-platform.md.

def test_a_start_refuses_an_existing_pid_file_and_touches_no_database(
        tmp_path, caplog):
    """The refusal precedes migrations: no database file is created at all.

    ``initialize_database`` is deliberately *not* mocked here. It creates the
    database on first use, so ``not db_path.exists()`` after the refused start is
    a direct assertion that the database was not touched, rather than a mock's
    word for it. The log line must name the file and the PID inside it, so an
    operator can tell a live daemon from a stale file.
    """
    pid_file = tmp_path / ".daemon.pid"
    pid_file.write_text("4321")
    db_path = tmp_path / "workshop.db"

    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.Daemon') as mock_daemon, \
         caplog.at_level(logging.ERROR):
        mock_load.return_value = {"database": {"path": str(db_path)}}
        with pytest.raises(SystemExit) as excinfo:
            main()

    assert excinfo.value.code != 0, "a refused start must exit non-zero"
    assert not db_path.exists(), "a refused start must not touch the database"
    mock_daemon.assert_not_called()
    assert str(pid_file) in caplog.text, "the log must name the PID file"
    assert "4321" in caplog.text, "the log must name the PID inside it"
    assert pid_file.read_text().strip() == "4321", (
        "the refused start must leave the existing PID file alone")


def test_a_start_refuses_an_unreadable_pid_file_and_says_so(tmp_path, caplog):
    """A file with no readable PID refuses too, and the log does not invent one."""
    pid_file = tmp_path / ".daemon.pid"
    pid_file.write_text("not-a-pid")

    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.Daemon'), \
         caplog.at_level(logging.ERROR):
        mock_load.return_value = {"database": {"path": str(tmp_path / "workshop.db")}}
        with pytest.raises(SystemExit):
            main()

    assert str(pid_file) in caplog.text
    assert "not-a-pid" not in caplog.text, "the file's contents are not a PID"
    assert "no PID could be read" in caplog.text


def test_a_second_start_over_an_existing_pid_file_is_refused(tmp_path):
    """Two sequential starts: the first wins, the second aborts and changes nothing.

    The first start leaves its PID file behind (its removal is the stop
    protocol's job, not the start's); the second must not overwrite it. The
    database is the real module here, so a broken guard would create it.
    """
    db_path = tmp_path / "workshop.db"
    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.Daemon') as mock_daemon:
        mock_load.return_value = {"database": {"path": str(db_path)}}
        main()  # the winner
        first_contents = (tmp_path / ".daemon.pid").read_text()
        with pytest.raises(SystemExit):
            main()  # the loser

    assert mock_daemon.call_count == 1, "only the winner may construct the daemon"
    assert (tmp_path / ".daemon.pid").read_text() == first_contents, (
        "the loser must not overwrite the winner's PID file")


def test_the_pid_file_create_is_exclusive_under_a_race(tmp_path):
    """Concurrent creators: exactly one wins, the rest get FileExistsError.

    This is the property ``O_CREAT|O_EXCL`` exists for, so it is pinned at the
    helper rather than left to be inferred from sequential starts.
    """
    from concurrent.futures import ThreadPoolExecutor
    from src.daemon_runner import _acquire_pid_file

    pid_file = tmp_path / ".daemon.pid"

    def attempt(_):
        try:
            fd = _acquire_pid_file(str(pid_file))
        except FileExistsError:
            return False
        os.close(fd)
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))

    assert outcomes.count(True) == 1, f"exactly one creator may win: {outcomes}"


def test_a_create_failure_other_than_already_exists_aborts_with_its_reason(
        tmp_path, caplog):
    """A missing parent directory is not "already running"; it still refuses.

    The outcome for the operator is the same -- it did not start -- so the
    message has the same shape, with the filesystem's own reason attached. The
    database must not be touched on this path either.
    """
    missing_parent = tmp_path / "no-such-dir" / ".daemon.pid"
    assert not missing_parent.parent.exists()
    db_path = tmp_path / "workshop.db"

    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.Daemon') as mock_daemon, \
         caplog.at_level(logging.ERROR):
        # Override the autouse fixture: force the create itself to fail without
        # that failure being "already exists".
        with patch('src.daemon_runner.PID_FILE', str(missing_parent)):
            mock_load.return_value = {"database": {"path": str(db_path)}}
            with pytest.raises(SystemExit) as excinfo:
                main()

    assert excinfo.value.code != 0
    assert not db_path.exists(), "a refused start must not touch the database"
    mock_daemon.assert_not_called()
    assert "cannot create PID file" in caplog.text
    assert str(missing_parent) in caplog.text


def test_the_ordinary_start_writes_the_pid_file(tmp_path):
    """The guard is also the record: a clean start leaves its own PID in the file."""
    with patch('sys.argv', ['daemon_runner.py']), \
         patch('src.daemon_runner.load_config') as mock_load, \
         patch('src.daemon_runner.initialize_database'), \
         patch('src.daemon_runner.Daemon'):
        mock_load.return_value = {"database": {"path": "test.db"}}
        main()

    assert (tmp_path / ".daemon.pid").read_text().strip() == str(os.getpid())
