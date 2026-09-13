"""Promote real captured failures into replay regression tests.

This is the point of capturing at all. A capture that stays in the outbox is an
artefact nobody runs; promoted to a fixture it becomes a test that fails the day
the handling regresses.

``python3 -m src.capture_promote --from <outbox>/failures``

For each capture it writes:

* ``tests/fixtures/<area>/<name>.<ext>`` - the scrubbed response body;
* ``tests/fixtures/<area>/<name>.meta.json`` - how to replay it;
* ``tests/test_ingest_regressions.py`` - regenerated, parametrized over every
  fixture found.

The generated assertions are deliberately weak. Per the testing strategy, a test
that pins today's output cements today's bug: the first job of these tests is to
prove the input is *handled gracefully* and the payload is preserved. Choosing the
correct behaviour is a human decision, made by tightening one generated case.

Captured bodies can carry personal data (creator names, page text), so scrubbing
is applied on the way in, not left to the reviewer. The body is also the response
exactly as received, truncated to the capture cap.
"""

import argparse
import json
import os
import re
import sys

FIXTURE_AREAS = {
    "web_selector_miss": "web",
    "api_unparsed_body": "steam_api",
    "api_unhandled_status": "steam_api",
}

DEFAULT_FIXTURES = "tests/fixtures"
DEFAULT_TEST_FILE = "tests/test_ingest_regressions.py"

_SECRET_PATTERNS = [
    (re.compile(rb"(key=)[^&\s\"']+", re.IGNORECASE), rb"\1<redacted>"),
    (re.compile(rb"(api_?key\"?\s*[:=]\s*\"?)[^\",\s}]+", re.IGNORECASE), rb"\1<redacted>"),
    (re.compile(rb"(sessionid[=:\"\s]+)[^\s;\"'&]+", re.IGNORECASE), rb"\1<redacted>"),
    (re.compile(rb"(steamLoginSecure[=:\"\s]+)[^\s;\"'&]+", re.IGNORECASE), rb"\1<redacted>"),
]


def scrub(body: bytes) -> bytes:
    """Strip credentials that must never reach a committed fixture."""
    for pattern, replacement in _SECRET_PATTERNS:
        body = pattern.sub(replacement, body)
    return body


def area_for(kind: str) -> str:
    return FIXTURE_AREAS.get(kind, "other")


def extension_for(content_type, body: bytes) -> str:
    lowered = (content_type or "").lower()
    if "html" in lowered or body.lstrip()[:1] == b"<":
        return ".html"
    if "json" in lowered or body.lstrip()[:1] in (b"{", b"["):
        return ".json"
    return ".bin"


def fixture_name(record: dict, record_path: str) -> str:
    """Stable, human-readable fixture stem derived from the capture.

    The sample filename already carries the shape digest and a per-shape sample
    number (``<digest8>-<n>``), so the stem is unique on its own.
    """
    stem = os.path.splitext(os.path.basename(record_path))[0]
    kind = re.sub(r"[^a-z0-9]+", "_", str(record.get("kind", "failure")).lower())
    return f"{kind}_{stem}"


def load_capture(record_path: str, outbox_root: str) -> tuple:
    """Return ``(record, body_bytes)`` for one capture record."""
    with open(record_path, "r", encoding="utf-8") as handle:
        record = json.load(handle)
    body_rel = record.get("body_file")
    body = b""
    if body_rel:
        candidates = [
            os.path.join(outbox_root, body_rel),
            os.path.join(os.path.dirname(os.path.dirname(record_path)), body_rel),
            os.path.join(os.path.dirname(record_path), os.path.basename(body_rel)),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                with open(candidate, "rb") as handle:
                    body = handle.read()
                break
    return record, body


def promote(record_path: str, outbox_root: str, fixtures_root: str = DEFAULT_FIXTURES) -> dict:
    """Write one capture out as a fixture plus replay metadata."""
    record, body = load_capture(record_path, outbox_root)
    kind = record.get("kind", "failure")
    area = area_for(kind)
    name = fixture_name(record, record_path)
    body = scrub(body)
    ext = extension_for(record.get("content_type"), body)

    area_dir = os.path.join(fixtures_root, area)
    os.makedirs(area_dir, exist_ok=True)
    body_path = os.path.join(area_dir, name + ext)
    with open(body_path, "wb") as handle:
        handle.write(body)

    original_url = record.get("final_url") or ""
    meta = {
        "name": name,
        "area": area,
        "kind": kind,
        "selector": record.get("selector"),
        "http_status": record.get("http_status"),
        "workshop_id": record.get("workshop_id"),
        # Scrubbed: the API key travels in the query string.
        "url": scrub(original_url.encode("utf-8", "replace")).decode("utf-8", "replace"),
        "content_type": record.get("content_type"),
        "body": os.path.basename(body_path),
        "shape": record.get("shape"),
        "captured_at": record.get("captured_at"),
        "app_version": record.get("app_version"),
    }
    meta_path = os.path.join(area_dir, name + ".meta.json")
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return meta


def collect_fixtures(fixtures_root: str = DEFAULT_FIXTURES) -> list:
    metas = []
    if not os.path.isdir(fixtures_root):
        return metas
    for dirpath, _dirnames, filenames in os.walk(fixtures_root):
        for filename in sorted(filenames):
            if filename.endswith(".meta.json"):
                with open(os.path.join(dirpath, filename), "r", encoding="utf-8") as handle:
                    metas.append(json.load(handle))
    return sorted(metas, key=lambda m: (m.get("area", ""), m.get("name", "")))


def render_test_module(fixtures: list, fixtures_root: str = DEFAULT_FIXTURES,
                       test_file: str = DEFAULT_TEST_FILE) -> str:
    entries = []
    for meta in fixtures:
        rel = os.path.join(meta["area"], meta["body"]).replace(os.sep, "/")
        entries.append({
            "name": meta["name"],
            "area": meta["area"],
            "kind": meta.get("kind"),
            "body": rel,
            "url": meta.get("url") or "",
            "status": meta.get("http_status") or 200,
            "content_type": meta.get("content_type") or "text/html",
        })
    # Resolved against the generated file's own directory, so the module works
    # wherever the pair is placed. Using the repo root here would make the
    # default layout look for tests/tests/fixtures.
    fixtures_rel = os.path.relpath(
        fixtures_root, os.path.dirname(os.path.abspath(test_file)) or ".").replace(os.sep, "/")
    return _TEST_TEMPLATE.format(
        fixtures_rel=fixtures_rel,
        cases=json.dumps(entries, indent=8, sort_keys=True),
    )


_TEST_TEMPLATE = '''"""Replay regressions generated by src/capture_promote.py.

Do not edit by hand: regenerate from the captures instead.

Each case is a real response the scraper could not handle. The assertions are
deliberately weak - they prove the input is handled gracefully and the payload is
preserved, and nothing more. Tightening one case into a real assertion is a human
decision, and is the step that turns a capture into a regression test.
"""
import json
import os

import pytest
import responses

FIXTURES_ROOT = os.path.join(os.path.dirname(__file__), "{fixtures_rel}")

CASES = {cases}


def _case_id(case):
    return case["name"]


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_captured_failure_is_handled_gracefully(case):
    body_path = os.path.join(FIXTURES_ROOT, case["body"])
    with open(body_path, "rb") as handle:
        body = handle.read()

    if case["area"] == "web":
        from src.web_scraper import scrape_extended_details

        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, case["url"], body=body,
                     status=case["status"], content_type=case["content_type"])
            result = scrape_extended_details(case["url"])

        # Documented contract: None on a request failure, a dict otherwise.
        assert result is None or isinstance(result, dict)
        if isinstance(result, dict):
            assert "description" in result and "tags" in result
        return

    from src.steam_api import get_workshop_details_api

    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, case["url"], body=body,
                 status=case["status"], content_type=case["content_type"])
        try:
            result = get_workshop_details_api(1, "TEST_KEY")
        except ValueError:
            # Documented current behaviour: a non-JSON body propagates. Recorded
            # here so a change of behaviour is a deliberate edit, not an accident.
            return

    assert result is None or isinstance(result, dict)
'''


def promote_all(failures_dir: str, fixtures_root: str = DEFAULT_FIXTURES,
                test_file: str = DEFAULT_TEST_FILE) -> dict:
    """Promote every capture under ``failures_dir`` and rewrite the test module."""
    outbox_root = os.path.dirname(os.path.normpath(failures_dir))
    promoted, skipped = [], []
    for dirpath, _dirnames, filenames in sorted(os.walk(failures_dir)):
        for filename in sorted(filenames):
            if not filename.endswith(".json") or filename == "_group.json":
                continue
            record_path = os.path.join(dirpath, filename)
            try:
                promoted.append(promote(record_path, outbox_root, fixtures_root))
            except (OSError, ValueError, KeyError) as exc:
                skipped.append((record_path, str(exc)))

    fixtures = collect_fixtures(fixtures_root)
    if fixtures:
        os.makedirs(os.path.dirname(test_file) or ".", exist_ok=True)
        with open(test_file, "w", encoding="utf-8") as handle:
            handle.write(render_test_module(fixtures, fixtures_root, test_file))
    return {"promoted": promoted, "skipped": skipped, "fixtures": len(fixtures)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="failures_dir", required=True,
                        help="the <outbox>/failures directory to promote")
    parser.add_argument("--fixtures", default=DEFAULT_FIXTURES)
    parser.add_argument("--test", default=DEFAULT_TEST_FILE)
    args = parser.parse_args(argv)

    if not os.path.isdir(args.failures_dir):
        print(f"no such directory: {args.failures_dir}", file=sys.stderr)
        return 1

    result = promote_all(args.failures_dir, args.fixtures, args.test)
    print(f"promoted {len(result['promoted'])} capture(s); "
          f"{result['fixtures']} fixture(s) total")
    for path, reason in result["skipped"]:
        print(f"  skipped {path}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
