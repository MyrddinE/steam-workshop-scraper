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
def _isolate_crash_state(tmp_path, monkeypatch):
    """No test may leave crash hooks or a ring buffer on the process.

    The entry-point tests call ``main()``, which installs process-wide hooks;
    without this they leak into every later test. ``APP_DIR`` is pointed at this
    test's tmp_path too: adoption scans the application folder for stranded
    dumps, and a real dump left in the checkout must never inflate another test's
    count or be moved out from under the operator.
    """
    # `raising=False` keeps the fixture harmless while APP_DIR does not exist yet,
    # so a new test fails against the old code for its own reason rather than on
    # the fixture.
    monkeypatch.setattr(crash, "APP_DIR", str(tmp_path / "app"), raising=False)
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
    return crash.record_exception(*_capture(function), process_name=process,
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
                "errors_this_run: 1", "captured_at:", "app_version:", "python:",
                "platform:", "cwd:", "argv:", "config_path: config.yaml",
                "database_path: workshop.db", "thread:", "locals: included",
                "caps:", "secrets_elided:"):
        assert key in text, f"the context header is missing {key!r}"
    # Legacy alias, kept for one release so a parser that only knows the old
    # spelling still reads a dump from this build.
    assert "timestamp:" in text


def test_a_cookie_value_in_a_local_and_in_the_message_is_elided(tmp_path):
    config = _config(tmp_path, session={"login_secure": COOKIE_SECRET,
                                        "csrf_token": CSRF_SECRET})

    def explode():
        payload = COOKIE_SECRET
        raise RuntimeError(f"echoed {COOKIE_SECRET} and {CSRF_SECRET}")

    path = _dump_path(config, explode)
    text = open(path, encoding="utf-8").read()

    assert COOKIE_SECRET not in text
    assert CSRF_SECRET not in text
    assert "***" in text
    assert "secrets_elided: none known" not in text


@pytest.mark.parametrize("spelling", ["csrf_token", "id"])
def test_a_session_token_key_is_still_elided(tmp_path, monkeypatch, spelling):
    """The CSRF token must be elided under either spelling of the config key.

    `session.id` is the deprecated spelling of `session.csrf_token`. The secret
    collector must know a value under either name, or a dump from an
    un-migrated (or half-migrated) config would write the CSRF token out
    verbatim. The cookie read is stubbed out so only the config read can supply
    the secret, pinning the config key itself.
    """
    config = _config(tmp_path, session={spelling: CSRF_SECRET})
    monkeypatch.setattr(crash, "_cookie_secrets", lambda config: [])

    def explode():
        raise RuntimeError(f"echoed {CSRF_SECRET}")

    path = _dump_path(config, explode)
    text = open(path, encoding="utf-8").read()

    assert CSRF_SECRET not in text
    assert "***" in text


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
    assert entry["error_occurrence"] == 1
    # Legacy alias, kept for one release for the out-of-repo puller.
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

    path = crash.record_exception(*_capture(explode), process_name="tui", config=config)

    assert path is not None
    assert os.path.dirname(path) == str(log_file.parent)
    assert os.path.basename(path).endswith("-tui-error1.txt")
    assert path in capsys.readouterr().err, "the path must be printed when it cannot be pulled"


def test_no_outbox_and_no_log_file_falls_back_to_the_application_folder(
        tmp_path, monkeypatch, capsys):
    """The last resort is the app folder, never the process working directory.

    The daemon under a scheduled task does not run with the application folder as
    its cwd, so a cwd dump can land where the operator will never look. The
    fallback is derived from this module's own location instead, and its path is
    still printed so a local dump is findable.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    app_folder = tmp_path / "app"
    # `raising=False` keeps this harmless while APP_DIR does not exist yet, so
    # the test fails against the old code for the right reason -- the dump lands
    # in the working directory -- rather than on a missing attribute.
    monkeypatch.setattr(crash, "APP_DIR", str(app_folder), raising=False)
    config = {"database": {"path": "workshop.db"}}

    def explode():
        raise RuntimeError("app folder probe")

    path = crash.record_exception(*_capture(explode), process_name="web", config=config)

    assert path is not None
    assert os.path.dirname(os.path.abspath(path)) == str(app_folder)
    assert os.path.basename(path).endswith("-web-error1.txt")
    assert path in capsys.readouterr().err
    assert list(elsewhere.iterdir()) == [], "the working directory was used"


def test_the_dumper_never_raises_when_the_destination_is_unwritable(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("this is a file, not a directory", encoding="utf-8")
    config = {"database": {"path": "workshop.db"},
              "daemon": {"outbox_dir": str(blocker)}}

    def explode():
        raise RuntimeError("unwritable probe")

    with caplog.at_level(logging.ERROR):
        path = crash.record_exception(*_capture(explode), process_name="tui",
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


def test_a_failure_before_logging_is_configured_still_writes_a_dump(
        tmp_path, monkeypatch, capsys):
    """The hooks go in before logging, so a logging-setup crash is dumped.

    The entry points used to configure logging and only then call
    ``crash.install``, so an exception raised *inside* the logging setup was
    captured nowhere: no log record (the handlers were the thing being built),
    no ring buffer, and no dump, because the hooks were not installed yet. This
    runs the early entry point with no logging configured at all and asserts both
    the local dump and the path printed for the operator.
    """
    app_folder = tmp_path / "app"
    monkeypatch.setattr(crash, "APP_DIR", str(app_folder))
    crash.install_hooks("tui")

    # The early call installs the hooks only. The ring buffer waits for
    # `install` after `basicConfig`, so a forced configuration cannot drop it.
    assert not [h for h in logging.getLogger().handlers
                if isinstance(h, crash.RecentLogHandler)]

    try:
        raise RuntimeError("crash while configuring logging")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    dumps = sorted(app_folder.glob("*.txt"))
    assert len(dumps) == 1, "the startup failure was not dumped"
    text = dumps[0].read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "crash while configuring logging" in text
    assert "process: tui" in text
    # The outbox is unknown yet, so the local path is the operator's only lead.
    assert str(dumps[0]) in capsys.readouterr().err


def test_early_hooks_then_install_leave_exactly_one_ring_buffer(tmp_path, monkeypatch):
    """The two-step install adds one ring buffer, not two, and chains once.

    ``install_hooks`` runs before logging and ``install`` after it; the forced
    ``basicConfig`` in between clears the root handlers, so this is the
    re-attach path in ``_attach_handler`` putting the single handler back.
    Repeated calls must not attach a second buffer or chain the excepthook onto
    itself (which would dump the same traceback twice).
    """
    monkeypatch.setattr(crash, "APP_DIR", str(tmp_path / "app"))
    config = _config(tmp_path)
    crash.install_hooks("tui")
    crash.install_hooks("tui")
    logging.basicConfig(level=logging.INFO, force=True)
    crash.install("tui", config, config_path="config.yaml")
    crash.install("tui", config, config_path="config.yaml")

    ring = [h for h in logging.getLogger().handlers
            if isinstance(h, crash.RecentLogHandler)]
    assert len(ring) == 1, "the ring buffer was attached more than once"

    try:
        raise RuntimeError("single-dump probe")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    assert len(_crash_files(tmp_path)) == 1, "the excepthook was chained twice"


# ── adopting what the early fallback caught ──────────────────────────────────

def _strand_a_dump(tmp_path, monkeypatch, message):
    """Write one dump through the early hook, into an isolated app folder."""
    app_folder = tmp_path / "app"
    monkeypatch.setattr(crash, "APP_DIR", str(app_folder))
    crash.install_hooks("tui")

    def explode():
        raise RuntimeError(message)

    try:
        explode()
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    stranded = sorted(app_folder.glob("*.txt"))
    assert len(stranded) == 1, "the early dump was not written to the fallback"
    return app_folder, stranded[0]


def test_a_stranded_dump_is_adopted_when_the_outbox_becomes_known(
        tmp_path, monkeypatch):
    """The dump the early fallback caught is moved in and registered.

    A crash before ``load_config`` returns cannot know ``daemon.outbox_dir``, so
    its dump lands locally and its path is printed. When ``install(config)`` runs
    the outbox is known, and the dump is moved into it and registered, so the
    puller collects it instead of leaving it stranded.
    """
    app_folder, stranded = _strand_a_dump(tmp_path, monkeypatch, "stranded probe")
    config = _config(tmp_path)

    crash.install("tui", config, config_path="config.yaml")

    outbox = tmp_path / "outbox"
    crashes = outbox / "crashes"
    adopted = list(crashes.glob("*.txt"))
    assert len(adopted) == 1
    assert "stranded probe" in adopted[0].read_text(encoding="utf-8")
    assert list(app_folder.glob("*.txt")) == [], "the local dump was left behind"

    manifest = json.loads((outbox / "manifest.json").read_text(encoding="utf-8"))
    entries = [entry for entry in manifest["artifacts"]
               if entry.get("kind") == "crash"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["path"] == f"crashes/{adopted[0].name}"
    payload = adopted[0].read_bytes()
    assert entry["bytes"] == len(payload)
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()

    # Idempotent: a second install finds nothing left to adopt or re-register.
    crash.install("tui", config, config_path="config.yaml")
    assert len(list(crashes.glob("*.txt"))) == 1
    manifest = json.loads((outbox / "manifest.json").read_text(encoding="utf-8"))
    assert len([entry for entry in manifest["artifacts"]
                if entry.get("kind") == "crash"]) == 1


def test_adoption_leaves_files_it_did_not_write_alone(tmp_path, monkeypatch):
    """Only this module's dump filenames are adopted, never a broad sweep."""
    app_folder = tmp_path / "app"
    app_folder.mkdir()
    monkeypatch.setattr(crash, "APP_DIR", str(app_folder))
    unrelated = app_folder / "operator-notes.txt"
    unrelated.write_text("operator notes\n", encoding="utf-8")
    # Right suffix, wrong shape: only a real dump has the UTC stamp prefix.
    near_miss = app_folder / "other-error1.txt"
    near_miss.write_text("not ours\n", encoding="utf-8")
    # An interrupted atomic write leaves a .tmp that must not be adopted.
    leftover = app_folder / (crash._dump_filename("tui", 1) + ".tmp")
    leftover.write_text("partial", encoding="utf-8")

    crash.install("tui", _config(tmp_path), config_path="config.yaml")

    assert unrelated.read_text(encoding="utf-8") == "operator notes\n"
    assert near_miss.read_text(encoding="utf-8") == "not ours\n"
    assert leftover.read_text(encoding="utf-8") == "partial"
    crashes = tmp_path / "outbox" / "crashes"
    assert not crashes.exists() or list(crashes.iterdir()) == []


def test_adoption_never_overwrites_an_existing_outbox_dump(tmp_path, monkeypatch):
    app_folder, stranded = _strand_a_dump(tmp_path, monkeypatch, "collision probe")
    crashes = tmp_path / "outbox" / "crashes"
    crashes.mkdir(parents=True)
    existing = crashes / stranded.name
    existing.write_text("already here\n", encoding="utf-8")

    crash.install("tui", _config(tmp_path), config_path="config.yaml")

    assert existing.read_text(encoding="utf-8") == "already here\n"
    adopted = [path for path in crashes.iterdir() if path != existing]
    assert len(adopted) == 1
    assert "collision probe" in adopted[0].read_text(encoding="utf-8")
    assert list(app_folder.glob("*.txt")) == []


def test_adoption_never_raises_when_the_outbox_is_unwritable(
        tmp_path, monkeypatch, caplog):
    app_folder, _ = _strand_a_dump(tmp_path, monkeypatch, "unwritable adoption")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    config = {"database": {"path": "workshop.db"},
              "daemon": {"outbox_dir": str(blocker)}}

    # Must not raise: adopting is best-effort, and the dump stays findable where
    # the printed path says it is.
    crash.install("tui", config, config_path="config.yaml")

    assert len(list(app_folder.glob("*.txt"))) == 1


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
