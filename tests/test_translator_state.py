"""The translation backoff must survive a daemon restart.

Before this, the delay lived only in memory. A daemon restart set the streak
back to zero, so the next failure was answered with the base delay again: an
account-level rejection that had already climbed to its hour was retried a
minute after every restart, indefinitely, and the log repeated the same line
each time. The user-visible report was "when I restart the daemon, the
translation starts over at a 60s delay".

Two things are persisted: the streak, which reconstructs the delay, and the
moment the next attempt falls due, so a restart waits only the remainder
instead of serving the whole delay again. Either one alone reconstructs the
other, so they are checked against each other here.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx2
import openai
import pytest
import yaml

from src.daemon_state import StateStore, state_path_for
from src.translator import (
    ACCOUNT_BASE_SECONDS,
    ACCOUNT_MAX_SECONDS,
    MAX_FAILURE_STREAK,
    RETRY_BASE_SECONDS,
    RETRY_MAX_SECONDS,
    STATE_SECTION,
    TranslatorThread,
    _coerce_streak,
    backoff_delay,
    remaining_backoff,
)


def _status_error(code: int) -> openai.APIStatusError:
    """A real SDK exception carrying the status the API returned."""
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx2.Response(code, request=request)
    return openai.APIStatusError("boom", response=response, body=None)


def _full_batch(size: int = 20) -> list[dict]:
    return [
        {"entity_type": "item", "entity_id": i, "field": "title_en",
         "original_text": "x", "priority": 3}
        for i in range(size)
    ]


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "state.yaml"))


@pytest.fixture
def api_config(mock_config):
    """conftest's config plus the key `run` needs before it will start at all."""
    config = dict(mock_config)
    config["openai"] = {
        "api_key": "SK-TEST",
        "endpoint": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    }
    return config


def _translator(mock_config, store):
    return TranslatorThread(mock_config, state_store=store)


# --- the state store ------------------------------------------------------

def test_state_file_lives_beside_the_database(tmp_path):
    path = state_path_for(str(tmp_path / "sub" / "workshop.db"))
    assert path == str(tmp_path / "sub" / ".daemon_state.yaml")


def test_round_trip(store):
    assert store.load() == {}
    assert store.save({"a": {"n": 1}}) is True
    assert store.load() == {"a": {"n": 1}}


def test_a_missing_file_is_not_an_error(store):
    """First run is the normal case: no file, no complaint, empty document."""
    assert store.load() == {}


def test_an_unparseable_file_is_ignored_not_fatal(store):
    with open(store.path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not: valid: yaml: at all\n")
    assert store.load() == {}


def test_a_non_mapping_document_is_ignored(store):
    """A file holding a list or a scalar must not become the document."""
    with open(store.path, "w", encoding="utf-8") as handle:
        handle.write("- one\n- two\n")
    assert store.load() == {}


def test_saving_merges_sections_instead_of_replacing_the_document(store):
    """A second writer's section must survive the first writer's save."""
    store.save({"translator": {"n": 1}})
    store.save({"somewhere_else": {"ok": True}})
    assert store.load() == {"translator": {"n": 1}, "somewhere_else": {"ok": True}}


def test_removing_one_section_leaves_the_others(store):
    store.save({"translator": {"n": 1}, "somewhere_else": {"ok": True}})
    assert store.remove("translator") is True
    assert store.load() == {"somewhere_else": {"ok": True}}


def test_removing_an_absent_section_is_harmless(store):
    assert store.remove("translator") is True


def test_a_write_leaves_no_temp_file_behind(store):
    store.save({"a": 1})
    assert not os.path.exists(store.path + ".tmp")


def test_an_unwritable_state_file_does_not_raise(tmp_path):
    """A diagnostic that can stop the work it paces is worse than no diagnostic."""
    # A path whose parent is a file, so makedirs/open must fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    store = StateStore(str(blocker / "state.yaml"))
    assert store.save({"a": 1}) is False
    assert store.load() == {}


def test_state_is_written_as_readable_yaml(store):
    """An operator has to be able to read the current backoff without a client."""
    store.save({STATE_SECTION: {"failure_streak": 2, "kind": "account-level"}})
    with open(store.path, "r", encoding="utf-8") as handle:
        text = handle.read()
    assert "failure_streak: 2" in text
    assert yaml.safe_load(text)[STATE_SECTION]["kind"] == "account-level"


# --- the delay shape, now a pure function ---------------------------------

def test_retryable_shape_is_unchanged():
    assert [backoff_delay(n, True) for n in range(1, 5)] == [
        RETRY_BASE_SECONDS * 2 ** n for n in range(4)
    ]
    assert backoff_delay(50, True) == RETRY_MAX_SECONDS


def test_account_level_shape_is_unchanged():
    assert backoff_delay(1, False) == ACCOUNT_BASE_SECONDS
    assert backoff_delay(2, False) == ACCOUNT_BASE_SECONDS * 2
    assert backoff_delay(50, False) == ACCOUNT_MAX_SECONDS


def test_an_absurd_streak_is_clamped_rather_than_computed():
    """A persisted streak is attacker-adjacent input: 2**10**9 must not be built.

    Without a bound this does not merely return the cap, it tries to construct
    the integer first and hangs the thread.
    """
    assert backoff_delay(10 ** 9, False) == ACCOUNT_MAX_SECONDS
    assert backoff_delay(MAX_FAILURE_STREAK * 100, True) == RETRY_MAX_SECONDS


def test_a_streak_below_one_still_yields_the_base():
    assert backoff_delay(0, True) == RETRY_BASE_SECONDS
    assert backoff_delay(-5, False) == ACCOUNT_BASE_SECONDS


def test_streaks_are_coerced_safely():
    assert _coerce_streak(3) == 3
    assert _coerce_streak("4") == 4
    assert _coerce_streak(None) == 0
    assert _coerce_streak("nonsense") == 0
    assert _coerce_streak(-2) == 0
    assert _coerce_streak(10 ** 9) == MAX_FAILURE_STREAK


def test_remaining_backoff_counts_down_to_the_moment():
    now = 1_000_000.0
    due = datetime.fromtimestamp(now + 300, timezone.utc).isoformat()
    assert remaining_backoff(due, now, ACCOUNT_MAX_SECONDS) == pytest.approx(300, abs=1)


def test_remaining_backoff_is_zero_once_the_moment_has_passed():
    now = 1_000_000.0
    due = datetime.fromtimestamp(now - 5, timezone.utc).isoformat()
    assert remaining_backoff(due, now, ACCOUNT_MAX_SECONDS) == 0


def test_remaining_backoff_rejects_unusable_values():
    assert remaining_backoff(None, 0.0, ACCOUNT_MAX_SECONDS) == 0
    assert remaining_backoff("not a date", 0.0, ACCOUNT_MAX_SECONDS) == 0
    assert remaining_backoff(12345, 0.0, ACCOUNT_MAX_SECONDS) == 0


def test_remaining_backoff_accepts_the_datetime_yaml_gives_back():
    """PyYAML resolves an ISO timestamp to a datetime, so both shapes arrive."""
    now = 1_000_000.0
    when = datetime.fromtimestamp(now + 60, timezone.utc)
    assert remaining_backoff(when, now, ACCOUNT_MAX_SECONDS) == pytest.approx(60, abs=1)


def test_a_naive_timestamp_is_read_as_utc():
    now = 1_000_000.0
    naive = datetime.fromtimestamp(now + 45, timezone.utc).replace(tzinfo=None)
    assert remaining_backoff(naive, now, ACCOUNT_MAX_SECONDS) == pytest.approx(45, abs=1)


def test_a_timestamp_beyond_the_cap_is_clamped():
    """A bad clock or an edited file must not park the thread for days."""
    now = 1_000_000.0
    due = datetime.fromtimestamp(now + 86400 * 30, timezone.utc).isoformat()
    assert remaining_backoff(due, now, ACCOUNT_MAX_SECONDS) == ACCOUNT_MAX_SECONDS


# --- what a failure records ----------------------------------------------

def test_a_failure_records_both_the_streak_and_the_due_moment(mock_config, store):
    thread = _translator(mock_config, store)
    thread._register_failure(_status_error(402))

    section = store.load()[STATE_SECTION]
    assert section["failure_streak"] == 1
    assert section["kind"] == "account-level"

    due = datetime.fromisoformat(section["next_attempt_at"])
    expected = datetime.now(timezone.utc) + timedelta(seconds=ACCOUNT_BASE_SECONDS)
    assert abs((due - expected).total_seconds()) < 5

    # Either value reconstructs the other.
    assert backoff_delay(section["failure_streak"], False) == ACCOUNT_BASE_SECONDS


def test_the_recorded_delay_matches_what_was_returned(mock_config, store):
    thread = _translator(mock_config, store)
    for _ in range(3):
        delay = thread._register_failure(_status_error(500))

    section = store.load()[STATE_SECTION]
    assert section["failure_streak"] == 3
    assert delay == RETRY_BASE_SECONDS * 4
    assert backoff_delay(section["failure_streak"], True) == delay


def test_a_success_clears_the_recorded_state(mock_config, store):
    thread = _translator(mock_config, store)
    thread._register_failure(_status_error(402))
    assert STATE_SECTION in store.load()

    thread._register_success()
    assert thread._failure_streak == 0
    assert STATE_SECTION not in store.load()


def test_a_success_with_nothing_outstanding_does_not_write(mock_config, store):
    """This runs after every batch, so the happy path must stay off the disk."""
    thread = _translator(mock_config, store)
    with patch.object(store, "remove") as remove:
        thread._register_success()
    remove.assert_not_called()
    assert not os.path.exists(store.path)


# --- the restart ----------------------------------------------------------

def test_a_restart_continues_the_backoff_instead_of_restarting_it(api_config, store):
    """The reported defect, end to end.

    Two account-level failures take the delay to 120 s. After a restart the
    third failure must be answered with 240 s, not with the 60 s base again.
    """
    first = _translator(api_config, store)
    assert first._register_failure(_status_error(402)) == ACCOUNT_BASE_SECONDS
    assert first._register_failure(_status_error(402)) == ACCOUNT_BASE_SECONDS * 2

    # A new process: fresh thread, same state file.
    second = _translator(api_config, store)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(round(seconds))
        if len(sleeps) >= 3:
            second.running = False

    def boom(*_args, **_kwargs):
        raise _status_error(402)

    with patch("src.translator.get_next_batch_for_translation",
               return_value=_full_batch()), \
         patch("src.translator._create_openai_client", return_value=MagicMock()), \
         patch.object(TranslatorThread, "_translate_batch", side_effect=boom), \
         patch.object(TranslatorThread, "_sleep", side_effect=fake_sleep):
        second.run()

    assert second._failure_streak == 4
    # The first wait is the remainder of the backoff that was already running,
    # then the sequence continues upwards from where it had reached.
    assert sleeps[0] == pytest.approx(ACCOUNT_BASE_SECONDS * 2, abs=5)
    assert sleeps[1] == ACCOUNT_BASE_SECONDS * 4
    assert sleeps[2] == ACCOUNT_BASE_SECONDS * 8
    assert ACCOUNT_BASE_SECONDS not in sleeps[1:], "the base delay must not restart"


def test_a_restart_waits_only_the_remainder(mock_config, store):
    """Most of a long backoff having elapsed, a restart serves what is left."""
    due = datetime.now(timezone.utc) + timedelta(seconds=30)
    store.save({STATE_SECTION: {
        "failure_streak": 6, "next_attempt_at": due.isoformat(), "kind": "account-level",
    }})

    thread = _translator(mock_config, store)
    slept = []
    thread._sleep = lambda seconds: slept.append(seconds)
    thread._resume_persisted_backoff()

    assert thread._failure_streak == 6
    assert slept == [pytest.approx(30, abs=5)]


def test_a_restart_after_the_backoff_expired_does_not_wait(mock_config, store):
    past = datetime.now(timezone.utc) - timedelta(seconds=5)
    store.save({STATE_SECTION: {
        "failure_streak": 4, "next_attempt_at": past.isoformat(), "kind": "account-level",
    }})

    thread = _translator(mock_config, store)
    slept = []
    thread._sleep = lambda seconds: slept.append(seconds)
    thread._resume_persisted_backoff()

    # The streak is still remembered, so the next failure escalates rather than
    # starting again -- but nothing is waited for.
    assert thread._failure_streak == 4
    assert slept == []
    assert thread._register_failure(_status_error(402)) == ACCOUNT_BASE_SECONDS * 16


def test_a_corrupt_recorded_streak_does_not_wedge_the_thread(mock_config, store):
    store.save({STATE_SECTION: {
        "failure_streak": 10 ** 9,
        "next_attempt_at": datetime.now(timezone.utc).isoformat(),
        "kind": "account-level",
    }})
    thread = _translator(mock_config, store)
    thread._sleep = MagicMock()
    thread._resume_persisted_backoff()

    assert thread._failure_streak == MAX_FAILURE_STREAK
    assert thread._register_failure(_status_error(402)) == ACCOUNT_MAX_SECONDS


def test_a_restart_with_no_recorded_state_waits_for_nothing(mock_config, store):
    thread = _translator(mock_config, store)
    slept = []
    thread._sleep = lambda seconds: slept.append(seconds)
    thread._resume_persisted_backoff()

    assert thread._failure_streak == 0
    assert slept == []


def test_a_recorded_section_of_the_wrong_shape_is_ignored(mock_config, store):
    store.save({STATE_SECTION: "not a mapping"})
    thread = _translator(mock_config, store)
    thread._sleep = MagicMock()
    thread._resume_persisted_backoff()
    assert thread._failure_streak == 0


# --- guards ---------------------------------------------------------------

def test_a_thread_without_a_store_stays_off_the_disk(mock_config):
    """The default must not write state, or constructing one litters the repo.

    Tests and embeddings build `TranslatorThread(config)` directly; if that
    wrote anything, it would land beside the working directory.
    """
    thread = TranslatorThread(mock_config)
    assert thread.state_store is None
    thread._register_failure(_status_error(402))
    thread._register_success()
    assert not os.path.exists(".daemon_state.yaml")


def test_the_daemon_gives_the_translator_a_state_store(tmp_path, mock_config):
    """Persistence is injected, so a missed wiring would silently do nothing."""
    from src.daemon import Daemon

    config = dict(mock_config)
    config["daemon"] = {"api_batch_size": 1, "target_appids": [1]}

    with patch("src.daemon.TranslatorThread") as translator_cls:
        Daemon(config, config_path=str(tmp_path / "config.yaml"))

    store = translator_cls.call_args.kwargs["state_store"]
    assert isinstance(store, StateStore)
    assert store.path == state_path_for(mock_config["database"]["path"])
