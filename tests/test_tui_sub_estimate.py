"""The subscription queue's estimate of when each item will be reached.

The web subscription overlay gives every queued row a live countdown to when it
will be reached (`templates/index.html`, `openAt = i * ceil(stepDelay / 1000)`),
and the TUI screen needs the same function. The engine's cost is not the web
overlay's, though: on the default path each queued item pays **one** gated page
read -- the pre-read that guards the POST; the confirmation read is retired
behind `subscribe_engine.VERIFY_AFTER_SUBSCRIBE`, and when it is on each item
pays a second -- while the POST itself is an XHR and pays no interval. So the
estimate *starts* at that many times the shared configured web delay, read fresh
on every tick so a throttle that doubles it mid-pass is reflected, and the delay
comes from the one owner (`src.web_worker.configured_web_delay`) rather than a
second copy.

*Measured live* (issue 41) that starting figure is about a third low: a gated
read costs the interval **plus** the request, and the POST spends time on the
clock even though it pays no interval. So from the first result on the screen
nudges the estimate with what the pass has actually done -- each finished item
is timed between the per-item results the pass already delivers, and the rows
still waiting are priced from the running mean of those observed durations,
seeded with the configured guess (the first item moves the mean a lot, later
ones less). Nothing is persisted and no history or rolling window is consulted.

The figures are presented as an estimate, never a promise: the pass can be
refused, throttled or cancelled after this was drawn.
"""

import math
import time

from src import subscribe_engine
from src.tui import SubscriptionQueueScreen
from src.web_worker import configured_web_delay


def _screen(tmp_path, config: dict) -> SubscriptionQueueScreen:
    return SubscriptionQueueScreen(
        str(tmp_path / "queue.db"), str(tmp_path / "pause.lock"), config,
    )


def _finished(screen: SubscriptionQueueScreen, *seconds: float) -> None:
    """Record that many items finished, each having taken the given cost.

    The durations go on the instance rather than through the private list so
    these tests run against the old code too and fail on the arithmetic, not on
    a missing attribute.
    """
    observed_so_far = list(getattr(screen, "_observed_seconds", []))
    for observed in seconds:
        wid = len(screen._outcomes) + 1
        screen._outcomes[wid] = subscribe_engine.SubscribeOutcome(
            wid, subscribe_engine.SUBSCRIBED, subscribed=True)
        observed_so_far.append(observed)
    screen._observed_seconds = observed_so_far


def test_the_step_is_one_gated_page_read_of_the_shared_delay(tmp_path):
    """The default path reads the page once per item; the POST is exempt."""
    config = {"daemon": {"web_delay_seconds": 12.0}}
    screen = _screen(tmp_path, config)

    assert screen._seed_item_seconds() == configured_web_delay(config)
    assert screen._seed_item_seconds() == 12.0
    # The third item is reached after two completed items, not after one.
    assert screen._estimate_remaining(0, 0.0) == 0
    assert screen._estimate_remaining(1, 0.0) == 12
    assert screen._estimate_remaining(2, 0.0) == 24


def test_the_step_is_two_gated_page_reads_when_the_confirmation_is_on(
        tmp_path, monkeypatch):
    """The retired read still prices an item while `VERIFY_AFTER_SUBSCRIBE` is on."""
    config = {"daemon": {"web_delay_seconds": 12.0}}
    screen = _screen(tmp_path, config)
    monkeypatch.setattr(subscribe_engine, "VERIFY_AFTER_SUBSCRIBE", True)

    assert screen._seed_item_seconds() == 2 * configured_web_delay(config)
    assert screen._seed_item_seconds() == 24.0


def test_the_initial_estimate_is_the_configured_delay_once(tmp_path):
    """Before any item finishes the running mean is exactly the seed."""
    config = {"daemon": {"web_delay_seconds": 6.0}}
    screen = _screen(tmp_path, config)

    assert screen._estimate_remaining(3, 0.0) == 18
    assert screen._estimated_item_seconds() == 6.0


def test_a_slow_item_lengthens_the_waiting_rows_countdowns(tmp_path):
    """An item that overran the guess prices the rest of the queue higher."""
    config = {"daemon": {"web_delay_seconds": 6.0}}
    screen = _screen(tmp_path, config)
    # The first item took 18 s, not the 6 s the configured delay predicted.
    _finished(screen, 18.0)

    # Row 2 is one item away, and that item now costs the seeded mean
    # (6 + 18) / 2 = 12 s; the uncorrected step said 2 * 6 - 18 = -6 s at this
    # instant, which the clamp would have drawn as 0.
    assert screen._estimate_remaining(2, 18.0) == 12
    assert screen._estimate_remaining(2, 18.0) > math.ceil(2 * 6.0 - 18.0)
    assert screen._estimated_item_seconds() == 12.0


def test_a_fast_item_shortens_the_waiting_rows_countdowns(tmp_path):
    """An item that came in under the guess prices the rest lower."""
    config = {"daemon": {"web_delay_seconds": 6.0}}
    screen = _screen(tmp_path, config)
    # This item took 2 s, well under the guess.
    _finished(screen, 2.0)

    # Row 2 is one item away at the mean (6 + 2) / 2 = 4 s; the uncorrected
    # step said 2 * 6 - 2 = 10 s here.
    assert screen._estimate_remaining(2, 2.0) == 4
    assert screen._estimate_remaining(2, 2.0) < math.ceil(2 * 6.0 - 2.0)
    assert screen._estimated_item_seconds() == 4.0


def test_the_correction_uses_the_observed_duration_not_the_item_count(tmp_path):
    """One finished item is not one fixed correction: its cost is the input."""
    config = {"daemon": {"web_delay_seconds": 6.0}}
    slow = _screen(tmp_path, config)
    fast = _screen(tmp_path, config)
    _finished(slow, 30.0)
    _finished(fast, 4.0)

    # Same finished count, same queue position, different durations: the two
    # screens must not draw the same number.
    assert slow._estimate_remaining(2, 30.0) == 18
    assert fast._estimate_remaining(2, 4.0) == 5
    assert len(slow._observed_seconds) == len(fast._observed_seconds) == 1
    assert slow._estimated_item_seconds() == 18.0   # (6 + 30) / 2
    assert fast._estimated_item_seconds() == 5.0    # (6 + 4) / 2


def test_the_seeded_mean_moves_less_with_each_later_item(tmp_path):
    """The configured guess is one virtual observation, so it decays."""
    config = {"daemon": {"web_delay_seconds": 6.0}}
    screen = _screen(tmp_path, config)

    assert screen._estimate_remaining(2, 0.0) == 12  # still the bare seed
    _finished(screen, 18.0)
    assert screen._estimate_remaining(2, 18.0) == 12
    _finished(screen, 18.0)
    assert screen._estimate_remaining(3, 36.0) == 14

    # 6 -> 12 -> 14: the first item moved the mean 6 s, the second only 2 s.
    mean_after_two = screen._estimated_item_seconds()
    assert mean_after_two == 14.0
    assert mean_after_two - 12.0 < 12.0 - screen._seed_item_seconds()


def test_each_items_duration_is_timed_between_the_passes_results(tmp_path):
    """The pass calls `on_result` after each item; the gap is that item's cost."""
    config = {"daemon": {"web_delay_seconds": 6.0}}
    screen = _screen(tmp_path, config)
    screen._pass_started_at = 100.0

    # The first item is measured from the pass's start, later ones from the
    # previous result -- so a slow first item and a fast second come out as 18
    # and 12, not as a fixed step.
    assert screen._item_seconds_since_last_result(118.0) == 18.0
    assert screen._item_seconds_since_last_result(130.0) == 12.0


def test_a_changed_configured_delay_changes_the_estimate(tmp_path):
    """A throttle doubles the shared delay mid-pass; the estimate follows."""
    config = {"daemon": {"web_delay_seconds": 12.0}}
    screen = _screen(tmp_path, config)
    before = screen._estimate_remaining(1, 0.0)

    # What `WebInterval._set` writes back through the same config dict the
    # screen handed the engine, after a throttle page doubles the delay.
    config["daemon"]["web_delay_seconds"] = 24.0

    assert before == 12
    assert screen._estimate_remaining(1, 0.0) == 24

    # The delay still pulls on the running mean once an item has finished: the
    # seed is re-read every tick, so it moves the mean with it.
    _finished(screen, 18.0)
    throttled = screen._estimate_remaining(2, 18.0)
    config["daemon"]["web_delay_seconds"] = 12.0
    assert screen._estimate_remaining(2, 18.0) < throttled


def test_the_countdown_never_goes_negative(tmp_path):
    """An item that overruns the mean leaves the waiting row at zero, not below."""
    config = {"daemon": {"web_delay_seconds": 6.0}}
    screen = _screen(tmp_path, config)
    _finished(screen, 40.0)

    # The current item has already spent far more than one full mean.
    assert screen._estimate_remaining(2, 500.0) == 0


def test_the_current_item_is_distinct_and_has_no_countdown(tmp_path):
    """One row is being processed; the rest still show an estimate."""
    config = {"daemon": {"web_delay_seconds": 12.0}}
    screen = _screen(tmp_path, config)
    screen._items = [
        {"workshop_id": 1, "title": "One"},
        {"workshop_id": 2, "title": "Two"},
        {"workshop_id": 3, "title": "Three"},
    ]
    screen._pass_running = True
    screen._pass_started_at = time.monotonic()

    # The first item is the one the engine is reading: no countdown, a word
    # that distinguishes it from the rows still waiting.
    countdown, status, _colour = screen._row_display(0, 0.0)
    assert countdown is None
    assert status == "subscribing..."

    # The second and third are still waiting, spaced by one read each.
    assert screen._row_display(1, 0.0)[0] == "~12s"
    assert screen._row_display(2, 0.0)[0] == "~24s"

    # Once an item's outcome lands its countdown is cleared and the next item
    # becomes the current one.
    from src import subscribe_engine
    screen._outcomes[1] = subscribe_engine.SubscribeOutcome(
        1, subscribe_engine.SUBSCRIBED, subscribed=True)
    assert screen._row_display(0, 0.0)[0] is None
    assert screen._row_display(0, 0.0)[1] == "subscribed"
    assert screen._row_display(1, 0.0)[1] == "subscribing..."

    screen._outcomes[2] = subscribe_engine.SubscribeOutcome(
        2, subscribe_engine.THROTTLED)
    assert screen._row_display(1, 0.0)[0] is None
    assert screen._row_display(1, 0.0)[1] == "left queued (throttled)"
    assert screen._row_display(2, 0.0)[1] == "subscribing..."

    # And with the pass over there is no countdown and no "current" row left.
    screen._pass_running = False
    assert screen._row_display(2, 0.0) == (None, None, None)
