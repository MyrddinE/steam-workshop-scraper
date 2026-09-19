"""The per-queue time-to-drain metric: active time, net API inflow, uncertainty.

The owner asked for a time to drain that can be shown immediately, carries its
uncertainty as a percentage, does not read a pause as a slowdown, and does not
invent a rate for a queue with no completions. Each of those is pinned here by
driving the real metric over a real database.

The "old behaviour" this file is written against is that there was no
``queue_eta`` metric at all, so every test failed with a ``KeyError`` before the
change; that run is recorded in the commit message. Within the new metric the
interesting properties are:

* the rate is completions divided by **active** seconds, so a recorded pause
  inside the window raises the rate and leaves the completions alone;
* only the queues ``.pauselock`` actually stops (web and image -- the daemon's
  other two stages are not gated by it) use active time;
* the API queue's rate is **net** of the staleness sweep's recorded rowcount,
  and the other three are gross and say so;
* the uncertainty is the Poisson relative standard error, ``100/sqrt(k)``, a
  percentage that narrows as evidence accumulates;
* a queue with no completions in the window gets no rate rather than a zero.
"""

from __future__ import annotations

import os
import time

import pytest

from src import activity, metrics
from src.daemon_state import StateStore, state_path_for
from src.database import get_connection, insert_or_update_item
from src.tui import StatsScreen

WINDOW = 10_000  # seconds; long enough that a synthetic pause is unambiguous


def _eta(db_path, window=WINDOW):
    return metrics.values(metrics.compute(
        db_path, ["queue_eta"], {"drain_window_seconds": window}))["queue_eta"]


def _item(db_path, workshop_id, **over):
    record = {"workshop_id": workshop_id, "title": f"item {workshop_id}",
              "fetch_status": 200, "api_priority": 0}
    record.update(over)
    insert_or_update_item(db_path, record)


#: Which outstanding flag an item carries while it is also a completion. A real
#: queue's items are generally outstanding while they complete, and the metric
#: needs a non-empty queue to have a time to drain at all.
_FLAG_FOR = {
    "api_fetched_at": ("api_priority", 3),
    "web_scraped_at": ("needs_web_scrape", 5),
    "image_fetched_at": ("needs_image", 5),
    "translated_at": ("translation_priority", 5),
}


def _completions(db_path, column, count, *, start=1, spacing=10):
    """``count`` live, queued items whose ``column`` is inside the window."""
    now = int(time.time())
    flag, value = _FLAG_FOR[column]
    for index in range(count):
        _item(db_path, start + index, **{flag: value, column: now - spacing * (index + 1)})


def _pause(db_path, lock_path, start, end):
    """Record one pause interval without touching the real lock file."""
    activity.begin_pause(str(lock_path), db_path, source="test", now=start)
    activity.end_pause(str(lock_path), db_path, now=end)


# --------------------------------------------------------------------------
# no rate until there is evidence
# --------------------------------------------------------------------------


def test_a_queue_with_no_completions_does_not_invent_a_rate(db_path):
    """Outstanding depth is real; the rate is honestly absent."""
    _item(db_path, 1, needs_web_scrape=5)

    web = _eta(db_path)["queues"]["web"]

    assert web["outstanding"] == 1
    assert web["completed"] == 0
    assert web["per_hour"] is None
    assert web["per_day"] is None
    assert web["eta_seconds"] is None
    assert web["uncertainty_pct"] is None


def test_an_empty_queue_is_drained_not_unmeasurable(db_path):
    _item(db_path, 1, needs_web_scrape=0)

    web = _eta(db_path)["queues"]["web"]

    assert web["outstanding"] == 0
    assert web["eta_seconds"] == 0.0


def test_outstanding_depth_excludes_dead_items(db_path):
    """A dead item can never complete, so it must not promise a drain."""
    _item(db_path, 1, needs_web_scrape=5)
    _item(db_path, 2, needs_web_scrape=5, fetch_status=-1)

    assert _eta(db_path)["queues"]["web"]["outstanding"] == 1


# --------------------------------------------------------------------------
# uncertainty
# --------------------------------------------------------------------------


def test_the_uncertainty_is_a_percentage_that_narrows_as_evidence_accumulates(db_path):
    _completions(db_path, "web_scraped_at", 1)
    sparse = _eta(db_path)["queues"]["web"]

    _completions(db_path, "web_scraped_at", 99, start=1000)
    abundant = _eta(db_path)["queues"]["web"]

    assert sparse["uncertainty_pct"] == pytest.approx(100.0)
    assert abundant["uncertainty_pct"] == pytest.approx(10.0)
    assert sparse["uncertainty_pct"] > abundant["uncertainty_pct"]
    assert 0 < abundant["uncertainty_pct"] <= 100


def test_the_rendered_uncertainty_is_a_percentage_not_an_absolute_span():
    value = {
        "window_seconds": 86400, "paused_seconds": 0, "sweep_inflow": 0,
        "queues": {"web": {"outstanding": 4, "completed": 4, "per_day": 4.2,
                           "eta_seconds": 86400.0, "uncertainty_pct": 50.0,
                           "basis": "gross", "honours_pause": True}},
    }
    text = StatsScreen._format_queue_eta(value)
    assert "± 50%" in text
    assert "d" in text, "the ETA is a span"
    assert "+/-" not in text


# --------------------------------------------------------------------------
# active time: a pause must not read as a slowdown
# --------------------------------------------------------------------------


def test_paused_time_is_excluded_from_the_rate(db_path, tmp_path):
    """The same completions over half the active time is twice the rate."""
    _completions(db_path, "web_scraped_at", 10)
    baseline = _eta(db_path)["queues"]["web"]

    now = int(time.time())
    _pause(db_path, tmp_path / "lock", now - 9000, now - 4000)  # 5000 s of 10000
    paused = _eta(db_path)["queues"]["web"]

    assert paused["completed"] == baseline["completed"] == 10
    assert paused["active_seconds"] < baseline["active_seconds"]
    assert paused["per_hour"] > baseline["per_hour"]
    assert paused["eta_seconds"] < baseline["eta_seconds"]


def test_only_the_queues_the_lock_stops_use_active_time(db_path, tmp_path):
    """The API loop and the translator are not gated by `.pauselock`.

    Subtracting the pause from a queue that kept working would overstate its
    rate, so those two keep the full window.
    """
    _completions(db_path, "api_fetched_at", 10)
    _completions(db_path, "web_scraped_at", 10, start=1000)
    baseline = _eta(db_path)["queues"]

    now = int(time.time())
    _pause(db_path, tmp_path / "lock", now - 9000, now - 4000)
    paused = _eta(db_path)["queues"]

    assert paused["web"]["per_hour"] > baseline["web"]["per_hour"]
    assert paused["api"]["per_hour"] == baseline["api"]["per_hour"]
    assert paused["api"]["honours_pause"] is False
    assert paused["web"]["honours_pause"] is True


def test_a_pause_still_in_progress_uses_the_active_time_before_it(db_path, tmp_path):
    _completions(db_path, "web_scraped_at", 10)
    baseline = _eta(db_path)["queues"]["web"]

    now = int(time.time())
    activity.begin_pause(str(tmp_path / "lock"), db_path, source="test",
                         now=now - 1000)  # never resumed
    in_progress = _eta(db_path)["queues"]["web"]

    assert in_progress["active_seconds"] < baseline["active_seconds"]
    assert in_progress["per_hour"] > baseline["per_hour"]


def test_nested_pause_holders_do_not_double_subtract(db_path, tmp_path):
    """The TUI screen holds the lock while the engine re-enters it."""
    _completions(db_path, "web_scraped_at", 10)
    baseline = _eta(db_path)["queues"]["web"]
    now = int(time.time())
    lock = str(tmp_path / "lock")

    activity.begin_pause(lock, db_path, source="tui", now=now - 9000)
    activity.begin_pause(lock, db_path, source="engine", now=now - 8000)
    activity.end_pause(lock, db_path, now=now - 4000)

    after = _eta(db_path)["queues"]["web"]
    # One ~5000 s interval, not the same paused time counted twice.
    assert after["completed"] == 10
    assert 1.5 * baseline["per_hour"] < after["per_hour"] < 2.5 * baseline["per_hour"]


# --------------------------------------------------------------------------
# the API queue's net inflow
# --------------------------------------------------------------------------


def test_a_recorded_sweep_inflow_lowers_the_net_api_rate(db_path):
    _completions(db_path, "api_fetched_at", 10)
    baseline = _eta(db_path)["queues"]["api"]

    activity.record_sweep_inflow(db_path, 5)
    net = _eta(db_path)["queues"]["api"]

    assert net["gross_completed"] == 10
    assert net["inflow_subtracted"] == 5
    assert net["completed"] == 5
    assert net["per_hour"] < baseline["per_hour"]
    assert net["eta_seconds"] > baseline["eta_seconds"]


def test_the_sweep_inflow_only_touches_the_api_queue(db_path):
    _completions(db_path, "api_fetched_at", 10)
    _completions(db_path, "web_scraped_at", 10, start=1000)
    baseline = _eta(db_path)["queues"]

    activity.record_sweep_inflow(db_path, 5)
    net = _eta(db_path)["queues"]

    assert net["api"]["completed"] == 5
    assert net["web"]["completed"] == baseline["web"]["completed"] == 10
    assert net["api"]["basis"] == "net"
    assert net["web"]["basis"] == "gross"
    assert net["translation"]["basis"] == "gross"


def test_a_sweep_outside_the_window_is_not_subtracted(db_path):
    _completions(db_path, "api_fetched_at", 10)
    baseline = _eta(db_path)["queues"]["api"]

    now = int(time.time())
    activity.record_sweep_inflow(db_path, 5, now=now - WINDOW - 60)
    after = _eta(db_path)["queues"]["api"]

    assert activity.sweep_inflow(db_path, now - WINDOW) == 0
    assert after["completed"] == baseline["completed"]


def test_the_staleness_sweep_records_its_own_rowcount(db_path):
    """The writer side: the daemon's sweep is what feeds the subtraction."""
    from src.daemon import Daemon

    stale = int(time.time()) - 40 * 86400
    for index in range(3):
        _item(db_path, 100 + index, api_priority=0, api_fetched_at=stale)
    _item(db_path, 200, api_priority=0, api_fetched_at=int(time.time()))

    daemon = Daemon({"database": {"path": db_path}, "daemon": {"target_appids": [1]}})
    daemon._promote_stale_items()

    assert activity.sweep_inflow(db_path, int(time.time()) - WINDOW) == 3


def test_the_engine_pause_lock_records_its_interval(db_path, tmp_path):
    """The subscribe engine's own PauseLock is the third `.pauselock` writer."""
    from src.subscribe_engine import PauseLock

    lock = str(tmp_path / ".pauselock")
    with PauseLock(lock, db_path=db_path, source="subscribe_engine"):
        assert os.path.exists(lock), "the lock is still the pause"

    section = StateStore(state_path_for(db_path)).load().get(activity.PAUSE_SECTION)
    assert section["open"] is None
    assert len(section["closed"]) == 1


# --------------------------------------------------------------------------
# cost: the outstanding counts reach their rows through the queue indexes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("queue,index", [
    ("api", "idx_api_queue"),
    ("web", "idx_web_scrape_queue"),
    ("image", "idx_image_queue"),
    ("translation", "idx_translation_priority"),
])
def test_the_outstanding_count_reads_its_queue_index(db_path, queue, index):
    conn = get_connection(db_path)
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        metrics._queue_eta(conn, {"db_path": db_path, "drain_window_seconds": WINDOW})
    finally:
        conn.close()

    spec = next(item for item in metrics._DRAIN_QUEUES if item[0] == queue)
    predicate = spec[2]
    sql = next(s for s in seen if predicate in s and "COUNT(*)" in s)
    import sqlite3
    check = sqlite3.connect(db_path)
    try:
        plan = " | ".join(row[3] for row in check.execute("EXPLAIN QUERY PLAN " + sql))
    finally:
        check.close()
    assert f"USING INDEX {index}" in plan, plan
    assert "SCAN workshop_items" not in plan, plan
