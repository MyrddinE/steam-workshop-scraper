"""Crash dumps that reach the outbox before the process drops to the console.

When the TUI, the web server or the daemon dies of an unhandled exception, the
traceback goes to a terminal nobody is reading. This module turns that traceback
into a small text file and publishes it through the same pull-outbox manifest the
database snapshots and the failure captures use, so the maintainer's existing
sync collects it with no changes.

Three escape routes have to be covered, because each one stops a different
exception from ever reaching the others:

* :data:`sys.excepthook` -- an exception raised outside any event loop;
* :data:`threading.excepthook` -- an exception that killed a worker thread;
* Textual's ``App._handle_exception`` -- the common case for the TUI, which
  catches the error in its message pump and workers and renders its own Rich
  traceback, so neither hook above ever sees it. The TUI overrides that method
  to write the dump before delegating.

One error is not the whole story: Textual calls ``_handle_exception`` once per
unhandled error and keeps every one of them (a normal run prints only the first,
which is why ``--dev`` says "N errors shown"). Every call therefore gets its own
file, numbered in the order the process reported them
(``<stamp>-<process>-error<N>.txt``), and each dump is written synchronously --
Textual closes the message loop as it exits, so anything handed to the event loop
would never run. Nothing here suppresses a second error; the only install-time
guarantee is that re-installing the hook does not chain it onto itself.

The file holds the traceback **with each frame's locals**, because the values in
play are usually the whole answer, and a Rich console rendering is gone the
moment the terminal scrolls. Locals are the most dangerous thing this project
writes to disk, so they are guarded three ways before they are rendered:

* every mapping entry whose key names a credential -- ``cookie``, ``token``,
  ``secret``, ``password``, ``passwd``, ``credential``, ``login``, ``sessionid``
  or anything ending in ``key`` -- has its value replaced with ``***``, and so
  does a local variable with such a name;
* every value the process knows about is gathered first (the current cookie set,
  the configured ``sessionid``/``steamLoginSecure`` in both accepted forms, the
  Steam and OpenAI API keys from the config *and* the environment, plus anything
  registered at runtime through :func:`register_secret`) and the finished text is
  scrubbed of those literals, longest first, exactly as the web capture does;
* values are truncated and the whole dump is capped, so a huge or hostile local
  cannot make the reporter the second thing to fail.

**The redaction is not complete, and the file must be treated accordingly.** It
is key-name based and known-value based: a credential the process never
registered, the config does not hold, and no key name describes would still be
written. The dump therefore goes only to the operator's own outbox -- it is
pulled by their sync and is never published anywhere -- and that containment is
the reason locals are included at all.

The reporter itself never raises: a crash reporter that crashes is worse than
none. Every failure is logged and swallowed, and the writer returns ``None``
rather than propagating.
"""

import hashlib
import logging
import os
import platform
import re
import sys
import threading
import traceback
from collections import deque
from datetime import datetime, timezone

from src import capture
from src.backup import update_manifest
from src.config import configured_outbox_dir
from src.session_cookie import ENCODED_SEPARATOR

# The directory inside the outbox, and the manifest kind the puller filters on.
CRASHES_DIR_NAME = "crashes"
CRASH_KIND = "crash"

# How many formatted log records the ring buffer keeps. What happened just
# before the crash is the difference between guessing and knowing, and this path
# is rarely exercised.
RECENT_LOG_RECORDS = 200

# Rendered-locals caps. Locals can be enormous or hostile, so nothing here is
# unbounded: a value, the number of values in one frame, how deep the redactor
# follows a container, and the size of the whole file.
MAX_LOCAL_VALUE_CHARS = 2000
MAX_LOCALS_PER_FRAME = 50
MAX_CONTAINER_ITEMS = 20
MAX_LOCAL_DEPTH = 3
MAX_LOCAL_NODES = 20000
MAX_LOG_RECORD_CHARS = 4000
MAX_DUMP_BYTES = 256 * 1024

# The string a redacted value becomes, matching the capture's elision.
REDACTED = "***"

# How many runtime-registered secrets are kept. Only recent ones can still be
# live credentials, and the point of the bound is that a long run cannot grow.
REGISTERED_SECRET_LIMIT = 8

# A mapping key naming any of these is redacted whole. `endswith("key")` catches
# `api_key`; overriding a benign `sort_key` costs a little context, and the
# instruction is to bias toward redaction.
_SENSITIVE_KEY_PARTS = (
    "cookie", "token", "secret", "password", "passwd", "credential",
    "login", "sessionid",
)

_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

# Guards the module state: the installed hooks, the context, the ring-buffer
# handler and the registered secrets. An RLock because register_secret can be
# reached from inside a logging handler, which the lock-holder may be driving.
_STATE_LOCK = threading.RLock()

# Serialises whole dumps. A crash in one thread must not interleave its manifest
# read-modify-write with another's; this is held for the IO, never for logging
# or for state.
_DUMP_LOCK = threading.Lock()

# How many errors this process run has reported, so each dump can say which one
# it is. Textual calls `App._handle_exception` once per unhandled error and a
# single error must never suppress a later one's dump, so this counter is the
# only "have we seen this before?" state there is.
_ERROR_COUNT = 0

# Credentials learned at runtime -- a pushed CSRF token, a refreshed
# `steamLoginSecure` -- that may exist nowhere in the config or the profile.
_REGISTERED = deque(maxlen=REGISTERED_SECRET_LIMIT)

_state = {
    "process": None,
    "config": None,
    "config_path": None,
    "handler": None,
    "sys_hook": None,
    "threading_hook": None,
    "prev_sys_hook": None,
    "prev_threading_hook": None,
}


class RecentLogHandler(logging.Handler):
    """Keep the last ``capacity`` formatted records for a crash dump.

    In memory rather than re-read from the log file: the file may not be
    configured at all, may be unreadable from here, and cannot be trusted to
    still be writable at crash time.
    """

    def __init__(self, capacity: int = RECENT_LOG_RECORDS):
        super().__init__()
        self.records = deque(maxlen=max(1, int(capacity)))

    def emit(self, record):
        try:
            self.records.append(self.format(record))
        # A handler must never raise into the logging call that fed it.
        except Exception:
            self.handleError(record)

    def recent_records(self):
        """The buffered records, oldest first."""
        return list(self.records)


def install(process_name, config=None, config_path=None):
    """Install the crash hooks and the recent-log ring buffer.

    ``process_name`` names the entry point (``"tui"``, ``"web"``, ``"daemon"``) and is
    recorded in every dump. ``config`` supplies the outbox and the cookie values
    to elide; it may be ``None`` when the config could not be loaded, which is
    exactly when a crash is likely.

    Idempotent: a second call does not chain the reporter onto itself, which
    would dump the same traceback twice. Whatever hook was installed before is
    remembered and still called, so the console output is unchanged. Call this
    *after* the entry point has configured logging, or the ring-buffer handler
    will be dropped by ``basicConfig(force=True)``.
    """
    try:
        with _STATE_LOCK:
            _state["process"] = process_name
            if config is not None:
                _state["config"] = config
            if config_path is not None:
                _state["config_path"] = config_path
            _attach_handler()
            if sys.excepthook is not _state["sys_hook"]:
                _state["prev_sys_hook"] = sys.excepthook
                _state["sys_hook"] = _sys_excepthook
                sys.excepthook = _sys_excepthook
            if threading.excepthook is not _state["threading_hook"]:
                _state["prev_threading_hook"] = threading.excepthook
                _state["threading_hook"] = _threading_excepthook
                threading.excepthook = _threading_excepthook
    except Exception:
        _log_own_failure("Could not install the crash reporter")


def uninstall():
    """Undo :func:`install` in this process.

    The entry points never call this; it exists so a test can put the process
    back the way it found it. It restores the previous hooks only while ours is
    still the installed one, so it cannot clobber a hook someone set later.
    """
    global _ERROR_COUNT
    with _STATE_LOCK:
        if _state["sys_hook"] is not None and sys.excepthook is _state["sys_hook"]:
            sys.excepthook = _state["prev_sys_hook"] or sys.__excepthook__
        if (_state["threading_hook"] is not None
                and threading.excepthook is _state["threading_hook"]):
            threading.excepthook = (_state["prev_threading_hook"]
                                    or threading.__excepthook__)
        handler = _state["handler"]
        if handler is not None:
            logging.getLogger().removeHandler(handler)
        _state.update(process=None, config=None, config_path=None, handler=None,
                      sys_hook=None, threading_hook=None,
                      prev_sys_hook=None, prev_threading_hook=None)
        _ERROR_COUNT = 0
        _REGISTERED.clear()


def register_secret(value):
    """Remember a credential the process learned at runtime, for the scrub.

    The pushed CSRF token and a refreshed ``steamLoginSecure`` exist nowhere in
    the config and may match nothing in the browser profile, so a crash inside
    the route that just stored one would otherwise dump it verbatim. Callers are
    ordinary request handlers: this never raises, and only the most recent
    handful of values are kept.
    """
    try:
        if value is None:
            return
        text = str(value)
        if not text:
            return
        with _STATE_LOCK:
            if text in _REGISTERED:
                _REGISTERED.remove(text)
            _REGISTERED.append(text)
    except BaseException:  # noqa: BLE001 - see docstring
        _log_own_failure("Could not register a secret for crash-dump elision")


def record_exception(exc_type, exc_value, exc_tb, *, process_name=None, config=None,
                     config_path=None):
    """Write one crash dump and return its path, or ``None`` on any failure.

    Never raises. ``process_name`` and ``config`` default to whatever :func:`install`
    recorded, so a hook can call this with only the exception.

    Every call writes a dump. Textual calls ``App._handle_exception`` once per
    unhandled error -- it does not stop at the first -- and a later error must
    never be suppressed by an earlier one, so each file is numbered in the order
    this process run reported them (``<stamp>-<process>-error<N>.txt``) and the
    header says how many errors had been reported when it was written.
    """
    try:
        return _record_exception(exc_type, exc_value, exc_tb, process=process_name,
                                 config=config, config_path=config_path)
    except BaseException:  # noqa: BLE001 - see docstring
        _log_own_failure("Crash dump failed")
        return None


# ── the hooks ────────────────────────────────────────────────────────────────

def _sys_excepthook(exc_type, exc_value, exc_tb):
    _safe_record(exc_type, exc_value, exc_tb)
    previous = _state["prev_sys_hook"]
    # The console still shows the crash exactly as it did before: whatever hook
    # was there is called afterwards, and ours reports without raising.
    if previous is not None and previous is not _sys_excepthook:
        previous(exc_type, exc_value, exc_tb)


def _threading_excepthook(args):
    _safe_record(args.exc_type, args.exc_value, args.exc_traceback)
    previous = _state["prev_threading_hook"]
    if previous is not None and previous is not _threading_excepthook:
        previous(args)


def _safe_record(exc_type, exc_value, exc_tb):
    """``record_exception``, but the previous hook is called even if it is not."""
    try:
        return record_exception(exc_type, exc_value, exc_tb)
    except BaseException:  # noqa: BLE001 - belt to record_exception's braces
        _log_own_failure("Crash dump failed")
        return None


# ── the dump ─────────────────────────────────────────────────────────────────

def _record_exception(exc_type, exc_value, exc_tb, *, process, config, config_path):
    global _ERROR_COUNT
    with _DUMP_LOCK:
        with _STATE_LOCK:
            resolved_process = _safe_process_name(process or _state["process"])
            resolved_config = config if config is not None else _state["config"]
            resolved_config_path = (config_path if config_path is not None
                                    else _state["config_path"])
            _ERROR_COUNT += 1
            occurrence = _ERROR_COUNT

        # Written synchronously, before anything returns to an event loop that
        # Textual is about to close: a dump deferred to a callback may never run.
        # Log first: the log file the TUI already writes is a second place the
        # maintainer reads, and the ring buffer below is the third.
        _log_crash(exc_type, exc_value, exc_tb, resolved_process)

        text = _render_dump(exc_type, exc_value, exc_tb, resolved_process,
                            resolved_config, resolved_config_path, occurrence)
        directory, outbox, should_print_path = _destination(resolved_config)
        path = os.path.join(directory,
                            _dump_filename(resolved_process, occurrence))
        payload = text.encode("utf-8", "replace")
        _write_atomic(path, payload)
        if outbox:
            _register_dump(outbox, path, payload, resolved_process, occurrence)
        elif should_print_path:
            _print_path(path)
        return path


def _log_crash(exc_type, exc_value, exc_tb, process):
    try:
        logging.error("Unhandled %s in the %s process",
                      getattr(exc_type, "__name__", "exception"), process,
                      exc_info=(exc_type, exc_value, exc_tb))
    except BaseException:  # noqa: BLE001 - logging must never break the dump
        pass


def _render_dump(exc_type, exc_value, exc_tb, process, config, config_path,
                 occurrence):
    secrets = _gather_secrets(config)
    elision = (f"{len(secrets)} known value(s) scrubbed" if secrets
               else "none known (redaction is by key name only)")
    tb_text, locals_note = _format_traceback(exc_type, exc_value, exc_tb, secrets)
    log_lines = _recent_log_records()

    def header(truncated):
        return _context_lines(exc_type, process, config, config_path, elision,
                              locals_note, truncated, occurrence)

    text = _scrub(_assemble(header(False), tb_text, log_lines), secrets)
    if len(text.encode("utf-8", "replace")) > MAX_DUMP_BYTES:
        # Re-render with the flag set, then keep a hard byte cap and make the
        # cut on a character boundary so the file never ends mid-character.
        text = _scrub(_assemble(header(True), tb_text, log_lines), secrets)
        marker = f"\n[dump truncated: only the first {MAX_DUMP_BYTES} bytes are kept]\n"
        room = MAX_DUMP_BYTES - len(marker.encode("utf-8"))
        head = text.encode("utf-8", "replace")[:room]
        text = head.decode("utf-8", "ignore") + marker
    return text


def _assemble(header_lines, tb_text, log_lines):
    parts = list(header_lines)
    parts.append("")
    parts.append("--- traceback (locals included; values capped) ---")
    parts.append(tb_text.rstrip("\n"))
    parts.append("")
    if log_lines:
        parts.append(f"--- recent log (last {len(log_lines)} record(s)) ---")
        parts.extend(log_lines)
    else:
        parts.append("--- recent log (none) ---")
    parts.append("")
    return "\n".join(parts)


def _context_lines(exc_type, process, config, config_path, elision, locals_note,
                   truncated, occurrence):
    database_path = ""
    if isinstance(config, dict):
        database = config.get("database")
        if isinstance(database, dict):
            database_path = database.get("path", "")
    captured_at = _utc_now_iso()
    return [
        "# steam-workshop-scraper crash dump",
        f"process: {process}",
        f"error_occurrence: {occurrence}",
        f"errors_this_run: {occurrence} (at the time of writing; a later crash "
        "would raise this)",
        f"captured_at: {captured_at}",
        # Legacy alias for one release: a parser that only knows `timestamp:`
        # (a tool not yet updated for the rename) still finds it, and a dump
        # written by an older build carries `timestamp:` alone, so a reader
        # must accept either spelling.
        f"timestamp: {captured_at}",
        f"app_version: {capture.app_version()}",
        f"python: {platform.python_version()} ({platform.python_implementation()})",
        f"platform: {platform.platform()}",
        f"cwd: {_safe_cwd()}",
        f"argv: {_safe_argv()}",
        f"config_path: {config_path or ''}",
        f"database_path: {database_path}",
        f"thread: {threading.current_thread().name}",
        f"exception: {getattr(exc_type, '__name__', None) or 'unknown'}",
        f"locals: {locals_note}",
        f"caps: value<={MAX_LOCAL_VALUE_CHARS} chars, "
        f"{MAX_LOCALS_PER_FRAME} locals/frame, dump<={MAX_DUMP_BYTES} bytes",
        f"secrets_elided: {elision}",
        f"truncated: {'yes' if truncated else 'no'}",
    ]


# ── the traceback, with locals ───────────────────────────────────────────────

def _format_traceback(exc_type, exc_value, exc_tb, secrets):
    """``(traceback text, locals note)`` for one exception.

    The stdlib renders the traceback -- including the ``^^^^`` markers -- but its
    ``capture_locals`` has already turned each local into a repr string by the
    time the frame summaries exist (``FrameSummary.__init__``), and a repr cannot
    be redacted by key any more. The locals are therefore taken from the raw
    frames here and installed on the summaries before the format call, so the
    redaction sees the mapping while it is still a mapping, and a value that
    cannot be represented can never break the render.
    """
    if exc_tb is None:
        exc_tb = getattr(exc_value, "__traceback__", None)
    try:
        te = traceback.TracebackException(exc_type, exc_value, exc_tb)
    except BaseException:  # noqa: BLE001 - locals are a bonus, the traceback is not
        return _plain_traceback(exc_type, exc_value, exc_tb), \
            "unavailable (could not be captured)"

    budget = [MAX_LOCAL_NODES]
    _install_locals(te, exc_value, exc_tb, secrets, budget, set())
    try:
        return "".join(te.format(chain=True)), "included (redacted and capped)"
    except BaseException:  # noqa: BLE001 - fall back rather than lose the dump
        return _plain_traceback(exc_type, exc_value, exc_tb), \
            "unavailable (rendering failed)"


def _install_locals(te, exc, tb, secrets, budget, seen):
    """Install redacted, capped locals on every frame a traceback will render.

    Follows the same cause/context/group graph the formatter walks, pairing each
    node with the raw frames of its own traceback.
    """
    if te is None or id(te) in seen:
        return
    seen.add(id(te))
    frames = _frames_of(tb)
    if len(frames) != len(te.stack):
        # The traceback we were handed and the one the exception carries can
        # differ; trust the exception's own when the shapes disagree.
        frames = _frames_of(getattr(exc, "__traceback__", None))
    for summary, frame in zip(te.stack, frames):
        try:
            raw = frame.f_locals
        except BaseException:  # noqa: BLE001 - a cleared frame has no locals
            raw = None
        if raw:
            summary.locals = _redact_locals(raw, secrets, budget)

    cause = getattr(exc, "__cause__", None)
    _install_locals(te.__cause__, cause, getattr(cause, "__traceback__", None),
                    secrets, budget, seen)
    context = getattr(exc, "__context__", None)
    _install_locals(te.__context__, context,
                    getattr(context, "__traceback__", None),
                    secrets, budget, seen)
    groups = getattr(te, "exceptions", None)
    if isinstance(groups, (list, tuple)):
        sub_exceptions = getattr(exc, "exceptions", ()) if exc is not None else ()
        for index, group_te in enumerate(groups):
            sub = sub_exceptions[index] if index < len(sub_exceptions) else None
            _install_locals(group_te, sub, getattr(sub, "__traceback__", None),
                            secrets, budget, seen)


def _frames_of(tb):
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame)
        tb = tb.tb_next
    return frames


def _plain_traceback(exc_type, exc_value, exc_tb):
    try:
        return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    except BaseException:  # noqa: BLE001 - even a hostile exception gets a line
        try:
            return f"{exc_type!r}: {exc_value!r}\n"
        except BaseException:
            return "<unrepresentable exception>\n"


def _redact_locals(locals_map, secrets, budget):
    """One frame's locals as display strings, redacted and bounded."""
    items = list(locals_map.items())
    rendered = {}
    for index, (name, value) in enumerate(items):
        if index >= MAX_LOCALS_PER_FRAME:
            rendered[f"<{len(items) - index} more locals omitted>"] = ""
            break
        if _sensitive_key(name):
            # A local named `token` or `steamLoginSecure` is the credential, so
            # the name alone is enough to redact it.
            rendered[str(name)] = REDACTED
            continue
        rendered[str(name)] = _redact_and_render(value, secrets, 0, set(), budget)
    return rendered


def _redact_and_render(value, secrets, depth, seen, budget):
    """A redacted, bounded, already-rendered copy of one local value.

    Mappings are redacted by key name, containers are followed so a mapping
    cannot hide one level down, and every leaf is rendered to a string here so a
    single unrepresentable member cannot take the whole frame with it. The result
    is always a string, which is what ``FrameSummary`` renders.
    """
    if depth > MAX_LOCAL_DEPTH:
        return "..."
    if budget[0] <= 0:
        return "..."
    budget[0] -= 1

    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            return "<recursive>"
        seen.add(marker)
        try:
            items = list(value.items())
        except BaseException:  # noqa: BLE001 - a hostile mapping still gets a repr
            return _render_scalar(value, secrets)
        parts = []
        for index, (key, item) in enumerate(items):
            if index >= MAX_CONTAINER_ITEMS:
                parts.append(f"... <{len(items) - index} more>")
                break
            safe_key = _safe_key(key)
            if _sensitive_key(key):
                parts.append(f"{safe_key!r}: {REDACTED!r}")
            else:
                parts.append(f"{safe_key!r}: "
                             f"{_redact_and_render(item, secrets, depth + 1, seen, budget)}")
        seen.discard(marker)
        return "{" + ", ".join(parts) + "}"

    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in seen:
            return "<recursive>"
        seen.add(marker)
        parts = []
        for index, item in enumerate(value):
            if index >= MAX_CONTAINER_ITEMS:
                try:
                    parts.append(f"... <{len(value) - index} more>")
                except BaseException:  # noqa: BLE001
                    parts.append("... <more>")
                break
            parts.append(_redact_and_render(item, secrets, depth + 1, seen, budget))
        seen.discard(marker)
        if isinstance(value, tuple):
            if len(parts) == 1:
                return "(" + parts[0] + ",)"
            return "(" + ", ".join(parts) + ")"
        return "[" + ", ".join(parts) + "]"

    if isinstance(value, (set, frozenset)):
        try:
            return f"<set of {len(value)}>"
        except BaseException:  # noqa: BLE001
            return "<set>"

    return _render_scalar(value, secrets)


def _render_scalar(value, secrets):
    """A truncated ``repr`` with the known literals scrubbed from it."""
    try:
        text = repr(value)
    except BaseException:  # noqa: BLE001 - one bad repr must not lose the dump
        return "<unrepresentable>"
    if not isinstance(text, str):
        return "<unrepresentable>"
    try:
        if secrets:
            text = capture.scrub_text(text, secrets)
    except BaseException:  # noqa: BLE001 - the final whole-text scrub still runs
        return "<unrepresentable>"
    if len(text) > MAX_LOCAL_VALUE_CHARS:
        omitted = len(text) - MAX_LOCAL_VALUE_CHARS
        text = text[:MAX_LOCAL_VALUE_CHARS] + f"... [{omitted} more characters]"
    return text


def _safe_key(key):
    if isinstance(key, str):
        return key
    try:
        return repr(key)
    except BaseException:  # noqa: BLE001
        return "<unprintable key>"


def _sensitive_key(name):
    lowered = str(name).lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS) \
        or lowered.endswith("key")


# ── secrets ──────────────────────────────────────────────────────────────────

def _gather_secrets(config):
    """Every literal credential value the process currently knows about."""
    values = []
    with _STATE_LOCK:
        values.extend(_REGISTERED)
    values.extend(_config_secrets(config))
    values.extend(_cookie_secrets(config))
    try:
        return capture.ordered_secrets(values)
    except BaseException:  # noqa: BLE001 - unordered is still safer than none
        return values


def _config_secrets(config):
    if not isinstance(config, dict):
        return []
    values = []
    session = config.get("session")
    if isinstance(session, dict):
        # Register both spellings: this collector's job is to know every secret
        # the process holds, and a value under either name must be elided. The
        # alias reader elsewhere picks one; a scrubber must not drop the other.
        _add_secret_value(values, session.get("csrf_token"))
        _add_secret_value(values, session.get("id"))
        _add_login_secure(values, session.get("login_secure"))
        try:
            from src.config import login_secure_value
            _add_login_secure(values, login_secure_value(config))
        except Exception as exc:  # noqa: BLE001 - best-effort
            logging.debug("Crash dump could not normalise login_secure: %s", exc)
    for section, keys in (("api", ("key",)), ("openai", ("api_key",))):
        block = config.get(section)
        if isinstance(block, dict):
            for key in keys:
                _add_secret_value(values, block.get(key))
    # The loader strips an env-derived key from the config before saving, so the
    # environment has to be read here as well or a crash would dump it verbatim.
    _add_secret_value(values, os.environ.get("STEAM_API_KEY"))
    _add_secret_value(values, os.environ.get("OPENAI_API_KEY"))
    return values


def _cookie_secrets(config):
    """The values of the cookie set the requests actually send."""
    if not isinstance(config, dict) or not config:
        return []
    try:
        from src.web_scraper import _build_workshop_cookies
        cookies = _build_workshop_cookies(config)
    except Exception as exc:  # noqa: BLE001 - a dump with no cookies is still a dump
        logging.debug("Crash dump could not read cookies for elision: %s", exc)
        return []
    values = []
    if isinstance(cookies, dict):
        for value in cookies.values():
            _add_secret_value(values, value)
        _add_login_secure(values, cookies.get("steamLoginSecure"))
    return values


def _add_login_secure(values, value):
    """The cookie value, both accepted separator forms, and its token half."""
    if isinstance(value, (list, tuple)):
        _add_secret_value(values, "||".join(str(part) for part in value))
        _add_secret_value(values, ENCODED_SEPARATOR.join(str(part) for part in value))
        return
    if value is None:
        return
    try:
        text = str(value)
    except BaseException:  # noqa: BLE001
        return
    if not text:
        return
    _add_secret_value(values, text)
    if ENCODED_SEPARATOR in text:
        _add_secret_value(values, text.replace(ENCODED_SEPARATOR, "||"))
    elif "||" in text:
        _add_secret_value(values, text.replace("||", ENCODED_SEPARATOR))
    try:
        from src.session_cookie import parse
        _add_secret_value(values, parse(text).token)
    except Exception as exc:  # noqa: BLE001 - best-effort
        logging.debug("Crash dump could not parse login_secure: %s", exc)


def _add_secret_value(values, value):
    if value is None:
        return
    try:
        text = str(value)
    except BaseException:  # noqa: BLE001
        return
    if text:
        values.append(text)


def _scrub(text, secrets):
    if not secrets:
        return text
    try:
        return capture.scrub_text(text, secrets)
    except BaseException:  # noqa: BLE001 - a failing scrub must not lose the dump
        _log_own_failure("Could not scrub the crash dump")
        return text


# ── destination and writing ──────────────────────────────────────────────────

def _destination(config):
    """``(directory, outbox or None, should_print_path)`` for this dump.

    The outbox is the configured ``daemon.outbox_dir`` (or the legacy
    ``backup_dir``). When it cannot be determined the dump goes beside the
    configured log file, else the working directory, and its path is printed --
    a dump the user cannot find is not a dump.
    """
    outbox = _configured_outbox_dir(config)
    if outbox:
        return os.path.join(outbox, CRASHES_DIR_NAME), outbox, False
    log_file = ""
    if isinstance(config, dict):
        logging_config = config.get("logging")
        if isinstance(logging_config, dict):
            log_file = logging_config.get("file") or ""
    if log_file:
        directory = os.path.dirname(os.path.abspath(log_file))
    else:
        directory = _safe_cwd(fallback=".")
    return directory, None, True


def _configured_outbox_dir(config):
    if not isinstance(config, dict):
        return None
    daemon_config = config.get("daemon")
    if not isinstance(daemon_config, dict):
        return None
    return configured_outbox_dir(daemon_config)


def _dump_filename(process, occurrence):
    stamp = _utc_now_iso().replace(":", "-")
    safe = _FILENAME_UNSAFE_RE.sub("-", str(process or "unknown")).strip("-") or "unknown"
    return f"{stamp}-{safe}-error{occurrence}.txt"


def _write_atomic(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temp_path = path + ".tmp"
    try:
        with open(temp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        _remove_quietly(temp_path)
        raise


def _remove_quietly(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logging.debug("Could not remove temporary crash dump %s: %s", path, exc)


def _register_dump(outbox, path, payload, process, occurrence):
    try:
        entry = {
            "path": os.path.relpath(path, outbox).replace(os.sep, "/"),
            "kind": CRASH_KIND,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mtime": datetime.fromtimestamp(
                os.path.getmtime(path), timezone.utc).isoformat(),
            "taken_at": _utc_now_iso(),
            "process": process,
            "error_occurrence": occurrence,
            # Legacy alias, honoured for one release: the puller reads the
            # manifest outside this repo, so the old key stays beside the new
            # one rather than being replaced in place.
            "error": occurrence,
        }
        update_manifest(outbox, entry)
    # The file is on disk and its path is returned either way; without the
    # manifest entry the sync will not collect it, so say so loudly.
    except Exception:  # noqa: BLE001 - the dump itself is not affected
        _log_own_failure("Crash dump was not registered in the outbox manifest")


def _recent_log_records():
    with _STATE_LOCK:
        handler = _state["handler"]
    if handler is None:
        return []
    try:
        lines = handler.recent_records()
    except BaseException:  # noqa: BLE001
        return []
    trimmed = []
    for line in lines:
        if not isinstance(line, str):
            try:
                line = repr(line)
            except BaseException:  # noqa: BLE001
                line = "<unrepresentable log line>"
        if len(line) > MAX_LOG_RECORD_CHARS:
            line = line[:MAX_LOG_RECORD_CHARS] + "... [truncated]"
        trimmed.append(line)
    return trimmed


# ── small helpers ────────────────────────────────────────────────────────────

def _attach_handler():
    handler = _state["handler"]
    if handler is None:
        handler = RecentLogHandler()
        _state["handler"] = handler
    # Re-attach if something (a forced basicConfig, a test teardown) removed it.
    root = logging.getLogger()
    if handler not in root.handlers:
        root.addHandler(handler)


def _safe_process_name(process):
    try:
        text = str(process or "unknown")
    except BaseException:  # noqa: BLE001
        return "unknown"
    return _FILENAME_UNSAFE_RE.sub("-", text).strip("-") or "unknown"


def _safe_cwd(fallback="<unknown>"):
    try:
        return os.getcwd()
    except BaseException:  # noqa: BLE001
        return fallback


def _safe_argv():
    try:
        return repr(sys.argv)
    except BaseException:  # noqa: BLE001
        return "<unrepresentable>"


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _print_path(path):
    try:
        print(f"Crash dump written to {path}", file=sys.stderr)
    except BaseException:  # noqa: BLE001 - printing must not break the exit
        pass


def _log_own_failure(message):
    """Report the reporter's own failure without ever raising."""
    try:
        logging.error("%s", message, exc_info=True)
        return
    except BaseException:  # noqa: BLE001 - fall through to a bare print
        pass
    try:
        print(f"crash reporter: {message}", file=sys.stderr)
    except BaseException:  # noqa: BLE001
        pass
