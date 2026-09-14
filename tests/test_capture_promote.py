"""Promotion: a capture must become a regression test that actually runs.

The whole point of capturing is this step. If the tool writes fixtures the
generated module cannot load, the corpus is inert.
"""
import importlib.util
import json
import os

import pytest

from src import capture, capture_promote


@pytest.fixture
def outbox(tmp_path):
    root = tmp_path / "outbox"
    capture.configure(str(root))
    yield str(root)
    capture.configure(None)


def _make_capture(outbox, kind="web_selector_miss", body=None, **kwargs):
    body = body or (
        '<html><head><title>Steam Workshop :: Error</title></head><body>'
        '<div class="errorBlock">There was a problem</div></body></html>')
    record = capture.record_failure(kind, body=body, **kwargs)
    assert record is not None
    record_path = os.path.join(outbox, record["body_file"])
    # The record sits beside its body; derive its path from the body path.
    return record_path[:-len(".body")] + ".json"


def _load_module(path):
    spec = importlib.util.spec_from_file_location("generated_regressions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── fixture writing ──────────────────────────────────────────────────────────

def test_promotes_a_web_capture_into_a_fixture(outbox, tmp_path):
    record_path = _make_capture(outbox, workshop_id=777, selector=".workshopItemDescription#highlightContent",
                                http_status=200,
                                final_url="https://steamcommunity.com/sharedfiles/filedetails/?id=777")
    fixtures = str(tmp_path / "tests" / "fixtures")

    meta = capture_promote.promote(record_path, outbox, fixtures)

    assert meta["area"] == "web"
    body_path = os.path.join(fixtures, "web", meta["body"])
    assert os.path.exists(body_path)
    with open(body_path, "rb") as handle:
        assert b"errorBlock" in handle.read()
    assert os.path.exists(os.path.join(fixtures, "web", meta["name"] + ".meta.json"))


def test_api_captures_land_in_the_api_area(outbox, tmp_path):
    record_path = _make_capture(outbox, kind="api_unparsed_body", body="<html>proxy</html>",
                                http_status=502,
                                final_url="https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/")
    fixtures = str(tmp_path / "tests" / "fixtures")

    meta = capture_promote.promote(record_path, outbox, fixtures)

    assert meta["area"] == "steam_api"
    assert os.path.isdir(os.path.join(fixtures, "steam_api"))


def test_credentials_are_scrubbed_from_the_fixture(outbox, tmp_path):
    record_path = _make_capture(
        outbox, kind="api_unparsed_body",
        body='{"api_key": "SUPERSECRET", "sessionid": "deadbeefcafe", "note": "ok"}',
        http_status=200,
        final_url="https://api.steampowered.com/x?key=SUPERSECRET&item=1")
    fixtures = str(tmp_path / "tests" / "fixtures")

    meta = capture_promote.promote(record_path, outbox, fixtures)

    with open(os.path.join(fixtures, "steam_api", meta["body"]), "rb") as handle:
        body = handle.read()
    assert b"SUPERSECRET" not in body
    assert b"deadbeefcafe" not in body
    assert b"ok" in body, "scrubbing removed unrelated content"
    assert "SUPERSECRET" not in json.dumps(meta)


# ── the generated module ─────────────────────────────────────────────────────

def test_generated_module_compiles_and_carries_every_case(outbox, tmp_path):
    _make_capture(outbox, workshop_id=1, selector=".a", http_status=200,
                  final_url="https://steamcommunity.com/sharedfiles/filedetails/?id=1")
    _make_capture(outbox, kind="api_unparsed_body", body="<html>err</html>", http_status=502,
                  final_url="https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/")
    tests_dir = tmp_path / "tests"
    fixtures = str(tests_dir / "fixtures")
    test_file = str(tests_dir / "test_ingest_regressions.py")

    result = capture_promote.promote_all(
        os.path.join(outbox, "failures"), fixtures, test_file)

    assert len(result["promoted"]) == 2
    assert result["skipped"] == []
    source = open(test_file).read()
    compile(source, test_file, "exec")
    module = _load_module(test_file)
    assert len(module.CASES) == 2
    assert {c["area"] for c in module.CASES} == {"web", "steam_api"}


def test_generated_replay_cases_actually_pass(outbox, tmp_path):
    """End to end: the generated module and its fixtures run and pass."""
    _make_capture(outbox, workshop_id=1, selector=".workshopItemDescription#highlightContent",
                  http_status=200,
                  final_url="https://steamcommunity.com/sharedfiles/filedetails/?id=1")
    _make_capture(outbox, kind="api_unparsed_body",
                  body="<html><body>proxy error</body></html>", http_status=502,
                  final_url="https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/")
    tests_dir = tmp_path / "tests"
    fixtures = str(tests_dir / "fixtures")
    test_file = str(tests_dir / "test_ingest_regressions.py")
    capture_promote.promote_all(os.path.join(outbox, "failures"), fixtures, test_file)

    module = _load_module(test_file)
    assert module.CASES
    for case in module.CASES:
        module.test_captured_failure_is_handled_gracefully(case)


def test_promote_all_is_idempotent(outbox, tmp_path):
    _make_capture(outbox, workshop_id=1, selector=".a", http_status=200,
                  final_url="https://steamcommunity.com/sharedfiles/filedetails/?id=1")
    tests_dir = tmp_path / "tests"
    fixtures = str(tests_dir / "fixtures")
    test_file = str(tests_dir / "test_ingest_regressions.py")
    failures = os.path.join(outbox, "failures")

    capture_promote.promote_all(failures, fixtures, test_file)
    first = capture_promote.collect_fixtures(fixtures)
    capture_promote.promote_all(failures, fixtures, test_file)
    second = capture_promote.collect_fixtures(fixtures)

    assert [m["name"] for m in first] == [m["name"] for m in second]
    assert len(second) == 1, "re-promoting duplicated the fixture"


def test_group_state_files_are_not_promoted_as_samples(outbox, tmp_path):
    _make_capture(outbox, workshop_id=1, selector=".a", http_status=200,
                  final_url="https://steamcommunity.com/sharedfiles/filedetails/?id=1")
    tests_dir = tmp_path / "tests"
    fixtures = str(tests_dir / "fixtures")

    capture_promote.promote_all(
        os.path.join(outbox, "failures"), fixtures,
        str(tests_dir / "test_ingest_regressions.py"))

    assert len(capture_promote.collect_fixtures(fixtures)) == 1


def test_a_body_less_capture_is_not_promoted_into_an_empty_fixture(outbox, tmp_path):
    """Image failures carry metadata only, so there is nothing to replay."""
    _make_capture(outbox, workshop_id=1, selector=".a", http_status=200,
                  final_url="https://steamcommunity.com/sharedfiles/filedetails/?id=1")
    capture.record_image_download(9, "https://cdn.example.invalid/9.jpg", False,
                                  http_status=404, headers={"X-Cache": "MISS"},
                                  error="HTTP 404", error_type="Exception")
    tests_dir = tmp_path / "tests"
    fixtures = str(tests_dir / "fixtures")

    result = capture_promote.promote_all(
        os.path.join(outbox, "failures"), fixtures,
        str(tests_dir / "test_ingest_regressions.py"))

    assert len(result["promoted"]) == 1, "the web capture is still promotable"
    assert any("no captured body" in reason for _path, reason in result["skipped"])
    metas = capture_promote.collect_fixtures(fixtures)
    assert len(metas) == 1
    assert not any(m["kind"] == "image_download_failed" for m in metas)
