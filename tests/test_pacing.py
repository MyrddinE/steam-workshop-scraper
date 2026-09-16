"""The shared backoff shape: doubling up, a ten-minute half-life down.

Every rate-seeking worker moves its inter-request delay the same way now, and
the arithmetic lives in one place so the three cannot drift apart. The
translator is deliberately not one of them -- it waits out a daily quota rather
than a rate -- and `src/translator.py` says so.

Two properties matter more than the formulas, and both are easy to get wrong:

* **The decay is paid in time.** The delay halves over `HALF_LIFE_SECONDS` of
  elapsed operation whatever its current size, which a success-counted rule
  cannot express: 200 successes is twenty minutes at a 6 s delay and under a
  second at an API delay.
* **A sustained failure cannot run away.** The delay doubles only when an
  attempt fails, and the next attempt is a whole delay away, so the intervals
  stretch as fast as the delay grows. After k refusals the delay is `d0 * 2**k`
  and the elapsed time is `d0 * (2**k - 1)`: the delay tracks the length of the
  outage. This is why no ceiling is needed, and it is worth a test because the
  intuition that says otherwise -- "doubling every few seconds for an hour" --
  is wrong, and it is the reason a ceiling looked necessary.
"""

from unittest.mock import patch

import pytest

from src import pacing


# --- up: a refusal doubles --------------------------------------------------

def test_a_refusal_doubles_the_delay():
    assert pacing.backoff(6.0) == 12.0
    assert pacing.backoff(0.01) == 0.02


def test_there_is_no_ceiling():
    """The ceiling went with the rest of the false-positive defences.

    It bounded a client that could never succeed, but it also stopped one
    reaching a sustainable rate above it, and the self-limiting property below
    is what makes it unnecessary.
    """
    delay = 6.0
    for _ in range(12):
        delay = pacing.backoff(delay)
    assert delay == 6.0 * 2 ** 12


# --- down: a half-life of healthy operation ---------------------------------

def test_a_half_life_halves_the_delay():
    assert pacing.decay(6.0, pacing.HALF_LIFE_SECONDS, 0.5) == pytest.approx(3.0)


def test_the_half_life_is_the_same_at_every_delay():
    """The property a success count cannot have."""
    for delay in (0.5, 6.0, 300.0):
        assert pacing.decay(delay, pacing.HALF_LIFE_SECONDS, 0.0) == \
            pytest.approx(delay / 2)


def test_two_half_lives_quarter_the_delay():
    assert pacing.decay(300.0, 2 * pacing.HALF_LIFE_SECONDS, 0.0) == pytest.approx(75.0)


def test_no_elapsed_time_leaves_the_delay_alone():
    """A fast request is not evidence of anything and must not move the delay."""
    assert pacing.decay(6.0, 0.0, 0.5) == 6.0
    assert pacing.decay(6.0, -1.0, 0.5) == 6.0


def test_the_decay_stops_at_the_floor():
    assert pacing.decay(6.0, 100 * pacing.HALF_LIFE_SECONDS, 0.5) == 0.5


def test_a_doubling_and_a_half_life_cancel():
    """The two directions are exact inverses, which is what makes the shape legible."""
    assert pacing.decay(pacing.backoff(0.25), pacing.HALF_LIFE_SECONDS, 0.01) == \
        pytest.approx(0.25)


# --- a sustained failure cannot run away ------------------------------------

def test_the_delay_tracks_the_outage_rather_than_outrunning_it():
    """`delay = elapsed + d0` after any number of refusals.

    Each attempt is a whole delay after the last, so the wait and the delay grow
    together and the elapsed time to reach `d0 * 2**k` is `d0 * (2**k - 1)`.
    This is the whole reason no cap is needed.
    """
    d0 = 6.0
    delay = d0
    elapsed = 0.0
    for _ in range(20):
        elapsed += delay              # the wait this delay imposes
        delay = pacing.backoff(delay)
    assert delay == pytest.approx(elapsed + d0)


def test_an_hour_of_continuous_refusal_is_about_an_hour_of_delay():
    """The claim, in the units it matters in.

    The naive reading -- double every few seconds for an hour -- gives a delay
    of years. It cannot happen: by the time the delay is that large, no time is
    left to have reached it.
    """
    d0 = 6.0
    delay = d0
    elapsed = 0.0
    while elapsed < 3600:
        elapsed += delay
        delay = pacing.backoff(delay)
    assert elapsed >= 3600, "the simulation ran for an hour of wall clock"
    assert delay < 3 * elapsed, f"delay {delay}s ran away from an {elapsed}s outage"


# --- what goes to disk ------------------------------------------------------

def test_only_the_persisted_copy_is_rounded():
    """Precision is kept where the arithmetic happens and given up on disk.

    A step at a 0.5 s delay is about 0.0006 s, so rounding in memory would stop
    the decay silently: round(0.4994, 3) is 0.5.
    """
    assert pacing.persistable(6.123456) == 6.12
    assert pacing.persistable(0.014999) == 0.01
    # The working value keeps its precision.
    assert pacing.decay(6.123456, 60.0, 0.0) != 6.12


def test_the_delay_is_written_only_once_it_has_moved_far_enough():
    """The decay runs on every success, so it needs a step of its own.

    The step is a heuristic, not a boundary anyone can land on exactly, so this
    checks either side of it rather than the exact value -- `6.0 + 0.05` is
    `6.049999...` in binary and comparing it to the step is a coin toss.
    """
    assert not pacing.needs_persist(6.0, 6.0)
    assert not pacing.needs_persist(6.0 + pacing.PERSIST_STEP_SECONDS * 0.1, 6.0)
    assert pacing.needs_persist(6.0 + pacing.PERSIST_STEP_SECONDS * 2, 6.0)
    assert pacing.needs_persist(5.0, 6.0)


# --- the clock --------------------------------------------------------------

def test_the_clock_measures_between_calls():
    clock = {"t": 100.0}
    with patch("src.pacing.now", side_effect=lambda: clock["t"]):
        ticker = pacing.Clock()
        clock["t"] = 130.0
        assert ticker.since() == pytest.approx(30.0)
        clock["t"] = 135.0
        assert ticker.since() == pytest.approx(5.0)


def test_the_clock_never_reports_negative_time():
    """A clock that went backwards must not look like healthy operation."""
    clock = {"t": 100.0}
    with patch("src.pacing.now", side_effect=lambda: clock["t"]):
        ticker = pacing.Clock()
        clock["t"] = 90.0
        assert ticker.since() == 0.0


# --- the wait ---------------------------------------------------------------

def _fake_time(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(pacing, "now", lambda: clock["t"])
    monkeypatch.setattr("time.sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return clock


def test_the_wait_serves_the_whole_period(monkeypatch):
    _fake_time(monkeypatch)
    assert pacing.wait(5.0, lambda: True) is True


def test_the_wait_stops_the_moment_the_worker_is_told_to(monkeypatch):
    """A long backoff must not hold a stop for its whole duration."""
    clock = _fake_time(monkeypatch)
    state = {"asked": 0}

    def keep_running():
        state["asked"] += 1
        return state["asked"] <= 3

    assert pacing.wait(3600.0, keep_running) is False
    assert clock["t"] < 10.0, "it gave up promptly rather than serving the hour"
