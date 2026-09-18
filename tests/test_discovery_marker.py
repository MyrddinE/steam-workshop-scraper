"""The discovery line marks the item being enriched, not the rejected one.

A discovery line used to carry a red `ignored` marker when the item failed its
enrichment filters and nothing at all when it passed, so the marker sat on the
uninteresting 99% of lines (*measured live* 2026-09-17 over the last 6 MB of
`scraper.log`: 53,523 of 54,057) and the item about to have its page and preview
fetched was the unmarked one. The line is inverted: the marker is dropped, and
`enriching` is appended in green when `_flag_scrape_and_image` returns true --
exactly "met the filter and is queued for additional details".

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


def _daemon(db_path, tmp_path) -> Daemon:
    config = {
        "database": {"path": db_path},
        "api": {"key": "TEST"},
        "daemon": {"batch_size": 1, "target_appids": [1]},
    }
    return Daemon(config, config_path=str(tmp_path / "config.yaml"))


def _discovery_line(db_path, tmp_path, caplog, *, enrich: bool) -> str:
    """Process one item with the enrichment filter answering `enrich`."""
    insert_or_update_item(db_path, {
        "workshop_id": 1, "title": "Sample", "status": 200, "api_priority": 5,
    })
    daemon = _daemon(db_path, tmp_path)
    existing = {"workshop_id": 1, "status": 200, "api_priority": 5}

    # `src.daemon` calls module-level `logging.info`, so the record rides the
    # root logger: lift the root level, not the module's.
    with caplog.at_level(logging.INFO), \
            mock.patch.object(daemon, "_should_enrich", return_value=enrich):
        daemon._process_item(existing, api_data={"title": "Sample", "status": 200})

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
