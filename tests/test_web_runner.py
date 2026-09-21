"""The web entry point's startup order: crash hooks before logging.

The crash reporter used to be installed only *after* ``logging.basicConfig`` and
``load_config``, so an exception raised while doing either had no destination at
all -- no record (the handlers were the thing being built), no ring buffer, no
dump. ``web_runner.main`` now installs the hooks first (they do not need
logging) and attaches the ring-buffer handler afterwards, via ``crash.install``.
This pins that order so a later reshuffle cannot put the hooks back after the
things that can fail.
"""

import pytest

from src import web_runner


def test_web_runner_installs_the_crash_hooks_before_logging_and_the_config(monkeypatch):
    order = []

    monkeypatch.setattr("sys.argv", ["web_runner.py", "config.yaml"])
    monkeypatch.setattr(web_runner.crash, "install_hooks",
                        lambda name: order.append(("hooks", name)))
    monkeypatch.setattr(web_runner.logging, "basicConfig",
                        lambda **kwargs: order.append(("logging", None)))

    def explode(config_path):
        order.append(("config", config_path))
        raise FileNotFoundError(config_path)

    monkeypatch.setattr(web_runner, "load_config", explode)

    with pytest.raises(SystemExit) as caught:
        web_runner.main()

    assert caught.value.code == 1
    assert order == [("hooks", "web"), ("logging", None),
                     ("config", "config.yaml")]
