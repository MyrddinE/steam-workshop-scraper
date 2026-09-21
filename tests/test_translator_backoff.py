"""The translation thread must back off when a batch fails.

Before this, `_translate_batch` swallowed its exception and the run loop slept
its normal 1 s, so a batch that could not succeed was re-fetched and re-sent
roughly every 1.2 s indefinitely. Observed live on a 402 spend-limit rejection:
four consecutive failures inside four seconds.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import httpx2
import openai
import pytest

from src.translator import (
    ACCOUNT_BASE_SECONDS,
    ACCOUNT_MAX_SECONDS,
    RETRY_BASE_SECONDS,
    RETRY_MAX_SECONDS,
    TranslatorThread,
    retryable_failure,
)


def _status_error(code: int) -> openai.APIStatusError:
    """A real SDK exception carrying the status the API returned."""
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx2.Response(code, request=request)
    return openai.APIStatusError("boom", response=response, body=None)


@pytest.fixture
def mock_config():
    return {
        "database": {"path": "test.db"},
        "openai": {
            "api_key": "SK-TEST",
            "endpoint": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
        },
    }


def _full_batch(size: int = 20) -> list[dict]:
    # A request is bounded by cost now, not by item count, so twenty one-character
    # titles no longer fill one. The text is long enough that the candidates
    # overflow `DEFAULT_BATCH_CHAR_CAP`, which is what the loop reads as "full".
    return [
        {"entity_type": "item", "entity_id": i, "field": "title_en",
         "original_text": "x" * 300, "priority": 3}
        for i in range(size)
    ]


# --- classification -------------------------------------------------------

@pytest.mark.parametrize("code", [401, 402, 403])
def test_account_level_failures_are_not_retryable(code):
    assert retryable_failure(_status_error(code)) is False


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_service_failures_are_retryable(code):
    assert retryable_failure(_status_error(code)) is True


def test_transport_failure_is_retryable():
    """No status code means the request never landed, which is worth retrying."""
    assert retryable_failure(ConnectionError("connection reset")) is True
    assert retryable_failure(TimeoutError("timed out")) is True


# --- delay shapes ---------------------------------------------------------

def test_retryable_backoff_grows_and_caps(mock_config):
    thread = TranslatorThread(mock_config)
    err = _status_error(500)
    delays = [thread._register_failure(err) for _ in range(12)]

    assert delays[:4] == [RETRY_BASE_SECONDS * 2 ** n for n in range(4)]
    assert max(delays) == RETRY_MAX_SECONDS
    assert all(d <= RETRY_MAX_SECONDS for d in delays)


def test_account_level_backoff_starts_high_and_caps(mock_config):
    thread = TranslatorThread(mock_config)
    err = _status_error(402)
    delays = [thread._register_failure(err) for _ in range(10)]

    assert delays[0] == ACCOUNT_BASE_SECONDS
    assert delays[1] == ACCOUNT_BASE_SECONDS * 2
    assert delays[-1] == ACCOUNT_MAX_SECONDS
    assert all(d <= ACCOUNT_MAX_SECONDS for d in delays)


def test_success_clears_the_streak(mock_config):
    thread = TranslatorThread(mock_config)
    thread._register_failure(_status_error(402))
    thread._register_failure(_status_error(402))
    assert thread._failure_streak == 2

    thread._register_success()
    assert thread._failure_streak == 0

    # And the next failure starts the account-level shape from the bottom again.
    assert thread._register_failure(_status_error(402)) == ACCOUNT_BASE_SECONDS


# --- the run loop ---------------------------------------------------------

def test_loop_backs_off_instead_of_retrying_immediately(mock_config):
    """The regression: a failing batch must not be retried at the normal cadence."""
    thread = TranslatorThread(mock_config)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            thread.running = False

    def boom(*_args, **_kwargs):
        raise _status_error(402)

    with patch("src.translator.get_next_batch_for_translation",
               return_value=_full_batch()), \
         patch("src.translator._create_openai_client", return_value=MagicMock()), \
         patch.object(TranslatorThread, "_translate_batch", side_effect=boom), \
         patch.object(TranslatorThread, "_sleep", side_effect=fake_sleep):
        thread.run()

    assert sleeps == [ACCOUNT_BASE_SECONDS, ACCOUNT_BASE_SECONDS * 2, ACCOUNT_BASE_SECONDS * 4]
    assert 1 not in sleeps, "the normal inter-batch pause must not be used for a failure"


def test_loop_resumes_normal_pacing_after_a_success(mock_config):
    thread = TranslatorThread(mock_config)
    sleeps = []
    attempts = {"n": 0}

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            thread.running = False

    def flaky(*_args, **_kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _status_error(500)

    with patch("src.translator.get_next_batch_for_translation",
               return_value=_full_batch()), \
         patch("src.translator._create_openai_client", return_value=MagicMock()), \
         patch.object(TranslatorThread, "_translate_batch", side_effect=flaky), \
         patch.object(TranslatorThread, "_sleep", side_effect=fake_sleep):
        thread.run()

    assert sleeps[0] == RETRY_BASE_SECONDS, "first failure backs off"
    assert sleeps[1] == 1, "a success returns to the normal inter-batch pause"
    assert thread._failure_streak == 0


def test_loop_still_idles_when_the_queue_is_empty(mock_config):
    thread = TranslatorThread(mock_config)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        thread.running = False

    with patch("src.translator.get_next_batch_for_translation", return_value=[]), \
         patch("src.translator._create_openai_client", return_value=MagicMock()), \
         patch.object(TranslatorThread, "_sleep", side_effect=fake_sleep):
        thread.run()

    assert sleeps == [30]


# --- _translate_batch no longer swallows ----------------------------------

def test_translate_batch_propagates_failure(mock_config):
    """The loop can only back off if it is told; swallowing is the original bug."""
    thread = TranslatorThread(mock_config)
    client = MagicMock()
    client.chat.completions.create.side_effect = _status_error(402)
    conn = MagicMock()

    with patch("src.translator.get_connection", return_value=conn):
        with pytest.raises(openai.APIStatusError):
            thread._translate_batch(_full_batch(1), client, "gpt-4o-mini")

    conn.close.assert_called_once()


# --- shutdown responsiveness ----------------------------------------------

def test_sleep_returns_at_once_when_already_stopped(mock_config):
    thread = TranslatorThread(mock_config)
    thread.running = False
    started = time.monotonic()
    thread._sleep(ACCOUNT_MAX_SECONDS)
    assert time.monotonic() - started < 0.5


def test_long_backoff_does_not_hold_shutdown(mock_config):
    """An hour of backoff must not become an hour of shutdown."""
    thread = TranslatorThread(mock_config)
    timer = threading.Timer(0.2, lambda: setattr(thread, "running", False))
    timer.start()
    try:
        started = time.monotonic()
        thread._sleep(ACCOUNT_MAX_SECONDS)
        elapsed = time.monotonic() - started
    finally:
        timer.cancel()

    assert elapsed < 2.0, f"shutdown was held for {elapsed:.1f}s"
