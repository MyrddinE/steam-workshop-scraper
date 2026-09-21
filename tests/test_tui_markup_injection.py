"""Steam text is data, not Rich markup.

Titles, tag names and persona names are full of square brackets -- *measured
live*, 129,533 of the library's titles hold a bracket pair, and 2 of the 9 items
queued for subscription did -- and a bracket pair is markup to
`Text.from_markup`. The subscription queue built its row with
``RichText.from_markup(f"[link={url}]{url}[/link] : {title}")``, so a title that
is not a valid style (``[najar]偶像大师 樋口円香（有断面+配音版）``) raised
``MissingStyle: Failed to get style 'najar'`` and took the whole screen down.
The same interpolation reached the detail pane, the list rows and the stats tag
table, where an unknown tag is silently swallowed instead.

These tests render the offending strings and assert two things: the render does
not raise, and the brackets are still on screen. This is a rendering fix, not a
stripping fix -- a title that comes back without its ``[najar]`` has had data
eaten out of it rather than protected from the parser. Against the old
interpolation the tests that cover a tag-shaped title fail, either on the
``MissingStyle`` crash (the queue screen, which parses with Rich) or on the
brackets coming back missing (the Textual-parsed widgets).
"""

import json
from unittest.mock import patch

import pytest
from textual.app import App, ComposeResult
from textual.content import Content
from textual.widgets import Label, Markdown, Static

from src import pending
from src import subscription
from src.tui import (
    DetailsPane,
    SubscriptionQueueScreen,
    WorkshopItem,
    escape_markup,
)
from tests.conftest import ASYNC_PAUSE


# The exact reported title first, then one string per markup shape that can
# reach the parser: a real style, a closing tag, a link, an unbalanced open
# bracket, and a bare close bracket.
HOSTILE_TEXT = [
    "[najar]偶像大师 樋口円香（有断面+配音版）",
    "[bold]looks like markup[/bold]",
    "[/]",
    "[link=https://x]click[/link]",
    "unbalanced [ bracket",
    "bare ] bracket",
    "[b]both[/b] and [najar]",
]


def _plain(widget) -> str:
    """The text a widget will actually draw, with markup resolved."""
    return str(widget.render())


def test_escape_markup_escapes_every_bracket_not_just_tag_shaped_ones():
    """`rich.markup.escape` leaves an unbalanced `[` standing; this must not."""
    assert escape_markup("[najar]x") == "\\[najar]x"
    assert escape_markup("unbalanced [ bracket") == "unbalanced \\[ bracket"
    assert escape_markup("bare ] bracket") == "bare ] bracket"


@pytest.mark.parametrize("hostile", HOSTILE_TEXT)
def test_the_escaped_text_round_trips_through_the_textual_parser(hostile):
    assert Content.from_markup(escape_markup(hostile)).plain == hostile


def _item(**overrides) -> dict:
    """A workshop item shaped like a search row, for the detail pane."""
    item = {
        "workshop_id": 3700100995,
        "title": "Plain Title",
        "personaname": "Plain Creator",
        "tags": "[]",
        "short_description": "Plain description",
        "own_subscribed": 0,
        "is_queued_for_subscription": 0,
        "own_first_subscribed_at": None,
    }
    item.update(overrides)
    return item


class _DetailPaneApp(App):
    def compose(self) -> ComposeResult:
        yield DetailsPane()


class _RowApp(App):
    def __init__(self, item: dict):
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        yield WorkshopItem(self.item)


# ---------------------------------------------------------------------------
# the reported crash: the subscription queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("title", HOSTILE_TEXT)
async def test_the_subscription_queue_renders_a_hostile_title_literally(tmp_path, title):
    screen = SubscriptionQueueScreen(
        str(tmp_path / "queue.db"), str(tmp_path / "pause.lock")
    )
    app = App()
    with patch(
        "src.tui.get_subscription_queue_items",
        return_value=[{"workshop_id": 3700100995, "title": title}],
    ):
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause(ASYNC_PAUSE)
            drawn = " ".join(_plain(w) for w in screen.query(Static))

    assert title in drawn, "the title's brackets must be displayed, not eaten"


# ---------------------------------------------------------------------------
# the same text through the detail pane: title, creator, tags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", HOSTILE_TEXT)
async def test_the_detail_pane_renders_a_hostile_title(hostile):
    app = _DetailPaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(DetailsPane)
        pane.item_data = _item(title=hostile)
        await pilot.pause(ASYNC_PAUSE)
        drawn = _plain(pane.query_one("#item-title", Label))

    assert hostile in drawn


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", HOSTILE_TEXT)
async def test_the_detail_pane_renders_a_hostile_creator(hostile):
    app = _DetailPaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(DetailsPane)
        pane.item_data = _item(personaname=hostile)
        await pilot.pause(ASYNC_PAUSE)
        drawn = _plain(pane.query_one("#item-creator", Label))

    assert hostile in drawn


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", HOSTILE_TEXT)
async def test_the_detail_pane_renders_hostile_tags(hostile):
    app = _DetailPaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(DetailsPane)
        pane.item_data = _item(tags=json.dumps([hostile, "ordinary"]))
        await pilot.pause(ASYNC_PAUSE)
        drawn = _plain(pane.query_one("#stat-tags", Label))

    assert hostile in drawn
    assert "ordinary" in drawn, "the escaped value must not swallow its neighbours"


# ---------------------------------------------------------------------------
# it is a rendering fix, not a stripping fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bracketed_title_still_displays_its_brackets():
    """The exact reported case: the data must survive, not merely not crash."""
    reported = "[najar]偶像大师 樋口円香（有断面+配音版）"
    app = _DetailPaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(DetailsPane)
        pane.item_data = _item(title=reported)
        await pilot.pause(ASYNC_PAUSE)
        drawn = _plain(pane.query_one("#item-title", Label))

    assert "[najar]" in drawn, "the tag-shaped data was eaten, not escaped"
    assert drawn == reported, "the title must be drawn character for character"


# ---------------------------------------------------------------------------
# the project's own markup still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_detail_pane_keeps_the_projects_own_markup():
    item = _item(title="[najar]Styled Title", is_queued_for_subscription=1)
    app = _DetailPaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(DetailsPane)
        pane.item_data = item
        await pilot.pause(ASYNC_PAUSE)

        title = pane.query_one("#item-title", Label).render()
        assert str(title) == "[najar]Styled Title"
        assert any(span.style == "b" for span in title.spans), "the title is still bold"

        state = subscription.subscription_state(item)
        glyph, colour, _css, _label = subscription.marker_spec(state)
        marker = pane.query_one("#item-sub-marker", Label).render()
        assert str(marker) == glyph
        assert any(span.style == colour for span in marker.spans), (
            "the subscription marker is still coloured"
        )


# ---------------------------------------------------------------------------
# the other interpolation sites the audit found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", HOSTILE_TEXT)
async def test_the_list_row_renders_a_hostile_title_and_creator(hostile):
    app = _RowApp(_item(title=hostile, personaname=hostile))
    async with app.run_test() as pilot:
        await pilot.pause(ASYNC_PAUSE)
        row = app.query_one(WorkshopItem)
        drawn = " ".join(_plain(w) for w in row.query(Label))

    assert hostile in drawn


# ---------------------------------------------------------------------------
# the translation-request notice: its own element, not markdown
# ---------------------------------------------------------------------------


def _pane_text(pane) -> str:
    """The detail text as drawn, with markup resolved.

    `Markdown` renders through child blocks that are `Static` widgets, so
    querying `Static` reaches the description as the reader receives it rather
    than only the source string handed to `Markdown.update`.
    """
    return "\n".join(_plain(widget) for widget in pane.query(Static))


@pytest.mark.asyncio
async def test_a_queued_item_draws_the_translation_notice_as_its_own_element():
    """The notice is an element, not a Rich tag inside the markdown.

    `Markdown` does not interpret Rich markup, so the old
    `> *[yellow]Translation requested...[/yellow]*` blockquote reached the reader
    with the brackets showing. The wording now comes from `src/pending.py`, the
    same string the web pane prints, and the emphasis is Textual markup resolved
    by the `Label`.
    """
    item = _item(translation_priority=5, translate_version=None,
                 short_description="The original description")
    app = _DetailPaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(DetailsPane)
        pane.item_data = item
        await pilot.pause(ASYNC_PAUSE)

        drawn = _pane_text(pane)
        assert "The original description" in drawn, "the description still renders"
        assert "[yellow]" not in drawn, "the tag must not reach the reader as text"
        assert pending.TRANSLATION_REQUESTED_NOTICE in drawn
        assert "[yellow]" not in pane.query_one("#detail-content", Markdown)._markdown

        notice = pane.query_one("#translation-notice", Label).render()

    assert str(notice) == pending.TRANSLATION_REQUESTED_NOTICE
    assert any("italic" in str(span.style) for span in notice.spans), \
        "the notice keeps the web pane's italic emphasis"


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [
    _item(translation_priority=5, translate_version=7),
    _item(translation_priority=0),
])
async def test_the_notice_stays_hidden_when_it_does_not_apply(item):
    """Issue 42 did not change when the notice appears."""
    app = _DetailPaneApp()
    async with app.run_test() as pilot:
        pane = app.query_one(DetailsPane)
        pane.item_data = item
        await pilot.pause(ASYNC_PAUSE)

        notice = pane.query_one("#translation-notice", Label)

        assert str(notice.render()) == ""
        assert not notice.display
