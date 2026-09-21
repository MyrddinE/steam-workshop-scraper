"""Legacy config keys: the ones still read, and the ones retired.

Most renamed keys are **retired**: the rename project is over and the live log
shows the deprecated warning has not fired in a long time, so the old value must
no longer influence anything. A config that carries the old key alone leaves the
current setting at its default, and is not ignored silently -- the key is named
in one warning so the operator learns the spelling they wrote does nothing.

`daemon.backup_dir` is the one the owner's rule settles the other way: it was
honoured with no warning at all, so it gets a warning and is **retained** -- an
outbox path that still works must not be dropped out from under an operator.
"""

import logging

from src.daemon import Daemon


def _config(db_path, **daemon):
    return {"database": {"path": db_path}, "daemon": dict(daemon, target_appids=[1])}


def _retired_warnings(caplog, legacy_key):
    """The warning lines that name `legacy_key` as a key that is no longer read."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "no longer used" in record.getMessage()
        and legacy_key in record.getMessage()
    ]


def _still_honoured_warnings(caplog, legacy_key):
    """The warning lines that name `legacy_key` as deprecated but still read."""
    return [
        record.getMessage()
        for record in caplog.records
        if "deprecated and still honoured" in record.getMessage()
        and legacy_key in record.getMessage()
    ]


# --- the retired delay keys -------------------------------------------------
#
# `daemon.request_delay_seconds` was the original name of the per-request pause,
# renamed once the delay was understood as an API rate control. Both spellings
# are now retired outright: the delay is daemon state (`.daemon_state.yaml`), not
# configuration, so there is no current key to rename to and the warning says the
# value is no longer read.

def test_the_retired_delay_key_is_not_honoured_and_warns(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, request_delay_seconds=3.0))
    assert daemon.api_delay == 1.5, "the retired value must not be read"
    warnings = _retired_warnings(caplog, "daemon.request_delay_seconds")
    assert len(warnings) == 1
    assert "no longer read" in warnings[0]
    assert "renamed to" not in warnings[0], "there is no current key to move it to"


def test_the_removed_delay_key_is_not_honoured_and_warns(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, api_delay_seconds=2.0))
    assert daemon.api_delay == 1.5, "the removed value must not be read"
    warnings = _retired_warnings(caplog, "daemon.api_delay_seconds")
    assert len(warnings) == 1
    assert "no longer read" in warnings[0]
    assert "renamed to" not in warnings[0]


def test_both_delay_spellings_are_retired_and_warn(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, api_delay_seconds=2.0,
                                request_delay_seconds=9.0))
    assert daemon.api_delay == 1.5, "neither dead spelling is read"
    assert len(_retired_warnings(caplog, "daemon.request_delay_seconds")) == 1
    assert len(_retired_warnings(caplog, "daemon.api_delay_seconds")) == 1


def test_the_web_and_image_delay_keys_are_retired_and_warn(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        Daemon(_config(db_path, web_delay_seconds=3.0, image_delay_seconds=4.0))
    assert len(_retired_warnings(caplog, "daemon.web_delay_seconds")) == 1
    assert len(_retired_warnings(caplog, "daemon.image_delay_seconds")) == 1


# --- the retired capture switch ---------------------------------------------
#
# `daemon.capture_web_scrapes` grew into `daemon.capture_web_downloads` when the
# switch came to cover every Steam community pull rather than the item page
# alone. The live log never shows the deprecated warning, so the old value is
# retired: a config that carries only it captures nothing.

def test_the_retired_capture_key_is_not_honoured_and_warns(db_path, tmp_path, caplog):
    from src import capture

    config = _config(db_path, outbox_dir=str(tmp_path / "outbox"),
                     capture_web_scrapes=True)
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(config)
    try:
        assert daemon.capture_web_downloads is False, "the retired value must not be read"
        assert capture.web_download_capture_active() is False
        warnings = _retired_warnings(caplog, "daemon.capture_web_scrapes")
        assert len(warnings) == 1
        assert "daemon.capture_web_downloads" in warnings[0]
    finally:
        capture.configure(None)


def test_the_current_capture_key_works_and_does_not_warn(db_path, tmp_path, caplog):
    from src import capture

    config = _config(db_path, outbox_dir=str(tmp_path / "outbox"),
                     capture_web_downloads=True)
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(config)
    try:
        assert daemon.capture_web_downloads is True
        assert capture.web_download_capture_active() is True
        assert not _retired_warnings(caplog, "daemon.capture_web_scrapes")
    finally:
        capture.configure(None)


def test_the_current_capture_key_wins_and_the_retired_key_still_warns(db_path, tmp_path, caplog):
    from src import capture

    config = _config(db_path, outbox_dir=str(tmp_path / "outbox"),
                     capture_web_downloads=False, capture_web_scrapes=True)
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(config)
    try:
        assert daemon.capture_web_downloads is False, "the current key is the one read"
        assert capture.web_download_capture_active() is False
        assert len(_retired_warnings(caplog, "daemon.capture_web_scrapes")) == 1
    finally:
        capture.configure(None)


# --- the still-honoured backup directory ------------------------------------
#
# `daemon.backup_dir` was renamed `daemon.outbox_dir` when the outbox came to
# hold more than database backups. Three processes read the same location and
# none of them ever warned, so the alias keeps working and now says so; it is
# retained until the log shows it unused.

def test_the_backup_dir_alias_is_still_honoured_and_warns(db_path, tmp_path, caplog):
    outbox = str(tmp_path / "outbox")
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, backup_dir=outbox))
    assert daemon.outbox_dir == outbox, "the alias still supplies the outbox"
    warnings = _still_honoured_warnings(caplog, "daemon.backup_dir")
    assert len(warnings) == 1
    assert "daemon.outbox_dir" in warnings[0]


def test_the_current_outbox_key_wins_and_the_backup_alias_still_warns(db_path, tmp_path, caplog):
    current = str(tmp_path / "current")
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, outbox_dir=current,
                                backup_dir=str(tmp_path / "old")))
    assert daemon.outbox_dir == current, "the current key is the one read"
    assert len(_still_honoured_warnings(caplog, "daemon.backup_dir")) == 1


# --- the retired batch knob -------------------------------------------------
#
# `daemon.batch_size` said "batch" while meaning items per API fetch, beside the
# translator's own `openai.batch`; it took the qualified name. The old value is
# no longer read: a config that carries only it gets the default, and one
# warning says the key is dead.

def test_the_retired_daemon_batch_key_is_not_honoured_and_warns(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, batch_size=7))
    assert daemon.api_batch_size == 10, "the retired value must not be read"
    warnings = _retired_warnings(caplog, "daemon.batch_size")
    assert len(warnings) == 1
    assert "daemon.api_batch_size" in warnings[0]


def test_the_current_daemon_batch_key_works_and_does_not_warn(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, api_batch_size=7))
    assert daemon.api_batch_size == 7
    assert not _retired_warnings(caplog, "daemon.batch_size")


def test_the_current_daemon_batch_key_wins_and_the_retired_key_still_warns(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, api_batch_size=3, batch_size=9))
    assert daemon.api_batch_size == 3, "the current key is the one read"
    assert len(_retired_warnings(caplog, "daemon.batch_size")) == 1


# --- the retired creator staleness window -----------------------------------
#
# `daemon.user_staleness_days` named the Steam creator with the wrong entity
# word, beside `daemon.item_staleness_days`, which deliberately keeps its name.
# Unlike `item_staleness_days`, the old creator spelling is retired.

def test_the_retired_creator_staleness_key_is_not_honoured_and_warns(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, user_staleness_days=45))
    assert daemon.creator_staleness_days == 90, "the retired value must not be read"
    warnings = _retired_warnings(caplog, "daemon.user_staleness_days")
    assert len(warnings) == 1
    assert "daemon.creator_staleness_days" in warnings[0]


def test_the_current_creator_staleness_key_works_and_does_not_warn(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, creator_staleness_days=45))
    assert daemon.creator_staleness_days == 45
    assert not _retired_warnings(caplog, "daemon.user_staleness_days")


def test_the_current_creator_staleness_key_wins_and_the_retired_key_still_warns(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, creator_staleness_days=45,
                                user_staleness_days=90))
    assert daemon.creator_staleness_days == 45, "the current key is the one read"
    assert len(_retired_warnings(caplog, "daemon.user_staleness_days")) == 1


# --- one warning per key per process ----------------------------------------
#
# The daemon log is never rotated, and a key can be read on every scrape or
# subscribe, so a repeated warning is unbounded noise. The helpers remember the
# `(section, legacy)` keys they have already named.

def test_a_retired_key_warning_fires_once_per_process(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        Daemon(_config(db_path, batch_size=7))
        Daemon(_config(db_path, batch_size=7))
    assert len(_retired_warnings(caplog, "daemon.batch_size")) == 1


def test_the_backup_dir_warning_fires_once_across_the_readers(db_path, tmp_path, caplog):
    from src.config import configured_outbox_dir

    outbox = str(tmp_path / "outbox")
    with caplog.at_level(logging.WARNING):
        Daemon(_config(db_path, backup_dir=outbox))
        configured_outbox_dir({"backup_dir": outbox})  # the web/crash readers
        configured_outbox_dir({"backup_dir": outbox})
    assert len(_still_honoured_warnings(caplog, "daemon.backup_dir")) == 1

