"""The legacy delay key still works, but must say that it is legacy.

It was honoured silently, so a config using the old name had no signal that the
name had changed and would never be updated. `config.yaml.example` no longer
advertises it, which makes the silence worse rather than better.
"""

import logging

from src.daemon import Daemon


def _config(db_path, **daemon):
    return {"database": {"path": db_path}, "daemon": dict(daemon, target_appids=[1])}


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
