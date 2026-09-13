"""Failure capture: bounded, additive, and registered for the puller.

The interesting properties are not "does it write a file" but the bounds. A
persistent break must cost a bounded number of files, and a page whose contents
rotate must not defeat that bound by producing a new shape digest per fetch.
"""
import hashlib
import json
import os

import pytest

from src import capture


@pytest.fixture
def outbox(tmp_path):
    """A configured outbox, torn down afterwards so tests stay independent."""
    root = tmp_path / "outbox"
    capture.configure(str(root))
    yield str(root)
    capture.configure(None)


def _manifest(outbox):
    with open(os.path.join(outbox, "manifest.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _failure_entries(outbox):
    return [a for a in _manifest(outbox)["artifacts"] if a.get("kind") == "failure"]


def _samples(outbox, group):
    group_dir = os.path.join(outbox, "failures", group)
    return sorted(n for n in os.listdir(group_dir)
                  if n.endswith(".json") and n != "_group.json")


def _group_state(outbox, group):
    with open(os.path.join(outbox, "failures", group, "_group.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _html(classes="workshopItemDescription", title="Steam Workshop", extra=""):
    return (f'<html><head><title>{title}</title></head><body>'
            f'<div class="{classes}">{extra}</div></body></html>')


# ── off by default ───────────────────────────────────────────────────────────

def test_capture_is_off_until_configured(tmp_path):
    capture.configure(None)
    assert capture.is_enabled() is False
    assert capture.record_failure("web_selector_miss", body="<html></html>") is None
    assert not os.path.exists(str(tmp_path / "outbox"))


def test_no_op_does_not_touch_the_filesystem(tmp_path):
    capture.configure(None)
    capture.record_failure("api_unhandled_status", http_status=403, body="{}")
    assert os.listdir(tmp_path) == []


# ── the record ───────────────────────────────────────────────────────────────

def test_record_carries_every_required_field(outbox):
    record = capture.record_failure(
        kind="web_selector_miss",
        stage="web_scrape",
        workshop_id=12345,
        selector=".workshopItemDescription#highlightContent",
        http_status=200,
        final_url="https://steamcommunity.com/sharedfiles/filedetails/?id=12345",
        body=_html(),
        content_type="text/html",
    )

    for field in ("kind", "stage", "workshop_id", "selector", "http_status",
                  "final_url", "body_file", "body_bytes", "body_sha256",
                  "shape", "captured_at", "app_version"):
        assert field in record, f"capture record is missing {field}"

    assert record["workshop_id"] == 12345
    assert record["http_status"] == 200
    assert set(record["shape"]) == {"class_digest", "class_count", "title_tag"}
    assert record["shape"]["class_count"] == 1
    assert record["shape"]["title_tag"] == "Steam Workshop"


def test_body_file_matches_the_recorded_hash(outbox):
    body = _html()
    record = capture.record_failure("web_selector_miss", workshop_id=1, body=body)

    body_path = os.path.join(outbox, record["body_file"])
    with open(body_path, "rb") as handle:
        stored = handle.read()

    assert stored == body.encode()
    assert hashlib.sha256(stored).hexdigest() == record["body_sha256"]
    assert record["body_bytes"] == len(body.encode())
    assert record["body_truncated"] is False


def test_oversized_body_is_truncated_but_hashed_in_full(outbox):
    body = "<html>" + ("x" * (capture.MAX_BODY_BYTES + 5000)) + "</html>"
    record = capture.record_failure("web_selector_miss", workshop_id=1, body=body)

    body_path = os.path.join(outbox, record["body_file"])
    assert os.path.getsize(body_path) == capture.MAX_BODY_BYTES
    assert record["body_truncated"] is True
    assert record["body_bytes"] == len(body.encode())
    assert record["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()


# ── shape digest ─────────────────────────────────────────────────────────────

def test_shape_digest_ignores_page_text_but_tracks_classes():
    same_a, _, _ = capture.describe_shape(_html(extra="first fetch").encode())
    same_b, _, _ = capture.describe_shape(_html(extra="totally different text").encode())
    other, _, _ = capture.describe_shape(_html(classes="newLayoutBlock other").encode())

    assert same_a == same_b, "rotating page text must not change the shape"
    assert same_a != other, "a changed class set must change the shape"


def test_shape_digest_handles_non_html_bodies():
    digest, count, title = capture.describe_shape(b'{"response": {"publishedfiledetails": []}}')
    assert len(digest) == 64
    assert count == 0
    assert title is None

    # Not JSON either: still a stable digest, and no exception.
    plain, _, _ = capture.describe_shape(b"<html>proxy error 503</html>")
    assert len(plain) == 64


# ── bounds ───────────────────────────────────────────────────────────────────

def test_only_the_first_n_samples_of_a_shape_are_kept(outbox):
    for i in range(capture.SAMPLES_PER_DIGEST + 4):
        capture.record_failure("web_selector_miss", workshop_id=i, body=_html())

    group = capture.group_id("web_selector_miss", None, None)
    files = _samples(outbox, group)
    assert len(files) == capture.SAMPLES_PER_DIGEST

    state = _group_state(outbox, group)
    assert state["sample_count"] == capture.SAMPLES_PER_DIGEST
    assert state["total_misses"] == capture.SAMPLES_PER_DIGEST + 4, \
        "every miss must be counted even when it is not captured"


def test_a_changed_shape_is_captured_again(outbox):
    capture.record_failure("web_selector_miss", workshop_id=1, body=_html(classes="layoutV1"))
    capture.record_failure("web_selector_miss", workshop_id=2, body=_html(classes="layoutV2"))

    group = capture.group_id("web_selector_miss", None, None)
    assert len(_samples(outbox, group)) == 2
    state = _group_state(outbox, group)
    assert len(state["digests"]) == 2


def test_rotating_content_cannot_defeat_the_file_bound(outbox):
    """The weakness the hard cap exists for: a new digest on every fetch."""
    for i in range(capture.MAX_VARIANTS_PER_GROUP * 4):
        # A different class set each time would otherwise capture a file per fetch.
        capture.record_failure("web_selector_miss", workshop_id=i,
                               body=_html(classes=f"layout{i} rotating"))

    group = capture.group_id("web_selector_miss", None, None)
    files = _samples(outbox, group)
    # 20 distinct shapes arrived; each was seen once, so the first
    # MAX_VARIANTS_PER_GROUP shapes are kept with one sample apiece.
    assert len(files) == capture.MAX_VARIANTS_PER_GROUP
    assert len(files) <= capture.MAX_VARIANTS_PER_GROUP * capture.SAMPLES_PER_DIGEST

    state = _group_state(outbox, group)
    assert state["variants_truncated"] is True
    assert len(state["digests"]) == capture.MAX_VARIANTS_PER_GROUP
    assert state["total_misses"] == capture.MAX_VARIANTS_PER_GROUP * 4


def test_a_flapping_shape_stays_bounded(outbox):
    """Two shapes alternating forever must cost a fixed number of files."""
    for i in range(30):
        capture.record_failure("web_selector_miss", workshop_id=i,
                               body=_html(classes="layoutA" if i % 2 else "layoutB"))

    group = capture.group_id("web_selector_miss", None, None)
    assert len(_samples(outbox, group)) == 2 * capture.SAMPLES_PER_DIGEST

    state = _group_state(outbox, group)
    assert state["total_misses"] == 30
    assert len(state["digests"]) == 2
    assert state["variants_truncated"] is False


def test_caps_survive_a_restart(outbox):
    """State is reloaded from disk, so a restart cannot reset the counters."""
    group = capture.group_id("web_selector_miss", None, None)
    for i in range(capture.SAMPLES_PER_DIGEST + 3):
        capture.record_failure("web_selector_miss", workshop_id=i, body=_html())

    before = _group_state(outbox, group)
    # Simulate a fresh process: drop all in-memory group state.
    capture.configure(outbox)
    capture.record_failure("web_selector_miss", workshop_id=99, body=_html())

    after = _group_state(outbox, group)
    assert after["total_misses"] == before["total_misses"] + 1
    assert after["sample_count"] == before["sample_count"], "cap was reset by the restart"
    assert len(_samples(outbox, group)) == capture.SAMPLES_PER_DIGEST


def test_group_state_is_rebuilt_when_it_is_missing(outbox):
    capture.record_failure("web_selector_miss", workshop_id=1, body=_html())
    group = capture.group_id("web_selector_miss", None, None)
    os.remove(os.path.join(outbox, "failures", group, "_group.json"))

    capture.configure(outbox)
    capture.record_failure("web_selector_miss", workshop_id=2, body=_html())

    state = _group_state(outbox, group)
    assert state["sample_count"] == 2, "samples on disk are the authority"


def test_distinct_groups_do_not_share_a_budget(outbox):
    for i in range(capture.SAMPLES_PER_DIGEST + 2):
        capture.record_failure("web_selector_miss", selector=".a", workshop_id=i, body=_html())
        capture.record_failure("web_selector_miss", selector=".b", workshop_id=i, body=_html())

    for selector in (".a", ".b"):
        group = capture.group_id("web_selector_miss", selector, None)
        assert len(_samples(outbox, group)) == capture.SAMPLES_PER_DIGEST


# ── manifest ─────────────────────────────────────────────────────────────────

def test_every_captured_file_is_registered_for_the_puller(outbox):
    capture.record_failure("web_selector_miss", workshop_id=1, body=_html(),
                           http_status=200, final_url="https://example.invalid/?id=1")

    entries = _failure_entries(outbox)
    roles = sorted(e["role"] for e in entries)
    assert roles == ["body", "group", "sample"]

    for entry in entries:
        # sync-outbox.py pulls one file per entry, checks sha256, and only
        # gunzips when compression is declared. These must be plain files.
        assert not entry["path"].endswith(".gz")
        assert "compression" not in entry
        full = os.path.join(outbox, entry["path"])
        with open(full, "rb") as handle:
            assert hashlib.sha256(handle.read()).hexdigest() == entry["sha256"]
        assert entry["group"]


def test_group_entry_carries_the_scale_counters(outbox):
    for i in range(5):
        capture.record_failure("web_selector_miss", workshop_id=i, body=_html())

    group_entry = next(e for e in _failure_entries(outbox) if e["role"] == "group")
    assert group_entry["sample_count"] == capture.SAMPLES_PER_DIGEST
    assert group_entry["total_misses"] == 5
    assert group_entry["first_seen"] and group_entry["last_seen"]
    assert group_entry["first_seen"] <= group_entry["last_seen"]


def test_repeated_captures_do_not_duplicate_manifest_entries(outbox):
    for i in range(4):
        capture.record_failure("api_unhandled_status", workshop_id=i, http_status=403,
                               body='{"response": {}}')
    after = _failure_entries(outbox)
    assert after, "nothing was captured"
    paths = [e["path"] for e in after]
    assert len(paths) == len(set(paths)), "manifest accumulated duplicate paths"


# ── robustness ───────────────────────────────────────────────────────────────

def test_capture_never_raises_when_the_outbox_cannot_be_written(tmp_path):
    """A diagnostic that can break the scrape loop is worse than no diagnostic."""
    # A path that cannot be a directory: an existing regular file.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    capture.configure(str(blocker))
    try:
        assert capture.record_failure("web_selector_miss", workshop_id=1, body=_html()) is None
    finally:
        capture.configure(None)


def test_app_version_is_reported(outbox):
    record = capture.record_failure("web_selector_miss", workshop_id=1, body=_html())
    assert record["app_version"]
