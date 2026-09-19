"""The discovery line's marker says whether anything was queued.

A discovery line used to carry a red `ignored` marker when the item failed its
enrichment filters and nothing at all when it passed, so the marker sat on the
uninteresting 99% of lines (*measured live* 2026-09-17 over the last 6 MB of
`scraper.log`: 53,523 of 54,057) and the item about to have its page and preview
fetched was the unmarked one. The line was inverted: `enriching` is appended in
green when `_raise_scrape_and_image_priorities` returns an enriched item.

That first form still claimed work that was not always queued. An item can match
its AppID's enrichment filters and have nothing to do -- its description is
current and its preview needs no attempt -- and it was drawn `enriching`
anyway. The marker is now decided by whether a scrape or image was actually
flagged, which gives three states:

* nothing queued -> `current` in grey (`\\033[90m`);
* queued and the filters matched -> `enriching` in green (`\\033[32m`);
* queued and the filters did not match -> no marker (the backlog scrape/image).

The word answers "was anything queued", not "did the filters match", so a
rejected item with nothing queued is `current` too. `enriched` still gates
translation and the creator refresh; only the marker uses the narrower fact.

These tests drive the real `_process_item` and read the emitted log record, so
they fail if the marker is put back the old way round. The colour is checked
against the web log viewer's own `ANSI_SGR` table in `templates/index.html`
rather than a second copy of the number: the viewer is what has to render the
code, and the TUI deliberately decodes no ANSI at all (`_poll_tail` writes each
raw line into a `RichLog`), so neither front end needed the change.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest import mock

from src.daemon import Daemon
from src.database import insert_or_update_item

_TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"

_ENRICHING_ESCAPE = "\033[32menriching\033[0m"
_CURRENT_ESCAPE = "\033[90mcurrent\033[0m"

# An item whose stored record is already at the fetched revision: same
# `steam_updated_at`/`time_updated`, a stored description, and a renderable
# image, so `_raise_scrape_and_image_priorities` has nothing to queue for it.
_CURRENT_ITEM = {
    "steam_updated_at": 1000,
    "extended_description": "Stored description",
    "image_extension": "jpg",
    "preview_url": "https://example.invalid/preview.jpg",
}
_CURRENT_API = {"time_updated": 1000}


def _daemon(db_path, tmp_path) -> Daemon:
    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST"},
        "daemon": {"batch_size": 1, "target_appids": [1]},
    }
    return Daemon(config, config_path=str(tmp_path / "config.yaml"))


def _discovery_line(db_path, tmp_path, caplog, *, enrich: bool,
                    existing: dict | None = None,
                    api_data: dict | None = None) -> str:
    """Process one item with the enrichment filter answering `enrich`.

    ``existing`` and ``api_data`` override fields of the pre-fetch record and
    the API payload, so a test can present an item on both sides of the revision
    test (the same number means "current", a different one means "changed").
    """
    row = {"workshop_id": 1, "title": "Sample", "status": 200, "api_priority": 5}
    row.update(existing or {})
    insert_or_update_item(db_path, row)
    daemon = _daemon(db_path, tmp_path)
    payload = {"title": "Sample", "status": 200}
    payload.update(api_data or {})

    # `src.daemon` calls module-level `logging.info`, so the record rides the
    # root logger: lift the root level, not the module's.
    with caplog.at_level(logging.INFO), \
            mock.patch.object(daemon, "_should_enrich", return_value=enrich):
        daemon._process_item(row, api_data=payload)

    lines = [r.getMessage() for r in caplog.records if "[A:1]" in r.getMessage()]
    assert len(lines) == 1, lines
    return lines[0]


def test_an_enriched_item_is_marked_enriching_in_green(db_path, tmp_path, caplog):
    line = _discovery_line(db_path, tmp_path, caplog, enrich=True)

    assert _ENRICHING_ESCAPE in line, line
    assert "ignored" not in line, "the rejected item's marker must not come back"


def test_a_rejected_item_carries_no_marker(db_path, tmp_path, caplog):
    line = _discovery_line(db_path, tmp_path, caplog, enrich=False)

    assert "\033[" not in line, f"the common line must be unmarked: {line!r}"
    assert "ignored" not in line, line
    assert "enriching" not in line, line


def test_the_marker_colour_is_the_green_the_web_viewer_renders(db_path, tmp_path, caplog):
    line = _discovery_line(db_path, tmp_path, caplog, enrich=True)

    codes = re.findall(r"\033\[(\d+)m", line)
    colours = [code for code in codes if code != "0"]  # 0 is the reset
    assert colours == ["32"], f"one green run, and it is SGR 32: {codes}"

    template = _TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"\b32:\s*'(#[0-9a-fA-F]{6})'", template)
    assert match, "the web viewer must map SGR 32"
    assert match.group(1) == "#98c379", (
        "the viewer's colour for the marker code must be its green"
    )


# --- an enriched item can still have nothing to queue ------------------------
#
# `enriched` is "matched the AppID's filters", which is a wider fact than "work
# was queued". When the stored description is already at the item's revision and
# the preview needs no attempt, no scrape and no image is flagged, so the line
# must not claim `enriching`. These drive the real `_process_item`, so the tests
# fail against the old marker, which was chosen from `enriched` alone.

def test_an_enriched_item_with_nothing_to_queue_is_marked_current(db_path, tmp_path, caplog):
    line = _discovery_line(db_path, tmp_path, caplog, enrich=True,
                           existing=_CURRENT_ITEM, api_data=_CURRENT_API)

    assert _CURRENT_ESCAPE in line, line
    assert "enriching" not in line, (
        "an enriched item that queued nothing must not claim the enriching marker"
    )


def test_a_rejected_item_with_nothing_to_queue_is_marked_current(db_path, tmp_path, caplog):
    """The same item rejected: the word answers "queued", not "filtered"."""
    line = _discovery_line(db_path, tmp_path, caplog, enrich=False,
                           existing=_CURRENT_ITEM, api_data=_CURRENT_API)

    assert _CURRENT_ESCAPE in line, line
    assert "enriching" not in line, line


def test_an_enriched_item_with_only_the_image_queued_is_still_enriching(
        db_path, tmp_path, caplog):
    """A queued image is queued work, even when the description is current."""
    with mock.patch("src.daemon.raise_web_scrape_priority") as web, \
            mock.patch("src.daemon.raise_image_priority") as image:
        line = _discovery_line(db_path, tmp_path, caplog, enrich=True,
                               existing={**_CURRENT_ITEM, "image_extension": None},
                               api_data=_CURRENT_API)

    web.assert_not_called()
    image.assert_called_once()
    assert _ENRICHING_ESCAPE in line, line
    assert "current" not in line, line


def test_the_current_marker_colour_is_the_grey_the_web_viewer_renders(
        db_path, tmp_path, caplog):
    line = _discovery_line(db_path, tmp_path, caplog, enrich=True,
                           existing=_CURRENT_ITEM, api_data=_CURRENT_API)

    codes = re.findall(r"\033\[(\d+)m", line)
    colours = [code for code in codes if code != "0"]  # 0 is the reset
    assert colours == ["90"], f"one grey run, and it is SGR 90: {codes}"

    template = _TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"\b90:\s*'(#[0-9a-fA-F]{6})'", template)
    assert match, "the web viewer must map SGR 90"
    assert match.group(1) == "#6b7280", (
        "the viewer's colour for the 'current' marker must be its grey"
    )
    assert match.group(1) != "#98c379", "the marker must not be the green one"
