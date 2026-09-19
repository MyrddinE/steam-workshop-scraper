"""The Subscribed field, its overlay control and the `Subscribed at` sort.

The field is the first enum in SEARCH_FILTER_SCHEMA and the first whose predicate spans
more than one column, so these tests pin the two things that field invites:

* the SQL builder and the in-memory mirror answer the same question, including
  every negation (they read one shared value table, and this is the test that
  says so rather than the code comment);
* every in-memory evaluation site actually carries the four columns, because a
  row dict missing one reads as NULL -- `never`, a false `queued`, a false
  `downloaded` -- and silently changes the answer instead of raising.

The front ends are exercised where the control lives: Textual through a real
app, the served JavaScript through node against the real functions, as the rest
of the web tests do.
"""
import json
import pytest
from unittest.mock import patch

from src.database import (
    initialize_database, insert_or_update_item, get_connection, search_items,
    _evaluate_filters, _build_sort_clause, _demote_filtered_out_queue_priorities,
    save_enrichment_filters, VALID_SORT_COLS,
)
from tests.conftest import ASYNC_PAUSE

# Literals rather than an import of the new constants, so this file still
# collects against a checkout without the field and each test fails on its own
# behaviour rather than as one collection error.
SUBSCRIBED_FIELD = "Subscribed"
SUBSCRIBED_VALUES = ["any", "never", "subscribed", "previously", "queued", "downloaded"]


# ── a database with one row per subscription state ───────────────────────────
#
# ids: 1 never | 2 subscribed | 3 previously | 4 queued (never) | 5 downloaded |
#      6 subscribed | 7 previously + queued | 8 downloaded
_EXPECTED = {
    "any": {1, 2, 3, 4, 5, 6, 7, 8},
    "never": {1, 4},
    "subscribed": {2, 5, 6, 8},
    "previously": {3, 7},
    "queued": {4, 7},
    "downloaded": {5, 8},
}

_FLAGS = {
    1: dict(own_subscribed=0, own_first_subscribed_at=None, is_queued_for_subscription=0, downloaded_at=None),
    2: dict(own_subscribed=1, own_first_subscribed_at=1000, is_queued_for_subscription=0, downloaded_at=None),
    3: dict(own_subscribed=0, own_first_subscribed_at=2000, is_queued_for_subscription=0, downloaded_at=None),
    4: dict(own_subscribed=0, own_first_subscribed_at=None, is_queued_for_subscription=1, downloaded_at=None),
    5: dict(own_subscribed=1, own_first_subscribed_at=3000, is_queued_for_subscription=0, downloaded_at=4000),
    6: dict(own_subscribed=1, own_first_subscribed_at=1500, is_queued_for_subscription=0, downloaded_at=None),
    7: dict(own_subscribed=0, own_first_subscribed_at=2500, is_queued_for_subscription=1, downloaded_at=None),
    8: dict(own_subscribed=1, own_first_subscribed_at=3500, is_queued_for_subscription=0, downloaded_at=4500),
}


@pytest.fixture
def subscribed_db(tmp_path):
    db_path = str(tmp_path / "subscribed.db")
    initialize_database(db_path)
    for wid, flags in _FLAGS.items():
        insert_or_update_item(db_path, {
            "workshop_id": wid,
            "title": f"Item {wid}",
            "consumer_appid": 294100,
            "fetch_status": 200,
            **flags,
        })
    return db_path


def _ids(rows):
    return {r["workshop_id"] for r in rows}


def _sql_ids(db_path, op, value):
    return _ids(search_items(
        db_path, filters=[{"field": SUBSCRIBED_FIELD, "op": op, "value": value}]))


def _memory_ids(db_path, op, value):
    criterion = {"field": SUBSCRIBED_FIELD, "op": op, "value": value}
    return {r["workshop_id"] for r in search_items(db_path)
            if _evaluate_filters(r, [criterion])}


# ── the six values, in both evaluators and cross-checked ─────────────────────

@pytest.mark.parametrize("value", SUBSCRIBED_VALUES)
def test_each_value_matches_its_predicate_in_sql(subscribed_db, value):
    assert _sql_ids(subscribed_db, "is", value) == _EXPECTED[value]


@pytest.mark.parametrize("value", SUBSCRIBED_VALUES)
def test_each_value_matches_its_predicate_in_memory(subscribed_db, value):
    assert _memory_ids(subscribed_db, "is", value) == _EXPECTED[value]


@pytest.mark.parametrize("op", ["is", "is_not"])
@pytest.mark.parametrize("value", SUBSCRIBED_VALUES)
def test_the_two_evaluators_agree_on_every_value_and_negation(subscribed_db, op, value):
    """The drift this field invites: one evaluator answering differently.

    The two reviewers are fed the same rows and the same filter, so a value the
    SQL builder and `_evaluate_single_filter` disagree about fails here rather
    than in the daemon's queue or the demotion walk.
    """
    assert _sql_ids(subscribed_db, op, value) == _memory_ids(subscribed_db, op, value)


@pytest.mark.parametrize("value", SUBSCRIBED_VALUES)
def test_every_negation_is_the_exact_complement(subscribed_db, value):
    """`is_not X` is everything `is X` did not match -- no gap, no overlap."""
    all_ids = _EXPECTED["any"]
    assert _sql_ids(subscribed_db, "is_not", value) == all_ids - _EXPECTED[value]
    assert _memory_ids(subscribed_db, "is_not", value) == all_ids - _EXPECTED[value]


def test_is_not_any_matches_nothing_in_both_evaluators(subscribed_db):
    """The combination the front ends refuse to build, pinned anyway.

    A NOT over "everything" is the empty set. A saved filter or an API call can
    still carry it, so both evaluators have to return the same (empty) answer.
    """
    assert _sql_ids(subscribed_db, "is_not", "any") == set()
    assert _memory_ids(subscribed_db, "is_not", "any") == set()


# ── the overlay: one predicate ANDed onto the builder's rows ─────────────────

def test_overlay_returns_the_intersection_with_the_builder_rows(subscribed_db):
    builder = [{"field": "Title", "op": "contains", "value": "Item"}]
    got = _ids(search_items(subscribed_db, filters=builder, subscribed_overlay="subscribed"))
    assert got == _EXPECTED["subscribed"]

    # A narrow builder row intersected with an overlay that excludes it is empty.
    narrow = [{"field": "Title", "op": "contains", "value": "Item 2"}]
    assert _ids(search_items(subscribed_db, filters=narrow, subscribed_overlay="previously")) == set()


def test_overlay_any_changes_nothing(subscribed_db):
    builder = [{"field": "Title", "op": "contains", "value": "Item"}]
    without = _ids(search_items(subscribed_db, filters=builder))
    with_any = _ids(search_items(subscribed_db, filters=builder, subscribed_overlay="any"))
    assert without == with_any
    assert without == _EXPECTED["any"]


def test_overlay_is_anded_outside_the_builder_group(subscribed_db):
    """An OR row must not be able to pull back what the overlay excluded.

    The overlay is applied as `AND (...)` after the builder's own parenthesised
    group. If it were appended as another row it would be ORed with the second
    row and `Item 4` (never subscribed) would leak through a `subscribed` view.
    """
    filters = [
        {"field": "Title", "op": "contains", "value": "Item 2"},
        {"logic": "OR", "field": "Title", "op": "contains", "value": "Item 4"},
    ]
    assert _ids(search_items(subscribed_db)) == _EXPECTED["any"]
    got = _ids(search_items(subscribed_db, filters=filters, subscribed_overlay="subscribed"))
    assert got == {2}


def test_unknown_overlay_value_constrains_nothing(subscribed_db):
    got = _ids(search_items(subscribed_db, subscribed_overlay="from-a-newer-build"))
    assert got == _EXPECTED["any"]


def test_a_legacy_overlay_value_is_normalised(subscribed_db):
    """A saved view still naming `currently` must not silently widen to `any`."""
    legacy = _ids(search_items(subscribed_db, subscribed_overlay="currently"))
    assert legacy == _EXPECTED["subscribed"]


def test_a_legacy_filter_value_is_normalised(subscribed_db):
    """A saved builder row still naming `pending` must keep naming `queued`."""
    legacy = _ids(search_items(
        subscribed_db,
        filters=[{"field": SUBSCRIBED_FIELD, "op": "is", "value": "pending"}]))
    assert legacy == _EXPECTED["queued"]
    assert _memory_ids(subscribed_db, "is", "pending") == _EXPECTED["queued"]


# ── the sort ─────────────────────────────────────────────────────────────────

def test_subscribed_at_is_an_allowed_sort_column(subscribed_db):
    assert "own_first_subscribed_at" in VALID_SORT_COLS


def test_subscribed_at_descending_puts_nulls_last(subscribed_db):
    """NULL means never subscribed, so descending must end with those rows.

    SQLite orders NULL below every value, so a plain DESC already does this;
    the test is here because that is a database behaviour, not an assumption.
    """
    rows = search_items(subscribed_db, sort_by="own_first_subscribed_at", sort_order="DESC")
    stamps = [r["own_first_subscribed_at"] for r in rows]
    assert len(stamps) == len(_EXPECTED["any"])
    non_null = [s for s in stamps if s is not None]
    assert non_null == sorted(non_null, reverse=True)
    assert len(non_null) == 6
    # Every dated row precedes every never-subscribed one.
    first_null = stamps.index(None)
    assert all(s is not None for s in stamps[:first_null])
    assert all(s is None for s in stamps[first_null:])


def test_an_unknown_sort_is_still_rejected(subscribed_db):
    assert _build_sort_clause("own_first_subscribed_at; DROP TABLE workshop_items", "DESC") == ""
    # A rejected sort leaves the result set intact rather than reaching SQL.
    assert _ids(search_items(subscribed_db, sort_by="own_first_subscribed_at; DROP TABLE workshop_items",
                             sort_order="DESC")) == _EXPECTED["any"]


# ── the in-memory sites that must carry the four columns ─────────────────────
#
# _evaluate_single_filter reads item.get(db_col), so a row dict missing a column
# answers from its NULL instead of raising. These two drive the real sites with
# records shaped the way the site builds them.

def _daemon_for(db_path, appid=294100):
    from src.daemon import Daemon
    return Daemon({
        "database": {"path": db_path},
        "api": {"key": "TEST_KEY"},
        "daemon": {"target_appids": [appid], "api_batch_size": 1, "request_delay_seconds": 0},
    })


def _item(**over):
    item = {
        "workshop_id": 1,
        "consumer_appid": 294100,
        "title": "A wallpaper",
        "steam_updated_at": 1000,
        "extended_description": None,
        "preview_url": None,
        "tags": [],
        "own_subscribed": 0,
        "own_first_subscribed_at": None,
        "is_queued_for_subscription": 0,
        "downloaded_at": None,
    }
    item.update(over)
    return item


def _flag(daemon, merged, existing, api_priority=3):
    with patch("src.daemon.raise_web_scrape_priority") as web, \
         patch("src.daemon.raise_image_priority") as img:
        outcome = daemon._raise_scrape_and_image_priorities(merged, existing, existing["workshop_id"], api_priority)
    return outcome, web, img


def test_daemon_reads_queued_from_the_prefetch_record(db_path):
    """`is_queued_for_subscription` is dropped by the merge, so the merged dict
    alone reads it as NULL and the daemon would answer "not queued"."""
    from src.daemon import MERGE_EXCLUDED_KEYS
    save_enrichment_filters(db_path, 294100, enrichment_filters=json.dumps(
        [{"field": SUBSCRIBED_FIELD, "op": "is", "value": "queued"}]))
    daemon = _daemon_for(db_path)

    existing = _item(is_queued_for_subscription=1)
    merged = {k: v for k, v in existing.items() if k not in MERGE_EXCLUDED_KEYS}
    assert "is_queued_for_subscription" not in merged

    outcome, web, _img = _flag(daemon, merged, existing)
    assert outcome.enriched is True
    web.assert_called_once_with(db_path, 1, 3)


def test_daemon_reads_downloaded_from_the_prefetch_record(db_path):
    """`downloaded_at` is excluded from the merge for the same reason."""
    from src.daemon import MERGE_EXCLUDED_KEYS
    save_enrichment_filters(db_path, 294100, enrichment_filters=json.dumps(
        [{"field": SUBSCRIBED_FIELD, "op": "is", "value": "downloaded"}]))
    daemon = _daemon_for(db_path)

    existing = _item(own_subscribed=1, own_first_subscribed_at=1000, downloaded_at=5000)
    merged = {k: v for k, v in existing.items() if k not in MERGE_EXCLUDED_KEYS}
    assert "downloaded_at" not in merged

    outcome, web, _img = _flag(daemon, merged, existing)
    assert outcome.enriched is True
    web.assert_called_once_with(db_path, 1, 3)


def test_demotion_walk_loads_the_columns_its_filter_reads(subscribed_db):
    """The migration walk selects only the referenced columns; the Subscribed
    field's virtual column expands to four, or the queue flag it reads is NULL."""
    save_enrichment_filters(subscribed_db, 294100, enrichment_filters=json.dumps(
        [{"field": SUBSCRIBED_FIELD, "op": "is", "value": "queued"}]))
    conn = get_connection(subscribed_db)
    conn.execute("UPDATE workshop_items SET needs_web_scrape = 2 WHERE workshop_id = 4")
    conn.execute("UPDATE workshop_items SET needs_web_scrape = 2 WHERE workshop_id = 1")
    conn.commit()

    _demote_filtered_out_queue_priorities(conn)
    conn.close()

    conn = get_connection(subscribed_db)
    priorities = {r["workshop_id"]: r["needs_web_scrape"] for r in conn.execute(
        "SELECT workshop_id, needs_web_scrape FROM workshop_items")}
    conn.close()

    assert priorities[4] == 2, "the queued item matches the filter and is left alone"
    assert priorities[1] == 1, "the never-subscribed item is excluded and demoted"


# ── the value control on both sides ──────────────────────────────────────────

def test_enum_value_options_drop_any_for_is_not():
    """Pure choice table behind the TUI control."""
    from src.tui import _enum_value_options
    assert [v for _, v in _enum_value_options(SUBSCRIBED_FIELD, "is")] == SUBSCRIBED_VALUES
    assert [v for _, v in _enum_value_options(SUBSCRIBED_FIELD, "is_not")] == \
        [v for v in SUBSCRIBED_VALUES if v != "any"]


@pytest.mark.asyncio
async def test_tui_enum_field_uses_a_select_where_others_use_free_text(mock_config):
    from src.tui import ScraperApp, SearchBuilder, SearchRow
    from textual.widgets import Select, Input

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.load_tui_state', return_value={}), \
         patch('src.tui.save_tui_state'), \
         patch('src.tui.search_items', return_value=[]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            builder = app.query_one("#search-builder", SearchBuilder)

            builder.set_filters([{"field": SUBSCRIBED_FIELD, "op": "is", "value": "never"}])
            await pilot.pause(ASYNC_PAUSE * 2)
            row = list(builder.query(SearchRow))[0]
            assert row.query_one("#value-input", Input).display is False
            value_select = row.query_one("#value-select", Select)
            assert value_select.display is True
            assert value_select.value == "never"

            # `is_not` offers the value list without `any`.
            builder.set_filters([{"field": SUBSCRIBED_FIELD, "op": "is_not", "value": "never"}])
            await pilot.pause(ASYNC_PAUSE * 2)
            row = list(builder.query(SearchRow))[0]
            offered = [v for _, v in row.query_one("#value-select", Select)._options
                       if v is not Select.NULL]
            assert "any" not in offered
            assert set(offered) == set(SUBSCRIBED_VALUES) - {"any"}

            # Every other field keeps the free-text input.
            builder.set_filters([{"field": "Title", "op": "contains", "value": "x"}])
            await pilot.pause(ASYNC_PAUSE * 2)
            row = list(builder.query(SearchRow))[0]
            assert row.query_one("#value-input", Input).display is True
            assert row.query_one("#value-select", Select).display is False


# ── the overlay control and its rules ────────────────────────────────────────

@pytest.mark.asyncio
async def test_tui_overlay_is_greyed_out_by_a_builder_subscribed_row(mock_config):
    from src.tui import ScraperApp, SearchBuilder
    from textual.widgets import Select

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.load_tui_state', return_value={}), \
         patch('src.tui.save_tui_state'), \
         patch('src.tui.search_items', return_value=[]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            builder = app.query_one("#search-builder", SearchBuilder)
            overlay = app.query_one("#subscribed-overlay", Select)
            assert overlay.disabled is False

            builder.set_filters([{"field": SUBSCRIBED_FIELD, "op": "is", "value": "never"}])
            await pilot.pause(ASYNC_PAUSE * 2)
            assert overlay.disabled is True
            assert overlay.tooltip, "the grey-out carries the reason"

            builder.set_filters([{"field": "Title", "op": "contains", "value": "x"}])
            await pilot.pause(ASYNC_PAUSE * 2)
            assert overlay.disabled is False
            assert not overlay.tooltip


@pytest.mark.asyncio
async def test_tui_overlay_survives_a_builder_change_and_stays_out_of_the_saved_filter(mock_config_with_api):
    from src.tui import ScraperApp, SearchBuilder
    from textual.widgets import Select

    with patch('src.tui.load_config', return_value=mock_config_with_api), \
         patch('src.tui.load_tui_state', return_value={}), \
         patch('src.tui.save_tui_state'), \
         patch('src.tui.search_items', return_value=[]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            builder = app.query_one("#search-builder", SearchBuilder)
            overlay = app.query_one("#subscribed-overlay", Select)
            overlay.value = "previously"
            await pilot.pause(ASYNC_PAUSE)

            builder.set_filters([{"field": "Title", "op": "contains", "value": "x"}])
            await pilot.pause(ASYNC_PAUSE * 2)
            assert overlay.value == "previously", "a builder change must not clear the overlay"
            assert app._effective_subscribed_overlay() == "previously"

            with patch('src.tui.save_enrichment_filters') as save:
                await app.action_save_filter_for_scraper()
            written = json.loads(save.call_args.kwargs["enrichment_filters"])
            assert written == [{"field": "Title", "op": "contains", "value": "x"}], \
                "the scraper filter is the builder's rows, never the overlay"

            with patch('src.tui.save_tui_state') as save_state:
                app._initial_load_done = True
                app.save_state()
            state = save_state.call_args.args[1]
            assert state["subscribed_overlay"] == "previously"


@pytest.mark.asyncio
async def test_tui_overlay_is_restored_with_the_view_and_ignored_while_greyed_out(mock_config):
    from src.tui import ScraperApp, SearchBuilder
    from textual.widgets import Select

    state = {
        "sort_by": "title",
        "sort_order": "ASC",
        "subscribed_overlay": "downloaded",
        "filters": [{"field": "Title", "op": "contains", "value": "x"}],
    }
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.load_tui_state', return_value=state), \
         patch('src.tui.save_tui_state'), \
         patch('src.tui.search_items', return_value=[]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE * 2)
            assert app.query_one("#subscribed-overlay", Select).value == "downloaded"
            assert app._effective_subscribed_overlay() == "downloaded"

            # A restored Subscribed row disables the overlay, and the search then
            # ignores the overlay value rather than ANDing a hidden second rule.
            builder = app.query_one("#search-builder", SearchBuilder)
            builder.set_filters([{"field": SUBSCRIBED_FIELD, "op": "is", "value": "never"}])
            await pilot.pause(ASYNC_PAUSE * 2)
            overlay = app.query_one("#subscribed-overlay", Select)
            assert overlay.disabled is True
            assert overlay.value == "downloaded", "greyed out, not cleared"
            assert app._effective_subscribed_overlay() == "any"


@pytest.mark.asyncio
async def test_tui_restores_a_legacy_overlay_value_as_subscribed(mock_config):
    """`.tui_state.yaml` may still hold `currently` from before the unification."""
    from src.tui import ScraperApp
    from textual.widgets import Select

    state = {
        "sort_by": "title",
        "sort_order": "ASC",
        "subscribed_overlay": "currently",
    }
    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.load_tui_state', return_value=state), \
         patch('src.tui.save_tui_state'), \
         patch('src.tui.search_items', return_value=[]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE * 2)
            assert app.query_one("#subscribed-overlay", Select).value == "subscribed"
            assert app._effective_subscribed_overlay() == "subscribed"


# ── the detail pane shows the date, on both sides ────────────────────────────

@pytest.mark.asyncio
async def test_tui_detail_pane_shows_subscribed_at_only_when_set(mock_config):
    from src.tui import ScraperApp, DetailsPane, format_ts
    from textual.widgets import Label

    with patch('src.tui.load_config', return_value=mock_config), \
         patch('src.tui.load_tui_state', return_value={}), \
         patch('src.tui.save_tui_state'), \
         patch('src.tui.search_items', return_value=[]):
        app = ScraperApp()
        async with app.run_test() as pilot:
            await pilot.pause(ASYNC_PAUSE)
            pane = app.query_one("#detail-pane", DetailsPane)
            base = {
                "workshop_id": 7, "title": "T", "tags": "[]",
                "own_subscribed": 1, "own_first_subscribed_at": 1700000000,
                "is_queued_for_subscription": 0, "downloaded_at": None,
            }
            pane.item_data = dict(base)
            await pilot.pause(ASYNC_PAUSE)
            label = pane.query_one("#item-sub-at", Label)
            rendered = str(label.render())
            assert label.display is True
            assert "Subscribed at:" in rendered
            assert format_ts(1700000000) in rendered

            pane.item_data = dict(base, own_subscribed=0, own_first_subscribed_at=None)
            await pilot.pause(ASYNC_PAUSE)
            assert label.display is False, "never seen subscribed renders no dated line"
            assert str(label.render()) == ""


# ── the served JavaScript, run in node against its real functions ────────────

from tests.test_webserver import (  # noqa: E402
    NODE, _extract_function, _run_node, _served_inline_script, web_client,
)
node = pytest.mark.skipif(NODE is None, reason="node is not installed")


VALUE_CONTROL_DRIVER = """
const updateFn = (__FN__);
const _isEnumField = (__ISENUM__);
const _enumValueOptions = (__OPTS__);
const ENUM_FIELDS = {Subscribed: ['any', 'never', 'subscribed', 'previously', 'queued', 'downloaded']};

function makeRow(field, op) {
  const parent = {replaceChild: function(next) { row._value = next; next.parentNode = parent; }};
  function el(tag) {
    return {tagName: tag.toUpperCase(), className: '', value: '', innerHTML: '',
            type: '', placeholder: '', parentNode: parent};
  }
  const fieldEl = {value: field};
  const opEl = {value: op};
  row = {_value: el('input'), querySelector: function(sel) {
    if (sel === '.field-select') return fieldEl;
    if (sel === '.op-select') return opEl;
    if (sel === '.value-input') return this._value;
    return null;
  }};
  return row;
}
global.document = {createElement: (t) => ({tagName: t.toUpperCase(), className: '', value: '', innerHTML: '',
                                           type: '', placeholder: '', parentNode: null})};

var row = makeRow('Subscribed', 'is');
updateFn(row, 'never');
const enumTag = row._value.tagName;
const enumHtml = row._value.innerHTML;
const enumValue = row._value.value;

row = makeRow('Subscribed', 'is_not');
updateFn(row, 'never');
const notHtml = row._value.innerHTML;

row = makeRow('Title', 'contains');
updateFn(row, 'hello');
const textTag = row._value.tagName;
const textValue = row._value.value;

console.log(JSON.stringify({enumTag: enumTag, enumHtml: enumHtml, enumValue: enumValue,
                            notHtml: notHtml, textTag: textTag, textValue: textValue}));
"""


@node
def test_web_enum_field_builds_a_select_value_control(web_client, tmp_path):
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (VALUE_CONTROL_DRIVER
              .replace("__FN__", _extract_function(script, "updateValueControl"))
              .replace("__ISENUM__", _extract_function(script, "_isEnumField"))
              .replace("__OPTS__", _extract_function(script, "_enumValueOptions")))
    out = _run_node(driver, tmp_path)

    assert out["enumTag"] == "SELECT", "an enum field's value is chosen, not typed"
    assert 'value="any"' in out["enumHtml"]
    assert out["enumValue"] == "never"
    assert 'value="any"' not in out["notHtml"], "is_not must not offer `any`"
    assert out["textTag"] == "INPUT"
    assert out["textValue"] == "hello"


OVERLAY_DRIVER = """
const _builderHasSubscribedRow = (__HAS__);
const _syncSubscribedOverlay = (__SYNC__);
const _overlayValue = (__VALUE__);
const _subscribedOverlayEl = (__EL__);
const SUBSCRIBED_FIELD = 'Subscribed';
const SUBSCRIBED_VALUES = ['any', 'never', 'subscribed', 'previously', 'queued', 'downloaded'];
const overlay = {disabled: false, title: '', value: 'queued'};
let rows = [{querySelector: () => ({value: 'Title'})}];
global.document = {
  getElementById: (id) => (id === 'subscribed-overlay' ? overlay : null),
  querySelectorAll: () => rows,
};
const out = {};
_syncSubscribedOverlay();
out.before = {has: _builderHasSubscribedRow(), disabled: overlay.disabled, value: _overlayValue()};
rows = [{querySelector: () => ({value: 'Subscribed'})}];
_syncSubscribedOverlay();
out.after = {has: _builderHasSubscribedRow(), disabled: overlay.disabled, title: overlay.title, value: _overlayValue()};
rows = [{querySelector: () => ({value: 'Title'})}];
overlay.value = 'not-a-value';
out.unknown = _overlayValue();
console.log(JSON.stringify(out));
"""


@node
def test_web_overlay_is_greyed_out_and_ignored_while_a_builder_row_owns_the_field(web_client, tmp_path):
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (OVERLAY_DRIVER
              .replace("__HAS__", _extract_function(script, "_builderHasSubscribedRow"))
              .replace("__SYNC__", _extract_function(script, "_syncSubscribedOverlay"))
              .replace("__VALUE__", _extract_function(script, "_overlayValue"))
              .replace("__EL__", _extract_function(script, "_subscribedOverlayEl")))
    out = _run_node(driver, tmp_path)

    assert out["before"] == {"has": False, "disabled": False, "value": "queued"}
    assert out["after"]["has"] is True
    assert out["after"]["disabled"] is True, "a Subscribed builder row greys the overlay out"
    assert out["after"]["title"], "the grey-out carries the reason"
    assert out["after"]["value"] == "any", "a greyed-out overlay constrains nothing"
    assert out["unknown"] == "any", "an unknown stored value constrains nothing"


SAVE_FILTER_BODY_DRIVER = """
const fn = (__FN__);
let body = null;
global.alert = () => {};
global.getFilters = () => ([{field: 'Subscribed', op: 'is', value: 'never'}]);
global.fetch = async (url, opts) => { body = JSON.parse(opts.body); return {ok: true}; };
(async () => { await fn(); console.log(JSON.stringify(body)); })();
"""


@node
def test_web_save_for_scraper_writes_only_the_builder_rows(web_client, tmp_path):
    client, _ = web_client
    fn = _extract_function(_served_inline_script(client), "saveFilterAndReport")
    body = _run_node(SAVE_FILTER_BODY_DRIVER.replace("__FN__", fn), tmp_path)

    assert body == {"filters": [{"field": "Subscribed", "op": "is", "value": "never"}]}, \
        "the overlay is view state and must not reach the saved scraper filter"


RENDER_SUBSCRIBED_AT_DRIVER = """
const fn = (__FN__);
const subFn = (__SUB_FN__);
let html = '';
global.document = { getElementById: () => ({ set innerHTML(v) { html = v; } }) };
global._showTranslated = true;
global._subTitleText = 'sub';
global._favTitleText = 'fav';
global._currentDetail = null;
global.wClass = () => 'wilson-low';
global.fmtSize = () => '1 MB';
global.sizeClass = () => '';
global.fmtCount = (n) => String(n || 0);
global._escapeHtml = (s) => String(s == null ? '' : s);
global.showSubscriptionMarker = subFn;
global._refreshOpenFolderButton = () => {};
const base = {
  workshop_id: 77, creator: 'Alice', creator_id: '76561198765432109',
  personaname: 'Alice', has_translation: false,
  display_title_original: 'Mod', title: 'Mod',
  subscription_state: 'subscribed', subscription_glyph: '\\u2605',
  subscription_colour: '#ffd700', subscription_class: 'sub-subscribed',
  subscription_label: 'Currently subscribed', subscription_tooltip: 'subscribed',
  subscription_clickable: false,
  steam_created_at: 1600000000, steam_updated_at: 1600000000,
  own_subscribed: 1, own_first_subscribed_at: 1700000000,
};
fn(base);
const dated = html;
fn(Object.assign({}, base, {own_subscribed: 0, own_first_subscribed_at: null,
                            subscription_state: 'never'}));
const undated = html;
console.log(JSON.stringify({dated: dated, undated: undated}));
"""


@node
def test_web_detail_pane_shows_subscribed_at_only_when_set(web_client, tmp_path):
    client, _ = web_client
    script = _served_inline_script(client)
    driver = (RENDER_SUBSCRIBED_AT_DRIVER
              .replace("__FN__", _extract_function(script, "renderDetail"))
              .replace("__SUB_FN__", _extract_function(script, "showSubscriptionMarker")))
    out = _run_node(driver, tmp_path)

    assert "Subscribed at:" in out["dated"]
    assert "2023-11-14" in out["dated"], "the stamp is the first-seen-subscribed date"
    assert "Subscribed at:" not in out["undated"], \
        "an item never seen subscribed must not gain a dated line"


def test_the_detail_payload_carries_the_first_subscribed_stamp(web_client):
    """The pane can only render what the payload ships, on both sides."""
    client, db_path = web_client
    insert_or_update_item(db_path, {"workshop_id": 88, "title": "Dated", "fetch_status": 200,
                                    "own_subscribed": 1, "own_first_subscribed_at": 1700000000})
    data = client.get('/api/item/88').get_json()
    assert data["own_first_subscribed_at"] == 1700000000
