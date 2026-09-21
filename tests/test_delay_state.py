"""The pacing delays are daemon state, not configuration.

`api_delay_seconds`, `web_delay_seconds` and `image_delay_seconds` used to be
`config.yaml` keys that the workers wrote back as the delays moved. They are not
settings anyone chooses: they describe what each worker is currently doing about
a rate Steam has not published. They now live in `.daemon_state.yaml` beside the
database, one section per worker, the same restart-surviving store the
translator's backoff uses -- and the config keys are retired with a warning, not
renamed, because there is no new spelling to move them to.

These pin the contract the migration has to keep: a moved delay survives a
restart, a config that still carries the old key is ignored with one warning, the
defaults stand when there is no state at all, `config.yaml` is never written with
a delay again, and one worker's write leaves the translator's section alone.
"""

import logging

import yaml

from src import pacing
from src.daemon import Daemon
from src.daemon_state import StateStore, state_path_for
from src.image_worker import ImageDownloadThread
from src.web_worker import WEB_DELAY_DEFAULT, WebScraperThread, configured_web_delay

# The defaults are pinned as literals, not read back from the modules, so the
# test states the contract rather than echoing the implementation.
API_DELAY_DEFAULT = 1.5
IMAGE_DELAY_DEFAULT = 2.0


def _store(db_path):
    return StateStore(state_path_for(db_path))


def _config(db_path, **daemon):
    return {"database": {"path": db_path}, "daemon": dict(daemon, target_appids=[1])}


def _retired_warnings(caplog, key):
    """The warning lines that name `key` as a key that is no longer read."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "no longer used" in record.getMessage()
        and key in record.getMessage()
    ]


# --- a moved delay survives a restart ----------------------------------------

def test_a_moved_web_delay_is_restored_by_a_fresh_worker(db_path):
    """The persisted value, not the default, is a restarted worker's start."""
    store = _store(db_path)
    worker = WebScraperThread(db_path, ".pauselock", state_store=store)
    assert worker.web_delay == WEB_DELAY_DEFAULT

    worker.web_delay = 12.5
    worker._persist_delay(force=True)

    assert store.load().get(pacing.WEB_DELAY_SECTION) == 12.5
    restarted = WebScraperThread(db_path, ".pauselock", state_store=store)
    assert restarted.web_delay == 12.5


def test_a_moved_image_delay_is_restored_by_a_fresh_worker(db_path):
    store = _store(db_path)
    worker = ImageDownloadThread(db_path, ".pauselock", state_store=store)

    worker.image_delay = 7.25
    worker._persist_delay(force=True)

    assert store.load().get(pacing.IMAGE_DELAY_SECTION) == 7.25
    restarted = ImageDownloadThread(db_path, ".pauselock", state_store=store)
    assert restarted.image_delay == 7.25


def test_a_moved_api_delay_is_restored_by_a_fresh_daemon(db_path):
    """The API worker's delay is a daemon attribute, restored the same way."""
    daemon = Daemon(_config(db_path))
    daemon._back_off_api_delay()

    moved = daemon.api_delay
    assert moved == API_DELAY_DEFAULT * 2
    assert _store(db_path).load().get(pacing.API_DELAY_SECTION) == moved

    restarted = Daemon(_config(db_path))
    assert restarted.api_delay == moved


def test_the_write_rate_is_bounded_by_the_persist_step(db_path):
    """A move smaller than the step is kept in memory, not written to disk."""
    store = _store(db_path)
    worker = WebScraperThread(db_path, ".pauselock", state_store=store)
    worker._persisted_web_delay = worker.web_delay

    worker.web_delay += pacing.PERSIST_STEP_SECONDS / 2
    worker._persist_delay()
    assert store.load().get(pacing.WEB_DELAY_SECTION) is None, \
        "a sub-step move must not earn a write"

    worker.web_delay += pacing.PERSIST_STEP_SECONDS
    worker._persist_delay()
    assert store.load().get(pacing.WEB_DELAY_SECTION) == \
        pacing.persistable(worker.web_delay)


def test_deleting_a_section_returns_the_worker_to_its_default(db_path):
    """The documented reset path: the section is the only thing to clear."""
    store = _store(db_path)
    worker = WebScraperThread(db_path, ".pauselock", state_store=store)
    worker.web_delay = 30.0
    worker._persist_delay(force=True)
    assert store.load().get(pacing.WEB_DELAY_SECTION) == 30.0

    StateStore(state_path_for(db_path)).remove(pacing.WEB_DELAY_SECTION)

    restarted = WebScraperThread(db_path, ".pauselock", state_store=store)
    assert restarted.web_delay == WEB_DELAY_DEFAULT


# --- the retired config keys are ignored, with one warning -------------------

def test_a_config_delay_key_is_ignored_and_the_persisted_value_stands(
        db_path, caplog):
    store = _store(db_path)
    store.save({pacing.API_DELAY_SECTION: 7.0})

    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, api_delay_seconds=99.0))

    assert daemon.api_delay == 7.0, "the dead config key must not be read"
    warnings = _retired_warnings(caplog, "daemon.api_delay_seconds")
    assert len(warnings) == 1
    assert "no longer read" in warnings[0]


def test_the_web_config_delay_key_is_ignored_and_warns_once(db_path, caplog):
    store = _store(db_path)
    store.save({pacing.WEB_DELAY_SECTION: 9.0})
    config = _config(db_path, web_delay_seconds=42.0)

    with caplog.at_level(logging.WARNING):
        assert configured_web_delay(config) == 9.0
        assert configured_web_delay(config) == 9.0

    assert len(_retired_warnings(caplog, "daemon.web_delay_seconds")) == 1, \
        "the warning is once per process, however often the delay is read"


def test_an_image_config_delay_key_is_ignored_and_warns_once(db_path, caplog):
    store = _store(db_path)
    store.save({pacing.IMAGE_DELAY_SECTION: 3.0})

    with caplog.at_level(logging.WARNING):
        daemon = Daemon(_config(db_path, image_delay_seconds=88.0))
        image = ImageDownloadThread(db_path, ".pauselock",
                                    state_store=daemon.state_store)

    assert image.image_delay == 3.0, "the dead config key must not be read"
    assert len(_retired_warnings(caplog, "daemon.image_delay_seconds")) == 1


# --- the defaults stand with no state and no config --------------------------

def test_the_defaults_hold_when_there_is_no_state_and_no_config(db_path):
    daemon = Daemon(_config(db_path))
    assert daemon.api_delay == API_DELAY_DEFAULT == 1.5
    assert WebScraperThread(db_path, ".pauselock").web_delay == WEB_DELAY_DEFAULT == 6.0
    assert ImageDownloadThread(db_path, ".pauselock").image_delay == IMAGE_DELAY_DEFAULT == 2.0


def test_a_corrupt_or_empty_section_falls_back_to_the_default(db_path):
    """A state value that is not a positive number is not trusted as a rate."""
    _store(db_path).save({pacing.WEB_DELAY_SECTION: "nonsense",
                          pacing.API_DELAY_SECTION: 0})
    daemon = Daemon(_config(db_path))
    assert daemon.api_delay == API_DELAY_DEFAULT
    assert WebScraperThread(db_path, ".pauselock", state_store=_store(db_path)).web_delay \
        == WEB_DELAY_DEFAULT


# --- config.yaml is never written with a delay again -------------------------

def test_nothing_writes_the_delays_back_into_config_yaml(db_path, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(_config(db_path)), encoding="utf-8")
    config = _config(db_path, api_batch_size=2)
    daemon = Daemon(config, config_path=str(config_path))

    # Move every worker's delay: the API one persists immediately, the other two
    # through their own force path.
    daemon._back_off_api_delay()
    web = WebScraperThread(db_path, ".pauselock", state_store=daemon.state_store)
    web.web_delay = 12.0
    web._persist_delay(force=True)
    image = ImageDownloadThread(db_path, ".pauselock", state_store=daemon.state_store)
    image.image_delay = 9.0
    image._persist_delay(force=True)

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for key in ("api_delay_seconds", "web_delay_seconds", "image_delay_seconds"):
        assert key not in saved.get("daemon", {}), \
            f"{key} must not be written to config.yaml any more"

    # The evidence the delay went somewhere durable instead.
    state = _store(db_path).load()
    assert state[pacing.API_DELAY_SECTION] == daemon.api_delay
    assert state[pacing.WEB_DELAY_SECTION] == 12.0
    assert state[pacing.IMAGE_DELAY_SECTION] == 9.0


# --- the translator's section is not touched --------------------------------

def test_the_translator_section_survives_every_delay_write(db_path):
    store = _store(db_path)
    translator_section = {"failure_streak": 4, "next_attempt_at": 1234567890.0,
                          "kind": "service"}
    store.save({"translation_backoff": translator_section})

    daemon = Daemon(_config(db_path))
    daemon._back_off_api_delay()
    web = WebScraperThread(db_path, ".pauselock", state_store=daemon.state_store)
    web.web_delay = 15.0
    web._persist_delay(force=True)
    image = ImageDownloadThread(db_path, ".pauselock", state_store=daemon.state_store)
    image.image_delay = 4.0
    image._persist_delay(force=True)

    assert store.load()["translation_backoff"] == translator_section
