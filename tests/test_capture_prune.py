"""Debug captures age out; failure evidence does not.

A debug capture is a session instrument: the switch is on for a session or two,
and a capture nobody has looked at inside a week is not going to be looked at.
Housekeeping therefore prunes ``web_downloads/``, ``image_downloads/`` and the
legacy ``scrapes/`` tree once a file is seven days old. ``failures/`` and
``crashes/`` are the evidence a regression test is built from and are removed
when they are pulled for review, never by age; ``db/`` is the backup thread's
business.

Pruning also drops the file's manifest entry, because the puller transfers one
file per entry: an entry left behind would make the next pull fail on a path
that no longer exists.
"""

import json
import os
from datetime import datetime, timezone

from src import capture
from src.backup import update_manifest

# The documented retention, pinned here so a test can fail on the value rather
# than borrowing it from the code it checks.
RETENTION_DAYS = 7


def test_the_retention_is_a_named_seven_day_constant():
    assert capture.DEBUG_CAPTURE_RETENTION_DAYS == RETENTION_DAYS


def _write(outbox, rel, *, age_days, register=True):
    path = os.path.join(outbox, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"capture")
    when = datetime.now(timezone.utc).timestamp() - age_days * 86400
    os.utime(path, (when, when))
    if register:
        update_manifest(outbox, {"path": rel, "kind": "web_download", "bytes": 7})
    return path


def _manifest_paths(outbox):
    with open(os.path.join(outbox, "manifest.json"), encoding="utf-8") as handle:
        return {entry["path"] for entry in json.load(handle)["artifacts"]}


def test_a_debug_capture_older_than_the_retention_is_pruned(tmp_path):
    outbox = str(tmp_path)
    path = _write(outbox, "web_downloads/old-item_page-1.json", age_days=RETENTION_DAYS + 1)

    removed = capture.prune_debug_captures(outbox)

    assert removed == ["web_downloads/old-item_page-1.json"]
    assert not os.path.exists(path)


def test_a_debug_capture_inside_the_window_stays(tmp_path):
    outbox = str(tmp_path)
    path = _write(outbox, "web_downloads/recent-item_page-1.json", age_days=1)

    removed = capture.prune_debug_captures(outbox)

    assert removed == []
    assert os.path.exists(path)


def test_an_image_download_capture_is_pruned_like_a_web_capture(tmp_path):
    outbox = str(tmp_path)
    path = _write(outbox, "image_downloads/old-image-1.json", age_days=RETENTION_DAYS + 2)

    assert capture.prune_debug_captures(outbox) == ["image_downloads/old-image-1.json"]
    assert not os.path.exists(path)


def test_the_legacy_scrapes_tree_is_pruned_too(tmp_path):
    """An outbox from a build before the rename still holds this tree."""
    outbox = str(tmp_path)
    path = _write(outbox, "scrapes/old-scrape-1.json", age_days=RETENTION_DAYS + 2)

    assert capture.prune_debug_captures(outbox) == ["scrapes/old-scrape-1.json"]
    assert not os.path.exists(path)


def test_a_web_ui_trace_is_pruned_like_the_other_debug_captures(tmp_path):
    """The trace is an instrument, not evidence: it ages out with the rest."""
    outbox = str(tmp_path)
    path = _write(outbox, "web_ui_trace/old-page-1.json", age_days=RETENTION_DAYS + 2)

    assert "web_ui_trace" in [os.path.basename(d) for d in capture.debug_capture_dirs(outbox)]
    assert capture.prune_debug_captures(outbox) == ["web_ui_trace/old-page-1.json"]
    assert not os.path.exists(path)


def test_a_failure_capture_of_any_age_stays(tmp_path):
    outbox = str(tmp_path)
    body = _write(outbox, "failures/web_unknown--x/abc-1.body", age_days=90)
    record = _write(outbox, "failures/web_unknown--x/abc-1.json", age_days=90)

    removed = capture.prune_debug_captures(outbox)

    assert removed == []
    assert os.path.exists(body)
    assert os.path.exists(record)


def test_a_crash_dump_of_any_age_stays(tmp_path):
    outbox = str(tmp_path)
    path = _write(outbox, "crashes/old-daemon-error1.txt", age_days=90)

    assert capture.prune_debug_captures(outbox) == []
    assert os.path.exists(path)


def test_the_database_snapshot_is_never_touched(tmp_path):
    outbox = str(tmp_path)
    path = _write(outbox, "db/workshop-backup.db", age_days=30, register=False)

    assert capture.prune_debug_captures(outbox) == []
    assert os.path.exists(path)


def test_a_pruned_capture_loses_its_manifest_entry(tmp_path):
    outbox = str(tmp_path)
    _write(outbox, "web_downloads/old-item_page-1.json", age_days=RETENTION_DAYS + 1)
    _write(outbox, "web_downloads/old-item_page-1.body", age_days=RETENTION_DAYS + 1)
    _write(outbox, "web_downloads/recent-item_page-2.json", age_days=1)
    update_manifest(outbox, {"path": "db/workshop-backup.db", "kind": "db", "bytes": 1})

    capture.prune_debug_captures(outbox)

    remaining = _manifest_paths(outbox)
    assert "web_downloads/old-item_page-1.json" not in remaining
    assert "web_downloads/old-item_page-1.body" not in remaining
    assert "web_downloads/recent-item_page-2.json" in remaining
    assert "db/workshop-backup.db" in remaining


def test_prune_with_no_outbox_is_a_no_op():
    capture.configure(None)
    assert capture.prune_debug_captures() == []


def test_prune_uses_the_configured_outbox_by_default(tmp_path):
    outbox = str(tmp_path)
    _write(outbox, "web_downloads/old-item_page-1.json", age_days=RETENTION_DAYS + 1)
    capture.configure(outbox)
    try:
        assert capture.prune_debug_captures() == ["web_downloads/old-item_page-1.json"]
    finally:
        capture.configure(None)


def test_a_capture_that_cannot_be_removed_does_not_raise(tmp_path, monkeypatch):
    outbox = str(tmp_path)
    path = _write(outbox, "web_downloads/old-item_page-1.json", age_days=RETENTION_DAYS + 1)

    def refuse(_path):
        raise OSError("locked")

    monkeypatch.setattr(capture.os, "remove", refuse)
    assert capture.prune_debug_captures(outbox) == []
    assert os.path.exists(path), "a refused delete leaves the file"

    # The entry is dropped first, so a file that survives a refused delete is a
    # stray file the puller ignores -- never an entry chasing a missing path.
    assert "web_downloads/old-item_page-1.json" not in _manifest_paths(outbox)
