"""The subscription queue's estimate of when each item will be reached.

The web subscription overlay gives every queued row a live countdown to when it
will be reached (`templates/index.html`, `openAt = i * ceil(stepDelay / 1000)`),
and the TUI screen needs the same function. The engine's cost is not the web
overlay's, though: each queued item pays **two** gated page reads -- the pre-read
that guards the POST and the confirmation read -- while the POST itself is an XHR
and pays no interval. So the per-item step here is twice the shared configured
web delay, read fresh on every tick so a throttle that doubles it mid-pass is
reflected, and the delay comes from the one owner
(`src.web_worker.configured_web_delay`) rather than a second copy.

The figures are presented as an estimate, never a promise: the pass can be
refused, throttled or cancelled after this was drawn.
"""

import time

from src.tui import SubscriptionQueueScreen
from src.web_worker import configured_web_delay


def _screen(tmp_path, config: dict) -> SubscriptionQueueScreen:
    return SubscriptionQueueScreen(
        str(tmp_path / "queue.db"), str(tmp_path / "pause.lock"), config,
    )


def test_the_step_is_two_gated_page_reads_of_the_shared_delay(tmp_path):
    """The engine reads the page twice per item; the POST is exempt."""
    config = {"daemon": {"web_delay_seconds": 12.0}}
    screen = _screen(tmp_path, config)

    assert screen._step_seconds() == 2 * configured_web_delay(config)
    assert screen._step_seconds() == 24.0
    # The third item is reached after two completed items, not after one.
    assert screen._estimate_remaining(0, 0.0) == 0
    assert screen._estimate_remaining(1, 0.0) == 24
    assert screen._estimate_remaining(2, 0.0) == 48


def test_a_changed_configured_delay_changes_the_estimate(tmp_path):
    """A throttle doubles the shared delay mid-pass; the estimate follows."""
    config = {"daemon": {"web_delay_seconds": 12.0}}
    screen = _screen(tmp_path, config)
    before = screen._estimate_remaining(1, 0.0)

    # What `WebInterval._set` writes back through the same config dict the
    # screen handed the engine, after a throttle page doubles the delay.
    config["daemon"]["web_delay_seconds"] = 24.0

    assert before == 24
    assert screen._estimate_remaining(1, 0.0) == 48


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
    countdown, status, _colour = screen._row_state(0, 0.0)
    assert countdown is None
    assert status == "subscribing..."

    # The second and third are still waiting, spaced by two reads each.
    assert screen._row_state(1, 0.0)[0] == "~24s"
    assert screen._row_state(2, 0.0)[0] == "~48s"

    # Once an item's outcome lands its countdown is cleared and the next item
    # becomes the current one.
    from src import subscribe_engine
    screen._outcomes[1] = subscribe_engine.SubscribeOutcome(
        1, subscribe_engine.SUBSCRIBED, subscribed=True)
    assert screen._row_state(0, 0.0)[0] is None
    assert screen._row_state(0, 0.0)[1] == "subscribed"
    assert screen._row_state(1, 0.0)[1] == "subscribing..."

    screen._outcomes[2] = subscribe_engine.SubscribeOutcome(
        2, subscribe_engine.THROTTLED)
    assert screen._row_state(1, 0.0)[0] is None
    assert screen._row_state(1, 0.0)[1] == "left queued (throttled)"
    assert screen._row_state(2, 0.0)[1] == "subscribing..."

    # And with the pass over there is no countdown and no "current" row left.
    screen._pass_running = False
    assert screen._row_state(2, 0.0) == (None, None, None)
