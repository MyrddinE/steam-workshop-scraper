"""A transient SQLite lock must not end the TUI session (issue 43).

Two defects were behind the 2026-09-18 crash:

* ``get_connection`` ran ``PRAGMA journal_mode=WAL`` on **every** connection. A
  journal-mode transition is not covered by the connection's 15 s busy timeout —
  it needs a moment where nothing else holds a lock — so a read that only wanted
  a row reached for a mode change while the daemon was writing and raised
  ``sqlite3.OperationalError: database is locked``. WAL is a persistent property
  of the file, so it is established once, in ``initialize_database``.
* That error was raised inside a Textual timer callback, and an exception there
  takes the whole session down. The unattended TUI polls now catch a lock, skip
  the tick and try again on the next one; a user-initiated action still raises.

The connection test deliberately *traces the SQL the connection executes*
rather than reading the source, so a rewrite that reaches the journal mode some
other way cannot pass by accident.
"""

import ast
import json
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import ListView

from src.database import get_connection, initialize_database
from src.tui import DetailsPane, ScraperApp
from tests.conftest import ASYNC_PAUSE

TUI_SOURCE = Path(__file__).resolve().parents[1] / "src" / "tui.py"
WEB_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"
NODE = shutil.which("node")


# --------------------------------------------------------------------------
# the journal mode is established once, at initialisation
# --------------------------------------------------------------------------

def test_get_connection_issues_no_journal_mode_statement(tmp_path):
    """Trace what the connection executes, not what the source says.

    A ``PRAGMA journal_mode`` here is the crash: it is the one statement a
    plain reader cannot afford, because it needs exclusive-ish access that the
    busy timeout does not wait out.
    """
    db_path = str(tmp_path / "trace.db")
    # A plain, non-WAL database, so a mode change would be visible in the trace.
    sqlite3.connect(db_path).close()

    statements = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with patch("src.database.sqlite3.connect", side_effect=traced_connect):
        conn = get_connection(db_path)
        try:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            conn.close()

    executed = "\n".join(statements).lower()
    assert "journal_mode" not in executed, (
        "get_connection must not change the journal mode; the database is put "
        "into WAL once by initialize_database"
    )


def test_initialize_database_puts_a_fresh_database_in_wal(tmp_path):
    """The mode is a property of the file, so one call is enough for every reader."""
    db_path = str(tmp_path / "fresh.db")
    initialize_database(db_path)

    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


# --------------------------------------------------------------------------
# an unattended poll skips the tick; the session survives
# --------------------------------------------------------------------------

def _one_item(**over):
    item = {
        "workshop_id": 1,
        "title": "Amazing Mod",
        "creator_steamid": "Author A",
        "consumer_appid": 294100,
        "extended_description": "This mod is truly amazing.",
        "tags": '["Graphic", "Utility"]',
    }
    item.update(over)
    return item


@pytest.mark.asyncio
async def test_detail_pane_poll_survives_a_locked_database(mock_config):
    """The pane that crashed: a lock leaves its state alone instead of raising."""
    results = [_one_item()]
    with patch("src.tui.load_config", return_value=mock_config), \
         patch("src.tui.search_items", return_value=results), \
         patch("src.tui.get_item_details", return_value=results[0]), \
         patch("src.tui.get_all_creator_ids", return_value=["Author A"]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            list_view = app.query_one(ListView)
            list_view.index = 0
            app.set_focus(list_view)
            await pilot.press("enter")
            await pilot.pause(ASYNC_PAUSE)

            pane = app.query_one("#detail-pane", DetailsPane)
            assert pane.item_data is not None, "the pane should have adopted the item"
            before = dict(pane.item_data)

            with patch(
                "src.tui.get_item_details",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                await pane.refresh_data()

            assert pane.item_data == before, "a skipped tick must not clear the pane"


@pytest.mark.asyncio
async def test_subscription_marker_poll_survives_a_locked_database(mock_config):
    """The one-shot marker poll skips a locked tick *and* re-arms for the next."""
    results = [_one_item(is_queued_for_subscription=1)]
    with patch("src.tui.load_config", return_value=mock_config), \
         patch("src.tui.search_items", return_value=results), \
         patch("src.tui.get_item_details", return_value=results[0]), \
         patch("src.tui.get_all_creator_ids", return_value=["Author A"]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            assert app._queued_subscription_ids() == [1], (
                "the rendered queued row should be the poll's subject"
            )

            with patch(
                "src.tui.get_subscription_states",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                await app._poll_queued_subscriptions()
                assert app._sub_poll_timer is not None, (
                    "a locked tick must arm the next one, or the row never "
                    "updates again"
                )
            app._stop_subscription_poll()


# --------------------------------------------------------------------------
# the shared guard, and its repeat-logging discipline
# --------------------------------------------------------------------------

def test_guard_db_poll_skips_a_locked_read_and_reports_it_once(caplog):
    """A repeated lock does not write a line per tick forever."""
    from src import db_poll

    calls = []

    @db_poll.guard_db_poll("unit-test reader")
    def read():
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with caplog.at_level(logging.DEBUG):
        assert read() is None
        assert read() is None
        assert read() is None
    assert len(calls) == 3, "every tick still tried; the failure was not cached"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "only the first failure of a run belongs at warning"
    assert "unit-test reader" in warnings[0].getMessage()
    assert any(
        r.levelno == logging.DEBUG and "still" in r.getMessage().lower()
        for r in caplog.records
    ), "repeats are still visible at debug"


def test_a_read_that_succeeds_again_arms_the_warning_once_more(caplog):
    """The suppression ends at the first success, so a fresh lock is reported."""
    from src import db_poll

    reporter = db_poll.RepeatFailureLog()

    with caplog.at_level(logging.WARNING):
        reporter.failed("reader", "reader: locked (%s)", "first")
        reporter.failed("reader", "reader: locked (%s)", "second")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == ["reader: locked (first)"]

    caplog.clear()
    reporter.succeeded("reader")
    with caplog.at_level(logging.DEBUG):
        reporter.failed("reader", "reader: locked (%s)", "third")
    assert caplog.records[0].levelno == logging.WARNING
    assert caplog.records[0].getMessage() == "reader: locked (third)"


# --------------------------------------------------------------------------
# the guard cannot be forgotten by the next poll that is added
# --------------------------------------------------------------------------

#: Timer callbacks that lead to a database read without doing it themselves.
#: Each entry names the reason; anything not here must carry the guard itself,
#: so a future `self.set_interval(..., self._new_poll)` fails this test until it
#: is either decorated or listed below with its reason.
_TIMERS_WITHOUT_THEIR_OWN_READ = {
    ("ScraperApp", "_poll_queued_subscriptions"):
        "reads through refresh_subscription_rows, which is guarded",
    ("StatsScreen", "_on_scheduler_tick"):
        "reads on the stats worker, which catches a failed metric and retries",
    ("DaemonManagerScreen", "_tick"):
        "reads the PID file, the log file's size, and the log itself; "
        "no database access",
    ("SubscriptionQueueScreen", "_tick_estimates"):
        "redraws an estimate; no database access",
    ("ScraperApp", "_tick_spinners"):
        "redraws spinners; no database access",
}


def _timer_callbacks():
    """(class, callback) for every ``self.set_interval``/``set_timer`` in the TUI."""
    tree = ast.parse(TUI_SOURCE.read_text(encoding="utf-8"))
    found = set()
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in ("set_interval", "set_timer") or len(node.args) < 2:
                    continue
                target = node.args[1]
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    found.add((cls.name, target.attr))
    return found


def test_every_database_reading_timer_in_the_tui_is_guarded():
    import src.tui as tui

    callbacks = _timer_callbacks()
    assert callbacks, "the AST walk found no timer callbacks; it checks nothing"

    unguarded = []
    for cls_name, callback in sorted(callbacks):
        if (cls_name, callback) in _TIMERS_WITHOUT_THEIR_OWN_READ:
            continue
        handler = getattr(getattr(tui, cls_name), callback)
        if not getattr(handler, "_tolerates_db_lock", False):
            unguarded.append(f"{cls_name}.{callback}")

    assert not unguarded, (
        "these TUI timer callbacks read the database with no lock guard, so a "
        "transient lock ends the session again: " + ", ".join(unguarded) +
        ". Decorate them with src.db_poll.guard_db_poll, or list them in "
        "_TIMERS_WITHOUT_THEIR_OWN_READ with the reason they cannot meet a lock."
    )


def test_the_subscription_polls_read_is_guarded():
    """The poll's own callback is exempt above only because its read is guarded here."""
    from src.tui import ScraperApp as App

    handler = App.refresh_subscription_rows
    assert getattr(handler, "_tolerates_db_lock", False)


# --------------------------------------------------------------------------
# the browser polls get the same tolerance as the TUI's
# --------------------------------------------------------------------------

def _template_function(name):
    """The text of a top-level JS function in the page, by name."""
    text = WEB_TEMPLATE.read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def test_web_detail_poll_keeps_retrying_after_a_transient_server_error():
    """A 500 from a locked database must not permanently disable the pane poll."""
    body = _template_function("_startDetailPoll")
    assert "resp.status === 404" in body, (
        "a genuinely missing item is what stops the poll; a transient failure "
        "must not, because the next tick can succeed"
    )
    non_ok = [line for line in body.splitlines() if "if (!resp.ok)" in line]
    assert non_ok, "the poll must branch on a failed response"
    assert all("_stopDetailPoll" not in line for line in non_ok), (
        "the detail poll stops on a non-200, so one transient 500 (a locked "
        "database) disables it for the rest of the session"
    )


def test_web_list_poll_keeps_retrying_after_a_transient_server_error():
    """The grid's marker poll must survive a 500 the same way the TUI's does."""
    body = _template_function("_startListPoll")
    assert "if (!resp.ok)" in body, (
        "the grid poll must branch on a failed response; otherwise resp.json() "
        "throws and the read is treated as a stop"
    )
    assert body.count("_listPollTimer = setTimeout(tick, delay)") == 1
    assert body.index("_listPollTimer = setTimeout(tick, delay)") > body.index("catch(e)"), (
        "the re-arm must sit outside the try/catch, so a failed read retries "
        "instead of ending the poll"
    )


#: Drives one 500 tick and the tick after it under node: a failed read must
#: schedule the next tick, and that tick must reach the server again.
POLL_RETRY_DRIVER = """
let _listPollTimer = 1;
let stopped = 0;
const _stopListPoll = () => { stopped += 1; _listPollTimer = null; };
const _applyPending = () => {};
const _applySub = () => {};
const _imageState = () => 'absent';
const _imageCellHtml = () => '';
const wClass = () => '';
const _listNeedsPoll = () => true;
// The tick dispatches every block through the one path; this driver measures
// the retry, so the dispatch is stubbed.
const dispatchItemUpdates = () => {};
function cell() {
  return {
    classList: {contains: (c) => c === 'has-spinner'},
    querySelector: () => null,
    getAttribute: (k) => (k === 'data-wid' ? '11' : null),
  };
}
const grid = {querySelectorAll: () => [cell()]};
global.document = {getElementById: () => grid};
let scheduled = [];
global.setTimeout = (fn, delay) => {
  scheduled.push({fn: fn, delay: delay});
  return scheduled.length;
};
let calls = 0;
global.fetch = async () => {
  calls += 1;
  if (calls === 1) return {ok: false, status: 500, statusText: 'ISE', json: async () => ({})};
  return {ok: true, status: 200, json: async () => []};
};
const fn = (__FN__);
(async () => {
  fn([], '', '');
  stopped = 0;                            // the arm-time stop is not a tick stop
  const first = scheduled.shift();
  await first.fn();                       // the tick that meets the 500
  const armedAfterFailure = scheduled.length;
  const second = scheduled.shift();       // the next tick, which must exist
  if (second) await second.fn();
  console.log(JSON.stringify({calls: calls, armedAfterFailure: armedAfterFailure,
                              stopped: stopped}));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed; cannot exercise the served JavaScript")
def test_web_list_poll_actually_reaches_the_server_again_after_a_500(tmp_path):
    """The property the source check pins, measured on the page's own script."""
    driver = POLL_RETRY_DRIVER.replace(
        "__FN__", _template_function("_startListPoll"))
    script_path = tmp_path / "driver.js"
    script_path.write_text(driver, encoding="utf-8")
    result = subprocess.run([NODE, str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, f"node driver failed:\n{result.stdout}\n{result.stderr}"
    out = json.loads(result.stdout)

    assert out["armedAfterFailure"] == 1, (
        "a 500 must leave the next tick armed; the poll used to stop here"
    )
    assert out["calls"] == 2, "the retry must actually ask the server again"
    assert out["stopped"] == 0, "only the poll's own stop clears the timer"

