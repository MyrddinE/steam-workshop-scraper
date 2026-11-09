"""The proxy resolver, its credential handling, and the 407 record.

The tests drive the resolver with a stub proxy map rather than the machine's
real environment: the production host has a registry proxy, the container may
have none, and a test that depended on either would be testing the host.
"""

import os

import pytest
import requests
import yaml

from src import network


@pytest.fixture(autouse=True)
def no_ambient_no_proxy(monkeypatch):
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_an_explicit_proxy_url_wins_and_carries_its_credentials():
    config = {"proxy": {"url": "http://proxy.example:8080",
                        "username": "operator", "password": "s3cret"}}

    settings = network.proxies_for("https://steamcommunity.com/", config)

    assert settings == {
        "http": "http://operator:s3cret@proxy.example:8080",
        "https": "http://operator:s3cret@proxy.example:8080",
    }


def test_a_bare_host_port_is_treated_as_http():
    settings = network.proxies_for(
        "https://steamcommunity.com/", {"proxy": {"url": "proxy.example:8080"}})

    assert settings["https"] == "http://proxy.example:8080"


def test_environment_proxy_is_used_when_no_url_is_configured(monkeypatch):
    monkeypatch.setattr(network, "get_environ_proxies",
                        lambda url: {"https": "http://env-proxy:3128"})

    settings = network.proxies_for(
        "https://steamcommunity.com/",
        {"proxy": {"username": "operator", "password": "s3cret"}})

    assert settings["https"] == "http://operator:s3cret@env-proxy:3128"


def test_no_proxy_bypasses_even_an_explicit_proxy():
    config = {"proxy": {"url": "http://proxy.example:8080",
                        "no_proxy": "steamcommunity.com"}}

    assert network.proxies_for("https://steamcommunity.com/", config) == {
        "http": None, "https": None}


def test_environment_no_proxy_is_honoured(monkeypatch):
    monkeypatch.setenv("NO_PROXY", ".steamcommunity.com")

    assert network.proxies_for(
        "https://steamcommunity.com/",
        {"proxy": {"url": "http://proxy.example:8080"}}) == {
        "http": None, "https": None}


def test_no_proxy_at_all_forces_the_environment_off(monkeypatch):
    """`{}` would let requests merge an environment proxy back in."""
    monkeypatch.setattr(network, "get_environ_proxies", lambda url: {})

    assert network.proxies_for("https://steamcommunity.com/", {}) == {
        "http": None, "https": None}


def test_display_strips_credentials_but_keeps_the_address():
    assert network.display_proxy(
        "http://operator:s3cret@proxy.example:8080/path?q=1"
    ) == "http://proxy.example:8080/path?q=1"


def test_a_407_is_recognised_as_a_proxy_authentication_problem(monkeypatch):
    monkeypatch.setattr(network, "proxy_url_for",
                        lambda url, config=None: "http://proxy.example:8080")
    exc = requests.exceptions.ProxyError(
        "Cannot connect to proxy. Tunnel connection failed: "
        "407 Proxy Authentication Required")

    result = network.proxy_error(exc)

    assert result["proxy_url"] == "http://proxy.example:8080"
    assert "407" in result["detail"]


def test_another_proxy_failure_is_not_an_authentication_problem():
    exc = requests.exceptions.ProxyError(
        "Cannot connect to proxy. Tunnel connection failed: connection refused")

    assert network.proxy_error(exc) is None


def test_the_proxy_record_round_trips_and_keeps_its_first_timestamp(tmp_path, monkeypatch):
    db_path = str(tmp_path / "workshop.db")

    monkeypatch.setattr(network.time, "time", lambda: 1000)
    assert network.record_auth_required(db_path, "http://proxy:8080", "detail")
    first = network.read_auth_required(db_path)
    assert first == {"url": "http://proxy:8080", "detail": "detail",
                     "detected_at": 1000}

    monkeypatch.setattr(network.time, "time", lambda: 2000)
    network.record_auth_required(db_path, "http://proxy:8080", "detail")
    assert network.read_auth_required(db_path)["detected_at"] == 1000, \
        "a repeated 407 must not look like a new problem"

    network.clear_auth_required(db_path)
    assert network.read_auth_required(db_path) is None


def test_a_changed_proxy_starts_a_new_timestamp(tmp_path, monkeypatch):
    db_path = str(tmp_path / "workshop.db")
    monkeypatch.setattr(network.time, "time", lambda: 1000)
    network.record_auth_required(db_path, "http://proxy-one:8080")
    monkeypatch.setattr(network.time, "time", lambda: 2000)
    network.record_auth_required(db_path, "http://proxy-two:8080")

    assert network.read_auth_required(db_path)["detected_at"] == 2000


def test_saving_credentials_writes_only_the_proxy_section(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "database:\n  path: workshop.db\nsession:\n  login_secure: LOGIN\n",
        encoding="utf-8")

    section = network.save_credentials(
        str(config_path), "http://proxy.example:8080", "operator", "s3cret")

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert section == {"url": "http://proxy.example:8080",
                       "username": "operator", "password": "s3cret"}
    assert saved["proxy"] == section
    assert saved["database"]["path"] == "workshop.db"
    assert saved["session"]["login_secure"] == "LOGIN"


def test_credentials_embedded_in_the_url_are_split_into_the_keys(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("database:\n  path: workshop.db\n", encoding="utf-8")

    section = network.save_credentials(
        str(config_path), "http://operator:s3cret@proxy.example:8080", "", "")

    assert section == {"url": "http://proxy.example:8080",
                       "username": "operator", "password": "s3cret"}


def test_saving_credentials_refuses_a_missing_config(tmp_path):
    missing = tmp_path / "missing.yaml"

    with pytest.raises(FileNotFoundError):
        network.save_credentials(
            str(missing), "http://proxy.example:8080", "operator", "s3cret")
    assert not os.path.exists(missing)


def test_the_configured_url_is_used_for_the_detected_readout():
    config = {"proxy": {"url": "http://proxy.example:8080"}}

    assert network.proxy_url_for("https://steamcommunity.com/", config) == \
        "http://proxy.example:8080"
