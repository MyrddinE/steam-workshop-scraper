"""Manual log rotation: the archive, the size readout, and the reopen guarantee.

Two processes hold the daemon log open -- the daemon's own ``FileHandler``
(``src/daemon_runner.py``) and the TUI's (``src/tui.py``) -- so renaming the log
is not enough on its own: a handler that still holds the old descriptor would
keep writing into the renamed inode and, once it was compressed, those records
would be gone from the live log silently. Every writer has to notice and reopen.
The mechanism this file pins is the **generation marker**: the rotator writes the
archive's name to ``<log>.generation`` beside the log, and every rotation-aware
handler caches what it last saw and reopens when it changes. That is the
cross-platform choice -- ``WatchedFileHandler`` does not reopen on Windows, which
is where this runs -- and the per-record cost is one ``os.stat`` on the marker,
not a read of the log.
"""

import gzip
import logging
import os
import threading

import pytest

from src import log_rotation
from src.daemon_runner import _log_file_handler


def _logger(name, handler):
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _lines(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


# ── the rotation itself ──────────────────────────────────────────────────────

def test_a_rotation_archives_the_previous_content_and_leaves_a_fresh_log(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("first line\nsecond line\n", encoding="utf-8")

    result = log_rotation.rotate_log(str(log))
    assert result["ok"] and result["started"], result
    assert log_rotation.wait_for_rotation(str(log), timeout=10)

    archive = result["archive"]
    # Beside the log, under a `logs` subfolder, named for the log and a UTC stamp.
    assert os.path.dirname(archive) == str(tmp_path / "logs")
    assert os.path.basename(archive).startswith("daemon-")
    assert archive.endswith(".log.gz")
    with gzip.open(archive, "rt", encoding="utf-8") as fh:
        assert fh.read() == "first line\nsecond line\n"

    # The live log is at the same path and empty: the rename happened up front.
    assert log.read_text(encoding="utf-8") == ""

    final = log_rotation.rotation_status(str(log))["result"]
    assert final["ok"] and final["archive"] == archive
    assert final["archive_size"] == os.path.getsize(archive)


def test_the_reopen_guarantee_a_handler_that_held_the_file_writes_to_the_new_one(tmp_path):
    """The crux: after a rotation every writer writes to the fresh file.

    This is the property the whole design exists for. The handler below is built
    by the daemon's own factory and holds the old descriptor across the rotation;
    its next record must land in the new live log, and must not appear in the
    sealed archive.
    """
    log = tmp_path / "daemon.log"
    handler = _log_file_handler(str(log))
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = _logger("test_rotation_reopen_guarantee", handler)
    try:
        logger.info("before-rotation")
        handler.flush()

        result = log_rotation.rotate_log(str(log))
        assert result["started"] is True, result
        assert log_rotation.wait_for_rotation(str(log), timeout=10)
        archive = result["archive"]

        # The same handler object, still alive from before the rotation.
        logger.info("after-rotation")
        handler.flush()
    finally:
        handler.close()

    live = log.read_text(encoding="utf-8")
    assert "after-rotation" in live, "the held-open handler did not reopen"
    assert "before-rotation" not in live, "the old content must have moved out"

    with gzip.open(archive, "rt", encoding="utf-8") as fh:
        archived = fh.read()
    assert "before-rotation" in archived
    assert "after-rotation" not in archived, \
        "a post-rotation record was written into the sealed archive"


def test_the_reopen_mechanism_is_the_generation_marker(tmp_path):
    """Pin the mechanism itself: the marker is written and drives the reopen.

    A marker-only change (no rename) is not enough to move the file, but it is
    enough to make the handler drop its descriptor and reopen the path -- which
    is what the marker is for. This fails if the handler is reverted to a plain
    ``FileHandler``.
    """
    log = tmp_path / "daemon.log"
    marker = str(log) + log_rotation.GENERATION_SUFFIX
    handler = log_rotation.RotationAwareFileHandler(str(log), encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = _logger("test_rotation_generation_marker", handler)
    try:
        logger.info("one")
        handler.flush()

        # Simulate a rotation's rename+recreate, then publish a new generation.
        os.replace(log, tmp_path / "old.log")
        log.write_text("", encoding="utf-8")
        log_rotation.write_generation(str(log), "daemon-20260921-143005.log.gz")

        assert os.path.exists(marker)
        logger.info("two")
        handler.flush()
    finally:
        handler.close()

    assert _lines(log) == ["two"], "the handler kept writing to the old inode"
    assert log_rotation.read_generation(str(log)) == "daemon-20260921-143005.log.gz"


def test_rotating_a_missing_log_is_a_noop_with_an_honest_message(tmp_path):
    result = log_rotation.rotate_log(str(tmp_path / "never-written.log"))
    assert result["ok"] is True
    assert result["started"] is False
    assert "does not exist" in result["message"]
    assert not (tmp_path / "logs").exists()


def test_rotating_an_empty_log_is_a_noop(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("", encoding="utf-8")
    result = log_rotation.rotate_log(str(log))
    assert result["ok"] is True
    assert result["started"] is False
    assert "empty" in result["message"]


def test_rotating_twice_in_a_row(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("content\n", encoding="utf-8")
    first = log_rotation.rotate_log(str(log))
    assert log_rotation.wait_for_rotation(str(log), timeout=10)
    # Nothing was logged in between, so the second press has nothing to rotate.
    second = log_rotation.rotate_log(str(log))
    assert second["ok"] is True and second["started"] is False
    assert len(list((tmp_path / "logs").glob("*.log.gz"))) == 1

    # And after a record, a second rotation produces a second archive.
    log.write_text("more\n", encoding="utf-8")
    third = log_rotation.rotate_log(str(log))
    assert third["started"] is True
    assert log_rotation.wait_for_rotation(str(log), timeout=10)
    assert len(list((tmp_path / "logs").glob("*.log.gz"))) == 2
    assert first["archive"] != third["archive"]


def test_a_second_press_while_one_is_compressing_is_refused(tmp_path, monkeypatch):
    """Concurrent presses must not produce two archives of the same content."""
    log = tmp_path / "daemon.log"
    log.write_text("content\n", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    real = log_rotation._gzip_into

    def slow(src, dst):
        started.set()
        release.wait(10)
        return real(src, dst)

    monkeypatch.setattr(log_rotation, "_gzip_into", slow)
    try:
        first = log_rotation.rotate_log(str(log))
        assert first["started"] is True
        assert started.wait(5), "the compression thread never started"

        second = log_rotation.rotate_log(str(log))
        assert second["ok"] is False
        assert "already in progress" in second["message"]
    finally:
        release.set()
        log_rotation.wait_for_rotation(str(log), timeout=10)
    assert len(list((tmp_path / "logs").glob("*.log.gz"))) == 1


def test_a_rename_failure_is_reported_not_raised(tmp_path):
    # The archive folder's path is occupied by a file, so the rename cannot work.
    log = tmp_path / "daemon.log"
    log.write_text("content\n", encoding="utf-8")
    (tmp_path / "logs").write_text("not a directory\n", encoding="utf-8")
    result = log_rotation.rotate_log(str(log))
    assert result["ok"] is False
    assert "Rotation failed" in result["message"]
    # The bytes are still where they were: nothing was moved and then lost.
    assert log.read_text(encoding="utf-8") == "content\n"


# ── the size readout ─────────────────────────────────────────────────────────

def test_the_readout_reports_the_live_size_and_updates_after_rotation(tmp_path):
    log = tmp_path / "daemon.log"
    log.write_text("x" * 2048, encoding="utf-8")

    before = log_rotation.log_status(str(log))
    assert before["log_size"] == 2048
    assert before["log_readout"] == "Log size: 2.0 KB"
    assert before["rotating"] is False

    assert log_rotation.rotate_log(str(log))["started"] is True
    assert log_rotation.wait_for_rotation(str(log), timeout=10)

    after = log_rotation.log_status(str(log))
    assert after["log_size"] == 0
    assert after["log_readout"] == "Log size: 0 B"
    assert "Rotated:" in after["rotation_message"]
    assert after["rotation_ok"] is True


def test_the_readout_is_honest_when_there_is_no_log(tmp_path):
    status = log_rotation.log_status(str(tmp_path / "missing.log"))
    assert status["log_readout"] == "Log size: no log file"
    assert status["log_size"] is None

    assert log_rotation.log_status(None)["log_readout"] == "Log size: not configured"


def test_the_generation_marker_falls_back_to_an_in_place_write(tmp_path, monkeypatch):
    """On Windows a reader can hold the marker open and refuse the replace.

    `write_generation` retries, then rewrites in place; the value only has to
    differ from the previous one, so a non-atomic write is still enough to make
    the handlers reopen.
    """
    log = tmp_path / "daemon.log"

    def refuse(source, target):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(log_rotation.os, "replace", refuse)
    log_rotation.write_generation(str(log), "daemon-x.log.gz")
    assert log_rotation.read_generation(str(log)) == "daemon-x.log.gz"


def test_the_handler_opens_the_log_with_delete_sharing_on_windows():
    """Pins the Windows half of the rename, which no test here can execute.

    Python's `io.open()` requests only read/write sharing, so a file a handler
    holds cannot be renamed by another process and `os.replace` would fail for as
    long as the daemon or the TUI runs -- the rotation would never work on the
    platform it is for. The handler therefore has its own opener, and it is the
    one that asks for FILE_SHARE_DELETE. This is a source guard on purpose: a
    revert to a plain `logging.FileHandler` is invisible on Linux.
    """
    import inspect

    source = inspect.getsource(log_rotation.RotationAwareFileHandler._open)
    assert "_windows_append_stream" in source
    assert "win32" in source
    windows_source = inspect.getsource(log_rotation._windows_fd)
    assert "_WINDOWS_SHARE_DELETE" in windows_source


def test_a_compression_failure_keeps_the_log_and_reports_where(tmp_path, monkeypatch):
    log = tmp_path / "daemon.log"
    log.write_text("content\n", encoding="utf-8")

    def explode(source, destination):
        # A half-written gzip must not be mistaken for a good archive.
        with open(destination, "wb") as fh:
            fh.write(b"not really gzip")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(log_rotation, "_gzip_into", explode)
    result = log_rotation.rotate_log(str(log))
    assert result["started"] is True
    assert log_rotation.wait_for_rotation(str(log), timeout=10)

    final = log_rotation.rotation_status(str(log))["result"]
    assert final["ok"] is False
    assert "No space left on device" in final["message"]
    assert ".log" in final["message"]
    raw = list((tmp_path / "logs").glob("*.log"))
    assert len(raw) == 1, "the renamed log must survive a failed compression"
    assert raw[0].read_text(encoding="utf-8") == "content\n"
    assert list((tmp_path / "logs").glob("*.log.gz")) == [], \
        "the partial archive must be removed"


def test_format_size():
    assert log_rotation.format_size(0) == "0 B"
    assert log_rotation.format_size(1023) == "1023 B"
    assert log_rotation.format_size(1024) == "1.0 KB"
    assert log_rotation.format_size(594 * 1024 * 1024) == "594.0 MB"
    assert log_rotation.format_size(2 * 1024 ** 3) == "2.0 GB"
