"""Stopping the daemon must not freeze the interface that asked for it.

`DaemonController.stop()` polls the process every half second for up to
`STOP_TIMEOUT_SECONDS` (15 s) and then waits up to another 3 s for a forced kill,
and `restart()` is `stop()` followed by `start()`. Run from the button handler,
that held the Textual event loop for the whole shutdown: no keypress, no screen
change and no timer, including the manager screen's own two-second log poll,
which fell silent at the moment its output was most wanted.

The behavioural test is the one that matters -- it asserts the click returns
*before* the shutdown finishes. Against the old direct call it fails, because
the handler did not return until `stop()` had.
"""

import time
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button

from src.tui import DaemonManagerScreen
from tests.conftest import ASYNC_PAUSE


class _SlowController:
    """Enough of the controller for the screen, with a stop worth waiting for."""

    def __init__(self, delay: float = 0.4):
        self.delay = delay
        self.started = False
        self.finished = False
        self.calls: list[str] = []

    def status(self):
        return {"running": not self.finished, "pid": None if self.finished else 4242}

    def is_running(self):
        return not self.finished

    def read_pid(self):
        return 4242

    def tail_log(self, since=0, max_bytes=0, max_lines=0):
        return {"lines": [], "offset": 0, "reset": False}

    def _transition(self, name: str):
        self.calls.append(name)
        self.started = True
        time.sleep(self.delay)
        self.finished = True
        return True, f"Daemon {name}"

    def start(self):
        return self._transition("start")

    def stop(self):
        return self._transition("stop")

    def restart(self):
        return self._transition("restart")


async def _open_screen(pilot):
    screen = DaemonManagerScreen(_SlowController())
    pilot.app.push_screen(screen)
    await pilot.pause(ASYNC_PAUSE)
    return screen


@pytest.mark.asyncio
async def test_the_stop_button_does_not_wait_for_the_shutdown():
    controller = _SlowController(delay=0.4)
    app = App()
    async with app.run_test() as pilot:
        screen = DaemonManagerScreen(controller)
        app.push_screen(screen)
        await pilot.pause(ASYNC_PAUSE)

        await pilot.click("#dm-stop")

        # The click returned and the shutdown is still running, which is the
        # whole point: the handler handed the blocking call to a worker instead
        # of making the event loop wait for it.
        assert controller.started, "the stop was handed off"
        assert not controller.finished, \
            "the click waited for the shutdown to finish -- the UI was frozen"
        assert screen._transitioning

        # And the controls say so, which is also what refuses a second press.
        assert screen.query_one("#dm-stop", Button).disabled

        await pilot.pause(1.0)
        assert controller.finished, "the worker ran the stop to completion"
        assert not screen._transitioning
        assert not screen.query_one("#dm-stop", Button).disabled


@pytest.mark.asyncio
async def test_a_second_transition_while_one_is_running_is_refused():
    """Two overlapping stops would fight over the same process."""
    controller = _SlowController(delay=0.4)
    app = App()
    async with app.run_test() as pilot:
        screen = DaemonManagerScreen(controller)
        app.push_screen(screen)
        await pilot.pause(ASYNC_PAUSE)

        await pilot.click("#dm-stop")
        screen._begin_transition("Stopping", controller.stop)

        assert controller.calls == ["stop"], "the second transition must be refused"
        await pilot.pause(1.0)
        assert not screen._transitioning, "the flag is cleared once the worker is done"


@pytest.mark.asyncio
async def test_a_controller_fault_puts_the_controls_back():
    """Otherwise a failed stop leaves the screen disabled and unusable."""
    class _Broken(_SlowController):
        def stop(self):
            self.calls.append("stop")
            raise RuntimeError("controller exploded")

    controller = _Broken()
    app = App()
    async with app.run_test() as pilot:
        screen = DaemonManagerScreen(controller)
        app.push_screen(screen)
        await pilot.pause(ASYNC_PAUSE)

        await pilot.click("#dm-stop")
        await pilot.pause(0.5)

        assert not screen._transitioning, "a fault must not wedge the screen"
        assert not screen.query_one("#dm-stop", Button).disabled


@pytest.mark.asyncio
async def test_a_restart_is_one_call_not_stop_then_start():
    """Two calls could interleave with another press between the halves."""
    controller = _SlowController(delay=0.1)
    app = App()
    async with app.run_test() as pilot:
        screen = DaemonManagerScreen(controller)
        app.push_screen(screen)
        await pilot.pause(ASYNC_PAUSE)

        await pilot.click("#dm-restart")
        await pilot.pause(0.6)

        assert controller.calls == ["restart"]


def test_the_handler_hands_the_work_to_a_worker():
    """A revert to calling the controller directly would freeze the UI again."""
    src = Path("src/tui.py").read_text(encoding="utf-8")
    screen = src[src.index("class DaemonManagerScreen"):]
    screen = screen[:screen.index("class SubscriptionQueueScreen")]
    handler = screen[screen.index("def on_button_pressed"):]

    assert "self.controller.stop()" not in handler
    assert "self.controller.restart()" not in handler
    assert handler.count("_begin_transition") == 3, \
        "every transition must go through the worker"
