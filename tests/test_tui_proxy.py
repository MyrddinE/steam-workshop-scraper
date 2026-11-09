"""The TUI's proxy prompt.

The prompt is the TUI half of the 407 recovery: a worker records the proxy
problem, `ScraperApp` pushes this screen, and saving writes only the proxy
section. The screen is exercised on its own so the test does not need a full
`ScraperApp` or a daemon.
"""

import pytest
from textual.app import App
from textual.widgets import Button, Input

from src.tui import ProxyCredentialsScreen


class _Harness(App):
    def __init__(self, screen):
        super().__init__()
        self._screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._screen)


def _press(screen, selector):
    screen.on_button_pressed(Button.Pressed(screen.query_one(selector, Button)))


@pytest.mark.asyncio
async def test_proxy_screen_saves_the_things_it_was_given(monkeypatch, tmp_path):
    saved = {}

    def fake_save(config_path, url, username, password, config=None, db_path=None):
        saved.update({"config_path": config_path, "url": url,
                      "username": username, "password": password,
                      "config": config, "db_path": db_path})
        return {"url": url}

    monkeypatch.setattr("src.tui.session_refresh.save_proxy_credentials", fake_save)
    screen = ProxyCredentialsScreen(
        str(tmp_path / "config.yaml"), str(tmp_path / "workshop.db"),
        url="http://proxy.example:8080", detail="proxy auth required",
        config={})

    async with _Harness(screen).run_test() as pilot:
        await pilot.pause()
        screen.query_one("#proxy-username", Input).value = "operator"
        screen.query_one("#proxy-password", Input).value = "s3cret"
        _press(screen, "#proxy-save")
        await pilot.pause()

    assert saved["config_path"] == str(tmp_path / "config.yaml")
    assert saved["url"] == "http://proxy.example:8080"
    assert saved["username"] == "operator"
    assert saved["password"] == "s3cret"
    assert saved["db_path"] == str(tmp_path / "workshop.db")


@pytest.mark.asyncio
async def test_proxy_screen_refuses_a_missing_password(monkeypatch, tmp_path):
    called = {"n": 0}
    monkeypatch.setattr(
        "src.tui.session_refresh.save_proxy_credentials",
        lambda *args, **kwargs: called.update(n=called["n"] + 1))
    screen = ProxyCredentialsScreen(
        str(tmp_path / "config.yaml"), str(tmp_path / "workshop.db"),
        url="http://proxy.example:8080", config={})

    async with _Harness(screen).run_test() as pilot:
        await pilot.pause()
        screen.query_one("#proxy-username", Input).value = "operator"
        _press(screen, "#proxy-save")
        await pilot.pause()

    assert called["n"] == 0, "an incomplete prompt must not save"


def test_the_watcher_opens_the_sign_in_page_once_per_problem(monkeypatch, tmp_path):
    """The TUI's half of the automatic sign-in: one browser tab, not one per tick."""
    import src.tui as tui
    from src import network, session_health

    opened = []
    monkeypatch.setattr(tui.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(tui.network, "read_auth_required", lambda db_path: None)
    monkeypatch.setattr(tui.session_health, "read",
                        lambda db_path: {"detected_at": 7, "detail": "expired"})

    class _Fake:
        db_path = str(tmp_path / "workshop.db")
        config_path = str(tmp_path / "config.yaml")
        config = {}
        _proxy_prompt_open = False
        _proxy_prompt_for = None
        _proxy_prompt_snoozed_until = 0.0
        _signin_opened_for = None
        _signin_last_opened_at = 0.0

        def __init__(self):
            self.notices = []
            self.screens = []

        def notify(self, message, severity=None):
            self.notices.append(message)

        def push_screen(self, screen, callback=None):
            self.screens.append((screen, callback))

        def _start_session_recheck(self):
            pass

    app = _Fake()
    tui.ScraperApp._check_network_problems(app)
    tui.ScraperApp._check_network_problems(app)

    assert opened == ["https://steamcommunity.com/my/"]
    assert app.screens == []


def test_the_watcher_pushes_the_proxy_prompt_instead_of_the_sign_in_page(
        monkeypatch, tmp_path):
    import src.tui as tui

    opened = []
    monkeypatch.setattr(tui.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(tui.network, "read_auth_required", lambda db_path: {
        "url": "http://proxy.example:8080",
        "detail": "407",
        "detected_at": 9,
    })
    monkeypatch.setattr(tui.session_health, "read", lambda db_path: {
        "detected_at": 7, "detail": "expired"})

    class _Fake:
        db_path = str(tmp_path / "workshop.db")
        config_path = str(tmp_path / "config.yaml")
        config = {}
        _proxy_prompt_open = False
        _proxy_prompt_for = None
        _proxy_prompt_snoozed_until = 0.0
        _signin_opened_for = None

        def __init__(self):
            self.screens = []

        def notify(self, message, severity=None):
            pass

        def push_screen(self, screen, callback=None):
            self.screens.append((screen, callback))

        def _proxy_prompt_closed(self, saved):
            pass

    app = _Fake()
    tui.ScraperApp._check_network_problems(app)

    assert opened == [], "signing in cannot fix a 407"
    assert len(app.screens) == 1
    assert isinstance(app.screens[0][0], tui.ProxyCredentialsScreen)
    assert app.screens[0][0].proxy_url == "http://proxy.example:8080"


def test_saving_proxy_credentials_reopens_the_session_flow(monkeypatch):
    """A session problem behind a 407 gets its sign-in/recheck chance again."""
    import src.tui as tui

    class _Fake:
        _proxy_prompt_open = True
        _proxy_prompt_snoozed_until = 0.0
        _signin_opened_for = 7
        config = {}
        config_path = "config.yaml"
        db_path = "workshop.db"

        def __init__(self):
            self.checks = []

        def notify(self, message, severity=None):
            pass

        def _check_network_problems(self):
            self.checks.append(1)

    app = _Fake()
    tui.ScraperApp._proxy_prompt_closed(app, True)

    assert app._proxy_prompt_open is False
    assert app._signin_opened_for is None
    assert app.checks == [1]


def test_later_still_lets_the_proxy_prompt_return(monkeypatch):
    import src.tui as tui

    class _Fake:
        _proxy_prompt_open = True
        _proxy_prompt_for = "key"
        _proxy_prompt_snoozed_until = 0.0
        _signin_opened_for = None

        def notify(self, message, severity=None):
            pass

    app = _Fake()
    tui.ScraperApp._proxy_prompt_closed(app, False)

    assert app._proxy_prompt_open is False
    assert app._proxy_prompt_for is None, "the key must not block the retry"
    assert app._proxy_prompt_snoozed_until > 0


def test_the_tui_sign_in_window_is_throttled_to_once_per_ten_minutes(
        monkeypatch, tmp_path):
    """A problem re-recorded every few seconds must not steal focus each time."""
    import src.tui as tui

    opened = []
    now = {"value": 1000.0}
    problem = {"detected_at": 7, "detail": "expired"}
    monkeypatch.setattr(tui.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(tui.network, "read_auth_required", lambda db_path: None)
    monkeypatch.setattr(tui.session_health, "read", lambda db_path: dict(problem))
    monkeypatch.setattr(tui.time, "time", lambda: now["value"])

    class _Fake:
        db_path = str(tmp_path / "workshop.db")
        config_path = str(tmp_path / "config.yaml")
        config = {}
        _proxy_prompt_open = False
        _proxy_prompt_for = None
        _proxy_prompt_snoozed_until = 0.0
        _signin_opened_for = None
        _signin_last_opened_at = 0.0

        def notify(self, message, severity=None):
            pass

        def _start_session_recheck(self):
            pass

    app = _Fake()
    tui.ScraperApp._check_network_problems(app)
    problem["detected_at"] = 8
    tui.ScraperApp._check_network_problems(app)

    assert len(opened) == 1, "a second problem inside the cooldown opened a window"

    now["value"] += 601
    problem["detected_at"] = 9
    tui.ScraperApp._check_network_problems(app)

    assert len(opened) == 2, "a problem after ten minutes should open a window"
