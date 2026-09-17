"""The shared request schedule must ration gaps, not callers.

``_rate_limit`` is process-wide and keyed on the daemon's adaptive delay, so every
caller — the batched details fetch, the creator summaries, discovery — draws on one
budget. That is exactly what makes it safe to run discovery on its own thread: it
redistributes the budget rather than enlarging it.

It was not thread-safe. Every caller read one ``_last_api_call``, worked out its own
gap from it, and wrote it back — so two callers arriving together both saw the same
stale timestamp, both decided no wait was owed, and both fired. Nothing failed; the
rate was simply higher than the delay said, and only when two callers overlapped,
which is the worst way for a rate limiter to be wrong.
"""

import threading
import time

import pytest

from src import steam_api


@pytest.fixture(autouse=True)
def _restore_schedule():
    """These tests move a module global; put it back."""
    saved_delay = steam_api._API_DELAY
    saved_slot = steam_api._next_slot
    yield
    steam_api.set_api_delay(saved_delay)
    steam_api._next_slot = saved_slot


def _fire_together(count: int, delay: float) -> list[float]:
    """Run ``count`` callers concurrently; return their completion times, sorted."""
    steam_api.set_api_delay(delay)
    stamps: list[float] = []
    lock = threading.Lock()
    start = threading.Barrier(count)

    def worker():
        start.wait()          # all of them arrive at the limiter together
        steam_api._rate_limit()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return sorted(stamps)


def test_callers_arriving_together_are_still_spaced():
    """The regression: five simultaneous callers used to fire as one."""
    delay = 0.08
    stamps = _fire_together(5, delay)

    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:])]
    assert len(gaps) == 4
    assert all(gap >= delay * 0.75 for gap in gaps), (
        f"callers slipped past the shared gap: {[round(g, 3) for g in gaps]}")


def test_the_whole_burst_takes_as_long_as_its_slots():
    """Five callers at 0.08 s occupy four gaps, not zero."""
    delay = 0.08
    stamps = _fire_together(5, delay)
    assert stamps[-1] - stamps[0] >= delay * 4 * 0.75


def test_a_single_caller_never_waits_for_its_own_slot():
    """The slot is reserved *before* sleeping, so the first caller pays nothing."""
    steam_api.set_api_delay(30.0)
    started = time.monotonic()
    assert steam_api._rate_limit() is True
    assert time.monotonic() - started < 1.0


def test_the_wait_can_be_abandoned_for_a_shutdown():
    """A backoff can leave a long wait; a stopping daemon must not serve it."""
    steam_api.set_api_delay(30.0)
    steam_api._rate_limit()            # take the first slot, reserving the next

    calls = {"n": 0}

    def keep_running():
        calls["n"] += 1
        return calls["n"] <= 2

    started = time.monotonic()
    assert steam_api._rate_limit(keep_running) is False
    assert time.monotonic() - started < 10.0, "it served a 30 s wait instead of giving up"


def test_a_wait_that_finishes_normally_reports_that_it_may_proceed():
    steam_api.set_api_delay(0.05)
    steam_api._rate_limit()
    assert steam_api._rate_limit(lambda: True) is True


def test_the_schedule_uses_a_monotonic_clock():
    """A wall-clock correction must not be able to break the gap.

    The previous implementation read `time.time()`, so an NTP step could either
    collapse the schedule or park it for the length of the step.
    """
    source = __import__("pathlib").Path("src/steam_api.py").read_text(encoding="utf-8")
    start = source.index("def _rate_limit")
    body = source[start:source.index("\ndef ", start + 1)]
    assert "time.monotonic()" in body
    assert "time.time()" not in body
