"""The coverage bars: what each bar's population is, and how both sides draw it.

The old `coverage` returned one count per stage, and its ``translated`` count was
``translate_version IS NOT NULL`` -- one item, one tick, whatever needed
translating and however many of its fields did. Three things were wrong with it,
and they are the properties these tests pin:

* **the unit**: the queue is per *field*, so an item with one non-ASCII field of
  its two counts once, not once per item, and its two fields count separately;
* **the population**: an ASCII-only item needs no translation, so it is excluded
  from the bar's population *and* from its maximum -- a library with nothing to
  translate has a zero-length bar, not a percentage stuck at 0.0%;
* **the dependency**: you cannot translate a description that was never pulled,
  so the Extended Web Translation bar can never be longer than the Extended Web
  bar above it.

Every bar's population is the same test that decides whether the field is
flagged, so the tests check the flagging rules rather than a rendering: non-empty
and non-ASCII, translated and current (``translation_is_current``). The three
translation bars deliberately have three different scopes, and the tests name
each one. The creator's name lives on ``users`` and is shared by every item that
creator made, so its bar counts *items*, which is what makes it comparable with
the per-item bars around it.

The rendering properties are checked on both sides: the three translation bars
are flush under their parents (no line, margin or row between them) and are
drawn with a distinct thinner treatment than the standard bar. The browser's row
rendering is executed under node -- the same functions the page runs, extracted
between the markers in ``templates/index.html`` -- so the assertion is about
what the browser draws, not about a string in the source.

Against the old code every test here fails: there was no ``bars`` list, no
``NOTHING_TO_TRANSLATE``, no ``coverage-child`` class and no marker block. The
failure output is recorded in the commit message.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src import metrics
from src.database import get_connection, insert_or_update_item, insert_or_update_user
from src.tui import StatsScreen

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "index.html"

COVERAGE_MARKER_START = "// ── coverage rows"
COVERAGE_MARKER_END = "// ── end coverage rows"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _item(db_path, workshop_id, appid=294100, tags=(), **over):
    record = {"workshop_id": workshop_id, "title": f"item {workshop_id}",
              "status": 200, "consumer_appid": appid, "tags": list(tags)}
    record.update(over)
    insert_or_update_item(db_path, record)


def _set_filters(db_path, appid, filters):
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO app_tracking (appid, enrichment_filters) VALUES (?, ?)",
        (appid, json.dumps(filters)),
    )
    conn.commit()
    conn.close()


def _coverage(db_path, target_appids=None):
    return metrics.values(metrics.compute(
        db_path, ["coverage"], {"target_appids": target_appids or []}
    ))["coverage"]


def _bars(scope):
    return {bar["key"]: bar for bar in scope["bars"]}


def _mature(db_path, appid=294100):
    _set_filters(db_path, appid, [{"field": "Tags", "op": "contains", "value": "Mature"}])


def _full_library(db_path):
    """One library that populates every bar and every scope.

    Item 1 is fully done, item 2 has fields still needing translation, item 3 is
    ASCII-only, item 4 is scraped with an empty description (the legitimate-blank
    ceiling) and item 5 is a bare row. One non-ASCII creator is translated and
    current; the other is not.
    """
    _mature(db_path)
    insert_or_update_user(db_path, {
        "steamid": 42, "personaname": "作者", "personaname_en": "Author",
        "api_fetched_at": 10, "translated_at": 20,
    })
    insert_or_update_user(db_path, {
        "steamid": 43, "personaname": "作家", "api_fetched_at": 10,
    })
    _item(db_path, 1, tags=["Mature"], title="テスト", title_en="Test",
          short_description="説明", short_description_en="Desc",
          extended_description="説明", extended_description_en="Desc",
          api_fetched_at=1, image_extension="jpg", creator=42,
          steam_updated_at=100, translate_version=100)
    _item(db_path, 2, tags=["Mature"], title="テスト2", short_description="説明2",
          extended_description="説明2", api_fetched_at=1, creator=43,
          steam_updated_at=100)
    _item(db_path, 3, tags=["Mature"], title="plain", short_description="plain",
          extended_description="plain", api_fetched_at=1, image_extension="jpg",
          creator=7)
    _item(db_path, 4, tags=["Mature"], title="blank page", web_scraped_at=5)
    _item(db_path, 5, tags=["Mature"], title="bare")


# --------------------------------------------------------------------------
# the unit: per field, not per item
# --------------------------------------------------------------------------


def test_one_non_ascii_field_of_two_counts_once_per_field(db_path):
    """The queue is three fields per item; the bar must be too.

    Item 1 has one non-ASCII field of its two; item 2 has both; item 3 has
    neither. The population is three fields, not three items and not the item
    count of the items that need anything (two). Only item 1's field is stored
    and current, so the fill is one field.
    """
    _mature(db_path)
    _item(db_path, 1, tags=["Mature"], title="テスト", title_en="Test",
          short_description="plain ascii", steam_updated_at=100, translate_version=100)
    _item(db_path, 2, tags=["Mature"], title="テスト2", short_description="説明",
          steam_updated_at=100)
    _item(db_path, 3, tags=["Mature"], title="plain", short_description="plain")

    bar = _bars(_coverage(db_path, [294100]))["translations"]

    assert bar["maximum"] == 3, "three fields need translation"
    assert bar["done"] == 1, "only the stored, current field is filled"
    assert bar["pct"] == round(1 / 3 * 100, 1)
    assert "1 filter-selected items need none" in bar["detail"]
    assert "translated" not in _bars(_coverage(db_path, [294100])), \
        "the old item-level translated count is gone"


def test_the_translation_scope_is_the_filter_selected_items(db_path):
    """`_queue_translations` returns unless the item was enriched.

    The Filters bar's API translations are only ever queued for filter-selected
    items, so the non-ASCII fields of an item the filters exclude are not in the
    population. The Extended Web Translation bar's scope is different on
    purpose: the scrape flags a description regardless of enrichment.
    """
    _mature(db_path)
    _item(db_path, 1, tags=["Mature"], title="テスト", short_description="説明",
          extended_description="説明")
    _item(db_path, 2, title="テスト2", short_description="説明2",
          extended_description="説明2")   # same AppID, fails the tag filter

    cov = _coverage(db_path, [294100])

    assert _bars(cov)["translations"]["maximum"] == 2, \
        "only the filter-selected item's two fields"
    assert _bars(cov)["web_translated"]["maximum"] == 2, \
        "the scrape queues the description whatever the filters select"


# --------------------------------------------------------------------------
# the population: ASCII-only items are excluded, and zero does not divide
# --------------------------------------------------------------------------


def test_ascii_only_library_has_nothing_to_translate(db_path):
    """Excluded from the population *and* from the maximum, not a stuck 0.0%."""
    _item(db_path, 1, title="Plain", short_description="Plain ascii")
    _item(db_path, 2, title="Another", short_description="Also ascii")

    bar = _bars(_coverage(db_path, []))["translations"]

    assert bar["maximum"] == 0
    assert bar["done"] == 0
    assert bar["pct"] is None, "a zero population is not a percentage at all"
    assert bar["empty"] == metrics.NOTHING_TO_TRANSLATE


def test_tui_reads_a_zero_population_as_nothing_to_translate(db_path):
    _item(db_path, 1, title="Plain", short_description="Plain ascii")

    text = StatsScreen._format_coverage(_coverage(db_path, []))
    line = next(line for line in text.splitlines() if line.startswith("Translations"))

    assert metrics.NOTHING_TO_TRANSLATE in line
    assert "%" not in line, "no 0.0% that could never move"
    assert "▁" in line, "the zero-length bar is still drawn, in the thin track"


# --------------------------------------------------------------------------
# the dependency: the child can never outrun its parent
# --------------------------------------------------------------------------


def test_extended_web_translation_never_exceeds_extended_web(db_path):
    """A non-ASCII description is a description, so the child fits inside it.

    The fixture has one current translation, one outstanding, one stale
    translation (a newer Steam revision), one ASCII description, one
    scraped-empty page and one bare item: the child's population is the
    non-ASCII descriptions, the parent's is every description the scrape can
    still supply.
    """
    _item(db_path, 1, extended_description="説明", extended_description_en="Desc",
          steam_updated_at=100, translate_version=100)
    _item(db_path, 2, extended_description="説明2", steam_updated_at=100)
    _item(db_path, 3, extended_description="説明3", extended_description_en="Old",
          steam_updated_at=200, translate_version=100)
    _item(db_path, 4, extended_description="plain ascii")
    _item(db_path, 5, web_scraped_at=5)          # scraped, answered empty
    _item(db_path, 6)                            # nothing yet

    cov = _coverage(db_path, [])
    parent = _bars(cov)["described"]
    child = _bars(cov)["web_translated"]

    assert parent["done"] == 4 and parent["maximum"] == 5
    assert child["maximum"] == 3 and child["done"] == 1
    assert child["maximum"] <= parent["maximum"]
    assert child["done"] <= parent["done"]
    assert child["pct"] <= parent["pct"]
    assert "answered with no description" in parent["detail"]


def test_a_fully_blank_web_library_has_a_zero_length_bar_at_both_levels(db_path):
    """Every page answered blank: the parent has nothing to reach either."""
    _item(db_path, 1, title="a", web_scraped_at=5)
    _item(db_path, 2, title="b", web_scraped_at=6)

    cov = _coverage(db_path, [])
    parent = _bars(cov)["described"]
    child = _bars(cov)["web_translated"]

    assert parent["maximum"] == 0 and parent["done"] == 0
    assert child["maximum"] == 0
    assert child["maximum"] <= parent["maximum"]


# --------------------------------------------------------------------------
# the creator: per item, from the users table
# --------------------------------------------------------------------------


def test_creator_translation_counts_items_by_their_creators_name(db_path):
    """The name lives per user; the bar is counted in items, like the others.

    The translated creator made two items, the untranslated one made one, the
    ASCII name one and the unknown creator one. Two of the three items behind a
    non-ASCII name are done -- an item count of two, not a user count of one.
    """
    insert_or_update_user(db_path, {
        "steamid": 42, "personaname": "作者", "personaname_en": "Author",
        "api_fetched_at": 10, "translated_at": 20,
    })
    insert_or_update_user(db_path, {
        "steamid": 43, "personaname": "作家", "api_fetched_at": 10,
    })
    insert_or_update_user(db_path, {
        "steamid": 44, "personaname": "Bob", "api_fetched_at": 10,
    })
    _item(db_path, 1, creator=42)
    _item(db_path, 2, creator=42)
    _item(db_path, 3, creator=43)
    _item(db_path, 4, creator=44)
    _item(db_path, 5, creator=99)   # no users row

    bar = _bars(_coverage(db_path, []))["creator_translated"]

    assert bar["maximum"] == 3, "three items behind a non-ASCII name"
    assert bar["done"] == 2, "the two items of the translated creator"
    assert bar["subsidiary"] is True
    assert "name is non-ASCII" in bar["detail"]


def test_a_creator_translation_is_stale_after_a_newer_persona_fetch(db_path):
    """`translated_at` is our clock, and the persona fetch moves the other one."""
    insert_or_update_user(db_path, {
        "steamid": 42, "personaname": "作者", "personaname_en": "Author",
        "api_fetched_at": 30, "translated_at": 20,   # name fetched after it was translated
    })
    _item(db_path, 1, creator=42)

    bar = _bars(_coverage(db_path, []))["creator_translated"]

    assert bar["maximum"] == 1
    assert bar["done"] == 0, "the stored translation is older than the fetched name"


# --------------------------------------------------------------------------
# both front ends: the rename, the flush pairs, the thin subsidiary bar
# --------------------------------------------------------------------------

#: The three parent/subsidiary pairs, by the label the metric owns.
COVERAGE_PAIRS = (
    ("API Data", "Translations"),
    ("Extended Web", "Extended Web Translation"),
    ("Creator", "Creator Translation"),
)


def _find_bar_line(lines, label):
    return next(line for line in lines if line.startswith(label))


def test_tui_parent_and_subsidiary_are_flush_and_the_child_is_thin(db_path):
    """The pair is two adjacent lines, the child using half-height glyphs.

    Flush means the child line immediately follows the parent line -- no blank
    line, no explanation, no separator -- and "thin" means the child draws
    lower-half/lower-eighth blocks where the parent draws a full cell and a
    shade. Both stay in the same column, so a shorter bar still means less
    coverage.
    """
    _full_library(db_path)
    lines = StatsScreen._coverage_block(_coverage(db_path, [294100]))

    for parent_label, child_label in COVERAGE_PAIRS:
        parent_index = next(i for i, line in enumerate(lines) if line.startswith(parent_label))
        child_index = next(i for i, line in enumerate(lines) if line.startswith(child_label))
        parent_line, child_line = lines[parent_index], lines[child_index]
        assert child_index == parent_index + 1, \
            f"{child_label} is not flush under {parent_label}"
        assert "█" in parent_line and "▄" not in parent_line
        assert "▄" in child_line and "█" not in child_line

    bar_lines = [line for line in lines
                 if any(line.startswith(label) for pair in COVERAGE_PAIRS for label in pair)]
    # Every bar's first glyph is in the same column: no subsidiary is indented.
    columns = {min(line.index(glyph) for glyph in "█░▄▁" if glyph in line)
               for line in bar_lines}
    assert len(columns) == 1, f"bars do not share a left edge: {sorted(columns)}"


def test_tui_uses_the_extended_web_rename(db_path):
    _full_library(db_path)
    text = StatsScreen._format_coverage(_coverage(db_path, [294100]))

    assert "Extended Web" in text
    assert "Extended Web Translation" in text
    assert not any(line.startswith("Description") for line in text.splitlines())
    assert not any(line.startswith("Web Description") for line in text.splitlines())


# --------------------------------------------------------------------------
# the browser side: the real page functions, executed
# --------------------------------------------------------------------------


def _coverage_js() -> str:
    """The coverage row functions, between their markers in the template.

    Extracted rather than re-implemented so the test runs the same code the
    browser does. The markers are part of the contract; a missing one is a
    failure, not a skip.
    """
    html = TEMPLATE.read_text(encoding="utf-8")
    start = html.index(COVERAGE_MARKER_START)
    end = html.index(COVERAGE_MARKER_END)
    end = html.index("\n", end)          # the whole marker line, comment and all
    return html[start:end]


def _render_web_rows(scope: dict) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - the environment ships node
        pytest.skip("node is not installed, so the page's rows cannot be executed")
    harness = (
        "function _escapeHtml(s){return String(s).replace(/&/g,'&amp;')"
        ".replace(/</g,'&lt;').replace(/>/g,'&gt;');}"
        "function fmtCount(n){return Number(n).toLocaleString('en-US');}"
        + _coverage_js()
        + "\nconsole.log(_coverageRows(" + json.dumps(scope) + "));"
    )
    completed = subprocess.run(
        [node, "-e", harness], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def test_web_parent_and_subsidiary_rows_are_flush(db_path):
    """No row, paragraph, margin or whitespace between the pair."""
    _full_library(db_path)
    html = _render_web_rows(_coverage(db_path, [294100]))

    for parent_label, child_label in COVERAGE_PAIRS:
        parent_row = html.index(">" + parent_label + "<")
        child_row = html.index(">" + child_label + "<")
        between = html[html.index("</tr>", parent_row):child_row]
        assert between.startswith('</tr><tr class="coverage-row coverage-child">'), \
            f"{child_label} is not flush under {parent_label}: {between[:80]!r}"
    assert html.count('</tr><tr class="coverage-row coverage-child">') == 3


def test_web_subsidiary_bar_is_drawn_thinner_than_the_standard_bar():
    """A smaller CSS height on the child's progress element, not a shorter bar."""
    html = TEMPLATE.read_text(encoding="utf-8")
    standard = re.search(r"\.coverage-table progress\s*\{[^}]*height:([^;]+);", html)
    child = re.search(
        r"\.coverage-table tr\.coverage-child progress\s*\{[^}]*height:([^;]+);", html)
    assert standard and child, "both coverage bar heights must be declared"
    assert float(child.group(1).rstrip("rem")) < float(standard.group(1).rstrip("rem"))
    # Flush rows: no vertical padding and no border spacing to open a gap.
    assert re.search(r"\.coverage-table\s*\{[^}]*border-collapse:collapse;", html)
    assert re.search(r"\.coverage-table td\s*\{[^}]*padding-top:0;[^}]*padding-bottom:0;", html)


def test_web_renders_the_extended_web_rename_from_the_metric(db_path):
    """The labels are the metric's, so the rename lands on both sides at once."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "COVERAGE_STAGES" not in html, "no hardcoded stage labels left"
    assert "bar.label" in _coverage_js()

    _full_library(db_path)
    rendered = _render_web_rows(_coverage(db_path, [294100]))
    assert ">Extended Web<" in rendered
    assert ">Extended Web Translation<" in rendered
    assert ">Description<" not in rendered


def test_web_reads_a_zero_population_as_nothing_to_translate(db_path):
    _item(db_path, 1, title="Plain", short_description="Plain ascii")

    rendered = _render_web_rows(_coverage(db_path, []))
    assert metrics.NOTHING_TO_TRANSLATE in rendered
    assert 'value="0" max="100"' in rendered
    assert "0.0%" not in rendered.split(">Translations<", 1)[1].split("</tr>", 1)[0]
