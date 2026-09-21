"""Legacy config keys: the ones still read, and the ones retired.

`daemon.request_delay_seconds` and `daemon.capture_web_scrapes` are still
honoured -- the renames that produced `daemon.api_delay_seconds` and
`daemon.capture_web_downloads` keep the old spelling working, with a warning.

`daemon.batch_size` and `daemon.user_staleness_days` are **retired**. The rename
project is over and the operator's config has used only the current spellings
long enough that the old value must no longer influence anything: a config that
carries the old key alone leaves the current setting at its default. It is not
ignored silently, though -- the key is named in one warning so the operator
learns the spelling they wrote does nothing.
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


def test_the_legacy_key_is_honoured_and_warns(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, request_delay_seconds=3.0))
    assert daemon.api_delay == 3.0
    assert any("deprecated" in record.message for record in caplog.records)


def test_the_current_key_does_not_warn(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, api_delay_seconds=2.0))
    assert daemon.api_delay == 2.0
    assert not any("deprecated" in record.message for record in caplog.records)


def test_the_current_key_wins_when_both_are_present(db_path, caplog):
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, api_delay_seconds=2.0, request_delay_seconds=9.0))
    assert daemon.api_delay == 2.0
    assert not any("deprecated" in record.message for record in caplog.records)


# --- the renamed capture switch ---------------------------------------------
#
# `daemon.capture_web_scrapes` grew into `daemon.capture_web_downloads` when the
# switch came to cover every Steam community pull rather than the item page
# alone. The old name has to keep working, or an installation that was capturing
# would silently stop after an upgrade.

def test_the_legacy_capture_key_is_honoured_and_warns(db_path, tmp_path, caplog):
    from src import capture

    config = _config(db_path, outbox_dir=str(tmp_path / "outbox"),
                     capture_web_scrapes=True)
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(config)
    try:
        assert daemon.capture_web_downloads is True
        assert capture.web_download_capture_active() is True, "the old key still captures"
        assert any("deprecated" in record.message for record in caplog.records)
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
        assert not any("deprecated" in record.message for record in caplog.records)
    finally:
        capture.configure(None)


def test_the_current_capture_key_wins_when_both_are_present(db_path, tmp_path, caplog):
    from src import capture

    config = _config(db_path, outbox_dir=str(tmp_path / "outbox"),
                     capture_web_downloads=False, capture_web_scrapes=True)
    with caplog.at_level(logging.WARNING):
        daemon = Daemon(config)
    try:
        assert daemon.capture_web_downloads is False, "the current key is the one read"
        assert capture.web_download_capture_active() is False
        assert not any("deprecated" in record.message for record in caplog.records)
    finally:
        capture.configure(None)


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

