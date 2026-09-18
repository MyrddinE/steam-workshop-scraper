"""The crash dump: a traceback that reaches the outbox, with its locals redacted.

When the TUI, the web server or the daemon dies of an unhandled exception the
traceback goes to a terminal nobody is reading. These tests hold the three
escape routes to their promises: a dump is written, the previous console
behaviour is still called, and re-installing does not double-dump.

They also hold the redaction, which is the only barrier now that locals are
included. Every case puts a value where a real crash would: in a local, in a
mapping under a credential-sounding key, in the exception message, or in nothing
the config knows about at all. The redaction is deliberately incomplete -- see
the module docstring -- so the negative cases here are about the values the
process *does* know, not about proving no credential can ever be written.
"""

import hashlib
import json
import logging
import os
import sys
import threading
from unittest.mock import patch

import pytest

from src import crash
from tests.conftest import ASYNC_PAUSE

COOKIE_SECRET = "COOKIE-SECRET-VALUE-0123456789"
CSRF_SECRET = "CSRF-SECRET-VALUE-0123456789"
STEAM_KEY = "STEAM-KEY-VALUE-0123456789"
OPENAI_KEY = "OPENAI-KEY-VALUE-0123456789"
ENV_STEAM_KEY = "ENV-STEAM-KEY-VALUE-0123456789"
ENV_OPENAI_KEY = "ENV-OPENAI-KEY-VALUE-0123456789"
REGISTERED_SECRET = "REGISTERED-SECRET-VALUE-0123456789"


@pytest.fixture(autouse=True)
def _isolate_crash_state():
    """No test may leave crash hooks or a ring buffer on the process.

    The entry-point tests call ``main()``, which installs process-wide hooks;
    without this they leak into every later test.
    """
    crash.uninstall()
    saved_sys = sys.excepthook
    saved_threading = threading.excepthook
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    crash.uninstall()
    sys.excepthook = saved_sys
    threading.excepthook = saved_threading
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _config(tmp_path, *, outbox=True, **extra):
    config = {"database": {"path": "workshop.db"}, "logging": {"level": "INFO"}}
    if outbox:
        config["daemon"] = {"outbox_dir": str(tmp_path / "outbox")}
    config.update(extra)
    return config


def _capture(function, *args, **kwargs):
    """Run ``function`` and return the ``sys.exc_info()`` it raised."""
    try:
        function(*args, **kwargs)
    except Exception:
        return sys.exc_info()
    raise AssertionError("the probe was expected to raise")


def _dump_path(config, function, *, process="tui"):
    return crash.record_exception(*_capture(function), process=process,
                                  config=config, config_path="config.yaml")


def _crash_files(tmp_path):
    return sorted((tmp_path / "outbox" / "crashes").glob("*.txt"))


# ── the dump itself ──────────────────────────────────────────────────────────

def test_the_dump_file_holds_the_traceback_and_the_context(tmp_path):
    config = _config(tmp_path)

    def explode():
        raise RuntimeError("context probe")

    path = _dump_path(config, explode)

    assert path is not None and os.path.isfile(path)
    assert os.path.dirname(path) == str(tmp_path / "outbox" / "crashes")
    assert os.path.basename(path).endswith("-tui-error1.txt")
    text = open(path, encoding="utf-8").read()
    assert "Traceback (most recent call last)" in text
    assert "RuntimeError: context probe" in text
    for key in ("process: tui", "error_occurrence: 1",
                "errors_this_run: 1", "timestamp:", "app_version:", "python:",
                "platform:", "cwd:", "argv:", "config_path: config.yaml",
                "database_path: workshop.db", "thread:", "locals: included",
                "caps:", "secrets_elided:"):
        assert key in text, f"the context header is missing {key!r}"


def test_a_cookie_value_in_a_local_and_in_the_message_is_elided(tmp_path):
    config = _config(tmp_path, session={"login_secure": COOKIE_SECRET,
                                        "id": CSRF_SECRET})

    def explode():
        payload = COOKIE_SECRET
        raise RuntimeError(f"echoed {COOKIE_SECRET} and {CSRF_SECRET}")

    path = _dump_path(config, explode)
    text = open(path, encoding="utf-8").read()

    assert COOKIE_SECRET not in text
    assert CSRF_SECRET not in text
    assert "***" in text
    assert "secrets_elided: none known" not in text


def test_the_configured_steam_and_openai_keys_are_elided(tmp_path):
    config = _config(tmp_path, api={"key": STEAM_KEY},
                     openai={"api_key": OPENAI_KEY})

    def explode():
        first = STEAM_KEY
        second = OPENAI_KEY
        raise RuntimeError(f"{STEAM_KEY} {OPENAI_KEY}")

    path = _dump_path(config, explode)
    text = open(path, encoding="utf-8").read()

    assert STEAM_KEY not in text
    assert OPENAI_KEY not in text


def test_the_environment_api_keys_are_elided(tmp_path, monkeypatch):
    """The loader strips an env key from the config, so the env must be read."""
    monkeypatch.setenv("STEAM_API_KEY", ENV_STEAM_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", ENV_OPENAI_KEY)
    config = _config(tmp_path)

    def explode():
        payload = ENV_STEAM_KEY
        raise RuntimeError(f"env {ENV_STEAM_KEY} {ENV_OPENAI_KEY}")

    path = _dump_path(config, explode)
    text = open(path, encoding="utf-8").read()

    assert ENV_STEAM_KEY not in text
    assert ENV_OPENAI_KEY not in text


def test_a_mapping_local_is_redacted_by_key_name(tmp_path):
    """The value is in no config, so only the key name can remove it."""
    config = _config(tmp_path)

    def explode():
        options = {"api_key": "UNLISTED-VALUE-0123456789",
                   "sort_key": "UNLISTED-VALUE-0123456789", "mode": "keep-me",
                   "Cookie": "cookie-value-here", "note": "visible"}
        raise RuntimeError("mapping probe")

    path = _dump_path(config, explode)
    text = open(path, encoding="utf-8").read()

    assert "UNLISTED-VALUE-0123456789" not in text, \
        "a credential-sounding key must be redacted"
    assert "cookie-value-here" not in text
    assert "keep-me" in text, "an ordinary key is left alone"
    assert "visible" in text
    assert "'api_key': '***'" in text


def test_a_huge_local_is_truncated_and_the_dump_is_capped(tmp_path):
    config = _config(tmp_path)

    def explode():
        blob = "A" * 100_000
        raise RuntimeError("big probe")

    path = _dump_path(config, explode)
    text = open(path, encoding="utf-8").read()

    assert "more characters]" in text, "the value must be truncated, not written whole"
    assert "A" * 100_000 not in text
    assert len(text.encode("utf-8")) <= crash.MAX_DUMP_BYTES


def test_an_unrepresentable_local_does_not_break_the_dump(tmp_path):
    config = _config(tmp_path)

    class Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

        __str__ = __repr__

    def explode():
        weird = Hostile()
        raise RuntimeError("hostile probe")

    path = _dump_path(config, explode)

    assert path is not None
    text = open(path, encoding="utf-8").read()
    assert "unrepresentable" in text
    assert "hostile probe" in text


def test_a_registered_secret_is_scrubbed_even_though_no_config_holds_it(tmp_path):
    config = _config(tmp_path)
    crash.install("tui", config, config_path="config.yaml")
    crash.register_secret(REGISTERED_SECRET)

    def explode():
        raise RuntimeError(f"leaked {REGISTERED_SECRET}")

    path = _dump_path(config, explode)
    text = open(path, encoding="utf-8").read()

    assert REGISTERED_SECRET not in text
    assert "***" in text


def test_the_recent_log_lines_are_in_the_dump(tmp_path):
    config = _config(tmp_path)
    root = logging.getLogger()
    saved_level = root.level
    root.setLevel(logging.DEBUG)
    crash.install("tui", config, config_path="config.yaml")
    try:
        logging.getLogger("crash-test").warning("a marker line before the crash")

        def explode():
            raise RuntimeError("log probe")

        path = _dump_path(config, explode)
    finally:
        root.setLevel(saved_level)

    text = open(path, encoding="utf-8").read()
    assert "a marker line before the crash" in text
    assert "--- recent log" in text


# ── destination and manifest ─────────────────────────────────────────────────

def test_the_manifest_gains_a_crash_entry(tmp_path):
    config = _config(tmp_path)

    def explode():
        raise RuntimeError("manifest probe")

    path = _dump_path(config, explode)
    outbox = tmp_path / "outbox"
    manifest = json.loads((outbox / "manifest.json").read_text(encoding="utf-8"))
    entries = [entry for entry in manifest["artifacts"]
               if entry.get("kind") == "crash"]

    assert len(entries) == 1
    entry = entries[0]
    assert entry["path"].startswith("crashes/")
    assert entry["path"].endswith("-tui-error1.txt")
    assert entry["process"] == "tui"
    assert entry["error"] == 1
    payload = (outbox / entry["path"]).read_bytes()
    assert entry["bytes"] == len(payload)
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert path == str(outbox / entry["path"])


def test_no_outbox_writes_beside_the_log_file_and_prints_the_path(tmp_path, capsys):
    log_file = tmp_path / "logs" / "scraper.log"
    config = {"database": {"path": "workshop.db"},
              "logging": {"level": "INFO", "file": str(log_file)}}

    def explode():
        raise RuntimeError("fallback probe")

    path = crash.record_exception(*_capture(explode), process="tui", config=config)

    assert path is not None
    assert os.path.dirname(path) == str(log_file.parent)
    assert os.path.basename(path).endswith("-tui-error1.txt")
    assert path in capsys.readouterr().err, "the path must be printed when it cannot be pulled"


def test_no_outbox_and_no_log_file_uses_the_working_directory(tmp_path, monkeypatch,
                                                              capsys):
    monkeypatch.chdir(tmp_path)
    config = {"database": {"path": "workshop.db"}}

    def explode():
        raise RuntimeError("cwd probe")

    path = crash.record_exception(*_capture(explode), process="web", config=config)

    assert path is not None
    assert os.path.dirname(os.path.abspath(path)) == str(tmp_path)
    assert os.path.basename(path).endswith("-web-error1.txt")
    assert path in capsys.readouterr().err


def test_the_dumper_never_raises_when_the_destination_is_unwritable(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("this is a file, not a directory", encoding="utf-8")
    config = {"database": {"path": "workshop.db"},
              "daemon": {"outbox_dir": str(blocker)}}

    def explode():
        raise RuntimeError("unwritable probe")

    with caplog.at_level(logging.ERROR):
        path = crash.record_exception(*_capture(explode), process="tui",
                                      config=config)

    assert path is None, "an unwritable destination returns None, it does not raise"
    assert "Crash dump failed" in caplog.text


# ── the installed hooks ──────────────────────────────────────────────────────

def test_the_installed_sys_excepthook_dumps_and_chains_the_previous_hook(tmp_path):
    config = _config(tmp_path)
    seen = []
    sys.excepthook = lambda *args: seen.append(args)
    crash.install("tui", config, config_path="config.yaml")
    try:
        try:
            raise RuntimeError("hook probe")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert len(seen) == 1, "the previous hook must still run, exactly once"
        assert isinstance(seen[0][1], RuntimeError)
        dumps = _crash_files(tmp_path)
        assert len(dumps) == 1
        assert "hook probe" in dumps[0].read_text(encoding="utf-8")
    finally:
        crash.uninstall()


def test_the_installed_threading_hook_dumps_a_worker_thread(tmp_path):
    config = _config(tmp_path)
    seen = []
    threading.excepthook = lambda args: seen.append(args)
    crash.install("daemon", config, config_path="config.yaml")
    try:
        def explode():
            raise RuntimeError("worker probe")

        thread = threading.Thread(target=explode, name="probe-thread")
        thread.start()
        thread.join()

        assert len(seen) == 1, "the previous threading hook must still run"
        dumps = _crash_files(tmp_path)
        assert len(dumps) == 1
        text = dumps[0].read_text(encoding="utf-8")
        assert "worker probe" in text
        assert "thread: probe-thread" in text
        assert "process: daemon" in text
    finally:
        crash.uninstall()


def test_installing_twice_does_not_double_dump(tmp_path):
    config = _config(tmp_path)
    calls = []
    sys.excepthook = lambda *args: calls.append(args)
    crash.install("tui", config, config_path="config.yaml")
    crash.install("tui", config, config_path="config.yaml")
    try:
        try:
            raise RuntimeError("once probe")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        assert len(_crash_files(tmp_path)) == 1, "the traceback was dumped twice"
        assert len(calls) == 1, "the second install ate the previous hook"
    finally:
        crash.uninstall()


def test_a_second_error_is_not_suppressed_by_the_first(tmp_path):
    """One run can report several errors; each one keeps its own dump."""
    config = _config(tmp_path)

    def first():
        raise RuntimeError("first error")

    def second():
        raise RuntimeError("second error")

    first_path = _dump_path(config, first)
    second_path = _dump_path(config, second)

    assert first_path != second_path
    assert first_path.endswith("-tui-error1.txt")
    assert second_path.endswith("-tui-error2.txt")
    first_text = open(first_path, encoding="utf-8").read()
    second_text = open(second_path, encoding="utf-8").read()
    assert "first error" in first_text
    assert "second error" not in first_text
    assert "second error" in second_text
    assert "errors_this_run: 1" in first_text
    assert "errors_this_run: 2" in second_text


# ── Textual's own path ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_app_handle_exception_writes_the_dump_and_delegates(tmp_path):
    """Textual catches the error itself, so the hooks above never see it.

    ``_handle_exception`` is invoked the way Textual invokes it -- from inside an
    ``except`` block -- because Rich's console traceback requires an active
    exception; calling it cold would raise from ``_fatal_error`` before any
    assertion here ran.
    """
    from src.database import initialize_database
    from src.tui import ScraperApp

    db_path = str(tmp_path / "tui.db")
    initialize_database(db_path)
    outbox = tmp_path / "outbox"
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"},
              "daemon": {"outbox_dir": str(outbox)}}
    crash.install("tui", config, config_path="config.yaml")

    with patch("src.tui.load_config", return_value=config):
        app = ScraperApp()
        error = RuntimeError("textual handler probe")
        with pytest.raises(RuntimeError):
            async with app.run_test() as pilot:
                await pilot.pause(ASYNC_PAUSE)
                try:
                    raise error
                except RuntimeError:
                    app._handle_exception(error)

        # The assertions live *outside* `run_test`: its teardown re-raises the
        # app's own exception, which would replace an assertion failure raised
        # inside the block. `_exception` and `_return_code` are Textual's own
        # bookkeeping, so they prove the delegate call ran.
        assert app._exception is error
        assert app._return_code == 1

        dumps = _crash_files(tmp_path)
        assert len(dumps) == 1
        assert "textual handler probe" in dumps[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_every_handle_exception_call_writes_its_own_dump(tmp_path):
    """Textual is not gated to the first error, so neither is the reporter.

    A normal run prints only the first renderable, which is why the second
    traceback used to vanish entirely. Both must reach the outbox.
    """
    from src.database import initialize_database
    from src.tui import ScraperApp

    db_path = str(tmp_path / "tui.db")
    initialize_database(db_path)
    outbox = tmp_path / "outbox"
    config = {"database": {"path": db_path}, "logging": {"level": "INFO"},
              "daemon": {"outbox_dir": str(outbox)}}
    crash.install("tui", config, config_path="config.yaml")

    with patch("src.tui.load_config", return_value=config):
        app = ScraperApp()
        first = RuntimeError("first textual error")
        second = RuntimeError("second textual error")
        with pytest.raises(RuntimeError):
            async with app.run_test() as pilot:
                await pilot.pause(ASYNC_PAUSE)
                for error in (first, second):
                    try:
                        raise error
                    except RuntimeError:
                        app._handle_exception(error)

        # Outside `run_test`, for the same reason as the test above. Textual
        # records only the first error, which is exactly why the second
        # traceback only exists in the file this reporter wrote.
        assert app._exception is first
        assert app._return_code == 1
        dumps = _crash_files(tmp_path)
        assert len(dumps) == 2, "a later error must not be suppressed"
        names = sorted(dump.name for dump in dumps)
        assert names[0].endswith("-error1.txt")
        assert names[1].endswith("-error2.txt")
        texts = [dump.read_text(encoding="utf-8") for dump in dumps]
        assert any("first textual error" in text for text in texts)
        assert any("second textual error" in text for text in texts)
