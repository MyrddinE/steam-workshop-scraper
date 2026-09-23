"""`.pauselock` carries its owner: scoped release, legacy tolerance, reclaim.

The lock used to be an empty file whose *existence* meant "paused", and any
writer could remove it. It is now a small JSON record -- ``owner``, ``pid``,
``acquired_at``, ``source`` -- written atomically, so:

* ``end_pause`` removes the file and closes the interval only for the caller
  that owns it (a legacy, unnamed lock stays releasable by anyone);
* ``begin_pause`` never steals a held lock, and self-heals one whose recorded
  pid is known dead;
* ``reclaim_if_owner_gone`` is the same self-heal the two worker polls run, so a
  holder that died does not stop the daemon's web and image work for good.

The tests here are the mechanism's own; the browser-side ownership is driven
through the node drivers in ``tests/test_subscribe_throttle.py``.
"""

import json
import logging
import os
from pathlib import Path

import pytest

from src import activity
from src.daemon_state import StateStore, state_path_for
import src.daemon_control


def _record(lock_path) -> dict:
    return json.loads(Path(lock_path).read_text(encoding="utf-8"))


def _pause_section(db_path):
    return StateStore(state_path_for(db_path)).load().get(activity.PAUSE_SECTION)


def _closed(db_path):
    section = _pause_section(db_path) or {}
    return section.get("closed_intervals") or []


def _open(db_path):
    section = _pause_section(db_path) or {}
    return section.get("open_interval")


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------


def test_acquiring_the_lock_writes_a_record_naming_its_owner(db_path, tmp_path):
    lock = tmp_path / ".pauselock"

    opened = activity.begin_pause(str(lock), db_path, source="tui",
                                  owner="tui-screen:7", now=1000)

    assert opened is True, "the absent -> present edge opens one interval"
    record = _record(lock)
    assert record["owner"] == "tui-screen:7"
    assert record["pid"] == os.getpid()
    assert record["source"] == "tui"
    assert record["acquired_at"] == 1000
    assert activity.lock_owner(str(lock))["owner"] == "tui-screen:7"


def test_a_missing_file_reads_as_no_record(tmp_path):
    assert activity.lock_owner(str(tmp_path / "absent")) is None


def test_an_unparseable_file_reads_as_no_record(tmp_path):
    lock = tmp_path / ".pauselock"
    lock.write_text("not json at all", encoding="utf-8")
    assert activity.lock_owner(str(lock)) is None


def test_the_record_is_written_atomically(tmp_path):
    """A reader must never see a half-written record: temp file plus replace."""
    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), None, owner="a")
    # The record parses as a whole, and the temp file that carried it is gone.
    assert _record(lock)["owner"] == "a"
    assert list(tmp_path.glob(".pauselock.*")) == [], \
        "the atomic write must not leave its temp file behind"


def test_a_refused_write_is_reported_not_raised(tmp_path, monkeypatch):
    """A filesystem that will not take the lock is logged, never fatal."""
    lock = tmp_path / ".pauselock"

    def boom(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(activity.tempfile, "mkstemp", boom)

    assert activity.begin_pause(str(lock), None, owner="a") is False
    assert not lock.exists()


# --------------------------------------------------------------------------
# release is scoped to the owner
# --------------------------------------------------------------------------


def test_a_non_owner_release_leaves_the_lock_and_its_interval_alone(db_path, tmp_path):
    """The defect this whole change fixes: removal used to be unconditional."""
    lock = tmp_path / ".pauselock"
    assert activity.begin_pause(str(lock), db_path, owner="screen", now=1000) is True
    assert _open(db_path)["at"] == 1000

    assert activity.end_pause(str(lock), db_path, owner="engine", now=1010) is False, \
        "a caller that does not own the lock must not release it"

    assert lock.exists(), "the file is still the pause"
    assert _record(lock)["owner"] == "screen", "the owner is unchanged"
    assert _open(db_path) is not None, "the interval belongs to the holder too"
    assert _closed(db_path) == [], "a refused release closes nothing"

    assert activity.end_pause(str(lock), db_path, owner="screen", now=1020) is True
    assert not lock.exists()
    assert _open(db_path) is None
    assert _closed(db_path) == [[1000, 1020]], \
        "the holder's own release closes the interval exactly once"


def test_a_nested_begin_neither_steals_nor_reopens_the_interval(db_path, tmp_path):
    """The TUI screen holds the lock while the engine re-enters it."""
    lock = tmp_path / ".pauselock"
    assert activity.begin_pause(str(lock), db_path, source="tui",
                                owner="screen", now=1000) is True

    assert activity.begin_pause(str(lock), db_path, source="engine",
                                owner="engine", now=1005) is False, \
        "a held lock is not stolen"
    assert _record(lock)["owner"] == "screen", "the nested begin changes nothing"
    assert _open(db_path)["at"] == 1000, "and opens no second interval"

    # The engine's release is refused; the screen's release ends the pause.
    assert activity.end_pause(str(lock), db_path, owner="engine", now=1010) is False
    assert lock.exists()
    assert activity.end_pause(str(lock), db_path, owner="screen", now=1020) is True
    assert _closed(db_path) == [[1000, 1020]], "one interval for one acquisition"


def test_a_legacy_empty_file_is_releasable_by_anyone(db_path, tmp_path):
    """An upgrade must not lock anyone out: the old empty file is unnamed."""
    lock = tmp_path / ".pauselock"
    lock.write_text("", encoding="utf-8")
    # The old build's interval, still open.
    StateStore(state_path_for(db_path)).save({activity.PAUSE_SECTION: {
        "open_interval": {"at": 1000, "source": "legacy"}, "closed_intervals": []}})

    assert activity.lock_owner(str(lock)) is None, "an empty file has no owner"

    assert activity.end_pause(str(lock), db_path, owner="web-page:1", now=1020) is True
    assert not lock.exists(), "a legacy lock is releasable by anyone"
    assert _open(db_path) is None
    assert _closed(db_path) == [[1000, 1020]]


def test_an_unparseable_lock_is_also_legacy_and_releasable(db_path, tmp_path):
    lock = tmp_path / ".pauselock"
    lock.write_text("{broken", encoding="utf-8")
    assert activity.lock_owner(str(lock)) is None, "a truncated record is unnamed"

    activity.end_pause(str(lock), db_path, owner="anyone")
    assert not lock.exists(), "a legacy lock is releasable by anyone"


def test_a_named_lock_is_not_releasable_by_an_unnamed_caller(db_path, tmp_path):
    """The other direction: an old-style caller must not free a named lock."""
    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, owner="page:1", now=1000)

    assert activity.end_pause(str(lock), db_path, now=1010) is False
    assert lock.exists()
    assert _open(db_path) is not None


def test_a_missing_lock_is_still_an_idempotent_success(db_path, tmp_path):
    """The close is attempted even with the file gone, so an interval cannot
    be left open while the lock is absent."""
    StateStore(state_path_for(db_path)).save({activity.PAUSE_SECTION: {
        "open_interval": {"at": 1000, "source": "legacy"}, "closed_intervals": []}})

    assert activity.end_pause(str(tmp_path / "absent"), db_path, now=1020) is True
    assert _open(db_path) is None
    assert _closed(db_path) == [[1000, 1020]]


def test_a_refused_release_is_logged_with_both_owners(db_path, tmp_path, caplog):
    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, owner="screen", now=1000)

    with caplog.at_level(logging.INFO):
        activity.end_pause(str(lock), db_path, owner="engine", now=1010)

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "screen" in text and "engine" in text, \
        f"the refusal must name both owners; got {text!r}"


# --------------------------------------------------------------------------
# reclaim: a dead holder must not stop the daemon
# --------------------------------------------------------------------------


def test_reclaim_removes_a_dead_owned_lock_and_closes_its_interval(
        db_path, tmp_path, monkeypatch, caplog):
    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, owner="dead-pass", now=1000)
    monkeypatch.setattr(src.daemon_control, "_pid_alive", lambda pid: False)

    with caplog.at_level(logging.WARNING):
        reclaimed = activity.reclaim_if_owner_gone(str(lock), db_path, now=1030)

    assert reclaimed is True
    assert not lock.exists(), "the dead holder's lock is gone"
    assert _open(db_path) is None
    assert _closed(db_path) == [[1000, 1030]], "and its interval closed exactly once"
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "dead-pass" in text, f"the reclaim must name the owner; got {text!r}"


def test_reclaim_keeps_a_live_owned_lock(db_path, tmp_path, monkeypatch):
    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, owner="live-pass", now=1000)
    monkeypatch.setattr(src.daemon_control, "_pid_alive", lambda pid: True)

    assert activity.reclaim_if_owner_gone(str(lock), db_path, now=1030) is False
    assert lock.exists()
    assert activity.lock_owner(str(lock))["owner"] == "live-pass"
    assert _open(db_path) is not None, "a live holder keeps its interval open"


def test_reclaim_keeps_a_lock_whose_liveness_is_unknown(db_path, tmp_path, monkeypatch):
    """``None`` means the platform cannot say; the lock is left alone."""
    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, owner="maybe-pass", now=1000)
    monkeypatch.setattr(src.daemon_control, "_pid_alive", lambda pid: None)

    assert activity.reclaim_if_owner_gone(str(lock), db_path, now=1030) is False
    assert lock.exists()
    assert _open(db_path) is not None


def test_reclaim_uses_the_daemon_control_pid_seam_not_os_kill(
        db_path, tmp_path, monkeypatch):
    """Liveness is ``daemon_control._pid_alive``; patching it is the seam.

    Patching ``os.kill`` would be the wrong test on Windows, whose branch never
    calls it -- and must not.
    """
    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, owner="dead-pass", now=1000)
    seen = []

    def fake_pid_alive(pid):
        seen.append(pid)
        return False

    monkeypatch.setattr(src.daemon_control, "_pid_alive", fake_pid_alive)

    assert activity.reclaim_if_owner_gone(str(lock), db_path, now=1030) is True
    assert seen == [os.getpid()], "the recorded pid is the one judged"


def test_a_legacy_lock_is_never_reclaimed(db_path, tmp_path, monkeypatch):
    """No pid means no judgement: the empty file is left alone."""
    lock = tmp_path / ".pauselock"
    lock.write_text("", encoding="utf-8")
    monkeypatch.setattr(src.daemon_control, "_pid_alive", lambda pid: False)

    assert activity.reclaim_if_owner_gone(str(lock), db_path) is False
    assert lock.exists()


def test_begin_pause_self_heals_a_dead_owned_lock(
        db_path, tmp_path, monkeypatch, caplog):
    """The same self-heal at acquisition, not only in the polls."""
    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, source="tui", owner="dead-pass", now=1000)
    monkeypatch.setattr(src.daemon_control, "_pid_alive", lambda pid: False)

    with caplog.at_level(logging.WARNING):
        opened = activity.begin_pause(str(lock), db_path, source="web",
                                      owner="new-pass", now=1030)

    assert opened is True, "the reclaim makes this a fresh absent -> present edge"
    assert activity.lock_owner(str(lock))["owner"] == "new-pass"
    assert activity.lock_owner(str(lock))["source"] == "web"
    assert _open(db_path)["at"] == 1030, "one interval, opened by the new owner"
    assert _closed(db_path) == [[1000, 1030]], \
        "the dead holder's interval is closed exactly once"
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "dead-pass" in text, "the self-heal logs the reclaim"


def test_begin_pause_does_not_steal_a_live_locks_owner(db_path, tmp_path, monkeypatch):
    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, source="tui", owner="live", now=1000)
    monkeypatch.setattr(src.daemon_control, "_pid_alive", lambda pid: True)

    assert activity.begin_pause(str(lock), db_path, source="web",
                                owner="other", now=1010) is False
    assert activity.lock_owner(str(lock))["owner"] == "live"
    assert _open(db_path)["at"] == 1000
    assert _closed(db_path) == []


# --------------------------------------------------------------------------
# the worker polls
# --------------------------------------------------------------------------


def test_the_web_worker_resumes_past_a_dead_owned_lock(db_path, tmp_path, monkeypatch):
    """A holder that died must not stop the daemon's web work for good."""
    from unittest.mock import patch
    from src.database import insert_or_update_item
    from src.web_worker import WebScraperThread

    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, owner="dead-pass", source="web", now=1000)
    monkeypatch.setattr(src.daemon_control, "_pid_alive", lambda pid: False)

    insert_or_update_item(db_path, {"workshop_id": 1, "web_scrape_priority": 5,
                                    "steam_updated_at": 1})
    worker = WebScraperThread(db_path, str(lock))
    served = 0

    def next_item(*args, **kwargs):
        nonlocal served
        served += 1
        if served > 1:
            worker.running = False
            return None
        return {"workshop_id": 1, "steam_updated_at": 1}

    scraped = {"description": "text", "tags": [], "body": None, "title": "T",
               "http_status": 200}

    with patch("src.web_worker.get_next_web_scrape_item", side_effect=next_item), \
         patch("src.web_worker.scrape_extended_details", return_value=scraped), \
         patch("time.sleep"), patch("src.pacing.wait"):
        worker.start()
        worker.join(timeout=5)

    assert not worker.is_alive(), "the worker must stop when the queue is exhausted"
    assert served >= 1, "the worker must pass the dead-owned lock and take work"
    assert not lock.exists(), "the reclaim removed the stranded lock"


def test_the_image_worker_resumes_past_a_dead_owned_lock(db_path, tmp_path, monkeypatch):
    from unittest.mock import patch
    from src.image_worker import ImageDownloadThread

    lock = tmp_path / ".pauselock"
    activity.begin_pause(str(lock), db_path, owner="dead-pass", source="web", now=1000)
    monkeypatch.setattr(src.daemon_control, "_pid_alive", lambda pid: False)

    worker = ImageDownloadThread(db_path, str(lock))
    asked = 0

    def next_item(*args, **kwargs):
        nonlocal asked
        asked += 1
        worker.running = False
        return None

    with patch("src.image_worker.get_next_image_item", side_effect=next_item), \
         patch("time.sleep"), patch("src.pacing.wait"):
        worker.start()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert asked == 1, "the worker must pass the dead-owned lock and reach the queue"
    assert not lock.exists(), "the reclaim removed the stranded lock"


# --------------------------------------------------------------------------
# the web routes carry the caller's owner
# --------------------------------------------------------------------------


@pytest.fixture
def web_client(tmp_path):
    from src.database import initialize_database
    from src.webserver import app, init_webserver

    db_path = str(tmp_path / "pause_owner_web.db")
    initialize_database(db_path)
    config = {"database": {"path": db_path}, "daemon": {"target_appids": [294100]}}
    init_webserver(db_path, config)
    return app.test_client(), db_path


def test_the_pause_routes_record_and_release_the_callers_owner(
        web_client, tmp_path, monkeypatch):
    """`/api/pause` and `/api/resume` pass the page's owner through unchanged."""
    client, db_path = web_client
    monkeypatch.chdir(tmp_path)
    lock = Path(".pauselock")

    assert client.post('/api/pause',
                       json={"owner": "page:3"}).get_json() == {"ok": True}
    record = _record(lock)
    assert record["owner"] == "page:3"
    assert record["source"] == "web_subscribe"

    # A different tab cannot release it.
    assert client.post('/api/resume',
                       json={"owner": "page:4"}).get_json() == {"ok": True}
    assert lock.exists(), "another page's resume must not remove this lock"
    assert _record(lock)["owner"] == "page:3"

    # Its own owner can.
    client.post('/api/resume', json={"owner": "page:3"})
    assert not lock.exists()


def test_the_resume_route_still_frees_a_legacy_empty_lock(web_client, tmp_path, monkeypatch):
    client, _ = web_client
    monkeypatch.chdir(tmp_path)
    lock = Path(".pauselock")
    lock.write_text("", encoding="utf-8")

    client.post('/api/resume', json={"owner": "page:any"})
    assert not lock.exists(), "an upgrade must not lock anyone out"


def test_the_pause_route_tolerates_a_caller_with_no_owner(web_client, tmp_path, monkeypatch):
    client, _ = web_client
    monkeypatch.chdir(tmp_path)
    lock = Path(".pauselock")

    assert client.post('/api/pause').get_json() == {"ok": True}
    assert _record(lock)["owner"] is None, "an anonymous caller writes an unnamed lock"
    assert activity.lock_owner(str(lock))["owner"] is None
    # Unnamed, so any caller may release it.
    client.post('/api/resume')
    assert not lock.exists()
